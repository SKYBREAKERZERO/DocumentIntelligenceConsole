"""Stage-1 atomic *candidates* with exact provenance, not approved requirements.

Only visible top-level punctuation is a candidate boundary. Logical connectors,
exception inheritance and condition scope are deliberately left for review.
This module never uses a remote model, fuzzy paraphrase or hidden source text.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .requirements import RequirementUnit, _source_signals

ATOMIC_VERSION = "atomic-candidates-v1"
_CLOSERS = {"(": ")", "（": "）", "[": "]", "【": "】", "{": "}", "「": "」", "『": "』"}
_QUOTES = {'"', "“", "”", "'"}
_SEPARATORS = ";；。"


@dataclass(frozen=True, slots=True)
class AtomicCandidate:
    candidate_key: str
    parent_unit_key: str
    document_id: str
    content_id: int
    start_in_unit: int
    end_in_unit: int
    start_in_content: int
    end_in_content: int
    text: str
    text_hash: str
    role: str              # REQUIREMENT_CANDIDATE | CONTEXT | TABLE_CONTEXT
    review_status: str     # REVIEW_REQUIRED | SOURCE_ONLY
    review_reasons: tuple[str, ...]
    cue_spans: tuple[tuple[str, int, int, str], ...]


def _boundary_candidates(text: str) -> tuple[int, ...]:
    """Find top-level punctuation; be conservative with nesting and quotes.

    Apostrophes in English words must not open a quoted span. Unbalanced
    brackets/quotes cause a single, reviewable candidate instead of guessing.
    """
    stack: list[str] = []
    quote: str | None = None
    boundaries: list[int] = []
    for idx, char in enumerate(text):
        if quote:
            if (quote == '“' and char == '”') or char == quote:
                quote = None
            continue
        if char in ('"', '“', "'") and not (char == "'" and idx and idx + 1 < len(text)
                                                 and text[idx - 1].isalpha() and text[idx + 1].isalpha()):
            quote = char
            continue
        if char in _CLOSERS:
            stack.append(_CLOSERS[char])
            continue
        if char in _CLOSERS.values():
            if not stack or stack[-1] != char:
                return ()
            stack.pop()
            continue
        if char in _SEPARATORS and not stack:
            boundaries.append(idx)
    if stack or quote:
        return ()
    return tuple(boundaries)


def extract_atomic_candidates(unit: RequirementUnit, *, max_candidates: int = 64) -> tuple[AtomicCandidate, ...]:
    """Produce exact source spans; all normative candidates need review.

    A separator becomes a split only when both sides have their own explicit
    normative cue. Otherwise the original line remains intact. Tables are
    parsed by a separate structural extractor, never by punctuation.
    """
    if max_candidates < 1:
        raise ValueError("max_candidates must be positive")
    text = unit.text
    if not text:
        return ()
    boundaries = () if unit.signals.structural_kind in ("TABLE_ROW", "TABLE_SEPARATOR") else _boundary_candidates(text)
    raw_edges = [0, *(idx + 1 for idx in boundaries), len(text)]
    raw_parts: list[tuple[int, int]] = []
    for left, right in zip(raw_edges, raw_edges[1:]):
        # Preserve punctuation with preceding segment; no source bytes deleted.
        start = left + len(text[left:right]) - len(text[left:right].lstrip())
        end = right - (len(text[left:right]) - len(text[left:right].rstrip()))
        if start < end:
            raw_parts.append((start, end))
    split = (len(raw_parts) > 1
             and all(_source_signals(text[left:right]).normative for left, right in raw_parts))
    spans = raw_parts if split else [(0, len(text))]
    if len(spans) > max_candidates:
        raise ValueError("Atomic candidate budget exceeded; no partial output")
    shared_scope = split and any(_source_signals(text[l:r]).conditional or
                                 _source_signals(text[l:r]).exception for l, r in spans)
    generated: list[AtomicCandidate] = []
    for left, right in spans:
        exact = text[left:right]
        cues = _source_signals(exact)
        role = ("TABLE_CONTEXT" if unit.signals.structural_kind in ("TABLE_ROW", "TABLE_SEPARATOR")
                else "REQUIREMENT_CANDIDATE" if cues.normative else "CONTEXT")
        reasons: list[str] = []
        if role == "REQUIREMENT_CANDIDATE":
            reasons.append("NORMATIVE_MEANING_UNVERIFIED")
            if shared_scope:
                reasons.append("CONDITION_OR_EXCEPTION_SCOPE_UNRESOLVED")
            if not split and len(boundaries) > 0:
                reasons.append("COMPOUND_BOUNDARY_NOT_SAFE")
            if sum(c[0] == "NORMATIVE" for c in cues.cue_spans) > 1:
                reasons.append("MULTIPLE_NORMATIVE_CUES")
        if role == "TABLE_CONTEXT":
            reasons.append("TABLE_SEMANTICS_REQUIRE_HEADER")
        if not split and (any(c in text for c in ('"', '“', "'", '(', '（')) or len(boundaries) > 0):
            if "COMPOUND_BOUNDARY_NOT_SAFE" not in reasons:
                reasons.append("STRUCTURAL_SCOPE_UNRESOLVED")
        digest = hashlib.sha256(exact.encode("utf-8", "surrogatepass")).hexdigest()
        key = hashlib.sha256(
            f"{ATOMIC_VERSION}|{unit.unit_key}|{left}|{right}|{digest}".encode("utf-8")
        ).hexdigest()
        generated.append(AtomicCandidate(
            candidate_key=key, parent_unit_key=unit.unit_key,
            document_id=unit.document_id, content_id=unit.content_id,
            start_in_unit=left, end_in_unit=right,
            start_in_content=unit.start_offset + left,
            end_in_content=unit.start_offset + right,
            text=exact, text_hash=digest, role=role,
            review_status="REVIEW_REQUIRED" if reasons else "SOURCE_ONLY",
            review_reasons=tuple(reasons), cue_spans=cues.cue_spans,
        ))
    validate_atomic_candidates(unit, tuple(generated))
    return tuple(generated)


def validate_atomic_candidates(unit: RequirementUnit, items: tuple[AtomicCandidate, ...]) -> None:
    if not items or len({x.candidate_key for x in items}) != len(items):
        raise ValueError("Missing or duplicate atomic candidate")
    prev = 0
    for item in items:
        left, right = item.start_in_unit, item.end_in_unit
        if not (prev <= left < right <= len(unit.text)):
            raise ValueError("Invalid or overlapping atomic offsets")
        # Whitespace may be lost from review spans; no substantive content can.
        if unit.text[prev:left].strip():
            raise ValueError("Uncovered substantive text in atomic candidates")
        if item.parent_unit_key != unit.unit_key or item.content_id != unit.content_id:
            raise ValueError("Atomic candidate source identity changed")
        if item.start_in_content != unit.start_offset + left or item.end_in_content != unit.start_offset + right:
            raise ValueError("Atomic source coordinate mismatch")
        if unit.text[left:right] != item.text:
            raise ValueError("Atomic text differs from source")
        if hashlib.sha256(item.text.encode("utf-8", "surrogatepass")).hexdigest() != item.text_hash:
            raise ValueError("Atomic source digest mismatch")
        for _, s, e, evidence in item.cue_spans:
            if not (0 <= s < e <= len(item.text)) or item.text[s:e] != evidence:
                raise ValueError("Atomic cue span mismatch")
        prev = right
    if unit.text[prev:].strip():
        raise ValueError("Uncovered trailing substantive text in atomic candidates")
