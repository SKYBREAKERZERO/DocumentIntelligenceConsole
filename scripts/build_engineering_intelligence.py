"""Explicit offline backfill for Engineering Intelligence v7 (Windows compatible).

By default this is read-only. --apply writes ONLY ei_* derived tables.
No Qdrant calls, no Ollama calls, no external HTTP calls.
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.storage.primary_document_policy import PrimarySpecificationDocumentPolicy
from app.engineering_intelligence.persistence import (
    backfill_content_relations, backfill_version, backfill_version_mappings,
    ensure_schema_ready, require_stage1_v2_schema,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local Engineering Intelligence v7 backfill")
    parser.add_argument("--database", required=True)
    parser.add_argument("--project", required=True, choices=("21MM", "24MM"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5432)
    parser.add_argument("--user", default="postgres")
    parser.add_argument("--password-env", default="POSTGRES_PASSWORD")
    parser.add_argument("--apply", action="store_true", help="Explicitly write additive ei_* derived rows")
    parser.add_argument("--all-versions", action="store_true", help="Also extract immutable historical versions")
    parser.add_argument("--relations", action="store_true", help="Generate exact-spec-and-section relation candidates")
    parser.add_argument("--version-mapping", action="store_true", help="Map consecutive MAIN versions")
    parser.add_argument("--max-chunks", type=int, default=100000)
    parser.add_argument("--max-units-per-content", type=int, default=200,
                        help="Fail-closed extraction cap per source Chunk; use a reviewed higher value for long source rows")
    parser.add_argument("--max-chars-per-content", type=int, default=2_000_000,
                        help="Fail closed on unusually large single source content; never truncate")
    parser.add_argument("--max-units", type=int, default=10000)
    parser.add_argument("--limit-specifications", type=int, default=0)
    args = parser.parse_args(argv)
    if args.max_chunks < 1 or args.max_units < 1 or args.limit_specifications < 0 or not 1 <= args.max_units_per_content <= 10000 or args.max_chars_per_content < 1:
        parser.error("positive bounds required")
    if args.version_mapping and not args.all_versions:
        parser.error("--version-mapping requires --all-versions to avoid incomplete mappings")
    if args.database.strip().lower() in ("rag", "rag_qabug"):
        parser.error("This command is for an explicit 21MM / 24MM specification database only")
    import psycopg
    password = os.environ.get(args.password_env) or getpass.getpass("PostgreSQL password: ")
    print("MODE:", "APPLY derived ei_* tables" if args.apply else "DRY RUN (read-only)", flush=True)
    with psycopg.connect(
        host=args.host, port=args.port, dbname=args.database, user=args.user,
        password=password, connect_timeout=10, application_name="dic-intelligence-v7-backfill",
    ) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT project_code FROM public.project_info LIMIT 1")
            row = cur.fetchone()
            if not row or str(row[0]).upper() != args.project:
                raise RuntimeError("Database identity mismatch; refusing cross-project writes")
            if args.apply:
                ensure_schema_ready(cur)
                require_stage1_v2_schema(cur)
            primary = PrimarySpecificationDocumentPolicy.sql_primary_predicate("d")
            cur.execute(
                f"""
                SELECT sv.specification_id, sv.id, sv.document_id, sp.spec_code,
                       sv.previous_version_id, sv.version_label
                FROM public.specification_versions sv
                JOIN public.specifications sp ON sp.id=sv.specification_id
                JOIN public.documents d ON d.document_id=sv.document_id
                WHERE sp.project_code=%s AND sv.branch_name='MAIN'
                  AND (%s OR sv.id=sp.current_version_id)
                  AND {primary}
                ORDER BY sp.id, sv.version_seq, sv.id
                """, (args.project, args.all_versions)
            )
            versions = cur.fetchall()
        conn.commit()  # close catalog read transaction before per-version snapshot
        unique_specs: set[int] = set()
        chosen = []
        for row in versions:
            if args.limit_specifications and row[0] not in unique_specs and len(unique_specs)>=args.limit_specifications:
                break
            unique_specs.add(int(row[0]))
            chosen.append(row)
        total_chunks = total_units = 0
        failed = 0
        for spec_id, vid, document_id, code, previous_id, label in chosen:
            try:
                with conn.cursor() as cur:
                    cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                    if args.apply:
                        cur.execute("""SELECT extractor_version FROM public.ei_version_backfill_receipts
                                       WHERE version_id=%s""", (vid,))
                        previous = cur.fetchone()
                        if previous and previous[0] != "requirement-units-v2":
                            raise ValueError(
                                "Legacy Stage1 data found. Use scripts/enrich_legacy_stage1_v2.py "
                                "after applying the additive v2 schema; do not overwrite reviewed units"
                            )
                    chunks, units = backfill_version(
                        cur, specification_id=int(spec_id), version_id=int(vid),
                        document_id=str(document_id), max_rows=args.max_chunks,
                        dry_run=not args.apply,
                        max_units_per_content=args.max_units_per_content,
                        max_chars_per_content=args.max_chars_per_content,
                    )
                    total_chunks += chunks
                    total_units += units
                    print(f"{'WRITE' if args.apply else 'CHECK'} {code or spec_id} {label}: chunks={chunks} units={units}", flush=True)
                # Avoid retaining long write transactions and busy connections.
                conn.commit()  # never retain a long-running dry-run transaction
            except Exception as exc:
                conn.rollback()
                failed += 1
                print(f"FAIL spec={spec_id} version={vid}: {type(exc).__name__}: {exc}", file=sys.stderr)
        if args.apply and args.version_mapping:
            for spec_id, vid, _doc, code, previous_id, label in chosen:
                if previous_id is None:
                    continue
                try:
                    with conn.cursor() as cur:
                        print(f"MAPPING {code} {label}: {backfill_version_mappings(cur,old_version_id=int(previous_id),new_version_id=int(vid), max_units=args.max_units)}")
                    conn.commit()
                except Exception as exc:
                    conn.rollback()
                    failed += 1
                    print(f"MAPPING FAIL {code}: {type(exc).__name__}: {exc}",file=sys.stderr)
        if args.apply and args.relations:
            for spec_id, vid, _doc, code, _previous_id, label in chosen:
                try:
                    with conn.cursor() as cur:
                        print(f"RELATIONS {code} {label}: {backfill_content_relations(cur,source_specification_id=int(spec_id), source_version_id=int(vid),source_spec_code=str(code or ''), max_units=args.max_units)}")
                    conn.commit()
                except Exception as exc:
                    conn.rollback()
                    failed += 1
                    print(f"RELATION FAIL {code}: {type(exc).__name__}: {exc}",file=sys.stderr)
        print(f"DONE chunks={total_chunks} units={total_units} specifications={len(unique_specs)} failures={failed}")
        return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
