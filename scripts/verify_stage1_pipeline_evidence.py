"""Rerun Stage1 diagnostics and verify every redacted source pointer offline.

NO original PDF/DOCX attestation, database read/write, or API/model requests.
Reports only the number of reproducible pipeline-JSON pointers, never source
text, filenames, identifiers or report snippets.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.engineering_intelligence.filtered_json_diagnostics import read_limited_json
from app.engineering_intelligence.pipeline_json_review import read_pipeline_files, review_pipeline_documents
from app.engineering_intelligence.stage1_review_evidence import verify_review_pointers


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline Stage 1 exported-JSON pointer verification")
    parser.add_argument("--json", type=Path, action="append", required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        documents = read_pipeline_files(args.json)
        report = read_limited_json(args.report, max_input_bytes=12_000_000)
        if report != review_pipeline_documents(documents):
            raise ValueError("Pipeline review report no longer matches exported JSON")
        outcome = verify_review_pointers(report, documents)
    except (ValueError, OSError, UnicodeError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "NOT_VERIFIED", "error_type": type(exc).__name__}), file=sys.stderr)
        return 2
    print(json.dumps(outcome, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
