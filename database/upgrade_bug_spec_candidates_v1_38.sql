-- v1.38 additive migration: execute ONLY on rag_qabug, never on a spec DB.
-- Requires explicit DBA grants: online reader SELECT, offline batch writer INSERT/UPDATE.
-- Cross-database identities are checked by the batch worker and revalidated at read.
BEGIN;
DO $guard$
BEGIN
  IF to_regclass('public.qabug_records') IS NULL
     OR to_regclass('public.bug_details') IS NULL
     OR to_regclass('public.specifications') IS NOT NULL THEN
    RAISE EXCEPTION 'Requires the isolated QABUG database, not a specification repository';
  END IF;
END $guard$;

CREATE TABLE IF NOT EXISTS public.qabug_software_spec_versions (
  id BIGSERIAL PRIMARY KEY,
  -- No fuzzy or wildcard approval: exact context and software version ONLY.
  issue_version_field TEXT NOT NULL CHECK (issue_version_field IN ('affected_version','software_version')),
  software_version TEXT NOT NULL CHECK (length(btrim(software_version)) > 0),
  model TEXT NOT NULL DEFAULT '',
  destination TEXT NOT NULL DEFAULT '',
  spec_scope TEXT NOT NULL CHECK (spec_scope IN ('21MM','24MM','COMMON')),
  spec_code TEXT NOT NULL CHECK (length(btrim(spec_code)) > 0),
  spec_version_id BIGINT NOT NULL CHECK (spec_version_id > 0),
  spec_version_label TEXT NOT NULL CHECK (length(btrim(spec_version_label)) > 0),
  spec_document_id TEXT NOT NULL CHECK (length(btrim(spec_document_id)) > 0),
  status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','APPROVED','REVOKED')),
  approved_by TEXT, approved_at TIMESTAMPTZ,
  approval_reason TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT ck_qabug_map_approval CHECK (
     (status='APPROVED' AND nullif(btrim(approved_by),'') IS NOT NULL
      AND approved_at IS NOT NULL AND nullif(btrim(approval_reason),'') IS NOT NULL)
     OR status <> 'APPROVED'),
  CONSTRAINT uq_qabug_spec_version_mapping UNIQUE (
      issue_version_field,software_version,model,destination,
      spec_scope,spec_code,spec_version_id)
);
CREATE INDEX IF NOT EXISTS ix_qabug_spec_approved_version
 ON public.qabug_software_spec_versions(issue_version_field,software_version,status);

CREATE TABLE IF NOT EXISTS public.qabug_spec_candidates (
  id BIGSERIAL PRIMARY KEY,
  bug_record_id BIGINT NOT NULL REFERENCES public.qabug_records(id) ON DELETE CASCADE,
  mapping_id BIGINT NOT NULL REFERENCES public.qabug_software_spec_versions(id),
  issue_key TEXT NOT NULL,
  issue_snapshot_hash CHAR(64) NOT NULL,
  phenomenon_json JSONB NOT NULL,
  spec_scope TEXT NOT NULL CHECK (spec_scope IN ('21MM','24MM','COMMON')),
  spec_code TEXT NOT NULL,
  spec_version_id BIGINT NOT NULL,
  spec_version_label TEXT NOT NULL,
  spec_document_id TEXT NOT NULL,
  spec_file_name TEXT NOT NULL,
  spec_unit_id BIGINT NOT NULL,
  spec_content_id BIGINT NOT NULL,
  section_id TEXT NOT NULL,
  page_number INTEGER,
  start_offset INTEGER NOT NULL CHECK (start_offset >= 0),
  end_offset INTEGER NOT NULL CHECK (end_offset > start_offset),
  spec_raw_text TEXT NOT NULL,
  spec_text_hash CHAR(64) NOT NULL,
  rank_score NUMERIC(7,5) NOT NULL CHECK (rank_score BETWEEN 0 AND 1),
  ranker_version TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'UNREVIEWED' CHECK (status IN ('UNREVIEWED','REJECTED','STALE')),
  verified_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_qabug_spec_candidate UNIQUE (bug_record_id,mapping_id,spec_unit_id)
);
CREATE INDEX IF NOT EXISTS ix_qabug_spec_candidates_issue
  ON public.qabug_spec_candidates(issue_key, status,rank_score DESC,id);
CREATE INDEX IF NOT EXISTS ix_qabug_spec_candidates_version
  ON public.qabug_spec_candidates(mapping_id,spec_version_id);
-- By default tables remain accessible only to their owner. No PUBLIC grant,
-- no broad privilege propagation, and no automatic approval of candidates.
REVOKE ALL ON public.qabug_software_spec_versions FROM PUBLIC;
REVOKE ALL ON public.qabug_spec_candidates FROM PUBLIC;
COMMIT;
