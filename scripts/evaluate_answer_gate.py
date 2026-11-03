"""Offline answer-release regression runner; NEVER a semantic or production certificate.

JSONL per case: case_id, dataset_type, answer, evidence:[{id,text,label}],
expected_accept. Real cases additionally require independent reviewer and source
artifact digest. This checks the deterministic release gates only, not whether a
causal/negative/conformance claim follows from the engineering specification.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.ai.answer_integrity import validate_enterprise_answer
from app.ai.claim_anchor import validate_claim_anchors


class DatasetError(ValueError):
    pass


def evaluate_case(case: dict[str, Any]) -> dict[str, Any]:
    cid = str(case.get("case_id") or "").strip()
    kind = str(case.get("dataset_type") or "").strip()
    if not cid or kind not in {"SYNTHETIC_CONTRACT", "HUMAN_REVIEWED_REAL"}:
        raise DatasetError("case_id and a supported dataset_type are required")
    if type(case.get("expected_accept")) is not bool:
        raise DatasetError(f"{cid}: expected_accept must be boolean")
    items = case.get("evidence")
    if not isinstance(items, list) or not items or len(items) > 100:
        raise DatasetError(f"{cid}: 1..100 evidence entries required")
    if not isinstance(case.get("answer"), str):
        raise DatasetError(f"{cid}: answer must be text")
    if kind == "HUMAN_REVIEWED_REAL":
        # A label alone must never silently upgrade synthetic examples into
        # the verified customer Golden Dataset required for enterprise signoff.
        checksum = str(case.get("source_artifact_sha256") or "")
        reviewer = str(case.get("reviewer") or "").strip()
        if not reviewer or not re.fullmatch(r"[a-fA-F0-9]{64}", checksum):
            raise DatasetError(f"{cid}: real cases require reviewer and source SHA256")
    seen: set[str] = set()
    evidence_objects: list[SimpleNamespace] = []
    for item in items:
        if not isinstance(item, dict):
            raise DatasetError(f"{cid}: evidence must be objects")
        eid = str(item.get("id") or "")
        if not re.fullmatch(r"[1-9]\d{0,5}", eid) or eid in seen:
            raise DatasetError(f"{cid}: invalid/duplicate citation ID")
        if not isinstance(item.get("text"), str) or not item["text"].strip():
            raise DatasetError(f"{cid}: empty source text")
        seen.add(eid)
        evidence_objects.append(SimpleNamespace(
            evidence_id=eid, content=item["text"],
            citation_label=str(item.get("label") or ""),
        ))
    allowed = tuple(sorted(seen))
    # Prepared model messages do not authorize citations. The dataset evidence
    # list itself is the sole source of the exact immutable allow-list.
    structural = validate_enterprise_answer(
        case["answer"], [], route="ENGINEERING_AGENT", allowed_ids=allowed,
    )
    verdict = structural
    if structural.accepted:
        verdict = validate_claim_anchors(
            case["answer"], admitted_evidence=evidence_objects, allowed_ids=allowed,
        )
    actual = verdict.accepted
    expected = case["expected_accept"]
    return {
        "case_id": cid, "dataset_type": kind, "matched": actual == expected,
        "expected_accept": expected, "actual_accept": actual, "reason": verdict.reason,
    }


def evaluate_jsonl(path: Path) -> dict[str, Any]:
    rows = []
    seen = set()
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise DatasetError("row must be an object")
            row = evaluate_case(value)
        except (ValueError, TypeError) as exc:
            raise DatasetError(f"line {lineno}: {exc}") from exc
        if row["case_id"] in seen:
            raise DatasetError(f"line {lineno}: duplicate case_id")
        seen.add(row["case_id"])
        rows.append(row)
    if not rows:
        raise DatasetError("empty dataset")
    return {
        "dataset_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "total": len(rows), "matched": sum(x["matched"] for x in rows),
        "mismatched": sum(not x["matched"] for x in rows),
        "synthetic_cases": sum(x["dataset_type"] == "SYNTHETIC_CONTRACT" for x in rows),
        "human_reviewed_real_cases": sum(x["dataset_type"] == "HUMAN_REVIEWED_REAL" for x in rows),
        "proves_semantic_entailment": False,
        "production_acceptance": "NOT_ASSESSED",
        "cases": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path, help="Offline, explicitly labeled JSONL cases")
    parser.add_argument("--output", type=Path, help="Optional JSON report")
    args = parser.parse_args()
    try:
        result = evaluate_jsonl(args.dataset)
    except (DatasetError, OSError) as exc:
        parser.error(str(exc))
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 1 if result["mismatched"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
