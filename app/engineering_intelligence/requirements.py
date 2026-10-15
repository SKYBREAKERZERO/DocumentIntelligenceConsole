"""Stage 1: conservative, offset-exact engineering source-unit extraction.

This is *not* an LLM requirement classifier. Every unit is a verbatim span
of the immutable source, and every extracted cue points back to verbatim text.
No inferred relationship or requirement is automatically approved.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass

EXTRACTOR_VERSION = "requirement-units-v2"

_LINE = re.compile(r"[^\r\n]+")
_BULLET = re.compile(
    r"^\s*(?:[-*•●・]|\(?\d{1,3}[.)、]|[（(]\d{1,3}[）)]|[a-zA-Z][.)])\s+"
)
_TABLE_RULE = re.compile(r"^\s*\|?(?:\s*:?-{2,}:?\s*\|)+\s*:?-{2,}:?\s*\|?\s*$")
_HEADING = re.compile(r"^\s*\d{1,3}(?:\.\d{1,3}){1,5}\s+\S")
_NORMATIVE = re.compile(
    r"\b(?:shall(?:\s+not)?|must(?:\s+not)?|should(?:\s+not)?|is required to)\b|"
    r"(?:しなければならない|する必要がある|こととする|ものとする|すること|禁止|必須|必须|应当|不得)",
    re.I,
)
_MANDATORY = re.compile(
    r"\b(?:shall|must|is required to)\b|(?:しなければならない|する必要がある|こととする|ものとする|すること|必須|必须|应当)", re.I,
)
_PROHIBITED = re.compile(r"\b(?:shall|must|should)\s+not\b|(?:禁止|不得|してはならない|しないこと)", re.I)
_RECOMMENDED = re.compile(r"\bshould\b|(?:推奨|建议|宜)", re.I)
_CONDITION = re.compile(r"\b(?:if|when|unless|provided that|while)\b|(?:(?<![应相适])当|如果|若|の場合|場合は|場合に|際に|際は|とき|時(?=[、，:：\s]|は|に)|条件下|条件を満たす場合)", re.I)
_EXCEPTION = re.compile(r"\b(?:except|unless|exception|NA only)\b|(?:除外|例外|但し|ただし|除非|例外情况)", re.I)
_REFERENCE = re.compile(r"\b(?:refer(?:ence)?\s+to|as\s+specified\s+in|defined\s+in)\b|(?:参照|引用|依据|従う)", re.I)
_IDENT = re.compile(r"(?<![A-Za-z0-9_])(?:REQ[-_:][A-Z0-9][A-Z0-9._:-]*[A-Z0-9]|UC[-_][A-Z0-9_-]{4,})(?![A-Za-z0-9_])", re.I)
_TIMING = re.compile(r"(?<![A-Za-z0-9])\d+(?:\.\d+)?\s*(?:msec|ms|seconds?|secs?|sec|s|min(?:utes?)?|毫秒|秒|分钟|分)(?![A-Za-z])", re.I)
# Recognize evidence of values and comparators; these are NOT normalized or
# interpreted as numeric constraints without human or rule-engine review.
_QUANTITY = re.compile(
    r"(?:<=|>=|==|≤|≥|<|>|=|±)\s*-?\d+(?:\.\d+)?(?:\s*(?:ms|msec|sec|s|kB|MB|GB|ePF|V|mA|%))?\b|"
    r"(?<![A-Za-z0-9])\d+(?:\.\d+)?\s*(?:kB|MB|GB|ePF|mA|mV|V|%)(?![A-Za-z])",
    re.I,
)
_MAX_CUES = 96


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip()


@dataclass(frozen=True, slots=True)
class RequirementSignals:
    normative: bool
    conditional: bool
    exception: bool
    timing: tuple[str, ...]
    reference_cue: bool
    identifiers: tuple[str, ...]
    # v2 fields are additive within the existing JSONB signals column.
    modality: str = "NONE"
    structural_kind: str = "PROSE"
    quantity_cues: tuple[str, ...] = ()
    # (cue_kind, start_in_unit, end_in_unit, exact_source_text)
    cue_spans: tuple[tuple[str, int, int, str], ...] = ()
    table_cells: tuple[tuple[int, int, str], ...] = ()


@dataclass(frozen=True, slots=True)
class RequirementUnit:
    unit_key: str
    document_id: str
    content_id: int
    section_id: str
    page_number: int | None
    start_offset: int
    end_offset: int
    text: str
    text_hash: str
    unit_type: str
    signals: RequirementSignals

    @property
    def evidence(self) -> dict:
        return {
            "document_id": self.document_id,
            "content_id": self.content_id,
            "section_id": self.section_id,
            "page_number": self.page_number,
            "start_offset": self.start_offset,
            "end_offset": self.end_offset,
            "source_hash": self.text_hash,
        }


def _structure(line: str) -> str:
    if _TABLE_RULE.fullmatch(line):
        return "TABLE_SEPARATOR"
    if "\t" in line or "|" in line or "│" in line:
        return "TABLE_ROW"
    if _BULLET.match(line):
        return "LIST_ITEM"
    if _HEADING.match(line):
        return "HEADING"
    return "PROSE"


def _table_cells(line: str) -> tuple[tuple[int, int, str], ...]:
    """Capture literal cell coordinates, without assigning semantics/headers."""
    dividers = tuple(re.finditer(r"[\t|│]", line))
    if not dividers:
        return ()
    edges = [0] + [d.start() + 1 for d in dividers]
    ends = [d.start() for d in dividers] + [len(line)]
    cells: list[tuple[int, int, str]] = []
    for left, right in zip(edges, ends):
        segment = line[left:right]
        start = left + len(segment) - len(segment.lstrip())
        end = right - (len(segment) - len(segment.rstrip()))
        cells.append((start, end, line[start:end]))
    # Surrounding pipes are table formatting rather than actual blank cells.
    if line.lstrip().startswith(("|", "│")) and cells and not cells[0][2]:
        cells.pop(0)
    if line.rstrip().endswith(("|", "│")) and cells and not cells[-1][2]:
        cells.pop()
    return tuple(cells)


def _source_signals(literal: str) -> RequirementSignals:
    normative = bool(_NORMATIVE.search(literal) or _PROHIBITED.search(literal))
    if _PROHIBITED.search(literal):
        modality = "PROHIBITED"
    elif _MANDATORY.search(literal):
        modality = "MANDATORY"
    elif _RECOMMENDED.search(literal):
        modality = "RECOMMENDED"
    else:
        modality = "NONE"
    cue_spans: list[tuple[str, int, int, str]] = []
    # Deliberately do not interpret extracted tokens as engineering rules.
    for kind, pattern in (
        ("NORMATIVE", _NORMATIVE),
        ("CONDITION", _CONDITION),
        ("EXCEPTION", _EXCEPTION),
        ("REFERENCE", _REFERENCE),
        ("IDENTIFIER", _IDENT),
        ("TIMING", _TIMING),
        ("QUANTITY", _QUANTITY),
    ):
        for match in pattern.finditer(literal):
            if len(cue_spans) >= _MAX_CUES:
                raise ValueError("Too many source cues in a single requirement unit; review source structure")
            cue_spans.append((kind, match.start(), match.end(), match.group(0)))
    cue_spans.sort(key=lambda entry: (entry[1], entry[2], entry[0]))
    return RequirementSignals(
        normative=normative,
        conditional=bool(_CONDITION.search(literal)),
        exception=bool(_EXCEPTION.search(literal)),
        timing=tuple(dict.fromkeys(m.group(0) for m in _TIMING.finditer(literal)))[:12],
        reference_cue=bool(_REFERENCE.search(literal)),
        identifiers=tuple(dict.fromkeys(m.group(0).upper() for m in _IDENT.finditer(literal)))[:12],
        modality=modality,
        structural_kind=_structure(literal),
        quantity_cues=tuple(dict.fromkeys(m.group(0) for m in _QUANTITY.finditer(literal)))[:24],
        cue_spans=tuple(cue_spans),
        table_cells=_table_cells(literal) if _structure(literal) in ("TABLE_ROW", "TABLE_SEPARATOR") else (),
    )


def validate_extracted_units(text: str, units: tuple[RequirementUnit, ...]) -> None:
    """Fail closed if any non-whitespace source text is lost or evidence drifts.

    Checks run in O(source characters + cues), without rewriting original text.
    Offsets remain *Python Unicode code-point offsets*, not PDF coordinates.
    """
    last = 0
    for unit in units:
        start, end = unit.start_offset, unit.end_offset
        if not (last <= start < end <= len(text)):
            raise ValueError("Invalid or overlapping requirement source offsets")
        if text[last:start].strip():
            raise ValueError("Non-whitespace source text omitted from extraction")
        literal = text[start:end]
        if literal != unit.text:
            raise ValueError("Requirement text does not match its source span")
        digest = hashlib.sha256(normalize_text(literal).encode("utf-8")).hexdigest()
        if digest != unit.text_hash:
            raise ValueError("Requirement source text hash mismatch")
        for left, right, value in unit.signals.table_cells:
            if not (0 <= left <= right <= len(literal)) or literal[left:right] != value:
                raise ValueError("Table cell coordinates do not match verbatim source")
        for kind, left, right, value in unit.signals.cue_spans:
            if not (0 <= left < right <= len(literal)) or literal[left:right] != value:
                raise ValueError(f"Source cue {kind} does not match verbatim evidence")
        last = end
    if text[last:].strip():
        raise ValueError("Trailing non-whitespace source text omitted from extraction")


def extract_requirement_units(
    *, document_id: str, content_id: int, section_id: str, text: str,
    page_number: int | None = None, max_units: int = 200,
    max_source_chars: int = 2_000_000,
) -> tuple[RequirementUnit, ...]:
    """Extract non-lossy physical source spans with cues and structural labels.

    A physical line remains intact: splitting on semicolons/sentences would
    incorrectly split engineering conditions. PDF text without line breaks
    remains one source unit and is flagged for downstream review when necessary.
    """
    if max_units < 1 or max_source_chars < 1:
        raise ValueError("Invalid requirement extraction resource limits")
    if not document_id or content_id <= 0 or not text:
        return ()
    if len(text) > max_source_chars:
        raise ValueError(
            f"Source content {content_id} exceeds {max_source_chars} characters; "
            "review/split upstream content rather than truncating evidence"
        )
    results: list[RequirementUnit] = []
    for match in _LINE.finditer(text):
        line = match.group(0)
        if not line.strip():
            continue
        start = match.start() + (len(line) - len(line.lstrip()))
        end = match.end() - (len(line) - len(line.rstrip()))
        if start >= end:
            continue
        if len(results) >= max_units:
            raise ValueError(
                f"Source content {content_id} exceeds {max_units} requirement units; "
                "split the upstream chunk or increase the reviewed bound"
            )
        literal = text[start:end]
        digest = hashlib.sha256(normalize_text(literal).encode("utf-8")).hexdigest()
        unit_key = hashlib.sha256(
            f"{document_id}|{content_id}|{start}|{end}|{digest}".encode("utf-8")
        ).hexdigest()
        signals = _source_signals(literal)
        kind = (
            "TABLE_ROW" if signals.structural_kind in ("TABLE_ROW", "TABLE_SEPARATOR") else
            "REQUIREMENT" if signals.normative else
            "REFERENCE" if signals.reference_cue else "SOURCE_TEXT"
        )
        results.append(RequirementUnit(
            unit_key=unit_key, document_id=document_id, content_id=content_id,
            section_id=section_id, page_number=page_number,
            start_offset=start, end_offset=end, text=literal,
            text_hash=digest, unit_type=kind, signals=signals,
        ))
    found = tuple(results)
    validate_extracted_units(text, found)
    return found
