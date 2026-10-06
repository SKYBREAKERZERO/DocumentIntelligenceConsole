-- Document Intelligence Console v5.2.7
-- Status-radial relationship graph projection helper.

BEGIN;

DO $$
BEGIN
    IF to_regclass('public.engineering_relation_catalog') IS NULL THEN
        RAISE EXCEPTION 'engineering_relation_catalog view is required first.';
    END IF;
END
$$;

CREATE OR REPLACE VIEW public.engineering_relation_graph_projection AS
SELECT
    relation_id,
    source_specification_id,
    source_specification_key,
    source_spec_code,
    source_specification_name,
    source_version,
    target_specification_id,
    target_specification_key,
    target_spec_code,
    target_specification_name,
    target_external_key,
    target_entity_key,
    target_entity_name,
    target_entity_type,
    relation_type,
    extraction_method,
    status,
    confidence,
    evidence_count,
    evidence_chunk_count,
    metadata,
    CASE
        WHEN COALESCE(metadata ->> 'target_identity_kind', '') = 'TITLE_ONLY'
             OR COALESCE(target_external_key, '') LIKE 'SPEC_TITLE:%'
             OR COALESCE(target_entity_type, '') = 'AUDIT_CANDIDATE'
            THEN 'AUDIT'
        WHEN status = 'CONFIRMED' THEN 'CONFIRMED'
        WHEN status = 'AMBIGUOUS' THEN 'AMBIGUOUS'
        ELSE 'UNRESOLVED'
    END AS graph_bucket
FROM public.engineering_relation_catalog;

COMMENT ON VIEW public.engineering_relation_graph_projection IS
'Status-radial graph helper view: classifies current engineering relations into CONFIRMED / UNRESOLVED / AMBIGUOUS / AUDIT buckets.';

INSERT INTO public.schema_version(component, version, name, applied_at)
VALUES (
    'document_ingestion_relation_graph_projection',
    1,
    'status-radial-graph-projection-v1',
    NOW()
)
ON CONFLICT (component)
DO UPDATE SET
    version = EXCLUDED.version,
    name = EXCLUDED.name,
    applied_at = EXCLUDED.applied_at;

COMMIT;
