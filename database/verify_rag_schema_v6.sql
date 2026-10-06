-- RAG Schema v6 / Engineering Relations verification
-- DDL source of truth: app/storage/schema_manager.py

-- 1) Schema versions
SELECT component, version, name, applied_at
FROM public.schema_version
ORDER BY component;

-- 2) Required relation tables
SELECT table_name
FROM information_schema.tables
WHERE table_schema = 'public'
  AND table_name IN (
      'relation_extraction_runs',
      'engineering_entities',
      'spec_relations',
      'relation_evidence'
  )
ORDER BY table_name;

-- 3) Extraction run audit history
SELECT
    specification_name,
    version_label,
    extractor_version,
    status,
    is_current,
    entity_count,
    relation_count,
    unresolved_count,
    created_at
FROM public.relation_extraction_status
ORDER BY specification_name, version_id, extraction_run_id DESC;

-- 4) Current engineering entities
SELECT
    specification_name,
    version_label,
    entity_type,
    entity_key,
    canonical_name,
    extraction_method,
    confidence,
    status
FROM public.engineering_entity_catalog
WHERE is_current_extraction IS TRUE
  AND is_current_source_version IS TRUE
ORDER BY specification_name, entity_type, canonical_name;

-- 5) Current confirmed relation graph facts (default Knowledge Graph source)
SELECT
    source_specification_name,
    source_version,
    relation_type,
    COALESCE(target_specification_name, target_entity_name, target_external_key) AS target,
    extraction_method,
    confidence,
    evidence_count
FROM public.spec_current_relations
ORDER BY source_specification_name, relation_type, target;

-- 6) Unresolved references preserved for later reconciliation
SELECT
    source_specification_name,
    source_version,
    relation_type,
    target_external_key,
    extraction_method,
    confidence,
    evidence_count
FROM public.engineering_relation_catalog
WHERE is_current_extraction IS TRUE
  AND status = 'UNRESOLVED'
ORDER BY source_specification_name, target_external_key;

-- 7) Evidence for one relationship / specification
-- Replace the name filter as needed.
SELECT
    source_specification_name,
    source_version,
    relation_type,
    COALESCE(target_specification_name, target_entity_name, target_external_key) AS target,
    document_id,
    page_number,
    sheet_name,
    cell_range,
    chapter_title,
    section_title,
    evidence_text
FROM public.relation_evidence_detail
WHERE source_specification_name ILIKE '%複合機能評価設計書%'
ORDER BY relation_id, evidence_id;

-- 8) Version/change history remains intact
SELECT
    specification_key,
    canonical_name,
    version_label,
    previous_version_label,
    version_role,
    deterministic_change_summary
FROM public.specification_version_history
ORDER BY specification_key, version_sort_key DESC NULLS LAST, version_seq DESC;

-- 9) RAG default remains Current-only
SELECT *
FROM public.rag_current_chunks
ORDER BY document_id, sort_order, content_id;
