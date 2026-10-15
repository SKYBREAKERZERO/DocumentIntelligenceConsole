"""Read-only, private Stage 1 review of native pipeline JSON (not Gold)."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.engineering_intelligence.pipeline_json_review import read_pipeline_files, review_pipeline_documents


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline Stage1 review of pipeline JSON; no source text exported")
    parser.add_argument("--json", type=Path, action="append", required=True, help="Repeat for each file, max 8")
    parser.add_argument("--report-out", type=Path, required=True)
    args = parser.parse_args(argv)
    if any(path.resolve() == args.report_out.resolve() for path in args.json):
        parser.error("Output cannot overwrite any source")
    try:
        report = review_pipeline_documents(read_pipeline_files(args.json))
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(args.report_out, flags, 0o600)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write((json.dumps(report, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
        except BaseException:
            args.report_out.unlink(missing_ok=True)
            raise
    except (ValueError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "NOT_REVIEWED", "error_type": type(exc).__name__}), file=sys.stderr)
        return 2
    print(json.dumps({"status": report["status"], "document_count": report["document_count"],
                      "finding_count": report["finding_count"], "identity_candidates": len(report["identity_candidates"]),
                      "source_verified": False, "manual_review_required": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
