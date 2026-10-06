-- Verification: RAG v6 Chunk/Evidence Exact Mapping extension

-- 1) Extension version
SELECT component, version, name, applied_at
FROM public.schema_version
WHERE component = 'document_ingestion_chunk_evidence_mapping';

-- 2) Required objects
SELECT
    to_regclass('public.relation_evidence') AS relation_evidence,
    to_regclass('public.contents') AS contents,
    to_regclass('public.relation_evidence_chunk_map') AS evidence_chunk_map;

-- 3) FK state: convalidated=false is acceptable immediately after upgrade;
--    new writes are still enforced. Validate later after all historical rows pass.
SELECT conname, convalidated
FROM pg_constraint
WHERE conrelid = 'public.relation_evidence'::regclass
  AND conname = 'fk_relation_evidence_content_exact';

-- 4) Mapping health
SELECT mapping_status, COUNT(*) AS evidence_count
FROM public.relation_evidence_chunk_map
GROUP BY mapping_status
ORDER BY mapping_status;

-- 5) Broken/unmapped evidence (must be investigated, never fuzzy-remapped)
SELECT
    mapped_evidence_id,
    relation_id,
    content_id,
    mapping_status,
    chunk_content_id_snapshot,
    chunk_document_id_snapshot,
    chunk_section_id_snapshot,
    chunk_page_number_snapshot,
    chunk_index_snapshot,
    chunk_content_hash_snapshot,
    mapped_chunk_document_id,
    mapped_chunk_section_id,
    mapped_chunk_page_number,
    mapped_chunk_index,
    mapped_chunk_live_content_hash
FROM public.relation_evidence_chunk_map
WHERE mapping_status <> 'EXACT'
ORDER BY mapped_evidence_id;

-- 6) Evidence -> canonical chunk one-hop trace
SELECT
    mapped_evidence_id,
    relation_id,
    content_id,
    mapping_status,
    mapped_chunk_document_id,
    mapped_chunk_chapter_id,
    mapped_chunk_section_id,
    mapped_chunk_page_number,
    mapped_chunk_index,
    mapped_chunk_sort_order,
    mapped_chunk_token_count,
    mapped_chunk_live_content_hash,
    LEFT(mapped_chunk_content, 500) AS chunk_preview
FROM public.relation_evidence_chunk_map
ORDER BY mapped_evidence_id
LIMIT 200;

-- 7) Validate FK only after query #5 confirms there are no legacy orphan ids.
-- ALTER TABLE public.relation_evidence
--   VALIDATE CONSTRAINT fk_relation_evidence_content_exact;
