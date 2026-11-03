"""Stage 2/3 read-only source validation and explicit query selectors.

An explicit section citation is structural evidence only, never a confirmed
engineering dependency. Version changes must come from persisted, READY,
body-to-body diff records and be independently hash verified.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

from .relations import find_reference_anchor
from .requirements import normalize_text

_SECTION = re.compile(
    r"(?:section|sec\.?|§|章节|章節|第\s*)(?:\s*|[:：])([1-9]\d*(?:\.\d+){0,6})(?:\s*章)?",
    re.I,
)
_KEYWORD = re.compile(r"(?:关键词|關鍵詞|关键字|keyword)\s*[:：=]\s*([^,，。；;\s]{2,60})", re.I)


def section_selector(question: str) -> str:
    """Only explicit section syntax is accepted; version 2.52 is not §2.52."""
    m = _SECTION.search(question or "")
    return m.group(1) if m else ""


def keyword_selector(question: str) -> str:
    """Only operator-explicit keyword filters; no inferred fuzzy matching."""
    m = _KEYWORD.search(question or "")
    return m.group(1).casefold() if m else ""


def verify_requirement_span(row: dict[str, Any], side: str) -> bool:
    """Verify full Unicode offsets, original bytes/text and normalized unit hash."""
    try:
        raw = row[f"{side}_text"]
        chunk = row[f"{side}_chunk"]
        start, end = int(row[f"{side}_start"]), int(row[f"{side}_end"])
        claimed = str(row[f"{side}_hash"] or "").strip()
        return (
            isinstance(raw, str) and bool(raw) and isinstance(chunk, str)
            and 0 <= start < end <= len(chunk)
            and chunk[start:end] == raw
            and hashlib.sha256(normalize_text(raw).encode("utf-8")).hexdigest() == claimed
        )
    except (KeyError, TypeError, ValueError):
        return False


def verify_section_reference(source: dict[str, Any], target: dict[str, Any]) -> bool:
    """Do not derive an engineering dependency from a spec/section citation."""
    return bool(
        verify_requirement_span(source, "source")
        and verify_requirement_span(target, "target")
        and str(source.get("source_document")) != ""
        and str(target.get("target_document")) != ""
        and find_reference_anchor(
            str(source["source_text"]), str(target["target_code"]),
            str(target["target_section"]),
        ) is not None
    )


def verify_persisted_diff(row: dict[str, Any]) -> bool:
    """Check raw, unnormalized SHA-256 used by SectionSnapshot.text_hash.

    A missing hash on a populated endpoint is not silently trusted. ADDED and
    REMOVED legitimately have only one endpoint.
    """
    kind = str(row.get("change_type") or "")
    if kind not in {"ADDED", "REMOVED", "MODIFIED", "MOVED"}:
        return False
    required = {"ADDED": (False, True), "REMOVED": (True, False)}.get(kind, (True, True))
    present = []
    for side, must_exist in zip(("old", "new"), required):
        raw, digest = row.get(f"{side}_text"), row.get(f"{side}_hash")
        exists = raw is not None and digest is not None
        if must_exist and not exists:
            return False
        if exists and hashlib.sha256(str(raw).encode("utf-8")).hexdigest() != str(digest).strip():
            return False
        present.append(exists)
    return any(present)


def matches_change_selector(row: dict[str, Any], section: str, keyword: str) -> bool:
    if section:
        section_id = str(row.get("section_id") or "").strip().lstrip("§")
        chapter_id = str(row.get("chapter_id") or "").strip().lstrip("§")
        if not (section_id == section or section_id.startswith(section + ".") or chapter_id == section):
            return False
    if keyword:
        haystack = " ".join(str(row.get(field) or "") for field in (
            "old_text", "new_text", "section_title", "chapter_title",
        )).casefold()
        if keyword not in haystack:
            return False
    return True

# v1.40: precise relationships between separately verified spans of one
# authoritative, immutable version.  An extraction order is not a causal edge.
_SECTION_PATH = re.compile(r"^[1-9]\d*(?:\.[1-9]\d*){0,7}$")


def structural_unit_pair(a: dict[str, Any], b: dict[str, Any], *,
                         consecutive_in_chunk: bool = False) -> str:
    """Classify only exact textual layout facts; never assert dependency.

    Input is independently hydrated from PRIMARY/SPEC_BODY documents.  The
    caller must determine whether two spans are consecutive *indexed units*
    within one chunk; we do not guess physical adjacency from numeric ids.
    """
    if not (verify_requirement_span(a, 'source') and verify_requirement_span(b, 'target')):
        return ''
    if (not a.get('source_document') or a['source_document'] != b.get('target_document')
            or not a.get('source_version_id') or a['source_version_id'] != b.get('target_version_id')
            or not a.get('source_code') or a['source_code'] != b.get('target_code')
            or a.get('source_unit_id') == b.get('target_unit_id')):
        return ''
    sa = str(a.get('source_section') or '').strip()
    sb = str(b.get('target_section') or '').strip()
    if not _SECTION_PATH.fullmatch(sa) or not _SECTION_PATH.fullmatch(sb):
        return ''
    if sa == sb:
        if (consecutive_in_chunk
                and a.get('source_content_id') == b.get('target_content_id')
                and int(a['source_end']) <= int(b['target_start'])):
            return 'ADJACENT_INDEXED_UNITS'
        return 'SAME_SECTION'
    if sb.startswith(sa + '.') and sb.count('.') == sa.count('.') + 1:
        return 'PARENT_CHILD_SECTION'
    return ''


def join_reference_and_diff(reference: Any, diff: Any) -> dict[str, Any] | None:
    """Join two already verified records; only flags need for revalidation.

    A historic text diff cannot by itself invalidate a source citation.  Match
    scope, actual target specification identity, exact section, target body
    document and the destination version to prevent cross-version leakage.
    """
    if (getattr(reference, 'source_type', '') != 'STRUCTURAL_REFERENCE'
            or getattr(diff, 'source_type', '') != 'CHAPTER_VERSION_DIFF'
            or getattr(reference, 'scope', None) != getattr(diff, 'scope', None)):
        return None
    rmeta, dmeta = reference.metadata or {}, diff.metadata or {}
    target = rmeta.get('target') or {}
    section = str(target.get('section') or '').strip()
    changed_section = str(getattr(diff, 'section_id', '') or '').strip()
    if not (section and changed_section and
            (section == changed_section or changed_section.startswith(section + '.'))):
        return None
    if (str(target.get('spec_code') or '') != str(getattr(diff, 'specification_code', '') or '')
            or str(target.get('version') or '') != str(dmeta.get('to_version') or '')
            or str(dmeta.get('to_document') or '') == ''):
        return None
    units = target.get('units') or []
    if not units or any(str(u.get('document_id') or '') != str(dmeta['to_document'])
                        for u in units):
        return None
    if not rmeta.get('source', {}).get('text_hash') or not dmeta.get('change_id'):
        return None
    return {
        'status': 'REVALIDATION_REQUIRED',
        'interpretation_limit': 'HISTORICAL_TARGET_TEXT_CHANGE_NOT_RELATION_INVALIDATION',
        'reference_source_hash': rmeta['source']['text_hash'],
        'target_spec_code': target['spec_code'],
        'target_section': section,
        'changed_section': changed_section,
        'from_version': dmeta['from_version'],
        'to_version': dmeta['to_version'],
        'change_id': dmeta['change_id'],
        'change_set_id': dmeta['change_set_id'],
        'old_hash': dmeta.get('old_hash'),
        'new_hash': dmeta.get('new_hash'),
    }
