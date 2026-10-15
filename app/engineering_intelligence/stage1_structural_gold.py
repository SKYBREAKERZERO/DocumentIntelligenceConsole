"""Stage 1 v1.27: independently reviewed source/section/cross-page Gold.

Offline. Gold labels come ONLY from a human; source/SQL reports are evidence
and must be regenerated against the original assets. They are not themselves
semantic truth. Stage1-gold-v3 remains the normative/scope evaluator.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any

from .stage1_native_sql_chain import CHAIN_SCHEMA
from .stage1_native_fidelity import SCHEMA as NATIVE_SCHEMA

SCHEMA = "stage1-structural-gold-v1"
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_LITERAL = "FOUR_LAYER_LITERAL_CANDIDATE_REVIEW_ONLY"
_SECTION = "SECTION_ID_IN_EXPORTED_TREE"
_PAIR_KEYS = ("previous_page", "previous_table_index", "next_page", "next_table_index")


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8", "surrogatepass")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _is_sha(value: Any) -> bool:
    return isinstance(value, str) and _DIGEST.fullmatch(value) is not None


def _pair_key(row: dict) -> tuple[int, int, int, int]:
    values = tuple(row.get(key) for key in _PAIR_KEYS)
    if (any(type(x) is not int or x < 0 for x in values)
            or values[0] < 1 or values[2] != values[0] + 1):
        raise ValueError("Invalid adjacent table locator")
    return values


def _check_report_pair(native: dict, chain: dict, *, max_contents: int = 2_000,
                       max_pairs: int = 10_000) -> tuple[list[dict], list[dict]]:
    if (not isinstance(native, dict) or not isinstance(chain, dict)
            or chain.get("project") not in ("21MM", "24MM")
            or native.get("schema") != NATIVE_SCHEMA or chain.get("schema") != CHAIN_SCHEMA
            or not _is_sha(native.get("document_hash"))
            or native["document_hash"] != chain.get("document_hash")
            or not _is_sha(native.get("native_file_sha256"))
            or native["native_file_sha256"] != chain.get("native_file_sha256")
            or chain.get("source_review_queue_complete") is not True
            or native.get("contents_not_sampled") != 0
            or native.get("entries_omitted") != 0
            or native.get("pipeline_evidence_queue_complete") is not True
            or native.get("crosspage_candidate_queue_complete") is not True
            or native.get("crosspage_pair_universe_complete") is not True
            or native.get("pipeline_json_review_replayed") is not True):
        raise ValueError("Incomplete, mismatched or unsupported source reports")
    nrows = native.get("entries")
    crows = chain.get("entries")
    universe = native.get("crosspage_pair_universe")
    proposed = native.get("crosspage_review_queue")
    if (not all(isinstance(x, list) for x in (nrows, crows, universe, proposed))
            or len(nrows) > max_contents or len(crows) > max_contents
            or len(universe) > max_pairs or len(proposed) > max_pairs
            or len(nrows) != native.get("pipeline_content_count")
            or len(universe) != native.get("crosspage_pair_universe_count")
            or len(crows) != chain.get("evidence_count")):
        raise ValueError("Report count or safety budget invalid")
    nindex: dict[int, dict] = {}
    for r in nrows:
        if not isinstance(r, dict):
            raise ValueError("Invalid native row")
        ix = r.get("content_index")
        if (type(ix) is not int or ix < 1 or ix in nindex
                or not _is_sha(r.get("content_sha256"))):
            raise ValueError("Invalid or duplicate native item")
        nindex[ix] = r
    if set(nindex) != set(range(1, len(nrows) + 1)):
        raise ValueError("Noncontiguous native indices")
    seen: set[str] = set()
    chains: list[dict] = []
    for r in crows:
        if not isinstance(r, dict):
            raise ValueError("Malformed chain row")
        ix, key = r.get("content_index"), r.get("review_key")
        if (type(ix) is not int or ix not in nindex or not _is_sha(key)
                or key in seen
                or r.get("native_alignment_status") != nindex[ix].get("text_alignment_status")
                or r.get("exported_section_status") != nindex[ix].get("section_tree_status")):
            raise ValueError("Chain/native identity disagreement")
        if (r.get("chain_status") == _LITERAL and (
                nindex[ix]["text_alignment_status"] not in (
                    "UNIQUE_NATIVE_LINE_CANDIDATE", "UNIQUE_NATIVE_MULTILINE_CANDIDATE")
                or nindex[ix]["section_tree_status"] != _SECTION
                or r.get("sql_content_status") != "EXACT_SQL_CONTENT_ONLY"
                or r.get("sql_unit_status") != "EXACT_SQL_UNIT_REVIEW_ONLY")):
            raise ValueError("Claimed complete chain contradicts source and SQL states")
        seen.add(key)
        chains.append({"content_index": ix, "review_key": key,
                       "content_sha256": nindex[ix]["content_sha256"],
                       "predicted_literal": r.get("chain_status") == _LITERAL,
                       "predicted_section_membership": nindex[ix]["section_tree_status"] == _SECTION})
    pairs: dict[tuple[int, int, int, int], dict] = {}
    for pair in universe:
        if not isinstance(pair, dict):
            raise ValueError("Invalid cross-page pair")
        key = _pair_key(pair)
        if key in pairs:
            raise ValueError("Duplicate cross-page pair")
        pairs[key] = {k: v for k, v in zip(_PAIR_KEYS, key)}
    suggested: set[tuple[int, int, int, int]] = set()
    for pair in proposed:
        if not isinstance(pair, dict):
            raise ValueError("Invalid suggested pair")
        key = _pair_key(pair)
        if key not in pairs or key in suggested:
            raise ValueError("Suggestion not in exhaustive pair universe")
        suggested.add(key)
    tables = [{**pairs[key], "predicted_continuation_candidate": key in suggested}
              for key in sorted(pairs)]
    return sorted(chains, key=lambda r: (r["content_index"], r["review_key"])), tables


def _case_ids(chains: list[dict], pairs: list[dict]) -> dict:
    return {"sources": chains, "adjacent_table_pairs": pairs}


def prepare_structural_gold(native: dict, chain: dict, *, dataset_id: str) -> dict:
    if (not isinstance(dataset_id, str) or not 1 <= len(dataset_id.strip()) <= 128
            or any(ord(c) < 32 for c in dataset_id)):
        raise ValueError("Bounded dataset identifier required")
    chains, pairs = _check_report_pair(native, chain)
    if not chains:
        raise ValueError("At least one source chain must be reviewed")
    manifest = {
        "dataset_id": dataset_id, "document_hash": native["document_hash"],
        "native_file_sha256": native["native_file_sha256"],
        "native_report_sha256": _digest(native), "chain_report_sha256": _digest(chain),
        "identity_fingerprint": _digest(_case_ids(chains, pairs)),
    }
    return {"schema": SCHEMA, "dataset": manifest,
            "sources": [{**r, "literal_source_correct": None,
                         "section_assignment_correct": None} for r in chains],
            "adjacent_table_pairs": [{**r, "true_continuation": None} for r in pairs],
            "review": {"annotation_state": "PENDING", "annotator_id": "",
                       "reviewer_id": "", "review_note": "", "reviewed_label_sha256": ""}}


def reviewed_label_digest(gold: dict) -> str:
    """Digest filled by independent reviewer AFTER all labels are complete."""
    return _digest({"dataset": gold.get("dataset"),
                    "sources": gold.get("sources"),
                    "adjacent_table_pairs": gold.get("adjacent_table_pairs")})


@dataclass(frozen=True, slots=True)
class BinaryMetrics:
    positive_labels: int
    negative_labels: int
    predicted_positive: int
    true_positive: int
    false_positive: int
    false_negative: int
    true_negative: int
    precision: float | None
    recall: float | None
    f1: float | None


def _metric(rows: list[tuple[bool, bool]]) -> BinaryMetrics:
    tp = sum(a and b for a, b in rows)
    fp = sum(a and not b for a, b in rows)
    fn = sum(not a and b for a, b in rows)
    tn = sum(not a and not b for a, b in rows)
    positive = tp + fn
    # No positives: undefined recall, F1 and precision; do not award perfect
    # score to a trivial all-negative evaluation set.
    if not positive:
        precision = recall = f1 = None
    else:
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / positive
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return BinaryMetrics(positive, fp + tn, tp + fp, tp, fp, fn, tn,
                         precision, recall, f1)


def evaluate_structural_gold(native: dict, chain: dict, gold: dict) -> dict:
    if not isinstance(gold, dict) or not isinstance(gold.get("dataset"), dict):
        raise ValueError("Malformed structural Gold")
    expected = prepare_structural_gold(native, chain,
                                      dataset_id=gold["dataset"].get("dataset_id", ""))
    if gold.get("schema") != SCHEMA or gold.get("dataset") != expected["dataset"]:
        raise ValueError("Gold dataset/report hash mismatch")
    sources = gold.get("sources")
    pairs = gold.get("adjacent_table_pairs")
    if (not isinstance(sources, list) or not isinstance(pairs, list)
            or len(sources) != len(expected["sources"])
            or len(pairs) != len(expected["adjacent_table_pairs"])):
        raise ValueError("Incomplete review universe")
    for observed, reference in zip(sources, expected["sources"]):
        if (not isinstance(observed, dict) or
                {k: v for k, v in observed.items() if k not in (
                    "literal_source_correct", "section_assignment_correct")} !=
                {k: v for k, v in reference.items() if k not in (
                    "literal_source_correct", "section_assignment_correct")}
                or any(type(observed.get(k)) is not bool for k in (
                    "literal_source_correct", "section_assignment_correct"))):
            raise ValueError("Invalid, unlabelled or reordered source review")
    for observed, reference in zip(pairs, expected["adjacent_table_pairs"]):
        if (not isinstance(observed, dict)
                or {k: v for k, v in observed.items() if k != "true_continuation"} !=
                {k: v for k, v in reference.items() if k != "true_continuation"}
                or type(observed.get("true_continuation")) is not bool):
            raise ValueError("Invalid or unlabelled cross-page pair")
    review = gold.get("review")
    if not isinstance(review, dict) or review.get("annotation_state") != "VERIFIED":
        raise ValueError("Human review incomplete")
    reviewer, annotator = review.get("reviewer_id"), review.get("annotator_id")
    if (not all(isinstance(v, str) and 1 <= len(v.strip()) <= 128
                and not any(ord(c) < 32 for c in v) for v in (reviewer, annotator))
            or reviewer == annotator
            or not isinstance(review.get("review_note"), str)
            or not 1 <= len(review["review_note"].strip()) <= 2_000
            or review.get("reviewed_label_sha256") != reviewed_label_digest(gold)):
        raise ValueError("Independent reviewer declaration/label digest invalid")
    chain_result = _metric([(r["predicted_literal"], r["literal_source_correct"]) for r in sources])
    section_result = _metric([(r["predicted_section_membership"], r["section_assignment_correct"]) for r in sources])
    table_result = _metric([(r["predicted_continuation_candidate"], r["true_continuation"]) for r in pairs])
    return {"schema": "stage1-structural-gold-result-v1",
            "dataset_id_sha256": _digest(gold["dataset"]["dataset_id"]),
            "document_hash": native["document_hash"],
            "native_file_sha256": native["native_file_sha256"],
            "project": chain.get("project"),
            "source_cases": len(sources), "adjacent_table_pairs": len(pairs),
            "literal_source_classification": asdict(chain_result),
            "section_membership_proxy": asdict(section_result),
            "crosspage_geometry_candidate": asdict(table_result),
            "crosspage_universe_exhaustive": True,
            "declared_independent_review": True,
            "reviewer_identity_authenticated": False,
            "physical_table_headers_semantically_verified": False,
            "bilingual_equivalence_approved": False,
            "dcm_state_transitions_approved": False,
            "production_acceptance": "NOT_ESTABLISHED_BY_AUTOMATED_SCORING",
            "source_data_included": False, "database_modified": False,
            "qdrant_modified": False}
