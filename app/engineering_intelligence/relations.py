"""Source-to-source relationship proposals with bounded explicit anchors.

No embedding similarity alone can establish a dependency or conflict. Both
endpoints must exist in the same project database and point to source spans.
Candidates are never automatically marked CONFIRMED.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from .requirements import RequirementUnit, normalize_text

_SPEC_REFERENCE = re.compile(
    r"(?:\b(?:refer(?:ence)?\s+to|according\s+to|defined\s+in|specified\s+in|see|spec(?:ification)?(?:\s+no\.?)?)\b|"
    r"参照|引用|依据|準拠|根据)\s*(?:the\s+)?(?:spec(?:ification)?\s*)?"
    r"(?P<code>[A-Z]{0,3}\d{3,6})(?![A-Za-z0-9])", re.I
)
_SECTION_REFERENCE = re.compile(r"(?:section|sec\.?|§|章节|節)\s*([1-9]\d*(?:\.\d+){0,6})", re.I)
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}|[\u4e00-\u9fff]{2,4}")
# A spec reference is meaningful only inside the same sentence/clause.
# Avoid a free-floating section elsewhere in a paragraph from being attached
# to a previously seen ECU/specification code. Decimal section dots and "Sec."
# are not sentence boundaries.
_CLAUSE_END = re.compile(r"[。！？;；\r\n]|[.!?](?=\s+(?:[A-Za-z][a-z]+|[\u4e00-\u9fff]))")
MAX_ANCHOR_DISTANCE = 120


@dataclass(frozen=True, slots=True)
class ReferenceAnchor:
    specification_code: str
    section_id: str
    start_offset: int
    end_offset: int
    raw_text: str


def extract_reference_anchors(source_text: str) -> tuple[ReferenceAnchor, ...]:
    """Find verifiable same-clause SPEC + section pairs, with source-exact spans.

    The anchor is *a reference*, not proof of dependency, compatibility,
    requirement equivalence, causality or functional influence. A clause with
    two section anchors for one reference is ambiguous and yields no edge.
    """
    refs = list(_SPEC_REFERENCE.finditer(source_text))
    anchors: list[ReferenceAnchor] = []
    for ix, ref in enumerate(refs):
        end = min(len(source_text), ref.end() + MAX_ANCHOR_DISTANCE)
        if ix + 1 < len(refs):
            end = min(end, refs[ix + 1].start())
        boundary = _CLAUSE_END.search(source_text, ref.end(), end)
        if boundary:
            end = boundary.start()
        sections = list(_SECTION_REFERENCE.finditer(source_text, ref.end(), end))
        if len(sections) != 1:
            continue
        section = sections[0]
        anchors.append(ReferenceAnchor(
            specification_code=ref.group('code').upper(), section_id=section.group(1),
            start_offset=ref.start(), end_offset=section.end(),
            raw_text=source_text[ref.start():section.end()],
        ))
    return tuple(anchors)


def find_reference_anchor(source_text: str, target_code: str,
                          target_section: str) -> ReferenceAnchor | None:
    """An exact source-text anchor, or None (never synthesize a target)."""
    code = target_code.strip().upper()
    section = target_section.strip().lstrip('§').strip()
    anchors = (anchor for anchor in extract_reference_anchors(source_text)
               if anchor.specification_code == code and anchor.section_id == section)
    return next(anchors, None)



def keyword_overlap(left: str, right: str) -> float:
    a = set(t.lower() for t in _WORD.findall(normalize_text(left)[:1200]))
    b = set(t.lower() for t in _WORD.findall(normalize_text(right)[:1200]))
    return len(a & b) / max(len(a | b), 1)


@dataclass(frozen=True, slots=True)
class RelationCandidate:
    candidate_key: str
    source_unit_key: str
    target_unit_key: str
    source_spec_code: str
    target_spec_code: str
    relation_type: str
    score: float
    status: str
    reason: str


def link_explicit_references(
    *, source_spec_code: str, source_units: tuple[RequirementUnit, ...],
    target_spec_code: str, target_units: tuple[RequirementUnit, ...],
    max_candidates_per_source: int = 3, minimum_overlap: float = 0.06,
) -> tuple[RelationCandidate, ...]:
    """Connect spans only when the source explicitly names the target spec.

    A document-level reference with no *unique* target requirement returns no edge;
    the broad relation remains in the old spec_relations registry. This avoids
    manufacturing a paragraph-to-paragraph dependency out of a vector score.
    """
    if not source_spec_code or not target_spec_code or max_candidates_per_source < 1:
        return ()
    target_code = target_spec_code.strip().upper()
    candidates: list[RelationCandidate] = []
    for source in source_units:
        section_anchors = {a.section_id for a in extract_reference_anchors(source.text)
                           if a.specification_code == target_code}
        if len(section_anchors) > max_candidates_per_source:
            raise ValueError("Explicit reference anchors exceed per-source review budget; refusing partial mapping")
        for requested_section in sorted(section_anchors):
            matches = [unit for unit in target_units
                       if unit.section_id.strip().lstrip('§').strip() == requested_section]
            # A section with more than one requirement is not a unique
            # requirement-to-requirement mapping. Do NOT select an arbitrary
            # paragraph using lexical overlap, even with a high similarity.
            if len(matches) != 1:
                continue
            target = matches[0]
            score = 0.65 + 0.35 * keyword_overlap(source.text, target.text)
            key = hashlib.sha256(f"{source.unit_key}|{target.unit_key}|REFERENCES".encode()).hexdigest()
            candidates.append(RelationCandidate(
                candidate_key=key, source_unit_key=source.unit_key, target_unit_key=target.unit_key,
                source_spec_code=source_spec_code, target_spec_code=target_spec_code,
                relation_type="REFERENCES_SECTION", score=round(score, 4),
                status="REVIEW_REQUIRED", reason="EXPLICIT_SPEC_AND_SECTION_ANCHOR",
            ))
    return tuple(candidates)
