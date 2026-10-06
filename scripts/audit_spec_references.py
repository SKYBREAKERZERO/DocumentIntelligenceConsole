from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path
import sys

import psycopg

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit current deterministic cross-specification references (no Qdrant)."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5432)
    parser.add_argument("--database", required=True)
    parser.add_argument("--user", default="postgres")
    parser.add_argument("--password-env", default="POSTGRES_PASSWORD")
    parser.add_argument("--project", default=None)
    parser.add_argument("--unresolved-only", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    password = os.environ.get(args.password_env)
    if not password:
        password = getpass.getpass(f"PostgreSQL password for {args.user}: ")

    with psycopg.connect(
        host=args.host,
        port=args.port,
        dbname=args.database,
        user=args.user,
        password=password,
        connect_timeout=10,
        application_name="dic-spec-reference-audit",
    ) as conn:
        with conn.cursor() as cur:
            view = (
                "public.spec_reference_unresolved_current"
                if args.unresolved_only
                else "public.spec_reference_current_edges"
            )
            where = ""
            params: tuple[object, ...] = ()
            if args.project:
                where = (
                    " WHERE source_specification_id IN "
                    "(SELECT id FROM public.specifications WHERE project_code = %s)"
                )
                params = (args.project,)
            cur.execute(
                f"""
                SELECT source_specification_key,
                       COALESCE(target_specification_key, target_external_key, '<none>'),
                       extraction_method, confidence, status, evidence_count
                FROM {view}
                {where}
                ORDER BY source_specification_key, status, 2
                """,
                params,
            )
            rows = list(cur.fetchall())

    if not rows:
        print("No matching references.")
        return 0

    for source, target, method, confidence, status, evidence_count in rows:
        print(
            f"{status:<10} {source} -> {target} | "
            f"{method} | confidence={float(confidence):.3f} | evidence={evidence_count}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
