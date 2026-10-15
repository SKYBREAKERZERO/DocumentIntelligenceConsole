"""Offline evaluation of *human labelled* Stage1 source spans; no cloud calls."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.engineering_intelligence.stage1_evaluation import (
    GOLD_SCHEMA, GOLD_SCHEMA_V2, evaluate_gold_file, evaluate_gold_quality_file,
)
from app.engineering_intelligence.stage1_evaluation_v3 import (
    GOLD_SCHEMA_V3, evaluate_gold_v3_file, redacted_quality_report,
    SOURCE_KINDS, COMPLEXITY_TAGS,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Offline human-labelled Stage1 requirement and scope evaluation")
    parser.add_argument("--gold", required=True, type=Path)
    parser.add_argument("--min-requirement-f1", type=float, default=None,
                        help="Optional CI acceptance gate; returns exit code 2 if not met")
    parser.add_argument("--min-scope-f1", type=float, default=None,
                        help="Requires v2 gold with at least one scope-positive label")
    parser.add_argument("--min-holdout-cases", type=int, default=1,
                        help="v3 only: require at least this many independently reviewed holdout cases")
    parser.add_argument("--min-negative-cases", type=int, default=0,
                        help="v3 only: require this many reviewed holdout negatives")
    parser.add_argument("--require-source-kind", action="append", default=[], choices=sorted(SOURCE_KINDS),
                        help="v3 only: require coverage of a particular physical source kind")
    parser.add_argument("--min-source-kind-cases", type=int, default=1,
                        help="v3 only: minimum reviewed holdout cases for each required source kind")
    parser.add_argument("--min-source-kind-f1", type=float, default=None,
                        help="v3 only: minimum requirement F1 in each required source kind")
    parser.add_argument("--require-complexity-tag", action="append", default=[],
                        choices=sorted(COMPLEXITY_TAGS),
                        help="v3 only: require a reviewed holdout complexity slice")
    parser.add_argument("--min-tag-cases", type=int, default=1,
                        help="v3 only: minimum holdout cases per required complexity tag")
    parser.add_argument("--min-tag-f1", type=float, default=None,
                        help="v3 only: minimum requirement F1 per required complexity tag")
    for role in ("condition", "exception", "action"):
        parser.add_argument(f"--min-{role}-f1", type=float, default=None,
                            help=f"v3 only: independently require {role} exact-span F1")
    args = parser.parse_args(argv)
    if (args.min_holdout_cases < 1 or args.min_negative_cases < 0
            or args.min_source_kind_cases < 1 or args.min_tag_cases < 1):
        parser.error("Minimum case counts must be valid nonnegative integers")
    for threshold in (args.min_requirement_f1, args.min_scope_f1, args.min_source_kind_f1,
                      args.min_tag_f1, args.min_condition_f1,
                      args.min_exception_f1, args.min_action_f1):
        if threshold is not None and not 0 <= threshold <= 1:
            parser.error("Thresholds must be between 0 and 1")
    if args.min_source_kind_f1 is not None and not args.require_source_kind:
        parser.error("--min-source-kind-f1 requires at least one --require-source-kind")
    if args.min_tag_f1 is not None and not args.require_complexity_tag:
        parser.error("--min-tag-f1 requires at least one --require-complexity-tag")
    if not args.gold.is_file() or args.gold.stat().st_size > 10_000_000:
        parser.error("Missing or oversized gold file")
    # Inspect only the schema discriminator before dispatching. Both evaluators
    # independently verify the full source digest and annotation coordinates.
    schema = json.loads(args.gold.read_text(encoding="utf-8")).get("schema")
    if schema != GOLD_SCHEMA_V3 and (args.require_source_kind or args.min_negative_cases
            or args.min_holdout_cases != 1 or args.min_source_kind_cases != 1
            or args.min_source_kind_f1 is not None or args.require_complexity_tag
            or args.min_tag_cases != 1 or args.min_tag_f1 is not None
            or any(getattr(args, f"min_{r}_f1") is not None
                   for r in ("condition", "exception", "action"))):
        parser.error("Holdout and source-kind gates require stage1-gold-v3")
    if schema == GOLD_SCHEMA_V3:
        result = evaluate_gold_v3_file(args.gold)
        require_f1 = result.overall.requirement_metrics.f1
        scope_f1 = result.overall.scope_f1
        result_json = redacted_quality_report(result)
        counts_passed = (result.holdout_cases >= args.min_holdout_cases
                         and result.negative_holdout_cases >= args.min_negative_cases)
        coverage_passed = all(
            (kind in result.by_source_kind
             and result.by_source_kind[kind].sample_count >= args.min_source_kind_cases
             and (args.min_source_kind_f1 is None or (
                 result.by_source_kind[kind].requirement_f1 is not None
                 and result.by_source_kind[kind].requirement_f1 >= args.min_source_kind_f1)))
            for kind in set(args.require_source_kind))
        tags_passed = all(
            (tag in result.by_complexity
             and result.by_complexity[tag].sample_count >= args.min_tag_cases
             and (args.min_tag_f1 is None or (
                 result.by_complexity[tag].requirement_f1 is not None
                 and result.by_complexity[tag].requirement_f1 >= args.min_tag_f1)))
            for tag in set(args.require_complexity_tag))
        role_passed = all(
            (getattr(args, f"min_{role}_f1") is None
             or (result.by_scope_role[role].f1 is not None
                 and result.by_scope_role[role].f1 >= getattr(args, f"min_{role}_f1")))
            for role in ("condition", "exception", "action"))
    elif schema == GOLD_SCHEMA_V2:
        result = evaluate_gold_quality_file(args.gold)
        require_f1 = result.requirement_metrics.f1
        scope_f1 = result.scope_f1
        result_json = asdict(result)
        counts_passed = coverage_passed = tags_passed = role_passed = True
    elif schema == GOLD_SCHEMA:
        result = evaluate_gold_file(args.gold)
        require_f1 = result.f1
        scope_f1 = None
        result_json = asdict(result)
        counts_passed = coverage_passed = tags_passed = role_passed = True
    else:
        parser.error("Unsupported human gold schema")
    passed = counts_passed and coverage_passed and tags_passed and role_passed and (args.min_requirement_f1 is None or require_f1 >= args.min_requirement_f1) and (
        args.min_scope_f1 is None or
        (scope_f1 is not None and scope_f1 >= args.min_scope_f1))
    output = {"metrics": result_json, "acceptance_passed": passed,
              "measurement_kind": "EXACT_SOURCE_SPAN_AGAINST_OPERATOR_SUPPLIED_LABELS",
              "production_acceptance": "NOT_ESTABLISHED_BY_THIS_TOOL",
              "independent_annotation_verification": "DECLARED_ONLY_NOT_IDENTITY_VERIFIED",
              "evaluation_split": "holdout" if schema == GOLD_SCHEMA_V3 else "unspecified_legacy",
              "coverage_gate_passed": counts_passed and coverage_passed and tags_passed,
              "role_gate_passed": role_passed}
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
