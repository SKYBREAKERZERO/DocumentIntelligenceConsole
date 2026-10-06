from __future__ import annotations

import re
from typing import Any

from app.analyzer.supporting_document_policy import SupportingDocumentPolicy


class PrimarySpecificationDocumentPolicy:
    """Single source of truth for primary specification-body selection.

    A specification entity can own several physical documents (body, cover sheet,
    checklist, translation, metadata-only artefacts, etc.).  RAG, relations and
    version comparison must use the specification body.  Supporting artefacts
    remain auditable but must never become the authoritative content source.

    This policy deliberately supports legacy rows whose metadata predates the
    ``document_role`` field by applying the same filename rules used during
    ingestion.
    """

    PRIMARY_ROLE = "PRIMARY"
    PRIMARY_EVIDENCE_ROLE = "PRIMARY_CONTENT"
    SUPPORTING_EVIDENCE_ROLE = "METADATA_SUPPORT"

    NON_PRIMARY_ROLES = frozenset(
        {"SUPPORTING", "METADATA_ONLY", "TRANSLATION", "ISSUE_REGISTER"}
    )
    EXCLUDED_QA_INDEX_POLICIES = frozenset({"EXCLUDE"})

    # PostgreSQL-compatible legacy fallback. Keep this aligned with
    # SupportingDocumentPolicy so existing databases are fixed without reimport.
    SQL_SUPPORTING_FILENAME_PATTERN = (
        r"(cover[ _-]*sheet|coversheet|check[ _-]*sheet|checksheet|check[ _-]*list|"
        r"test[ _-]*sheet|testsheet|review[ _-]*sheet|reviewsheet|"
        r"evidence[ _-]*sheet|evidencesheet|reference[ _-]*list|referencelist|"
        r"change[ _-]*request|changerequest|summary[ _-]*sheet|summarysheet|"
        r"表紙|封面|チェック[ _-]*シート|チェックリスト|テスト[ _-]*シート|"
        r"試験[ _-]*シート|レビュー[ _-]*シート|エビデンス[ _-]*シート|"
        r"参照[ _-]*一覧|参考[ _-]*一覧|変更[ _-]*依頼|サマリ[ー]?[ _-]*シート|"
        r"添付[ _-]*資料|添付資料|添付書類|attachment[ _-]*material)"
    )

    @classmethod
    def is_primary(cls, *, file_name: str = "", metadata: dict[str, Any] | None = None) -> bool:
        metadata = dict(metadata or {})
        role = str(metadata.get("document_role") or cls.PRIMARY_ROLE).strip().upper()
        if role in cls.NON_PRIMARY_ROLES:
            return False
        qa_policy = str(metadata.get("qa_index_policy") or "").strip().upper()
        if qa_policy in cls.EXCLUDED_QA_INDEX_POLICIES:
            return False
        if str(metadata.get("supporting_document_type") or "").strip():
            return False
        return SupportingDocumentPolicy.classify(str(file_name or "")) is None

    @classmethod
    def role_rank(cls, *, file_name: str = "", metadata: dict[str, Any] | None = None) -> int:
        """Return a deterministic evidence priority; higher means more authoritative."""
        if cls.is_primary(file_name=file_name, metadata=metadata):
            return 100
        match = SupportingDocumentPolicy.classify(str(file_name or ""))
        document_type = str(
            (metadata or {}).get("supporting_document_type")
            or (match.document_type if match else "")
        ).upper()
        # Cover sheets are useful metadata, but intentionally below any body.
        if document_type == "COVER_SHEET":
            return 20
        if document_type in {"CHANGE_REQUEST", "REFERENCE_LIST"}:
            return 15
        return 10

    @classmethod
    def evidence_role(cls, *, file_name: str = "", metadata: dict[str, Any] | None = None) -> str:
        return (
            cls.PRIMARY_EVIDENCE_ROLE
            if cls.is_primary(file_name=file_name, metadata=metadata)
            else cls.SUPPORTING_EVIDENCE_ROLE
        )

    @classmethod
    def sql_primary_predicate(cls, alias: str = "d") -> str:
        """Return an internal SQL predicate for a trusted table alias.

        The alias is code-authored only; it is never populated from user input.
        """
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", alias):
            raise ValueError("Unsafe SQL alias")
        roles = ", ".join(f"'{value}'" for value in sorted(cls.NON_PRIMARY_ROLES))
        pattern = cls.SQL_SUPPORTING_FILENAME_PATTERN.replace("'", "''")
        return f"""(
            UPPER(COALESCE({alias}.metadata ->> 'document_role', 'PRIMARY')) NOT IN ({roles})
            AND UPPER(COALESCE({alias}.metadata ->> 'qa_index_policy', 'INCLUDE')) <> 'EXCLUDE'
            AND COALESCE({alias}.metadata ->> 'supporting_document_type', '') = ''
            AND COALESCE({alias}.file_name, '') !~* '{pattern}'
        )"""
