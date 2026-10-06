from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.chunk_evidence_mapping import ChunkEvidenceMappingService


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit RAG v6 Evidence -> Chunk exact mappings."
    )
    parser.add_argument("--host", default=os.getenv("POSTGRES_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("POSTGRES_PORT", "5432")))
    parser.add_argument("--database", default=os.getenv("POSTGRES_DB", "rag_24mm"))
    parser.add_argument("--user", default=os.getenv("POSTGRES_USER", "postgres"))
    parser.add_argument("--password-env", default="POSTGRES_PASSWORD")
    parser.add_argument("--show-broken", action="store_true")
    parser.add_argument("--strict", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    password = os.getenv(args.password_env)
    if password is None:
        print(f"ERROR: environment variable {args.password_env} is not set.", file=sys.stderr)
        return 2

    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        print("ERROR: psycopg is required.", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        return 2

    def connect():
        return psycopg.connect(
            host=args.host,
            port=args.port,
            dbname=args.database,
            user=args.user,
            password=password,
            row_factory=dict_row,
            connect_timeout=10,
        )

    service = ChunkEvidenceMappingService(connect)
    summary = service.audit_summary()
    print(f"Database: {args.database}")
    for key in ("EXACT", "CONTENT_CHANGED", "IDENTITY_CHANGED", "CHUNK_MISSING", "UNMAPPED", "TOTAL"):
        print(f"{key:18s} {summary.get(key, 0)}")
    print(f"EXACT_RATIO        {service.exact_ratio():.6f}")

    if args.show_broken:
        rows = service.list_by_status(
            ("CONTENT_CHANGED", "IDENTITY_CHANGED", "CHUNK_MISSING", "UNMAPPED"),
            limit=10000,
        )
        for row in rows:
            print(
                "BROKEN",
                f"evidence_id={row.get('evidence_id')}",
                f"relation_id={row.get('relation_id')}",
                f"content_id={row.get('content_id')}",
                f"status={row.get('mapping_status')}",
            )

    if args.strict:
        service.assert_no_broken_mappings()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
