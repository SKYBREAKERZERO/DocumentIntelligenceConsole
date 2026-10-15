"""Build a PRIVATE UNLABELLED Stage1 v3 annotation set. Never pre-approves labels."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.engineering_intelligence.stage1_evaluation_v3 import (
    COMPLEXITY_TAGS, GOLD_SCHEMA_V3, SOURCE_KINDS, SPLITS, case_source_fingerprint,
)


def build_unlabelled_v3(source: dict, *, dataset_id: str,
                        max_cases: int = 2000, max_chars: int = 5_000_000) -> dict:
    if not isinstance(source, dict) or source.get("schema") != "stage1-source-items-v1":
        raise ValueError("Expected stage1-source-items-v1")
    cases = source.get("cases")
    if not isinstance(cases, list) or not cases or len(cases) > max_cases:
        raise ValueError("Nonempty bounded cases required")
    if not isinstance(dataset_id, str) or not dataset_id.strip() or len(dataset_id) > 128:
        raise ValueError("Explicit dataset ID required")
    rows, used, budget = [], set(), 0
    for c in cases:
        if not isinstance(c, dict):
            raise ValueError("Invalid source item")
        doc, cid, sec, txt = (c.get(k) for k in ("document_id", "content_id", "section_id", "text"))
        project, kind, split, tags = (c.get(k) for k in (
            "project", "source_kind", "split", "complexity_tags"))
        if (not isinstance(doc, str) or not doc or type(cid) is not int or cid < 1
            or not isinstance(sec, str) or not sec or not isinstance(txt, str) or not txt
            or project not in ("21MM", "24MM") or kind not in SOURCE_KINDS
            or split not in SPLITS or not isinstance(tags, list) or not tags
            or len(tags) > 4 or any(tag not in COMPLEXITY_TAGS for tag in tags)
            or len(set(tags)) != len(tags) or ("NORMAL" in tags and len(tags) != 1)):
            raise ValueError("Source identity, project, split, physical kind and tags required")
        key = (project, doc, cid)
        if key in used:
            raise ValueError("Duplicate scoped source identity")
        used.add(key)
        budget += len(txt)
        if budget > max_chars:
            raise ValueError("Source character budget exceeded")
        digest = hashlib.sha256(txt.encode("utf-8", "surrogatepass")).hexdigest()
        if c.get("source_sha256", digest) != digest:
            raise ValueError("Source text checksum drift")
        rows.append({"project": project, "document_id": doc,
            "content_id": cid, "section_id": sec, "text": txt,
            "source_sha256": digest, "source_kind": kind, "split": split,
            "complexity_tags": tags, "annotation_state": "PENDING",
            "expected_requirement_spans": [], "scope_annotations": [],
            "independent_review": {
                "annotator_id": "", "reviewer_id": "", "review_note": "",
                "reviewed_label_sha256": "",
            }})
    return {"schema": GOLD_SCHEMA_V3,
            "dataset": {"dataset_id": dataset_id,
                        "source_fingerprint": case_source_fingerprint(rows)},
            "cases": rows}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Prepare unlabelled reviewed Stage1 holdout dataset (local only)")
    p.add_argument("--source-json", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--dataset-id", required=True)
    p.add_argument("--confirm-local-sensitive-copy", action="store_true")
    args = p.parse_args(argv)
    if not args.confirm_local_sensitive_copy:
        p.error("Explicit --confirm-local-sensitive-copy required; output includes proprietary source")
    if not args.source_json.is_file() or args.source_json.stat().st_size > 10_000_000:
        p.error("Missing/oversized source JSON")
    result = build_unlabelled_v3(json.loads(args.source_json.read_text(encoding="utf-8")),
                                 dataset_id=args.dataset_id)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(args.out, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write((json.dumps(result, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    except BaseException:
        args.out.unlink(missing_ok=True)
        raise
    print(json.dumps({"status": "UNLABELLED_NEEDS_INDEPENDENT_HUMAN_REVIEW",
        "case_count": len(result["cases"]), "source_fingerprint": result["dataset"]["source_fingerprint"],
        "contains_private_original_text": True, "no_quality_claim": True}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
