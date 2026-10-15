-- Stage 1 v3: only derived candidate/evidence tables. Never edits source units,
-- approved review decisions or established relations. Explicit operator migration.
BEGIN;
CREATE TABLE IF NOT EXISTS public.ei_atomic_candidates (
    candidate_key CHAR(64) PRIMARY KEY,
    source_unit_id BIGINT NOT NULL REFERENCES public.ei_requirement_units(id) ON DELETE CASCADE,
    version_id BIGINT NOT NULL REFERENCES public.specification_versions(id) ON DELETE CASCADE,
    extraction_version TEXT NOT NULL,
    start_in_unit INTEGER NOT NULL CHECK (start_in_unit >= 0),
    end_in_unit INTEGER NOT NULL CHECK (end_in_unit > start_in_unit),
    start_in_content INTEGER NOT NULL CHECK (start_in_content >= 0),
    end_in_content INTEGER NOT NULL CHECK (end_in_content > start_in_content),
    source_text_hash CHAR(64) NOT NULL,
    source_text TEXT NOT NULL,
    candidate_role TEXT NOT NULL CHECK (candidate_role IN ('REQUIREMENT_CANDIDATE','CONTEXT','TABLE_CONTEXT')),
    review_status TEXT NOT NULL DEFAULT 'REVIEW_REQUIRED' CHECK (review_status IN ('REVIEW_REQUIRED','SOURCE_ONLY','APPROVED','REJECTED')),
    review_reasons JSONB NOT NULL DEFAULT '[]'::jsonb,
    source_cues JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_unit_id,start_in_unit,end_in_unit)
);
CREATE INDEX IF NOT EXISTS ix_ei_atomic_v3_version ON public.ei_atomic_candidates(version_id,source_unit_id);
CREATE INDEX IF NOT EXISTS ix_ei_atomic_v3_review ON public.ei_atomic_candidates(version_id,review_status);
CREATE TABLE IF NOT EXISTS public.ei_table_evidence (
    table_key CHAR(64) PRIMARY KEY,
    version_id BIGINT NOT NULL REFERENCES public.specification_versions(id) ON DELETE CASCADE,
    document_id TEXT NOT NULL,
    content_id BIGINT NOT NULL,
    extraction_version TEXT NOT NULL,
    header_status TEXT NOT NULL CHECK (header_status IN ('EXPLICIT_MARKDOWN','UNRESOLVED')),
    rows JSONB NOT NULL,
    review_reasons JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_ei_table_v3_version ON public.ei_table_evidence(version_id,content_id);
CREATE TABLE IF NOT EXISTS public.ei_stage1_v3_receipts (
    version_id BIGINT PRIMARY KEY REFERENCES public.specification_versions(id) ON DELETE CASCADE,
    source_units_digest CHAR(64) NOT NULL,
    source_unit_count BIGINT NOT NULL CHECK (source_unit_count >= 0),
    atomic_candidate_count BIGINT NOT NULL CHECK (atomic_candidate_count >= 0),
    table_block_count BIGINT NOT NULL CHECK (table_block_count >= 0),
    extraction_version TEXT NOT NULL,
    completed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMIT;
