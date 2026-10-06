-- Document Intelligence Console v5.2.6
-- Relation current-generation guard / graph integrity extension.
-- Non-destructive: historical runs, relations and evidence are retained.

BEGIN;

DO $$
BEGIN
    IF to_regclass('public.relation_extraction_runs') IS NULL
       OR to_regclass('public.specifications') IS NULL THEN
        RAISE EXCEPTION 'RAG Schema v6 engineering relation tables are required first.';
    END IF;
END
$$;

-- Repair legacy databases that have multiple current runs for one Specification.
-- Prefer the run belonging to specifications.current_version_id, then the
-- newest completed/started run. All older generations remain as audit history.
WITH ranked AS (
    SELECT
        run.id,
        ROW_NUMBER() OVER (
            PARTITION BY run.specification_id
            ORDER BY
                (run.status = 'READY') DESC,
                (run.version_id = sp.current_version_id) DESC,
                run.finished_at DESC NULLS LAST,
                run.started_at DESC NULLS LAST,
                run.id DESC
        ) AS rn
    FROM public.relation_extraction_runs run
    JOIN public.specifications sp ON sp.id = run.specification_id
    WHERE run.is_current IS TRUE
)
UPDATE public.relation_extraction_runs run
SET is_current = FALSE
FROM ranked
WHERE run.id = ranked.id
  AND ranked.rn > 1;

CREATE UNIQUE INDEX IF NOT EXISTS uq_relation_run_current_specification
ON public.relation_extraction_runs(specification_id)
WHERE is_current IS TRUE;

CREATE INDEX IF NOT EXISTS idx_relation_runs_current_ready_spec
ON public.relation_extraction_runs(specification_id, status, id DESC)
WHERE is_current IS TRUE;

INSERT INTO public.schema_version(component, version, name, applied_at)
VALUES (
    'document_ingestion_relation_current_guard',
    1,
    'one-current-run-per-specification-v1',
    NOW()
)
ON CONFLICT (component)
DO UPDATE SET
    version = EXCLUDED.version,
    name = EXCLUDED.name,
    applied_at = EXCLUDED.applied_at;

COMMIT;
