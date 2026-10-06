-- Document Intelligence Console Enterprise
-- RAG Schema v6 extension: Chunk/Evidence Exact Mapping v1
-- Non-destructive. Main document_ingestion_rag schema remains v6.
--
-- Authoritative mapping:
--     relation_evidence.content_id -> contents.id
--
-- All snapshot columns below are audit invariants. They are never used as
-- fallback keys and therefore cannot silently remap an Evidence row.

BEGIN;

DO $$
BEGIN
    IF to_regclass('public.relation_evidence') IS NULL THEN
        RAISE EXCEPTION 'public.relation_evidence is missing. Upgrade to RAG Schema v6 first.';
    END IF;
    IF to_regclass('public.contents') IS NULL THEN
        RAISE EXCEPTION 'public.contents is missing.';
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'relation_evidence'
          AND column_name = 'content_id'
    ) THEN
        RAISE EXCEPTION 'public.relation_evidence.content_id is missing. This is not a compatible v6 schema.';
    END IF;
END
$$;

ALTER TABLE public.relation_evidence
    ADD COLUMN IF NOT EXISTS chunk_content_id_snapshot BIGINT,
    ADD COLUMN IF NOT EXISTS chunk_document_id_snapshot TEXT,
    ADD COLUMN IF NOT EXISTS chunk_chapter_id_snapshot TEXT,
    ADD COLUMN IF NOT EXISTS chunk_section_id_snapshot TEXT,
    ADD COLUMN IF NOT EXISTS chunk_page_number_snapshot INTEGER,
    ADD COLUMN IF NOT EXISTS chunk_index_snapshot INTEGER,
    ADD COLUMN IF NOT EXISTS chunk_sort_order_snapshot INTEGER,
    ADD COLUMN IF NOT EXISTS chunk_content_hash_snapshot TEXT,
    ADD COLUMN IF NOT EXISTS chunk_mapping_captured_at TIMESTAMPTZ;

ALTER TABLE public.relation_evidence
    ALTER COLUMN content_id DROP NOT NULL;

CREATE INDEX IF NOT EXISTS idx_relation_evidence_content_id
    ON public.relation_evidence (content_id);

-- Enforce new/updated mappings while allowing historical rows to be audited
-- before a full validation. ON DELETE SET NULL preserves immutable Evidence.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'public.relation_evidence'::regclass
          AND contype = 'f'
          AND conname = 'fk_relation_evidence_content_exact'
    ) THEN
        ALTER TABLE public.relation_evidence
            ADD CONSTRAINT fk_relation_evidence_content_exact
            FOREIGN KEY (content_id)
            REFERENCES public.contents(id)
            ON DELETE SET NULL
            NOT VALID;
    END IF;
END
$$;

CREATE OR REPLACE FUNCTION public.capture_relation_evidence_chunk_snapshot()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    chunk_row public.contents%ROWTYPE;
BEGIN
    IF NEW.content_id IS NULL THEN
        -- Preserve the last exact identity when ON DELETE SET NULL removes
        -- the live mapping. This keeps historical Evidence auditable.
        IF TG_OP = 'UPDATE' AND OLD.content_id IS NOT NULL THEN
            NEW.chunk_content_id_snapshot := COALESCE(
                OLD.chunk_content_id_snapshot,
                OLD.content_id
            );
            NEW.chunk_document_id_snapshot := OLD.chunk_document_id_snapshot;
            NEW.chunk_chapter_id_snapshot := OLD.chunk_chapter_id_snapshot;
            NEW.chunk_section_id_snapshot := OLD.chunk_section_id_snapshot;
            NEW.chunk_page_number_snapshot := OLD.chunk_page_number_snapshot;
            NEW.chunk_index_snapshot := OLD.chunk_index_snapshot;
            NEW.chunk_sort_order_snapshot := OLD.chunk_sort_order_snapshot;
            NEW.chunk_content_hash_snapshot := OLD.chunk_content_hash_snapshot;
            NEW.chunk_mapping_captured_at := COALESCE(
                OLD.chunk_mapping_captured_at,
                NOW()
            );
        ELSIF TG_OP = 'INSERT' THEN
            NEW.chunk_content_id_snapshot := NULL;
            NEW.chunk_mapping_captured_at := NOW();
        END IF;
        RETURN NEW;
    END IF;

    SELECT *
    INTO chunk_row
    FROM public.contents
    WHERE id = NEW.content_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION
            'relation_evidence.content_id % does not reference public.contents.id',
            NEW.content_id
            USING ERRCODE = '23503';
    END IF;

    NEW.chunk_content_id_snapshot := chunk_row.id;
    NEW.chunk_document_id_snapshot := chunk_row.document_id::TEXT;
    NEW.chunk_chapter_id_snapshot := chunk_row.chapter_id::TEXT;
    NEW.chunk_section_id_snapshot := chunk_row.section_id::TEXT;
    NEW.chunk_page_number_snapshot := chunk_row.page_number;
    NEW.chunk_index_snapshot := chunk_row.chunk_index;
    NEW.chunk_sort_order_snapshot := chunk_row.sort_order;
    NEW.chunk_content_hash_snapshot := chunk_row.content_hash;
    NEW.chunk_mapping_captured_at := NOW();
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS trg_relation_evidence_chunk_snapshot
    ON public.relation_evidence;

CREATE TRIGGER trg_relation_evidence_chunk_snapshot
BEFORE INSERT OR UPDATE OF content_id
ON public.relation_evidence
FOR EACH ROW
EXECUTE FUNCTION public.capture_relation_evidence_chunk_snapshot();

-- Backfill all currently resolvable v6 Evidence rows from the canonical Chunk.
UPDATE public.relation_evidence AS re
SET
    chunk_content_id_snapshot = c.id,
    chunk_document_id_snapshot = c.document_id::TEXT,
    chunk_chapter_id_snapshot = c.chapter_id::TEXT,
    chunk_section_id_snapshot = c.section_id::TEXT,
    chunk_page_number_snapshot = c.page_number,
    chunk_index_snapshot = c.chunk_index,
    chunk_sort_order_snapshot = c.sort_order,
    chunk_content_hash_snapshot = c.content_hash,
    chunk_mapping_captured_at = COALESCE(re.chunk_mapping_captured_at, NOW())
FROM public.contents AS c
WHERE re.content_id = c.id
  AND re.chunk_mapping_captured_at IS NULL;

-- One-hop audit/read model. re.* keeps every v6 Evidence location field
-- (page/sheet/row/cell/chapter/section/evidence_text) without coupling this
-- extension to their exact names.
CREATE OR REPLACE VIEW public.relation_evidence_chunk_map AS
SELECT
    COALESCE(
        NULLIF(to_jsonb(re) ->> 'evidence_id', '')::BIGINT,
        NULLIF(to_jsonb(re) ->> 'id', '')::BIGINT
    ) AS mapped_evidence_id,
    re.*,
    c.document_id AS mapped_chunk_document_id,
    c.chapter_id AS mapped_chunk_chapter_id,
    c.section_id AS mapped_chunk_section_id,
    c.page_number AS mapped_chunk_page_number,
    c.chunk_index AS mapped_chunk_index,
    c.sort_order AS mapped_chunk_sort_order,
    c.token_count AS mapped_chunk_token_count,
    c.content_hash AS mapped_chunk_live_content_hash,
    c.metadata AS mapped_chunk_metadata,
    c.content AS mapped_chunk_content,
    CASE
        WHEN re.content_id IS NULL
         AND re.chunk_content_id_snapshot IS NOT NULL THEN 'CHUNK_MISSING'
        WHEN re.content_id IS NULL THEN 'UNMAPPED'
        WHEN c.id IS NULL THEN 'CHUNK_MISSING'
        WHEN re.chunk_document_id_snapshot IS DISTINCT FROM c.document_id::TEXT
          OR re.chunk_chapter_id_snapshot IS DISTINCT FROM c.chapter_id::TEXT
          OR re.chunk_section_id_snapshot IS DISTINCT FROM c.section_id::TEXT
          OR re.chunk_page_number_snapshot IS DISTINCT FROM c.page_number
          OR re.chunk_index_snapshot IS DISTINCT FROM c.chunk_index
          OR re.chunk_sort_order_snapshot IS DISTINCT FROM c.sort_order
            THEN 'IDENTITY_CHANGED'
        WHEN re.chunk_content_hash_snapshot IS DISTINCT FROM c.content_hash
            THEN 'CONTENT_CHANGED'
        ELSE 'EXACT'
    END AS mapping_status
FROM public.relation_evidence AS re
LEFT JOIN public.contents AS c
  ON c.id = re.content_id;

COMMENT ON VIEW public.relation_evidence_chunk_map IS
'Exact Evidence->Chunk mapping: relation_evidence.content_id joins contents.id. Snapshot fields are audit invariants only; no fuzzy/chunk_index fallback is performed.';

-- Track this additive capability independently from main RAG Schema v6.
INSERT INTO public.schema_version (component, version, name, applied_at)
VALUES (
    'document_ingestion_chunk_evidence_mapping',
    1,
    'chunk-evidence-exact-v1',
    NOW()
)
ON CONFLICT (component)
DO UPDATE SET
    version = EXCLUDED.version,
    name = EXCLUDED.name,
    applied_at = EXCLUDED.applied_at;

COMMIT;
