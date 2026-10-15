"""Strict, offline Stage 1 human-labelled holdout evaluation.

This evaluates operator-declared independent review, not cryptographic proof of
reviewer identity. It never exports source text, connects to databases, or asks
an LLM to manufacture ground truth. V1/V2 schemas stay supported separately.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .atomic import extract_atomic_candidates
from .condition_scope import extract_requirement_scope
from .requirements import extract_requirement_units
from .stage1_evaluation import (
    GOLD_SCHEMA_V2,
    Stage1QualityMetrics,
    evaluate_gold_quality_cases,
)

GOLD_SCHEMA_V3 = "stage1-gold-v3"
SOURCE_KINDS = frozenset({"DOCX_TEXT", "DOCX_TABLE", "PDF_TEXT", "PDF_TABLE"})
COMPLEXITY_TAGS = frozenset({
    "NORMAL", "MERGED_HEADER", "CROSS_PAGE", "NESTED_CONDITION",
    "EXCEPTION", "MULTILINGUAL", "NO_REQUIREMENT", "NESTED_TABLE",
})
SPLITS = frozenset({"development", "holdout"})
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8", "surrogatepass")


def case_source_fingerprint(cases: list[dict[str, Any]]) -> str:
    """Pin the source identities and slice assignment, not mutable labels."""
    source = [{key: row.get(key) for key in (
        "project", "document_id", "content_id", "section_id", "source_sha256",
        "source_kind", "split", "complexity_tags")}
        for row in cases]
    return hashlib.sha256(_canonical(source)).hexdigest()


def case_label_digest(case: dict[str, Any]) -> str:
    """A digest of completed human labels; never an approval operation."""
    value = {
        "project": case.get("project"),
        "document_id": case.get("document_id"),
        "content_id": case.get("content_id"),
        "section_id": case.get("section_id"),
        "source_kind": case.get("source_kind"),
        "split": case.get("split"),
        "complexity_tags": case.get("complexity_tags"),
        "source_sha256": case.get("source_sha256"),
        "expected_requirement_spans": case.get("expected_requirement_spans"),
        "scope_annotations": case.get("scope_annotations"),
    }
    return hashlib.sha256(_canonical(value)).hexdigest()


def _nonblank(value: Any, label: str, *, limit: int = 128) -> str:
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or any(ord(c) < 32 for c in value)):
        raise ValueError(f"{label} must be a bounded nonempty identifier")
    return value.strip()


@dataclass(frozen=True, slots=True)
class SliceMetrics:
    sample_count: int
    gold_positive_requirements: int
    predicted_requirements: int
    requirement_false_positives: int
    requirement_f1: float | None
    gold_scope_parts: int
    scope_false_positives: int
    scope_f1: float | None
    status: str


@dataclass(frozen=True, slots=True)
class ScopeRoleMetrics:
    labelled: int
    predicted: int
    true_positives: int
    false_positives: int
    false_negatives: int
    precision: float | None
    recall: float | None
    f1: float | None


@dataclass(frozen=True, slots=True)
class RedactedErrorCase:
    source_identity_sha256_prefix: str
    project: str
    source_kind: str
    missing_requirement_spans: tuple[tuple[int, int], ...]
    extra_requirement_spans: tuple[tuple[int, int], ...]
    missing_scope_parts: tuple[tuple[str, int, int], ...]
    extra_scope_parts: tuple[tuple[str, int, int], ...]
    offsets_truncated: bool


@dataclass(frozen=True, slots=True)
class Stage1GoldV3Result:
    schema: str
    dataset_id: str
    source_fingerprint: str
    declared_independent_review: bool
    review_identity_verified: bool
    evaluated_split: str
    total_cases: int
    holdout_cases: int
    negative_holdout_cases: int
    overall: Stage1QualityMetrics
    by_source_kind: dict[str, SliceMetrics]
    by_complexity: dict[str, SliceMetrics]
    by_project: dict[str, SliceMetrics]
    by_scope_role: dict[str, ScopeRoleMetrics]
    redacted_error_cases: tuple[RedactedErrorCase, ...]
    truncated_error_cases: int
    evaluation_status: str
    production_acceptance: str


def _validate_v3(data: dict[str, Any], *, max_cases: int, max_chars: int) -> list[dict[str, Any]]:
    if not isinstance(data, dict) or data.get("schema") != GOLD_SCHEMA_V3:
        raise ValueError("Stage1 v3 human gold schema required")
    info = data.get("dataset")
    if not isinstance(info, dict):
        raise ValueError("Explicit gold dataset manifest required")
    _nonblank(info.get("dataset_id"), "dataset_id")
    digest = info.get("source_fingerprint")
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise ValueError("Manifest source_fingerprint must be SHA-256")
    cases = data.get("cases")
    if not isinstance(cases, list) or not cases or len(cases) > max_cases:
        raise ValueError("Nonempty bounded set of cases required")
    if any(not isinstance(c, dict) for c in cases):
        raise ValueError("Invalid gold case")
    if case_source_fingerprint(cases) != digest:
        raise ValueError("Dataset source fingerprint drift")
    seen: set[tuple[str, str, int]] = set()
    total_chars = 0
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("Invalid gold case")
        project = case.get("project")
        if project not in ("21MM", "24MM"):
            raise ValueError("Gold case needs explicit 21MM/24MM project")
        document_id = _nonblank(case.get("document_id"), "document_id")
        content_id = case.get("content_id")
        if type(content_id) is not int or content_id < 1:
            raise ValueError("Gold content_id must be a positive integer")
        key = (project, document_id, content_id)
        if key in seen:
            raise ValueError("Duplicate project/document/content gold identity")
        seen.add(key)
        _nonblank(case.get("section_id"), "section_id", limit=512)
        if case.get("source_kind") not in SOURCE_KINDS:
            raise ValueError("Unsupported source_kind; mark native provenance explicitly")
        if case.get("split") not in SPLITS:
            raise ValueError("Each source needs an explicit development/holdout split")
        tags = case.get("complexity_tags")
        if (not isinstance(tags, list) or not tags or len(tags) > 4
                or any(not isinstance(x, str) or x not in COMPLEXITY_TAGS for x in tags)
                or len(set(tags)) != len(tags)
                or ("NORMAL" in tags and len(tags) != 1)):
            raise ValueError("Explicit bounded noncontradictory complexity_tags required")
        source = case.get("text")
        if not isinstance(source, str) or not source:
            raise ValueError("Original nonempty text required")
        total_chars += len(source)
        if total_chars > max_chars:
            raise ValueError("V3 gold source character budget exceeded")
        sha = hashlib.sha256(source.encode("utf-8", "surrogatepass")).hexdigest()
        if case.get("source_sha256") != sha:
            raise ValueError("Gold source SHA-256 drift")
        if case.get("annotation_state") != "VERIFIED":
            raise ValueError("Unlabelled or incomplete gold case cannot be scored")
        review = case.get("independent_review")
        if not isinstance(review, dict):
            raise ValueError("Human independent review declaration required")
        annotator = _nonblank(review.get("annotator_id"), "annotator_id")
        reviewer = _nonblank(review.get("reviewer_id"), "reviewer_id")
        _nonblank(review.get("review_note"), "review_note", limit=2000)
        if annotator == reviewer:
            raise ValueError("Annotator and reviewer must be different identifiers")
        if review.get("reviewed_label_sha256") != case_label_digest(case):
            raise ValueError("Human review label digest drift")
    return cases


def _v2_cases(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Legacy scoring identifies a record by (document_id, content_id); v3
    # additionally isolates project. A same-named DOCX in 21MM and 24MM must
    # never collapse to one source or trigger a false duplicate error.
    return [{**row, "document_id": f"{row['project']}:{row['document_id']}"}
            for row in rows]


def _slice_metrics(rows: list[dict[str, Any]]) -> SliceMetrics:
    """Negative-only slices stay visible, but cannot pretend to have a recall/F1."""
    positives = sum(len(r["expected_requirement_spans"]) for r in rows)
    if positives:
        data = {"schema": GOLD_SCHEMA_V2, "cases": _v2_cases(rows)}
        score = evaluate_gold_quality_cases(data)
        req = score.requirement_metrics
        return SliceMetrics(req.sample_count, req.labelled_requirements,
            req.predicted_requirements, req.false_positives, req.f1,
            score.labelled_scope_parts, score.scope_false_positives,
            score.scope_f1, score.status)
    predicted = predicted_scope = 0
    for row in rows:
        for unit in extract_requirement_units(document_id=row["document_id"],
                content_id=row["content_id"], section_id=row["section_id"], text=row["text"]):
            for atom in extract_atomic_candidates(unit):
                if atom.role == "REQUIREMENT_CANDIDATE":
                    predicted += 1
                    scope = extract_requirement_scope(unit, atom)
                    predicted_scope += sum(getattr(scope, r) is not None
                                           for r in ("condition", "exception", "action"))
    return SliceMetrics(len(rows), 0, predicted, predicted, None,
                        0, predicted_scope, None, "NO_POSITIVE_GOLD")


def _offsets_capped(values: set[tuple[int, int]] | set[tuple[str, int, int]],
                    *, limit: int = 12) -> tuple:
    return tuple(sorted(values)[:limit])


def _error_diagnostics(rows: list[dict[str, Any]], *, max_errors: int = 40):
    """Per-role recall and bounded error coordinates, never private sentences."""
    roles = ("condition", "exception", "action")
    role_tp = {r: 0 for r in roles}
    role_fp = {r: 0 for r in roles}
    role_fn = {r: 0 for r in roles}
    errors: list[RedactedErrorCase] = []
    all_errors = 0
    for case in rows:
        expected_req = {tuple(s) for s in case["expected_requirement_spans"]}
        expected_parts: set[tuple[str, int, int]] = set()
        for annotation in case["scope_annotations"]:
            for role in roles:
                if annotation.get(f"{role}_span") is not None:
                    a, b = annotation[f"{role}_span"]
                    expected_parts.add((role, a, b))
        actual_req: set[tuple[int, int]] = set()
        actual_parts: set[tuple[str, int, int]] = set()
        for unit in extract_requirement_units(document_id=case["document_id"],
                content_id=case["content_id"], section_id=case["section_id"], text=case["text"]):
            for atom in extract_atomic_candidates(unit):
                if atom.role != "REQUIREMENT_CANDIDATE":
                    continue
                actual_req.add((atom.start_in_content, atom.end_in_content))
                scope = extract_requirement_scope(unit, atom)
                for role in roles:
                    part = getattr(scope, role)
                    if part is not None:
                        actual_parts.add((role, part.start_in_content, part.end_in_content))
        for role in roles:
            truth = {p for p in expected_parts if p[0] == role}
            found = {p for p in actual_parts if p[0] == role}
            role_tp[role] += len(truth & found)
            role_fp[role] += len(found - truth)
            role_fn[role] += len(truth - found)
        missing_req = expected_req - actual_req
        extra_req = actual_req - expected_req
        missing_parts = expected_parts - actual_parts
        extra_parts = actual_parts - expected_parts
        if not (missing_req or extra_req or missing_parts or extra_parts):
            continue
        all_errors += 1
        if len(errors) >= max_errors:
            continue
        identity = hashlib.sha256(_canonical([case["project"],case["document_id"],
                                              case["content_id"]])).hexdigest()[:20]
        errors.append(RedactedErrorCase(identity, case["project"], case["source_kind"],
            _offsets_capped(missing_req), _offsets_capped(extra_req),
            _offsets_capped(missing_parts), _offsets_capped(extra_parts),
            any(len(x) > 12 for x in (missing_req, extra_req, missing_parts, extra_parts))))
    scores = {}
    for role in roles:
        tp, fp, fn = role_tp[role], role_fp[role], role_fn[role]
        if tp + fn == 0:
            precision = recall = f1 = None
        else:
            precision = tp / (tp+fp) if tp + fp else 0.0
            recall = tp / (tp+fn)
            f1 = (2 * precision * recall / (precision+recall)) if precision + recall else 0.0
        scores[role] = ScopeRoleMetrics(tp+fn, tp+fp, tp, fp, fn, precision, recall, f1)
    return scores, tuple(errors), all_errors - len(errors)


def evaluate_gold_v3_cases(data: dict[str, Any], *, max_cases: int = 2000,
                           max_chars: int = 5_000_000) -> Stage1GoldV3Result:
    cases = _validate_v3(data, max_cases=max_cases, max_chars=max_chars)
    holdout = [c for c in cases if c["split"] == "holdout"]
    if not holdout:
        raise ValueError("No independently reviewed holdout cases")
    if not any(c["expected_requirement_spans"] for c in holdout):
        raise ValueError("Holdout has no positive requirement labels")
    # The v2 scorer enforces span range, overlap and full scope coverage for
    # holdout. For development cases, verify the same label contract separately;
    # non-positive cases are accepted as legitimate reviewed negatives.
    for split in ("holdout", "development"):
        group = [c for c in cases if c["split"] == split]
        if group and any(c["expected_requirement_spans"] for c in group):
            evaluate_gold_quality_cases({"schema": GOLD_SCHEMA_V2, "cases": _v2_cases(group)},
                                        max_cases=max_cases, max_chars=max_chars)
        elif group:
            if any(c.get("expected_requirement_spans") != [] or c.get("scope_annotations") != []
                   for c in group):
                raise ValueError("Negative cases must have explicit empty annotations")
    score = evaluate_gold_quality_cases({"schema": GOLD_SCHEMA_V2, "cases": _v2_cases(holdout)},
                                        max_cases=max_cases, max_chars=max_chars)
    kinds = {x: _slice_metrics([c for c in holdout if c["source_kind"] == x])
             for x in sorted({c["source_kind"] for c in holdout})}
    tags = {x: _slice_metrics([c for c in holdout if x in c["complexity_tags"]])
            for x in sorted({tag for c in holdout for tag in c["complexity_tags"]})}
    projects = {x: _slice_metrics([c for c in holdout if c["project"] == x])
                for x in sorted({c["project"] for c in holdout})}
    role_scores, errors, truncated = _error_diagnostics(holdout)
    return Stage1GoldV3Result(GOLD_SCHEMA_V3, data["dataset"]["dataset_id"],
        data["dataset"]["source_fingerprint"], True, False, "holdout",
        len(cases), len(holdout), sum(not c["expected_requirement_spans"] for c in holdout),
        score, kinds, tags, projects, role_scores, errors, truncated,
        "MEASURED_OPERATOR_DECLARED_GOLD",
        "NOT_ESTABLISHED_BY_AUTOMATED_SCORING")


def evaluate_gold_v3_file(path: Path | str) -> Stage1GoldV3Result:
    p = Path(path)
    if not p.is_file() or p.stat().st_size > 10_000_000:
        raise ValueError("Missing or oversized gold file")
    return evaluate_gold_v3_cases(json.loads(p.read_text(encoding="utf-8")))


def redacted_quality_report(result: Stage1GoldV3Result) -> dict[str, Any]:
    """Counts and identifiers only; never raw snippets, review notes or passwords."""
    return asdict(result)
