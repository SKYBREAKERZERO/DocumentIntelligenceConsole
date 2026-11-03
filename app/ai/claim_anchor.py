"""Deterministic parameter/version grounding for enterprise answers.

Not a semantic entailment engine. This gate rejects some objectively unsupported
literal engineering facts, but *passing* never certifies causal, temporal or
normative truth. Data is taken from the actual admitted evidence objects, not
from the question, model-authored text, or synthetic prompt headers.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Iterable

from app.ai.answer_integrity import AnswerValidation

# Bound to an explicit physical unit to avoid mistaking spec/section numbers,
# ordering indices, citation IDs and page numbers for measured requirements.
_NUMERIC_UNIT = re.compile(
    r"(?<![A-Za-z0-9_.])(?P<number>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>毫秒|秒|分钟|小时|℃|°C|°F|kHz|MHz|Hz|mV|kV|V|mA|A|dB|ms|msec|sec|s|%|％)"
    r"(?![a-zA-Z\d])", re.IGNORECASE,
)
_VERSION = re.compile(r"(?<![a-zA-Z0-9])v\s*(\d+(?:\.\d+){1,3})(?![\w.])", re.IGNORECASE)
_CITE = re.compile(r"(?<!\w)\[([1-9]\d{0,5})\]")
_UNIT_FACTORS = {
    "ms": ("time_ms", "1"), "msec": ("time_ms", "1"), "毫秒": ("time_ms", "1"),
    "s": ("time_ms", "1000"), "sec": ("time_ms", "1000"), "秒": ("time_ms", "1000"),
    "分钟": ("time_ms", "60000"), "小时": ("time_ms", "3600000"),
    "hz": ("frequency_hz", "1"), "khz": ("frequency_hz", "1000"),
    "mhz": ("frequency_hz", "1000000"),
    "mv": ("voltage_mv", "1"), "v": ("voltage_mv", "1000"),
    "kv": ("voltage_mv", "1000000"),
    "ma": ("current_ma", "1"), "a": ("current_ma", "1000"),
    "%": ("percent", "1"), "％": ("percent", "1"),
    "db": ("decibel", "1"), "°c": ("temperature_c", "1"), "℃": ("temperature_c", "1"),
    "°f": ("temperature_f", "1"),
}


def _measurements(text: str) -> set[tuple[str, Decimal]]:
    result: set[tuple[str, Decimal]] = set()
    for match in _NUMERIC_UNIT.finditer(text):
        dimension, factor = _UNIT_FACTORS[match.group("unit").lower()]
        try:
            result.add((dimension, Decimal(match.group("number")) * Decimal(factor)))
        except InvalidOperation:
            continue
    return result


# Limited, literal ON/OFF association check. No inferred engineering
# condition precedence, no QA conformance inference.
_STATE = re.compile(r"(?<![A-Za-z0-9])(?:ON|OFF)(?![A-Za-z0-9])", re.I)


def _unambiguous_state_measurements(text: str) -> set[tuple[str, str, Decimal]]:
    pairs: set[tuple[str, str, Decimal]] = set()
    for clause in re.split(r"[。；;\n]+|(?<!\d)\.(?!\d)", text):
        states = {m.group(0).upper() for m in _STATE.finditer(clause)}
        if len(states) == 1:
            pairs.update((next(iter(states)), dimension, number)
                         for dimension, number in _measurements(clause))
    return pairs


def _versions(text: str) -> set[str]:
    # Only explicit v-prefixed versions are treated as version assertions:
    # unqualified 4.1 can be a chapter number and is intentionally excluded.
    return {match.group(1) for match in _VERSION.finditer(text)}


def _claim_segments(answer: str) -> Iterable[str]:
    """Check each fact-bearing line; references must occur on that line.

    The caller has already checked paragraph/line/sentence citation structure.
    Keep citations after Chinese sentence-final punctuation on the same segment.
    """
    for line in answer.splitlines():
        if not line.strip():
            continue
        # Preserve scope for trailing citations, without inferring evidence
        # availability from any citation-like text in a source document.
        normalized = re.sub(
            r"([。！？!?])([ \t]*(?:\[[1-9]\d*\][ \t]*)+)", r" \2\1", line,
        )
        for segment in re.split(r"(?<=[。！？!?])\s*", normalized):
            if segment.strip():
                yield segment


def validate_claim_anchors(
    answer: str,
    *,
    admitted_evidence: Iterable[object],
    allowed_ids: Iterable[str],
) -> AnswerValidation:
    """Fail closed when cited evidence has no asserted unit/value or v-version.

    No inference about implication, negation, counterfactuals, completeness,
    or causal engineering relationships is made. An explicit source quote
    cannot be faked by the untrusted model because evidence objects are passed
    separately from the answer string and allow-list.
    """
    allowed = frozenset(str(x) for x in allowed_ids)
    anchors: dict[str, tuple[set[tuple[str, Decimal]], set[str]]] = {}
    scoped_anchors: dict[str, set[tuple[str, str, Decimal]]] = {}
    for item in admitted_evidence:
        evidence_id = str(getattr(item, "evidence_id", ""))
        if evidence_id not in allowed:
            continue
        source = "\n".join((
            str(getattr(item, "content", "") or ""),
            str(getattr(item, "citation_label", "") or ""),
        ))
        anchors[evidence_id] = (_measurements(source), _versions(source))
        scoped_anchors[evidence_id] = _unambiguous_state_measurements(source)
    if set(allowed) - anchors.keys():
        return AnswerValidation(False, "MISSING_ADMITTED_EVIDENCE_OBJECT")
    for segment in _claim_segments(answer):
        claim_text = _CITE.sub("", segment)
        measures = _measurements(claim_text)
        versions = _versions(claim_text)
        if not measures and not versions:
            continue
        local_ids = set(_CITE.findall(segment)) & allowed
        cited_measurements: set[tuple[str, Decimal]] = set()
        cited_versions: set[str] = set()
        for citation in local_ids:
            values, source_versions = anchors[citation]
            cited_measurements.update(values)
            cited_versions.update(source_versions)
        if not measures.issubset(cited_measurements):
            return AnswerValidation(False, "UNSUPPORTED_PARAMETER_OR_UNIT", tuple(sorted(local_ids)))
        if not versions.issubset(cited_versions):
            return AnswerValidation(False, "UNSUPPORTED_SPEC_VERSION", tuple(sorted(local_ids)))
        # If the source exposes a single-state clause association, a cited
        # measurement from the opposite state must not be transplanted into
        # this one. Ambiguous source clauses are NOT certified semantically.
        claim_states = {x.group(0).upper() for x in _STATE.finditer(claim_text)}
        if measures and len(claim_states) == 1:
            state = next(iter(claim_states))
            known_pairs = set().union(*(scoped_anchors.get(i, set()) for i in local_ids))
            relevant = {(dim, val) for st, dim, val in known_pairs if st == state}
            if relevant and not measures.issubset(relevant):
                return AnswerValidation(False, "WRONG_ON_OFF_CONDITION_PARAMETER", tuple(sorted(local_ids)))
    return AnswerValidation(True, "LITERAL_PARAMETER_ANCHORS_PRESENT_NOT_SEMANTIC_PROOF")


_EXHAUSTIVE = re.compile(
    r"(?:所有|全部|完整列出|完整清单|全量|逐条列出|一条不漏|穷尽|"
    r"all\s+(?:the\s+)?(?:requirements|conditions|exceptions|clauses)|"
    r"(?:complete|exhaustive)\s+list)", re.IGNORECASE,
)


def requires_exhaustive_coverage(question: str) -> bool:
    """A bounded Top-K search is not proof of exhaustive clause coverage."""
    return bool(_EXHAUSTIVE.search(str(question or "")))


EXHAUSTIVE_UNVERIFIED_ANSWER = (
    "证据覆盖不足：当前工程正文检索使用有限条目召回，不能证明已经找到全部要求、"
    "条件、例外或表格。为避免遗漏关键工程约束，本次不生成声称完整的清单。"
    "请限定式样书及章节，并通过完整正文/要求索引覆盖率审计后再进行逐条核对。"
)
