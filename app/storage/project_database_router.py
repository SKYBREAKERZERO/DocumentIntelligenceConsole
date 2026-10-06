from __future__ import annotations

from dataclasses import replace
from threading import RLock

from app.config.config_loader import ConfigLoader, DatabaseConfig
from app.database.connection import DatabaseConnection
from app.project.project_registry import ProjectDefinition, ProjectRegistry


class ProjectDatabaseRoutingError(RuntimeError):
    """Raised when a project cannot be mapped to a PostgreSQL database."""


class ProjectDatabaseRouter:
    """Resolve a user-selected project to its dedicated PostgreSQL database.

    GUI document conversion may provide an explicit runtime DatabaseConfig.
    This keeps the actual PostgresStorage target identical to the database
    currently shown in PostgreSQL Settings, even when the settings have not
    yet been persisted to config.yaml.
    """

    _runtime_lock = RLock()
    _runtime_overrides: dict[str, DatabaseConfig] = {}

    @classmethod
    def set_runtime_override(
        cls,
        project_code: str,
        config: DatabaseConfig,
    ) -> None:
        project = ProjectRegistry.resolve(project_code)
        if not config.database.strip():
            raise ProjectDatabaseRoutingError(
                f"Runtime PostgreSQL database is empty for {project.display_name}."
            )
        with cls._runtime_lock:
            cls._runtime_overrides[project.code] = config

    @classmethod
    def clear_runtime_override(
        cls,
        project_code: str | None = None,
    ) -> None:
        with cls._runtime_lock:
            if project_code is None:
                cls._runtime_overrides.clear()
                return
            project = ProjectRegistry.resolve(project_code)
            cls._runtime_overrides.pop(project.code, None)

    @classmethod
    def get_runtime_override(
        cls,
        project_code: str,
    ) -> DatabaseConfig | None:
        project = ProjectRegistry.resolve(project_code)
        with cls._runtime_lock:
            return cls._runtime_overrides.get(project.code)

    @classmethod
    def resolve_database_config(
        cls,
        project_code: str,
    ) -> tuple[ProjectDefinition, DatabaseConfig]:
        project = ProjectRegistry.resolve(project_code)

        runtime = cls.get_runtime_override(project.code)
        if runtime is not None:
            return project, runtime

        app_config = ConfigLoader.load()
        base = app_config.database

        database_name = base.project_databases.get(project.code)
        if not database_name:
            raise ProjectDatabaseRoutingError(
                f"No PostgreSQL database is configured for project "
                f"{project.display_name} ({project.code})."
            )

        routed = replace(
            base,
            database=database_name,
        )
        return project, routed

    @classmethod
    def create_connection(
        cls,
        project_code: str,
    ) -> tuple[ProjectDefinition, DatabaseConnection]:
        project, config = cls.resolve_database_config(project_code)
        return project, DatabaseConnection(config=config)
