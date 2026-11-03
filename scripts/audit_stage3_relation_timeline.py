"""Read-only v1.42 audit: a reviewed Stage 2 edge vs a later body version."""
from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.engineering_intelligence.relation_temporal_audit import assess_temporal_relation


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description='Stage 3 historical relation evidence audit')
    p.add_argument('--database', required=True)
    p.add_argument('--project', required=True)
    p.add_argument('--relation-id', required=True, type=int)
    p.add_argument('--new-target-version-id', required=True, type=int)
    p.add_argument('--user', required=True)
    p.add_argument('--host', default='127.0.0.1')
    p.add_argument('--port', default=5432, type=int)
    p.add_argument('--password-env', default='POSTGRES_PASSWORD')
    p.add_argument('--output', type=Path)
    args = p.parse_args(argv)
    if (args.database.lower() in {'rag', 'rag_qabug'} or not args.database
            or not 1 <= args.port <= 65535 or args.user.lower() in {'postgres', 'root'}
            or not args.user):
        p.error('Use an individual engineering project database and restricted reader')
    if args.output and (args.output.exists() or not args.output.parent.is_dir()):
        p.error('Output must be a new file in an existing directory')
    import psycopg
    from psycopg.rows import dict_row
    password = os.environ.get(args.password_env) or getpass.getpass('Read-only PG password: ')
    with psycopg.connect(host=args.host, port=args.port, dbname=args.database,
                         user=args.user, password=password, connect_timeout=10,
                         row_factory=dict_row,
                         application_name='dic-stage3-temporal-audit') as conn:
        with conn.cursor() as cur:
            cur.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            cur.execute("SET LOCAL lock_timeout='5s'")
            cur.execute("SET LOCAL statement_timeout='90s'")
            cur.execute('SELECT current_database() AS db,project_code FROM public.project_info')
            identity = cur.fetchall()
            if len(identity) != 1 or (identity[0]['db'], identity[0]['project_code']) != (args.database, args.project):
                raise RuntimeError('Connected database/project identity mismatch')
            report = assess_temporal_relation(cur, project=args.project,
                    relation_id=args.relation_id, new_target_version_id=args.new_target_version_id)
        conn.rollback()
    output = json.dumps(report, ensure_ascii=False, indent=2, default=str) + '\n'
    if args.output:
        with args.output.open('x', encoding='utf-8') as f:
            f.write(output)
    print(output, end='')
    return 2 if report['status'] == 'INSUFFICIENT_EVIDENCE' else 0


if __name__ == '__main__':
    raise SystemExit(main())
