-- Stage1 v4: explicit, additive provenance-bound review and immutable audit.
-- Apply only after a verified backup and v7, v2 and v3 schema migrations.
-- Does not change requirement units, prior candidate statuses or relations.
BEGIN;
CREATE TABLE IF NOT EXISTS public.ei_stage1_review_proposals (
    proposal_key CHAR(64) PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('SCOPE','TABLE')),
    source_key CHAR(64) NOT NULL,
    version_id BIGINT NOT NULL REFERENCES public.specification_versions(id) ON DELETE RESTRICT,
    extraction_version TEXT NOT NULL,
    evidence_digest CHAR(64) NOT NULL,
    source_text_digest CHAR(64) NOT NULL,
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload)='object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(kind,source_key,extraction_version)
);
CREATE INDEX IF NOT EXISTS ix_ei_stage1_review_version
    ON public.ei_stage1_review_proposals(version_id,kind,proposal_key);
CREATE TABLE IF NOT EXISTS public.ei_stage1_review_receipts (
    version_id BIGINT PRIMARY KEY REFERENCES public.specification_versions(id) ON DELETE RESTRICT,
    source_units_digest CHAR(64) NOT NULL,
    proposal_set_digest CHAR(64) NOT NULL,
    source_unit_count BIGINT NOT NULL CHECK (source_unit_count >= 0),
    proposal_count BIGINT NOT NULL CHECK (proposal_count >= 0),
    extraction_version TEXT NOT NULL,
    completed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Append-only reviewer decisions. Never overwrite earlier human judgements.
CREATE TABLE IF NOT EXISTS public.ei_stage1_review_events (
    id BIGSERIAL PRIMARY KEY,
    request_id UUID NOT NULL UNIQUE,
    proposal_key CHAR(64) NOT NULL REFERENCES public.ei_stage1_review_proposals(proposal_key) ON DELETE RESTRICT,
    evidence_digest CHAR(64) NOT NULL,
    previous_event_id BIGINT,
    reviewer TEXT NOT NULL CHECK (length(btrim(reviewer)) BETWEEN 1 AND 128),
    status TEXT NOT NULL CHECK (status IN ('CONFIRMED','REJECTED','NEEDS_CLARIFICATION')),
    assertion TEXT,
    target_unit_key TEXT,
    reason TEXT NOT NULL CHECK (length(btrim(reason)) BETWEEN 1 AND 2000),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_ei_stage1_review_assertion
        CHECK ((status='CONFIRMED' AND assertion IS NOT NULL)
            OR (status <> 'CONFIRMED' AND assertion IS NULL AND target_unit_key IS NULL))
);
CREATE INDEX IF NOT EXISTS ix_ei_stage1_review_event_latest
    ON public.ei_stage1_review_events(proposal_key,id DESC);
-- DEFENSE IN DEPTH: immutable evidence and audit; use a restricted DB role.
CREATE OR REPLACE FUNCTION public.ei_stage1_v4_deny_mutation() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Stage1 v4 evidence and audit are append-only';
END $$;
DROP TRIGGER IF EXISTS ei_stage1_v4_proposals_immutable ON public.ei_stage1_review_proposals;
CREATE TRIGGER ei_stage1_v4_proposals_immutable BEFORE UPDATE OR DELETE
    ON public.ei_stage1_review_proposals FOR EACH ROW
    EXECUTE FUNCTION public.ei_stage1_v4_deny_mutation();
DROP TRIGGER IF EXISTS ei_stage1_v4_receipts_immutable ON public.ei_stage1_review_receipts;
CREATE TRIGGER ei_stage1_v4_receipts_immutable BEFORE UPDATE OR DELETE
    ON public.ei_stage1_review_receipts FOR EACH ROW
    EXECUTE FUNCTION public.ei_stage1_v4_deny_mutation();
DROP TRIGGER IF EXISTS ei_stage1_v4_events_immutable ON public.ei_stage1_review_events;
CREATE TRIGGER ei_stage1_v4_events_immutable BEFORE UPDATE OR DELETE
    ON public.ei_stage1_review_events FOR EACH ROW
    EXECUTE FUNCTION public.ei_stage1_v4_deny_mutation();
COMMIT;
