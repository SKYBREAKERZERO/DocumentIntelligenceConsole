SELECT component, version, name
FROM public.schema_version
WHERE component = 'document_ingestion_relation_graph_projection';

SELECT graph_bucket, COUNT(*) AS relation_count
FROM public.engineering_relation_graph_projection
GROUP BY graph_bucket
ORDER BY graph_bucket;

SELECT
    relation_id,
    source_specification_key,
    target_specification_key,
    target_external_key,
    status,
    graph_bucket
FROM public.engineering_relation_graph_projection
WHERE source_specification_key LIKE '%508%'
ORDER BY relation_id;
