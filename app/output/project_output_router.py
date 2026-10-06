from __future__ import annotations

from pathlib import Path

from app.project.project_registry import ProjectRegistry


class ProjectOutputRouter:
    """
    Resolve JSON output paths for GUI processing.

    Rules:
        - PostgreSQL OFF + JSON ON:
              <output>/<project_code>/<file>.json
        - PostgreSQL ON:
              preserve the existing behavior:
              <output>/<file>.json

    The project folder is intentionally created only for JSON-only mode.
    This keeps the existing PostgreSQL workflow unchanged while preventing
    21MM / 24MM / Common JSON files from being mixed for users without PostgreSQL.
    """

    @classmethod
    def resolve_json_output_path(
        cls,
        *,
        output_directory: str | Path,
        project_code: str,
        file_stem: str,
        save_database: bool,
    ) -> Path:
        root = Path(output_directory).expanduser().resolve()
        project = ProjectRegistry.resolve(project_code)

        normalized_stem = str(file_stem or "").strip()
        if not normalized_stem:
            raise ValueError("file_stem cannot be empty.")

        if save_database:
            target_directory = root
        else:
            target_directory = root / project.code

        return target_directory / f"{normalized_stem}.json"

    @classmethod
    def ensure_json_output_directory(
        cls,
        *,
        output_directory: str | Path,
        project_code: str,
        save_database: bool,
    ) -> Path:
        root = Path(output_directory).expanduser().resolve()
        project = ProjectRegistry.resolve(project_code)

        if save_database:
            root.mkdir(parents=True, exist_ok=True)
            return root

        # JSON-only mode prepares every registered processing-scope bucket. This
        # keeps specification projects and generic Common documents separated
        # even on machines without PostgreSQL.
        for definition in ProjectRegistry.PROJECTS.values():
            (root / definition.code).mkdir(parents=True, exist_ok=True)

        return root / project.code
