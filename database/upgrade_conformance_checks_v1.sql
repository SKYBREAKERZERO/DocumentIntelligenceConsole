-- Optional QABUG-only, additive controlled applicability/check configuration.
-- Apply after v1.38 as a vetted non-login owner. No automatic grants/enrollment.
-- Both migration and operator approval are separate from runtime read-only Q&A.
BEGIN;
DO $$ BEGIN
 IF to_regclass('public.qabug_software_spec_versions') IS NULL OR to_regclass('public.specifications') IS NOT NULL THEN
   RAISE EXCEPTION 'Requires dedicated QABUG v1.38 schema'; END IF;
END $$;
CREATE SCHEMA IF NOT EXISTS dic_conformance;
REVOKE ALL ON SCHEMA dic_conformance FROM PUBLIC;
CREATE TABLE IF NOT EXISTS dic_conformance.authorized_reviewers(
 login_name NAME PRIMARY KEY, approved_by TEXT NOT NULL,justification TEXT NOT NULL CHECK(length(btrim(justification))>=12),disabled_at TIMESTAMPTZ);
CREATE TABLE IF NOT EXISTS dic_conformance.checks(
 id BIGSERIAL PRIMARY KEY,mapping_id BIGINT NOT NULL REFERENCES public.qabug_software_spec_versions(id),
 mapping_fingerprint CHAR(64) NOT NULL,content_id BIGINT NOT NULL CHECK(content_id>0),document_id TEXT NOT NULL,
 content_hash CHAR(64) NOT NULL,section_id TEXT NOT NULL,rule_json JSONB NOT NULL,
 status TEXT NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','APPROVED','REVOKED')),
 approved_by TEXT,approved_at TIMESTAMPTZ,approval_reason TEXT,
 UNIQUE(mapping_id,content_id,content_hash,rule_json));
CREATE INDEX IF NOT EXISTS ix_dic_checks_mapping ON dic_conformance.checks(mapping_id,status);
CREATE TABLE IF NOT EXISTS dic_conformance.review_audit(
 id BIGSERIAL PRIMARY KEY,object_kind TEXT NOT NULL CHECK(object_kind IN ('MAPPING','CHECK')),object_id BIGINT NOT NULL,
 previous_status TEXT NOT NULL,new_status TEXT NOT NULL,fingerprint CHAR(64) NOT NULL,
 actor NAME NOT NULL,reason TEXT NOT NULL,reviewed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp());
CREATE INDEX IF NOT EXISTS ix_dic_conformance_audit_head ON dic_conformance.review_audit(object_kind,object_id,id DESC);

CREATE OR REPLACE FUNCTION dic_conformance.fingerprint(p_kind TEXT,p_id BIGINT)
RETURNS TEXT LANGUAGE plpgsql STABLE SET search_path=pg_catalog AS $$
DECLARE value jsonb;
BEGIN
 IF p_kind='MAPPING' THEN
  SELECT jsonb_build_array(issue_version_field,software_version,model,destination,spec_scope,spec_code,spec_version_id,spec_version_label,spec_document_id)
    INTO value FROM public.qabug_software_spec_versions WHERE id=p_id;
 ELSIF p_kind='CHECK' THEN
  SELECT jsonb_build_array(mapping_id,mapping_fingerprint,content_id,document_id,content_hash,section_id,rule_json)
    INTO value FROM dic_conformance.checks WHERE id=p_id;
 ELSE RAISE EXCEPTION 'Unknown object kind'; END IF;
 IF value IS NULL THEN RETURN NULL; END IF;
 RETURN encode(sha256(convert_to(value::text,'UTF8')),'hex');
END $$;

CREATE OR REPLACE FUNCTION dic_conformance.review(p_kind TEXT,p_id BIGINT,p_fingerprint TEXT,p_new TEXT,p_reason TEXT)
RETURNS BIGINT LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE previous text;current_hash text;map_id bigint;bound_hash text;
BEGIN
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname=session_user AND (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls))
   OR NOT EXISTS(SELECT 1 FROM dic_conformance.authorized_reviewers WHERE login_name=session_user AND disabled_at IS NULL) THEN
   RAISE EXCEPTION 'Authorized non-administrator reviewer session required'; END IF;
 IF p_kind NOT IN ('MAPPING','CHECK') OR p_new NOT IN ('PENDING','APPROVED','REVOKED') OR length(btrim(p_reason)) NOT BETWEEN 12 AND 2000
    OR p_fingerprint !~ '^[0-9a-f]{64}$' THEN RAISE EXCEPTION 'Invalid review request'; END IF;
 IF p_kind='MAPPING' THEN
  SELECT status INTO previous FROM public.qabug_software_spec_versions WHERE id=p_id FOR UPDATE;
 ELSE
  SELECT status,mapping_id,mapping_fingerprint INTO previous,map_id,bound_hash FROM dic_conformance.checks WHERE id=p_id FOR UPDATE;
  IF p_new='APPROVED' AND (NOT EXISTS(SELECT 1 FROM public.qabug_software_spec_versions WHERE id=map_id AND status='APPROVED')
    OR bound_hash IS DISTINCT FROM dic_conformance.fingerprint('MAPPING',map_id)
    OR NOT EXISTS(SELECT 1 FROM dic_conformance.review_audit WHERE id=(SELECT max(id) FROM dic_conformance.review_audit
      WHERE object_kind='MAPPING' AND object_id=map_id) AND new_status='APPROVED' AND fingerprint=bound_hash)) THEN
    RAISE EXCEPTION 'Check requires current authenticated mapping approval'; END IF;
 END IF;
 current_hash:=dic_conformance.fingerprint(p_kind,p_id);
 IF previous IS NULL OR current_hash IS DISTINCT FROM p_fingerprint OR NOT
   ((previous='PENDING' AND p_new='APPROVED') OR (previous='APPROVED' AND p_new='REVOKED') OR (previous='REVOKED' AND p_new='PENDING')) THEN
   RAISE EXCEPTION 'Object changed, missing, or invalid transition'; END IF;
 IF p_kind='MAPPING' THEN
  UPDATE public.qabug_software_spec_versions SET status=p_new,approved_by=CASE WHEN p_new='PENDING' THEN NULL ELSE session_user END,
   approved_at=CASE WHEN p_new='PENDING' THEN NULL ELSE clock_timestamp() END,approval_reason=CASE WHEN p_new='PENDING' THEN NULL ELSE p_reason END WHERE id=p_id;
 ELSE
  UPDATE dic_conformance.checks SET status=p_new,approved_by=CASE WHEN p_new='PENDING' THEN NULL ELSE session_user END,
   approved_at=CASE WHEN p_new='PENDING' THEN NULL ELSE clock_timestamp() END,approval_reason=CASE WHEN p_new='PENDING' THEN NULL ELSE p_reason END WHERE id=p_id;
 END IF;
 INSERT INTO dic_conformance.review_audit(object_kind,object_id,previous_status,new_status,fingerprint,actor,reason)
 VALUES(p_kind,p_id,previous,p_new,current_hash,session_user,btrim(p_reason));
 RETURN p_id;
END $$;

CREATE OR REPLACE FUNCTION dic_conformance.guard_object_write()
RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE authority name;value jsonb;
BEGIN
 SELECT pg_get_userbyid(proowner) INTO authority FROM pg_proc WHERE oid='dic_conformance.review(text,bigint,text,text,text)'::regprocedure;
 IF current_user=authority THEN IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF; END IF;
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Direct deletion forbidden'; END IF;
 value:=to_jsonb(NEW);
 IF (TG_OP='UPDATE' AND to_jsonb(OLD)->>'status'<>'PENDING') OR value->>'status'<>'PENDING'
   OR value->>'approved_by' IS NOT NULL OR value->>'approved_at' IS NOT NULL OR value->>'approval_reason' IS NOT NULL THEN
   RAISE EXCEPTION 'Only unapproved proposals may be written directly'; END IF;
 RETURN NEW;
END $$;
CREATE OR REPLACE FUNCTION dic_conformance.guard_audit_write()
RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE authority name;
BEGIN
 SELECT pg_get_userbyid(proowner) INTO authority FROM pg_proc WHERE oid='dic_conformance.review(text,bigint,text,text,text)'::regprocedure;
 IF TG_OP<>'INSERT' OR current_user<>authority THEN RAISE EXCEPTION 'Audit is append-only through review authority'; END IF;
 RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS dic_conformance_mapping_guard ON public.qabug_software_spec_versions;
CREATE TRIGGER dic_conformance_mapping_guard BEFORE INSERT OR UPDATE OR DELETE ON public.qabug_software_spec_versions
 FOR EACH ROW EXECUTE FUNCTION dic_conformance.guard_object_write();
DROP TRIGGER IF EXISTS dic_conformance_check_guard ON dic_conformance.checks;
CREATE TRIGGER dic_conformance_check_guard BEFORE INSERT OR UPDATE OR DELETE ON dic_conformance.checks
 FOR EACH ROW EXECUTE FUNCTION dic_conformance.guard_object_write();
DROP TRIGGER IF EXISTS dic_conformance_audit_guard ON dic_conformance.review_audit;
CREATE TRIGGER dic_conformance_audit_guard BEFORE INSERT OR UPDATE OR DELETE ON dic_conformance.review_audit
 FOR EACH ROW EXECUTE FUNCTION dic_conformance.guard_audit_write();
REVOKE ALL ON ALL TABLES IN SCHEMA dic_conformance FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA dic_conformance FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA dic_conformance FROM PUBLIC;
COMMIT;
