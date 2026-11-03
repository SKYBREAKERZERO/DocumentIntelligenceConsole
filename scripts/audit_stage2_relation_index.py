"""Audit a single engineering SPEC_BODY index in a read-only PG snapshot."""
from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.engineering_intelligence.relation_index_integrity import inspect_relation_index


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage 2 read-only SPEC_BODY index-integrity audit")
    parser.add_argument("--database", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--spec-code", required=True)
    parser.add_argument("--user", required=True, help="Dedicated read-only PostgreSQL role")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5432)
    parser.add_argument("--password-env", default="POSTGRES_PASSWORD")
    parser.add_argument("--output", type=Path, help="New JSON report file (no overwrite)")
    args = parser.parse_args(argv)
    if (not args.database or args.database.lower() in {'rag', 'rag_qabug'}
            or not 1 <= args.port <= 65535 or not args.user
            or args.user.lower() in {'postgres', 'root'}):
        parser.error("An individual engineering database and restricted reader LOGIN are required")
    if args.output and (args.output.exists() or not args.output.parent.is_dir()):
        parser.error("Report parent must exist; report must not already exist")
    import psycopg
    password = os.environ.get(args.password_env) or getpass.getpass("Read-only PostgreSQL password: ")
    with psycopg.connect(host=args.host, port=args.port, dbname=args.database,
                         user=args.user, password=password, connect_timeout=10,
                         application_name="dic-stage2-index-integrity") as conn:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            cur.execute("SET LOCAL lock_timeout='5s'")
            cur.execute("SET LOCAL statement_timeout='90s'")
            cur.execute("SELECT current_database(),project_code FROM public.project_info")
            identities = cur.fetchall()
            if len(identities) != 1 or tuple(identities[0]) != (args.database, args.project):
                raise RuntimeError("Connected database/project identity mismatch")
            result = inspect_relation_index(cur, project=args.project, spec_code=args.spec_code)
        conn.rollback()
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        with args.output.open('x', encoding='utf-8') as file:
            file.write(text)
    print(text, end='')
    return 0 if result['status'] == 'PASS' else 2


if __name__ == '__main__':
    raise SystemExit(main())
