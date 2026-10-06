from __future__ import annotations

from dataclasses import dataclass


class ProjectRegistryError(ValueError):
    """Raised when an unknown or missing processing scope is supplied."""


@dataclass(frozen=True, slots=True)
class ProjectDefinition:
    code: str
    display_name: str
    database_default: str
    uses_specification_taxonomy: bool = True


class ProjectRegistry:
    """
    Canonical processing-scope registry.

    21MM / 24MM are specification projects.
    Common is a generic document-processing scope for ordinary documents such
    as news, meeting material, screenshots, reports and other non-spec content.
    QA/BUG is a dedicated issue-register domain backed by an isolated PostgreSQL
    database and Qdrant collection; it never participates in specification
    taxonomy/version management.

    Assignment is an explicit business decision made by the GUI user.  The
    registry normalizes/validates that decision; it never infers a project from
    file contents.
    """

    PROJECTS: dict[str, ProjectDefinition] = {
        "21MM": ProjectDefinition(
            code="21MM",
            display_name="21MM",
            database_default="rag_21mm",
            uses_specification_taxonomy=True,
        ),
        "24MM": ProjectDefinition(
            code="24MM",
            display_name="24MM",
            database_default="rag_24mm",
            uses_specification_taxonomy=True,
        ),
        "COMMON": ProjectDefinition(
            code="COMMON",
            display_name="Common",
            database_default="rag",
            uses_specification_taxonomy=False,
        ),
        "QABUG": ProjectDefinition(
            code="QABUG",
            display_name="QA/BUG",
            database_default="rag_qabug",
            uses_specification_taxonomy=False,
        ),
    }

    _ALIASES: dict[str, str] = {
        "21MM": "21MM",
        "24MM": "24MM",
        "COMMON": "COMMON",
        "COMMON_DATA": "COMMON",
        "GENERAL": "COMMON",
        "共通": "COMMON",
        "QABUG": "QABUG",
        "QA_BUG": "QABUG",
        "QA/BUG": "QABUG",
        "QA BUG": "QABUG",
        "质量问题": "QABUG",
        "品質問題": "QABUG",
    }

    @classmethod
    def resolve(cls, value: str | None) -> ProjectDefinition:
        if value is None or not str(value).strip():
            raise ProjectRegistryError("Project / scope must be selected.")

        normalized = str(value).strip().upper().replace("-", "_")
        code = cls._ALIASES.get(normalized)
        if code is None:
            supported = ", ".join(
                project.display_name for project in cls.PROJECTS.values()
            )
            raise ProjectRegistryError(
                f"Unsupported project / scope '{value}'. Supported: {supported}."
            )
        return cls.PROJECTS[code]

    @classmethod
    def display_values(cls) -> tuple[str, ...]:
        return tuple(project.display_name for project in cls.PROJECTS.values())
