-- Document Intelligence Console Enterprise v5.2.1
-- RAG Schema v6.2 deterministic cross-specification reference helpers.
--
-- Purpose:
--   * SQL-first reference identity registry before Qdrant/embedding.
--   * Exact normalized code/title/alias lookup.
--   * Current resolved/unresolved reference audit views.
--
-- This script is non-destructive. It does not alter existing business rows.

BEGIN;

CREATE OR REPLACE FUNCTION public.normalize_spec_reference_code(value TEXT)
RETURNS TEXT
LANGUAGE SQL
IMMUTABLE
PARALLEL SAFE
AS $$
    SELECT regexp_replace(
        upper(
            translate(
                COALESCE(value, ''),
                '０１２３４５６７８９‐‑‒–—―',
                '0123456789------'
            )
        ),
        '[[:space:]]+',
        '',
        'g'
    );
$$;

CREATE OR REPLACE FUNCTION public.normalize_spec_reference_title(value TEXT)
RETURNS TEXT
LANGUAGE SQL
IMMUTABLE
PARALLEL SAFE
AS $$
    SELECT regexp_replace(
        upper(
            translate(
                COALESCE(value, ''),
                '０１２３４５６７８９‐‑‒–—―',
                '0123456789------'
            )
        ),
        '[[:space:]_.:/,，。()（）\[\]【】{}<>《》「」『』・·-]+',
        '',
        'g'
    );
$$;

CREATE INDEX IF NOT EXISTS idx_specifications_reference_code_norm
    ON public.specifications (public.normalize_spec_reference_code(spec_code))
    WHERE status = 'ACTIVE' AND spec_code IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_specifications_project_active
    ON public.specifications (project_code, status, id);

CREATE OR REPLACE VIEW public.spec_reference_registry AS
SELECT
    sp.id AS specification_id,
    sp.specification_key,
    sp.project_code,
    sp.current_version_id,
    sp.status,
    'SPEC_CODE'::TEXT AS reference_type,
    sp.spec_code AS raw_reference,
    public.normalize_spec_reference_code(sp.spec_code) AS normalized_reference
FROM public.specifications sp
WHERE sp.status = 'ACTIVE'
  AND NULLIF(BTRIM(sp.spec_code), '') IS NOT NULL

UNION ALL

SELECT
    sp.id,
    sp.specification_key,
    sp.project_code,
    sp.current_version_id,
    sp.status,
    'CANONICAL_NAME'::TEXT,
    sp.canonical_name,
    public.normalize_spec_reference_title(sp.canonical_name)
FROM public.specifications sp
WHERE sp.status = 'ACTIVE'
  AND NULLIF(BTRIM(sp.canonical_name), '') IS NOT NULL

UNION ALL

SELECT
    sp.id,
    sp.specification_key,
    sp.project_code,
    sp.current_version_id,
    sp.status,
    'SPECIFICATION_KEY'::TEXT,
    sp.specification_key,
    public.normalize_spec_reference_code(sp.specification_key)
FROM public.specifications sp
WHERE sp.status = 'ACTIVE'

UNION ALL

SELECT
    sp.id,
    sp.specification_key,
    sp.project_code,
    sp.current_version_id,
    sp.status,
    'ALIAS'::TEXT,
    alias_value.alias_text,
    public.normalize_spec_reference_title(alias_value.alias_text)
FROM public.specifications sp
CROSS JOIN LATERAL (
    SELECT jsonb_array_elements_text(
        CASE
            WHEN jsonb_typeof(COALESCE(sp.metadata, '{}'::jsonb)->'reference_aliases') = 'array'
            THEN COALESCE(sp.metadata, '{}'::jsonb)->'reference_aliases'
            ELSE '[]'::jsonb
        END
    ) AS alias_text
    UNION ALL
    SELECT jsonb_array_elements_text(
        CASE
            WHEN jsonb_typeof(COALESCE(sp.metadata, '{}'::jsonb)->'aliases') = 'array'
            THEN COALESCE(sp.metadata, '{}'::jsonb)->'aliases'
            ELSE '[]'::jsonb
        END
    ) AS alias_text
) alias_value
WHERE sp.status = 'ACTIVE'
  AND NULLIF(BTRIM(alias_value.alias_text), '') IS NOT NULL;

CREATE OR REPLACE VIEW public.spec_reference_current_edges AS
SELECT
    cat.relation_id,
    cat.source_specification_id,
    cat.source_specification_key,
    cat.source_specification_name,
    cat.source_version_id,
    cat.source_version,
    cat.target_specification_id,
    cat.target_specification_key,
    cat.target_specification_name,
    cat.target_external_key,
    cat.extraction_method,
    cat.confidence,
    cat.status,
    cat.evidence_count,
    cat.metadata,
    cat.created_at
FROM public.engineering_relation_catalog cat
WHERE cat.relation_type = 'REFERENCES'
  AND cat.is_current_extraction IS TRUE
  AND cat.is_current_source_version IS TRUE;

CREATE OR REPLACE VIEW public.spec_reference_unresolved_current AS
SELECT *
FROM public.spec_reference_current_edges
WHERE status IN ('UNRESOLVED', 'AMBIGUOUS')
   OR target_specification_id IS NULL;

COMMIT;
