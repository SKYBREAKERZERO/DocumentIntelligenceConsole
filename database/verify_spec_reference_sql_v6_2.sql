-- Verify deterministic SQL-first cross-specification reference support.

SELECT
    to_regprocedure('public.normalize_spec_reference_code(text)') AS normalize_code_function,
    to_regprocedure('public.normalize_spec_reference_title(text)') AS normalize_title_function,
    to_regclass('public.spec_reference_registry') AS registry_view,
    to_regclass('public.spec_reference_current_edges') AS current_edges_view,
    to_regclass('public.spec_reference_unresolved_current') AS unresolved_view;

SELECT
    public.normalize_spec_reference_code(' ２００７ ') AS code_should_be_2007,
    public.normalize_spec_reference_code('AB–1001') AS dash_should_normalize,
    public.normalize_spec_reference_title('《User Profile Communication Interface Spec》') AS normalized_title;

SELECT
    specification_id,
    specification_key,
    project_code,
    reference_type,
    raw_reference,
    normalized_reference
FROM public.spec_reference_registry
ORDER BY project_code, specification_id, reference_type, raw_reference
LIMIT 100;

SELECT
    source_specification_key,
    target_specification_key,
    target_external_key,
    extraction_method,
    confidence,
    status,
    evidence_count
FROM public.spec_reference_current_edges
ORDER BY source_specification_id, relation_id;

SELECT
    source_specification_key,
    target_external_key,
    extraction_method,
    confidence,
    status
FROM public.spec_reference_unresolved_current
ORDER BY source_specification_id, relation_id;
