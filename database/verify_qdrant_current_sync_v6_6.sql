SELECT component, version, name, applied_at
FROM public.schema_version
WHERE component = 'document_ingestion_qdrant_current_sync';

SELECT column_name, data_type
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name = 'embeddings'
  AND column_name IN ('index_hash', 'source_view', 'last_synced_at', 'is_current_index')
ORDER BY column_name;

SELECT to_regclass('public.qdrant_sync_runs') AS qdrant_sync_runs;

SELECT
    project_code,
    collection_name,
    status,
    source_count,
    upserted_count,
    deleted_count,
    qdrant_after_count,
    started_at,
    finished_at
FROM public.qdrant_sync_runs
ORDER BY id DESC
LIMIT 10;
