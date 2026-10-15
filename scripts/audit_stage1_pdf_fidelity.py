"""Offline, redacted native-PDF / Pipeline JSON literal alignment audit."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.engineering_intelligence.filtered_json_diagnostics import read_limited_json
from app.engineering_intelligence.native_layout import inspect_pdf_native
from app.engineering_intelligence.pipeline_json_review import read_pipeline_files
from app.engineering_intelligence.stage1_native_fidelity import reconcile_pdf_pipeline


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Stage1 PDF-vs-JSON read-only fidelity review (not semantic acceptance)")
    p.add_argument("--pdf", type=Path, required=True)
    p.add_argument("--json", type=Path, required=True, action="append")
    p.add_argument("--review", type=Path, required=True)
    p.add_argument("--document-index", type=int, default=1, help="1-based index in the supplied JSON list")
    p.add_argument("--report-out", type=Path, required=True)
    p.add_argument("--pdf-table-geometry", action="store_true", help="Opt in to geometric table candidates")
    p.add_argument("--max-contents", type=int, default=2000)
    p.add_argument("--max-file-bytes", type=int, default=100_000_000)
    try:
        args = p.parse_args(argv)
        inputs = [args.pdf, args.review, *args.json]
        if (args.pdf.suffix.lower() != ".pdf" or args.max_contents < 1
                or args.max_file_bytes < 1 or any(x.is_symlink() for x in inputs)
                or any(x.resolve() == args.report_out.resolve() for x in inputs)):
            raise ValueError("Invalid or overlapping input/output paths")
        docs = read_pipeline_files(args.json)
        review = read_limited_json(args.review, max_input_bytes=6_000_000)
        native = inspect_pdf_native(args.pdf, max_file_bytes=args.max_file_bytes,
                                    include_table_geometry=args.pdf_table_geometry)
        audit = reconcile_pdf_pipeline(docs, review, native, document_index=args.document_index,
                                       max_contents=args.max_contents)
        # Only create the requested destination; never overwrite another report.
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(args.report_out, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(audit, f, indent=2, ensure_ascii=False)
            f.write("\n")
    except (ValueError, OSError, UnicodeError, json.JSONDecodeError, RuntimeError) as exc:
        print(json.dumps({"status": "NOT_AUDITED", "error_type": type(exc).__name__}), file=sys.stderr)
        return 2
    print(json.dumps({"status": audit["status"], "contents_sampled": audit["contents_sampled"],
                      "contents_not_sampled": audit["contents_not_sampled"],
                      "native_pdf_page_count": audit["native_pdf_page_count"],
                      "semantic_acceptance_complete": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
