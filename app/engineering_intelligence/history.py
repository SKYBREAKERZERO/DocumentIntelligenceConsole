"""Conservative alignment of source-backed requirements between revisions.

Exact identities are matched first. Approximate matches remain REVIEW_REQUIRED;
this layer never asserts that a changed engineering condition is equivalent.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from difflib import SequenceMatcher

from .requirements import RequirementUnit, normalize_text


@dataclass(frozen=True, slots=True)
class UnitMapping:
    old_key: str | None
    new_key: str | None
    change_type: str
    similarity: float
    review_status: str


def align_requirement_versions(
    old: tuple[RequirementUnit, ...], new: tuple[RequirementUnit, ...], *,
    threshold: float = 0.72, max_comparisons: int = 20000,
) -> tuple[UnitMapping, ...]:
    """Deterministic, bounded one-to-one matches across different version sets.

    O((n+m) + pair_budget) rather than unbounded O(n*m). For identical
    content in multiple sections, the same section ID gets preference.
    """
    if not (0.0 <= threshold <= 1.0) or max_comparisons < 0:
        raise ValueError("Invalid alignment bounds")
    result: list[UnitMapping] = []
    used_new: set[int] = set()
    used_old: set[int] = set()
    # Deques avoid quadratic rescanning when thousands of rows share the
    # same boilerplate text/hash. Prefer exact text in the same section.
    by_hash: dict[str, deque[int]] = defaultdict(deque)
    by_hash_section: dict[tuple[str, str], deque[int]] = defaultdict(deque)
    for j, unit in enumerate(new):
        by_hash[unit.text_hash].append(j)
        by_hash_section[(unit.text_hash, unit.section_id)].append(j)

    for i, left in enumerate(old):
        same_section = by_hash_section.get((left.text_hash, left.section_id))
        general = by_hash.get(left.text_hash)
        j: int | None = None
        for queue in (same_section, general):
            if not queue:
                continue
            while queue and queue[0] in used_new:
                queue.popleft()
            if queue:
                j = queue.popleft()
                break
        if j is None:
            continue
        used_old.add(i)
        used_new.add(j)
        relation = "UNCHANGED" if left.section_id == new[j].section_id else "MOVED"
        result.append(UnitMapping(left.unit_key, new[j].unit_key, relation, 1.0, "DETERMINISTIC"))

    # Pre-index by section and stable requirement identifiers; no O(n*m)
    # scan over unrelated sections. Never label omitted comparisons as fact.
    section_index: dict[str, list[int]] = defaultdict(list)
    id_index: dict[str, list[int]] = defaultdict(list)
    for j, right in enumerate(new):
        if j in used_new:
            continue
        section_index[right.section_id].append(j)
        for ident in right.signals.identifiers:
            id_index[ident].append(j)
    candidates: list[tuple[float, int, int]] = []
    comparisons = 0
    for i, left in enumerate(old):
        if i in used_old:
            continue
        related: set[int] = set(section_index.get(left.section_id, ()))
        for ident in left.signals.identifiers:
            related.update(id_index.get(ident, ()))
        left_text = normalize_text(left.text)[:1200]
        for j in sorted(related):
            if j in used_new:
                continue
            if comparisons >= max_comparisons:
                raise ValueError("Version alignment candidate budget exceeded; refusing incomplete change classification")
            comparisons += 1
            score = SequenceMatcher(None, left_text, normalize_text(new[j].text)[:1200], autojunk=True).ratio()
            if score >= threshold:
                candidates.append((score, i, j))
    for score, i, j in sorted(candidates, key=lambda row: (-row[0], row[1], row[2])):
        if i in used_old or j in used_new:
            continue
        used_old.add(i)
        used_new.add(j)
        result.append(UnitMapping(old[i].unit_key, new[j].unit_key, "MODIFIED", round(score, 4), "REVIEW_REQUIRED"))
    for i, unit in enumerate(old):
        if i not in used_old:
            result.append(UnitMapping(unit.unit_key, None, "REMOVED", 0, "DETERMINISTIC"))
    for j, unit in enumerate(new):
        if j not in used_new:
            result.append(UnitMapping(None, unit.unit_key, "ADDED", 0, "DETERMINISTIC"))
    return tuple(result)
