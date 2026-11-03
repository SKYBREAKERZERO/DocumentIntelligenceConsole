-- DocumentIntelligenceConsole v1.33 / Stage 2 controlled review, PostgreSQL >= 14.
-- Install with a dedicated, non-login DDL/owner role, on ONE engineering DB at a time.
-- REQUIRED: existing v7 tables, trusted backups, explicit operator authorization.
-- This migration DOES NOT grant any human access or silently modify existing grants.
-- Run verify_engineering_stage2_v1_33.sql and manually review direct table grants.
BEGIN;
DO $$
BEGIN
  IF pg_catalog.pg_encoding_to_char((SELECT encoding FROM pg_catalog.pg_database WHERE datname=current_database())) <> 'UTF8'
     OR (SELECT count(*) FROM public.project_info) <> 1
     OR NOT EXISTS (SELECT 1 FROM public.project_info WHERE upper(project_code) NOT IN ('QABUG','RAG_QABUG'))
     OR to_regclass('public.ei_content_relations') IS NULL
     OR to_regclass('public.ei_relation_review_audit') IS NULL
     OR to_regclass('public.ei_version_backfill_receipts') IS NULL
     OR to_regclass('public.ei_stage1_enrichment_receipts') IS NULL THEN
    RAISE EXCEPTION 'Stage 2 migration requires exactly one engineering project and complete v7 schema';
  END IF;
END $$;

CREATE SCHEMA IF NOT EXISTS dic_review;
REVOKE ALL ON SCHEMA dic_review FROM PUBLIC;
CREATE TABLE IF NOT EXISTS dic_review.authorized_reviewers (
   login_name NAME PRIMARY KEY,
   approved_by TEXT NOT NULL CHECK (length(btrim(approved_by)) >= 2),
   justification TEXT NOT NULL CHECK (length(btrim(justification)) >= 12),
   enrolled_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
   disabled_at TIMESTAMPTZ
);
REVOKE ALL ON dic_review.authorized_reviewers FROM PUBLIC;

-- Historical v1.31/v1.32 actor fields are self-declared. They are preserved,
-- but must never be retroactively treated as authenticated DB approvals.
ALTER TABLE public.ei_relation_review_audit
  ADD COLUMN IF NOT EXISTS identity_provenance TEXT NOT NULL DEFAULT 'UNVERIFIED_LEGACY';
ALTER TABLE public.ei_relation_review_audit
  DROP CONSTRAINT IF EXISTS ck_dic_review_identity_provenance;
ALTER TABLE public.ei_relation_review_audit
  ADD CONSTRAINT ck_dic_review_identity_provenance
  CHECK (identity_provenance IN ('UNVERIFIED_LEGACY','DB_SESSION_USER'));

-- The trigger deliberately executes as INVOKER: within SECURITY DEFINER it
-- observes the controlled function's OWNER; in a direct client DML it sees
-- the client role. Database owners/superusers remain trusted administrators.
CREATE OR REPLACE FUNCTION dic_review.guard_relation_write()
RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $body$
DECLARE fn_owner name;
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
  RETURN NEW;
END $body$;

CREATE OR REPLACE FUNCTION dic_review.guard_audit_write()
RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $body$
DECLARE fn_owner name;
BEGIN
  IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'Review audit events are append-only'; END IF;
  SELECT pg_get_userbyid(p.proowner) INTO fn_owner FROM pg_catalog.pg_proc p
  WHERE p.oid = 'dic_review.apply_content_relation_review(bigint,text,text,text,text,text,text)'::regprocedure;
  IF fn_owner IS NULL OR current_user <> fn_owner THEN
    RAISE EXCEPTION 'Audit insert allowed only via controlled review owner';
  END IF;
  RETURN NEW;
END $body$;

CREATE OR REPLACE FUNCTION dic_review.apply_content_relation_review(
   p_candidate_id BIGINT, p_project TEXT, p_expected TEXT, p_new TEXT,
   p_reason TEXT, p_source_hash TEXT, p_target_hash TEXT
) RETURNS BIGINT LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog AS $body$
DECLARE
  edge record;
  identity_count integer;
  audit_status text;
  checked_version_id bigint;
  receipt record;
  source_doc record;
  actor text := session_user::text;
  affected bigint;
BEGIN
  -- Caller must be a *login* expressly enrolled by DBA, not merely a SET ROLE
  -- or a spoofed p_actor string. Only an enrolled reviewer gets function EXECUTE.
  IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles
             WHERE rolname=session_user AND (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)) THEN
    RAISE EXCEPTION 'Privileged administrator identities cannot sign human relation reviews';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM dic_review.authorized_reviewers
                 WHERE login_name=session_user AND disabled_at IS NULL) THEN
    RAISE EXCEPTION 'Authenticated reviewer login is not authorized';
  END IF;
  IF p_candidate_id IS NULL OR p_candidate_id < 1 OR p_project IS NULL
     OR length(btrim(p_project)) = 0 OR upper(p_project) IN ('QABUG','RAG_QABUG')
     OR p_expected NOT IN ('REVIEW_REQUIRED','CONFIRMED','REJECTED')
     OR p_new NOT IN ('REVIEW_REQUIRED','CONFIRMED','REJECTED')
     OR NOT ((p_expected='REVIEW_REQUIRED' AND p_new IN ('CONFIRMED','REJECTED'))
        OR (p_expected IN ('CONFIRMED','REJECTED') AND p_new='REVIEW_REQUIRED'))
     OR p_reason IS NULL OR length(btrim(p_reason)) NOT BETWEEN 12 AND 2000
     OR p_source_hash IS NULL OR p_target_hash IS NULL
     OR p_source_hash !~ '^[0-9a-f]{64}$' OR p_target_hash !~ '^[0-9a-f]{64}$' THEN
    RAISE EXCEPTION 'Invalid Stage 2 review preconditions';
  END IF;
  SELECT count(*) INTO identity_count FROM public.project_info
    WHERE project_code = p_project;
  IF identity_count <> 1 OR (SELECT count(*) FROM public.project_info) <> 1 THEN
    RAISE EXCEPTION 'Database engineering project identity mismatch';
  END IF;

  -- Lock the candidate and both specification heads, to serialize version
  -- switches with the audit transition. Caller must COMMIT/ROLLBACK atomically.
  SELECT r.id, r.status, r.source_unit_id, r.target_unit_id,
         su.version_id AS source_version, tu.version_id AS target_version,
         su.specification_id AS source_spec, tu.specification_id AS target_spec,
         sv.specification_id AS sv_spec, tv.specification_id AS tv_spec,
         src.current_version_id AS source_current, tgt.current_version_id AS target_current,
         src.project_code AS source_project, tgt.project_code AS target_project,
         sv.document_id AS source_primary_doc, tv.document_id AS target_primary_doc,
         su.document_id AS source_document, tu.document_id AS target_document,
         su.raw_text AS source_text, tu.raw_text AS target_text,
         su.text_hash AS source_hash, tu.text_hash AS target_hash,
         su.start_offset AS source_start, su.end_offset AS source_end,
         tu.start_offset AS target_start, tu.end_offset AS target_end,
         sc.document_id AS source_chunk_doc, tc.document_id AS target_chunk_doc,
         sc.content AS source_chunk, tc.content AS target_chunk,
         sd.file_name AS source_file, td.file_name AS target_file,
         sd.metadata AS source_meta, td.metadata AS target_meta
    INTO edge
    FROM public.ei_content_relations r
    JOIN public.ei_requirement_units su ON su.id=r.source_unit_id
    JOIN public.ei_requirement_units tu ON tu.id=r.target_unit_id
    JOIN public.specifications src ON src.id=su.specification_id
    JOIN public.specifications tgt ON tgt.id=tu.specification_id
    JOIN public.specification_versions sv ON sv.id=su.version_id
    JOIN public.specification_versions tv ON tv.id=tu.version_id
    JOIN public.contents sc ON sc.id=su.content_id
    JOIN public.contents tc ON tc.id=tu.content_id
    JOIN public.documents sd ON sd.document_id=su.document_id
    JOIN public.documents td ON td.document_id=tu.document_id
    WHERE r.id=p_candidate_id FOR UPDATE OF r, src, tgt, su, tu, sv, tv, sc, tc, sd, td;
  IF NOT FOUND OR edge.status IS DISTINCT FROM p_expected THEN
    RAISE EXCEPTION 'Candidate absent or changed since preview';
  END IF;
  IF edge.source_project IS DISTINCT FROM p_project OR edge.target_project IS DISTINCT FROM p_project
     OR edge.source_version IS DISTINCT FROM edge.source_current
     OR edge.target_version IS DISTINCT FROM edge.target_current
     OR edge.source_spec IS DISTINCT FROM edge.sv_spec OR edge.target_spec IS DISTINCT FROM edge.tv_spec
     OR edge.source_primary_doc IS DISTINCT FROM edge.source_document
     OR edge.target_primary_doc IS DISTINCT FROM edge.target_document
     OR edge.source_chunk_doc IS DISTINCT FROM edge.source_document
     OR edge.target_chunk_doc IS DISTINCT FROM edge.target_document
     OR edge.source_unit_id=edge.target_unit_id
     OR edge.source_text IS NULL OR edge.target_text IS NULL
     OR length(edge.source_text)=0 OR length(edge.target_text)=0
     OR edge.source_start < 0 OR edge.target_start < 0
     OR edge.source_end <= edge.source_start OR edge.target_end <= edge.target_start
     OR substring(edge.source_chunk FROM edge.source_start+1 FOR edge.source_end-edge.source_start) IS DISTINCT FROM edge.source_text
     OR substring(edge.target_chunk FROM edge.target_start+1 FOR edge.target_end-edge.target_start) IS DISTINCT FROM edge.target_text
     OR btrim(edge.source_hash::text) IS DISTINCT FROM p_source_hash
     OR btrim(edge.target_hash::text) IS DISTINCT FROM p_target_hash THEN
    RAISE EXCEPTION 'Source/target evidence invalid, drifted or historical';
  END IF;
  -- NFKC source SHA-256 parity with Python normalize_text, on PostgreSQL 17.
  IF encode(sha256(convert_to(btrim(regexp_replace(normalize(edge.source_text,NFKC), '[[:space:]]+', ' ', 'g')), 'UTF8')),'hex') <> p_source_hash
     OR encode(sha256(convert_to(btrim(regexp_replace(normalize(edge.target_text,NFKC), '[[:space:]]+', ' ', 'g')), 'UTF8')),'hex') <> p_target_hash THEN
    RAISE EXCEPTION 'Source/target SHA-256 mismatch';
  END IF;
  -- Fail closed for known coversheets/approval records, regardless of role.
  FOREACH checked_version_id IN ARRAY ARRAY[1,2] LOOP
    IF checked_version_id=1 THEN SELECT edge.source_file AS file_name, edge.source_meta AS metadata INTO source_doc;
    ELSE SELECT edge.target_file AS file_name, edge.target_meta AS metadata INTO source_doc; END IF;
    IF upper(coalesce(nullif(btrim(source_doc.metadata->>'document_role'),''),'PRIMARY')) NOT IN
       ('PRIMARY','SPEC_BODY','PRIMARY_CONTENT')
       OR upper(btrim(coalesce(source_doc.metadata->>'qa_index_policy','INCLUDE')))='EXCLUDE'
       OR coalesce(source_doc.metadata->>'supporting_document_type','') <> ''
       OR coalesce(source_doc.file_name,'') ~* 
       '(cover[ _-]*sheet|coversheet|check[ _-]*sheet|checksheet|check[ _-]*list|checklist|test[ _-]*sheet|testsheet|review[ _-]*sheet|reviewsheet|evidence[ _-]*sheet|evidencesheet|reference[ _-]*list|referencelist|change[ _-]*request|changerequest|summary[ _-]*sheet|summarysheet|表紙|封面|チェック[ _-]*シート|チェックリスト|テスト[ _-]*シート|試験[ _-]*シート|レビュー[ _-]*シート|エビデンス[ _-]*シート|参照[ _-]*一覧|参考[ _-]*一覧|変更[ _-]*依頼|サマリ[ー]?[ _-]*シート|添付[ _-]*資料|添付資料|添付書類|attachment[ _-]*material)'
    THEN RAISE EXCEPTION 'Non-primary source document cannot be reviewed'; END IF;
  END LOOP;
  FOR checked_version_id IN SELECT DISTINCT v FROM unnest(ARRAY[edge.source_version,edge.target_version]) AS parts(v) LOOP
    SELECT rb.document_id, rb.source_chunk_count, rb.source_unit_count, rb.max_content_id,
           rb.extractor_version, rb.source_digest,
           sv.document_id AS version_document,
           (SELECT count(*) FROM public.contents c WHERE c.document_id=sv.document_id) AS n_chunks,
           (SELECT coalesce(max(c.id),0) FROM public.contents c WHERE c.document_id=sv.document_id) AS max_chunk,
           (SELECT count(*) FROM public.ei_requirement_units u WHERE u.version_id=sv.id) AS n_units
      INTO receipt FROM public.specification_versions sv
      LEFT JOIN public.ei_version_backfill_receipts rb ON rb.version_id=sv.id
      WHERE sv.id=checked_version_id;
    IF NOT FOUND OR receipt.document_id IS DISTINCT FROM receipt.version_document
       OR receipt.source_chunk_count IS DISTINCT FROM receipt.n_chunks
       OR receipt.source_unit_count IS DISTINCT FROM receipt.n_units
       OR receipt.max_content_id IS DISTINCT FROM receipt.max_chunk
       OR receipt.extractor_version NOT IN ('requirement-units-v1','requirement-units-v2') THEN
      RAISE EXCEPTION 'Incomplete or drifted source-version backfill';
    END IF;
    IF receipt.extractor_version='requirement-units-v1' AND NOT EXISTS (
         SELECT 1 FROM public.ei_stage1_enrichment_receipts er
         WHERE er.version_id=checked_version_id AND er.extractor_version='requirement-units-v2'
           AND er.source_digest=receipt.source_digest AND er.source_unit_count=receipt.source_unit_count
    ) THEN RAISE EXCEPTION 'Legacy source requires Stage 1 enrichment receipt'; END IF;
  END LOOP;
  SELECT a.new_status INTO audit_status FROM public.ei_relation_review_audit a
     WHERE a.candidate_id=p_candidate_id ORDER BY a.id DESC LIMIT 1;
  IF (edge.status='REVIEW_REQUIRED' AND audit_status IS NOT NULL AND audit_status<>'REVIEW_REQUIRED')
      OR (edge.status<>'REVIEW_REQUIRED' AND audit_status IS DISTINCT FROM edge.status) THEN
    RAISE EXCEPTION 'Audit head and candidate state diverged';
  END IF;

  UPDATE public.ei_content_relations
    SET status=p_new, reviewed_at=clock_timestamp(), reviewed_by=actor
    WHERE id=p_candidate_id AND status=p_expected RETURNING id INTO affected;
  IF affected IS NULL THEN RAISE EXCEPTION 'Review raced with another transaction'; END IF;
  INSERT INTO public.ei_relation_review_audit
    (candidate_id,actor,previous_status,new_status,reason,identity_provenance)
    VALUES (p_candidate_id,actor,p_expected,p_new,btrim(p_reason),'DB_SESSION_USER');
  RETURN affected;
END $body$;

REVOKE ALL ON FUNCTION dic_review.apply_content_relation_review(BIGINT,TEXT,TEXT,TEXT,TEXT,TEXT,TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION dic_review.guard_relation_write() FROM PUBLIC;
REVOKE ALL ON FUNCTION dic_review.guard_audit_write() FROM PUBLIC;

DROP TRIGGER IF EXISTS dic_stage2_relation_guard ON public.ei_content_relations;
CREATE TRIGGER dic_stage2_relation_guard BEFORE INSERT OR UPDATE OR DELETE ON public.ei_content_relations
 FOR EACH ROW EXECUTE FUNCTION dic_review.guard_relation_write();
DROP TRIGGER IF EXISTS dic_stage2_audit_guard ON public.ei_relation_review_audit;
CREATE TRIGGER dic_stage2_audit_guard BEFORE INSERT OR UPDATE OR DELETE ON public.ei_relation_review_audit
 FOR EACH ROW EXECUTE FUNCTION dic_review.guard_audit_write();
COMMIT;
