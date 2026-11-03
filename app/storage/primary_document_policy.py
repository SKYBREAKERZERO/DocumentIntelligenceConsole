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

    # An explicit unfamiliar value is NOT proof of specification-body status.
    # Missing legacy roles still require the trusted version/document join at
    # each technical evidence path; the filename fallback alone is not proof.
    ALLOWED_PRIMARY_ROLES = frozenset({"PRIMARY", "SPEC_BODY", "PRIMARY_CONTENT"})
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
        if role not in cls.ALLOWED_PRIMARY_ROLES:
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
        roles = ", ".join(f"'{value}'" for value in sorted(cls.ALLOWED_PRIMARY_ROLES))
        pattern = cls.SQL_SUPPORTING_FILENAME_PATTERN.replace("'", "''")
        return f"""(
            UPPER(COALESCE(NULLIF(BTRIM({alias}.metadata ->> 'document_role'), ''), 'PRIMARY')) IN ({roles})
            AND UPPER(COALESCE({alias}.metadata ->> 'qa_index_policy', 'INCLUDE')) <> 'EXCLUDE'
            AND COALESCE({alias}.metadata ->> 'supporting_document_type', '') = ''
            AND COALESCE({alias}.file_name, '') !~* '{pattern}'
        )"""

    @classmethod
    def sql_preferred_body_version_id(cls, specification_alias: str) -> str:
        """Select a MAIN SPEC_BODY version independently of a polluted pointer.

        The normal current pointer is preferred *only when it points to a body*.
        If it points to a Coversheet, choose the latest genuine MAIN body.
        Do not fall back to older extracted units if the preferred body has not
        been indexed: absence of evidence must be visible, never silently stale.
        SQL identifiers are internal literals and validated, not user-supplied.
        """
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", specification_alias):
            raise ValueError("Unsafe SQL alias")
        primary = cls.sql_primary_predicate("body_doc")
        return f"""(
            SELECT body_ver.id
            FROM public.specification_versions body_ver
            JOIN public.documents body_doc ON body_doc.document_id=body_ver.document_id
            WHERE body_ver.specification_id={specification_alias}.id
              AND body_ver.branch_name='MAIN' AND {primary}
            ORDER BY CASE WHEN body_ver.id={specification_alias}.current_version_id
                          THEN 0 ELSE 1 END,
                     body_ver.version_sort_key DESC NULLS LAST,
                     body_ver.version_seq DESC, body_ver.id DESC
            LIMIT 1
        )"""
