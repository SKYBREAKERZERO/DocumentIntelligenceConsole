"""Explicit, non-destructive legacy Stage-1 v1 -> v2 enrichment.

Never overwrites approved requirement units or reviewed relation rows. Old v1
units/receipts are retained. --apply requires independent PostgreSQL backup.
This command does not contact Qdrant, Ollama or the public internet.
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.engineering_intelligence.persistence import enrich_legacy_stage1_version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage 1 v2 legacy source enhancement")
    parser.add_argument("--database", required=True)
    parser.add_argument("--project", required=True, choices=("21MM", "24MM"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5432)
    parser.add_argument("--user", default="postgres")
    parser.add_argument("--password-env", default="POSTGRES_PASSWORD")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-confirmed", action="store_true",
                        help="Operator confirms an independently verified database backup")
    parser.add_argument("--max-versions", type=int, default=0)
    parser.add_argument("--max-chunks", type=int, default=100_000)
    parser.add_argument("--max-units-per-content", type=int, default=200)
    parser.add_argument("--max-chars-per-content", type=int, default=2_000_000)
    args = parser.parse_args(argv)
    if (args.database.lower() in {"rag", "rag_qabug"} or
        args.max_versions < 0 or args.max_chunks < 1 or
        not 1 <= args.max_units_per_content <= 10_000 or
        args.max_chars_per_content < 1):
        parser.error("Invalid database scope or resource budget")
    if args.apply and not args.backup_confirmed:
        parser.error("--apply requires --backup-confirmed after an independently verified backup")
    import psycopg
    password = os.environ.get(args.password_env) or getpass.getpass("PostgreSQL password: ")
    completed = failed = 0
    with psycopg.connect(
        host=args.host, port=args.port, dbname=args.database, user=args.user,
        password=password, connect_timeout=10,
        application_name="dic-stage1-v2-legacy-enrichment",
    ) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT project_code FROM public.project_info LIMIT 1")
            identity = cur.fetchone()
            if not identity or str(identity[0]).upper() != args.project:
                raise RuntimeError("Project/database identity mismatch")
            cur.execute("""SELECT r.version_id, r.document_id
                           FROM public.ei_version_backfill_receipts r
                           JOIN public.specification_versions sv ON sv.id=r.version_id
                           JOIN public.specifications sp ON sp.id=sv.specification_id
                           WHERE sp.project_code=%s AND r.extractor_version='requirement-units-v1'
                           ORDER BY sp.id, r.version_id""", (args.project,))
            old = cur.fetchall()
        conn.commit()
        for vid, doc in old[:args.max_versions or None]:
            try:
                with conn.cursor() as cur:
                    cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                    rows, units = enrich_legacy_stage1_version(
                        cur, version_id=int(vid), document_id=str(doc),
                        max_rows=args.max_chunks, max_units_per_content=args.max_units_per_content,
                        max_chars_per_content=args.max_chars_per_content,
                        dry_run=not args.apply,
                    )
                conn.commit()  # each version is an atomic transaction
                completed += 1
                print(f"{'ENRICH' if args.apply else 'CHECK'} version={vid} chunks={rows} units={units}", flush=True)
            except Exception as exc:
                conn.rollback()
                failed += 1
                print(f"FAIL version={vid} {type(exc).__name__}: {exc}",file=sys.stderr)
        print(f"Legacy Stage1 v2: completed={completed} failed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
