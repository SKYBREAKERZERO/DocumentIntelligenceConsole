from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable

from app.analyzer.specification_taxonomy import (
    ExactSpecificationRule,
    SpecificationTaxonomy,
    SpecificationTaxonomyLoader,
    TaxonomySubtype,
    TaxonomyType,
)
from app.model.document import Document
from app.analyzer.specification_version_resolver import SpecificationVersionResolver
from app.analyzer.supporting_document_policy import SupportingDocumentPolicy
from app.analyzer.issue_register_policy import IssueRegisterPolicy
from app.project.project_registry import ProjectRegistry


@dataclass(frozen=True, slots=True)
class ClassificationMatch:
    """Auditable classifier result."""

    value: str | None
    source: str | None
    matched_text: str | None
    confidence: float
    status: str
    rule_id: str | None = None

    def to_metadata(self) -> dict[str, object]:
        return {
            "value": self.value,
            "source": self.source,
            "matched_text": self.matched_text,
            "confidence": self.confidence,
            "status": self.status,
            "rule_id": self.rule_id,
        }


@dataclass(frozen=True, slots=True)
class _Evidence:
    source: str
    text: str
    weight: int
    confidence: float


@dataclass(frozen=True, slots=True)
class _ScoredCandidate:
    value: str
    source: str
    matched_text: str
    score: int
    confidence: float
    rule_id: str | None = None


@dataclass(frozen=True, slots=True)
class _ExactResult:
    rule: ExactSpecificationRule
    source: str
    matched_text: str


class SpecificationClassifier:
    """
    Dictionary-driven specification classifier.

    Processing-scope assignment remains authoritative GUI input:
        21MM / 24MM / Common

    Common mode is intentionally not classified as a specification.  It keeps
    the same document/chapter/section/content/JSON/PostgreSQL structure while
    leaving series/region/spec_type/spec_subtype empty.

    Specification identity is loaded from:
        config/specification_taxonomy.yaml

    Classification order:
        1. explicit metadata overrides
        2. exact rule: region + spec number + official-name keyword
        3. compound subtype keyword (e.g. Bluetooth Audio, Navigation HMI)
        4. broad type keyword

    Body text is disabled by default in the taxonomy because references to
    other specifications must not redefine the current document identity.
    """

    CLASSIFIER_NAME = "SpecificationClassifier"

    _SERIES_PATTERN = re.compile(
        r"(?<![A-Z0-9])(?P<value>\d{2}(?:MM|UP|CY))",
        re.IGNORECASE,
    )

    _SPEC_ID_PATTERN = re.compile(
        r"^(?P<value>A?\d{3,4})(?=[_\-\s])",
        re.IGNORECASE,
    )

    def __init__(
        self,
        project_code: str | None = None,
        *,
        taxonomy_path: str | None = None,
    ) -> None:
        self.project = (
            ProjectRegistry.resolve(project_code)
            if project_code is not None
            else None
        )
        self.taxonomy: SpecificationTaxonomy = SpecificationTaxonomyLoader.load(
            taxonomy_path
        )
        self.version_resolver = SpecificationVersionResolver(project_code=project_code)

    def process(self, document: Document) -> Document:
        if document is None:
            raise ValueError("document cannot be None.")
        if not isinstance(document, Document):
            raise TypeError(
                "SpecificationClassifier expects an app.model.document.Document instance."
            )

        metadata = document.metadata

        if self.project is not None:
            metadata["project_code"] = self.project.code
            metadata["project_name"] = self.project.display_name
            metadata["project_assignment_source"] = "USER_SELECTED"

            if not self.project.uses_specification_taxonomy:
                return self._mark_common_document(document)

        # Jira/QA/BUG registers are engineering artefacts, not immutable
        # specification masters. XLSXParser marks them from their table headers;
        # preserve that role before filename-based specification classification.
        if str(metadata.get("artifact_type") or "").upper() == IssueRegisterPolicy.ARTIFACT_TYPE:
            IssueRegisterPolicy.apply_document(
                document,
                fields=metadata.get("issue_register_fields") or (),
                confidence=float(metadata.get("issue_register_confidence") or 1.0),
            )
            return document

        # Supporting artefacts such as CoverSheet / CheckSheet / TestSheet can
        # legitimately share the parent specification number and version while
        # having different content. They are documents, not primary versions.
        # Classify them before normal specification identity/version resolution.
        if SupportingDocumentPolicy.classify(document.file_name) is not None:
            SupportingDocumentPolicy.apply(document)
            return document

        series_override = self._normalize_override(metadata.get("series_override"))
        region_override = self._normalize_override(metadata.get("region_scope_override"))
        spec_type_override = self._normalize_override(metadata.get("spec_type_override"))
        spec_subtype_override = self._normalize_override(
            metadata.get("spec_subtype_override")
        )

        evidence = tuple(self._build_evidence(document))

        series = (
            ClassificationMatch(
                value=series_override,
                source="metadata.series_override",
                matched_text=series_override,
                confidence=1.0,
                status="OVERRIDE",
            )
            if series_override
            else self._classify_series(evidence)
        )

        explicit_region = (
            ClassificationMatch(
                value=region_override,
                source="metadata.region_scope_override",
                matched_text=region_override,
                confidence=1.0,
                status="OVERRIDE",
            )
            if region_override
            else self._classify_region(evidence)
        )

        exact_result = self._match_exact_rule(
            document=document,
            evidence=evidence,
            explicit_region=explicit_region.value,
        )

        if spec_subtype_override:
            subtype_parent = self.taxonomy.subtype_by_code(spec_subtype_override)
            spec_subtype = ClassificationMatch(
                value=spec_subtype_override,
                source="metadata.spec_subtype_override",
                matched_text=spec_subtype_override,
                confidence=1.0,
                status="OVERRIDE",
            )
            parent_from_subtype = (
                subtype_parent.parent_type if subtype_parent is not None else None
            )
        elif exact_result and exact_result.rule.spec_subtype:
            spec_subtype = ClassificationMatch(
                value=exact_result.rule.spec_subtype,
                source=exact_result.source,
                matched_text=exact_result.matched_text,
                confidence=1.0,
                status="EXACT_RULE",
                rule_id=exact_result.rule.rule_id,
            )
            parent_from_subtype = exact_result.rule.spec_type
        else:
            spec_subtype, parent_from_subtype = self._classify_subtype(evidence)

        if spec_type_override:
            spec_type = ClassificationMatch(
                value=spec_type_override,
                source="metadata.spec_type_override",
                matched_text=spec_type_override,
                confidence=1.0,
                status="OVERRIDE",
            )
        elif exact_result:
            spec_type = ClassificationMatch(
                value=exact_result.rule.spec_type,
                source=exact_result.source,
                matched_text=exact_result.matched_text,
                confidence=1.0,
                status="EXACT_RULE",
                rule_id=exact_result.rule.rule_id,
            )
        elif parent_from_subtype:
            spec_type = ClassificationMatch(
                value=parent_from_subtype,
                source=spec_subtype.source,
                matched_text=spec_subtype.matched_text,
                confidence=spec_subtype.confidence,
                status="DERIVED_FROM_SUBTYPE",
                rule_id=spec_subtype.rule_id,
            )
        else:
            spec_type = self._classify_type(evidence)

        if region_override:
            region = explicit_region
        elif exact_result:
            region = ClassificationMatch(
                value=exact_result.rule.region_scope,
                source=exact_result.source,
                matched_text=exact_result.matched_text,
                confidence=1.0,
                status="EXACT_RULE",
                rule_id=exact_result.rule.rule_id,
            )
        else:
            region = explicit_region

        metadata["series"] = series.value
        metadata["region_scope"] = region.value
        metadata["spec_type"] = spec_type.value
        metadata["spec_subtype"] = spec_subtype.value
        metadata["specification_code"] = (
            exact_result.rule.spec_id
            if exact_result is not None
            else self._extract_spec_id(document.file_name)
        )
        metadata["specification_rule_id"] = (
            exact_result.rule.rule_id if exact_result is not None else None
        )
        metadata["specification_classifier"] = self.CLASSIFIER_NAME
        metadata["specification_classifier_version"] = self.taxonomy.version
        metadata["specification_taxonomy_version"] = self.taxonomy.version
        metadata["specification_classifier_status"] = self._overall_status(
            series=series,
            spec_type=spec_type,
        )
        metadata["specification_classification"] = {
            "series": series.to_metadata(),
            "region_scope": region.to_metadata(),
            "spec_type": spec_type.to_metadata(),
            "spec_subtype": spec_subtype.to_metadata(),
        }

        return self.version_resolver.process(document)

    def _mark_common_document(self, document: Document) -> Document:
        """Mark a generic/non-spec document without applying the spec taxonomy."""

        metadata = document.metadata
        not_applicable = ClassificationMatch(
            value=None,
            source="project.COMMON",
            matched_text=None,
            confidence=1.0,
            status="NOT_APPLICABLE",
        )

        metadata["series"] = None
        metadata["region_scope"] = None
        metadata["spec_type"] = None
        metadata["spec_subtype"] = None
        metadata["specification_classifier"] = self.CLASSIFIER_NAME
        metadata["specification_classifier_version"] = self.taxonomy.version
        metadata["specification_taxonomy_version"] = self.taxonomy.version
        metadata["specification_classifier_status"] = "NOT_APPLICABLE"
        metadata["specification_classification"] = {
            "series": not_applicable.to_metadata(),
            "region_scope": not_applicable.to_metadata(),
            "spec_type": not_applicable.to_metadata(),
            "spec_subtype": not_applicable.to_metadata(),
        }
        return self.version_resolver.process(document)

    def _classify_series(self, evidence: tuple[_Evidence, ...]) -> ClassificationMatch:
        candidates: list[_ScoredCandidate] = []
        for item in evidence:
            for match in self._SERIES_PATTERN.finditer(item.text):
                candidates.append(
                    _ScoredCandidate(
                        value=str(match.group("value")).upper(),
                        source=item.source,
                        matched_text=match.group(0),
                        score=item.weight,
                        confidence=item.confidence,
                    )
                )
        return self._resolve_scored(candidates)

    def _classify_region(self, evidence: tuple[_Evidence, ...]) -> ClassificationMatch:
        candidates: list[_ScoredCandidate] = []
        for item in evidence:
            raw = self._normalize_text(item.text)
            for region_code, aliases in self.taxonomy.region_aliases.items():
                for alias in aliases:
                    if self._contains_raw_alias(raw, alias):
                        candidates.append(
                            _ScoredCandidate(
                                value=region_code,
                                source=item.source,
                                matched_text=alias,
                                score=item.weight + len(alias),
                                confidence=item.confidence,
                            )
                        )
        return self._resolve_scored(candidates)

    def _match_exact_rule(
        self,
        *,
        document: Document,
        evidence: tuple[_Evidence, ...],
        explicit_region: str | None,
    ) -> _ExactResult | None:
        spec_id = self._extract_spec_id(document.file_name)
        if not spec_id:
            return None

        rules = [
            rule
            for rule in self.taxonomy.exact_rules
            if rule.spec_id == spec_id
        ]
        if not rules:
            return None

        filename = self._normalize_for_match(document.file_name)
        scored: list[tuple[int, ExactSpecificationRule, str]] = []

        for rule in rules:
            if explicit_region and not self._region_compatible(
                explicit_region, rule.region_scope
            ):
                continue

            matched_keyword = ""
            keyword_score = 0
            for keyword in rule.name_keywords:
                if self._contains_keyword(filename, keyword):
                    normalized_keyword = self._normalize_for_match(keyword)
                    score = len(normalized_keyword)
                    if score > keyword_score:
                        keyword_score = score
                        matched_keyword = keyword

            # With no explicit region, number-only matching is unsafe because
            # the same specification number can mean different documents in
            # NA and Except-NA catalogs (e.g. 555 / 705).
            if not explicit_region and not matched_keyword:
                continue

            score = self.taxonomy.exact_rule_bonus + keyword_score
            if explicit_region == rule.region_scope:
                score += 500
            scored.append((score, rule, matched_keyword or spec_id))

        if not scored:
            return None

        scored.sort(key=lambda item: item[0], reverse=True)
        highest = scored[0][0]
        strongest = [item for item in scored if item[0] == highest]

        identities = {
            (item[1].spec_type, item[1].spec_subtype, item[1].region_scope)
            for item in strongest
        }
        if len(identities) != 1:
            return None

        _, rule, matched = strongest[0]
        return _ExactResult(
            rule=rule,
            source="taxonomy.exact_rule",
            matched_text=matched,
        )

    def _classify_subtype(
        self,
        evidence: tuple[_Evidence, ...],
    ) -> tuple[ClassificationMatch, str | None]:
        candidates: list[_ScoredCandidate] = []
        parent_by_subtype: dict[str, str] = {}

        for spec_type in self.taxonomy.spec_types:
            for subtype in spec_type.subtypes:
                parent_by_subtype[subtype.code] = subtype.parent_type
                candidates.extend(
                    self._keyword_candidates(
                        value=subtype.code,
                        keywords=subtype.keywords,
                        evidence=evidence,
                        bonus=self.taxonomy.subtype_bonus,
                    )
                )

        result = self._resolve_scored(candidates)
        return result, parent_by_subtype.get(result.value or "")

    def _classify_type(self, evidence: tuple[_Evidence, ...]) -> ClassificationMatch:
        candidates: list[_ScoredCandidate] = []
        for spec_type in self.taxonomy.spec_types:
            candidates.extend(
                self._keyword_candidates(
                    value=spec_type.code,
                    keywords=spec_type.keywords,
                    evidence=evidence,
                    bonus=0,
                )
            )
        return self._resolve_scored(candidates)

    def _keyword_candidates(
        self,
        *,
        value: str,
        keywords: tuple[str, ...],
        evidence: tuple[_Evidence, ...],
        bonus: int,
    ) -> list[_ScoredCandidate]:
        result: list[_ScoredCandidate] = []
        for item in evidence:
            normalized_text = self._normalize_for_match(item.text)
            for keyword in keywords:
                normalized_keyword = self._normalize_for_match(keyword)
                if len(normalized_keyword.replace(" ", "")) < self.taxonomy.minimum_keyword_length:
                    continue
                if not self._contains_normalized(normalized_text, normalized_keyword):
                    continue
                # Long compound identities should outrank broad tokens, but
                # short canonical codes (BT / HMI / DTV) must remain peers.
                # Otherwise a one-character length difference would turn a
                # real filename conflict into a false winner.
                specificity = (
                    min(len(normalized_keyword), 80)
                    if (" " in normalized_keyword or len(normalized_keyword) > 4)
                    else 0
                )
                result.append(
                    _ScoredCandidate(
                        value=value,
                        source=item.source,
                        matched_text=keyword,
                        score=item.weight + bonus + specificity,
                        confidence=item.confidence,
                    )
                )
        return result

    @staticmethod
    def _resolve_scored(candidates: list[_ScoredCandidate]) -> ClassificationMatch:
        if not candidates:
            return ClassificationMatch(
                value=None,
                source=None,
                matched_text=None,
                confidence=0.0,
                status="UNRESOLVED",
            )

        best_by_value: dict[str, _ScoredCandidate] = {}
        for item in candidates:
            previous = best_by_value.get(item.value)
            if previous is None or item.score > previous.score:
                best_by_value[item.value] = item

        highest_score = max(item.score for item in best_by_value.values())
        strongest = [
            item for item in best_by_value.values() if item.score == highest_score
        ]

        if len(strongest) != 1:
            return ClassificationMatch(
                value=None,
                source=strongest[0].source,
                matched_text=None,
                confidence=0.0,
                status="AMBIGUOUS",
            )

        item = strongest[0]
        return ClassificationMatch(
            value=item.value,
            source=item.source,
            matched_text=item.matched_text,
            confidence=item.confidence,
            status="MATCHED",
            rule_id=item.rule_id,
        )

    def _build_evidence(self, document: Document) -> Iterable[_Evidence]:
        weights = self.taxonomy.source_weights

        yield _Evidence(
            source="file_name",
            text=self._normalize_text(document.file_name),
            weight=int(weights.get("file_name", 300)),
            confidence=1.0,
        )

        metadata = document.metadata or {}
        for key in ("specification_title", "document_title", "title"):
            value = metadata.get(key)
            if not self._has_text(value):
                continue
            source = f"metadata.{key}"
            yield _Evidence(
                source=source,
                text=self._normalize_text(value),
                weight=int(weights.get(source, 240)),
                confidence=0.95,
            )

        chapter_weight = int(weights.get("chapter.title", 80))
        for chapter in document.chapters[:10]:
            for field_name in ("title_jp", "title_en"):
                value = getattr(chapter, field_name, None)
                if self._has_text(value):
                    yield _Evidence(
                        source=f"chapter.{field_name}",
                        text=self._normalize_text(value),
                        weight=chapter_weight,
                        confidence=0.75,
                    )

        section_weight = int(weights.get("section.title", 60))
        for section in document.sections[:20]:
            for field_name in ("title_jp", "title_en"):
                value = getattr(section, field_name, None)
                if self._has_text(value):
                    yield _Evidence(
                        source=f"section.{field_name}",
                        text=self._normalize_text(value),
                        weight=section_weight,
                        confidence=0.65,
                    )

        if self.taxonomy.body_fallback_enabled:
            body_weight = int(weights.get("content.text", 5))
            for content in document.contents[:10]:
                value = getattr(content, "text", None)
                if self._has_text(value):
                    yield _Evidence(
                        source="content.text",
                        text=self._normalize_text(str(value)[:1500]),
                        weight=body_weight,
                        confidence=0.35,
                    )

    @classmethod
    def _extract_spec_id(cls, file_name: str) -> str | None:
        normalized = unicodedata.normalize("NFKC", str(file_name or "")).strip()
        match = cls._SPEC_ID_PATTERN.match(normalized)
        if match is None:
            return None
        return str(match.group("value")).upper()

    @staticmethod
    def _region_compatible(explicit_region: str, rule_region: str) -> bool:
        if explicit_region == rule_region:
            return True
        explicit_parts = set(explicit_region.split("_"))
        rule_parts = set(rule_region.split("_"))
        return bool(explicit_parts & rule_parts) and rule_region not in {"NA", "EXCEPT_NA"}

    @classmethod
    def _contains_keyword(cls, text: str, keyword: str) -> bool:
        return cls._contains_normalized(
            cls._normalize_for_match(text),
            cls._normalize_for_match(keyword),
        )

    @staticmethod
    def _contains_normalized(text: str, keyword: str) -> bool:
        if not text or not keyword:
            return False
        if len(keyword.replace(" ", "")) <= 4:
            return f" {keyword} " in f" {text} "
        return keyword in text

    @staticmethod
    def _contains_raw_alias(text: str, alias: str) -> bool:
        normalized_text = unicodedata.normalize("NFKC", str(text or "")).casefold()
        normalized_alias = unicodedata.normalize("NFKC", str(alias or "")).casefold()
        return bool(normalized_alias) and normalized_alias in normalized_text

    @staticmethod
    def _normalize_override(value: object) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip().upper()
        return normalized or None

    @staticmethod
    def _normalize_text(value: object) -> str:
        text = unicodedata.normalize("NFKC", str(value or ""))
        return text.replace("\u3000", " ")

    @staticmethod
    def _normalize_for_match(value: object) -> str:
        text = unicodedata.normalize("NFKC", str(value or "")).casefold()
        text = re.sub(r"[\s_\-./\\\[\](){}:;,+]+", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    @staticmethod
    def _has_text(value: object) -> bool:
        return value is not None and bool(str(value).strip())

    @staticmethod
    def _overall_status(
        *,
        series: ClassificationMatch,
        spec_type: ClassificationMatch,
    ) -> str:
        if spec_type.value:
            return "SUCCESS" if series.value else "PARTIAL"
        if spec_type.status == "AMBIGUOUS":
            return "AMBIGUOUS"
        return "UNRESOLVED"
