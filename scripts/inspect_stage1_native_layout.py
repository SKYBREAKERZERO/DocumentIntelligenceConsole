"""Explicit, offline Stage1 native layout report; no SQL or cloud access."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.engineering_intelligence.native_layout import inspect_native_file
from app.engineering_intelligence.crosspage import suggest_crosspage_tables


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Read-only Stage1 DOCX/PDF native layout evidence")
    p.add_argument("--file", required=True, type=Path)
    p.add_argument("--output", type=Path, help="Explicit local destination for review JSON")
    p.add_argument("--include-source-text", action="store_true", help="Include sensitive source in JSON")
    p.add_argument("--pdf-table-geometry", action="store_true", help="Opt-in PDF table geometry candidates; never verified semantics")
    p.add_argument("--max-file-bytes", type=int, default=100_000_000)
    args = p.parse_args(argv)
    options = {"include_table_geometry": True} if args.pdf_table_geometry and args.file.suffix.lower() == ".pdf" else {}
    if args.pdf_table_geometry and args.file.suffix.lower() != ".pdf":
        p.error("--pdf-table-geometry requires a PDF file")
    result = inspect_native_file(args.file, max_file_bytes=args.max_file_bytes, **options)
    payload = result.as_dict(include_text=args.include_source_text)
    continuations = suggest_crosspage_tables(result) if args.pdf_table_geometry else ()
    payload["cross_page_review_candidates"] = [asdict(c) for c in continuations]
    payload["summary"] = {
        "tables": len(result.tables),
        "physical_cells": sum(len(t.cells) for t in result.tables),
        "docx_paragraphs": len(result.paragraphs),
        "pdf_lines": len(result.lines),
        "pdf_geometry_tables": len(result.pdf_table_candidates),
        "cross_page_review_candidates": len(continuations),
        "needs_review": sum(bool(t.review_reasons) for t in result.tables),
    }
    if args.output:
        # Explicit output only; never overwrite existing reports implicitly.
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as out:
            json.dump(payload, out, ensure_ascii=False, indent=2)
            out.write("\n")
        print(f"Local Stage1 report saved: {args.output} (sensitive source text included: {args.include_source_text})")
    else:
        print(json.dumps({k: payload[k] for k in
                          ("version", "format", "file_sha256", "source_size", "summary", "review_reasons")},
                         ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
