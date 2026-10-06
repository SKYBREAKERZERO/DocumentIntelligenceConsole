from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.relations.manager import EngineeringRelationManager


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill deterministic engineering relations for existing specification versions."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5432)
    parser.add_argument("--database", required=True)
    parser.add_argument("--user", default="postgres")
    parser.add_argument("--password-env", default="POSTGRES_PASSWORD")
    parser.add_argument("--project", choices=("21MM", "24MM"), required=True)
    parser.add_argument("--force", action="store_true", help="Create a new extraction generation even if a READY run exists for the same version.")
    parser.add_argument("--all-versions", action="store_true", help="Backfill historical MAIN versions too. Default: only each Specification current_version_id.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    password = os.environ.get(args.password_env)
    if not password:
        password = getpass.getpass(f"PostgreSQL password for {args.user}: ")

    manager = EngineeringRelationManager()
    processed = 0
    skipped = 0
    failed = 0

    with psycopg.connect(
        host=args.host,
        port=args.port,
        dbname=args.database,
        user=args.user,
        password=password,
        connect_timeout=10,
        application_name="dic-engineering-relation-backfill",
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT sv.specification_id, sv.id, sv.document_id, sv.version_label, sp.canonical_name
                FROM public.specification_versions sv
                JOIN public.specifications sp ON sp.id = sv.specification_id
                WHERE sp.project_code = %s
                  AND sv.branch_name = 'MAIN'
                  AND (%s OR sv.id = sp.current_version_id)
                ORDER BY sp.id, sv.version_sort_key NULLS LAST, sv.version_seq
                """,
                (args.project, bool(args.all_versions)),
            )
            versions = list(cur.fetchall())

        for specification_id, version_id, document_id, version_label, canonical_name in versions:
            try:
                with conn.cursor() as cur:
                    result = manager.ensure_for_version(
                        cur=cur,
                        specification_id=int(specification_id),
                        version_id=int(version_id),
                        document_id=str(document_id),
                        force=bool(args.force),
                    )
                    if result is None:
                        skipped += 1
                        print(f"SKIP  {canonical_name} {version_label}: relation schema not ready")
                    else:
                        processed += 1
                        print(f"OK    {canonical_name} {version_label}: {result.summary_text}")
                conn.commit()
            except Exception as exc:
                conn.rollback()
                failed += 1
                print(f"FAIL  {canonical_name} {version_label}: {exc}", file=sys.stderr)

        with conn.cursor() as cur:
            resolved = manager.reconcile_unresolved(cur=cur, project_code=args.project)
        conn.commit()

    print(
        f"DONE processed={processed} skipped={skipped} failed={failed} reconciled={resolved}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
