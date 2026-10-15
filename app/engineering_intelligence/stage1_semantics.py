"""Source-only Stage 1 bilingual and cross-page JSON diagnostics.

The pipeline JSON has lost native PDF coordinates. Signals here are for review,
not automatic translation equivalence, table joining or rule approval.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any

from .requirements import extract_requirement_units
from .atomic import extract_atomic_candidates
from .condition_scope import extract_requirement_scope
from .stage1_review_evidence import make_pointer

_EN_MODAL = re.compile(r"\b(?:shall|must|should|is required to)\b", re.I)
_JP_MODAL = re.compile(r"(?:しなければならない|する必要がある|すること|こととする|ものとする|してはならない|禁止|必須)")
_JP = re.compile(r"[ぁ-ゟァ-ヿ]")
_TABLE_LINE = re.compile(r"(?m)^[ \t]*[^\n]{1,400}[|│\t][^\n]{1,400}$")


def source_language(text: str) -> str:
    """Language of a *normative source unit*, never a translation judgement."""
    en, jp = bool(_EN_MODAL.search(text)), bool(_JP_MODAL.search(text))
    if en and jp:
        return "MIXED"
    if jp:
        return "JA"
    if en:
        return "EN"
    return "JA" if _JP.search(text) else "OTHER"


def inspect_requirement_signals(
    document_hash: str, contents: list[dict[str, Any]], *,
    max_items: int = 1_000, max_chars: int = 1_000_000,
    max_evidence: int = 100,
) -> dict[str, Any]:
    if not all(type(b) is int and b > 0 for b in (max_items, max_chars, max_evidence)):
        raise ValueError("Invalid semantic probe budget")
    by_language: Counter[str] = Counter()
    evidence: list[dict[str, Any]] = []
    evidence_total = 0
    section_langs: dict[str, set[str]] = {}
    scope_unresolved = scope_candidate = 0
    covered = analysed_chars = 0
    failed_extract = False
    for index, content in enumerate(contents, 1):
        body = content["text"]
        if covered >= max_items or analysed_chars + len(body) > max_chars:
            break
        covered += 1
        analysed_chars += len(body)
        try:
            units = extract_requirement_units(
                document_id=document_hash, content_id=index,
                section_id=str(content.get("section_id") or "unknown"),
                page_number=content.get("page_number") if type(content.get("page_number")) is int else None,
                text=body,
            )
        except ValueError:
            failed_extract = True
            continue
        for unit in units:
            if not unit.signals.normative:
                continue
            language = source_language(unit.text)
            by_language[language] += 1
            section_langs.setdefault(unit.section_id, set()).add(language)
            # A reviewer can locate this exact physical line in the supplied
            # export. Pairing two languages or approving their semantics is
            # deliberately out of scope.
            evidence_total += 1
            if len(evidence) < max_evidence:
                evidence.append(make_pointer(
                    document_hash, content, index, unit.start_offset, unit.end_offset,
                    category="NORMATIVE_SOURCE", status=f"LANGUAGE_{language}_UNVERIFIED",
                ))
            try:
                atoms = extract_atomic_candidates(unit)
                for atom in atoms:
                    if atom.role == "REQUIREMENT_CANDIDATE":
                        scope = extract_requirement_scope(unit, atom)
                        scope_candidate += 1
                        scope_unresolved += scope.status == "UNRESOLVED"
                        if scope.status == "UNRESOLVED":
                            evidence_total += 1
                            if len(evidence) < max_evidence:
                                evidence.append(make_pointer(
                                    document_hash, content, index,
                                    atom.start_in_content, atom.end_in_content,
                                    category="SCOPE_SOURCE", status="UNRESOLVED",
                                ))
            except ValueError:
                scope_unresolved += 1
                evidence_total += 1
                if len(evidence) < max_evidence:
                    evidence.append(make_pointer(
                        document_hash, content, index, unit.start_offset, unit.end_offset,
                        category="SCOPE_SOURCE", status="EXTRACTION_FAILED",
                    ))
    return {
        "language_counts": dict(sorted(by_language.items())),
        "bilingual_section_count": sum({"JA", "EN"} <= langs for langs in section_langs.values()),
        "bilingual_document": by_language["JA"] > 0 and by_language["EN"] > 0,
        "mixed_language_count": by_language["MIXED"],
        "scope_candidates_sampled": scope_candidate,
        "scope_unresolved_sampled": scope_unresolved,
        "probe_complete": covered == len(contents) and not failed_extract,
        "evidence_queue": evidence,
        "evidence_total": evidence_total,
        "evidence_truncated": max(0, evidence_total - len(evidence)),
    }


def pdf_table_fragment_pairs(
    contents: list[dict[str, Any]],
) -> tuple[tuple[str, int, tuple[int, int, int], tuple[int, int, int]], ...]:
    """Review-only PDF adjacent-page row signals, retaining first-row offsets.

    Each source descriptor is (1-based content index, start, end). This is
    flattened text evidence, *not* PDF cell geometry or a table merge.
    """
    row_counts: Counter[tuple[str, int]] = Counter()
    first_rows: dict[tuple[str, int], tuple[int, int, int]] = {}
    for index, content in enumerate(contents, 1):
        page = content.get("page_number")
        if type(page) is not int or not 1 <= page <= 100_000:
            continue
        sid = content.get("section_id")
        if not isinstance(sid, str) or not sid or sid == "unknown":
            continue
        text = content["text"]
        for match in _TABLE_LINE.finditer(text):
            row = match.group(0)
            if row.count("|") < 2 and row.count("│") < 2 and row.count("\t") < 2:
                continue
            key = (sid, page)
            row_counts[key] += 1
            first_rows.setdefault(key, (index, match.start(), match.end()))
    found = []
    for (sid, page), count in sorted(row_counts.items()):
        right_key = (sid, page + 1)
        if count >= 2 and row_counts[right_key] >= 2:
            found.append((sid, page, first_rows[(sid, page)], first_rows[right_key]))
    return tuple(found)


def possible_pdf_page_table_fragments(contents: list[dict[str, Any]]) -> tuple[tuple[str, int], ...]:
    """Flag contiguous-page flattened row fragments; NEVER join or imply headers."""
    return tuple((sid, page) for sid, page, _, _ in pdf_table_fragment_pairs(contents))
