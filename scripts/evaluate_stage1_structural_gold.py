"""v1.27 local structural Gold worksheet and strict offline acceptance gates.

This evaluates reviewer-declared labels; it never authorizes semantic links,
modifies SQL/Qdrant, or reports Windows/live-database acceptance.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.engineering_intelligence.stage1_evaluation_v3 import evaluate_gold_v3_file
from app.engineering_intelligence.stage1_structural_gold import (
    evaluate_structural_gold,
    prepare_structural_gold,
)


def _load(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 10_000_000:
        raise ValueError("Missing, symlinked or oversized local input")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Expected JSON object")
    return data


def _write_new(path: Path, content: dict) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if path.is_symlink():
        raise ValueError("Output symlink refused")
    raw = (json.dumps(content, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    if len(raw) > 10_000_000:
        raise ValueError("Output exceeds safety budget")
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _quality_gate(result: dict, v3_result, args) -> dict:
    measures = (("literal_source_classification", args.min_literal_f1),
                ("section_membership_proxy", args.min_section_proxy_f1),
                ("crosspage_geometry_candidate", args.min_crosspage_f1))
    quality = all(result[k]["f1"] is not None and result[k]["f1"] >= threshold
                  for k, threshold in measures)
    positives = all(result[k]["positive_labels"] >= args.min_positive_labels
                    for k, _ in measures)
    counts = (result["source_cases"] >= args.min_source_cases
              and result["adjacent_table_pairs"] >= args.min_crosspage_pairs)
    # Project and complexity coverage are explicit: a narrow DCM-only v3
    # holdout cannot silently qualify the entire Stage1 multilingual/table
    # contract. These are corpus-level coverage checks, NOT source-identity
    # authentication across different Gold files.
    required_tags = ("MULTILINGUAL", "CROSS_PAGE", "NESTED_CONDITION", "EXCEPTION")
    v3_coverage = (v3_result is not None
                   and result["project"] in v3_result.by_project
                   and all(tag in v3_result.by_complexity
                           and v3_result.by_complexity[tag].sample_count >= 1
                           for tag in required_tags))
    v3_gate = (v3_coverage
               and v3_result.holdout_cases >= args.min_holdout_cases
               and v3_result.overall.requirement_metrics.f1 >= args.min_requirement_f1
               and v3_result.overall.scope_f1 is not None
               and v3_result.overall.scope_f1 >= args.min_scope_f1)
    return {"schema": "stage1-acceptance-gate-v1", "structural_metrics": result,
            "requirement_scope_gold_present": v3_result is not None,
            "v3_gold_source_fingerprint": v3_result.source_fingerprint if v3_result else None,
            "v3_project_complexity_coverage_passed": bool(v3_coverage),
            "required_v3_complexity_tags": list(required_tags),
            "holdout_cases": v3_result.holdout_cases if v3_result else 0,
            "requirement_f1": v3_result.overall.requirement_metrics.f1 if v3_result else None,
            "scope_f1": v3_result.overall.scope_f1 if v3_result else None,
            "measured_human_gold_gates_passed": bool(quality and positives and counts and v3_gate),
            "reasons": [reason for blocked, reason in (
                (not quality, "STRUCTURAL_F1_BELOW_THRESHOLD_OR_UNDEFINED"),
                (not positives, "INSUFFICIENT_POSITIVE_STRUCTURAL_LABELS"),
                (not counts, "INSUFFICIENT_STRUCTURAL_SAMPLE_COUNT"),
                (not v3_gate, "REQUIREMENT_SCOPE_HOLDOUT_MISSING_OR_BELOW_THRESHOLD"),
            ) if blocked],
            "thresholds": {"literal_f1": args.min_literal_f1,
                           "section_membership_proxy_f1": args.min_section_proxy_f1,
                           "crosspage_f1": args.min_crosspage_f1,
                           "requirement_f1": args.min_requirement_f1,
                           "scope_f1": args.min_scope_f1,
                           "min_source_cases": args.min_source_cases,
                           "min_crosspage_pairs": args.min_crosspage_pairs,
                           "min_positive_labels": args.min_positive_labels,
                           "min_holdout_cases": args.min_holdout_cases},
            "human_reviewer_identity_authenticated": False,
            "real_business_document_validation_completed": False,
            "windows_gui_live_database_acceptance_completed": False,
            "stage1_production_acceptance": "NOT_ESTABLISHED_BY_THIS_TOOL",
            "database_modified": False, "qdrant_modified": False}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="v1.27 source/section/crosspage independent Gold review")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--prepare", action="store_true", help="Create PENDING Gold worksheet")
    mode.add_argument("--evaluate", action="store_true", help="Score verified human labels")
    p.add_argument("--native-report", required=True, type=Path)
    p.add_argument("--chain-report", required=True, type=Path)
    p.add_argument("--gold", type=Path, help="Verified structural Gold JSON; --evaluate only")
    p.add_argument("--dataset-id", help="Unique label set identifier; --prepare only")
    p.add_argument("--v3-gold", type=Path, help="Independently reviewed v3 Requirement/Scope Gold")
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--confirm-local-review", action="store_true")
    for field, default in (("literal", .90), ("section-proxy", .90),
                           ("crosspage", .90), ("requirement", .90), ("scope", .85)):
        p.add_argument(f"--min-{field}-f1", type=float, default=default)
    for field, default in (("source-cases", 10), ("crosspage-pairs", 2),
                           ("positive-labels", 1), ("holdout-cases", 10)):
        p.add_argument(f"--min-{field}", type=int, default=default)
    args = p.parse_args(argv)
    if (not args.confirm_local_review or (args.prepare and (not args.dataset_id or args.gold or args.v3_gold))
            or (args.evaluate and (args.dataset_id or not args.gold))):
        p.error("Explicit local review confirmation and mode-specific inputs required")
    if any(not 0 <= x <= 1 for x in (
            args.min_literal_f1, args.min_section_proxy_f1, args.min_crosspage_f1,
            args.min_requirement_f1, args.min_scope_f1)):
        p.error("F1 thresholds must be between 0 and 1")
    if any(x < 1 for x in (args.min_source_cases, args.min_crosspage_pairs,
                         args.min_positive_labels, args.min_holdout_cases)):
        p.error("Sample and positive label minima must be positive")
    inputs = [args.native_report, args.chain_report]
    if args.gold:
        inputs.append(args.gold)
    if args.v3_gold:
        inputs.append(args.v3_gold)
    if any(args.out.resolve() == path.resolve() for path in inputs):
        p.error("Output must not be an input file")
    try:
        native, chain = (_load(args.native_report), _load(args.chain_report))
        if args.prepare:
            result = prepare_structural_gold(native, chain, dataset_id=args.dataset_id)
            _write_new(args.out, result)
            print(json.dumps({"status": "PENDING_HUMAN_ANNOTATION",
                              "sources": len(result["sources"]),
                              "adjacent_table_pairs": len(result["adjacent_table_pairs"]),
                              "no_accuracy_claim": True}))
            return 0
        gold = _load(args.gold)
        result = evaluate_structural_gold(native, chain, gold)
        v3 = evaluate_gold_v3_file(args.v3_gold) if args.v3_gold else None
        report = _quality_gate(result, v3, args)
        _write_new(args.out, report)
        print(json.dumps({"status": "REVIEWED_GOLD_MEASURED",
                          "acceptance_gates_passed": report["measured_human_gold_gates_passed"],
                          "reasons": report["reasons"],
                          "production_acceptance": "NOT_ESTABLISHED_BY_THIS_TOOL"}))
        return 0 if report["measured_human_gold_gates_passed"] else 2
    except (ValueError, OSError, TypeError, KeyError, json.JSONDecodeError) as exc:
        p.error(f"Local Gold evaluation refused: {type(exc).__name__}: {exc}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
