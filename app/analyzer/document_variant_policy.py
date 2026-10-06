from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import Path
from typing import Any

from app.model.document import Document


class DocumentVariantPolicy:
    """Recognize a same-spec/same-version language variant without weakening immutability.

    A real primary version conflict remains an error. Only a clearly signalled
    localization pair (e.g. ``*_en.ppt`` versus the Japanese source) may be stored
    as a non-primary TRANSLATION artefact attached by metadata to the immutable
    primary SpecificationVersion.
    """

    ROLE = "TRANSLATION"
    QA_INDEX_POLICY = "EXCLUDE"

    _LANGUAGE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
        ("EN", re.compile(r"(?:^|[\s_\-\[\(])(?:en|eng|english)(?:$|[\s_\-\]\)])", re.I)),
        ("JA", re.compile(r"(?:^|[\s_\-\[\(])(?:ja|jp|jpn|japanese)(?:$|[\s_\-\]\)])", re.I)),
        ("ZH", re.compile(r"(?:^|[\s_\-\[\(])(?:zh|cn|chs|cht|chinese)(?:$|[\s_\-\]\)])", re.I)),
        ("KO", re.compile(r"(?:^|[\s_\-\[\(])(?:ko|kr|kor|korean)(?:$|[\s_\-\]\)])", re.I)),
    )

    @classmethod
    def detect_language(cls, file_name: str, metadata: dict[str, Any] | None = None) -> str | None:
        metadata = dict(metadata or {})
        for key in ("language_code", "language", "document_language"):
            raw = metadata.get(key)
            if isinstance(raw, str):
                value = raw.strip().upper()
                aliases = {
                    "ENGLISH": "EN", "ENG": "EN",
                    "JAPANESE": "JA", "JP": "JA", "JPN": "JA",
                    "CHINESE": "ZH", "CN": "ZH",
                    "KOREAN": "KO", "KR": "KO",
                }
                if value in {"EN", "JA", "ZH", "KO"}:
                    return value
                if value in aliases:
                    return aliases[value]

        stem = unicodedata.normalize("NFKC", Path(str(file_name)).stem)
        for code, pattern in cls._LANGUAGE_PATTERNS:
            if pattern.search(stem):
                return code

        lower = stem.lower()
        if "[common]" in lower:
            return "EN"
        if "[共通]" in stem:
            return "JA"

        # Kana is a strong Japanese signal. Avoid treating generic CJK ideographs
        # alone as Japanese because Chinese filenames may contain the same codepoints.
        if re.search(r"[ぁ-んァ-ヶ]", stem):
            return "JA"

        # English engineering titles are frequently all-ASCII even without an
        # explicit _en suffix. Require a domain word to avoid over-classification.
        if stem.isascii() and re.search(r"\b(specification|requirement|interface|function|coordination)\b", lower):
            return "EN"

        return None

    @classmethod
    def is_translation_pair(
        cls,
        *,
        existing_file_name: str,
        incoming_file_name: str,
        existing_metadata: dict[str, Any] | None = None,
        incoming_metadata: dict[str, Any] | None = None,
    ) -> bool:
        existing_language = cls.detect_language(existing_file_name, existing_metadata)
        incoming_language = cls.detect_language(incoming_file_name, incoming_metadata)

        # Strongest signal: both sides declare different languages.
        if existing_language and incoming_language and existing_language != incoming_language:
            return True

        # One explicit localization suffix is enough when the primary identity and
        # version were already established by the caller. This handles common
        # Toyota naming such as ``...ver2.51_en.ppt`` paired with a Japanese file.
        if bool(existing_language) ^ bool(incoming_language):
            return True

        # Common/共通 filename pairs are frequently the English/Japanese variants.
        existing = unicodedata.normalize("NFKC", Path(existing_file_name).stem).lower()
        incoming = unicodedata.normalize("NFKC", Path(incoming_file_name).stem).lower()
        common_pair = (
            ("[common]" in existing and "[共通]" in incoming)
            or ("[共通]" in existing and "[common]" in incoming)
        )
        return common_pair

    @classmethod
    def apply_translation_variant(
        cls,
        document: Document,
        *,
        specification_key: str,
        specification_code: str | None,
        specification_name: str | None,
        version_label: str,
        primary_document_id: str,
        primary_file_name: str,
        primary_metadata: dict[str, Any] | None,
        content_hash: str,
    ) -> None:
        metadata = document.metadata
        incoming_language = cls.detect_language(document.file_name, metadata)
        primary_language = cls.detect_language(primary_file_name, primary_metadata)
        if incoming_language is None and primary_language == "EN":
            incoming_language = "JA"
        elif incoming_language is None and primary_language == "JA":
            incoming_language = "EN"
        incoming_language = incoming_language or "UNSPECIFIED"

        metadata.update(
            {
                "document_role": cls.ROLE,
                "qa_index_policy": cls.QA_INDEX_POLICY,
                "translation_language": incoming_language,
                "variant_kind": "LANGUAGE_VARIANT",
                "variant_content_hash": content_hash,
                "variant_of_document_id": primary_document_id,
                "variant_of_file_name": primary_file_name,
                "parent_specification_key": specification_key,
                "parent_specification_code": specification_code,
                "parent_specification_name": specification_name,
                "parent_specification_version": version_label,
                "specification_classifier_status": "TRANSLATION_VARIANT",
                "specification_version_status": "NOT_APPLICABLE",
                "import_status": "IMPORTED_TRANSLATION_VARIANT",
            }
        )

        for key in (
            "specification_code",
            "specification_key",
            "specification_name",
            "specification_version",
            "specification_version_source",
            "specification_rule_id",
        ):
            metadata[key] = None

        digest = hashlib.sha256(document.file_name.encode("utf-8")).hexdigest()[:12]
        metadata["document_id"] = (
            f"{specification_key}@{version_label}#TRANSLATION:{incoming_language}:{digest}"
        )
