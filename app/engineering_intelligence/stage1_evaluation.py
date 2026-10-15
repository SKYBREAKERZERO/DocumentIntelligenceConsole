"""Offline, source-anchored Stage1 extraction evaluation using human gold labels.

Never fabricates answers or sends private text to a model. Only exact offsets
count as TP. Labelled samples come from the operator's authorized corpus.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .atomic import extract_atomic_candidates
from .requirements import extract_requirement_units

GOLD_SCHEMA = "stage1-gold-v1"


@dataclass(frozen=True, slots=True)
class Stage1Metrics:
    sample_count: int
    labelled_requirements: int
    predicted_requirements: int
    true_positives: int
    false_positives: int
    false_negatives: int
    precision: float
    recall: float
    f1: float


def evaluate_gold_cases(data: dict[str, Any], *, max_cases: int = 2_000,
                        max_chars: int = 5_000_000) -> Stage1Metrics:
    if data.get("schema") != GOLD_SCHEMA or not isinstance(data.get("cases"), list):
        raise ValueError("Unsupported Stage1 gold schema")
    cases = data["cases"]
    if not cases or len(cases) > max_cases:
        raise ValueError("A nonempty, bounded set of real labelled cases is required")
    seen: set[tuple[str, int]] = set()
    tp = fp = fn = predicted = labelled = size = 0
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("Invalid gold case")
        source = case.get("text")
        if not isinstance(source, str) or not source:
            raise ValueError("Gold case must contain original source text")
        size += len(source)
        if size > max_chars:
            raise ValueError("Gold source character budget exceeded")
        doc = case.get("document_id")
        cid = case.get("content_id")
        section = case.get("section_id")
        if not isinstance(doc, str) or not doc or not isinstance(cid, int) or cid < 1:
            raise ValueError("Gold source identity required")
        if not isinstance(section, str) or not section:
            raise ValueError("Gold section identity required")
        case_id = (doc, cid)
        if case_id in seen:
            raise ValueError("Duplicate gold content identity")
        seen.add(case_id)
        digest = hashlib.sha256(source.encode("utf-8", "surrogatepass")).hexdigest()
        if case.get("source_sha256") != digest:
            raise ValueError("Gold source checksum drift")
        expected = case.get("expected_requirement_spans")
        if not isinstance(expected, list):
            raise ValueError("Explicit human-labelled requirement spans are required")
        truth: set[tuple[int,int]] = set()
        for span in expected:
            if not isinstance(span, list) or len(span) != 2:
                raise ValueError("Gold span must be [start,end]")
            left,right = span
            if (not isinstance(left,int) or isinstance(left,bool) or not isinstance(right,int)
                or isinstance(right,bool) or not 0 <= left < right <= len(source)):
                raise ValueError("Gold span outside immutable source")
            truth.add((left,right))
        if len(truth) != len(expected):
            raise ValueError("Duplicate gold span")
        units = extract_requirement_units(document_id=doc,content_id=cid,section_id=section,text=source)
        actual: set[tuple[int,int]] = set()
        for unit in units:
            for atom in extract_atomic_candidates(unit):
                if atom.role == "REQUIREMENT_CANDIDATE":
                    actual.add((atom.start_in_content,atom.end_in_content))
        overlap = len(actual & truth)
        tp += overlap
        fp += len(actual) - overlap
        fn += len(truth) - overlap
        labelled += len(truth)
        predicted += len(actual)
    if labelled == 0:
        raise ValueError("Gold data needs at least one positively labelled requirement before reporting accuracy")
    precision = tp / (tp+fp) if tp+fp else 0.0
    recall = tp / (tp+fn) if tp+fn else 1.0
    f1 = 2*precision*recall/(precision+recall) if (precision+recall) else 0.0
    return Stage1Metrics(len(cases),labelled,predicted,tp,fp,fn,precision,recall,f1)


def evaluate_gold_file(path: str | Path) -> Stage1Metrics:
    p=Path(path)
    if p.stat().st_size > 10_000_000:
        raise ValueError("Gold file exceeds 10 MB safety budget")
    data=json.loads(p.read_text(encoding="utf-8"))
    return evaluate_gold_cases(data)


# Stage1 v2 adds *independently human-labelled* condition/exception/action spans.
# The v1 metrics and schema above remain fully supported for compatibility.
GOLD_SCHEMA_V2 = "stage1-gold-v2"
_SCOPE_KINDS = ("condition", "exception", "action")


@dataclass(frozen=True, slots=True)
class Stage1QualityMetrics:
    schema: str
    requirement_metrics: Stage1Metrics
    labelled_scope_parts: int
    predicted_scope_parts: int
    scope_true_positives: int
    scope_false_positives: int
    scope_false_negatives: int
    scope_precision: float | None
    scope_recall: float | None
    scope_f1: float | None
    unresolved_gold_requirements: int
    status: str  # VALIDATED_GOLD / NO_SCOPE_POSITIVES


def _validated_span(source: str, value: Any, *, label: str) -> tuple[int, int]:
    if (not isinstance(value, list) or len(value) != 2
            or any(type(x) is not int for x in value)):
        raise ValueError(f"{label} requires explicit [start,end] integer offsets")
    left, right = value
    if not 0 <= left < right <= len(source):
        raise ValueError(f"{label} outside original source")
    return left, right


def evaluate_gold_quality_cases(data: dict[str, Any], *, max_cases: int = 2_000,
                                max_chars: int = 5_000_000) -> Stage1QualityMetrics:
    """Evaluate human scope labels *separately* from exact atomic spans.

    No inferred human labels, no partial coverage masked as full evaluation.
    Every positive requirement must have exactly one explicit scope annotation;
    UNRESOLVED truth contributes no fabricated positive scope parts.
    """
    if data.get("schema") != GOLD_SCHEMA_V2:
        raise ValueError("Stage1 v2 gold schema required for scope metrics")
    # Reuse all v1 source/hashes/identity/atomic-span integrity checks.
    baseline = evaluate_gold_cases({**data, "schema": GOLD_SCHEMA},
                                   max_cases=max_cases, max_chars=max_chars)
    from .condition_scope import extract_requirement_scope

    expected_parts: set[tuple[str, int, str, int, int]] = set()
    actual_parts: set[tuple[str, int, str, int, int]] = set()
    unresolved = 0
    cases = data["cases"]
    for case in cases:
        source = case["text"]
        case_identity = (case["document_id"], case["content_id"])
        # Use tuples for source identity; do not concatenate attacker-controlled
        # identifiers into a key that could collide on punctuation.
        identity = json.dumps(case_identity, ensure_ascii=False, separators=(",", ":"))
        truth_reqs = {_validated_span(source, x, label="requirement_span")
                      for x in case["expected_requirement_spans"]}
        truth_ordered = sorted(truth_reqs)
        if any(a[1] > b[0] for a, b in zip(truth_ordered, truth_ordered[1:])):
            raise ValueError("Overlapping labelled requirements need explicit human disambiguation")
        annotations = case.get("scope_annotations")
        if not isinstance(annotations, list):
            raise ValueError("Each Stage1 v2 case needs complete human scope_annotations")
        seen_reqs: set[tuple[int, int]] = set()
        for entry in annotations:
            if not isinstance(entry, dict):
                raise ValueError("Invalid human scope annotation")
            req = _validated_span(source, entry.get("requirement_span"), label="requirement_span")
            if req not in truth_reqs or req in seen_reqs:
                raise ValueError("Scope annotation must identify a unique labelled requirement")
            seen_reqs.add(req)
            status = entry.get("status")
            if status not in ("ACTION_ONLY", "CONDITION_GOVERNS_ACTION",
                              "EXCEPTION_OVERRIDES_ACTION", "UNRESOLVED"):
                raise ValueError("Unknown human scope interpretation")
            available: set[str] = set()
            spans_for_req: list[tuple[int, int, str]] = []
            for role in _SCOPE_KINDS:
                value = entry.get(f"{role}_span")
                if value is None:
                    continue
                a, b = _validated_span(source, value, label=f"{role}_span")
                if not req[0] <= a < b <= req[1]:
                    raise ValueError("Human scope anchor lies outside labelled requirement")
                available.add(role)
                spans_for_req.append((a, b, role))
                expected_parts.add((identity, 0, role, a, b))
            if len(spans_for_req) != len(available):
                raise ValueError("Repeated scope role in human annotation")
            # A scope is a logical partition of one requirement; overlapping
            # source ranges are ambiguous and must be labelled UNRESOLVED.
            for i, (a, b, _) in enumerate(spans_for_req):
                if any(a < d and b > c for c, d, _ in spans_for_req[i + 1:]):
                    raise ValueError("Overlapping human scope fields")
            valid_parts = {
                "ACTION_ONLY": {"action"},
                "CONDITION_GOVERNS_ACTION": {"condition", "action"},
                "EXCEPTION_OVERRIDES_ACTION": {"exception", "action"},
                "UNRESOLVED": set(),
            }[status]
            if available != valid_parts:
                raise ValueError("Human scope parts contradict annotated status")
            if status == "UNRESOLVED":
                unresolved += 1
        if seen_reqs != truth_reqs:
            raise ValueError("Every positive requirement must receive one human scope annotation")
        units = extract_requirement_units(document_id=case["document_id"],
            content_id=case["content_id"], section_id=case["section_id"], text=source)
        for unit in units:
            for atom in extract_atomic_candidates(unit):
                if atom.role != "REQUIREMENT_CANDIDATE":
                    continue
                scope = extract_requirement_scope(unit, atom)
                for role in _SCOPE_KINDS:
                    part = getattr(scope, role)
                    if part is not None:
                        actual_parts.add((identity, 0, role,
                                          part.start_in_content, part.end_in_content))
    tp = len(expected_parts & actual_parts)
    fp = len(actual_parts - expected_parts)
    fn = len(expected_parts - actual_parts)
    if not expected_parts:
        # Do NOT report an arbitrary 100% recall for a dataset that has no
        # labelled scope component. This includes all-UNRESOLVED gold sets.
        precision = recall = f1 = None
        status = "NO_SCOPE_POSITIVES"
    else:
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn)
        f1 = (2 * precision * recall / (precision + recall)
              if precision + recall else 0.0)
        status = "VALIDATED_GOLD"
    return Stage1QualityMetrics(GOLD_SCHEMA_V2, baseline, len(expected_parts),
        len(actual_parts), tp, fp, fn, precision, recall, f1, unresolved, status)


def evaluate_gold_quality_file(path: str | Path) -> Stage1QualityMetrics:
    p = Path(path)
    if not p.is_file() or p.stat().st_size > 10_000_000:
        raise ValueError("Missing gold file or exceeds 10 MB safety budget")
    return evaluate_gold_quality_cases(json.loads(p.read_text(encoding="utf-8")))
