"""Read-only proof-gated native PDF/DOCX to PostgreSQL content span preview.

No SQL writes, cloud calls, file export, LLM inference or fuzzy matches.
The stored document source_hash must equal the actual native file SHA-256.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.engineering_intelligence.native_layout import inspect_native_file
from app.engineering_intelligence.persistence import load_version_units, require_complete_backfill
from app.engineering_intelligence.source_binding import SourceContent, bind_native_to_sql
from app.engineering_intelligence.stage1_v3 import verify_live_source_snapshot


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage1 v1.12 native-to-SQL exact source candidate preview (read-only)")
    parser.add_argument('--file', type=Path, required=True)
    parser.add_argument('--project', choices=('21MM', '24MM'), required=True)
    parser.add_argument('--database', required=True)
    parser.add_argument('--version-id', required=True, type=int)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', default=5432, type=int)
    parser.add_argument('--user', default='postgres')
    parser.add_argument('--password-env', default='POSTGRES_PASSWORD')
    parser.add_argument('--max-contents', default=20_000, type=int)
    parser.add_argument('--max-units', default=80_000, type=int)
    parser.add_argument('--max-source-chars', default=30_000_000, type=int)
    parser.add_argument('--native-table-review', action='store_true', help='Show redacted physical cell/source binding summaries; never confirms headers')
    parser.add_argument('--max-table-details', default=30, type=int)
    parser.add_argument('--header-review-table', default=None,
                        help='Explicit native table ID (DOCX:0 or PDF:1:0); preview only')
    parser.add_argument('--header-rows', default=None,
                        help='Explicit physical header rows, e.g. 0 or 0,1; not automatically approved')
    parser.add_argument('--review-proposal-key', default=None,
        help='Read-only: compare one existing Stage1 review proposal with verified native evidence')
    args = parser.parse_args(argv)
    if (args.database.lower() != 'rag_' + args.project.lower() or args.version_id < 1
            or args.max_units < 1 or args.max_contents < 1 or args.max_source_chars < 1
            or not 1 <= args.port <= 65535 or not 1 <= args.max_table_details <= 100):
        parser.error('Invalid database/project identity or resource budgets')
    if bool(args.header_review_table) != bool(args.header_rows):
        parser.error('--header-review-table and --header-rows must be used together')
    if args.header_rows:
        try:
            rows = tuple(int(i) for i in args.header_rows.split(','))
        except ValueError:
            parser.error('Header rows must be comma-separated integers')
    else:
        rows = ()
    report = inspect_native_file(args.file)
    import psycopg
    secret = os.environ.get(args.password_env) or getpass.getpass('PostgreSQL password: ')
    with psycopg.connect(host=args.host, port=args.port, dbname=args.database,
                         user=args.user, password=secret, connect_timeout=10,
                         application_name='dic-stage1-native-binding-readonly') as conn:
        with conn.cursor() as cur:
            cur.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
            cur.execute('SELECT project_code FROM public.project_info LIMIT 1')
            identity = cur.fetchone()
            if not identity or str(identity[0]).upper() != args.project:
                raise ValueError('Project/database identity mismatch')
            cur.execute('''SELECT sv.document_id,d.source_hash FROM public.specification_versions sv
                JOIN public.specifications sp ON sp.id=sv.specification_id
                JOIN public.documents d ON d.document_id=sv.document_id
                WHERE sv.id=%s AND sp.project_code=%s''', (args.version_id, args.project))
            owned = cur.fetchone()
            if owned is None:
                raise ValueError('Requested specification version not in project')
            doc_id, trusted_hash = owned
            # Enforce file provenance *before* fetching the complete SQL document.
            if not trusted_hash or str(trusted_hash).lower() != report.file_sha256.lower():
                raise ValueError('SQL document source_hash does not match native binary; refusing candidate binding')
            require_complete_backfill(cur, args.version_id)
            verify_live_source_snapshot(cur, version_id=args.version_id, max_rows=args.max_contents)
            # Budget-check server-side before retrieving private full text into
            # the client process. An unbounded SELECT could exceed local RAM.
            cur.execute('''SELECT COUNT(*), COALESCE(SUM(length(content)),0)
                            FROM public.contents WHERE document_id=%s''', (doc_id,))
            source_count, source_chars = cur.fetchone()
            if int(source_count) > args.max_contents or int(source_chars) > args.max_source_chars:
                raise ValueError('SQL source exceeds local row/character budget')
            cur.execute('''SELECT id,page_number,content FROM public.contents WHERE document_id=%s
                ORDER BY id LIMIT %s''', (doc_id,args.max_contents + 1))
            source_rows = cur.fetchall()
            if len(source_rows) > args.max_contents:
                raise ValueError('Source contents exceed resource budget')
            contents = tuple(SourceContent(int(cid), str(doc_id), str(body or ''),
                                           int(page) if page is not None else None)
                             for cid,page,body in source_rows)
            units = load_version_units(cur, args.version_id, max_units=args.max_units + 1)
            if len(units) > args.max_units:
                raise ValueError('Requirement unit budget exceeded')
            bindings = bind_native_to_sql(
                report, document_id=str(doc_id), expected_source_file_sha256=str(trusted_hash),
                contents=contents, units=units, max_source_chars=args.max_source_chars,
            )
            review_detail = None
            if args.review_proposal_key:
                from app.engineering_intelligence.review_queue import load_review_detail
                review_detail = load_review_detail(
                    cur, project=args.project, version_id=args.version_id,
                    proposal_key=args.review_proposal_key)
    # Do not log document text, credentials, local file path or file contents.
    extra = {}
    comparison = None
    if review_detail is not None:
        from app.engineering_intelligence.review_native_compare import compare_review_to_native
        comparison = compare_review_to_native(review_detail, report, contents, bindings)
        extra['review_proposal_comparison'] = {
            'proposal_key': comparison.proposal_key, 'status': comparison.status,
            'source_sha256': comparison.source_sha256,
            'reviewed_cells': comparison.reviewed_cells,
            'exact_cells': comparison.exact_cells,
            'review_flags': list(comparison.review_flags),
            'human_review_required': comparison.human_review_required,
            # No raw source texts by default; admins use the local GUI for that.
            'matches': list(comparison.matches[:args.max_table_details]),
            'matches_truncated': len(comparison.matches) > args.max_table_details,
        }
    if args.native_table_review:
        from app.engineering_intelligence.native_review_bundle import native_bundle_summary
        bundle = native_bundle_summary(report, bindings)
        extra.update({'native_table_count': bundle['table_count'],
                 'crosspage_candidate_count': len(bundle['possible_crosspage']),
                 'native_table_review': bundle['tables'][:args.max_table_details],
                 'possible_crosspage': bundle['possible_crosspage'][:args.max_table_details],
                 'table_details_truncated': bundle['table_count'] > args.max_table_details,
                 'native_reviews_are_unapproved': True})
    if args.header_review_table:
        from app.engineering_intelligence.table_header_review import (
            preview_table_header_columns, redacted_header_review)
        if comparison is not None:
            from app.engineering_intelligence.table_header_review import require_proposal_matches_header_table
            require_proposal_matches_header_table(comparison, args.header_review_table)
        preview = preview_table_header_columns(
            report, bindings, table_identity=args.header_review_table, header_rows=rows)
        extra['explicit_header_review'] = redacted_header_review(preview)
    print(json.dumps({
        'version': 'stage1-v1.16', 'source_file_sha256': report.file_sha256,
        'document_id': str(doc_id), 'version_id': args.version_id,
        'source_contents': len(contents), 'requirement_units': len(units),
        'native_anchors': len(bindings), 'status_counts': dict(Counter(b.status for b in bindings)),
        'review_only': True, 'authority': 'UNIQUE_EXACT_TEXT_SOURCE_CANDIDATE_NOT_APPROVED_SEMANTIC_RELATION',
        **extra,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
