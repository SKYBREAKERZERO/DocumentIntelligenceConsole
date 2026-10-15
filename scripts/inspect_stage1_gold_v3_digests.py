"""Print only per-case review SHA-256 for completed *human* annotations.

This helper does not approve anything, edit a gold file, contact a server, or
print engineering source text. The second reviewer must still inspect source,
labels and slices before entering their own ID/digest in the gold file.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.engineering_intelligence.stage1_evaluation_v3 import (
    GOLD_SCHEMA_V3, case_label_digest, case_source_fingerprint,
)


def inspect_label_digests(data: dict) -> list[dict]:
    if data.get("schema") != GOLD_SCHEMA_V3 or not isinstance(data.get("cases"), list):
        raise ValueError("Expected stage1-gold-v3")
    cases = data["cases"]
    if data.get("dataset", {}).get("source_fingerprint") != case_source_fingerprint(cases):
        raise ValueError("Original source identities / slice assignment drift")
    result = []
    for c in cases:
        if c.get("annotation_state") != "READY_FOR_REVIEW":
            continue
        if (not isinstance(c.get("expected_requirement_spans"), list)
                or not isinstance(c.get("scope_annotations"), list)):
            raise ValueError("Incomplete human labels")
        import hashlib
        fingerprint = hashlib.sha256(json.dumps(
            [c["project"], c["document_id"], c["content_id"]],
            ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()[:20]
        result.append({"source_identity_sha256_prefix": fingerprint,
                       "label_digest_for_manual_review": case_label_digest(c)})
    return result


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Redacted candidate digests; NOT an approval")
    p.add_argument("--gold", required=True, type=Path)
    args = p.parse_args(argv)
    if not args.gold.is_file() or args.gold.stat().st_size > 10_000_000:
        p.error("Missing/oversized gold file")
    results = inspect_label_digests(json.loads(args.gold.read_text(encoding="utf-8")))
    print(json.dumps({"not_an_approval": True, "ready_for_review": results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
