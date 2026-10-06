-- RAG Schema v5 / Specification Change History verification
-- DDL source of truth: app/storage/schema_manager.py

-- 1) Schema version
SELECT component, version, name, applied_at
FROM public.schema_version
ORDER BY component;

-- 2) Specification Master + Current Version
SELECT
    specification_key,
    spec_code,
    spec_type,
    spec_subtype,
    canonical_name,
    current_version,
    version_count,
    history_count
FROM public.specification_catalog
ORDER BY spec_type, spec_code, canonical_name;

-- 3) Business-version history (semantic order, newest first)
SELECT
    specification_key,
    canonical_name,
    version_label,
    previous_version_label,
    version_role,
    added_count,
    modified_count,
    removed_count,
    moved_count,
    deterministic_change_summary,
    document_id,
    created_at
FROM public.specification_version_history
ORDER BY specification_key, version_sort_key DESC NULLS LAST, version_seq DESC;

-- 4) Adjacent version change summaries generated automatically at ingestion/upgrade
SELECT
    specification_key,
    canonical_name,
    from_version,
    to_version,
    is_adjacent,
    added_count,
    modified_count,
    removed_count,
    moved_count,
    unchanged_count,
    total_change_count,
    summary_text
FROM public.specification_change_summary
WHERE is_adjacent IS TRUE
ORDER BY specification_key, to_version_id;

-- 5) Detailed change rows for one specification
-- Replace the name filter as needed.
SELECT
    canonical_name,
    from_version,
    to_version,
    change_type,
    chapter_title,
    section_title,
    old_page_number,
    new_page_number,
    similarity_score,
    old_text,
    new_text
FROM public.specification_change_detail
WHERE canonical_name ILIKE '%複合機能評価設計書%'
ORDER BY to_version, change_type, COALESCE(new_sort_order, old_sort_order, 0);

-- 6) Default RAG query: Current only
SELECT *
FROM public.rag_current_chunks
ORDER BY document_id, sort_order, content_id;

-- 7) Explicit historical/all-version query
SELECT *
FROM public.rag_chunks
ORDER BY document_id, sort_order, content_id;
