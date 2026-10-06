from __future__ import annotations

from dataclasses import replace

from app.config.config_loader import DatabaseConfig
from app.storage.project_database_router import ProjectDatabaseRouter


def _config(database: str) -> DatabaseConfig:
    return DatabaseConfig(
        enabled=True,
        host="127.0.0.1",
        port=5432,
        database=database,
        project_databases={
            "21MM": "rag_21mm",
            "24MM": "rag_24mm",
            "COMMON": "rag",
        },
        user="postgres",
        password="test-only",
        password_env="POSTGRES_PASSWORD",
        connect_timeout=10,
    )


def test_runtime_override_wins_over_persisted_config() -> None:
    ProjectDatabaseRouter.clear_runtime_override()
    try:
        explicit = _config("rag_24mm")
        ProjectDatabaseRouter.set_runtime_override("24MM", explicit)
        project, resolved = ProjectDatabaseRouter.resolve_database_config("24MM")
        assert project.code == "24MM"
        assert resolved.database == "rag_24mm"
        assert resolved is explicit
    finally:
        ProjectDatabaseRouter.clear_runtime_override()


def test_runtime_override_is_scope_specific() -> None:
    ProjectDatabaseRouter.clear_runtime_override()
    try:
        explicit = _config("custom_24")
        ProjectDatabaseRouter.set_runtime_override("24MM", explicit)
        assert ProjectDatabaseRouter.get_runtime_override("24MM") is explicit
        assert ProjectDatabaseRouter.get_runtime_override("21MM") is None
    finally:
        ProjectDatabaseRouter.clear_runtime_override()
