"""Replay fixed multi-domain Skill plans against operator-curated JSONL cases.

OFFLINE ONLY: these are deterministic routing/coverage contract checks; a 100%
score does NOT prove live SQL/Qdrant or actual RAG answer correctness.

Usage: python scripts/evaluate_v150_skill_workflows.py --cases examples/skills/v150_workflow_cases.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.skills.live_workflows import build_live_skill_workflow, missing_evidence_facets  # noqa: E402

MAX_BYTES = 2_000_000
MAX_CASES = 300


class CaseEvidence:
    def __init__(self, kind: str) -> None:
        self.source_type = kind


def evaluate(path: Path) -> dict:
    raw = path.read_bytes()
    if len(raw) > MAX_BYTES:
        raise ValueError("CASES_FILE_TOO_LARGE")
    lines = [x for x in raw.decode("utf-8").splitlines() if x.strip()]
    if not 1 <= len(lines) <= MAX_CASES:
        raise ValueError("INVALID_CASE_COUNT")
    output = []
    seen = set()
    for raw_line in lines:
        case = json.loads(raw_line)
        if not isinstance(case, dict):
            raise ValueError("INVALID_CASE")
        cid, question = case.get("case_id"), case.get("question")
        kinds, expected = case.get("retrieved_kinds"), case.get("expected_missing")
        if (not isinstance(cid, str) or len(cid) > 100 or cid in seen
                or not isinstance(question, str) or len(question) > 4000
                or not isinstance(kinds, list) or len(kinds) > 40
                or not all(isinstance(k, str) and len(k) <= 60 for k in kinds)
                or not isinstance(expected, list) or not all(isinstance(x, str) for x in expected)):
            raise ValueError("INVALID_CASE_SCHEMA_OR_DUPLICATE")
        seen.add(cid)
        workflow = build_live_skill_workflow(question)
        missing = missing_evidence_facets((CaseEvidence(k) for k in kinds), workflow.evidence_facets)
        passed = list(missing) == expected
        output.append({"case_id": cid, "passed": passed, "skills": list(workflow.skills),
                       "mandatory_tools": list(workflow.required_tools), "missing_facets": list(missing)})
    return {"dataset_type": "OFFLINE_SYNTHETIC_ROUTING_ONLY", "cases": len(output),
            "passed": sum(bool(x["passed"]) for x in output), "failed": sum(not x["passed"] for x in output),
            "live_database_verified": False, "semantic_answer_accuracy_verified": False,
            "results": output}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        result = evaluate(args.cases)
    except (ValueError, TypeError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "BLOCKED", "reason": type(exc).__name__}), file=sys.stderr)
        return 2
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    return 0 if result["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
