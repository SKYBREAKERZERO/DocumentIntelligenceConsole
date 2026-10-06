-- Document Intelligence Console v5.3.0
-- Current-only PostgreSQL -> Qdrant operational sync schema.
-- PostgreSQL remains the source of truth; Qdrant is a derived current index.

BEGIN;

DO $$
BEGIN
    IF to_regclass('public.rag_current_chunks') IS NULL
       OR to_regclass('public.embeddings') IS NULL
       OR to_regclass('public.vector_delete_queue') IS NULL THEN
        RAISE EXCEPTION 'RAG Schema v6 with rag_current_chunks / embeddings / vector_delete_queue is required first.';
    END IF;
END
$$;

ALTER TABLE public.embeddings
    ADD COLUMN IF NOT EXISTS index_hash TEXT,
    ADD COLUMN IF NOT EXISTS source_view TEXT,
    ADD COLUMN IF NOT EXISTS last_synced_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS is_current_index BOOLEAN NOT NULL DEFAULT TRUE;

CREATE INDEX IF NOT EXISTS idx_embeddings_qdrant_current_sync
ON public.embeddings(qdrant_collection, model_name, model_version, content_id);

CREATE TABLE IF NOT EXISTS public.qdrant_sync_runs (
    id BIGSERIAL PRIMARY KEY,
    project_code TEXT NOT NULL,
    database_name TEXT NOT NULL,
    collection_name TEXT NOT NULL,
    source_view TEXT NOT NULL DEFAULT 'public.rag_current_chunks',
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    embedding_model TEXT NOT NULL,
    embedding_version TEXT NOT NULL,
    vector_dimension INTEGER NOT NULL,
    source_count INTEGER NOT NULL DEFAULT 0,
    qdrant_before_count INTEGER NOT NULL DEFAULT 0,
    new_count INTEGER NOT NULL DEFAULT 0,
    changed_count INTEGER NOT NULL DEFAULT 0,
    unchanged_count INTEGER NOT NULL DEFAULT 0,
    stale_count INTEGER NOT NULL DEFAULT 0,
    upserted_count INTEGER NOT NULL DEFAULT 0,
    deleted_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    qdrant_after_count INTEGER,
    drift_delta INTEGER NOT NULL DEFAULT 0,
    warnings JSONB NOT NULL DEFAULT '[]'::jsonb,
    error_message TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_qdrant_sync_runs_scope_started
ON public.qdrant_sync_runs(project_code, collection_name, started_at DESC);

INSERT INTO public.schema_version(component, version, name, applied_at)
VALUES (
    'document_ingestion_qdrant_current_sync',
    1,
    'current-only-qdrant-sync-v1',
    NOW()
)
ON CONFLICT (component)
DO UPDATE SET
    version = EXCLUDED.version,
    name = EXCLUDED.name,
    applied_at = EXCLUDED.applied_at;

COMMIT;
