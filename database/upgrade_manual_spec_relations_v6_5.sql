-- Document Intelligence Console v5.2.8
-- Manual specification relation editor / audit history.
-- Non-destructive: automatic extractor output remains immutable and separate.

BEGIN;

DO $$
BEGIN
    IF to_regclass('public.specifications') IS NULL
       OR to_regclass('public.specification_versions') IS NULL THEN
        RAISE EXCEPTION 'RAG Schema v6 specification lifecycle tables are required first.';
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS public.manual_spec_relations (
    id BIGSERIAL PRIMARY KEY,
    project_code TEXT NOT NULL,
    source_specification_id BIGINT NOT NULL,
    source_version_id BIGINT NOT NULL,
    target_specification_id BIGINT,
    target_external_key TEXT,
    semantic_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'UNRESOLVED',
    confidence NUMERIC(5,4) NOT NULL DEFAULT 1.0,
    evidence_kind TEXT NOT NULL DEFAULT 'ENGINEERING_DECISION',
    evidence_text TEXT NOT NULL,
    target_locator_type TEXT,
    target_locator_value TEXT,
    note TEXT,
    created_by TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_manual_relation_target CHECK (
        target_specification_id IS NOT NULL OR target_external_key IS NOT NULL
    ),
    CONSTRAINT ck_manual_relation_status CHECK (
        status IN ('CONFIRMED', 'UNRESOLVED', 'AMBIGUOUS')
    ),
    CONSTRAINT ck_manual_relation_evidence CHECK (
        length(btrim(evidence_text)) > 0
    ),
    CONSTRAINT fk_manual_relation_source_spec
        FOREIGN KEY (source_specification_id)
        REFERENCES public.specifications(id) ON DELETE CASCADE,
    CONSTRAINT fk_manual_relation_source_version
        FOREIGN KEY (source_version_id)
        REFERENCES public.specification_versions(id) ON DELETE CASCADE,
    CONSTRAINT fk_manual_relation_target_spec
        FOREIGN KEY (target_specification_id)
        REFERENCES public.specifications(id) ON DELETE SET NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_manual_spec_relation_active_identity
ON public.manual_spec_relations (
    source_version_id,
    semantic_type,
    COALESCE(target_specification_id, 0::BIGINT),
    COALESCE(target_external_key, ''),
    COALESCE(target_locator_type, ''),
    COALESCE(target_locator_value, '')
)
WHERE is_active IS TRUE;

CREATE INDEX IF NOT EXISTS idx_manual_spec_relation_source_current
ON public.manual_spec_relations(source_specification_id, source_version_id, is_active, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_manual_spec_relation_target
ON public.manual_spec_relations(target_specification_id, target_external_key, status)
WHERE is_active IS TRUE;

CREATE TABLE IF NOT EXISTS public.manual_spec_relation_audit (
    id BIGSERIAL PRIMARY KEY,
    manual_relation_id BIGINT NOT NULL,
    action TEXT NOT NULL,
    actor TEXT,
    snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_manual_relation_audit_action CHECK (
        action IN ('CREATE', 'UPDATE', 'DELETE', 'RECONCILE')
    ),
    CONSTRAINT fk_manual_relation_audit_relation
        FOREIGN KEY (manual_relation_id)
        REFERENCES public.manual_spec_relations(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_manual_relation_audit_history
ON public.manual_spec_relation_audit(manual_relation_id, created_at DESC, id DESC);

INSERT INTO public.schema_version(component, version, name, applied_at)
VALUES (
    'document_ingestion_manual_spec_relations',
    1,
    'manual-spec-relations-audit-v1',
    NOW()
)
ON CONFLICT (component)
DO UPDATE SET
    version = EXCLUDED.version,
    name = EXCLUDED.name,
    applied_at = EXCLUDED.applied_at;

COMMIT;
