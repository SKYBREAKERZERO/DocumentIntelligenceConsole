"""Live PG17 Stage 3 read-only role/schema smoke check; never makes changes."""
from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.engineering_intelligence.stage3_pg_runtime_check import inspect_stage3_runtime


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Read-only PostgreSQL 17 Stage 3 deployment smoke check')
    parser.add_argument('--database', required=True)
    parser.add_argument('--project', required=True)
    parser.add_argument('--user', required=True)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=5432)
    parser.add_argument('--password-env', default='POSTGRES_PASSWORD')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    if (not args.database or args.database.lower() in {'rag','rag_qabug'}
            or not args.user or args.user.lower() in {'postgres','root'}
            or not 1 <= args.port <= 65535):
        parser.error('Dedicated engineering database and non-admin reader required')
    if args.output and (args.output.exists() or not args.output.parent.is_dir()):
        parser.error('Output must be new and its parent existing')
    import psycopg
    from psycopg.rows import dict_row
    password = os.environ.get(args.password_env) or getpass.getpass('PG reader password: ')
    with psycopg.connect(host=args.host, port=args.port, dbname=args.database,
                         user=args.user, password=password, connect_timeout=10,
                         row_factory=dict_row, application_name='dic-stage3-runtime-smoke') as conn:
        try:
            with conn.cursor() as cur:
                cur.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
                cur.execute("SET LOCAL lock_timeout='5s'")
                cur.execute("SET LOCAL statement_timeout='60s'")
                report = inspect_stage3_runtime(cur, database=args.database, project=args.project)
        finally:
            conn.rollback()
    body = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        args.output.open('x',encoding='utf-8').write(body)
    print(body, end='')
    return 0 if report['status'] == 'PASS' else 2


if __name__ == '__main__':
    raise SystemExit(main())
