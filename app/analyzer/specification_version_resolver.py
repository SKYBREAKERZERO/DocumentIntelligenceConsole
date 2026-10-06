from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import Path

from app.model.document import Document
from app.project.project_registry import ProjectRegistry


class SpecificationVersionResolver:
    """Resolve stable specification identity and human version metadata.

    Project/scope remains an explicit GUI decision.  This resolver only runs
    specification identity/version logic for scopes that use the specification
    taxonomy (21MM / 24MM).  Common documents remain unversioned.

    Resolution priority:
        1. metadata overrides
        2. classifier-provided specification code
        3. leading catalog number in the filename
        4. normalized canonical-name fallback

    Version priority:
        1. metadata.version_override / specification_version_override
        2. filename version token (v2.52, Version 7.60, Rev.A, R03 ...)
        3. unresolved (PostgreSQL creates an immutable hash-based fallback)
    """

    _SPEC_ID_PATTERN = re.compile(
        r"^(?P<value>A?\d{3,4})(?=[_\-\s])",
        re.IGNORECASE,
    )

    _VERSION_PATTERNS = (
        re.compile(
            r"(?<![A-Za-z0-9])(?P<value>v\s*\d+(?:[._-]\d+){1,3}[A-Za-z]?)",
            re.IGNORECASE,
        ),
        re.compile(
            r"(?<![A-Za-z0-9])(?:ver(?:sion)?)[\s._-]*(?P<value>\d+(?:[._-]\d+){1,3}[A-Za-z]?)",
            re.IGNORECASE,
        ),
        re.compile(
            r"(?<![A-Za-z0-9])(?P<value>rev(?:ision)?[\s._-]*[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*)",
            re.IGNORECASE,
        ),
        re.compile(
            r"(?<![A-Za-z0-9])(?P<value>R\d{2,4})(?![A-Za-z0-9])",
            re.IGNORECASE,
        ),
    )

    _PROJECT_TOKEN_PATTERN = re.compile(
        r"(?<![A-Za-z0-9])(?:\d{2}(?:MM|CY|UP))(?![A-Za-z0-9])",
        re.IGNORECASE,
    )

    _REGION_TOKEN_PATTERN = re.compile(
        r"(?:\[(?:NA|JP|CN|KR|TW|EU|AU|EXCEPT\s*NA)[^\]]*\]|"
        r"(?<![A-Za-z0-9])(?:EXCEPT\s*NA|NORTH\s*AMERICA|TMNA)(?![A-Za-z0-9]))",
        re.IGNORECASE,
    )

    def __init__(self, project_code: str | None = None) -> None:
        self.project = ProjectRegistry.resolve(project_code) if project_code else None

    def process(self, document: Document) -> Document:
        if document is None:
            raise ValueError("document cannot be None.")
        if not isinstance(document, Document):
            raise TypeError(
                "SpecificationVersionResolver expects an app.model.document.Document instance."
            )

        metadata = document.metadata
        project_code = metadata.get("project_code")
        project = self.project
        if project is None and project_code:
            project = ProjectRegistry.resolve(str(project_code))

        if project is not None and not project.uses_specification_taxonomy:
            self._mark_not_applicable(metadata)
            return document

        spec_code = self._first_nonempty(
            metadata.get("specification_code_override"),
            metadata.get("specification_code"),
            self._extract_spec_code(document.file_name),
        )
        if spec_code:
            spec_code = str(spec_code).strip().upper()

        version_override = self._first_nonempty(
            metadata.get("specification_version_override"),
            metadata.get("version_override"),
        )
        if version_override:
            version_label = self._normalize_version_label(str(version_override))
            version_source = "metadata.version_override"
            version_status = "OVERRIDE"
        else:
            detected = self._extract_version(document.file_name)
            if detected:
                version_label = detected
                version_source = "file_name"
                version_status = "DETECTED"
            else:
                version_label = None
                version_source = None
                version_status = "UNRESOLVED"

        canonical_override = self._first_nonempty(
            metadata.get("specification_name_override"),
            metadata.get("canonical_name_override"),
        )
        canonical_name = (
            str(canonical_override).strip()
            if canonical_override
            else self._derive_canonical_name(
                document.file_name,
                spec_code=spec_code,
                version_label=version_label,
            )
        )

        project_identity = (
            project.code
            if project is not None
            else str(project_code or "SPEC").strip().upper() or "SPEC"
        )
        region = str(metadata.get("region_scope") or "UNSPECIFIED").strip().upper()

        if spec_code:
            specification_key = f"{project_identity}:{region}:{spec_code}"
            identity_source = "catalog_code"
        else:
            digest_source = self._normalize_identity_text(canonical_name)
            digest = hashlib.sha256(digest_source.encode("utf-8")).hexdigest()[:20]
            specification_key = f"{project_identity}:{region}:NAME:{digest}"
            identity_source = "canonical_name_hash"

        metadata["specification_code"] = spec_code
        metadata["specification_key"] = specification_key
        metadata["specification_name"] = canonical_name
        metadata["specification_identity_source"] = identity_source
        metadata["specification_version"] = version_label
        metadata["specification_version_source"] = version_source
        metadata["specification_version_status"] = version_status

        # Keep the legacy document.version field compatible with the new model.
        if version_label:
            metadata["version"] = version_label

        # A version-aware document_id makes normal versioned imports immutable:
        # v2.52 and v2.53 cannot overwrite each other even when filenames are
        # otherwise similar.  Explicit document_id remains authoritative.
        if version_label and not metadata.get("document_id"):
            metadata["document_id"] = f"{specification_key}@{version_label}"

        return document

    @staticmethod
    def _mark_not_applicable(metadata: dict[str, object]) -> None:
        metadata["specification_code"] = None
        metadata["specification_key"] = None
        metadata["specification_name"] = None
        metadata["specification_identity_source"] = "project.COMMON"
        metadata["specification_version"] = None
        metadata["specification_version_source"] = None
        metadata["specification_version_status"] = "NOT_APPLICABLE"

    @classmethod
    def _extract_spec_code(cls, file_name: str) -> str | None:
        stem = Path(str(file_name)).stem.strip()
        match = cls._SPEC_ID_PATTERN.search(stem)
        return match.group("value").upper() if match else None

    @classmethod
    def _extract_version(cls, file_name: str) -> str | None:
        stem = unicodedata.normalize("NFKC", Path(str(file_name)).stem)
        for pattern in cls._VERSION_PATTERNS:
            match = pattern.search(stem)
            if match:
                return cls._normalize_version_label(match.group("value"))
        return None

    @staticmethod
    def _normalize_version_label(value: str) -> str:
        value = unicodedata.normalize("NFKC", value).strip()
        value = re.sub(r"\s+", "", value)
        value = value.replace("_", ".")
        # Keep semantic labels readable while avoiding accidental whitespace.
        if re.fullmatch(r"v\d+(?:[.-]\d+)+[A-Za-z]?", value, re.IGNORECASE):
            return "v" + value[1:]
        if re.fullmatch(r"\d+(?:[.-]\d+)+[A-Za-z]?", value):
            return "v" + value
        if value.lower().startswith("revision"):
            return "Rev" + value[len("revision") :].lstrip(".-")
        if value.lower().startswith("rev"):
            return "Rev" + value[3:].lstrip(".-")
        return value

    @classmethod
    def _derive_canonical_name(
        cls,
        file_name: str,
        *,
        spec_code: str | None,
        version_label: str | None,
    ) -> str:
        text = unicodedata.normalize("NFKC", Path(str(file_name)).stem)

        if spec_code:
            text = re.sub(
                rf"^\s*{re.escape(spec_code)}(?=[_\-\s])[_\-\s]*",
                "",
                text,
                count=1,
                flags=re.IGNORECASE,
            )

        text = cls._PROJECT_TOKEN_PATTERN.sub(" ", text)
        text = cls._REGION_TOKEN_PATTERN.sub(" ", text)

        # Remove a detected version using all supported patterns rather than
        # relying on the normalized label's exact punctuation.
        if version_label:
            for pattern in cls._VERSION_PATTERNS:
                text = pattern.sub(" ", text)

        text = re.sub(r"\[(?:NA|JP|CN|KR|TW|EU|AU)[^\]]*\]", " ", text, flags=re.I)
        text = text.replace("[TOYOTA]", " ")
        text = re.sub(r"[_\-]+", " ", text)
        text = re.sub(r"\s+", " ", text).strip(" ._-[]()")

        return text or Path(str(file_name)).stem.strip()

    @staticmethod
    def _normalize_identity_text(value: str) -> str:
        value = unicodedata.normalize("NFKC", str(value)).upper()
        value = re.sub(r"[^A-Z0-9一-龥ぁ-んァ-ヶ]+", " ", value)
        return re.sub(r"\s+", " ", value).strip()

    @staticmethod
    def _first_nonempty(*values: object) -> object | None:
        for value in values:
            if value is not None and str(value).strip():
                return value
        return None
