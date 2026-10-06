from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from app.config.config_loader import ConfigLoader


class SpecificationTaxonomyError(RuntimeError):
    """Specification taxonomy configuration error."""


@dataclass(frozen=True, slots=True)
class TaxonomySubtype:
    code: str
    parent_type: str
    display_name: str
    keywords: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TaxonomyType:
    code: str
    display_name: str
    keywords: tuple[str, ...]
    subtypes: tuple[TaxonomySubtype, ...]


@dataclass(frozen=True, slots=True)
class ExactSpecificationRule:
    rule_id: str
    region_scope: str
    spec_id: str
    spec_type: str
    spec_subtype: str | None
    name_keywords: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SpecificationTaxonomy:
    version: str
    body_fallback_enabled: bool
    minimum_keyword_length: int
    exact_rule_bonus: int
    subtype_bonus: int
    source_weights: dict[str, int]
    region_aliases: dict[str, tuple[str, ...]]
    spec_types: tuple[TaxonomyType, ...]
    exact_rules: tuple[ExactSpecificationRule, ...]
    source_path: Path

    def type_by_code(self, code: str | None) -> TaxonomyType | None:
        if not code:
            return None
        normalized = str(code).strip().upper()
        return next((item for item in self.spec_types if item.code == normalized), None)

    def subtype_by_code(self, code: str | None) -> TaxonomySubtype | None:
        if not code:
            return None
        normalized = str(code).strip().upper()
        for spec_type in self.spec_types:
            for subtype in spec_type.subtypes:
                if subtype.code == normalized:
                    return subtype
        return None


class SpecificationTaxonomyLoader:
    """
    Load the user-maintainable specification classification dictionary.

    Resolution order:
        1. SPECIFICATION_TAXONOMY_FILE environment variable
        2. <application>/config/specification_taxonomy.yaml
        3. bundled PyInstaller resource/config/specification_taxonomy.yaml

    The external application-directory file intentionally wins in frozen
    builds so engineering teams can update the dictionary without changing
    Python classification code.
    """

    ENV_NAME = "SPECIFICATION_TAXONOMY_FILE"
    RELATIVE_PATH = Path("config") / "specification_taxonomy.yaml"

    _cache: SpecificationTaxonomy | None = None
    _cache_path: Path | None = None
    _cache_mtime_ns: int | None = None

    @classmethod
    def load(
        cls,
        path: str | Path | None = None,
        *,
        force_reload: bool = False,
    ) -> SpecificationTaxonomy:
        resolved = cls.resolve_path(path)
        stat = resolved.stat()

        if (
            not force_reload
            and cls._cache is not None
            and cls._cache_path == resolved
            and cls._cache_mtime_ns == stat.st_mtime_ns
        ):
            return cls._cache

        try:
            raw = yaml.safe_load(resolved.read_text(encoding="utf-8"))
        except Exception as exc:  # pragma: no cover - defensive IO path
            raise SpecificationTaxonomyError(
                f"Failed to read specification taxonomy: {resolved}: {exc}"
            ) from exc

        taxonomy = cls._build(raw, resolved)
        cls._cache = taxonomy
        cls._cache_path = resolved
        cls._cache_mtime_ns = stat.st_mtime_ns
        return taxonomy

    @classmethod
    def resolve_path(cls, path: str | Path | None = None) -> Path:
        candidates: list[Path] = []

        if path is not None:
            candidates.append(Path(path).expanduser())
        else:
            env_value = os.getenv(cls.ENV_NAME)
            if env_value and env_value.strip():
                candidates.append(Path(env_value.strip()).expanduser())

            app_candidate = (
                ConfigLoader.get_application_directory() / cls.RELATIVE_PATH
            )
            resource_candidate = (
                ConfigLoader.get_resource_directory() / cls.RELATIVE_PATH
            )
            candidates.extend([app_candidate, resource_candidate])

        seen: set[Path] = set()
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            if resolved.is_file():
                return resolved

        searched = "\n".join(f"  - {item}" for item in seen)
        raise SpecificationTaxonomyError(
            "Specification taxonomy file was not found. Searched:\n" + searched
        )

    @classmethod
    def _build(cls, raw: Any, source_path: Path) -> SpecificationTaxonomy:
        if not isinstance(raw, dict):
            raise SpecificationTaxonomyError("Taxonomy root must be a mapping.")

        version = str(raw.get("version") or "unknown").strip()
        classification = raw.get("classification") or {}
        if not isinstance(classification, dict):
            raise SpecificationTaxonomyError("classification must be a mapping.")

        source_weights_raw = classification.get("source_weights") or {}
        if not isinstance(source_weights_raw, dict):
            raise SpecificationTaxonomyError("source_weights must be a mapping.")
        source_weights = {
            str(key): int(value)
            for key, value in source_weights_raw.items()
        }

        region_aliases_raw = raw.get("region_aliases") or {}
        if not isinstance(region_aliases_raw, dict):
            raise SpecificationTaxonomyError("region_aliases must be a mapping.")
        region_aliases: dict[str, tuple[str, ...]] = {}
        for region, aliases in region_aliases_raw.items():
            region_code = cls._canonical_code(region, "region code")
            region_aliases[region_code] = cls._string_tuple(aliases)

        spec_types_raw = raw.get("spec_types") or {}
        if not isinstance(spec_types_raw, dict):
            raise SpecificationTaxonomyError("spec_types must be a mapping.")

        spec_types: list[TaxonomyType] = []
        subtype_codes: set[str] = set()
        for type_code_raw, type_data_raw in spec_types_raw.items():
            type_code = cls._canonical_code(type_code_raw, "spec type code")
            type_data = type_data_raw or {}
            if not isinstance(type_data, dict):
                raise SpecificationTaxonomyError(
                    f"spec_types.{type_code} must be a mapping."
                )

            subtypes: list[TaxonomySubtype] = []
            subtypes_raw = type_data.get("subtypes") or {}
            if not isinstance(subtypes_raw, dict):
                raise SpecificationTaxonomyError(
                    f"spec_types.{type_code}.subtypes must be a mapping."
                )

            for subtype_code_raw, subtype_data_raw in subtypes_raw.items():
                subtype_code = cls._canonical_code(
                    subtype_code_raw, "spec subtype code"
                )
                if subtype_code in subtype_codes:
                    raise SpecificationTaxonomyError(
                        "spec subtype codes must be globally unique because "
                        f"the current database master uses code as PK: {subtype_code}"
                    )
                subtype_codes.add(subtype_code)
                subtype_data = subtype_data_raw or {}
                if not isinstance(subtype_data, dict):
                    raise SpecificationTaxonomyError(
                        f"Subtype {subtype_code} must be a mapping."
                    )
                subtypes.append(
                    TaxonomySubtype(
                        code=subtype_code,
                        parent_type=type_code,
                        display_name=str(
                            subtype_data.get("display_name") or subtype_code
                        ).strip(),
                        keywords=cls._string_tuple(subtype_data.get("keywords")),
                    )
                )

            spec_types.append(
                TaxonomyType(
                    code=type_code,
                    display_name=str(type_data.get("display_name") or type_code).strip(),
                    keywords=cls._string_tuple(type_data.get("keywords")),
                    subtypes=tuple(subtypes),
                )
            )

        type_codes = {item.code for item in spec_types}
        exact_rules_raw = raw.get("exact_rules") or []
        if not isinstance(exact_rules_raw, list):
            raise SpecificationTaxonomyError("exact_rules must be a list.")

        exact_rules: list[ExactSpecificationRule] = []
        for index, item_raw in enumerate(exact_rules_raw, start=1):
            if not isinstance(item_raw, dict):
                raise SpecificationTaxonomyError(
                    f"exact_rules[{index}] must be a mapping."
                )
            spec_type = cls._canonical_code(item_raw.get("spec_type"), "spec_type")
            if spec_type not in type_codes:
                raise SpecificationTaxonomyError(
                    f"exact rule references unknown spec_type: {spec_type}"
                )
            spec_subtype_raw = item_raw.get("spec_subtype")
            spec_subtype = (
                cls._canonical_code(spec_subtype_raw, "spec_subtype")
                if spec_subtype_raw
                else None
            )
            if spec_subtype and spec_subtype not in subtype_codes:
                raise SpecificationTaxonomyError(
                    f"exact rule references unknown spec_subtype: {spec_subtype}"
                )
            exact_rules.append(
                ExactSpecificationRule(
                    rule_id=str(item_raw.get("rule_id") or f"rule-{index}").strip(),
                    region_scope=cls._canonical_code(
                        item_raw.get("region_scope"), "region_scope"
                    ),
                    spec_id=str(item_raw.get("spec_id") or "").strip().upper(),
                    spec_type=spec_type,
                    spec_subtype=spec_subtype,
                    name_keywords=cls._string_tuple(item_raw.get("name_keywords")),
                )
            )

        return SpecificationTaxonomy(
            version=version,
            body_fallback_enabled=bool(
                classification.get("body_fallback_enabled", False)
            ),
            minimum_keyword_length=max(
                1, int(classification.get("minimum_keyword_length", 2))
            ),
            exact_rule_bonus=int(classification.get("exact_rule_bonus", 1000)),
            subtype_bonus=int(classification.get("subtype_bonus", 200)),
            source_weights=source_weights,
            region_aliases=region_aliases,
            spec_types=tuple(spec_types),
            exact_rules=tuple(exact_rules),
            source_path=source_path,
        )

    @staticmethod
    def _canonical_code(value: Any, field_name: str) -> str:
        normalized = str(value or "").strip().upper()
        if not normalized:
            raise SpecificationTaxonomyError(f"{field_name} cannot be empty.")
        return normalized

    @staticmethod
    def _string_tuple(value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str):
            values = [value]
        elif isinstance(value, (list, tuple)):
            values = list(value)
        else:
            raise SpecificationTaxonomyError("keyword/alias values must be a list or string.")
        result: list[str] = []
        seen: set[str] = set()
        for item in values:
            text = str(item or "").strip()
            if not text:
                continue
            key = text.casefold()
            if key in seen:
                continue
            seen.add(key)
            result.append(text)
        return tuple(result)
