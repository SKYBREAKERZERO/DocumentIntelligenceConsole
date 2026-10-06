from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable

from app.relations.policy import EngineeringRelationPolicy, EngineeringRelationPolicyLoader


EXTRACTOR_VERSION = "engineering-relations-v5-toyota-dependency-graph"


@dataclass(frozen=True, slots=True)
class KnownSpecification:
    specification_id: int
    specification_key: str
    canonical_name: str
    spec_code: str | None = None
    project_code: str | None = None
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ExtractionUnit:
    content_id: int
    text: str
    page_number: int | None = None
    chapter_id: str | None = None
    chapter_title: str | None = None
    section_id: str | None = None
    section_title: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    section_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ExtractionEvidence:
    content_id: int
    page_number: int | None
    chapter_id: str | None
    chapter_title: str | None
    section_id: str | None
    section_title: str | None
    sheet_name: str | None
    cell_range: str | None
    evidence_text: str
    start_offset: int | None
    end_offset: int | None
    evidence_hash: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ExtractedEntity:
    entity_type: str
    entity_key: str
    canonical_name: str
    normalized_value: str | None
    extraction_method: str
    confidence: float
    status: str = "CONFIRMED"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ExtractedRelation:
    relation_key: str
    relation_type: str
    extraction_method: str
    confidence: float
    status: str
    is_explicit: bool
    source_entity_key: str | None = None
    target_entity_key: str | None = None
    target_specification_id: int | None = None
    target_external_key: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    evidence: list[ExtractionEvidence] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class RelationExtractionResult:
    entities: tuple[ExtractedEntity, ...]
    relations: tuple[ExtractedRelation, ...]
    unresolved_count: int
    stats: dict[str, Any]


class EngineeringRelationExtractor:
    """Deterministic pre-Qdrant engineering-relation extractor.

    v4 specializes deterministic cross-specification extraction for Toyota-style
    specifications without vector search or LLM authority. It treats Toyota
    Specification No. as the primary identity, keeps title-only references as
    secondary audit candidates, and preserves unresolved references for later
    registry reconciliation.

    Authoritative graph facts are therefore explainable from PostgreSQL source
    data plus exact evidence chunks.  Semantic/Qdrant retrieval remains optional
    and is intentionally not used by this extractor.
    """

    def __init__(self, policy: EngineeringRelationPolicy | None = None) -> None:
        self.policy = policy or EngineeringRelationPolicyLoader.load()

    _REQ_PATTERNS = (
        re.compile(r"\b(?P<id>REQ[-_:][A-Z0-9][A-Z0-9._:-]{2,})\b", re.I),
        re.compile(
            r"(?:Requirement(?:\s+ID)?|要求\s*ID|要件\s*ID)\s*[:：#=]?\s*"
            r"(?P<id>(?:REQ[-_:]?)?[A-Z0-9][A-Z0-9._:-]{2,})",
            re.I,
        ),
    )
    _SIGNAL_PATTERN = re.compile(
        r"(?:CAN\s+Signal|LIN\s+Signal|Signal(?:\s+(?:Name|ID))?|信号名|シグナル(?:名)?)"
        r"\s*[:：#=]\s*(?P<name>[A-Za-z_][A-Za-z0-9_.:/-]{2,})",
        re.I,
    )
    _INTERFACE_PATTERN = re.compile(
        r"(?:Service\s+Interface|Interface(?:\s+(?:Name|ID))?|I/F|インターフェース(?:名)?|API(?:\s+Name)?)"
        r"\s*[:：#=]\s*(?P<name>[A-Za-z_][A-Za-z0-9_.:/-]{2,})",
        re.I,
    )
    _FUNCTION_PATTERN = re.compile(
        r"(?:Function(?:\s+(?:Name|ID))?|機能名)\s*[:：#=]\s*(?P<name>[A-Za-z0-9_][A-Za-z0-9_.:/ -]{2,80})",
        re.I,
    )
    _ECU_PATTERN = re.compile(
        r"(?:Source\s+ECU|Destination\s+ECU|ECU(?:\s+Name)?|ECU名)\s*[:：#=]\s*(?P<name>[A-Za-z0-9_][A-Za-z0-9_.:/-]{1,40})",
        re.I,
    )
    _PROTOCOL_PATTERN = re.compile(
        r"(?:Protocol|通信プロトコル|プロトコル)\s*[:：#=]\s*(?P<name>[A-Za-z0-9_][A-Za-z0-9_.+:/-]{1,40})",
        re.I,
    )

    # Explicit labels are deliberately multilingual.  Numeric-only catalog
    # codes are accepted only when accompanied by one of these labels/cues or
    # when the content is inside a recognized reference-document section.
    _SPEC_TOKEN_PATTERN = re.compile(
        r"(?:Specification|Spec(?:ification)?(?:\s+(?:No\.?|ID|Code))?|"
        r"Document(?:\s+(?:No\.?|ID|Code))?|"
        r"仕様書?|式様書?|式样书?|規格書?|文書(?:番号|No\.?|ID)|文件(?:编号|号))"
        r"(?:\s*[:：#=\-]\s*|\s+)"
        r"(?P<token>(?=[A-Za-z0-9_.:/\-]*[0-9])"
        r"[A-Za-z0-9][A-Za-z0-9_.:/\-]{1,40})",
        re.I,
    )
    _REFERENCE_CUE_PATTERN = re.compile(
        r"(?:refer(?:ence|red)?\s+(?:to|by)|according\s+to|comply(?:\s+with)?|see\s+also|see\s+|"
        r"based\s+on|defined\s+in|specified\s+in|as\s+specified\s+in|"
        r"参照|準拠|従う|基づく|依拠|適用|引用|関連文書|適用文書|参照文書|"
        r"依据|依照|参照|引用|适用|相关文档|关联文档|参考文件)",
        re.I,
    )
    _REFERENCE_SECTION_PATTERN = re.compile(
        r"(?:references?|referenced\s+documents?|applicable\s+documents?|related\s+documents?|relevant\s+specifications?|relevant\s+documents?|"
        r"normative\s+references?|参照文書|適用文書|関連文書|引用文書|参考文献|参照仕様書|"
        r"引用文档|适用文档|相关文档|关联文档|参考文件|引用式样书|适用式样书)",
        re.I,
    )
    _QUOTED_TITLE_PATTERN = re.compile(
        r"(?:《(?P<cjk>[^》]{3,160})》|「(?P<jp>[^」]{3,160})」|『(?P<jp2>[^』]{3,160})』|"
        r"\"(?P<dq>[^\"]{4,160})\")"
    )
    _STANDARD_PATTERN = re.compile(
        r"\b(?:ISO|IEC|IEEE|JIS|SAE|DIN|EN|ASTM)\s*[-:]?\s*[A-Z]?\s*\d{2,6}(?:[-:]\d+)*(?::\d{4})?\b",
        re.I,
    )
    _INTERNAL_ONLY_PATTERN = re.compile(
        r"(?:本書|本仕様書|本式样书|本規格|this\s+(?:document|specification)|"
        r"(?:section|chapter)\s+\d|第\s*\d+\s*[章节節章])",
        re.I,
    )
    _NEGATIVE_REFERENCE_PATTERN = re.compile(
        r"(?:not\s+(?:a|an)\s+(?:document|specification)?[- ]?reference|"
        r"not\s+create\s+(?:an?\s+)?(?:external\s+)?relation|"
        r"no\s+document[- ]reference\s+cue|"
        r"不是.{0,20}(?:文档|文件|式样书|仕様書)?引用|"
        r"不得.{0,30}(?:识别|識別).{0,20}(?:外部|文档|式样书|仕様書)?引用)",
        re.I,
    )
    _REFERENCE_ROW_CODE_PATTERN = re.compile(
        r"^\s*(?P<code>(?:[A-Za-z]{1,12}[A-Za-z0-9]*[-_/])?[A-Za-z0-9]{2,16}(?:[-_/][A-Za-z0-9]{1,16}){0,4}|\d{3,6})\s*(?:\||\t| {2,})",
        re.I,
    )
    _TOYOTA_REFERENCE_TABLE_HEADER_PATTERN = re.compile(
        r"Specification\s+name\s*\|\s*Specification\s+No\.?",
        re.I,
    )
    _TOYOTA_REFERENCE_CELL_CODE_PATTERN = re.compile(
        r"^(?:[A-Z]\d{2,6}|\d{2,6}(?:[ _-][A-Z]{1,4})?)$",
        re.I,
    )
    # Primary Toyota catalog identity. Keep this deliberately strict: title/file
    # tokens such as 21CY_APP_DATA_REPORTING or BLUETOOTHSPECIFICATIONS are not
    # specification numbers and must never become primary graph nodes. General
    # non-Toyota codes such as ABC-1234 remain supported through the second form.
    _PRIMARY_SPEC_CODE_PATTERN = re.compile(
        r"^(?:"
        r"(?:[A-Z]{0,3}\d{2,6})(?:[_ -]?(?:NA|EU|JP|CN|US|ROW|GLOBAL))?"
        r"|[A-Z]{2,12}[-_/]\d{2,10}(?:[-_/][A-Z0-9]{1,8}){0,2}"
        r")$",
        re.I,
    )
    _BRACKET_CODE_PATTERN = re.compile(
        r"\[\s*(?P<code>[A-Za-z]{0,3}\d{2,6}(?:[_ -]?(?:NA|EU|JP|CN|US|ROW|GLOBAL))?)\s*\]",
        re.I,
    )

    # Toyota production specifications frequently reference another specification
    # with the catalog number *before* the title, e.g.
    #   "Please refer to 408_User Profile Sequence Specfor the sequence"
    # or use a bracketed catalog number after an explicit reference/compliance cue,
    # e.g.
    #   "(refer to spec [703])"
    #   "shall comply with [703] specification"
    # These patterns deliberately require a reference/compliance cue so ordinary
    # numeric values such as 408 ms are not promoted to specification edges.
    _TOYOTA_CODE_TITLE_REFERENCE_PATTERN = re.compile(
        r"(?P<cue>please\s+refer\s+to|refer\s+to|see\s+|according\s+to|"
        r"as\s+specified\s+in|specified\s+in|defined\s+in|based\s+on|"
        r"参照|を参照|参照のこと)\s*"
        r"(?:the\s+)?(?:spec(?:ification)?\s*)?"
        r"(?P<code>[A-Za-z]?\d{2,6})"
        r"[_\s-]+"
        r"(?P<title>[A-Za-z0-9][A-Za-z0-9 _./&()+-]{2,140}?"
        r"(?:Spec(?:ification)?|仕様書|式様書|式样书))"
        r"(?:[_\s-]*(?:Ver(?:sion)?\.?)[_\s-]*(?P<version>[A-Za-z0-9._-]+))?"
        r"(?:\s*[-–—]\s*(?P<locator>[A-Za-z][A-Za-z0-9_-]{2,80}))?",
        re.I,
    )
    _TOYOTA_BRACKET_SPEC_REFERENCE_PATTERN = re.compile(
        r"(?P<cue>refer(?:ence)?\s+to|please\s+refer\s+to|see\s+|according\s+to|"
        r"comply\s+with|compliant\s+with|conform\s+to|based\s+on|"
        r"as\s+specified\s+in|specified\s+in|defined\s+in|"
        r"参照|準拠|従う|適合|依据|依照|参照)\s*"
        r"(?:the\s+)?(?:spec(?:ification)?\s*)?"
        r"\[\s*(?P<code>[A-Za-z]?\d{2,6}(?:[-_][A-Za-z0-9]+)?)\s*\]"
        r"(?:\s*(?:spec(?:ification)?|仕様書|式様書|式样书))?",
        re.I,
    )

    _SPEC_TOKEN_STOPWORDS = {
        "VERSION", "VER", "NAME", "TITLE", "TYPE", "ID", "NO", "NUMBER",
        "DOCUMENT", "DOC", "SHEET", "SECTION", "CHAPTER", "STATUS", "REQUIREMENT",
        "THIS", "CURRENT",
    }

    _RX_CUES = re.compile(r"\b(?:rx|receive|received|input|consume|consumer|destination)\b|受信|入力", re.I)
    _TX_CUES = re.compile(r"\b(?:tx|transmit|transmitted|output|send|sender|source|produce|producer)\b|送信|出力", re.I)
    _EXPOSE_CUES = re.compile(r"\b(?:expose|provide|provider|implement|server)\b|提供|公開|実装", re.I)
    _USE_CUES = re.compile(r"\b(?:use|uses|call|calls|client|invoke|access)\b|使用|利用|呼出", re.I)

    def extract(
        self,
        *,
        source_specification_id: int,
        source_specification_key: str,
        units: Iterable[ExtractionUnit],
        known_specifications: Iterable[KnownSpecification],
    ) -> RelationExtractionResult:
        known = tuple(known_specifications)
        units = tuple(units)
        # Toyota documents often define the authoritative Specification
        # name/number mapping in one table and later refer to the title from a
        # different chunk. Build one document-local registry before extraction
        # so those references resolve to the Spec No. even when the target
        # specification has not been imported yet.
        local_reference_registry = self._build_document_local_reference_registry(units)

        entities: dict[str, ExtractedEntity] = {}
        relations: dict[str, ExtractedRelation] = {}
        unit_count = 0
        ignored_standard_reference_count = 0

        for unit in units:
            unit_count += 1
            text = str(unit.text or "")
            if not text.strip():
                continue

            ignored_standard_reference_count += len(self._STANDARD_PATTERN.findall(text))
            self._extract_specification_references(
                unit=unit,
                text=text,
                source_specification_id=source_specification_id,
                source_specification_key=source_specification_key,
                known=known,
                relations=relations,
                local_reference_registry=local_reference_registry,
            )
            self._extract_entity_family(
                unit=unit,
                text=text,
                pattern_set=self._REQ_PATTERNS,
                group="id",
                entity_type="REQUIREMENT",
                relation_type="DEFINES_REQUIREMENT",
                method="EXACT_REQUIREMENT_ID",
                confidence=1.0,
                entities=entities,
                relations=relations,
                source_specification_key=source_specification_key,
            )
            self._extract_entity_family(
                unit=unit,
                text=text,
                pattern_set=(self._SIGNAL_PATTERN,),
                group="name",
                entity_type="SIGNAL",
                relation_type=self._signal_relation_type(text),
                method="EXPLICIT_SIGNAL_LABEL",
                confidence=0.99,
                entities=entities,
                relations=relations,
                source_specification_key=source_specification_key,
            )
            self._extract_entity_family(
                unit=unit,
                text=text,
                pattern_set=(self._INTERFACE_PATTERN,),
                group="name",
                entity_type="INTERFACE",
                relation_type=self._interface_relation_type(text),
                method="EXPLICIT_INTERFACE_LABEL",
                confidence=0.99,
                entities=entities,
                relations=relations,
                source_specification_key=source_specification_key,
            )
            self._extract_entity_family(
                unit=unit,
                text=text,
                pattern_set=(self._FUNCTION_PATTERN,),
                group="name",
                entity_type="FUNCTION",
                relation_type="DEFINES_FUNCTION",
                method="EXPLICIT_FUNCTION_LABEL",
                confidence=0.98,
                entities=entities,
                relations=relations,
                source_specification_key=source_specification_key,
            )
            self._extract_entity_family(
                unit=unit,
                text=text,
                pattern_set=(self._ECU_PATTERN,),
                group="name",
                entity_type="ECU",
                relation_type="RELATED_TO_ECU",
                method="EXPLICIT_ECU_LABEL",
                confidence=0.98,
                entities=entities,
                relations=relations,
                source_specification_key=source_specification_key,
            )
            self._extract_entity_family(
                unit=unit,
                text=text,
                pattern_set=(self._PROTOCOL_PATTERN,),
                group="name",
                entity_type="PROTOCOL",
                relation_type="USES_PROTOCOL",
                method="EXPLICIT_PROTOCOL_LABEL",
                confidence=0.98,
                entities=entities,
                relations=relations,
                source_specification_key=source_specification_key,
            )

        title_candidates_coalesced = self._coalesce_title_candidates(relations)
        unresolved_count = sum(1 for relation in relations.values() if relation.status != "CONFIRMED")
        ambiguous_count = sum(1 for relation in relations.values() if relation.status == "AMBIGUOUS")
        reference_count = sum(1 for relation in relations.values() if relation.relation_type == "REFERENCES")
        by_type: dict[str, int] = {}
        by_method: dict[str, int] = {}
        for relation in relations.values():
            by_type[relation.relation_type] = by_type.get(relation.relation_type, 0) + 1
            by_method[relation.extraction_method] = by_method.get(relation.extraction_method, 0) + 1

        return RelationExtractionResult(
            entities=tuple(entities.values()),
            relations=tuple(relations.values()),
            unresolved_count=unresolved_count,
            stats={
                "extractor_version": EXTRACTOR_VERSION,
                "unit_count": unit_count,
                "entity_count": len(entities),
                "relation_count": len(relations),
                "spec_reference_count": reference_count,
                "unresolved_count": unresolved_count,
                "ambiguous_count": ambiguous_count,
                "ignored_standard_reference_count": ignored_standard_reference_count,
                "title_candidates_coalesced": title_candidates_coalesced,
                "relation_type_counts": by_type,
                "extraction_method_counts": by_method,
                "qdrant_used": False,
                "llm_used": False,
            },
        )

    def _extract_specification_references(
        self,
        *,
        unit: ExtractionUnit,
        text: str,
        source_specification_id: int,
        source_specification_key: str,
        known: tuple[KnownSpecification, ...],
        relations: dict[str, ExtractedRelation],
        local_reference_registry: dict[str, tuple[str, str]],
    ) -> None:
        normalized_text = unicodedata.normalize("NFKC", text)
        context_kind = "REFERENCE_SECTION" if self._is_reference_section(unit) else "BODY"
        matched_spans: list[tuple[int, int]] = []
        row_title_to_code: dict[str, str] = {
            normalized_title: code
            for normalized_title, (code, _raw_title) in local_reference_registry.items()
        }
        source_codes = self._source_spec_codes(
            source_specification_id=source_specification_id,
            source_specification_key=source_specification_key,
            known=known,
        )

        code_index: dict[str, list[KnownSpecification]] = {}
        title_index: dict[str, list[KnownSpecification]] = {}
        for target in known:
            if int(target.specification_id) == int(source_specification_id):
                continue
            if target.spec_code:
                code_index.setdefault(self._normalize_code(target.spec_code), []).append(target)
            for alias in self._target_aliases(target):
                normalized_alias = self._normalize_title(alias)
                if normalized_alias:
                    title_index.setdefault(normalized_alias, []).append(target)

        # 1) Exact known code/name/alias matching.  Unlike v1, a title mention is
        # not automatically authoritative unless it occurs in an explicit
        # reference context.  This cuts narrative false positives substantially.
        for target in known:
            if int(target.specification_id) == int(source_specification_id):
                continue

            aliases: list[tuple[str, str, float]] = []
            if target.spec_code:
                aliases.append((str(target.spec_code).strip(), "EXACT_SPEC_CODE", 1.0))
            if target.canonical_name and len(target.canonical_name.strip()) >= 4:
                aliases.append((target.canonical_name.strip(), "EXACT_SPEC_NAME", 0.995))
            for alias in target.aliases:
                if alias and len(str(alias).strip()) >= 4:
                    aliases.append((str(alias).strip(), "EXACT_SPEC_ALIAS", 0.99))

            seen_aliases: set[tuple[str, str]] = set()
            for alias, method, confidence in aliases:
                normalized_alias_text = unicodedata.normalize("NFKC", alias)
                dedup_key = (normalized_alias_text.casefold(), method)
                if dedup_key in seen_aliases:
                    continue
                seen_aliases.add(dedup_key)

                for match in re.finditer(re.escape(normalized_alias_text), normalized_text, re.I):
                    start, end = match.span()
                    if not self._reference_context_ok(
                        unit=unit,
                        text=normalized_text,
                        start=start,
                        end=end,
                        method=method,
                    ):
                        continue
                    self._upsert_reference_relation(
                        relations=relations,
                        source_specification_key=source_specification_key,
                        target_specification_id=int(target.specification_id),
                        target_external_key=None,
                        target_key=f"SPEC:{target.specification_id}",
                        method=method,
                        confidence=confidence,
                        status="CONFIRMED",
                        evidence=self._make_evidence(unit, text, start, end),
                        metadata={
                            "target_specification_key": target.specification_key,
                            "matched_reference": alias,
                            "normalized_reference": (
                                self._normalize_code(alias)
                                if method == "EXACT_SPEC_CODE"
                                else self._normalize_title(alias)
                            ),
                            "context_kind": context_kind,
                            "resolution_mode": "SQL_REGISTRY_EXACT",
                            "target_identity_kind": "SPEC_MASTER",
                            "graph_visibility": "PRIMARY",
                        },
                    )
                    matched_spans.append((start, end))

        # 2) Toyota code-before-title references and bracketed catalog references.
        # These are emitted even when the target specification has not been
        # imported yet.  That is essential for the graph: a source-only import
        # must still produce an UNRESOLVED placeholder edge such as 508 -> 408.
        for match in self._TOYOTA_CODE_TITLE_REFERENCE_PATTERN.finditer(normalized_text):
            start, end = match.span("code")
            raw_code = self._trim_reference_token(match.group("code"))
            code = self._normalize_code(raw_code)
            if (
                not code
                or code in self._SPEC_TOKEN_STOPWORDS
                or code in source_codes
                or not self._is_plausible_spec_code(raw_code)
            ):
                continue
            targets = [
                item for item in code_index.get(code, [])
                if int(item.specification_id) != int(source_specification_id)
            ]
            cue = str(match.group("cue") or "").strip()
            title_hint = str(match.group("title") or "").strip() or None
            version_hint = str(match.group("version") or "").strip() or None
            locator = str(match.group("locator") or "").strip() or None
            self._emit_code_candidate(
                relations=relations,
                unit=unit,
                text=text,
                source_specification_key=source_specification_key,
                raw_reference=raw_code,
                normalized_reference=code,
                start=start,
                end=min(len(text), end),
                targets=targets,
                method="TOYOTA_CODE_TITLE_REFERENCE",
                context_kind=context_kind,
                confidence=0.995,
                extra_metadata={
                    "toyota_reference_kind": "REFERENCES_SPEC",
                    "reference_cue": cue,
                    "target_title_hint": title_hint,
                    "target_version_hint": version_hint,
                    "target_locator": locator,
                },
            )
            matched_spans.append((start, end))

        # 1b) Document-local Toyota title -> Spec No. mapping. This is
        # stronger than a global fuzzy/title guess because the mapping is stated
        # explicitly by the source document's own Relevant/Reference
        # Specification table.
        for normalized_title, (local_code, raw_title) in local_reference_registry.items():
            if not self._is_plausible_spec_code(local_code):
                continue
            normalized_raw_title = unicodedata.normalize("NFKC", raw_title).strip()
            if len(normalized_raw_title) < 4:
                continue
            for match in re.finditer(re.escape(normalized_raw_title), normalized_text, re.I):
                start, end = match.span()
                if any(start >= a and end <= b for a, b in matched_spans):
                    continue
                if not self._reference_context_ok(
                    unit=unit,
                    text=normalized_text,
                    start=start,
                    end=end,
                    method="LOCAL_REFERENCE_TABLE_TITLE",
                ):
                    continue
                targets = [
                    item for item in code_index.get(self._normalize_code(local_code), [])
                    if int(item.specification_id) != int(source_specification_id)
                ]
                self._emit_code_candidate(
                    relations=relations,
                    unit=unit,
                    text=text,
                    source_specification_key=source_specification_key,
                    raw_reference=raw_title,
                    normalized_reference=self._normalize_code(local_code),
                    start=start,
                    end=min(len(text), end),
                    targets=targets,
                    method="LOCAL_REFERENCE_TABLE_TITLE",
                    context_kind=context_kind,
                    confidence=0.995 if targets else 0.985,
                    extra_metadata={
                        "toyota_reference_kind": "REFERENCES_SPEC",
                        "target_title_hint": raw_title,
                        "resolution_mode": "DOCUMENT_LOCAL_REFERENCE_REGISTRY",
                        "target_identity_kind": "SPEC_CODE",
                        "graph_visibility": "PRIMARY",
                    },
                )
                matched_spans.append((start, end))

        for match in self._TOYOTA_BRACKET_SPEC_REFERENCE_PATTERN.finditer(normalized_text):
            start, end = match.span("code")
            raw_code = self._trim_reference_token(match.group("code"))
            code = self._normalize_code(raw_code)
            if (
                not code
                or code in self._SPEC_TOKEN_STOPWORDS
                or code in source_codes
                or not self._is_plausible_spec_code(raw_code)
            ):
                continue
            targets = [
                item for item in code_index.get(code, [])
                if int(item.specification_id) != int(source_specification_id)
            ]
            cue = str(match.group("cue") or "").strip()
            cue_key = unicodedata.normalize("NFKC", cue).casefold()
            reference_kind = (
                "COMPLIES_WITH"
                if any(token in cue_key for token in ("comply", "compliant", "conform", "準拠", "従う", "適合"))
                else "REFERENCES_SPEC"
            )
            self._emit_code_candidate(
                relations=relations,
                unit=unit,
                text=text,
                source_specification_key=source_specification_key,
                raw_reference=raw_code,
                normalized_reference=code,
                start=start,
                end=min(len(text), end),
                targets=targets,
                method="TOYOTA_BRACKET_SPEC_REFERENCE",
                context_kind=context_kind,
                confidence=0.995,
                extra_metadata={
                    "toyota_reference_kind": reference_kind,
                    "reference_cue": cue,
                },
            )
            matched_spans.append((start, end))

        # 2b) Toyota sentences often carry more than one bracketed specification
        # after a single cue, e.g. "shall comply specification [190] and [191]
        # documents". Capture every bracket in the cue-bearing sentence.
        for sentence_start, sentence in self._iter_reference_sentences(normalized_text):
            if self._NEGATIVE_REFERENCE_PATTERN.search(sentence):
                continue
            cue_match = self._REFERENCE_CUE_PATTERN.search(sentence)
            if not cue_match:
                continue
            cue_text = cue_match.group(0)
            cue_key = unicodedata.normalize("NFKC", cue_text).casefold()
            reference_kind = (
                "COMPLIES_WITH"
                if any(token in cue_key for token in ("comply", "compliant", "conform", "準拠", "従う", "適合"))
                else "REFERENCES_SPEC"
            )
            for bracket in self._BRACKET_CODE_PATTERN.finditer(sentence):
                raw_code = self._trim_reference_token(bracket.group("code"))
                code = self._normalize_code(raw_code)
                if (
                    not code
                    or code in self._SPEC_TOKEN_STOPWORDS
                    or code in source_codes
                    or not self._is_plausible_spec_code(raw_code)
                ):
                    continue
                start = sentence_start + bracket.start("code")
                end = sentence_start + bracket.end("code")
                targets = [
                    item for item in code_index.get(code, [])
                    if int(item.specification_id) != int(source_specification_id)
                ]
                self._emit_code_candidate(
                    relations=relations,
                    unit=unit,
                    text=text,
                    source_specification_key=source_specification_key,
                    raw_reference=raw_code,
                    normalized_reference=code,
                    start=start,
                    end=min(len(text), end),
                    targets=targets,
                    method="TOYOTA_BRACKET_LIST_REFERENCE",
                    context_kind=context_kind,
                    confidence=0.995,
                    extra_metadata={
                        "toyota_reference_kind": reference_kind,
                        "reference_cue": cue_text,
                        "target_identity_kind": "SPEC_CODE",
                        "graph_visibility": "PRIMARY",
                    },
                )
                matched_spans.append((start, end))

        # 3) Explicit "Specification <code>" style references.  Unknown codes
        # are retained instead of discarded so a later import can reconcile them.
        for match in self._SPEC_TOKEN_PATTERN.finditer(normalized_text):
            start, end = match.span("token")
            if any(start >= a and end <= b for a, b in matched_spans):
                continue
            raw_token = self._trim_reference_token(match.group("token"))
            token = self._normalize_code(raw_token)
            if (
                not token
                or token in self._SPEC_TOKEN_STOPWORDS
                or token in source_codes
                or not self._is_plausible_spec_code(raw_token)
            ):
                continue
            if self._looks_like_standard_reference(match.group(0)):
                continue
            targets = [item for item in code_index.get(token, []) if int(item.specification_id) != int(source_specification_id)]
            self._emit_code_candidate(
                relations=relations,
                unit=unit,
                text=text,
                source_specification_key=source_specification_key,
                raw_reference=raw_token,
                normalized_reference=token,
                start=start,
                end=min(len(text), end),
                targets=targets,
                method="EXPLICIT_SPEC_REFERENCE",
                context_kind=context_kind,
                confidence=0.985,
            )
            matched_spans.append((start, end))

        # 4) Toyota "Specification name | Specification No." tables place the
        # catalog number in the right-hand cell (e.g. "User profile sequence
        # specification | 408").  The previous generic row parser expected the
        # code in the first cell and therefore missed 408/703 while sometimes
        # mistaking long titles for catalog codes.
        if context_kind == "REFERENCE_SECTION" and self._TOYOTA_REFERENCE_TABLE_HEADER_PATTERN.search(normalized_text):
            for row_start, row_text in self._iter_lines_with_offsets(normalized_text):
                if "|" not in row_text or self._looks_like_standard_reference(row_text):
                    continue
                cells = [cell.strip() for cell in row_text.split("|")]
                if len(cells) < 2:
                    continue
                raw_code = cells[-1]
                if not self._TOYOTA_REFERENCE_CELL_CODE_PATTERN.fullmatch(raw_code):
                    continue
                code = self._normalize_code(raw_code)
                if (
                    not code
                    or code in self._SPEC_TOKEN_STOPWORDS
                    or code in source_codes
                    or not self._is_plausible_spec_code(raw_code)
                ):
                    continue
                code_pos = row_text.rfind(raw_code)
                start = row_start + max(0, code_pos)
                end = start + len(raw_code)
                if any(start >= a and end <= b for a, b in matched_spans):
                    continue
                title_hint = cells[-2].strip() or None
                targets = [
                    item for item in code_index.get(code, [])
                    if int(item.specification_id) != int(source_specification_id)
                ]
                self._emit_code_candidate(
                    relations=relations,
                    unit=unit,
                    text=text,
                    source_specification_key=source_specification_key,
                    raw_reference=raw_code,
                    normalized_reference=code,
                    start=start,
                    end=end,
                    targets=targets,
                    method="TOYOTA_REFERENCE_TABLE_CODE",
                    context_kind=context_kind,
                    confidence=0.995 if targets else 0.985,
                    extra_metadata={
                        "toyota_reference_kind": "RELEVANT_SPEC",
                        "target_title_hint": title_hint,
                        "reference_table_layout": "TITLE_CODE",
                    },
                )
                if title_hint:
                    normalized_title = self._normalize_title(title_hint)
                    if normalized_title:
                        row_title_to_code[normalized_title] = code
                matched_spans.append((start, end))

            # Some Toyota exports serialize the first catalog number as a
            # standalone line immediately after the table header. Preserve it as
            # an unresolved/confirmed catalog reference rather than dropping it.
            lines = list(self._iter_lines_with_offsets(normalized_text))
            for idx, (row_start, row_text) in enumerate(lines):
                if not self._TOYOTA_REFERENCE_TABLE_HEADER_PATTERN.search(row_text):
                    continue
                if idx + 1 >= len(lines):
                    break
                next_start, next_text = lines[idx + 1]
                raw_code = next_text.strip()
                if not self._TOYOTA_REFERENCE_CELL_CODE_PATTERN.fullmatch(raw_code):
                    break
                code = self._normalize_code(raw_code)
                if code in source_codes or not self._is_plausible_spec_code(raw_code):
                    break
                start = next_start + next_text.find(raw_code)
                end = start + len(raw_code)
                if any(start >= a and end <= b for a, b in matched_spans):
                    break
                targets = [
                    item for item in code_index.get(code, [])
                    if int(item.specification_id) != int(source_specification_id)
                ]
                self._emit_code_candidate(
                    relations=relations,
                    unit=unit,
                    text=text,
                    source_specification_key=source_specification_key,
                    raw_reference=raw_code,
                    normalized_reference=code,
                    start=start,
                    end=end,
                    targets=targets,
                    method="TOYOTA_REFERENCE_TABLE_CODE",
                    context_kind=context_kind,
                    confidence=0.98,
                    extra_metadata={
                        "toyota_reference_kind": "RELEVANT_SPEC",
                        "reference_table_layout": "TITLE_CODE_STANDALONE_FIRST_CODE",
                    },
                )
                matched_spans.append((start, end))
                break

        # 5) Tables under Applicable/Referenced Documents frequently contain only
        # "2007 | Some Specification | Rev2.1" with no word "Specification"
        # before the code.  Reference-section context makes that deterministic.
        if context_kind == "REFERENCE_SECTION" and not self._TOYOTA_REFERENCE_TABLE_HEADER_PATTERN.search(normalized_text):
            for row_start, row_text in self._iter_lines_with_offsets(normalized_text):
                if not row_text.strip() or self._looks_like_standard_reference(row_text):
                    continue
                row_match = self._REFERENCE_ROW_CODE_PATTERN.search(row_text)
                if not row_match:
                    continue
                raw_code = row_match.group("code")
                code = self._normalize_code(raw_code)
                if (
                    not code
                    or code in self._SPEC_TOKEN_STOPWORDS
                    or code in source_codes
                    or not self._is_plausible_spec_code(raw_code)
                ):
                    continue
                start = row_start + row_match.start("code")
                end = row_start + row_match.end("code")
                if any(start >= a and end <= b for a, b in matched_spans):
                    continue
                parts = [part.strip() for part in row_text.split("|")]
                if len(parts) >= 2 and parts[1]:
                    row_title = self._normalize_title(parts[1])
                    if row_title:
                        row_title_to_code[row_title] = code
                targets = [item for item in code_index.get(code, []) if int(item.specification_id) != int(source_specification_id)]
                self._emit_code_candidate(
                    relations=relations,
                    unit=unit,
                    text=text,
                    source_specification_key=source_specification_key,
                    raw_reference=raw_code,
                    normalized_reference=code,
                    start=start,
                    end=end,
                    targets=targets,
                    method="REFERENCE_SECTION_CODE",
                    context_kind=context_kind,
                    confidence=0.99 if targets else 0.97,
                )
                matched_spans.append((start, end))

        # 6) Quoted document titles are common in Chinese/Japanese specifications.
        # Exact registry aliases resolve immediately; unmatched titles are kept as
        # unresolved candidates only when a reference cue/section proves intent.
        for match in self._QUOTED_TITLE_PATTERN.finditer(normalized_text):
            raw_title = next((value for value in match.groupdict().values() if value), "").strip()
            if not raw_title:
                continue
            start, end = match.span()
            if not self._reference_context_ok(
                unit=unit,
                text=normalized_text,
                start=start,
                end=end,
                method="QUOTED_SPEC_TITLE",
            ):
                continue
            normalized_title = self._normalize_title(raw_title)
            if not normalized_title:
                continue
            targets = [item for item in title_index.get(normalized_title, []) if int(item.specification_id) != int(source_specification_id)]
            linked_code = row_title_to_code.get(normalized_title)
            if not targets and linked_code:
                code_targets = [
                    item for item in code_index.get(linked_code, [])
                    if int(item.specification_id) != int(source_specification_id)
                ]
                self._emit_code_candidate(
                    relations=relations,
                    unit=unit,
                    text=text,
                    source_specification_key=source_specification_key,
                    raw_reference=raw_title,
                    normalized_reference=linked_code,
                    start=start,
                    end=min(end, len(text)),
                    targets=code_targets,
                    method="REFERENCE_SECTION_TITLE_WITH_CODE",
                    context_kind=context_kind,
                    confidence=0.98 if code_targets else 0.96,
                )
                continue
            if len(targets) == 1:
                target = targets[0]
                self._upsert_reference_relation(
                    relations=relations,
                    source_specification_key=source_specification_key,
                    target_specification_id=int(target.specification_id),
                    target_external_key=None,
                    target_key=f"SPEC:{target.specification_id}",
                    method="EXACT_QUOTED_SPEC_TITLE",
                    confidence=0.99,
                    status="CONFIRMED",
                    evidence=self._make_evidence(unit, text, start, min(end, len(text))),
                    metadata={
                        "target_specification_key": target.specification_key,
                        "matched_reference": raw_title,
                        "normalized_reference": normalized_title,
                        "context_kind": context_kind,
                        "resolution_mode": "SQL_REGISTRY_EXACT",
                        "target_identity_kind": "SPEC_MASTER",
                        "graph_visibility": "PRIMARY",
                    },
                )
            elif len(targets) > 1:
                self._upsert_reference_relation(
                    relations=relations,
                    source_specification_key=source_specification_key,
                    target_specification_id=None,
                    target_external_key=f"SPEC_TITLE:{normalized_title[:180]}",
                    target_key=f"EXTERNAL:SPEC_TITLE:{normalized_title[:180]}",
                    method="QUOTED_SPEC_TITLE",
                    confidence=0.70,
                    status="AMBIGUOUS",
                    evidence=self._make_evidence(unit, text, start, min(end, len(text))),
                    metadata={
                        "matched_reference": raw_title,
                        "normalized_reference": normalized_title,
                        "context_kind": context_kind,
                        "candidate_specification_ids": [int(item.specification_id) for item in targets],
                        "resolution_mode": "SQL_REGISTRY_AMBIGUOUS",
                        "target_identity_kind": "TITLE_ONLY",
                        "graph_visibility": "SECONDARY",
                    },
                )
            elif not self._looks_like_internal_reference(raw_title):
                self._upsert_reference_relation(
                    relations=relations,
                    source_specification_key=source_specification_key,
                    target_specification_id=None,
                    target_external_key=f"SPEC_TITLE:{normalized_title[:180]}",
                    target_key=f"EXTERNAL:SPEC_TITLE:{normalized_title[:180]}",
                    method="QUOTED_SPEC_TITLE",
                    confidence=0.82,
                    status="UNRESOLVED",
                    evidence=self._make_evidence(unit, text, start, min(end, len(text))),
                    metadata={
                        "matched_reference": raw_title,
                        "normalized_reference": normalized_title,
                        "context_kind": context_kind,
                        "resolution_mode": "SQL_REGISTRY_UNRESOLVED",
                        "target_identity_kind": "TITLE_ONLY",
                        "graph_visibility": "SECONDARY",
                    },
                )

    def _emit_code_candidate(
        self,
        *,
        relations: dict[str, ExtractedRelation],
        unit: ExtractionUnit,
        text: str,
        source_specification_key: str,
        raw_reference: str,
        normalized_reference: str,
        start: int,
        end: int,
        targets: list[KnownSpecification],
        method: str,
        context_kind: str,
        confidence: float,
        extra_metadata: dict[str, Any] | None = None,
    ) -> None:
        extra_metadata = dict(extra_metadata or {})
        extra_metadata.setdefault("target_identity_kind", "SPEC_CODE")
        extra_metadata.setdefault("graph_visibility", "PRIMARY")
        if len(targets) == 1:
            target = targets[0]
            target_id = int(target.specification_id)
            self._upsert_reference_relation(
                relations=relations,
                source_specification_key=source_specification_key,
                target_specification_id=target_id,
                target_external_key=None,
                target_key=f"SPEC:{target_id}",
                method=method,
                confidence=confidence,
                status="CONFIRMED",
                evidence=self._make_evidence(unit, text, start, end),
                metadata={
                    "target_specification_key": target.specification_key,
                    "matched_reference": raw_reference,
                    "normalized_reference": normalized_reference,
                    "context_kind": context_kind,
                    "resolution_mode": "SQL_REGISTRY_EXACT",
                    **extra_metadata,
                },
            )
            return

        if len(targets) > 1:
            self._upsert_reference_relation(
                relations=relations,
                source_specification_key=source_specification_key,
                target_specification_id=None,
                target_external_key=f"SPEC:{normalized_reference}",
                target_key=f"EXTERNAL:SPEC:{normalized_reference}",
                method=method,
                confidence=min(confidence, 0.75),
                status="AMBIGUOUS",
                evidence=self._make_evidence(unit, text, start, end),
                metadata={
                    "matched_reference": raw_reference,
                    "normalized_reference": normalized_reference,
                    "context_kind": context_kind,
                    "candidate_specification_ids": [int(item.specification_id) for item in targets],
                    "resolution_mode": "SQL_REGISTRY_AMBIGUOUS",
                    **extra_metadata,
                },
            )
            return

        self._upsert_reference_relation(
            relations=relations,
            source_specification_key=source_specification_key,
            target_specification_id=None,
            target_external_key=f"SPEC:{normalized_reference}",
            target_key=f"EXTERNAL:SPEC:{normalized_reference}",
            method=method,
            confidence=confidence,
            status="UNRESOLVED",
            evidence=self._make_evidence(unit, text, start, end),
            metadata={
                "matched_reference": raw_reference,
                "normalized_reference": normalized_reference,
                "context_kind": context_kind,
                "resolution_mode": "SQL_REGISTRY_UNRESOLVED",
                **extra_metadata,
            },
        )

    def _upsert_reference_relation(
        self,
        *,
        relations: dict[str, ExtractedRelation],
        source_specification_key: str,
        target_specification_id: int | None,
        target_external_key: str | None,
        target_key: str,
        method: str,
        confidence: float,
        status: str,
        evidence: ExtractionEvidence,
        metadata: dict[str, Any],
    ) -> None:
        relation_key = self._relation_key(source_specification_key, "REFERENCES", None, target_key)
        relation = relations.get(relation_key)
        if relation is None:
            relation = ExtractedRelation(
                relation_key=relation_key,
                relation_type="REFERENCES",
                extraction_method=method,
                confidence=confidence,
                status=status,
                is_explicit=True,
                target_specification_id=target_specification_id,
                target_external_key=target_external_key,
                metadata=dict(metadata),
            )
            relation.metadata["extraction_methods"] = [method]
            relations[relation_key] = relation
        else:
            methods = list(relation.metadata.get("extraction_methods") or [])
            if method not in methods:
                methods.append(method)
            relation.metadata["extraction_methods"] = methods
            relation.confidence = max(float(relation.confidence), float(confidence))
            # Exact confirmed evidence dominates an earlier unresolved candidate.
            if status == "CONFIRMED" and relation.status != "CONFIRMED":
                relation.status = "CONFIRMED"
                relation.target_specification_id = target_specification_id
                relation.target_external_key = None
                relation.extraction_method = method
            for field in ("target_title_hint", "target_version_hint", "target_locator"):
                new_value = metadata.get(field)
                old_value = relation.metadata.get(field)
                if new_value is None or old_value is None or str(new_value) == str(old_value):
                    continue
                values_key = f"{field}_values"
                values = list(relation.metadata.get(values_key) or [])
                for item in (old_value, new_value):
                    if item not in values:
                        values.append(item)
                relation.metadata[values_key] = values
                relation.metadata[f"{field}_conflict"] = True
            relation.metadata.update({k: v for k, v in metadata.items() if v is not None})
        self._append_evidence(relation, evidence)

    def _build_document_local_reference_registry(
        self,
        units: tuple[ExtractionUnit, ...],
    ) -> dict[str, tuple[str, str]]:
        """Return unique Toyota title -> (Spec No., raw title) mappings.

        The source document's own reference table is authoritative for local
        identity. Ambiguous title mappings are deliberately dropped rather than
        guessed.
        """
        candidates: dict[str, dict[str, str]] = {}

        def add(raw_title: str, raw_code: str) -> None:
            title = unicodedata.normalize("NFKC", str(raw_title or "")).strip()
            code = self._normalize_code(raw_code)
            if not title or len(title) < 4 or not self._is_plausible_spec_code(code):
                return
            normalized_title = self._normalize_title(title)
            if not normalized_title:
                return
            candidates.setdefault(normalized_title, {})[code] = title

        for unit in units:
            text = unicodedata.normalize("NFKC", str(unit.text or ""))
            if not text.strip():
                continue

            reference_context = (
                self._is_reference_section(unit)
                or bool(self._TOYOTA_REFERENCE_TABLE_HEADER_PATTERN.search(text))
            )
            if reference_context:
                for _row_start, row_text in self._iter_lines_with_offsets(text):
                    if "|" not in row_text or self._looks_like_standard_reference(row_text):
                        continue
                    cells = [cell.strip() for cell in row_text.split("|")]
                    if len(cells) < 2:
                        continue
                    raw_code = cells[-1]
                    if not self._TOYOTA_REFERENCE_CELL_CODE_PATTERN.fullmatch(raw_code):
                        continue
                    add(cells[-2], raw_code)

            # Inline code-before-title forms also provide an authoritative local
            # alias, e.g. "408_User Profile Sequence Spec".
            for match in self._TOYOTA_CODE_TITLE_REFERENCE_PATTERN.finditer(text):
                add(match.group("title"), match.group("code"))

        result: dict[str, tuple[str, str]] = {}
        for normalized_title, code_map in candidates.items():
            if len(code_map) != 1:
                continue
            code, raw_title = next(iter(code_map.items()))
            result[normalized_title] = (code, raw_title)
        return result

    @classmethod
    def _is_plausible_spec_code(cls, value: str) -> bool:
        raw = unicodedata.normalize("NFKC", str(value or "")).strip()
        if not raw or len(raw) > 32:
            return False
        normalized = cls._normalize_code(raw)
        if not normalized or normalized in cls._SPEC_TOKEN_STOPWORDS:
            return False
        title_words = (
            "FUNCTION", "SPECIFICATION", "SPECIFICATIONS", "SYSTEM", "AUDIO",
            "REPORTING", "BLUETOOTH", "PROFILE", "CONNECTED", "DOCUMENT",
        )
        generic_code = re.fullmatch(
            r"[A-Z]{2,12}[-_/]\d{2,10}(?:[-_/][A-Z0-9]{1,8}){0,2}",
            normalized,
        )
        if any(word in normalized for word in title_words) and not generic_code:
            return False
        return bool(cls._PRIMARY_SPEC_CODE_PATTERN.fullmatch(raw))

    @classmethod
    def _source_spec_codes(
        cls,
        *,
        source_specification_id: int,
        source_specification_key: str,
        known: tuple[KnownSpecification, ...],
    ) -> set[str]:
        result: set[str] = set()
        for item in known:
            if int(item.specification_id) != int(source_specification_id):
                continue
            if item.spec_code:
                code = cls._normalize_code(item.spec_code)
                if code:
                    result.add(code)
        tail = str(source_specification_key or "").split(":")[-1].strip()
        if tail and cls._is_plausible_spec_code(tail):
            result.add(cls._normalize_code(tail))
        return result

    @staticmethod
    def _iter_reference_sentences(text: str) -> Iterable[tuple[int, str]]:
        for match in re.finditer(r"[^\n\r。！？!?]+(?:[。！？!?]|$)", text):
            sentence = match.group(0)
            if sentence.strip():
                yield match.start(), sentence

    def _coalesce_title_candidates(self, relations: dict[str, ExtractedRelation]) -> int:
        """Merge title-only aliases into code/master relations when identity is known."""
        title_to_key: dict[str, str] = {}
        for key, relation in relations.items():
            if relation.relation_type != "REFERENCES":
                continue
            hint = str(relation.metadata.get("target_title_hint") or "").strip()
            if not hint:
                continue
            normalized = self._normalize_title(hint)
            if normalized:
                title_to_key[normalized] = key

        removed = 0
        for key, relation in list(relations.items()):
            external = str(relation.target_external_key or "")
            if not external.startswith("SPEC_TITLE:"):
                continue
            normalized = external[len("SPEC_TITLE:"):].strip()
            destination_key = title_to_key.get(normalized)
            if not destination_key or destination_key == key:
                continue
            destination = relations.get(destination_key)
            if destination is None:
                continue
            for evidence in relation.evidence:
                self._append_evidence(destination, evidence)
            methods = list(destination.metadata.get("extraction_methods") or [])
            for method in relation.metadata.get("extraction_methods") or [relation.extraction_method]:
                if method not in methods:
                    methods.append(method)
            destination.metadata["extraction_methods"] = methods
            aliases = list(destination.metadata.get("title_aliases") or [])
            matched = relation.metadata.get("matched_reference")
            if matched and matched not in aliases:
                aliases.append(matched)
            if aliases:
                destination.metadata["title_aliases"] = aliases
            destination.confidence = max(destination.confidence, relation.confidence)
            del relations[key]
            removed += 1
        return removed

    def _reference_context_ok(
        self,
        *,
        unit: ExtractionUnit,
        text: str,
        start: int,
        end: int,
        method: str,
    ) -> bool:
        if self._is_reference_section(unit):
            return True
        window = text[max(0, start - 70): min(len(text), end + 70)]
        if self._NEGATIVE_REFERENCE_PATTERN.search(window):
            return False
        if self._REFERENCE_CUE_PATTERN.search(window):
            return True
        # Codes preceded by an explicit Specification/Document label are already
        # unambiguous document-reference syntax.
        if method == "EXACT_SPEC_CODE" and self._SPEC_TOKEN_PATTERN.search(window):
            return True
        # Quoted names without any reference cue are narrative text, not graph facts.
        return False

    @classmethod
    def _is_reference_section(cls, unit: ExtractionUnit) -> bool:
        candidates = [unit.section_title, unit.chapter_title]
        for value in candidates:
            if value and cls._REFERENCE_SECTION_PATTERN.search(unicodedata.normalize("NFKC", str(value))):
                return True
        meta = dict(unit.metadata or {})
        section_meta = dict(unit.section_metadata or {})
        for key in ("section_title", "heading", "title"):
            value = meta.get(key) or section_meta.get(key)
            if value and cls._REFERENCE_SECTION_PATTERN.search(unicodedata.normalize("NFKC", str(value))):
                return True
        return False

    @classmethod
    def _looks_like_internal_reference(cls, value: str) -> bool:
        return bool(cls._INTERNAL_ONLY_PATTERN.search(unicodedata.normalize("NFKC", str(value or ""))))

    @classmethod
    def _looks_like_standard_reference(cls, value: str) -> bool:
        return bool(cls._STANDARD_PATTERN.search(unicodedata.normalize("NFKC", str(value or ""))))

    @staticmethod
    def _target_aliases(target: KnownSpecification) -> tuple[str, ...]:
        values: list[str] = []
        if target.canonical_name:
            values.append(str(target.canonical_name))
        values.extend(str(item) for item in target.aliases if item)
        seen: set[str] = set()
        result: list[str] = []
        for value in values:
            key = EngineeringRelationExtractor._normalize_title(value)
            if key and key not in seen:
                seen.add(key)
                result.append(value)
        return tuple(result)

    @staticmethod
    def _iter_lines_with_offsets(text: str) -> Iterable[tuple[int, str]]:
        offset = 0
        for line in text.splitlines(keepends=True):
            clean = line.rstrip("\r\n")
            yield offset, clean
            offset += len(line)
        if text and not text.endswith(("\n", "\r")) and "\n" not in text and "\r" not in text:
            return

    @staticmethod
    def _trim_reference_token(value: str) -> str:
        text = unicodedata.normalize("NFKC", str(value or "")).strip()
        # Stop before common prose delimiters accidentally swallowed by the broad
        # token pattern.  Preserve internal hyphens/slashes used by catalog codes.
        text = re.split(r"[|,，;；。\n\r\t]", text, maxsplit=1)[0].strip()
        text = re.split(r"\s+(?:for|第|の|is|shall|规定|要求|詳細|details?)\b", text, maxsplit=1, flags=re.I)[0].strip()
        return text.strip(" .,:;()[]{}<>《》「」『』\"'")[:80]

    def _extract_entity_family(
        self,
        *,
        unit: ExtractionUnit,
        text: str,
        pattern_set: tuple[re.Pattern[str], ...],
        group: str,
        entity_type: str,
        relation_type: str,
        method: str,
        confidence: float,
        entities: dict[str, ExtractedEntity],
        relations: dict[str, ExtractedRelation],
        source_specification_key: str,
    ) -> None:
        for pattern in pattern_set:
            for match in pattern.finditer(text):
                raw = self._clean_value(match.group(group))
                if not raw:
                    continue
                entity_key = f"{entity_type}:{self._normalize_token(raw)}"
                if entity_key not in entities:
                    entities[entity_key] = ExtractedEntity(
                        entity_type=entity_type,
                        entity_key=entity_key,
                        canonical_name=raw,
                        normalized_value=self._normalize_token(raw),
                        extraction_method=method,
                        confidence=confidence,
                    )
                relation_key = self._relation_key(source_specification_key, relation_type, None, entity_key)
                relation = relations.get(relation_key)
                if relation is None:
                    relation = ExtractedRelation(
                        relation_key=relation_key,
                        relation_type=relation_type,
                        extraction_method=method,
                        confidence=confidence,
                        status="CONFIRMED",
                        is_explicit=True,
                        target_entity_key=entity_key,
                    )
                    relations[relation_key] = relation
                self._append_evidence(relation, self._make_evidence(unit, text, match.start(group), match.end(group)))

    @classmethod
    def _signal_relation_type(cls, text: str) -> str:
        if cls._RX_CUES.search(text):
            return "CONSUMES_SIGNAL"
        if cls._TX_CUES.search(text):
            return "PRODUCES_SIGNAL"
        return "USES_SIGNAL"

    @classmethod
    def _interface_relation_type(cls, text: str) -> str:
        if cls._EXPOSE_CUES.search(text):
            return "EXPOSES_INTERFACE"
        if cls._USE_CUES.search(text):
            return "USES_INTERFACE"
        return "USES_INTERFACE"

    @staticmethod
    def _relation_key(source_spec_key: str, relation_type: str, source_entity_key: str | None, target_key: str) -> str:
        raw = "|".join((source_spec_key, relation_type, source_entity_key or "SPEC", target_key))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _normalize_code(value: str) -> str:
        normalized = unicodedata.normalize("NFKC", str(value or "")).upper()
        normalized = normalized.translate(str.maketrans({"‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "―": "-"}))
        normalized = re.sub(r"\s+", "", normalized)
        normalized = normalized.strip(".,;:()[]{}<>《》「」『』\"'")
        return normalized

    @staticmethod
    def _normalize_title(value: str) -> str:
        normalized = unicodedata.normalize("NFKC", str(value or "")).upper().strip()
        normalized = normalized.translate(str.maketrans({"‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "―": "-"}))
        normalized = re.sub(r"[\s\-_./:：・·,，。()（）\[\]【】]+", "", normalized)
        return normalized.strip("《》「」『』\"'")

    @staticmethod
    def _normalize_token(value: str) -> str:
        normalized = unicodedata.normalize("NFKC", str(value or "")).upper()
        normalized = re.sub(r"\s+", "", normalized)
        return normalized.strip(".,;:()[]{}<>")

    @staticmethod
    def _clean_value(value: str) -> str:
        return str(value or "").strip().strip(".,;:()[]{}<>")[:160]

    @staticmethod
    def _append_evidence(relation: ExtractedRelation, evidence: ExtractionEvidence) -> None:
        if any(item.evidence_hash == evidence.evidence_hash for item in relation.evidence):
            return
        relation.evidence.append(evidence)

    def _make_evidence(self, unit: ExtractionUnit, text: str, start: int, end: int) -> ExtractionEvidence:
        start = max(0, min(int(start), len(text)))
        end = max(start, min(int(end), len(text)))
        half_window = max(60, int(self.policy.max_evidence_snippet_chars // 2))
        left = max(0, start - half_window)
        right = min(len(text), end + half_window)
        snippet = text[left:right].strip()
        meta = dict(unit.metadata or {})
        section_meta = dict(unit.section_metadata or {})
        sheet_name = meta.get("sheet_name") or section_meta.get("sheet_name")
        cell_range = meta.get("cell_range") or section_meta.get("cell_range")
        if cell_range is None:
            first_row = meta.get("first_row_number") or section_meta.get("first_row_number")
            last_row = meta.get("last_row_number") or section_meta.get("last_row_number")
            if first_row is not None:
                cell_range = f"rows {first_row}-{last_row or first_row}"
        evidence_hash = hashlib.sha256(f"{unit.content_id}|{start}|{end}|{snippet}".encode("utf-8")).hexdigest()
        return ExtractionEvidence(
            content_id=int(unit.content_id),
            page_number=unit.page_number,
            chapter_id=unit.chapter_id,
            chapter_title=unit.chapter_title,
            section_id=unit.section_id,
            section_title=unit.section_title,
            sheet_name=str(sheet_name) if sheet_name is not None else None,
            cell_range=str(cell_range) if cell_range is not None else None,
            evidence_text=snippet,
            start_offset=start,
            end_offset=end,
            evidence_hash=evidence_hash,
            metadata={"extractor_version": EXTRACTOR_VERSION},
        )
