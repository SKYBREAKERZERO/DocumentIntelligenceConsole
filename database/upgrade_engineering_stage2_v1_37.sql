-- Stage 2 v1.37: fail-closed database citation semantics, PostgreSQL 17.
-- Opt-in per-engineering database. Apply AFTER v1.33 controlled review migration.
-- Execute as the existing non-login review function OWNER (or vetted DBA),
-- NOT via an application/reviewer login. It changes no candidate statuses,
-- review audits, data grants, or historical acceptance claims.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='90s';
DO $body$
DECLARE owner_oid oid;
BEGIN
  IF current_setting('server_version_num')::integer < 170000
     OR (SELECT count(*) FROM public.project_info) <> 1
     OR NOT EXISTS (SELECT 1 FROM public.project_info
                    WHERE upper(project_code) NOT IN ('QABUG','RAG_QABUG'))
     OR to_regprocedure('dic_review.apply_content_relation_review(bigint,text,text,text,text,text,text)') IS NULL
     OR to_regprocedure('dic_review.guard_relation_write()') IS NULL
     OR to_regprocedure('dic_review.guard_audit_write()') IS NULL
     OR to_regclass('public.ei_requirement_units') IS NULL
     OR to_regclass('public.ei_content_relations') IS NULL THEN
    RAISE EXCEPTION 'v1.37 requires PostgreSQL 17 engineering DB and installed v1.33 trusted review';
  END IF;
  SELECT p.proowner INTO owner_oid FROM pg_catalog.pg_proc p
    WHERE p.oid=to_regprocedure('dic_review.apply_content_relation_review(bigint,text,text,text,text,text,text)');
  IF owner_oid IS NULL OR owner_oid <> (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_user)
     OR EXISTS (SELECT 1 FROM pg_catalog.pg_roles r
                WHERE r.oid=owner_oid AND (r.rolcanlogin OR r.rolsuper OR r.rolcreaterole OR r.rolbypassrls))
  THEN RAISE EXCEPTION 'Run migration only as vetted non-login trusted review function owner'; END IF;
  IF (SELECT p.proowner FROM pg_catalog.pg_proc p
      WHERE p.oid=to_regprocedure('dic_review.guard_relation_write()')) <> owner_oid THEN
    RAISE EXCEPTION 'Relation guard and review authority do not share trusted owner';
  END IF;
  IF NOT EXISTS (
      SELECT 1 FROM pg_catalog.pg_proc p
      WHERE p.oid=to_regprocedure('dic_review.apply_content_relation_review(bigint,text,text,text,text,text,text)')
        AND p.prosecdef AND 'search_path=pg_catalog'=ANY(p.proconfig)
        AND NOT EXISTS (
          SELECT 1 FROM pg_catalog.aclexplode(COALESCE(p.proacl,
                                  pg_catalog.acldefault('f',p.proowner))) a
          WHERE a.grantee=0 AND a.privilege_type='EXECUTE')) THEN
    RAISE EXCEPTION 'Existing SECURITY DEFINER authority is not trusted';
  END IF;
  IF (SELECT count(*) FROM pg_catalog.pg_trigger t
      JOIN pg_catalog.pg_class c ON c.oid=t.tgrelid
      JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
      WHERE n.nspname='public' AND c.relname='ei_relation_review_audit'
        AND t.tgname='dic_stage2_audit_guard' AND t.tgenabled='O'
        AND t.tgfoid=to_regprocedure('dic_review.guard_audit_write()')
        AND t.tgtype=31) <> 1 THEN
    RAISE EXCEPTION 'Expected append-only audit guard is missing or disabled';
  END IF;
END $body$;

-- v1.37 server-side conservative citation proof. PostgreSQL ARE syntax is
-- intentionally a supported SUBSET of the Python citation parser: false
-- negatives are review-only, not permission to infer missing dependencies.
-- Always use bounded SOURCE text, dynamic patterns only after validation.
CREATE OR REPLACE FUNCTION dic_review.has_unambiguous_section_reference(
   p_source TEXT, p_code TEXT, p_section TEXT
) RETURNS BOOLEAN LANGUAGE plpgsql IMMUTABLE
SET search_path=pg_catalog AS $body$
DECLARE
   code text := upper(btrim(p_code));
   section text := btrim(regexp_replace(btrim(p_section),'^§[[:space:]]*','',''));
   clause text;
   ref_prefix text :=
       '(?:refer[[:space:]]+to|reference[[:space:]]+to|according[[:space:]]+to|defined[[:space:]]+in|specified[[:space:]]+in|see|spec(?:ification)?(?:[[:space:]]+no[.])?|参照|引用|依据|準拠|根据)[[:space:]]*(?:the[[:space:]]+)?(?:spec(?:ification)?[[:space:]]*)?';
   section_pattern text :=
       '(?:section|sec[.]?|§|章节|節)[[:space:]]*[1-9][0-9]*(?:[.][0-9]+){0,6}';
   reference_count bigint;
   section_count bigint;
   explicit_spec_count bigint;
BEGIN
   IF p_source IS NULL OR p_code IS NULL OR p_section IS NULL
      OR length(p_source)=0 OR length(p_source)>32768
      OR code !~ '^[A-Z]{0,3}[0-9]{3,6}$'
      OR section !~ '^[1-9][0-9]*(?:[.][0-9]+){0,6}$' THEN
      RETURN FALSE;
   END IF;
   -- Normalize only the abbreviation's separator for sentence splitting;
   -- never alter the source stored on the relation or its evidence hash.
   FOR clause IN SELECT regexp_split_to_table(
       regexp_replace(p_source,'(?i)(sec)[.]([[:space:]])','\1\2','g'),
       '[。！？;；[:cntrl:]]|[.!?][[:space:]]+') LOOP
       -- Refuse segments with more than one explicitly introduced code,
       -- or more than one section, even if one pair appears to match.
       SELECT count(*) INTO reference_count FROM regexp_matches(clause,
           ref_prefix || '[A-Z]{0,3}[0-9]{3,6}([^[:alnum:]_]|$)','gi');
       SELECT count(*) INTO section_count FROM regexp_matches(clause,
           section_pattern || '([^[:digit:].]|$)','gi');
       SELECT count(*) INTO explicit_spec_count FROM regexp_matches(clause,
           'spec(?:ification)?[[:space:]]+[A-Z]{0,3}[0-9]{3,6}([^[:alnum:]_]|$)','gi');
       IF reference_count=1 AND section_count=1 AND explicit_spec_count <= 1 AND
          -- The section must FOLLOW that spec anchor within 120 characters.
          -- Mere co-occurrence (or a heading before the reference) is unsafe.
          clause ~* ('(^|[^[:alnum:]_])' || ref_prefix || code ||
                     '([^[:alnum:]_]).{0,120}(?:section|sec[.]?|§|章节|節)[[:space:]]*' ||
                     section || '([^[:digit:].]|$)') THEN
          RETURN TRUE;
       END IF;
   END LOOP;
   RETURN FALSE;
END $body$;

CREATE OR REPLACE FUNCTION dic_review.guard_relation_write()
RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $body$
DECLARE
  fn_owner name;
  source_literal text;
  target_code text;
  target_section text;
  target_version bigint;
  matching_target_units bigint;
BEGIN
  SELECT pg_get_userbyid(p.proowner) INTO fn_owner FROM pg_catalog.pg_proc p
  WHERE p.oid = 'dic_review.apply_content_relation_review(bigint,text,text,text,text,text,text)'::regprocedure;
  IF fn_owner IS NULL THEN RAISE EXCEPTION 'Missing controlled review authority'; END IF;
  IF TG_OP = 'INSERT' THEN
    IF NEW.status <> 'REVIEW_REQUIRED' OR NEW.reviewed_at IS NOT NULL OR NEW.reviewed_by IS NOT NULL THEN
      RAISE EXCEPTION 'Proposals must start REVIEW_REQUIRED without review identity';
    END IF;
    RETURN NEW;
  END IF;
  IF TG_OP = 'DELETE' THEN
    IF current_user <> fn_owner THEN RAISE EXCEPTION 'Direct relation deletion forbidden'; END IF;
    RETURN OLD;
  END IF;
  IF current_user <> fn_owner THEN
    RAISE EXCEPTION 'Direct relation update forbidden';
  END IF;
  -- v1.37: the database independently validates *references*, even when a
  -- caller skips Python and invokes the SECURITY DEFINER authority directly.
  -- A reference proves only a citation, NEVER a functional dependency.
  IF NEW.status='CONFIRMED' AND NEW.relation_type='REFERENCES_SECTION'
     AND (OLD.status IS DISTINCT FROM NEW.status
          OR OLD.source_unit_id IS DISTINCT FROM NEW.source_unit_id
          OR OLD.target_unit_id IS DISTINCT FROM NEW.target_unit_id
          OR OLD.relation_type IS DISTINCT FROM NEW.relation_type) THEN
    SELECT su.raw_text, sp.spec_code, tu.section_id, tu.version_id
      INTO source_literal,target_code,target_section,target_version
      FROM public.ei_requirement_units su
      JOIN public.ei_requirement_units tu ON tu.id=NEW.target_unit_id
      JOIN public.specifications sp ON sp.id=tu.specification_id
      WHERE su.id=NEW.source_unit_id;
    IF NOT FOUND OR source_literal IS NULL OR target_code IS NULL
       OR target_section IS NULL
       OR NOT dic_review.has_unambiguous_section_reference(
                  source_literal,target_code,target_section) THEN
       RAISE EXCEPTION 'Stage 2 DB reference anchor missing or ambiguous';
    END IF;
    SELECT count(*) INTO matching_target_units
      FROM public.ei_requirement_units u
      WHERE u.version_id=target_version
        AND btrim(regexp_replace(u.section_id,'^§[[:space:]]*','','')) =
            btrim(regexp_replace(target_section,'^§[[:space:]]*','',''));
    IF matching_target_units <> 1 THEN
      RAISE EXCEPTION 'Stage 2 DB target section does not uniquely identify one requirement';
    END IF;
  END IF;
  RETURN NEW;
END $body$;

REVOKE ALL ON FUNCTION dic_review.has_unambiguous_section_reference(TEXT,TEXT,TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION dic_review.guard_relation_write() FROM PUBLIC;
-- Leave the existing guard trigger, review function, grants and audit history intact.
-- This upgrade must FAIL rather than silently CREATE a missing/disabled trigger.
DO $body$
BEGIN
  IF (SELECT count(*) FROM pg_catalog.pg_trigger t
      JOIN pg_catalog.pg_class c ON c.oid=t.tgrelid
      JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
      WHERE n.nspname='public' AND c.relname='ei_content_relations'
        AND t.tgname='dic_stage2_relation_guard' AND t.tgenabled='O'
        AND t.tgfoid=to_regprocedure('dic_review.guard_relation_write()')
        AND t.tgtype=31) <> 1 THEN
    RAISE EXCEPTION 'Active v1.33 relation write guard not found; migration aborted';
  END IF;
END $body$;
COMMIT;
