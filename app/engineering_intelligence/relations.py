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

    A document-level reference with no precise target section returns no edge;
    the broad relation remains in the old spec_relations registry. This avoids
    manufacturing a paragraph-to-paragraph dependency out of a vector score.
    """
    if not source_spec_code or not target_spec_code or max_candidates_per_source < 1:
        return ()
    target_code = target_spec_code.upper()
    candidates: list[RelationCandidate] = []
    for source in source_units:
        refs = list(_SPEC_REFERENCE.finditer(source.text))
        section_anchors: set[str] = set()
        for index, ref in enumerate(refs):
            if ref.group("code").upper() != target_code:
                continue
            # Associate each section ONLY with its immediately preceding spec
            # reference, never a different code in the same paragraph.
            segment_end = refs[index+1].start() if index + 1 < len(refs) else len(source.text)
            section = _SECTION_REFERENCE.search(source.text[ref.end():segment_end])
            if section:
                section_anchors.add(section.group(1))
        for requested_section in sorted(section_anchors):
            matches = [unit for unit in target_units if unit.section_id.strip().lstrip('§') == requested_section]
            if not matches:
                continue
            ranked = sorted(
                ((0.65 + 0.35 * keyword_overlap(source.text, unit.text), unit) for unit in matches),
                key=lambda item: (-item[0], item[1].start_offset, item[1].unit_key),
            )
            for score, target in ranked[:max_candidates_per_source]:
                key = hashlib.sha256(f"{source.unit_key}|{target.unit_key}|REFERENCES".encode()).hexdigest()
                candidates.append(RelationCandidate(
                    candidate_key=key, source_unit_key=source.unit_key, target_unit_key=target.unit_key,
                    source_spec_code=source_spec_code, target_spec_code=target_spec_code,
                    relation_type="REFERENCES_SECTION", score=round(score, 4),
                    status="REVIEW_REQUIRED", reason="EXPLICIT_SPEC_AND_SECTION_ANCHOR",
                ))
    return tuple(candidates)
