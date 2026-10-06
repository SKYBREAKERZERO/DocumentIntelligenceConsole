from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import cmp_to_key
from typing import Any, Iterator, Sequence

import psycopg
from psycopg import sql

from app.analyzer.specification_taxonomy import SpecificationTaxonomyLoader
from app.analyzer.specification_version_resolver import SpecificationVersionResolver
from app.model.document import Document
from app.storage.specification_version_manager import SpecificationVersionManager
from app.storage.specification_change_manager import SpecificationChangeManager
from app.storage.primary_document_policy import PrimarySpecificationDocumentPolicy


RAG_SCHEMA_VERSION = 6
RAG_SCHEMA_NAME = "rag-schema-v6-engineering-relations"
PROJECT_SCHEMA_VERSION = 6
PROJECT_SCHEMA_NAME = "project-routing-taxonomy-version-diff-relations-v3"


class SchemaManagerError(RuntimeError):
    """PostgreSQL RAG schema initialization / verification error."""


@dataclass(frozen=True, slots=True)
class SchemaReport:
    ready: bool
    version: int | None
    required_tables: tuple[str, ...]
    available_tables: tuple[str, ...]
    missing_tables: tuple[str, ...]
    required_views: tuple[str, ...]
    available_views: tuple[str, ...]
    missing_views: tuple[str, ...]
    missing_columns: dict[str, tuple[str, ...]] = field(default_factory=dict)
    created_objects: tuple[str, ...] = ()
    equivalent_indexes_reused: tuple[str, ...] = ()
    duplicate_index_groups: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def status_text(self) -> str:
        if self.ready:
            base = (
                f"RAG Schema v{self.version or RAG_SCHEMA_VERSION} ready | "
                f"Tables {len(self.available_tables)}/{len(self.required_tables)} | "
                f"Views {len(self.available_views)}/{len(self.required_views)}"
            )
            if self.duplicate_index_groups:
                base += (
                    f"\nLegacy duplicate index groups: "
                    f"{len(self.duplicate_index_groups)} (left untouched)"
                )
            return base

        parts = ["RAG schema is not ready"]
        if self.missing_tables:
            parts.append("Missing tables: " + ", ".join(self.missing_tables))
        if self.missing_views:
            parts.append("Missing views: " + ", ".join(self.missing_views))
        if self.missing_columns:
            for table_name, columns in self.missing_columns.items():
                parts.append(
                    f"{table_name} missing columns: " + ", ".join(columns)
                )
        return "\n".join(parts)


class SchemaManager:
    """
    Non-destructive PostgreSQL schema manager for the ingestion/RAG platform.

    Responsibilities:
        - create fresh RAG tables
        - upgrade existing ingestion tables by adding missing columns
        - create only useful RAG/worker indexes
        - avoid creating an index when an equivalent B-Tree already exists
        - create the rag_chunks read view
        - record schema version
        - audit duplicate legacy indexes without automatically dropping them

    This manager never executes DROP TABLE, TRUNCATE or DELETE against business
    data. Legacy duplicate indexes are reported but not automatically removed.
    """

    REQUIRED_TABLE_COLUMNS: dict[str, set[str]] = {
        "project_info": {
            "project_code",
            "project_name",
            "created_at",
            "updated_at",
        },
        "spec_types": {
            "code",
            "display_name",
            "enabled",
            "created_at",
            "updated_at",
        },
        "spec_subtypes": {
            "code",
            "spec_type_code",
            "display_name",
            "enabled",
            "created_at",
            "updated_at",
        },
        "specifications": {
            "id",
            "specification_key",
            "project_code",
            "region_scope",
            "spec_code",
            "spec_type",
            "spec_subtype",
            "canonical_name",
            "current_version_id",
            "status",
            "metadata",
            "created_at",
            "updated_at",
        },
        "specification_versions": {
            "id",
            "specification_id",
            "version_seq",
            "version_label",
            "version_sort_key",
            "branch_name",
            "previous_version_id",
            "document_id",
            "content_hash",
            "status",
            "version_source",
            "revision_reason",
            "change_summary",
            "effective_at",
            "created_at",
            "updated_at",
        },
        "specification_change_sets": {
            "id",
            "specification_id",
            "from_version_id",
            "to_version_id",
            "branch_name",
            "algorithm_version",
            "is_adjacent",
            "status",
            "added_count",
            "modified_count",
            "removed_count",
            "moved_count",
            "unchanged_count",
            "summary_text",
            "created_at",
            "updated_at",
        },
        "specification_changes": {
            "id",
            "change_set_id",
            "specification_id",
            "from_version_id",
            "to_version_id",
            "change_type",
            "entity_type",
            "entity_key",
            "chapter_id",
            "chapter_title",
            "section_id",
            "section_title",
            "old_content_ids",
            "new_content_ids",
            "old_text",
            "new_text",
            "old_hash",
            "new_hash",
            "old_page_number",
            "new_page_number",
            "old_sort_order",
            "new_sort_order",
            "similarity_score",
            "details",
            "created_at",
        },
        "relation_extraction_runs": {
            "id",
            "specification_id",
            "version_id",
            "document_id",
            "extractor_version",
            "status",
            "is_current",
            "entity_count",
            "relation_count",
            "unresolved_count",
            "stats",
            "error_message",
            "started_at",
            "finished_at",
            "created_at",
        },
        "engineering_entities": {
            "id",
            "extraction_run_id",
            "specification_id",
            "version_id",
            "entity_type",
            "entity_key",
            "canonical_name",
            "normalized_value",
            "extraction_method",
            "confidence",
            "status",
            "metadata",
            "created_at",
        },
        "spec_relations": {
            "id",
            "extraction_run_id",
            "relation_key",
            "source_specification_id",
            "source_version_id",
            "source_entity_id",
            "target_specification_id",
            "target_entity_id",
            "target_external_key",
            "relation_type",
            "extraction_method",
            "confidence",
            "status",
            "is_explicit",
            "metadata",
            "created_at",
        },
        "relation_evidence": {
            "id",
            "relation_id",
            "document_id",
            "content_id",
            "page_number",
            "chapter_id",
            "chapter_title",
            "section_id",
            "section_title",
            "sheet_name",
            "cell_range",
            "evidence_text",
            "start_offset",
            "end_offset",
            "evidence_hash",
            "metadata",
            "created_at",
        },
        "documents": {
            "id",
            "document_id",
            "file_name",
            "file_type",
            "title",
            "module",
            "project_code",
            "project_name",
            "project_assignment_source",
            "series",
            "region_scope",
            "spec_type",
            "spec_subtype",
            "metadata",
            "created_at",
            "updated_at",
        },
        "chapters": {
            "id",
            "document_id",
            "chapter_id",
            "title_jp",
            "title_en",
            "sort_order",
            "metadata",
            "created_at",
            "updated_at",
        },
        "sections": {
            "id",
            "document_id",
            "chapter_id",
            "section_id",
            "parent_section_id",
            "title_jp",
            "title_en",
            "level",
            "sort_order",
            "page_number",
            "metadata",
            "created_at",
            "updated_at",
        },
        "contents": {
            "id",
            "document_id",
            "chapter_id",
            "section_id",
            "content",
            "page_number",
            "chunk_index",
            "token_count",
            "sort_order",
            "metadata",
            "content_hash",
            "embedding_status",
            "embedding_started_at",
            "embedded_at",
            "embedding_model",
            "embedding_version",
            "embedded_content_hash",
            "embedding_error",
            "embedding_retry_count",
            "created_at",
            "updated_at",
        },
        "embeddings": {
            "id",
            "content_id",
            "model_name",
            "model_version",
            "vector_dimension",
            "qdrant_collection",
            "qdrant_point_id",
            "content_hash",
            "created_at",
            "updated_at",
        },
        "vector_delete_queue": {
            "id",
            "content_id",
            "document_id",
            "reason",
            "queued_at",
            "processed_at",
            "last_error",
        },
        "schema_version": {
            "component",
            "version",
            "name",
            "applied_at",
        },
    }

    REQUIRED_VIEWS = (
        "rag_chunks",
        "rag_current_chunks",
        "specification_catalog",
        "specification_version_history",
        "specification_change_summary",
        "specification_change_detail",
        "engineering_entity_catalog",
        "engineering_relation_catalog",
        "spec_current_relations",
        "relation_evidence_detail",
        "relation_extraction_status",
    )

    def __init__(
        self,
        *,
        host: str | None = None,
        port: int | None = None,
        database: str | None = None,
        user: str | None = None,
        password: str | None = None,
        connect_timeout: int = 10,
        database_connection: Any | None = None,
        project_code: str | None = None,
        project_name: str | None = None,
    ) -> None:
        self.database_connection = database_connection
        self.host = str(host or "").strip()
        self.port = int(port or 5432)
        self.database = str(database or "").strip()
        self.user = str(user or "").strip()
        self.password = password
        self.connect_timeout = int(connect_timeout)
        self.project_code = str(project_code or "").strip() or None
        self.project_name = str(project_name or "").strip() or None

        if self.database_connection is None:
            missing = [
                name
                for name, value in (
                    ("host", self.host),
                    ("database", self.database),
                    ("user", self.user),
                )
                if not value
            ]
            if missing:
                raise ValueError(
                    "Missing PostgreSQL connection settings: "
                    + ", ".join(missing)
                )
            if self.connect_timeout <= 0:
                raise ValueError("connect_timeout must be greater than 0.")

    @contextmanager
    def _connect(self) -> Iterator[Any]:
        if self.database_connection is not None:
            with self.database_connection.connect() as connection:
                yield connection
            return

        with psycopg.connect(
            host=self.host,
            port=self.port,
            dbname=self.database,
            user=self.user,
            password=self.password,
            connect_timeout=self.connect_timeout,
            application_name="document-ingestion-schema-manager",
        ) as connection:
            yield connection

    def ensure_schema(self) -> SchemaReport:
        created: list[str] = []
        reused: list[str] = []
        warnings: list[str] = []

        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    self._create_base_tables(cursor, created)
                    self._seed_spec_types(cursor)
                    self._seed_spec_subtypes(cursor)
                    self._upgrade_columns(cursor, created)
                    self._backfill_compatibility_columns(cursor)
                    self._backfill_legacy_supporting_document_roles(cursor, created)
                    self._backfill_content_chapter(cursor)
                    self._backfill_specification_version_history(
                        cursor, created=created, warnings=warnings
                    )
                    self._ensure_version_constraints(cursor, created, warnings)
                    self._backfill_version_sort_and_change_history(
                        cursor, created=created, warnings=warnings
                    )
                    self._repair_primary_specification_current_pointers(cursor, created)
                    self._ensure_constraints_and_indexes(
                        cursor,
                        created=created,
                        reused=reused,
                        warnings=warnings,
                    )
                    self._create_rag_views(cursor, created)
                    self._record_project_identity(cursor)
                    self._record_schema_version(cursor)
                connection.commit()
        except Exception as exc:
            raise SchemaManagerError(
                f"Failed to initialize / upgrade RAG schema: {exc}"
            ) from exc

        report = self.inspect_schema()
        return SchemaReport(
            ready=report.ready,
            version=report.version,
            required_tables=report.required_tables,
            available_tables=report.available_tables,
            missing_tables=report.missing_tables,
            required_views=report.required_views,
            available_views=report.available_views,
            missing_views=report.missing_views,
            missing_columns=report.missing_columns,
            created_objects=tuple(created),
            equivalent_indexes_reused=tuple(reused),
            duplicate_index_groups=report.duplicate_index_groups,
            warnings=tuple(warnings) + report.warnings,
        )

    def inspect_schema(self) -> SchemaReport:
        required_tables = tuple(self.REQUIRED_TABLE_COLUMNS)

        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT table_name
                        FROM information_schema.tables
                        WHERE table_schema = 'public'
                          AND table_type = 'BASE TABLE'
                        """
                    )
                    table_names = {row[0] for row in cursor.fetchall()}

                    cursor.execute(
                        """
                        SELECT table_name, column_name
                        FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = ANY(%s)
                        """,
                        (list(required_tables),),
                    )
                    columns: dict[str, set[str]] = {
                        table: set() for table in required_tables
                    }
                    for table_name, column_name in cursor.fetchall():
                        if table_name in columns:
                            columns[table_name].add(column_name)

                    cursor.execute(
                        """
                        SELECT table_name
                        FROM information_schema.views
                        WHERE table_schema = 'public'
                        """
                    )
                    view_names = {row[0] for row in cursor.fetchall()}

                    version = self._read_schema_version(cursor)
                    duplicate_groups = self._find_duplicate_index_groups(cursor)
        except Exception as exc:
            raise SchemaManagerError(
                f"Failed to inspect RAG schema: {exc}"
            ) from exc

        missing_tables = tuple(
            table for table in required_tables if table not in table_names
        )
        available_tables = tuple(
            table for table in required_tables if table in table_names
        )
        missing_views = tuple(
            view for view in self.REQUIRED_VIEWS if view not in view_names
        )
        available_views = tuple(
            view for view in self.REQUIRED_VIEWS if view in view_names
        )

        missing_columns: dict[str, tuple[str, ...]] = {}
        for table, expected in self.REQUIRED_TABLE_COLUMNS.items():
            if table not in table_names:
                continue
            missing = tuple(sorted(expected - columns.get(table, set())))
            if missing:
                missing_columns[table] = missing

        ready = (
            not missing_tables
            and not missing_views
            and not missing_columns
            and version == RAG_SCHEMA_VERSION
        )

        return SchemaReport(
            ready=ready,
            version=version,
            required_tables=required_tables,
            available_tables=available_tables,
            missing_tables=missing_tables,
            required_views=self.REQUIRED_VIEWS,
            available_views=available_views,
            missing_views=missing_views,
            missing_columns=missing_columns,
            duplicate_index_groups=tuple(duplicate_groups),
        )

    # ------------------------------------------------------------------
    # DDL
    # ------------------------------------------------------------------

    def _create_base_tables(self, cursor: Any, created: list[str]) -> None:
        statements: Sequence[tuple[str, str]] = (
            (
                "schema_version",
                """
                CREATE TABLE IF NOT EXISTS public.schema_version (
                    component TEXT PRIMARY KEY,
                    version INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """,
            ),
            (
                "project_info",
                """
                CREATE TABLE IF NOT EXISTS public.project_info (
                    project_code TEXT PRIMARY KEY,
                    project_name TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """,
            ),
            (
                "spec_types",
                """
                CREATE TABLE IF NOT EXISTS public.spec_types (
                    code TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """,
            ),
            (
                "spec_subtypes",
                """
                CREATE TABLE IF NOT EXISTS public.spec_subtypes (
                    code TEXT PRIMARY KEY,
                    spec_type_code TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    CONSTRAINT fk_spec_subtypes_type
                        FOREIGN KEY (spec_type_code)
                        REFERENCES public.spec_types(code)
                        ON DELETE RESTRICT
                )
                """,
            ),
            (
                "specifications",
                """
                CREATE TABLE IF NOT EXISTS public.specifications (
                    id BIGSERIAL PRIMARY KEY,
                    specification_key TEXT NOT NULL,
                    project_code TEXT NOT NULL,
                    region_scope TEXT,
                    spec_code TEXT,
                    spec_type TEXT,
                    spec_subtype TEXT,
                    canonical_name TEXT NOT NULL,
                    current_version_id BIGINT,
                    status TEXT NOT NULL DEFAULT 'ACTIVE',
                    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    CONSTRAINT uq_specifications_specification_key
                        UNIQUE (specification_key)
                )
                """,
            ),
            (
                "documents",
                """
                CREATE TABLE IF NOT EXISTS public.documents (
                    id BIGSERIAL PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    file_name TEXT NOT NULL,
                    file_type TEXT NOT NULL,
                    title TEXT,
                    module TEXT,
                    document_type TEXT,
                    version TEXT,
                    company TEXT,
                    category TEXT,
                    project_code TEXT,
                    project_name TEXT,
                    project_assignment_source TEXT,
                    series TEXT,
                    region_scope TEXT,
                    spec_type TEXT,
                    spec_subtype TEXT,
                    source_file TEXT,
                    language JSONB,
                    source_hash TEXT,
                    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    CONSTRAINT uq_documents_document_id UNIQUE (document_id)
                )
                """,
            ),
            (
                "chapters",
                """
                CREATE TABLE IF NOT EXISTS public.chapters (
                    id BIGSERIAL PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    chapter_id TEXT NOT NULL,
                    title_jp TEXT,
                    title_en TEXT,
                    sort_order INTEGER,
                    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    CONSTRAINT uq_chapters_document_chapter
                        UNIQUE (document_id, chapter_id),
                    CONSTRAINT fk_chapters_document
                        FOREIGN KEY (document_id)
                        REFERENCES public.documents(document_id)
                        ON DELETE CASCADE
                )
                """,
            ),
            (
                "sections",
                """
                CREATE TABLE IF NOT EXISTS public.sections (
                    id BIGSERIAL PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    chapter_id TEXT NOT NULL,
                    section_id TEXT NOT NULL,
                    parent_section_id TEXT,
                    title_jp TEXT,
                    title_en TEXT,
                    level INTEGER NOT NULL DEFAULT 2,
                    sort_order INTEGER,
                    page_number INTEGER,
                    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    CONSTRAINT uq_sections_document_section
                        UNIQUE (document_id, section_id),
                    CONSTRAINT fk_sections_document
                        FOREIGN KEY (document_id)
                        REFERENCES public.documents(document_id)
                        ON DELETE CASCADE
                )
                """,
            ),
            (
                "contents",
                """
                CREATE TABLE IF NOT EXISTS public.contents (
                    id BIGSERIAL PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    chapter_id TEXT,
                    section_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    page_number INTEGER,
                    chunk_index INTEGER NOT NULL DEFAULT 0,
                    token_count INTEGER NOT NULL DEFAULT 0,
                    sort_order INTEGER,
                    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    content_hash TEXT,
                    embedding_status TEXT NOT NULL DEFAULT 'PENDING',
                    embedding_started_at TIMESTAMPTZ,
                    embedded_at TIMESTAMPTZ,
                    embedding_model TEXT,
                    embedding_version TEXT,
                    embedded_content_hash TEXT,
                    embedding_error TEXT,
                    embedding_retry_count INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    CONSTRAINT uq_contents_document_section_chunk
                        UNIQUE (document_id, section_id, chunk_index),
                    CONSTRAINT fk_contents_document
                        FOREIGN KEY (document_id)
                        REFERENCES public.documents(document_id)
                        ON DELETE CASCADE
                )
                """,
            ),
            (
                "embeddings",
                """
                CREATE TABLE IF NOT EXISTS public.embeddings (
                    id BIGSERIAL PRIMARY KEY,
                    content_id BIGINT NOT NULL,
                    model_name TEXT NOT NULL,
                    model_version TEXT NOT NULL DEFAULT 'dense-v1',
                    vector_dimension INTEGER NOT NULL DEFAULT 1024,
                    qdrant_collection TEXT NOT NULL DEFAULT 'document_chunks',
                    qdrant_point_id TEXT NOT NULL,
                    content_hash TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    CONSTRAINT uq_embeddings_content_model_version
                        UNIQUE (content_id, model_name, model_version),
                    CONSTRAINT fk_embeddings_content
                        FOREIGN KEY (content_id)
                        REFERENCES public.contents(id)
                        ON DELETE CASCADE
                )
                """,
            ),
            (
                "vector_delete_queue",
                """
                CREATE TABLE IF NOT EXISTS public.vector_delete_queue (
                    id BIGSERIAL PRIMARY KEY,
                    content_id BIGINT NOT NULL,
                    document_id TEXT,
                    reason TEXT NOT NULL DEFAULT 'STALE_CONTENT',
                    queued_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    processed_at TIMESTAMPTZ,
                    last_error TEXT,
                    CONSTRAINT uq_vector_delete_queue_content UNIQUE (content_id)
                )
                """,
            ),
            (
                "specification_versions",
                """
                CREATE TABLE IF NOT EXISTS public.specification_versions (
                    id BIGSERIAL PRIMARY KEY,
                    specification_id BIGINT NOT NULL,
                    version_seq INTEGER NOT NULL,
                    version_label TEXT NOT NULL,
                    version_sort_key TEXT,
                    branch_name TEXT NOT NULL DEFAULT 'MAIN',
                    previous_version_id BIGINT,
                    document_id TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'APPROVED',
                    version_source TEXT,
                    revision_reason TEXT,
                    change_summary TEXT,
                    effective_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    CONSTRAINT uq_spec_versions_label
                        UNIQUE (specification_id, branch_name, version_label),
                    CONSTRAINT uq_spec_versions_document
                        UNIQUE (document_id),
                    CONSTRAINT uq_spec_versions_sequence
                        UNIQUE (specification_id, branch_name, version_seq),
                    CONSTRAINT fk_spec_versions_specification
                        FOREIGN KEY (specification_id)
                        REFERENCES public.specifications(id)
                        ON DELETE RESTRICT,
                    CONSTRAINT fk_spec_versions_previous
                        FOREIGN KEY (previous_version_id)
                        REFERENCES public.specification_versions(id)
                        ON DELETE SET NULL,
                    CONSTRAINT fk_spec_versions_document
                        FOREIGN KEY (document_id)
                        REFERENCES public.documents(document_id)
                        ON DELETE RESTRICT
                )
                """,
            ),
            (
                "specification_change_sets",
                """
                CREATE TABLE IF NOT EXISTS public.specification_change_sets (
                    id BIGSERIAL PRIMARY KEY,
                    specification_id BIGINT NOT NULL,
                    from_version_id BIGINT NOT NULL,
                    to_version_id BIGINT NOT NULL,
                    branch_name TEXT NOT NULL DEFAULT 'MAIN',
                    algorithm_version TEXT NOT NULL,
                    is_adjacent BOOLEAN NOT NULL DEFAULT FALSE,
                    status TEXT NOT NULL DEFAULT 'READY',
                    added_count INTEGER NOT NULL DEFAULT 0,
                    modified_count INTEGER NOT NULL DEFAULT 0,
                    removed_count INTEGER NOT NULL DEFAULT 0,
                    moved_count INTEGER NOT NULL DEFAULT 0,
                    unchanged_count INTEGER NOT NULL DEFAULT 0,
                    summary_text TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    CONSTRAINT uq_spec_change_sets_pair_algorithm
                        UNIQUE (specification_id, from_version_id, to_version_id, algorithm_version),
                    CONSTRAINT fk_spec_change_sets_specification
                        FOREIGN KEY (specification_id) REFERENCES public.specifications(id) ON DELETE CASCADE,
                    CONSTRAINT fk_spec_change_sets_from_version
                        FOREIGN KEY (from_version_id) REFERENCES public.specification_versions(id) ON DELETE CASCADE,
                    CONSTRAINT fk_spec_change_sets_to_version
                        FOREIGN KEY (to_version_id) REFERENCES public.specification_versions(id) ON DELETE CASCADE
                )
                """,
            ),
            (
                "specification_changes",
                """
                CREATE TABLE IF NOT EXISTS public.specification_changes (
                    id BIGSERIAL PRIMARY KEY,
                    change_set_id BIGINT NOT NULL,
                    specification_id BIGINT NOT NULL,
                    from_version_id BIGINT NOT NULL,
                    to_version_id BIGINT NOT NULL,
                    change_type TEXT NOT NULL,
                    entity_type TEXT NOT NULL DEFAULT 'SECTION',
                    entity_key TEXT NOT NULL,
                    chapter_id TEXT,
                    chapter_title TEXT,
                    section_id TEXT,
                    section_title TEXT,
                    old_content_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
                    new_content_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
                    old_text TEXT,
                    new_text TEXT,
                    old_hash TEXT,
                    new_hash TEXT,
                    old_page_number INTEGER,
                    new_page_number INTEGER,
                    old_sort_order INTEGER,
                    new_sort_order INTEGER,
                    similarity_score DOUBLE PRECISION,
                    details JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    CONSTRAINT fk_spec_changes_change_set
                        FOREIGN KEY (change_set_id) REFERENCES public.specification_change_sets(id) ON DELETE CASCADE,
                    CONSTRAINT fk_spec_changes_specification
                        FOREIGN KEY (specification_id) REFERENCES public.specifications(id) ON DELETE CASCADE,
                    CONSTRAINT fk_spec_changes_from_version
                        FOREIGN KEY (from_version_id) REFERENCES public.specification_versions(id) ON DELETE CASCADE,
                    CONSTRAINT fk_spec_changes_to_version
                        FOREIGN KEY (to_version_id) REFERENCES public.specification_versions(id) ON DELETE CASCADE
                )
                """,
            ),
            (
                "relation_extraction_runs",
                """
                CREATE TABLE IF NOT EXISTS public.relation_extraction_runs (
                    id BIGSERIAL PRIMARY KEY,
                    specification_id BIGINT NOT NULL,
                    version_id BIGINT NOT NULL,
                    document_id TEXT NOT NULL,
                    extractor_version TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'READY',
                    is_current BOOLEAN NOT NULL DEFAULT FALSE,
                    entity_count INTEGER NOT NULL DEFAULT 0,
                    relation_count INTEGER NOT NULL DEFAULT 0,
                    unresolved_count INTEGER NOT NULL DEFAULT 0,
                    stats JSONB NOT NULL DEFAULT '{}'::jsonb,
                    error_message TEXT,
                    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    finished_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    CONSTRAINT fk_relation_runs_specification
                        FOREIGN KEY (specification_id) REFERENCES public.specifications(id) ON DELETE CASCADE,
                    CONSTRAINT fk_relation_runs_version
                        FOREIGN KEY (version_id) REFERENCES public.specification_versions(id) ON DELETE CASCADE,
                    CONSTRAINT fk_relation_runs_document
                        FOREIGN KEY (document_id) REFERENCES public.documents(document_id) ON DELETE CASCADE
                )
                """,
            ),
            (
                "engineering_entities",
                """
                CREATE TABLE IF NOT EXISTS public.engineering_entities (
                    id BIGSERIAL PRIMARY KEY,
                    extraction_run_id BIGINT NOT NULL,
                    specification_id BIGINT NOT NULL,
                    version_id BIGINT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_key TEXT NOT NULL,
                    canonical_name TEXT NOT NULL,
                    normalized_value TEXT,
                    extraction_method TEXT NOT NULL,
                    confidence NUMERIC(5,4) NOT NULL DEFAULT 1.0,
                    status TEXT NOT NULL DEFAULT 'CONFIRMED',
                    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    CONSTRAINT uq_engineering_entity_run_key
                        UNIQUE (extraction_run_id, entity_type, entity_key),
                    CONSTRAINT fk_engineering_entities_run
                        FOREIGN KEY (extraction_run_id) REFERENCES public.relation_extraction_runs(id) ON DELETE CASCADE,
                    CONSTRAINT fk_engineering_entities_specification
                        FOREIGN KEY (specification_id) REFERENCES public.specifications(id) ON DELETE CASCADE,
                    CONSTRAINT fk_engineering_entities_version
                        FOREIGN KEY (version_id) REFERENCES public.specification_versions(id) ON DELETE CASCADE
                )
                """,
            ),
            (
                "spec_relations",
                """
                CREATE TABLE IF NOT EXISTS public.spec_relations (
                    id BIGSERIAL PRIMARY KEY,
                    extraction_run_id BIGINT NOT NULL,
                    relation_key TEXT NOT NULL,
                    source_specification_id BIGINT NOT NULL,
                    source_version_id BIGINT NOT NULL,
                    source_entity_id BIGINT,
                    target_specification_id BIGINT,
                    target_entity_id BIGINT,
                    target_external_key TEXT,
                    relation_type TEXT NOT NULL,
                    extraction_method TEXT NOT NULL,
                    confidence NUMERIC(5,4) NOT NULL DEFAULT 1.0,
                    status TEXT NOT NULL DEFAULT 'CONFIRMED',
                    is_explicit BOOLEAN NOT NULL DEFAULT TRUE,
                    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    CONSTRAINT uq_spec_relation_run_key UNIQUE (extraction_run_id, relation_key),
                    CONSTRAINT ck_spec_relations_target CHECK (
                        target_specification_id IS NOT NULL OR
                        target_entity_id IS NOT NULL OR
                        target_external_key IS NOT NULL
                    ),
                    CONSTRAINT fk_spec_relations_run
                        FOREIGN KEY (extraction_run_id) REFERENCES public.relation_extraction_runs(id) ON DELETE CASCADE,
                    CONSTRAINT fk_spec_relations_source_spec
                        FOREIGN KEY (source_specification_id) REFERENCES public.specifications(id) ON DELETE CASCADE,
                    CONSTRAINT fk_spec_relations_source_version
                        FOREIGN KEY (source_version_id) REFERENCES public.specification_versions(id) ON DELETE CASCADE,
                    CONSTRAINT fk_spec_relations_source_entity
                        FOREIGN KEY (source_entity_id) REFERENCES public.engineering_entities(id) ON DELETE CASCADE,
                    CONSTRAINT fk_spec_relations_target_spec
                        FOREIGN KEY (target_specification_id) REFERENCES public.specifications(id) ON DELETE SET NULL,
                    CONSTRAINT fk_spec_relations_target_entity
                        FOREIGN KEY (target_entity_id) REFERENCES public.engineering_entities(id) ON DELETE CASCADE
                )
                """,
            ),
            (
                "relation_evidence",
                """
                CREATE TABLE IF NOT EXISTS public.relation_evidence (
                    id BIGSERIAL PRIMARY KEY,
                    relation_id BIGINT NOT NULL,
                    document_id TEXT NOT NULL,
                    content_id BIGINT,
                    page_number INTEGER,
                    chapter_id TEXT,
                    chapter_title TEXT,
                    section_id TEXT,
                    section_title TEXT,
                    sheet_name TEXT,
                    cell_range TEXT,
                    evidence_text TEXT NOT NULL,
                    start_offset INTEGER,
                    end_offset INTEGER,
                    evidence_hash TEXT NOT NULL,
                    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    CONSTRAINT uq_relation_evidence_hash UNIQUE (relation_id, evidence_hash),
                    CONSTRAINT fk_relation_evidence_relation
                        FOREIGN KEY (relation_id) REFERENCES public.spec_relations(id) ON DELETE CASCADE,
                    CONSTRAINT fk_relation_evidence_document
                        FOREIGN KEY (document_id) REFERENCES public.documents(document_id) ON DELETE CASCADE,
                    CONSTRAINT fk_relation_evidence_content
                        FOREIGN KEY (content_id) REFERENCES public.contents(id) ON DELETE SET NULL
                )
                """,
            ),
        )

        for name, statement in statements:
            existed = self._relation_exists(cursor, name, kinds=("r", "p"))
            cursor.execute(statement)
            if not existed:
                created.append(f"table:{name}")

    @staticmethod
    def _seed_spec_types(cursor: Any) -> None:
        taxonomy = SpecificationTaxonomyLoader.load()
        for spec_type in taxonomy.spec_types:
            cursor.execute(
                """
                INSERT INTO public.spec_types(
                    code, display_name, enabled, created_at, updated_at
                )
                VALUES (%s, %s, TRUE, NOW(), NOW())
                ON CONFLICT (code)
                DO UPDATE SET
                    display_name = EXCLUDED.display_name,
                    enabled = TRUE,
                    updated_at = NOW()
                """,
                (spec_type.code, spec_type.display_name),
            )

    @staticmethod
    def _seed_spec_subtypes(cursor: Any) -> None:
        taxonomy = SpecificationTaxonomyLoader.load()
        for spec_type in taxonomy.spec_types:
            for subtype in spec_type.subtypes:
                cursor.execute(
                    """
                    INSERT INTO public.spec_subtypes(
                        code, spec_type_code, display_name, enabled, created_at, updated_at
                    )
                    VALUES (%s, %s, %s, TRUE, NOW(), NOW())
                    ON CONFLICT (code)
                    DO UPDATE SET
                        spec_type_code = EXCLUDED.spec_type_code,
                        display_name = EXCLUDED.display_name,
                        enabled = TRUE,
                        updated_at = NOW()
                    """,
                    (subtype.code, spec_type.code, subtype.display_name),
                )

    def _upgrade_columns(self, cursor: Any, created: list[str]) -> None:
        upgrades: dict[str, Sequence[tuple[str, str]]] = {
            "specifications": (
                ("specification_key", "TEXT"),
                ("project_code", "TEXT"),
                ("region_scope", "TEXT"),
                ("spec_code", "TEXT"),
                ("spec_type", "TEXT"),
                ("spec_subtype", "TEXT"),
                ("canonical_name", "TEXT"),
                ("current_version_id", "BIGINT"),
                ("status", "TEXT NOT NULL DEFAULT 'ACTIVE'"),
                ("metadata", "JSONB NOT NULL DEFAULT '{}'::jsonb"),
                ("created_at", "TIMESTAMPTZ NOT NULL DEFAULT NOW()"),
                ("updated_at", "TIMESTAMPTZ NOT NULL DEFAULT NOW()"),
            ),
            "specification_versions": (
                ("specification_id", "BIGINT"),
                ("version_seq", "INTEGER"),
                ("version_label", "TEXT"),
                ("version_sort_key", "TEXT"),
                ("branch_name", "TEXT NOT NULL DEFAULT 'MAIN'"),
                ("previous_version_id", "BIGINT"),
                ("document_id", "TEXT"),
                ("content_hash", "TEXT"),
                ("status", "TEXT NOT NULL DEFAULT 'APPROVED'"),
                ("version_source", "TEXT"),
                ("revision_reason", "TEXT"),
                ("change_summary", "TEXT"),
                ("effective_at", "TIMESTAMPTZ"),
                ("created_at", "TIMESTAMPTZ NOT NULL DEFAULT NOW()"),
                ("updated_at", "TIMESTAMPTZ NOT NULL DEFAULT NOW()"),
            ),
            "specification_change_sets": (
                ("specification_id", "BIGINT"),
                ("from_version_id", "BIGINT"),
                ("to_version_id", "BIGINT"),
                ("branch_name", "TEXT NOT NULL DEFAULT 'MAIN'"),
                ("algorithm_version", "TEXT"),
                ("is_adjacent", "BOOLEAN NOT NULL DEFAULT FALSE"),
                ("status", "TEXT NOT NULL DEFAULT 'READY'"),
                ("added_count", "INTEGER NOT NULL DEFAULT 0"),
                ("modified_count", "INTEGER NOT NULL DEFAULT 0"),
                ("removed_count", "INTEGER NOT NULL DEFAULT 0"),
                ("moved_count", "INTEGER NOT NULL DEFAULT 0"),
                ("unchanged_count", "INTEGER NOT NULL DEFAULT 0"),
                ("summary_text", "TEXT"),
                ("created_at", "TIMESTAMPTZ NOT NULL DEFAULT NOW()"),
                ("updated_at", "TIMESTAMPTZ NOT NULL DEFAULT NOW()"),
            ),
            "specification_changes": (
                ("change_set_id", "BIGINT"),
                ("specification_id", "BIGINT"),
                ("from_version_id", "BIGINT"),
                ("to_version_id", "BIGINT"),
                ("change_type", "TEXT"),
                ("entity_type", "TEXT NOT NULL DEFAULT 'SECTION'"),
                ("entity_key", "TEXT"),
                ("chapter_id", "TEXT"),
                ("chapter_title", "TEXT"),
                ("section_id", "TEXT"),
                ("section_title", "TEXT"),
                ("old_content_ids", "JSONB NOT NULL DEFAULT '[]'::jsonb"),
                ("new_content_ids", "JSONB NOT NULL DEFAULT '[]'::jsonb"),
                ("old_text", "TEXT"),
                ("new_text", "TEXT"),
                ("old_hash", "TEXT"),
                ("new_hash", "TEXT"),
                ("old_page_number", "INTEGER"),
                ("new_page_number", "INTEGER"),
                ("old_sort_order", "INTEGER"),
                ("new_sort_order", "INTEGER"),
                ("similarity_score", "DOUBLE PRECISION"),
                ("details", "JSONB NOT NULL DEFAULT '{}'::jsonb"),
                ("created_at", "TIMESTAMPTZ NOT NULL DEFAULT NOW()"),
            ),
            "documents": (
                ("file_name", "TEXT"),
                ("file_type", "TEXT"),
                ("title", "TEXT"),
                ("module", "TEXT"),
                ("document_type", "TEXT"),
                ("version", "TEXT"),
                ("company", "TEXT"),
                ("category", "TEXT"),
                ("project_code", "TEXT"),
                ("project_name", "TEXT"),
                ("project_assignment_source", "TEXT"),
                ("series", "TEXT"),
                ("region_scope", "TEXT"),
                ("spec_type", "TEXT"),
                ("spec_subtype", "TEXT"),
                ("source_file", "TEXT"),
                ("language", "JSONB"),
                ("source_hash", "TEXT"),
                ("metadata", "JSONB NOT NULL DEFAULT '{}'::jsonb"),
                ("created_at", "TIMESTAMPTZ NOT NULL DEFAULT NOW()"),
                ("updated_at", "TIMESTAMPTZ NOT NULL DEFAULT NOW()"),
            ),
            "chapters": (
                ("metadata", "JSONB NOT NULL DEFAULT '{}'::jsonb"),
                ("created_at", "TIMESTAMPTZ NOT NULL DEFAULT NOW()"),
                ("updated_at", "TIMESTAMPTZ NOT NULL DEFAULT NOW()"),
            ),
            "sections": (
                ("parent_section_id", "TEXT"),
                ("page_number", "INTEGER"),
                ("metadata", "JSONB NOT NULL DEFAULT '{}'::jsonb"),
                ("created_at", "TIMESTAMPTZ NOT NULL DEFAULT NOW()"),
                ("updated_at", "TIMESTAMPTZ NOT NULL DEFAULT NOW()"),
            ),
            "contents": (
                ("chapter_id", "TEXT"),
                ("sort_order", "INTEGER"),
                ("metadata", "JSONB NOT NULL DEFAULT '{}'::jsonb"),
                ("content_hash", "TEXT"),
                ("embedding_status", "TEXT NOT NULL DEFAULT 'PENDING'"),
                ("embedding_started_at", "TIMESTAMPTZ"),
                ("embedded_at", "TIMESTAMPTZ"),
                ("embedding_model", "TEXT"),
                ("embedding_version", "TEXT"),
                ("embedded_content_hash", "TEXT"),
                ("embedding_error", "TEXT"),
                ("embedding_retry_count", "INTEGER NOT NULL DEFAULT 0"),
                ("created_at", "TIMESTAMPTZ NOT NULL DEFAULT NOW()"),
                ("updated_at", "TIMESTAMPTZ NOT NULL DEFAULT NOW()"),
            ),
            "embeddings": (
                ("content_id", "BIGINT"),
                ("model_name", "TEXT"),
                ("model_version", "TEXT NOT NULL DEFAULT 'dense-v1'"),
                ("vector_dimension", "INTEGER NOT NULL DEFAULT 1024"),
                ("qdrant_collection", "TEXT NOT NULL DEFAULT 'document_chunks'"),
                ("qdrant_point_id", "TEXT"),
                ("content_hash", "TEXT"),
                ("created_at", "TIMESTAMPTZ NOT NULL DEFAULT NOW()"),
                ("updated_at", "TIMESTAMPTZ NOT NULL DEFAULT NOW()"),
            ),
        }

        for table_name, definitions in upgrades.items():
            if not self._relation_exists(cursor, table_name, kinds=("r", "p")):
                continue
            existing = self._table_columns(cursor, table_name)
            for column_name, definition in definitions:
                if column_name in existing:
                    continue
                cursor.execute(
                    sql.SQL("ALTER TABLE public.{} ADD COLUMN {} {}").format(
                        sql.Identifier(table_name),
                        sql.Identifier(column_name),
                        sql.SQL(definition),
                    )
                )
                created.append(f"column:{table_name}.{column_name}")
                existing.add(column_name)

    @staticmethod
    def _backfill_compatibility_columns(cursor: Any) -> None:
        cursor.execute(
            """
            UPDATE public.documents
            SET file_name = COALESCE(NULLIF(file_name, ''), title, document_id),
                file_type = COALESCE(NULLIF(file_type, ''), module, document_type, 'unknown')
            WHERE file_name IS NULL
               OR file_name = ''
               OR file_type IS NULL
               OR file_type = ''
            """
        )

    @staticmethod
    def _backfill_legacy_supporting_document_roles(cursor: Any, created: list[str]) -> None:
        """Normalize legacy CoverSheet/etc. metadata without deleting business data.

        Older releases could persist a cover/check/test sheet before the supporting
        document policy existed.  v1.01 marks those physical files as metadata
        support so every later RAG/Qdrant/version/relation path applies the same
        arbitration.  Immutable version rows are intentionally retained for audit.
        """
        pattern = PrimarySpecificationDocumentPolicy.SQL_SUPPORTING_FILENAME_PATTERN
        cursor.execute(
            """
            UPDATE public.documents
            SET metadata = COALESCE(metadata, '{}'::jsonb) || jsonb_build_object(
                    'document_role', 'SUPPORTING',
                    'evidence_role', 'METADATA_SUPPORT',
                    'qa_index_policy', 'EXCLUDE',
                    'supporting_document_type',
                        CASE
                            WHEN COALESCE(file_name, '') ~* '(cover[ _-]*sheet|coversheet|表紙|封面)' THEN 'COVER_SHEET'
                            WHEN COALESCE(file_name, '') ~* '(check[ _-]*(sheet|list)|checksheet|checklist|チェック)' THEN 'CHECK_SHEET'
                            WHEN COALESCE(file_name, '') ~* '(test[ _-]*sheet|testsheet|テスト[ _-]*シート|試験[ _-]*シート)' THEN 'TEST_SHEET'
                            WHEN COALESCE(file_name, '') ~* '(review[ _-]*sheet|reviewsheet|レビュー)' THEN 'REVIEW_SHEET'
                            WHEN COALESCE(file_name, '') ~* '(reference[ _-]*list|referencelist|参照[ _-]*一覧|参考[ _-]*一覧)' THEN 'REFERENCE_LIST'
                            WHEN COALESCE(file_name, '') ~* '(change[ _-]*request|changerequest|変更[ _-]*依頼)' THEN 'CHANGE_REQUEST'
                            WHEN COALESCE(file_name, '') ~* '(summary[ _-]*sheet|summarysheet|サマリ)' THEN 'SUMMARY_SHEET'
                            WHEN COALESCE(file_name, '') ~* '(添付[ _-]*資料|添付資料|添付書類|attachment[ _-]*material)' THEN 'ATTACHMENT'
                            ELSE COALESCE(NULLIF(metadata ->> 'supporting_document_type', ''), 'SUPPORTING_DOCUMENT')
                        END,
                    'specification_classifier_status', 'SUPPORTING_DOCUMENT',
                    'specification_version_status', 'NOT_APPLICABLE',
                    'primary_document_policy_version', 'v1.01'
                ),
                updated_at = NOW()
            WHERE COALESCE(file_name, '') ~* %s
              AND (
                    UPPER(COALESCE(metadata ->> 'document_role', '')) <> 'SUPPORTING'
                 OR UPPER(COALESCE(metadata ->> 'qa_index_policy', '')) <> 'EXCLUDE'
                 OR COALESCE(metadata ->> 'supporting_document_type', '') = ''
              )
            """,
            (pattern,),
        )
        changed = int(cursor.rowcount or 0)
        if changed:
            created.append(f"backfill:legacy_supporting_document_roles:{changed}")

    @staticmethod
    def _repair_primary_specification_current_pointers(
        cursor: Any, created: list[str]
    ) -> None:
        """Heal legacy masters whose Current pointer references a supporting artefact.

        Immutable version rows stay untouched.  Only the mutable master pointer is
        repaired, and only when its existing target is not a PRIMARY body.  If no
        body exists, Current becomes NULL so a Cover Sheet cannot masquerade as a
        business version.
        """
        primary_current = PrimarySpecificationDocumentPolicy.sql_primary_predicate("current_doc")
        primary_candidate = PrimarySpecificationDocumentPolicy.sql_primary_predicate("candidate_doc")
        cursor.execute(
            f"""
            WITH resolved AS (
                SELECT
                    sp.id AS specification_id,
                    sp.current_version_id,
                    EXISTS (
                        SELECT 1
                        FROM public.specification_versions current_sv
                        LEFT JOIN public.documents current_doc
                          ON current_doc.document_id = current_sv.document_id
                        WHERE current_sv.id = sp.current_version_id
                          AND {primary_current}
                    ) AS current_is_primary,
                    (
                        SELECT candidate_sv.id
                        FROM public.specification_versions candidate_sv
                        LEFT JOIN public.documents candidate_doc
                          ON candidate_doc.document_id = candidate_sv.document_id
                        WHERE candidate_sv.specification_id = sp.id
                          AND candidate_sv.branch_name = 'MAIN'
                          AND {primary_candidate}
                        ORDER BY
                            candidate_sv.version_sort_key DESC NULLS LAST,
                            candidate_sv.version_seq DESC,
                            candidate_sv.id DESC
                        LIMIT 1
                    ) AS preferred_primary_version_id
                FROM public.specifications sp
            )
            UPDATE public.specifications sp
            SET current_version_id = resolved.preferred_primary_version_id,
                updated_at = NOW()
            FROM resolved
            WHERE sp.id = resolved.specification_id
              AND resolved.current_is_primary IS FALSE
              AND sp.current_version_id IS DISTINCT FROM resolved.preferred_primary_version_id
            """
        )
        changed = int(cursor.rowcount or 0)
        if changed:
            created.append(f"repair:primary_current_version:{changed}")

    @staticmethod
    def _backfill_content_chapter(cursor: Any) -> None:
        cursor.execute(
            """
            UPDATE public.contents c
            SET chapter_id = s.chapter_id
            FROM public.sections s
            WHERE c.chapter_id IS NULL
              AND s.document_id = c.document_id
              AND s.section_id = c.section_id
            """
        )

    def _backfill_specification_version_history(
        self,
        cursor: Any,
        *,
        created: list[str],
        warnings: list[str],
    ) -> None:
        """Attach legacy 21MM/24MM documents to Master/Version non-destructively.

        Existing document/chunk rows are never rewritten.  The migration only
        creates missing Master/Version records and points Current to the newest
        comparable MAIN version.  Common documents are intentionally skipped.
        """

        cursor.execute(
            """
            SELECT
                d.document_id,
                d.file_name,
                d.file_type,
                d.project_code,
                d.region_scope,
                d.spec_type,
                d.spec_subtype,
                d.version,
                d.metadata,
                d.created_at
            FROM public.documents d
            WHERE COALESCE(d.project_code, '') <> ''
              AND UPPER(d.project_code) <> 'COMMON'
              AND NOT EXISTS (
                    SELECT 1
                    FROM public.specification_versions sv
                    WHERE sv.document_id = d.document_id
              )
            ORDER BY d.created_at ASC, d.id ASC
            """
        )
        rows = list(cursor.fetchall())
        if not rows:
            return

        migrated = 0
        for (
            document_id,
            file_name,
            file_type,
            project_code,
            region_scope,
            spec_type,
            spec_subtype,
            legacy_version,
            metadata_raw,
            created_at,
        ) in rows:
            try:
                project = str(project_code).strip().upper()
                resolver = SpecificationVersionResolver(project_code=project)
                metadata = dict(metadata_raw or {})
                if not PrimarySpecificationDocumentPolicy.is_primary(
                    file_name=str(file_name or document_id), metadata=metadata
                ):
                    # Supporting artefacts remain in public.documents for audit but
                    # never become immutable business versions in v1.01.
                    continue
                metadata.update(
                    {
                        "project_code": project,
                        "region_scope": region_scope,
                        "spec_type": spec_type,
                        "spec_subtype": spec_subtype,
                    }
                )
                if legacy_version and not metadata.get("version_override"):
                    metadata["version_override"] = legacy_version

                dummy = Document(
                    file_name=str(file_name or document_id),
                    file_type=str(file_type or "unknown"),
                    metadata=metadata,
                )
                resolver.process(dummy)
                resolved = dummy.metadata

                specification_key = str(
                    resolved.get("specification_key") or ""
                ).strip()
                if not specification_key:
                    continue

                cursor.execute(
                    """
                    SELECT content
                    FROM public.contents
                    WHERE document_id = %s
                    ORDER BY id ASC
                    """,
                    (document_id,),
                )
                payload = "\n".join(str(row[0] or "") for row in cursor.fetchall())
                import hashlib

                content_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
                version_label = str(
                    resolved.get("specification_version") or ""
                ).strip()
                version_status = "MIGRATED"
                version_source = str(
                    resolved.get("specification_version_source")
                    or "schema_migration"
                )
                if not version_label:
                    version_label = f"LEGACY-{content_hash[:12]}"
                    version_status = "UNRESOLVED"
                    version_source = "schema_migration_content_hash"

                cursor.execute(
                    """
                    INSERT INTO public.specifications (
                        specification_key, project_code, region_scope, spec_code,
                        spec_type, spec_subtype, canonical_name, status, metadata,
                        created_at, updated_at
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s, %s, 'ACTIVE',
                        CAST(%s AS jsonb), NOW(), NOW()
                    )
                    ON CONFLICT (specification_key)
                    DO UPDATE SET
                        region_scope = COALESCE(EXCLUDED.region_scope, public.specifications.region_scope),
                        spec_code = COALESCE(EXCLUDED.spec_code, public.specifications.spec_code),
                        spec_type = COALESCE(EXCLUDED.spec_type, public.specifications.spec_type),
                        spec_subtype = COALESCE(EXCLUDED.spec_subtype, public.specifications.spec_subtype),
                        updated_at = NOW()
                    RETURNING id, current_version_id
                    """,
                    (
                        specification_key,
                        project,
                        resolved.get("region_scope"),
                        resolved.get("specification_code"),
                        resolved.get("spec_type"),
                        resolved.get("spec_subtype"),
                        resolved.get("specification_name") or str(file_name),
                        __import__("json").dumps(
                            resolved, ensure_ascii=False, default=str
                        ),
                    ),
                )
                specification_id, current_version_id = cursor.fetchone()

                cursor.execute(
                    """
                    SELECT id, content_hash, document_id
                    FROM public.specification_versions
                    WHERE specification_id = %s
                      AND branch_name = 'MAIN'
                      AND version_label = %s
                    LIMIT 1
                    """,
                    (specification_id, version_label),
                )
                collision = cursor.fetchone()
                if collision:
                    if str(collision[1] or "") == content_hash:
                        continue
                    version_label = f"{version_label}-MIGRATED-{content_hash[:8]}"

                cursor.execute(
                    """
                    SELECT COALESCE(MAX(version_seq), 0) + 1
                    FROM public.specification_versions
                    WHERE specification_id = %s AND branch_name = 'MAIN'
                    """,
                    (specification_id,),
                )
                version_seq = int(cursor.fetchone()[0])

                cursor.execute(
                    """
                    INSERT INTO public.specification_versions (
                        specification_id, version_seq, version_label, branch_name,
                        previous_version_id, document_id, content_hash, status,
                        version_source, created_at, updated_at
                    )
                    VALUES (
                        %s, %s, %s, 'MAIN', %s, %s, %s, %s, %s,
                        COALESCE(%s, NOW()), NOW()
                    )
                    RETURNING id
                    """,
                    (
                        specification_id,
                        version_seq,
                        version_label,
                        current_version_id,
                        document_id,
                        content_hash,
                        version_status,
                        version_source,
                        created_at,
                    ),
                )
                new_version_id = int(cursor.fetchone()[0])

                promote = current_version_id is None
                if current_version_id is not None and version_status != "UNRESOLVED":
                    cursor.execute(
                        "SELECT version_label, status FROM public.specification_versions WHERE id = %s",
                        (current_version_id,),
                    )
                    current = cursor.fetchone()
                    if not current or str(current[1]) == "UNRESOLVED":
                        promote = True
                    else:
                        comparison = SpecificationVersionManager.compare_version_labels(
                            version_label, str(current[0])
                        )
                        promote = comparison is None or comparison > 0

                if promote:
                    cursor.execute(
                        """
                        UPDATE public.specifications
                        SET current_version_id = %s, updated_at = NOW()
                        WHERE id = %s
                        """,
                        (new_version_id, specification_id),
                    )
                migrated += 1
            except Exception as exc:
                warnings.append(
                    f"Legacy specification version backfill skipped for {document_id}: {exc}"
                )

        if migrated:
            created.append(f"backfill:specification_versions:{migrated}")

    @staticmethod
    def _backfill_version_sort_and_change_history(
        cursor: Any,
        *,
        created: list[str],
        warnings: list[str],
    ) -> None:
        """Normalize business-version order and build adjacent change history.

        This migration is intentionally non-destructive. Existing immutable
        document/version rows are retained; only derived ordering pointers,
        version_sort_key and deterministic diff cache rows are updated.
        """

        if not SchemaManager._relation_exists(cursor, "specification_versions", kinds=("r", "p")):
            return
        if not SchemaManager._relation_exists(cursor, "specification_change_sets", kinds=("r", "p")):
            return
        if not SchemaManager._relation_exists(cursor, "specification_changes", kinds=("r", "p")):
            return

        cursor.execute(
            """
            SELECT DISTINCT specification_id
            FROM public.specification_versions
            WHERE branch_name = 'MAIN'
            ORDER BY specification_id
            """
        )
        specification_ids = [int(row[0]) for row in cursor.fetchall()]
        if not specification_ids:
            return

        change_manager = SpecificationChangeManager()
        rebuilt_pairs = 0
        primary_history_doc = PrimarySpecificationDocumentPolicy.sql_primary_predicate("history_doc")

        for specification_id in specification_ids:
            cursor.execute(
                f"""
                SELECT sv.id, sv.version_label, sv.version_seq
                FROM public.specification_versions sv
                LEFT JOIN public.documents history_doc
                  ON history_doc.document_id = sv.document_id
                WHERE sv.specification_id = %s
                  AND sv.branch_name = 'MAIN'
                  AND {primary_history_doc}
                ORDER BY sv.version_seq, sv.id
                """,
                (specification_id,),
            )
            rows = [
                {
                    "id": int(row[0]),
                    "label": str(row[1]),
                    "seq": int(row[2]),
                }
                for row in cursor.fetchall()
            ]
            if not rows:
                continue

            for row in rows:
                sort_key = SpecificationVersionManager.version_sort_key(row["label"])
                cursor.execute(
                    """
                    UPDATE public.specification_versions
                    SET version_sort_key = %s
                    WHERE id = %s
                      AND version_sort_key IS DISTINCT FROM %s
                    """,
                    (sort_key, row["id"], sort_key),
                )

            def compare_rows(left: dict[str, Any], right: dict[str, Any]) -> int:
                comparison = SpecificationVersionManager.compare_version_labels(
                    left["label"], right["label"]
                )
                if comparison is not None:
                    return comparison
                return (left["seq"] > right["seq"]) - (left["seq"] < right["seq"])

            ordered = sorted(rows, key=cmp_to_key(compare_rows))

            cursor.execute(
                """
                UPDATE public.specification_change_sets
                SET is_adjacent = FALSE, updated_at = NOW()
                WHERE specification_id = %s
                """,
                (specification_id,),
            )

            previous_id: int | None = None
            for row in ordered:
                cursor.execute(
                    """
                    UPDATE public.specification_versions
                    SET previous_version_id = %s, updated_at = NOW()
                    WHERE id = %s
                      AND previous_version_id IS DISTINCT FROM %s
                    """,
                    (previous_id, row["id"], previous_id),
                )

                if previous_id is None:
                    cursor.execute(
                        """
                        UPDATE public.specification_versions
                        SET change_summary = COALESCE(NULLIF(change_summary, ''), 'Initial version')
                        WHERE id = %s
                        """,
                        (row["id"],),
                    )
                else:
                    cursor.execute("SAVEPOINT specification_diff_backfill")
                    try:
                        change_manager.ensure_comparison(
                            cur=cursor,
                            specification_id=specification_id,
                            from_version_id=previous_id,
                            to_version_id=row["id"],
                            is_adjacent=True,
                            force=True,
                        )
                        cursor.execute("RELEASE SAVEPOINT specification_diff_backfill")
                        rebuilt_pairs += 1
                    except Exception as exc:
                        cursor.execute("ROLLBACK TO SAVEPOINT specification_diff_backfill")
                        cursor.execute("RELEASE SAVEPOINT specification_diff_backfill")
                        warnings.append(
                            "Specification change backfill skipped for "
                            f"specification_id={specification_id}, "
                            f"to_version_id={row['id']}: {exc}"
                        )
                previous_id = row["id"]

        if rebuilt_pairs:
            created.append(f"backfill:specification_change_pairs:{rebuilt_pairs}")

    @staticmethod
    def _ensure_version_constraints(
        cursor: Any,
        created: list[str],
        warnings: list[str],
    ) -> None:
        """Add circular/current-version constraints after both tables exist."""

        cursor.execute(
            """
            SELECT 1
            FROM pg_constraint
            WHERE conname = 'fk_specifications_current_version'
              AND conrelid = 'public.specifications'::regclass
            """
        )
        if cursor.fetchone() is None:
            try:
                cursor.execute(
                    """
                    ALTER TABLE public.specifications
                    ADD CONSTRAINT fk_specifications_current_version
                    FOREIGN KEY (current_version_id)
                    REFERENCES public.specification_versions(id)
                    ON DELETE SET NULL
                    DEFERRABLE INITIALLY DEFERRED
                    """
                )
                created.append("constraint:fk_specifications_current_version")
            except Exception as exc:
                warnings.append(
                    "Could not add current-version FK automatically: " + str(exc)
                )
                raise

    def _ensure_constraints_and_indexes(
        self,
        cursor: Any,
        *,
        created: list[str],
        reused: list[str],
        warnings: list[str],
    ) -> None:
        desired = (
            (
                "uq_specifications_specification_key",
                "specifications",
                ("specification_key",),
                True,
            ),
            (
                "idx_specifications_project_region_type",
                "specifications",
                ("project_code", "region_scope", "spec_type"),
                False,
            ),
            (
                "idx_specifications_current_version",
                "specifications",
                ("current_version_id",),
                False,
            ),
            (
                "uq_spec_versions_label",
                "specification_versions",
                ("specification_id", "branch_name", "version_label"),
                True,
            ),
            (
                "uq_spec_versions_sequence",
                "specification_versions",
                ("specification_id", "branch_name", "version_seq"),
                True,
            ),
            (
                "uq_spec_versions_document",
                "specification_versions",
                ("document_id",),
                True,
            ),
            (
                "idx_spec_versions_spec_sequence",
                "specification_versions",
                ("specification_id", "version_seq"),
                False,
            ),
            (
                "idx_spec_versions_branch_status",
                "specification_versions",
                ("branch_name", "status"),
                False,
            ),
            (
                "idx_spec_versions_business_order",
                "specification_versions",
                ("specification_id", "branch_name", "version_sort_key"),
                False,
            ),
            (
                "uq_spec_change_sets_pair_algorithm",
                "specification_change_sets",
                ("specification_id", "from_version_id", "to_version_id", "algorithm_version"),
                True,
            ),
            (
                "idx_spec_change_sets_adjacent",
                "specification_change_sets",
                ("specification_id", "is_adjacent", "to_version_id"),
                False,
            ),
            (
                "idx_spec_changes_set_type",
                "specification_changes",
                ("change_set_id", "change_type"),
                False,
            ),
            (
                "idx_spec_changes_spec_versions",
                "specification_changes",
                ("specification_id", "from_version_id", "to_version_id"),
                False,
            ),
            (
                "idx_relation_runs_version_current",
                "relation_extraction_runs",
                ("version_id", "is_current", "status"),
                False,
            ),
            (
                "idx_relation_runs_specification",
                "relation_extraction_runs",
                ("specification_id", "version_id"),
                False,
            ),
            (
                "uq_engineering_entity_run_key",
                "engineering_entities",
                ("extraction_run_id", "entity_type", "entity_key"),
                True,
            ),
            (
                "idx_engineering_entities_type_name",
                "engineering_entities",
                ("entity_type", "canonical_name"),
                False,
            ),
            (
                "uq_spec_relation_run_key",
                "spec_relations",
                ("extraction_run_id", "relation_key"),
                True,
            ),
            (
                "idx_spec_relations_source_type",
                "spec_relations",
                ("source_specification_id", "source_version_id", "relation_type"),
                False,
            ),
            (
                "idx_spec_relations_target_spec",
                "spec_relations",
                ("target_specification_id", "relation_type"),
                False,
            ),
            (
                "idx_spec_relations_status",
                "spec_relations",
                ("status", "relation_type"),
                False,
            ),
            (
                "uq_relation_evidence_hash",
                "relation_evidence",
                ("relation_id", "evidence_hash"),
                True,
            ),
            (
                "idx_relation_evidence_content",
                "relation_evidence",
                ("content_id", "relation_id"),
                False,
            ),
            ("uq_documents_document_id", "documents", ("document_id",), True),
            (
                "idx_documents_project_spec_type",
                "documents",
                ("project_code", "spec_type"),
                False,
            ),
            (
                "idx_documents_project_region_spec_type",
                "documents",
                ("project_code", "region_scope", "spec_type"),
                False,
            ),
            (
                "idx_documents_project_spec_subtype",
                "documents",
                ("project_code", "spec_type", "spec_subtype"),
                False,
            ),
            (
                "idx_documents_series_spec_type",
                "documents",
                ("series", "spec_type"),
                False,
            ),
            (
                "uq_chapters_document_chapter",
                "chapters",
                ("document_id", "chapter_id"),
                True,
            ),
            (
                "uq_sections_document_section",
                "sections",
                ("document_id", "section_id"),
                True,
            ),
            (
                "uq_contents_document_section_chunk",
                "contents",
                ("document_id", "section_id", "chunk_index"),
                True,
            ),
            (
                "idx_sections_document_chapter",
                "sections",
                ("document_id", "chapter_id"),
                False,
            ),
            (
                "idx_sections_parent",
                "sections",
                ("document_id", "parent_section_id"),
                False,
            ),
            (
                "idx_contents_document_order",
                "contents",
                ("document_id", "id"),
                False,
            ),
            (
                "idx_contents_embedding_queue",
                "contents",
                ("embedding_status", "id"),
                False,
            ),
            (
                "idx_contents_embedding_stale",
                "contents",
                ("embedding_status", "embedding_started_at"),
                False,
            ),
            (
                "uq_embeddings_content_model_version",
                "embeddings",
                ("content_id", "model_name", "model_version"),
                True,
            ),
            (
                "idx_embeddings_content",
                "embeddings",
                ("content_id",),
                False,
            ),
            (
                "uq_vector_delete_queue_content",
                "vector_delete_queue",
                ("content_id",),
                True,
            ),
            (
                "idx_vector_delete_queue_pending",
                "vector_delete_queue",
                ("processed_at", "id"),
                False,
            ),
        )

        for name, table, columns, unique in desired:
            if not self._all_columns_exist(cursor, table, columns):
                warnings.append(
                    f"Skipped index {name}: missing columns on {table}."
                )
                continue
            equivalent = self._find_equivalent_index(
                cursor,
                table_name=table,
                columns=columns,
                unique=unique,
            )
            if equivalent:
                reused.append(f"{name} -> {equivalent}")
                continue

            unique_sql = sql.SQL("UNIQUE ") if unique else sql.SQL("")
            cursor.execute(
                sql.SQL("CREATE {}INDEX {} ON public.{} USING btree ({})").format(
                    unique_sql,
                    sql.Identifier(name),
                    sql.Identifier(table),
                    sql.SQL(", ").join(sql.Identifier(col) for col in columns),
                )
            )
            created.append(f"index:{name}")

        # Authoritative relation state has exactly one current extraction run
        # per Specification, and that run should prefer its current business
        # version. Repair legacy databases before adding the guard index.
        if self._all_columns_exist(
            cursor, "relation_extraction_runs", ("specification_id", "version_id", "is_current")
        ):
            cursor.execute(
                """
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
                  AND ranked.rn > 1
                """
            )
            cursor.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_relation_run_current_specification
                ON public.relation_extraction_runs(specification_id)
                WHERE is_current IS TRUE
                """
            )
            # Keep the older per-version guard for compatibility; the
            # Specification-level guard is intentionally stricter.
            cursor.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_relation_run_current_version
                ON public.relation_extraction_runs(version_id)
                WHERE is_current IS TRUE
                """
            )
            cursor.execute(
                """
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
                    applied_at = EXCLUDED.applied_at
                """
            )

    @staticmethod
    def _create_rag_views(cursor: Any, created: list[str]) -> None:
        """Create all read models, including Current-only RAG access.

        v1.01 treats a Specification as a logical entity with one authoritative
        body and optional supporting artefacts.  Legacy cover/check/test sheets
        may still exist in specification_versions; every read model therefore
        applies the same primary-document policy instead of trusting a polluted
        current_version_id blindly.
        """

        primary_d = PrimarySpecificationDocumentPolicy.sql_primary_predicate("d")
        primary_cvd = PrimarySpecificationDocumentPolicy.sql_primary_predicate("cvd")
        primary_hd = PrimarySpecificationDocumentPolicy.sql_primary_predicate("hd")
        primary_svd = PrimarySpecificationDocumentPolicy.sql_primary_predicate("svd")
        primary_fvd = PrimarySpecificationDocumentPolicy.sql_primary_predicate("fvd")
        primary_tvd = PrimarySpecificationDocumentPolicy.sql_primary_predicate("tvd")
        primary_pd = PrimarySpecificationDocumentPolicy.sql_primary_predicate("pd")

        cursor.execute("SELECT to_regclass('public.rag_chunks')")
        rag_chunks_existed = cursor.fetchone()[0] is not None

        cursor.execute(
            f"""
            CREATE OR REPLACE VIEW public.rag_chunks AS
            SELECT
                c.id AS content_id,
                c.document_id,
                COALESCE(NULLIF(d.file_name, ''), d.title, d.document_id) AS file_name,
                COALESCE(NULLIF(d.file_type, ''), d.module, d.document_type) AS file_type,
                d.project_code,
                d.project_name,
                d.project_assignment_source,
                d.series,
                d.spec_type,
                COALESCE(c.chapter_id, s.chapter_id) AS chapter_id,
                ch.title_jp AS chapter_title_jp,
                ch.title_en AS chapter_title_en,
                COALESCE(NULLIF(ch.title_jp, ''), ch.title_en) AS chapter_title,
                c.section_id,
                s.parent_section_id,
                s.title_jp AS section_title_jp,
                s.title_en AS section_title_en,
                COALESCE(NULLIF(s.title_jp, ''), s.title_en) AS section_title,
                s.level AS section_level,
                c.page_number,
                c.chunk_index,
                c.sort_order,
                c.token_count,
                c.content,
                c.content_hash,
                c.embedding_status,
                c.embedding_started_at,
                c.embedded_at,
                c.embedding_model,
                c.embedding_version,
                c.embedded_content_hash,
                c.embedding_retry_count,
                c.embedding_error,
                c.created_at,
                c.updated_at,
                -- Existing output order above is intentionally frozen.  New
                -- columns must only be appended for CREATE OR REPLACE VIEW.
                d.spec_subtype,
                d.region_scope,
                sp.id AS specification_id,
                sp.specification_key,
                sp.spec_code AS specification_code,
                sp.canonical_name AS specification_name,
                sv.id AS specification_version_id,
                sv.version_label AS specification_version,
                sv.version_seq AS specification_version_seq,
                sv.branch_name AS specification_branch,
                sv.status AS specification_version_status,
                (
                    sv.id = COALESCE(
                        (
                            SELECT psv.id
                            FROM public.specification_versions psv
                            LEFT JOIN public.documents pd ON pd.document_id = psv.document_id
                            WHERE psv.specification_id = sp.id
                              AND psv.branch_name = 'MAIN'
                              AND {primary_pd}
                            ORDER BY
                                CASE WHEN psv.id = sp.current_version_id THEN 0 ELSE 1 END,
                                psv.version_sort_key DESC NULLS LAST, psv.version_seq DESC, psv.id DESC
                            LIMIT 1
                        ),
                        sp.current_version_id
                    )
                ) AS is_current_version,
                COALESCE(d.metadata ->> 'document_role', 'PRIMARY') AS document_role,
                COALESCE(d.metadata ->> 'supporting_document_type', '') AS supporting_document_type,
                COALESCE(d.metadata ->> 'qa_index_policy', 'INCLUDE') AS qa_index_policy
            FROM public.contents c
            LEFT JOIN public.documents d
              ON d.document_id = c.document_id
            LEFT JOIN public.sections s
              ON s.document_id = c.document_id
             AND s.section_id = c.section_id
            LEFT JOIN public.chapters ch
              ON ch.document_id = c.document_id
             AND ch.chapter_id = COALESCE(c.chapter_id, s.chapter_id)
            LEFT JOIN public.specification_versions sv
              ON sv.document_id = d.document_id
            LEFT JOIN public.specifications sp
              ON sp.id = sv.specification_id
            """
        )
        if not rag_chunks_existed:
            created.append("view:rag_chunks")

        cursor.execute("SELECT to_regclass('public.rag_current_chunks')")
        current_existed = cursor.fetchone()[0] is not None
        cursor.execute(
            """
            CREATE OR REPLACE VIEW public.rag_current_chunks AS
            SELECT rc.*
            FROM public.rag_chunks rc
            WHERE (
                    rc.project_code = 'COMMON'
                 OR rc.specification_version_id IS NULL
                 OR rc.is_current_version IS TRUE
            )
              AND UPPER(COALESCE(rc.document_role, 'PRIMARY')) NOT IN
                  ('ISSUE_REGISTER', 'METADATA_ONLY', 'SUPPORTING', 'TRANSLATION')
              AND UPPER(COALESCE(rc.qa_index_policy, 'INCLUDE')) <> 'EXCLUDE'
              AND COALESCE(rc.supporting_document_type, '') = ''
            """
        )
        if not current_existed:
            created.append("view:rag_current_chunks")

        cursor.execute("SELECT to_regclass('public.specification_catalog')")
        catalog_existed = cursor.fetchone()[0] is not None
        cursor.execute(
            f"""
            CREATE OR REPLACE VIEW public.specification_catalog AS
            SELECT
                sp.id AS specification_id,
                sp.specification_key,
                sp.project_code,
                sp.region_scope,
                sp.spec_code,
                sp.spec_type,
                sp.spec_subtype,
                COALESCE(
                    NULLIF(cvd.metadata ->> 'specification_name', ''),
                    NULLIF(cvd.title, ''),
                    sp.canonical_name
                ) AS canonical_name,
                sp.status AS specification_status,
                cv.id AS current_version_id,
                cv.version_label AS current_version,
                cv.version_seq AS current_version_seq,
                cv.status AS current_version_status,
                cv.document_id AS current_document_id,
                COALESCE(hist.version_count, 0) AS version_count,
                GREATEST(COALESCE(hist.version_count, 0) - 1, 0) AS history_count,
                sp.created_at,
                sp.updated_at
            FROM public.specifications sp
            JOIN LATERAL (
                SELECT sv.*
                FROM public.specification_versions sv
                LEFT JOIN public.documents cvd
                  ON cvd.document_id = sv.document_id
                WHERE sv.specification_id = sp.id
                  AND sv.branch_name = 'MAIN'
                  AND {primary_cvd}
                ORDER BY
                    CASE WHEN sv.id = sp.current_version_id THEN 0 ELSE 1 END,
                    sv.version_sort_key DESC NULLS LAST,
                    sv.version_seq DESC,
                    sv.id DESC
                LIMIT 1
            ) cv ON TRUE
            LEFT JOIN public.documents cvd
              ON cvd.document_id = cv.document_id
            LEFT JOIN LATERAL (
                SELECT COUNT(*)::INTEGER AS version_count
                FROM public.specification_versions hsv
                LEFT JOIN public.documents hd
                  ON hd.document_id = hsv.document_id
                WHERE hsv.specification_id = sp.id
                  AND hsv.branch_name = 'MAIN'
                  AND {primary_hd}
            ) hist ON TRUE
            """
        )
        if not catalog_existed:
            created.append("view:specification_catalog")

        cursor.execute("SELECT to_regclass('public.specification_version_history')")
        history_existed = cursor.fetchone()[0] is not None
        cursor.execute(
            f"""
            CREATE OR REPLACE VIEW public.specification_version_history AS
            WITH primary_versions AS (
                SELECT
                    sv.*,
                    LEAD(sv.id) OVER (
                        PARTITION BY sv.specification_id, sv.branch_name
                        ORDER BY sv.version_sort_key DESC NULLS LAST, sv.version_seq DESC, sv.id DESC
                    ) AS primary_previous_version_id
                FROM public.specification_versions sv
                LEFT JOIN public.documents svd
                  ON svd.document_id = sv.document_id
                WHERE {primary_svd}
            )
            SELECT
                sv.id AS specification_version_id,
                sv.specification_id,
                sp.specification_key,
                sp.project_code,
                sp.region_scope,
                sp.spec_code,
                sp.spec_type,
                sp.spec_subtype,
                cat.canonical_name,
                sv.version_seq,
                sv.version_label,
                sv.branch_name,
                sv.primary_previous_version_id AS previous_version_id,
                sv.document_id,
                sv.content_hash,
                sv.status,
                sv.version_source,
                sv.revision_reason,
                sv.change_summary,
                sv.effective_at,
                (cat.current_version_id = sv.id) AS is_current,
                CASE
                    WHEN cat.current_version_id = sv.id THEN 'CURRENT'
                    ELSE 'HISTORY'
                END AS version_role,
                sv.created_at,
                sv.updated_at,
                -- Application v1.01: history is body-only and adjacency is body-to-body.
                sv.version_sort_key,
                pv.version_label AS previous_version_label,
                COALESCE(cs.added_count, 0) AS added_count,
                COALESCE(cs.modified_count, 0) AS modified_count,
                COALESCE(cs.removed_count, 0) AS removed_count,
                COALESCE(cs.moved_count, 0) AS moved_count,
                COALESCE(cs.unchanged_count, 0) AS unchanged_count,
                cs.summary_text AS deterministic_change_summary
            FROM primary_versions sv
            JOIN public.specifications sp
              ON sp.id = sv.specification_id
            JOIN public.specification_catalog cat
              ON cat.specification_id = sv.specification_id
            LEFT JOIN public.specification_versions pv
              ON pv.id = sv.primary_previous_version_id
            LEFT JOIN public.specification_change_sets cs
              ON cs.specification_id = sv.specification_id
             AND cs.from_version_id = sv.primary_previous_version_id
             AND cs.to_version_id = sv.id
             AND cs.is_adjacent IS TRUE
             AND cs.status = 'READY'
             AND cs.algorithm_version = 'section-structural-v1'
            """
        )
        if not history_existed:
            created.append("view:specification_version_history")

        cursor.execute("SELECT to_regclass('public.specification_change_summary')")
        change_summary_existed = cursor.fetchone()[0] is not None
        cursor.execute(
            f"""
            CREATE OR REPLACE VIEW public.specification_change_summary AS
            SELECT
                cs.id AS change_set_id,
                cs.specification_id,
                sp.specification_key,
                sp.project_code,
                sp.canonical_name,
                cs.from_version_id,
                fv.version_label AS from_version,
                cs.to_version_id,
                tv.version_label AS to_version,
                cs.branch_name,
                cs.algorithm_version,
                cs.is_adjacent,
                cs.status,
                cs.added_count,
                cs.modified_count,
                cs.removed_count,
                cs.moved_count,
                cs.unchanged_count,
                (cs.added_count + cs.modified_count + cs.removed_count + cs.moved_count) AS total_change_count,
                cs.summary_text,
                cs.created_at,
                cs.updated_at
            FROM public.specification_change_sets cs
            JOIN public.specifications sp ON sp.id = cs.specification_id
            JOIN public.specification_versions fv ON fv.id = cs.from_version_id
            JOIN public.specification_versions tv ON tv.id = cs.to_version_id
            LEFT JOIN public.documents fvd ON fvd.document_id = fv.document_id
            LEFT JOIN public.documents tvd ON tvd.document_id = tv.document_id
            WHERE {primary_fvd}
              AND {primary_tvd}
            """
        )
        if not change_summary_existed:
            created.append("view:specification_change_summary")

        cursor.execute("SELECT to_regclass('public.specification_change_detail')")
        change_detail_existed = cursor.fetchone()[0] is not None
        cursor.execute(
            f"""
            CREATE OR REPLACE VIEW public.specification_change_detail AS
            SELECT
                sc.id AS change_id,
                sc.change_set_id,
                sc.specification_id,
                sp.specification_key,
                sp.canonical_name,
                fv.version_label AS from_version,
                tv.version_label AS to_version,
                sc.change_type,
                sc.entity_type,
                sc.entity_key,
                sc.chapter_id,
                sc.chapter_title,
                sc.section_id,
                sc.section_title,
                sc.old_content_ids,
                sc.new_content_ids,
                sc.old_text,
                sc.new_text,
                sc.old_hash,
                sc.new_hash,
                sc.old_page_number,
                sc.new_page_number,
                sc.old_sort_order,
                sc.new_sort_order,
                sc.similarity_score,
                sc.details,
                sc.created_at
            FROM public.specification_changes sc
            JOIN public.specifications sp ON sp.id = sc.specification_id
            JOIN public.specification_versions fv ON fv.id = sc.from_version_id
            JOIN public.specification_versions tv ON tv.id = sc.to_version_id
            LEFT JOIN public.documents fvd ON fvd.document_id = fv.document_id
            LEFT JOIN public.documents tvd ON tvd.document_id = tv.document_id
            WHERE {primary_fvd}
              AND {primary_tvd}
            """
        )
        if not change_detail_existed:
            created.append("view:specification_change_detail")

        cursor.execute("SELECT to_regclass('public.engineering_entity_catalog')")
        entity_catalog_existed = cursor.fetchone()[0] is not None
        cursor.execute(
            f"""
            CREATE OR REPLACE VIEW public.engineering_entity_catalog AS
            SELECT
                e.id AS entity_id,
                e.extraction_run_id,
                e.specification_id,
                sp.specification_key,
                sp.canonical_name AS specification_name,
                e.version_id,
                sv.version_label AS version_label,
                e.entity_type,
                e.entity_key,
                e.canonical_name,
                e.normalized_value,
                e.extraction_method,
                e.confidence,
                e.status,
                run.extractor_version,
                (
                    run.status = 'READY'
                    AND run.id = (
                        SELECT rer.id
                        FROM public.relation_extraction_runs rer
                        WHERE rer.version_id = e.version_id
                          AND rer.status = 'READY'
                        ORDER BY rer.id DESC
                        LIMIT 1
                    )
                ) AS is_current_extraction,
                (
                    e.version_id = COALESCE(
                        (
                            SELECT psv.id
                            FROM public.specification_versions psv
                            LEFT JOIN public.documents pd ON pd.document_id = psv.document_id
                            WHERE psv.specification_id = sp.id
                              AND psv.branch_name = 'MAIN'
                              AND {primary_pd}
                            ORDER BY
                                CASE WHEN psv.id = sp.current_version_id THEN 0 ELSE 1 END,
                                psv.version_sort_key DESC NULLS LAST, psv.version_seq DESC, psv.id DESC
                            LIMIT 1
                        ),
                        sp.current_version_id
                    )
                ) AS is_current_source_version,
                e.metadata,
                e.created_at
            FROM public.engineering_entities e
            JOIN public.relation_extraction_runs run ON run.id = e.extraction_run_id
            JOIN public.specifications sp ON sp.id = e.specification_id
            JOIN public.specification_versions sv ON sv.id = e.version_id
            """
        )
        if not entity_catalog_existed:
            created.append("view:engineering_entity_catalog")

        cursor.execute("SELECT to_regclass('public.engineering_relation_catalog')")
        relation_catalog_existed = cursor.fetchone()[0] is not None
        cursor.execute(
            f"""
            CREATE OR REPLACE VIEW public.engineering_relation_catalog AS
            SELECT
                r.id AS relation_id,
                r.extraction_run_id,
                r.source_specification_id,
                src.specification_key AS source_specification_key,
                src.canonical_name AS source_specification_name,
                r.source_version_id,
                sv.version_label AS source_version,
                r.source_entity_id,
                se.entity_type AS source_entity_type,
                se.entity_key AS source_entity_key,
                se.canonical_name AS source_entity_name,
                r.target_specification_id,
                tgt.specification_key AS target_specification_key,
                tgt.canonical_name AS target_specification_name,
                r.target_entity_id,
                te.entity_type AS target_entity_type,
                te.entity_key AS target_entity_key,
                te.canonical_name AS target_entity_name,
                r.target_external_key,
                r.relation_type,
                r.extraction_method,
                r.confidence,
                r.status,
                r.is_explicit,
                COUNT(ev.id)::INTEGER AS evidence_count,
                run.extractor_version,
                (
                    run.status = 'READY'
                    AND run.id = (
                        SELECT rer.id
                        FROM public.relation_extraction_runs rer
                        WHERE rer.version_id = r.source_version_id
                          AND rer.status = 'READY'
                        ORDER BY rer.id DESC
                        LIMIT 1
                    )
                ) AS is_current_extraction,
                (
                    r.source_version_id = COALESCE(
                        (
                            SELECT psv.id
                            FROM public.specification_versions psv
                            LEFT JOIN public.documents pd ON pd.document_id = psv.document_id
                            WHERE psv.specification_id = src.id
                              AND psv.branch_name = 'MAIN'
                              AND {primary_pd}
                            ORDER BY
                                CASE WHEN psv.id = src.current_version_id THEN 0 ELSE 1 END,
                                psv.version_sort_key DESC NULLS LAST, psv.version_seq DESC, psv.id DESC
                            LIMIT 1
                        ),
                        src.current_version_id
                    )
                ) AS is_current_source_version,
                r.metadata,
                r.created_at
            FROM public.spec_relations r
            JOIN public.relation_extraction_runs run ON run.id = r.extraction_run_id
            JOIN public.specifications src ON src.id = r.source_specification_id
            JOIN public.specification_versions sv ON sv.id = r.source_version_id
            LEFT JOIN public.engineering_entities se ON se.id = r.source_entity_id
            LEFT JOIN public.specifications tgt ON tgt.id = r.target_specification_id
            LEFT JOIN public.engineering_entities te ON te.id = r.target_entity_id
            LEFT JOIN public.relation_evidence ev ON ev.relation_id = r.id
            GROUP BY
                r.id, r.extraction_run_id, r.source_specification_id, src.specification_key,
                src.canonical_name, r.source_version_id, sv.version_label, r.source_entity_id,
                se.entity_type, se.entity_key, se.canonical_name, r.target_specification_id,
                tgt.specification_key, tgt.canonical_name, r.target_entity_id, te.entity_type,
                te.entity_key, te.canonical_name, r.target_external_key, r.relation_type,
                r.extraction_method, r.confidence, r.status, r.is_explicit,
                run.extractor_version, run.status, src.current_version_id, r.metadata, r.created_at
            """
        )
        if not relation_catalog_existed:
            created.append("view:engineering_relation_catalog")

        cursor.execute("SELECT to_regclass('public.spec_current_relations')")
        current_relations_existed = cursor.fetchone()[0] is not None
        cursor.execute(
            """
            CREATE OR REPLACE VIEW public.spec_current_relations AS
            SELECT *
            FROM public.engineering_relation_catalog
            WHERE is_current_extraction IS TRUE
              AND is_current_source_version IS TRUE
              AND status = 'CONFIRMED'
            """
        )
        if not current_relations_existed:
            created.append("view:spec_current_relations")

        cursor.execute("SELECT to_regclass('public.relation_evidence_detail')")
        evidence_detail_existed = cursor.fetchone()[0] is not None
        cursor.execute(
            """
            CREATE OR REPLACE VIEW public.relation_evidence_detail AS
            SELECT
                ev.id AS evidence_id,
                ev.relation_id,
                cat.source_specification_id,
                cat.source_specification_key,
                cat.source_specification_name,
                cat.source_version_id,
                cat.source_version,
                cat.relation_type,
                cat.target_specification_id,
                cat.target_specification_name,
                cat.target_entity_id,
                cat.target_entity_type,
                cat.target_entity_name,
                cat.target_external_key,
                cat.extraction_method,
                cat.confidence,
                cat.status,
                ev.document_id,
                ev.content_id,
                ev.page_number,
                ev.chapter_id,
                ev.chapter_title,
                ev.section_id,
                ev.section_title,
                ev.sheet_name,
                ev.cell_range,
                ev.evidence_text,
                ev.start_offset,
                ev.end_offset,
                ev.evidence_hash,
                ev.metadata,
                ev.created_at
            FROM public.relation_evidence ev
            JOIN public.engineering_relation_catalog cat ON cat.relation_id = ev.relation_id
            WHERE cat.is_current_extraction IS TRUE
            """
        )
        if not evidence_detail_existed:
            created.append("view:relation_evidence_detail")

        cursor.execute("SELECT to_regclass('public.relation_extraction_status')")
        extraction_status_existed = cursor.fetchone()[0] is not None
        cursor.execute(
            """
            CREATE OR REPLACE VIEW public.relation_extraction_status AS
            SELECT
                run.id AS extraction_run_id,
                run.specification_id,
                sp.specification_key,
                sp.canonical_name AS specification_name,
                run.version_id,
                sv.version_label,
                run.document_id,
                run.extractor_version,
                run.status,
                run.is_current,
                run.entity_count,
                run.relation_count,
                run.unresolved_count,
                run.stats,
                run.error_message,
                run.started_at,
                run.finished_at,
                run.created_at
            FROM public.relation_extraction_runs run
            JOIN public.specifications sp ON sp.id = run.specification_id
            JOIN public.specification_versions sv ON sv.id = run.version_id
            """
        )
        if not extraction_status_existed:
            created.append("view:relation_extraction_status")

    def _record_project_identity(self, cursor: Any) -> None:
        if not self.project_code:
            return

        project_name = self.project_name or self.project_code

        cursor.execute(
            """
            SELECT project_code, project_name
            FROM public.project_info
            ORDER BY created_at ASC
            LIMIT 1
            """
        )
        existing = cursor.fetchone()
        if existing and str(existing[0]) != self.project_code:
            raise SchemaManagerError(
                "Target database is already bound to project "
                f"{existing[1]} ({existing[0]}), not "
                f"{project_name} ({self.project_code})."
            )

        cursor.execute(
            """
            INSERT INTO public.project_info(
                project_code, project_name, created_at, updated_at
            )
            VALUES (%s, %s, NOW(), NOW())
            ON CONFLICT (project_code)
            DO UPDATE SET
                project_name = EXCLUDED.project_name,
                updated_at = NOW()
            """,
            (self.project_code, project_name),
        )

        cursor.execute(
            """
            INSERT INTO public.schema_version(component, version, name, applied_at)
            VALUES ('document_ingestion_project', %s, %s, NOW())
            ON CONFLICT (component)
            DO UPDATE SET
                version = EXCLUDED.version,
                name = EXCLUDED.name,
                applied_at = EXCLUDED.applied_at
            """,
            (PROJECT_SCHEMA_VERSION, PROJECT_SCHEMA_NAME),
        )

    @staticmethod
    def _record_schema_version(cursor: Any) -> None:
        cursor.execute(
            """
            INSERT INTO public.schema_version(component, version, name, applied_at)
            VALUES ('document_ingestion_rag', %s, %s, NOW())
            ON CONFLICT (component)
            DO UPDATE SET
                version = EXCLUDED.version,
                name = EXCLUDED.name,
                applied_at = EXCLUDED.applied_at
            """,
            (RAG_SCHEMA_VERSION, RAG_SCHEMA_NAME),
        )

    @staticmethod
    def _read_schema_version(cursor: Any) -> int | None:
        if not SchemaManager._relation_exists(
            cursor, "schema_version", kinds=("r", "p")
        ):
            return None
        cursor.execute(
            """
            SELECT version
            FROM public.schema_version
            WHERE component = 'document_ingestion_rag'
            """
        )
        row = cursor.fetchone()
        return int(row[0]) if row else None

    # ------------------------------------------------------------------
    # Catalog helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _relation_exists(cursor: Any, name: str, kinds: tuple[str, ...]) -> bool:
        cursor.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public'
                  AND c.relname = %s
                  AND c.relkind = ANY(%s)
            )
            """,
            (name, list(kinds)),
        )
        return bool(cursor.fetchone()[0])

    @staticmethod
    def _table_columns(cursor: Any, table_name: str) -> set[str]:
        cursor.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = %s
            """,
            (table_name,),
        )
        return {row[0] for row in cursor.fetchall()}

    @classmethod
    def _all_columns_exist(
        cls,
        cursor: Any,
        table_name: str,
        columns: Sequence[str],
    ) -> bool:
        actual = cls._table_columns(cursor, table_name)
        return all(column in actual for column in columns)

    @staticmethod
    def _find_equivalent_index(
        cursor: Any,
        *,
        table_name: str,
        columns: Sequence[str],
        unique: bool,
    ) -> str | None:
        cursor.execute(
            """
            SELECT
                idx.relname AS index_name,
                i.indisunique,
                array_agg(att.attname ORDER BY keys.ordinality) AS columns
            FROM pg_index i
            JOIN pg_class tbl ON tbl.oid = i.indrelid
            JOIN pg_namespace ns ON ns.oid = tbl.relnamespace
            JOIN pg_class idx ON idx.oid = i.indexrelid
            JOIN pg_am am ON am.oid = idx.relam
            JOIN LATERAL unnest(i.indkey)
                WITH ORDINALITY AS keys(attnum, ordinality) ON TRUE
            JOIN pg_attribute att
              ON att.attrelid = tbl.oid
             AND att.attnum = keys.attnum
            WHERE ns.nspname = 'public'
              AND tbl.relname = %s
              AND am.amname = 'btree'
              AND i.indpred IS NULL
              AND i.indexprs IS NULL
            GROUP BY idx.relname, i.indisunique
            """,
            (table_name,),
        )
        wanted = tuple(columns)
        for index_name, is_unique, index_columns in cursor.fetchall():
            if bool(is_unique) != bool(unique):
                continue
            if tuple(index_columns or ()) == wanted:
                return str(index_name)
        return None

    @staticmethod
    def _find_duplicate_index_groups(cursor: Any) -> list[str]:
        cursor.execute(
            """
            SELECT
                tbl.relname AS table_name,
                idx.relname AS index_name,
                i.indisunique,
                i.indisprimary,
                array_agg(att.attname ORDER BY keys.ordinality) AS columns
            FROM pg_index i
            JOIN pg_class tbl ON tbl.oid = i.indrelid
            JOIN pg_namespace ns ON ns.oid = tbl.relnamespace
            JOIN pg_class idx ON idx.oid = i.indexrelid
            JOIN pg_am am ON am.oid = idx.relam
            JOIN LATERAL unnest(i.indkey)
                WITH ORDINALITY AS keys(attnum, ordinality) ON TRUE
            JOIN pg_attribute att
              ON att.attrelid = tbl.oid
             AND att.attnum = keys.attnum
            WHERE ns.nspname = 'public'
              AND am.amname = 'btree'
              AND i.indpred IS NULL
              AND i.indexprs IS NULL
              AND tbl.relname IN (
                    'specifications', 'specification_versions',
                    'documents', 'chapters', 'sections', 'contents',
                    'embeddings', 'vector_delete_queue'
              )
            GROUP BY tbl.relname, idx.relname, i.indisunique, i.indisprimary
            ORDER BY tbl.relname, idx.relname
            """
        )

        groups: dict[tuple[str, bool, tuple[str, ...]], list[str]] = {}
        for table, index_name, is_unique, _is_primary, columns in cursor.fetchall():
            key = (str(table), bool(is_unique), tuple(columns or ()))
            groups.setdefault(key, []).append(str(index_name))

        result: list[str] = []
        for (table, is_unique, columns), names in groups.items():
            if len(names) <= 1:
                continue
            result.append(
                f"{table} ({'UNIQUE ' if is_unique else ''}"
                f"{', '.join(columns)}): {', '.join(names)}"
            )
        return result
