-- RAG Schema v4 verification / usage reference
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

-- 3) Main / History
SELECT
    specification_key,
    version_seq,
    version_label,
    version_role,
    document_id,
    created_at
FROM public.specification_version_history
ORDER BY specification_key, version_seq DESC;

-- 4) Default RAG query: Current only
SELECT *
FROM public.rag_current_chunks
ORDER BY document_id, sort_order, content_id;

-- 5) Explicit historical/all-version query
SELECT *
FROM public.rag_chunks
ORDER BY document_id, sort_order, content_id;
