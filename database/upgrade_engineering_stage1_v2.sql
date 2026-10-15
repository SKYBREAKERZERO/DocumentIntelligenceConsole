-- Engineering Intelligence Stage 1 v2: additive, non-destructive enrichment.
-- Apply only to backed-up rag_21mm/rag_24mm databases after schema v7.
-- Preserves APPROVED requirement units, review history and confirmed edges.
BEGIN;
CREATE TABLE IF NOT EXISTS public.ei_requirement_enrichments (
    unit_id BIGINT PRIMARY KEY REFERENCES public.ei_requirement_units(id) ON DELETE CASCADE,
    extractor_version TEXT NOT NULL,
    derived_unit_type TEXT NOT NULL CHECK (derived_unit_type IN
        ('REQUIREMENT', 'REFERENCE', 'TABLE_ROW', 'SOURCE_TEXT')),
    source_text_hash CHAR(64) NOT NULL,
    signals JSONB NOT NULL,
    enriched_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_ei_enrichment_version
    ON public.ei_requirement_enrichments(extractor_version);
CREATE TABLE IF NOT EXISTS public.ei_stage1_enrichment_receipts (
    version_id BIGINT PRIMARY KEY REFERENCES public.specification_versions(id) ON DELETE CASCADE,
    extractor_version TEXT NOT NULL,
    source_digest CHAR(64) NOT NULL,
    source_unit_count BIGINT NOT NULL CHECK (source_unit_count >= 0),
    completed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
COMMIT;
