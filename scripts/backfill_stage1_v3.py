"""Explicit local Stage1 v3 derivation; dry-run by default, no cloud calls.

Requires v7+v2+v3 schema and complete v2 source receipts. Never edits source
requirements, approved relation rows or historical mapping decisions.
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.engineering_intelligence.stage1_v3 import backfill_stage1_v3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Local Stage1 v3 atomic/table derived backfill')
    parser.add_argument('--database', required=True)
    parser.add_argument('--project', required=True, choices=('21MM', '24MM'))
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=5432)
    parser.add_argument('--user', default='postgres')
    parser.add_argument('--password-env', default='POSTGRES_PASSWORD')
    parser.add_argument('--version-id', type=int, help='Limit to one exact version ID')
    parser.add_argument('--max-versions', type=int, default=20)
    parser.add_argument('--max-units', type=int, default=40_000)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--backup-confirmed', action='store_true')
    args = parser.parse_args(argv)
    if (args.database.strip().lower() != 'rag_' + args.project.lower()
        or args.max_versions < 1 or args.max_units < 1 or args.port < 1 or args.port > 65535
        or (args.version_id is not None and args.version_id < 1)):
        parser.error('Invalid project/database identity or bounds')
    if args.apply and not args.backup_confirmed:
        parser.error('--apply requires --backup-confirmed and a verified database backup')
    import psycopg
    secret = os.environ.get(args.password_env) or getpass.getpass('PostgreSQL password: ')
    done = failures = 0
    with psycopg.connect(host=args.host,port=args.port, dbname=args.database,
                         user=args.user,password=secret,connect_timeout=10,
                         application_name='dic-stage1-v3-derivation') as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT project_code FROM public.project_info LIMIT 1')
            identity = cur.fetchone()
            if not identity or str(identity[0]).upper() != args.project:
                raise RuntimeError('Project/database identity mismatch; no changes were made')
            cur.execute('''SELECT DISTINCT sv.id FROM public.specification_versions sv
                JOIN public.specifications sp ON sp.id=sv.specification_id
                JOIN public.ei_version_backfill_receipts rec ON rec.version_id=sv.id
                WHERE sp.project_code=%s AND (%s IS NULL OR sv.id=%s)
                ORDER BY sv.id LIMIT %s''',
                (args.project,args.version_id,args.version_id,args.max_versions))
            version_ids = [int(row[0]) for row in cur.fetchall()]
        conn.commit()
        if args.version_id and not version_ids:
            raise RuntimeError('Requested version not present or not previously backfilled')
        for vid in version_ids:
            try:
                with conn.cursor() as cur:
                    cur.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
                    result = backfill_stage1_v3(
                        cur,version_id=vid,dry_run=not args.apply,max_units=args.max_units,
                    )
                if args.apply:
                    conn.commit()
                else:
                    conn.rollback()
                done += 1
                print(f"{'APPLIED' if args.apply else 'PREVIEW'} version={vid} "
                      f"units={result.source_units} clauses={result.atomic_candidates} "
                      f"tables={result.table_blocks} requires_review={result.review_required}",flush=True)
            except Exception as exc:
                conn.rollback()
                failures += 1
                print(f'FAILED version={vid}: {type(exc).__name__}: {exc}',file=sys.stderr)
        print(f'Stage1 v3 completed={done} failed={failures}')
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
