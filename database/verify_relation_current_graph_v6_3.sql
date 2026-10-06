\echo 'Verify relation current-generation guard v6.3'

SELECT specification_id, COUNT(*) AS current_runs
FROM public.relation_extraction_runs
WHERE is_current IS TRUE
GROUP BY specification_id
HAVING COUNT(*) > 1;

SELECT indexname, indexdef
FROM pg_indexes
WHERE schemaname = 'public'
  AND indexname = 'uq_relation_run_current_specification';

SELECT component, version, name
FROM public.schema_version
WHERE component = 'document_ingestion_relation_current_guard';
