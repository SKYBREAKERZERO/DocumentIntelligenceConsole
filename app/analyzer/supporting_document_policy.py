from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from app.model.document import Document


@dataclass(frozen=True, slots=True)
class SupportingDocumentMatch:
    document_type: str
    matched_text: str
    parent_specification_code: str | None = None
    parent_specification_version: str | None = None


class SupportingDocumentPolicy:
    """Classify non-primary engineering artefacts that accompany a specification.

    Cover/check/test/review sheets often carry the same leading Toyota spec number
    and version as the primary specification. Treating them as a primary
    SpecificationVersion causes a false immutable-version conflict because their
    content is intentionally different. They remain auditable PostgreSQL documents
    but are not allowed to create/replace a primary specification version and are
    excluded from the normal Current Qdrant QA index.
    """

    ROLE = "SUPPORTING"
    QA_INDEX_POLICY = "EXCLUDE"

    _RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
        ("COVER_SHEET", re.compile(r"\bcover\s*sheet\b|\bcoversheet\b|表紙|封面", re.I)),
        ("CHECK_SHEET", re.compile(r"\bcheck\s*sheet\b|\bchecksheet\b|\bcheck\s*list\b|\bchecklist\b|チェック\s*シート|チェックリスト", re.I)),
        ("TEST_SHEET", re.compile(r"\btest\s*sheet\b|\btestsheet\b|テスト\s*シート|試験\s*シート", re.I)),
        ("REVIEW_SHEET", re.compile(r"\breview\s*sheet\b|\breviewsheet\b|レビュー\s*シート", re.I)),
        ("EVIDENCE_SHEET", re.compile(r"\bevidence\s*sheet\b|\bevidencesheet\b|エビデンス\s*シート", re.I)),
        ("REFERENCE_LIST", re.compile(r"\breference\s*list\b|\breferencelist\b|参照\s*一覧|参考\s*一覧", re.I)),
        ("CHANGE_REQUEST", re.compile(r"\bchange\s*request\b|\bchangerequest\b|変更\s*依頼", re.I)),
        ("SUMMARY_SHEET", re.compile(r"\bsummary\s*sheet\b|\bsummarysheet\b|サマリ[ー]?\s*シート", re.I)),
        ("ATTACHMENT", re.compile(r"添付\s*資料|添付資料|添付書類|attachment\s*material", re.I)),
    )
    _SPEC_CODE = re.compile(r"^(?P<code>A?\d{3,4})(?=[_\-\s])", re.I)
    _VERSION = re.compile(
        r"(?<![A-Za-z0-9])(?:v|ver(?:sion)?)[\s._-]*(?P<value>\d+(?:[._-]\d+){1,3}[A-Za-z]?)(?![A-Za-z0-9])",
        re.I,
    )

    @classmethod
    def normalize_name(cls, file_name: str) -> str:
        stem = unicodedata.normalize("NFKC", Path(str(file_name)).stem)
        stem = stem.replace("－", "-").replace("–", "-").replace("—", "-")
        # Delimiters used heavily in engineering filenames must become spaces so
        # word-boundary rules work for e.g. `_CoverSheet` and `[NA]_CheckSheet`.
        return re.sub(r"[_\-\[\](){}]+", " ", stem)

    @classmethod
    def classify(cls, file_name: str) -> SupportingDocumentMatch | None:
        normalized = cls.normalize_name(file_name)
        compact = re.sub(r"\s+", "", normalized)
        combined = f"{normalized} {compact}"
        for document_type, pattern in cls._RULES:
            match = pattern.search(combined)
            if not match:
                continue
            raw_stem = unicodedata.normalize("NFKC", Path(str(file_name)).stem)
            code_match = cls._SPEC_CODE.search(raw_stem)
            version_match = cls._VERSION.search(raw_stem)
            version = None
            if version_match:
                version = "v" + version_match.group("value").replace("_", ".")
            return SupportingDocumentMatch(
                document_type=document_type,
                matched_text=match.group(0),
                parent_specification_code=(code_match.group("code").upper() if code_match else None),
                parent_specification_version=version,
            )
        return None

    @classmethod
    def is_supporting(cls, document_or_metadata: Document | dict | str) -> bool:
        if isinstance(document_or_metadata, Document):
            metadata = document_or_metadata.metadata or {}
            if str(metadata.get("document_role") or "").upper() == cls.ROLE:
                return True
            return cls.classify(document_or_metadata.file_name) is not None
        if isinstance(document_or_metadata, dict):
            return str(document_or_metadata.get("document_role") or "").upper() == cls.ROLE
        return cls.classify(str(document_or_metadata)) is not None

    @classmethod
    def apply(cls, document: Document) -> SupportingDocumentMatch | None:
        match = cls.classify(document.file_name)
        if match is None and not cls.is_supporting(document.metadata or {}):
            return None

        metadata = document.metadata
        if match is not None:
            metadata["supporting_document_type"] = match.document_type
            metadata["supporting_document_match"] = match.matched_text
            if match.parent_specification_code:
                metadata["parent_specification_code"] = match.parent_specification_code
            if match.parent_specification_version:
                metadata["parent_specification_version"] = match.parent_specification_version

        metadata["document_role"] = cls.ROLE
        metadata["evidence_role"] = "METADATA_SUPPORT"
        metadata["qa_index_policy"] = cls.QA_INDEX_POLICY
        metadata["specification_classifier_status"] = "SUPPORTING_DOCUMENT"
        metadata["specification_identity_source"] = "supporting_document_policy"
        metadata["specification_version_status"] = "NOT_APPLICABLE"

        # Important: stale JSON generated by older releases may already contain
        # primary-spec identity fields. Clear them deterministically so a CoverSheet
        # can never collide with the immutable primary SpecificationVersion.
        for key in (
            "specification_code",
            "specification_key",
            "specification_name",
            "specification_version",
            "specification_version_source",
            "specification_rule_id",
        ):
            metadata[key] = None

        # Keep the original file name as the supporting document identity. This is
        # stable on re-import and does not occupy a SpecificationVersion row.
        if not metadata.get("document_id") or "@" in str(metadata.get("document_id") or ""):
            metadata["document_id"] = document.file_name
        return match
