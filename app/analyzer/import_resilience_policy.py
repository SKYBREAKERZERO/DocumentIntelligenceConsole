from __future__ import annotations

from app.analyzer.supporting_document_policy import SupportingDocumentPolicy
from app.model.document import Document


class ImportResiliencePolicy:
    """Policy for auditable documents that contain no extractable RAG text.

    Empty/scanned/graphics-only source files are not silently discarded. They are
    persisted as metadata-only audit records, explicitly excluded from the normal
    Current Qdrant index, and marked with a warning code for operator review.
    """

    ROLE_METADATA_ONLY = "METADATA_ONLY"
    ROLE_TRANSLATION = "TRANSLATION"
    ROLE_ISSUE_REGISTER = "ISSUE_REGISTER"
    NON_VERSIONED_ROLES = {"SUPPORTING", ROLE_METADATA_ONLY, ROLE_TRANSLATION, ROLE_ISSUE_REGISTER}

    @classmethod
    def mark_metadata_only(
        cls,
        document: Document,
        *,
        warning_code: str,
        parser_name: str,
        warning_message: str,
    ) -> Document:
        # If the filename itself is a known supporting artefact, keep the more
        # specific SUPPORTING role rather than downgrading it to METADATA_ONLY.
        match = SupportingDocumentPolicy.apply(document)
        if match is None:
            metadata = document.metadata
            metadata["document_role"] = cls.ROLE_METADATA_ONLY
            metadata["qa_index_policy"] = "EXCLUDE"
            metadata["specification_classifier_status"] = "METADATA_ONLY_DOCUMENT"
            metadata["specification_version_status"] = "NOT_APPLICABLE"
        else:
            metadata = document.metadata

        warnings = list(metadata.get("import_warning_codes") or [])
        if warning_code not in warnings:
            warnings.append(warning_code)

        metadata.update(
            {
                "import_status": "IMPORTED_WITH_WARNING",
                "import_warning_codes": warnings,
                "import_warning_message": warning_message,
                "metadata_only": True,
                "parser": parser_name,
                "parser_status": "WARNING_NO_EXTRACTABLE_CONTENT",
                "chapter_count": 0,
                "section_count": 0,
                "content_count": 0,
            }
        )
        document.chapters.clear()
        document.sections.clear()
        document.contents.clear()
        return document

    @classmethod
    def is_non_versioned(cls, document: Document) -> bool:
        role = str((document.metadata or {}).get("document_role") or "PRIMARY").upper()
        return role in cls.NON_VERSIONED_ROLES

    @classmethod
    def detach_primary_identity(cls, document: Document) -> None:
        """Move any stale classifier identity to auditable parent/hint metadata."""
        metadata = document.metadata
        role = str(metadata.get("document_role") or "PRIMARY").upper()
        if role not in cls.NON_VERSIONED_ROLES:
            return

        mapping = {
            "specification_code": "parent_specification_code",
            "specification_key": "parent_specification_key",
            "specification_name": "parent_specification_name",
            "specification_version": "parent_specification_version",
        }
        for source, target in mapping.items():
            value = metadata.get(source)
            if value not in (None, "") and metadata.get(target) in (None, ""):
                metadata[target] = value

        for key in (
            "specification_code",
            "specification_key",
            "specification_name",
            "specification_version",
            "specification_version_source",
            "specification_rule_id",
        ):
            metadata[key] = None

        metadata["specification_version_status"] = "NOT_APPLICABLE"
        metadata["specification_identity_source"] = f"document_role:{role.lower()}"
        # A non-primary artefact must never reuse <specification>@<version> and
        # overwrite the immutable primary document row.
        metadata["document_id"] = document.file_name
