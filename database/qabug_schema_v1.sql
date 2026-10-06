-- Document Intelligence Console v5.9.2-CN

-- Compatibility bootstrap file; provisions current Enterprise QA/BUG RAG schema v2

-- Execute after connecting to target database rag_qabug.

-- Idempotent/non-destructive: no DROP/TRUNCATE.

BEGIN;

CREATE TABLE IF NOT EXISTS public.schema_version (
                component TEXT PRIMARY KEY,
                version INTEGER NOT NULL,
                name TEXT NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

CREATE TABLE IF NOT EXISTS public.qabug_source_files (
                id BIGSERIAL PRIMARY KEY,
                source_file_name TEXT NOT NULL,
                source_file_path TEXT,
                source_format TEXT NOT NULL,
                record_type TEXT NOT NULL CHECK (record_type IN ('QA','BUG')),
                source_hash TEXT NOT NULL,
                file_size_bytes BIGINT,
                row_count BIGINT,
                metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                first_seen_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_imported_at TIMESTAMPTZ,
                UNIQUE(record_type, source_hash)
            );

CREATE TABLE IF NOT EXISTS public.qabug_import_runs (
                id BIGSERIAL PRIMARY KEY,
                source_file_id BIGINT REFERENCES public.qabug_source_files(id),
                record_type TEXT NOT NULL CHECK (record_type IN ('QA','BUG')),
                mode TEXT NOT NULL DEFAULT 'UPSERT',
                status TEXT NOT NULL DEFAULT 'RUNNING',
                started_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                finished_at TIMESTAMPTZ,
                rows_seen BIGINT NOT NULL DEFAULT 0,
                rows_valid BIGINT NOT NULL DEFAULT 0,
                rows_upserted BIGINT NOT NULL DEFAULT 0,
                rows_skipped BIGINT NOT NULL DEFAULT 0,
                rows_failed BIGINT NOT NULL DEFAULT 0,
                chunks_upserted BIGINT NOT NULL DEFAULT 0,
                qdrant_points_upserted BIGINT NOT NULL DEFAULT 0,
                options JSONB NOT NULL DEFAULT '{}'::jsonb,
                error_summary TEXT
            );

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

CREATE TABLE IF NOT EXISTS public.qabug_records (
                id BIGSERIAL PRIMARY KEY,
                record_key TEXT NOT NULL UNIQUE,
                record_type TEXT NOT NULL CHECK (record_type IN ('QA','BUG')),
                issue_key TEXT,
                issue_id TEXT,
                parent_issue_id TEXT,
                issue_type TEXT,
                project_key TEXT,
                project_name TEXT,
                summary TEXT,
                description TEXT,
                status_raw TEXT,
                status_norm TEXT NOT NULL DEFAULT 'UNSPECIFIED',
                priority_raw TEXT,
                priority_norm TEXT NOT NULL DEFAULT 'UNSPECIFIED',
                resolution_raw TEXT,
                resolution_norm TEXT NOT NULL DEFAULT 'UNSPECIFIED',
                assignee TEXT,
                reporter TEXT,
                creator TEXT,
                component TEXT,
                release_plan_version TEXT,
                affected_version TEXT,
                software_version TEXT,
                grade TEXT,
                destination TEXT,
                model TEXT,
                environment TEXT,
                labels TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
                created_at_source TIMESTAMPTZ,
                updated_at_source TIMESTAMPTZ,
                resolved_at_source TIMESTAMPTZ,
                due_at_source TIMESTAMPTZ,
                source_file_id BIGINT REFERENCES public.qabug_source_files(id),
                source_sheet TEXT,
                source_row_number BIGINT,
                source_hash TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                index_hash TEXT NOT NULL,
                raw_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                custom_fields JSONB NOT NULL DEFAULT '{}'::jsonb,
                normalized_fields JSONB NOT NULL DEFAULT '{}'::jsonb,
                quality_flags JSONB NOT NULL DEFAULT '[]'::jsonb,
                acl JSONB NOT NULL DEFAULT '{}'::jsonb,
                is_queryable BOOLEAN NOT NULL DEFAULT TRUE,
                is_current BOOLEAN NOT NULL DEFAULT TRUE,
                imported_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(record_type, issue_key)
            );

ALTER TABLE public.qabug_records ADD COLUMN IF NOT EXISTS is_queryable BOOLEAN NOT NULL DEFAULT TRUE;

CREATE TABLE IF NOT EXISTS public.qa_details (
                record_id BIGINT PRIMARY KEY REFERENCES public.qabug_records(id) ON DELETE CASCADE,
                precondition TEXT,
                procedure_expected TEXT,
                actual_behavior TEXT,
                answer TEXT,
                answer_type TEXT,
                answer_requested_at TIMESTAMPTZ,
                final_answer_group TEXT,
                final_answer TEXT,
                final_answerer TEXT,
                rationale TEXT,
                reference_spec TEXT,
                sheet_name TEXT,
                test_spec_name TEXT,
                test_spec_defect_detail TEXT,
                item_cell TEXT,
                responsible_group TEXT,
                change_target TEXT,
                customer_report TEXT
            );

CREATE TABLE IF NOT EXISTS public.bug_details (
                record_id BIGINT PRIMARY KEY REFERENCES public.qabug_records(id) ON DELETE CASCADE,
                phenomenon TEXT,
                repro_steps TEXT,
                expected_result TEXT,
                actual_result TEXT,
                analysis_result TEXT,
                analysis_comment TEXT,
                analysis_owner TEXT,
                root_cause TEXT,
                cause_module TEXT,
                cause_company TEXT,
                countermeasure TEXT,
                countermeasure_comment TEXT,
                countermeasure_owner TEXT,
                countermeasure_scope TEXT,
                effect_result TEXT,
                effect_version TEXT,
                effect_date TIMESTAMPTZ,
                recurrence_prevention_escape TEXT,
                recurrence_prevention_injection TEXT,
                impact_analysis TEXT,
                impact TEXT,
                occurrence_probability TEXT,
                occurrence_frequency TEXT,
                occurrence_date TIMESTAMPTZ,
                occurrence_environment_detail TEXT,
                recovery_procedure TEXT,
                problem_type TEXT,
                function_class TEXT
            );

CREATE TABLE IF NOT EXISTS public.qabug_relations (
                id BIGSERIAL PRIMARY KEY,
                source_record_id BIGINT NOT NULL REFERENCES public.qabug_records(id) ON DELETE CASCADE,
                relation_type TEXT NOT NULL,
                target_issue_key TEXT NOT NULL,
                source_field TEXT,
                evidence_text TEXT,
                relation_hash TEXT NOT NULL,
                metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(source_record_id, relation_hash)
            );

CREATE TABLE IF NOT EXISTS public.qabug_comments (
                id BIGSERIAL PRIMARY KEY,
                record_id BIGINT NOT NULL REFERENCES public.qabug_records(id) ON DELETE CASCADE,
                sequence_no INTEGER NOT NULL,
                commented_at TIMESTAMPTZ,
                author TEXT,
                body TEXT NOT NULL,
                comment_hash TEXT NOT NULL,
                metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                UNIQUE(record_id, comment_hash)
            );

CREATE TABLE IF NOT EXISTS public.qabug_attachments (
                id BIGSERIAL PRIMARY KEY,
                record_id BIGINT NOT NULL REFERENCES public.qabug_records(id) ON DELETE CASCADE,
                file_name TEXT,
                uri TEXT,
                added_at TIMESTAMPTZ,
                added_by TEXT,
                attachment_hash TEXT NOT NULL,
                metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                UNIQUE(record_id, attachment_hash)
            );

CREATE TABLE IF NOT EXISTS public.qabug_chunks (
                id BIGSERIAL PRIMARY KEY,
                record_id BIGINT NOT NULL REFERENCES public.qabug_records(id) ON DELETE CASCADE,
                chunk_key TEXT NOT NULL UNIQUE,
                section_type TEXT NOT NULL,
                section_order INTEGER NOT NULL,
                content TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                index_hash TEXT NOT NULL,
                token_count INTEGER,
                metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                qdrant_point_id BIGINT NOT NULL UNIQUE,
                qdrant_collection TEXT NOT NULL DEFAULT 'rag_qabug_current_chunks',
                embedding_status TEXT NOT NULL DEFAULT 'PENDING',
                embedding_model TEXT NOT NULL DEFAULT 'BAAI/bge-m3',
                embedding_version TEXT NOT NULL DEFAULT 'current-index-v1',
                embedded_content_hash TEXT,
                embedded_at TIMESTAMPTZ,
                is_current BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

CREATE TABLE IF NOT EXISTS public.qabug_embedding_state (
                chunk_id BIGINT PRIMARY KEY REFERENCES public.qabug_chunks(id) ON DELETE CASCADE,
                qdrant_collection TEXT NOT NULL,
                qdrant_point_id BIGINT NOT NULL,
                content_hash TEXT NOT NULL,
                index_hash TEXT NOT NULL,
                embedding_model TEXT NOT NULL,
                embedding_version TEXT NOT NULL,
                vector_dimension INTEGER NOT NULL,
                synced_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(qdrant_collection, qdrant_point_id)
            );

CREATE TABLE IF NOT EXISTS public.qabug_quality_findings (
                id BIGSERIAL PRIMARY KEY,
                import_run_id BIGINT REFERENCES public.qabug_import_runs(id) ON DELETE SET NULL,
                record_id BIGINT REFERENCES public.qabug_records(id) ON DELETE CASCADE,
                severity TEXT NOT NULL CHECK (severity IN ('INFO','WARN','ERROR')),
                code TEXT NOT NULL,
                field_name TEXT,
                message TEXT NOT NULL,
                details JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                resolved_at TIMESTAMPTZ
            );

CREATE TABLE IF NOT EXISTS public.qabug_import_errors (
                id BIGSERIAL PRIMARY KEY,
                import_run_id BIGINT NOT NULL REFERENCES public.qabug_import_runs(id) ON DELETE CASCADE,
                source_row_number BIGINT,
                issue_key TEXT,
                error_code TEXT NOT NULL,
                error_message TEXT NOT NULL,
                raw_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

CREATE INDEX IF NOT EXISTS ix_qabug_records_type_status ON public.qabug_records(record_type, status_norm) WHERE is_current AND is_queryable;

CREATE INDEX IF NOT EXISTS ix_qabug_records_type_priority ON public.qabug_records(record_type, priority_norm) WHERE is_current AND is_queryable;

CREATE INDEX IF NOT EXISTS ix_qabug_records_type_resolution ON public.qabug_records(record_type, resolution_norm) WHERE is_current AND is_queryable;

CREATE INDEX IF NOT EXISTS ix_qabug_records_component_status ON public.qabug_records(component, status_norm) WHERE is_current AND is_queryable AND component IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_qabug_records_assignee_status ON public.qabug_records(assignee, status_norm) WHERE is_current AND is_queryable AND assignee IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_qabug_records_project_updated ON public.qabug_records(project_key, updated_at_source DESC NULLS LAST) WHERE is_current AND is_queryable;

CREATE INDEX IF NOT EXISTS ix_qabug_records_release_version ON public.qabug_records(release_plan_version) WHERE is_current AND is_queryable AND release_plan_version IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_qabug_records_affected_version ON public.qabug_records(affected_version) WHERE is_current AND is_queryable AND affected_version IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_qabug_records_created ON public.qabug_records(created_at_source DESC NULLS LAST) WHERE is_current AND is_queryable;

CREATE INDEX IF NOT EXISTS ix_qabug_records_updated ON public.qabug_records(updated_at_source DESC NULLS LAST) WHERE is_current AND is_queryable;

CREATE INDEX IF NOT EXISTS ix_qabug_records_type_issue_type ON public.qabug_records(record_type, issue_type) WHERE is_current AND is_queryable AND issue_type IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_qabug_records_reporter_updated ON public.qabug_records(reporter, updated_at_source DESC NULLS LAST) WHERE is_current AND is_queryable AND reporter IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_qabug_records_destination_type ON public.qabug_records(destination, record_type) WHERE is_current AND is_queryable AND destination IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_qabug_records_model_type ON public.qabug_records(model, record_type) WHERE is_current AND is_queryable AND model IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_qabug_source_files_name ON public.qabug_source_files(record_type, source_file_name);

CREATE INDEX IF NOT EXISTS ix_qa_details_answer_type ON public.qa_details(answer_type) WHERE answer_type IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_qa_details_final_answerer ON public.qa_details(final_answerer) WHERE final_answerer IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_qa_details_responsible_group ON public.qa_details(responsible_group) WHERE responsible_group IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_bug_details_cause_module ON public.bug_details(cause_module) WHERE cause_module IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_bug_details_analysis_owner ON public.bug_details(analysis_owner) WHERE analysis_owner IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_bug_details_countermeasure_owner ON public.bug_details(countermeasure_owner) WHERE countermeasure_owner IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_bug_details_effect_result ON public.bug_details(effect_result) WHERE effect_result IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_bug_details_problem_type ON public.bug_details(problem_type) WHERE problem_type IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_bug_details_function_class ON public.bug_details(function_class) WHERE function_class IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_qabug_chunks_record_current ON public.qabug_chunks(record_id, section_order) WHERE is_current;

CREATE INDEX IF NOT EXISTS ix_qabug_chunks_embed_pending ON public.qabug_chunks(embedding_status, id) WHERE is_current AND embedding_status <> 'COMPLETED';

CREATE INDEX IF NOT EXISTS ix_qabug_relations_target ON public.qabug_relations(target_issue_key, relation_type);

CREATE INDEX IF NOT EXISTS ix_qabug_relations_source_type ON public.qabug_relations(source_record_id, relation_type);

CREATE INDEX IF NOT EXISTS ix_qabug_quality_open ON public.qabug_quality_findings(severity, code, created_at DESC) WHERE resolved_at IS NULL;

CREATE INDEX IF NOT EXISTS ix_qabug_records_quarantine ON public.qabug_records(record_type, source_file_id, source_row_number) WHERE is_current AND NOT is_queryable;

CREATE INDEX IF NOT EXISTS ix_bug_details_effect_version ON public.bug_details(effect_version) WHERE effect_version IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_bug_details_effect_date ON public.bug_details(effect_date) WHERE effect_date IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_bug_details_occurrence_date ON public.bug_details(occurrence_date) WHERE occurrence_date IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_qa_details_answer_requested_at ON public.qa_details(answer_requested_at) WHERE answer_requested_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_qabug_import_errors_run ON public.qabug_import_errors(import_run_id, created_at);
CREATE INDEX IF NOT EXISTS ix_qabug_qdrant_sync_runs_started ON public.qabug_qdrant_sync_runs(collection_name, started_at DESC);

CREATE INDEX IF NOT EXISTS ix_qabug_records_labels_gin ON public.qabug_records USING GIN(labels);

CREATE INDEX IF NOT EXISTS ix_qabug_records_custom_fields_gin ON public.qabug_records USING GIN(custom_fields jsonb_path_ops);

CREATE INDEX IF NOT EXISTS ix_qabug_records_fts ON public.qabug_records USING GIN(to_tsvector('simple', COALESCE(summary,'') || ' ' || COALESCE(description,'')));

CREATE INDEX IF NOT EXISTS ix_qabug_chunks_fts ON public.qabug_chunks USING GIN(to_tsvector('simple', content));

CREATE OR REPLACE VIEW public.qabug_current_records AS
            SELECT r.*, q.precondition, q.procedure_expected, q.actual_behavior, q.answer, q.answer_type,
                   q.final_answer, q.final_answerer, q.rationale, q.reference_spec, q.test_spec_name,
                   b.phenomenon, b.repro_steps, b.expected_result, b.actual_result, b.analysis_result,
                   b.root_cause, b.cause_module, b.countermeasure, b.effect_result, b.effect_version,
                   b.impact_analysis, b.impact, b.recovery_procedure
            FROM public.qabug_records r
            LEFT JOIN public.qa_details q ON q.record_id=r.id
            LEFT JOIN public.bug_details b ON b.record_id=r.id
            WHERE r.is_current AND r.is_queryable;

CREATE OR REPLACE VIEW public.qabug_current_chunks AS
            SELECT c.id AS content_id,
                   r.record_key AS document_id,
                   sf.source_file_name AS file_name,
                   'QABUG'::TEXT AS project_code,
                   NULL::TEXT AS specification_code,
                   r.summary AS specification_name,
                   NULL::TEXT AS specification_version,
                   c.section_type AS chapter_title,
                   c.section_type AS section_id,
                   c.section_type AS section_title,
                   NULL::INTEGER AS page_number,
                   c.section_order - 1 AS chunk_index,
                   c.content_hash,
                   c.content,
                   c.qdrant_point_id,
                   c.index_hash,
                   c.embedding_status,
                   c.embedding_model,
                   c.embedding_version,
                   c.embedded_content_hash,
                   c.embedded_at,
                   r.record_type,
                   r.issue_key,
                   r.issue_id,
                   r.issue_type,
                   r.status_raw,
                   r.status_norm,
                   r.priority_raw,
                   r.priority_norm,
                   r.resolution_raw,
                   r.resolution_norm,
                   r.assignee,
                   r.reporter,
                   r.component,
                   r.release_plan_version,
                   r.affected_version,
                   r.software_version,
                   r.destination,
                   r.model,
                   r.environment,
                   r.source_row_number,
                   c.section_type,
                   c.metadata,
                   r.raw_json,
                   r.custom_fields,
                   r.quality_flags
            FROM public.qabug_chunks c
            JOIN public.qabug_records r ON r.id=c.record_id
            LEFT JOIN public.qabug_source_files sf ON sf.id=r.source_file_id
            WHERE c.is_current AND r.is_current AND r.is_queryable;

CREATE OR REPLACE VIEW public.qabug_record_catalog AS
            SELECT record_type, issue_key, issue_id, issue_type, status_raw, status_norm,
                   priority_raw, priority_norm, resolution_raw, resolution_norm,
                   project_key, project_name, component, assignee, reporter,
                   release_plan_version, affected_version, software_version,
                   summary, created_at_source, updated_at_source, resolved_at_source,
                   source_sheet, source_row_number, quality_flags, is_queryable
            FROM public.qabug_records
            WHERE is_current AND is_queryable;

CREATE OR REPLACE VIEW public.qabug_quarantine_records AS
            SELECT r.id, r.record_key, r.record_type, r.issue_key, r.issue_id, r.summary,
                   r.source_file_id, sf.source_file_name, r.source_sheet, r.source_row_number,
                   r.quality_flags, r.raw_json, r.custom_fields, r.imported_at, r.updated_at
            FROM public.qabug_records r
            LEFT JOIN public.qabug_source_files sf ON sf.id=r.source_file_id
            WHERE r.is_current AND NOT r.is_queryable;

CREATE OR REPLACE VIEW public.qabug_quality_summary AS
            SELECT severity, code, COUNT(*)::BIGINT AS finding_count,
                   COUNT(*) FILTER (WHERE resolved_at IS NULL)::BIGINT AS open_count,
                   MAX(created_at) AS latest_at
            FROM public.qabug_quality_findings
            GROUP BY severity, code;

INSERT INTO public.schema_version(component, version, name, applied_at) VALUES ('qabug', 2, 'enterprise-qabug-rag-v2', CURRENT_TIMESTAMP) ON CONFLICT (component) DO UPDATE SET version=GREATEST(public.schema_version.version, EXCLUDED.version), name=EXCLUDED.name, applied_at=CURRENT_TIMESTAMP;

COMMIT;
