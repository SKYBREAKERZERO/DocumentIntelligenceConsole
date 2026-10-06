SELECT component, version, name
FROM public.schema_version
WHERE component = 'document_ingestion_manual_spec_relations';

SELECT
    COUNT(*) FILTER (WHERE is_active IS TRUE) AS active_manual_relations,
    COUNT(*) FILTER (WHERE is_active IS TRUE AND status = 'CONFIRMED') AS confirmed,
    COUNT(*) FILTER (WHERE is_active IS TRUE AND status = 'UNRESOLVED') AS unresolved,
    COUNT(*) FILTER (WHERE is_active IS TRUE AND status = 'AMBIGUOUS') AS ambiguous
FROM public.manual_spec_relations;

SELECT
    mr.id,
    src.spec_code AS source_spec_no,
    src.canonical_name AS source_name,
    COALESCE(tgt.spec_code, mr.target_external_key) AS target,
    mr.semantic_type,
    mr.status,
    mr.evidence_kind,
    mr.target_locator_type,
    mr.target_locator_value,
    mr.created_by,
    mr.updated_at
FROM public.manual_spec_relations mr
JOIN public.specifications src ON src.id = mr.source_specification_id
LEFT JOIN public.specifications tgt ON tgt.id = mr.target_specification_id
WHERE mr.is_active IS TRUE
ORDER BY mr.updated_at DESC, mr.id DESC;
