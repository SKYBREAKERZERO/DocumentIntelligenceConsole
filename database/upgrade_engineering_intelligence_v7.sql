-- Engineering Intelligence v7 / additive-only migration / PostgreSQL 17 compatible.
-- Apply separately to rag_21mm and rag_24mm, after full database backups.
-- NEVER run against rag_qabug: QA/BUG remains in a separate authority database.
-- All new relations are evidence-backed CANDIDATES until reviewed.
BEGIN;
CREATE TABLE IF NOT EXISTS public.ei_requirement_units (
    id BIGSERIAL PRIMARY KEY,
    unit_key CHAR(64) NOT NULL UNIQUE,
    extractor_version TEXT NOT NULL,
    specification_id BIGINT NOT NULL REFERENCES public.specifications(id) ON DELETE CASCADE,
    version_id BIGINT NOT NULL REFERENCES public.specification_versions(id) ON DELETE CASCADE,
    document_id TEXT NOT NULL REFERENCES public.documents(document_id) ON DELETE CASCADE,
    content_id BIGINT NOT NULL REFERENCES public.contents(id) ON DELETE CASCADE,
    section_id TEXT NOT NULL,
    page_number INTEGER,
    start_offset INTEGER NOT NULL CHECK (start_offset >= 0),
    end_offset INTEGER NOT NULL CHECK (end_offset > start_offset),
    unit_type TEXT NOT NULL CHECK (unit_type IN ('REQUIREMENT','REFERENCE','TABLE_ROW','SOURCE_TEXT')),
    raw_text TEXT NOT NULL,
    text_hash CHAR(64) NOT NULL,
    signals JSONB NOT NULL DEFAULT '{}'::jsonb,
    review_status TEXT NOT NULL DEFAULT 'AUTO_EXTRACTED'
        CHECK (review_status IN ('AUTO_EXTRACTED','APPROVED','REJECTED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_ei_unit_no_empty_text CHECK (length(raw_text) > 0),
    CONSTRAINT uq_ei_unit_span UNIQUE (version_id, content_id, start_offset, end_offset, text_hash)
);
CREATE INDEX IF NOT EXISTS ix_ei_units_spec_version_section
    ON public.ei_requirement_units(specification_id, version_id, section_id);
CREATE INDEX IF NOT EXISTS ix_ei_units_content ON public.ei_requirement_units(content_id);
CREATE INDEX IF NOT EXISTS ix_ei_units_text_hash ON public.ei_requirement_units(text_hash);

CREATE TABLE IF NOT EXISTS public.ei_content_relations (
    id BIGSERIAL PRIMARY KEY,
    candidate_key CHAR(64) NOT NULL UNIQUE,
    source_unit_id BIGINT NOT NULL REFERENCES public.ei_requirement_units(id) ON DELETE CASCADE,
    target_unit_id BIGINT NOT NULL REFERENCES public.ei_requirement_units(id) ON DELETE CASCADE,
    relation_type TEXT NOT NULL,
    extraction_method TEXT NOT NULL,
    score NUMERIC(6,5) NOT NULL CHECK (score BETWEEN 0 AND 1),
    status TEXT NOT NULL DEFAULT 'REVIEW_REQUIRED'
        CHECK (status IN ('REVIEW_REQUIRED','CONFIRMED','REJECTED','STALE')),
    rationale TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reviewed_at TIMESTAMPTZ,
    reviewed_by TEXT,
    CONSTRAINT ck_ei_no_self_relation CHECK (source_unit_id <> target_unit_id)
);
CREATE INDEX IF NOT EXISTS ix_ei_rel_source ON public.ei_content_relations(source_unit_id,status);
CREATE INDEX IF NOT EXISTS ix_ei_rel_target ON public.ei_content_relations(target_unit_id,status);

CREATE TABLE IF NOT EXISTS public.ei_version_mappings (
    id BIGSERIAL PRIMARY KEY,
    source_version_id BIGINT NOT NULL REFERENCES public.specification_versions(id) ON DELETE CASCADE,
    target_version_id BIGINT NOT NULL REFERENCES public.specification_versions(id) ON DELETE CASCADE,
    old_unit_id BIGINT REFERENCES public.ei_requirement_units(id) ON DELETE CASCADE,
    new_unit_id BIGINT REFERENCES public.ei_requirement_units(id) ON DELETE CASCADE,
    old_unit_key TEXT NOT NULL DEFAULT '',
    new_unit_key TEXT NOT NULL DEFAULT '',
    change_type TEXT NOT NULL CHECK (change_type IN ('UNCHANGED','MOVED','MODIFIED','REMOVED','ADDED')),
    similarity NUMERIC(6,5) NOT NULL CHECK (similarity BETWEEN 0 AND 1),
    review_status TEXT NOT NULL CHECK (review_status IN ('DETERMINISTIC','REVIEW_REQUIRED','APPROVED','REJECTED')),
    algorithm_version TEXT NOT NULL DEFAULT 'requirement-align-v1',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_ei_version_map_has_endpoint CHECK (old_unit_id IS NOT NULL OR new_unit_id IS NOT NULL),
    CONSTRAINT uq_ei_version_map UNIQUE (source_version_id,target_version_id,old_unit_key,new_unit_key,algorithm_version)
);
CREATE INDEX IF NOT EXISTS ix_ei_version_map_pair ON public.ei_version_mappings(source_version_id,target_version_id,change_type);

-- Provenance-preserving audit trail: no UPDATE/DELETE access for ordinary query users.
CREATE TABLE IF NOT EXISTS public.ei_relation_review_audit (
    id BIGSERIAL PRIMARY KEY,
    candidate_id BIGINT NOT NULL REFERENCES public.ei_content_relations(id) ON DELETE CASCADE,
    actor TEXT NOT NULL,
    previous_status TEXT NOT NULL,
    new_status TEXT NOT NULL,
    reason TEXT NOT NULL,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- A complete, immutable-source receipt is required before classifying absent
-- requirement units as REMOVED or ADDED. Empty/whitespace chunks count too.
CREATE TABLE IF NOT EXISTS public.ei_version_backfill_receipts (
    version_id BIGINT PRIMARY KEY REFERENCES public.specification_versions(id) ON DELETE CASCADE,
    document_id TEXT NOT NULL REFERENCES public.documents(document_id) ON DELETE CASCADE,
    extractor_version TEXT NOT NULL,
    source_chunk_count BIGINT NOT NULL CHECK (source_chunk_count >= 0),
    source_unit_count BIGINT NOT NULL CHECK (source_unit_count >= 0),
    max_content_id BIGINT NOT NULL CHECK (max_content_id >= 0),
    source_digest CHAR(64) NOT NULL,
    completed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- An explicit reference to a section with multiple requirement units does
-- NOT justify inventing a source-paragraph -> target-paragraph edge.
CREATE TABLE IF NOT EXISTS public.ei_unresolved_relation_anchors (
    id BIGSERIAL PRIMARY KEY,
    source_unit_id BIGINT NOT NULL REFERENCES public.ei_requirement_units(id) ON DELETE CASCADE,
    target_version_id BIGINT NOT NULL REFERENCES public.specification_versions(id) ON DELETE CASCADE,
    target_section_id TEXT NOT NULL,
    reason TEXT NOT NULL CHECK (reason IN (
        'AMBIGUOUS_TARGET_SECTION', 'TARGET_SECTION_MISSING',
        'TARGET_VERSION_NOT_INDEXED', 'RESOLVED'
    )),
    candidate_count INTEGER NOT NULL DEFAULT 0 CHECK (candidate_count >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_ei_unresolved_anchor UNIQUE (source_unit_id, target_version_id, target_section_id)
);
CREATE INDEX IF NOT EXISTS ix_ei_unresolved_version_reason
    ON public.ei_unresolved_relation_anchors(target_version_id, reason);
CREATE INDEX IF NOT EXISTS ix_ei_units_version_id_id
    ON public.ei_requirement_units(version_id,id);
CREATE INDEX IF NOT EXISTS ix_ei_units_version_section_id
    ON public.ei_requirement_units(version_id,section_id,id);

COMMIT;
