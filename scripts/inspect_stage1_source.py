"""Read-only, local Stage-1 preview for a *user-authorized plain text file*.

No database writes, HTTP requests, cloud LLM calls or model downloads.
The report is printed to stdout; source text is never printed by default.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.engineering_intelligence.quality import review_requirement_units, summarize_requirement_units
from app.engineering_intelligence.requirements import extract_requirement_units


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage 1 deterministic local source quality inspection")
    parser.add_argument("--file", type=Path, required=True, help="UTF-8 plain-text source")
    parser.add_argument("--document-id", required=True)
    parser.add_argument("--content-id", required=True, type=int)
    parser.add_argument("--section", required=True)
    parser.add_argument("--page", type=int)
    parser.add_argument("--max-units", type=int, default=200)
    parser.add_argument("--max-source-chars", type=int, default=2_000_000)
    parser.add_argument("--detail", action="store_true", help="Print evidence coordinates, not source text")
    parser.add_argument("--atomic-table", action="store_true", help="Stage1 v3 review-only atomic and table counters")
    parser.add_argument("--scope", action="store_true", help="Review-only source-span condition/exception candidates")
    args = parser.parse_args(argv)
    # Bound by file size before allocating a potentially enormous UTF-8 string.
    if args.max_source_chars < 1 or args.max_units < 1:
        parser.error("Resource limits must be positive")
    if args.file.stat().st_size > args.max_source_chars * 4 + 4:
        parser.error("Input exceeds resource budget; review/split the source")
    text = args.file.read_text(encoding="utf-8-sig")
    units = extract_requirement_units(
        document_id=args.document_id, content_id=args.content_id,
        section_id=args.section, page_number=args.page, text=text,
        max_units=args.max_units, max_source_chars=args.max_source_chars,
    )
    findings = review_requirement_units(text, units)
    summary = summarize_requirement_units(units, findings)
    if args.atomic_table:
        from app.engineering_intelligence.stage1_v3 import prepare_stage1_v3
        from app.engineering_intelligence.quality import review_stage1_v3
        v3, atoms, tables = prepare_stage1_v3(version_id=1, units=units)
        summary['stage1_v3'] = {
            'atomic_candidates': v3.atomic_candidates,
            'atomic_review_required': v3.review_required,
            'table_blocks': v3.table_blocks,
            'table_headers_explicit': sum(t.header_status == 'EXPLICIT_MARKDOWN' for t in tables),
            'table_headers_unresolved': sum(t.header_status == 'UNRESOLVED' for t in tables),
            'review_findings': len(review_stage1_v3(units)),
        }
        if args.detail:
            summary['stage1_v3']['atomic_spans'] = [
                {'parent': a.parent_unit_key, 'start': a.start_in_content, 'end': a.end_in_content,
                 'role': a.role, 'review_reasons': a.review_reasons}
                for a in atoms
            ]
            summary['stage1_v3']['table_evidence'] = [
                {'key': t.table_key, 'header_status': t.header_status,
                 'review_reasons': t.review_reasons, 'rows': len(t.rows)} for t in tables
            ]
    if args.scope:
        from app.engineering_intelligence.atomic import extract_atomic_candidates
        from app.engineering_intelligence.condition_scope import extract_requirement_scope
        candidates = tuple(extract_requirement_scope(u, atom)
                           for u in units for atom in extract_atomic_candidates(u))
        from collections import Counter
        summary["condition_scope"] = {
            "review_only": True,
            "total": len(candidates),
            "status_counts": dict(Counter(x.status for x in candidates)),
            "condition_spans": sum(x.condition is not None for x in candidates),
            "exception_spans": sum(x.exception is not None for x in candidates),
            "explicitly_unresolved": sum(x.status == "UNRESOLVED" for x in candidates),
        }
        if args.detail:
            summary["condition_scope"]["evidence"] = [
                {"candidate": x.candidate_key, "status": x.status,
                 "reasons": x.review_reasons,
                 "condition": (x.condition.start_in_content, x.condition.end_in_content) if x.condition else None,
                 "exception": (x.exception.start_in_content, x.exception.end_in_content) if x.exception else None,
                 "action": (x.action.start_in_content, x.action.end_in_content) if x.action else None}
                for x in candidates
            ]
    if args.detail:
        summary["evidence"] = [
            {"unit_key": u.unit_key, "start": u.start_offset, "end": u.end_offset,
             "unit_type": u.unit_type, "structure": u.signals.structural_kind,
             "cue_types": [cue[0] for cue in u.signals.cue_spans]}
            for u in units
        ]
        summary["reviews"] = [asdict(f) for f in findings]
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
