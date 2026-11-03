"""Stage 2 offline content relation graph exporter; PostgreSQL read-only by design."""
from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.engineering_intelligence.content_relation_graph_report import (
    ALLOWED_STATUSES,
    build_graph_snapshot,
    verify_graph_bundle,
    write_graph_bundle,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Stage 2 content relation graph snapshot")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--output", type=Path, help="Create a NEW private HTML/JSON export directory")
    mode.add_argument("--verify", type=Path, help="Verify a previously exported bundle (offline)")
    parser.add_argument("--database", help="Engineering PostgreSQL DB (not QA/BUG)")
    parser.add_argument("--project", help="Exact project code, e.g. 21MM")
    parser.add_argument("--status", action="append", choices=sorted(ALLOWED_STATUSES))
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5432)
    parser.add_argument("--user", help="Dedicated read-only database principal")
    parser.add_argument("--password-env", default="POSTGRES_PASSWORD")
    args = parser.parse_args(argv)
    if args.verify:
        result = verify_graph_bundle(args.verify)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    if (not args.project or not args.database or not args.user
            or not 1 <= args.limit <= 200 or args.database.lower() in {"rag", "rag_qabug"}
            or args.project.upper() in {"QABUG", "RAG_QABUG"}):
        parser.error("Export requires --database, --project, --user, --limit 1..200; QA/BUG is forbidden")
    if args.output.exists() or args.output.is_symlink() or not args.output.parent.is_dir():
        parser.error("--output must be a NEW directory under an existing parent")
    import psycopg

    password = os.environ.get(args.password_env) or getpass.getpass("PostgreSQL password: ")
    with psycopg.connect(
        host=args.host, port=args.port, dbname=args.database, user=args.user,
        password=password, connect_timeout=10, application_name="dic-stage2-graph-export",
    ) as conn:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            cur.execute("SELECT project_code FROM public.project_info ORDER BY project_code LIMIT 2")
            identities = cur.fetchall()
            if len(identities) != 1 or str(identities[0][0]).upper() != args.project.upper():
                raise RuntimeError("Database project identity mismatch; graph export refused")
            snapshot = build_graph_snapshot(
                cur, project=args.project,
                statuses=args.status or ("REVIEW_REQUIRED",), limit=args.limit,
            )
        conn.rollback()
    hashes = write_graph_bundle(snapshot, args.output)
    print(json.dumps({"output": str(args.output), "counts": snapshot["counts"],
                      "sha256": hashes}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
