from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import json
from typing import Any, Iterator

import psycopg


CONFIG_SCHEMA = "app_config"
CONFIG_SCHEMA_VERSION = 1
CONFIG_SCHEMA_NAME = "enterprise-configuration-governance-v1"


class ConfigurationSchemaError(RuntimeError):
    """Raised when the enterprise configuration schema cannot be managed safely."""


@dataclass(frozen=True, slots=True)
class ConfigurationSchemaReport:
    ready: bool
    version: int | None
    required_tables: tuple[str, ...]
    available_tables: tuple[str, ...]
    missing_tables: tuple[str, ...]
    missing_columns: dict[str, tuple[str, ...]] = field(default_factory=dict)
    created_objects: tuple[str, ...] = ()
    seeded_defaults: int = 0
    warnings: tuple[str, ...] = ()

    def status_text(self) -> str:
        if self.ready:
            return (
                f"Configuration Schema v{self.version or CONFIG_SCHEMA_VERSION} ready | "
                f"Tables {len(self.available_tables)}/{len(self.required_tables)} | "
                f"Defaults preserved / seeded safely"
            )
        parts = ["Configuration schema is not ready"]
        if self.missing_tables:
            parts.append("Missing tables: " + ", ".join(self.missing_tables))
        for table_name, columns in self.missing_columns.items():
            parts.append(f"{table_name} missing columns: " + ", ".join(columns))
        return "\n".join(parts)


class ConfigurationSchemaManager:
    """
    Non-destructive control-plane schema manager.

    Configuration objects are isolated in ``app_config`` so business RAG data
    remains in ``public``. This manager never executes DROP TABLE, TRUNCATE,
    DELETE, or destructive reset operations. Existing configuration values are
    preserved by ``ON CONFLICT DO NOTHING``. Raw credentials are intentionally
    excluded from all default rows; secrets must remain in the OS credential
    mechanism / environment and can only be referenced by name.
    """

    TABLE_COLUMNS: dict[str, set[str]] = {
        "app_settings": {
            "setting_key", "setting_value", "value_type", "category",
            "description", "is_secret", "credential_reference", "is_system",
            "created_at", "updated_at",
        },
        "project_scopes": {
            "scope_code", "display_name", "postgres_database", "enabled",
            "include_common", "metadata", "created_at", "updated_at",
        },
        "ai_model_profiles": {
            "id", "profile_name", "provider", "base_url", "model_name",
            "model_role", "context_window", "temperature", "supports_streaming",
            "supports_tools", "supports_json", "is_active", "is_default",
            "config", "created_at", "updated_at",
        },
        "embedding_profiles": {
            "id", "profile_name", "provider", "model_name", "dimension",
            "distance_metric", "embedding_version", "normalize_vectors",
            "document_prefix", "query_prefix", "is_active", "is_default",
            "created_at", "updated_at",
        },
        "rag_runtime_profiles": {
            "id", "profile_name", "planner_model_profile_id",
            "chat_model_profile_id", "embedding_profile_id", "default_top_k",
            "final_evidence_k", "min_semantic_score", "enable_query_rewrite",
            "enable_multi_query", "enable_relation_search", "enable_version_search",
            "enable_reranking", "enable_evidence_grading",
            "enable_conflict_detection", "max_agent_iterations", "config",
            "is_active", "is_default", "created_at", "updated_at",
        },
        "vector_collection_registry": {
            "id", "scope_code", "collection_name", "embedding_profile_id",
            "vector_dimension", "distance_metric", "payload_contract",
            "payload_contract_version", "sync_generation", "last_sync_at",
            "expected_point_count", "actual_point_count", "status", "metadata",
            "created_at", "updated_at",
        },
        "prompt_templates": {
            "id", "prompt_key", "display_name", "prompt_type", "is_active",
            "created_at", "updated_at",
        },
        "prompt_versions": {
            "id", "prompt_id", "version", "content", "created_at",
        },
        "data_quality_rules": {
            "rule_code", "rule_name", "severity", "enabled", "config",
            "created_at", "updated_at",
        },
    }

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
        self.project_code = str(project_code or "").strip().upper() or None
        self.project_name = str(project_name or project_code or "").strip() or self.project_code

        if self.database_connection is None:
            missing = [
                name for name, value in (
                    ("host", self.host), ("database", self.database), ("user", self.user)
                ) if not value
            ]
            if missing:
                raise ValueError("Missing PostgreSQL connection settings: " + ", ".join(missing))
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
            application_name="document-intelligence-configuration-schema-manager",
        ) as connection:
            yield connection

    def ensure_schema(self) -> ConfigurationSchemaReport:
        created: list[str] = []
        warnings: list[str] = []
        seeded = 0
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(f"CREATE SCHEMA IF NOT EXISTS {CONFIG_SCHEMA}")
                    created.append(f"schema:{CONFIG_SCHEMA}")
                    self._create_tables(cursor, created)
                    self._create_indexes(cursor, created)
                    seeded += self._seed_defaults(cursor)
                    self._record_current_scope(cursor)
                    self._record_configuration_version(cursor)
                connection.commit()
        except Exception as exc:
            raise ConfigurationSchemaError(
                f"Failed to initialize / upgrade enterprise configuration schema: {exc}"
            ) from exc

        inspected = self.inspect_schema()
        return ConfigurationSchemaReport(
            ready=inspected.ready,
            version=inspected.version,
            required_tables=inspected.required_tables,
            available_tables=inspected.available_tables,
            missing_tables=inspected.missing_tables,
            missing_columns=inspected.missing_columns,
            created_objects=tuple(dict.fromkeys(created)),
            seeded_defaults=seeded,
            warnings=tuple(warnings) + inspected.warnings,
        )

    def inspect_schema(self) -> ConfigurationSchemaReport:
        required_tables = tuple(self.TABLE_COLUMNS)
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT table_name
                        FROM information_schema.tables
                        WHERE table_schema = %s AND table_type = 'BASE TABLE'
                        """,
                        (CONFIG_SCHEMA,),
                    )
                    tables = {row[0] for row in cursor.fetchall()}
                    cursor.execute(
                        """
                        SELECT table_name, column_name
                        FROM information_schema.columns
                        WHERE table_schema = %s AND table_name = ANY(%s)
                        """,
                        (CONFIG_SCHEMA, list(required_tables)),
                    )
                    columns: dict[str, set[str]] = {name: set() for name in required_tables}
                    for table_name, column_name in cursor.fetchall():
                        if table_name in columns:
                            columns[table_name].add(column_name)
                    cursor.execute(
                        """
                        SELECT version
                        FROM public.schema_version
                        WHERE component = 'configuration'
                        """
                    )
                    row = cursor.fetchone()
                    version = int(row[0]) if row else None
        except Exception as exc:
            raise ConfigurationSchemaError(
                f"Failed to inspect enterprise configuration schema: {exc}"
            ) from exc

        missing_tables = tuple(name for name in required_tables if name not in tables)
        available_tables = tuple(name for name in required_tables if name in tables)
        missing_columns: dict[str, tuple[str, ...]] = {}
        for table_name, expected in self.TABLE_COLUMNS.items():
            if table_name not in tables:
                continue
            missing = tuple(sorted(expected - columns.get(table_name, set())))
            if missing:
                missing_columns[table_name] = missing
        ready = not missing_tables and not missing_columns and version == CONFIG_SCHEMA_VERSION
        return ConfigurationSchemaReport(
            ready=ready,
            version=version,
            required_tables=required_tables,
            available_tables=available_tables,
            missing_tables=missing_tables,
            missing_columns=missing_columns,
        )

    @staticmethod
    def _create_tables(cursor: Any, created: list[str]) -> None:
        statements = {
            "app_settings": """
                CREATE TABLE IF NOT EXISTS app_config.app_settings (
                    setting_key TEXT PRIMARY KEY,
                    setting_value JSONB,
                    value_type TEXT NOT NULL DEFAULT 'string',
                    category TEXT NOT NULL DEFAULT 'general',
                    description TEXT,
                    is_secret BOOLEAN NOT NULL DEFAULT FALSE,
                    credential_reference TEXT,
                    is_system BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    CONSTRAINT ck_app_settings_secret_reference CHECK (
                        is_secret = FALSE OR
                        (setting_value IS NULL AND credential_reference IS NOT NULL)
                    )
                )
            """,
            "project_scopes": """
                CREATE TABLE IF NOT EXISTS app_config.project_scopes (
                    scope_code TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    postgres_database TEXT,
                    enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    include_common BOOLEAN NOT NULL DEFAULT TRUE,
                    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """,
            "ai_model_profiles": """
                CREATE TABLE IF NOT EXISTS app_config.ai_model_profiles (
                    id BIGSERIAL PRIMARY KEY,
                    profile_name TEXT NOT NULL UNIQUE,
                    provider TEXT NOT NULL,
                    base_url TEXT,
                    model_name TEXT NOT NULL,
                    model_role TEXT NOT NULL,
                    context_window INTEGER,
                    temperature NUMERIC(5,3),
                    supports_streaming BOOLEAN NOT NULL DEFAULT TRUE,
                    supports_tools BOOLEAN NOT NULL DEFAULT FALSE,
                    supports_json BOOLEAN NOT NULL DEFAULT FALSE,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    is_default BOOLEAN NOT NULL DEFAULT FALSE,
                    config JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    CONSTRAINT ck_ai_model_role CHECK (
                        model_role IN ('CHAT','EMBEDDING','RERANKER','PLANNER')
                    )
                )
            """,
            "embedding_profiles": """
                CREATE TABLE IF NOT EXISTS app_config.embedding_profiles (
                    id BIGSERIAL PRIMARY KEY,
                    profile_name TEXT NOT NULL UNIQUE,
                    provider TEXT NOT NULL,
                    model_name TEXT NOT NULL,
                    dimension INTEGER NOT NULL CHECK (dimension > 0),
                    distance_metric TEXT NOT NULL DEFAULT 'Cosine',
                    embedding_version TEXT NOT NULL,
                    normalize_vectors BOOLEAN NOT NULL DEFAULT TRUE,
                    document_prefix TEXT,
                    query_prefix TEXT,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    is_default BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """,
            "rag_runtime_profiles": """
                CREATE TABLE IF NOT EXISTS app_config.rag_runtime_profiles (
                    id BIGSERIAL PRIMARY KEY,
                    profile_name TEXT NOT NULL UNIQUE,
                    planner_model_profile_id BIGINT REFERENCES app_config.ai_model_profiles(id),
                    chat_model_profile_id BIGINT REFERENCES app_config.ai_model_profiles(id),
                    embedding_profile_id BIGINT REFERENCES app_config.embedding_profiles(id),
                    default_top_k INTEGER NOT NULL DEFAULT 8 CHECK (default_top_k > 0),
                    final_evidence_k INTEGER NOT NULL DEFAULT 8 CHECK (final_evidence_k > 0),
                    min_semantic_score NUMERIC(8,6) DEFAULT 0.25,
                    enable_query_rewrite BOOLEAN NOT NULL DEFAULT TRUE,
                    enable_multi_query BOOLEAN NOT NULL DEFAULT TRUE,
                    enable_relation_search BOOLEAN NOT NULL DEFAULT TRUE,
                    enable_version_search BOOLEAN NOT NULL DEFAULT TRUE,
                    enable_reranking BOOLEAN NOT NULL DEFAULT TRUE,
                    enable_evidence_grading BOOLEAN NOT NULL DEFAULT TRUE,
                    enable_conflict_detection BOOLEAN NOT NULL DEFAULT TRUE,
                    max_agent_iterations INTEGER NOT NULL DEFAULT 4 CHECK (max_agent_iterations > 0),
                    config JSONB NOT NULL DEFAULT '{}'::jsonb,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    is_default BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """,
            "vector_collection_registry": """
                CREATE TABLE IF NOT EXISTS app_config.vector_collection_registry (
                    id BIGSERIAL PRIMARY KEY,
                    scope_code TEXT NOT NULL,
                    collection_name TEXT NOT NULL UNIQUE,
                    embedding_profile_id BIGINT REFERENCES app_config.embedding_profiles(id),
                    vector_dimension INTEGER NOT NULL CHECK (vector_dimension > 0),
                    distance_metric TEXT NOT NULL,
                    payload_contract TEXT NOT NULL,
                    payload_contract_version TEXT NOT NULL,
                    sync_generation TEXT,
                    last_sync_at TIMESTAMPTZ,
                    expected_point_count BIGINT,
                    actual_point_count BIGINT,
                    status TEXT NOT NULL DEFAULT 'REGISTERED',
                    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    CONSTRAINT uq_vector_registry_scope UNIQUE (scope_code, collection_name)
                )
            """,
            "prompt_templates": """
                CREATE TABLE IF NOT EXISTS app_config.prompt_templates (
                    id BIGSERIAL PRIMARY KEY,
                    prompt_key TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL,
                    prompt_type TEXT NOT NULL,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """,
            "prompt_versions": """
                CREATE TABLE IF NOT EXISTS app_config.prompt_versions (
                    id BIGSERIAL PRIMARY KEY,
                    prompt_id BIGINT NOT NULL REFERENCES app_config.prompt_templates(id),
                    version INTEGER NOT NULL CHECK (version > 0),
                    content TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    CONSTRAINT uq_prompt_version UNIQUE (prompt_id, version)
                )
            """,
            "data_quality_rules": """
                CREATE TABLE IF NOT EXISTS app_config.data_quality_rules (
                    rule_code TEXT PRIMARY KEY,
                    rule_name TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    config JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    CONSTRAINT ck_dq_severity CHECK (severity IN ('INFO','WARNING','ERROR','CRITICAL'))
                )
            """,
        }
        for name, statement in statements.items():
            cursor.execute(statement)
            created.append(f"{CONFIG_SCHEMA}.{name}")

    @staticmethod
    def _create_indexes(cursor: Any, created: list[str]) -> None:
        indexes = {
            "idx_app_settings_category": "CREATE INDEX IF NOT EXISTS idx_app_settings_category ON app_config.app_settings(category)",
            "idx_ai_models_role_active": "CREATE INDEX IF NOT EXISTS idx_ai_models_role_active ON app_config.ai_model_profiles(model_role, is_active)",
            "idx_embedding_profiles_active": "CREATE INDEX IF NOT EXISTS idx_embedding_profiles_active ON app_config.embedding_profiles(is_active, is_default)",
            "idx_rag_runtime_active": "CREATE INDEX IF NOT EXISTS idx_rag_runtime_active ON app_config.rag_runtime_profiles(is_active, is_default)",
            "idx_vector_registry_scope": "CREATE INDEX IF NOT EXISTS idx_vector_registry_scope ON app_config.vector_collection_registry(scope_code, status)",
            "idx_prompt_templates_type": "CREATE INDEX IF NOT EXISTS idx_prompt_templates_type ON app_config.prompt_templates(prompt_type, is_active)",
            "idx_dq_rules_enabled": "CREATE INDEX IF NOT EXISTS idx_dq_rules_enabled ON app_config.data_quality_rules(enabled, severity)",
        }
        for name, statement in indexes.items():
            cursor.execute(statement)
            created.append(f"index:{name}")

    def _seed_defaults(self, cursor: Any) -> int:
        inserted = 0
        settings = (
            ("ai.temperature", 0.2, "number", "ai", "Default generation temperature"),
            ("ai.context_window", 4096, "integer", "ai", "Default Ollama context window (CPU-friendly baseline)"),
            ("rag.default_top_k", 12, "integer", "rag", "Candidate retrieval limit before governance/reranking"),
            ("rag.final_evidence_k", 4, "integer", "rag", "Maximum governed evidence supplied to the answer model"),
            ("rag.default_scope", "AUTO", "string", "rag", "Default automatic project scope routing"),
            ("ai.ollama_connect_timeout", 10, "integer", "ai", "Ollama connection timeout seconds"),
            ("ai.ollama_read_timeout", 600, "integer", "ai", "Ollama non-stream read timeout seconds"),
            ("ai.ollama_first_load_timeout", 900, "integer", "ai", "Ollama first response/model load timeout seconds"),
            ("ai.ollama_stream_idle_timeout", 180, "integer", "ai", "Maximum idle seconds between streamed payloads"),
            ("ai.ollama_keep_alive", "30m", "string", "ai", "Ollama model keep-alive duration"),
            ("qdrant.timeout_seconds", 10, "integer", "qdrant", "Qdrant request timeout"),
        )
        for key, value, value_type, category, description in settings:
            cursor.execute(
                """
                INSERT INTO app_config.app_settings(
                    setting_key, setting_value, value_type, category, description, is_system
                ) VALUES (%s, %s::jsonb, %s, %s, %s, TRUE)
                ON CONFLICT (setting_key) DO NOTHING
                """,
                (key, json.dumps(value, ensure_ascii=False), value_type, category, description),
            )
            inserted += max(0, int(cursor.rowcount or 0))

        cursor.execute(
            """
            INSERT INTO app_config.embedding_profiles(
                profile_name, provider, model_name, dimension, distance_metric,
                embedding_version, normalize_vectors, is_active, is_default
            ) VALUES (
                'bge-m3-current', 'sentence-transformers', 'BAAI/bge-m3', 1024,
                'Cosine', 'current-index-v1', TRUE, TRUE, TRUE
            )
            ON CONFLICT (profile_name) DO NOTHING
            """
        )
        inserted += max(0, int(cursor.rowcount or 0))

        cursor.execute(
            """
            INSERT INTO app_config.rag_runtime_profiles(
                profile_name, embedding_profile_id, default_top_k, final_evidence_k,
                min_semantic_score, enable_query_rewrite, enable_multi_query,
                enable_relation_search, enable_version_search, enable_reranking,
                enable_evidence_grading, enable_conflict_detection,
                max_agent_iterations, is_active, is_default
            )
            SELECT 'enterprise-auto', id, 12, 4, 0.25, TRUE, TRUE, TRUE, TRUE,
                   TRUE, TRUE, TRUE, 4, TRUE, TRUE
            FROM app_config.embedding_profiles
            WHERE profile_name = 'bge-m3-current'
            ON CONFLICT (profile_name) DO NOTHING
            """
        )
        inserted += max(0, int(cursor.rowcount or 0))

        prompts = (
            ("engineering_qa", "Engineering Grounded QA", "ANSWER"),
            ("agent_planner", "Enterprise Retrieval Planner", "PLANNER"),
            ("version_compare", "Specification Version Comparison", "ANSWER"),
            ("relation_analysis", "Engineering Relation Analysis", "ANSWER"),
            ("issue_qa", "QA / BUG / Jira Grounded QA", "ANSWER"),
            ("general_chat", "General Non-Repository Chat", "ANSWER"),
        )
        for prompt_key, display_name, prompt_type in prompts:
            cursor.execute(
                """
                INSERT INTO app_config.prompt_templates(prompt_key, display_name, prompt_type)
                VALUES (%s, %s, %s)
                ON CONFLICT (prompt_key) DO NOTHING
                """,
                (prompt_key, display_name, prompt_type),
            )
            inserted += max(0, int(cursor.rowcount or 0))

        rules = (
            ("QDRANT_MISSING_POINT", "SQL current chunk missing from Qdrant", "ERROR"),
            ("QDRANT_ORPHAN_POINT", "Qdrant point has no SQL current chunk", "ERROR"),
            ("POINT_ID_MISMATCH", "Qdrant point id differs from content_id", "ERROR"),
            ("CONTENT_HASH_MISMATCH", "Qdrant content hash differs from PostgreSQL", "ERROR"),
            ("STALE_EMBEDDING", "Embedded content hash is stale", "ERROR"),
            ("PAYLOAD_CONTRACT_INVALID", "Qdrant payload contract is invalid", "ERROR"),
            ("VERSION_CONFLICT", "Document metadata and internal version marker conflict", "WARNING"),
            ("MISSING_SPEC_CODE", "Specification code is missing", "ERROR"),
            ("MISSING_SECTION_TITLE", "Section title is missing", "WARNING"),
            ("EMPTY_CLASSIFICATION", "Classification field is empty", "INFO"),
        )
        for rule_code, rule_name, severity in rules:
            cursor.execute(
                """
                INSERT INTO app_config.data_quality_rules(rule_code, rule_name, severity)
                VALUES (%s, %s, %s)
                ON CONFLICT (rule_code) DO NOTHING
                """,
                (rule_code, rule_name, severity),
            )
            inserted += max(0, int(cursor.rowcount or 0))
        return inserted

    def _record_current_scope(self, cursor: Any) -> None:
        if not self.project_code:
            return
        cursor.execute(
            """
            INSERT INTO app_config.project_scopes(
                scope_code, display_name, postgres_database, enabled, include_common
            ) VALUES (%s, %s, %s, TRUE, %s)
            ON CONFLICT (scope_code) DO UPDATE SET
                display_name = EXCLUDED.display_name,
                postgres_database = EXCLUDED.postgres_database,
                updated_at = NOW()
            """,
            (
                self.project_code,
                self.project_name or self.project_code,
                self.database or None,
                self.project_code != "COMMON",
            ),
        )

        collection_name = {
            "21MM": "rag_21mm_current_chunks",
            "24MM": "rag_24mm_current_chunks",
            "COMMON": "rag_common_current_chunks",
        }.get(self.project_code)
        if collection_name:
            cursor.execute(
                """
                INSERT INTO app_config.vector_collection_registry(
                    scope_code, collection_name, embedding_profile_id,
                    vector_dimension, distance_metric, payload_contract,
                    payload_contract_version, status
                )
                SELECT %s, %s, id, 1024, 'Cosine',
                       'enterprise-current-chunk', 'v1', 'REGISTERED'
                FROM app_config.embedding_profiles
                WHERE profile_name = 'bge-m3-current'
                ON CONFLICT (collection_name) DO UPDATE SET
                    scope_code = EXCLUDED.scope_code,
                    embedding_profile_id = EXCLUDED.embedding_profile_id,
                    vector_dimension = EXCLUDED.vector_dimension,
                    distance_metric = EXCLUDED.distance_metric,
                    payload_contract = EXCLUDED.payload_contract,
                    payload_contract_version = EXCLUDED.payload_contract_version,
                    updated_at = NOW()
                """,
                (self.project_code, collection_name),
            )

    @staticmethod
    def _record_configuration_version(cursor: Any) -> None:
        # Reuse the existing component-version registry instead of creating a
        # second competing schema-version table.
        cursor.execute(
            """
            INSERT INTO public.schema_version(component, version, name, applied_at)
            VALUES ('configuration', %s, %s, NOW())
            ON CONFLICT (component) DO UPDATE SET
                version = EXCLUDED.version,
                name = EXCLUDED.name,
                applied_at = EXCLUDED.applied_at
            """,
            (CONFIG_SCHEMA_VERSION, CONFIG_SCHEMA_NAME),
        )
