-- v5.9.2-CN QA/BUG schema v2 additive migration.
-- Non-destructive: no DROP/TRUNCATE; existing QA/BUG records/chunks are preserved.
BEGIN;

CREATE TABLE IF NOT EXISTS public.qabug_qdrant_sync_runs (
    id BIGSERIAL PRIMARY KEY,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    collection_name TEXT NOT NULL,
    source_count BIGINT NOT NULL DEFAULT 0,
    qdrant_before_count BIGINT NOT NULL DEFAULT 0,
    new_count BIGINT NOT NULL DEFAULT 0,
    changed_count BIGINT NOT NULL DEFAULT 0,
    unchanged_count BIGINT NOT NULL DEFAULT 0,
    stale_count BIGINT NOT NULL DEFAULT 0,
    upserted_count BIGINT NOT NULL DEFAULT 0,
    deleted_count BIGINT NOT NULL DEFAULT 0,
    qdrant_after_count BIGINT,
    embedding_model TEXT NOT NULL,
    embedding_version TEXT NOT NULL,
    vector_dimension INTEGER NOT NULL,
    error_message TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ix_qabug_qdrant_sync_runs_started
ON public.qabug_qdrant_sync_runs(collection_name, started_at DESC);

INSERT INTO public.schema_version(component, version, name, applied_at)
VALUES ('qabug', 2, 'enterprise-qabug-rag-v2', CURRENT_TIMESTAMP)
ON CONFLICT (component) DO UPDATE
SET version=GREATEST(public.schema_version.version, EXCLUDED.version),
    name=EXCLUDED.name,
    applied_at=CURRENT_TIMESTAMP;

COMMIT;
