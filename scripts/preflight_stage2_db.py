"""Run an opt-in read-only Stage 2 permission preflight as the audit DBA."""
from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.engineering_intelligence.stage2_db_preflight import inspect_stage2_db


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only PostgreSQL Stage 2 authorization preflight")
    parser.add_argument("--database", required=True, help="Exact target engineering DB name")
    parser.add_argument("--project", required=True, help="Exact single project_info.project_code")
    parser.add_argument("--reviewer", required=True, help="Dedicated human reviewer LOGIN to audit")
    parser.add_argument("--app-role", action="append", default=[], help="Optional app/worker LOGIN (repeat)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5432)
    parser.add_argument("--audit-user", required=True, help="DBA read-only auditing LOGIN with registry visibility")
    parser.add_argument("--password-env", default="POSTGRES_PASSWORD")
    parser.add_argument("--output", type=Path, help="Exclusive JSON report; contains role names, no secrets")
    args = parser.parse_args(argv)
    if not args.database.strip() or not 1 <= args.port <= 65535:
        parser.error("Explicit database and valid port required")
    if args.database.lower() in {"rag", "rag_qabug"}:
        parser.error("Specify an individual engineering project database")
    if args.output and (args.output.exists() or not args.output.parent.is_dir()):
        parser.error("Output parent must exist and report must not already exist")

    import psycopg  # Optional until operator explicitly runs a DB preflight.

    password = os.environ.get(args.password_env) or getpass.getpass("Audit PostgreSQL password: ")
    with psycopg.connect(host=args.host, port=args.port, dbname=args.database,
                         user=args.audit_user, password=password, connect_timeout=10,
                         application_name="dic-stage2-db-preflight") as conn:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            cur.execute("SET LOCAL lock_timeout = '5s'")
            cur.execute("SET LOCAL statement_timeout = '90s'")
            report = inspect_stage2_db(cur, database=args.database, project=args.project,
                                       reviewer=args.reviewer, application_roles=tuple(args.app_role))
        conn.rollback()
    output = json.dumps(report.as_dict(), ensure_ascii=False, indent=2) + "\n"
    if args.output:
        # x prevents clobbering previously signed/audited evidence artifacts.
        with args.output.open("x", encoding="utf-8", newline="\n") as f:
            f.write(output)
    print(output, end="")
    return 0 if report.passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
