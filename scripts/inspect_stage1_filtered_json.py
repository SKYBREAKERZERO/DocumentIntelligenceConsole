"""Offline diagnostic for operator-filtered Stage 1 JSON; no private text output."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.engineering_intelligence.filtered_json_diagnostics import (
    diagnostic_dict, read_limited_json,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Diagnose filtered Stage1 JSON locally (not accuracy evaluation)")
    parser.add_argument("--json", required=True, type=Path)
    parser.add_argument("--report-out", required=True, type=Path,
                        help="New file only; metadata-only report, no source text")
    parser.add_argument("--max-cases", type=int, default=2_000)
    parser.add_argument("--max-chars", type=int, default=5_000_000)
    parser.add_argument("--max-findings-per-case", type=int, default=12)
    args = parser.parse_args(argv)
    if args.json.resolve() == args.report_out.resolve():
        parser.error("Input and output must be different files")
    try:
        data = read_limited_json(args.json)
        report = diagnostic_dict(data, max_cases=args.max_cases,
            max_chars=args.max_chars, max_findings_per_case=args.max_findings_per_case)
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(args.report_out, flags, 0o600)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write((json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        except BaseException:
            args.report_out.unlink(missing_ok=True)
            raise
    except (ValueError, UnicodeError, OSError, json.JSONDecodeError) as exc:
        # Never echo a source text value or a local private path in exceptions.
        print(json.dumps({"status": "NOT_EVALUATED", "error_type": type(exc).__name__,
                          "manual_review_required": True}), file=sys.stderr)
        return 2
    print(json.dumps({"status": report["result"], "case_count": report["case_count"],
        "requirement_candidates": report["requirement_candidates"],
        "quality_metrics_available": False, "source_verified": False,
        "manual_privacy_review_before_external_sharing": True}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
