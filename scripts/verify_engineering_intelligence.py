"""Read-only Engineering Intelligence source/schema/backfill integrity report.

No external HTTP, no AI calls, no writes. Designed for Windows Python 3.12.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.engineering_intelligence.persistence import schema_ready, stage1_v2_schema_ready


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Engineering Intelligence quality report")
    parser.add_argument("--database", required=True)
    parser.add_argument("--project", required=True, choices=("21MM", "24MM"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5432)
    parser.add_argument("--user", default="postgres")
    parser.add_argument("--password-env", default="POSTGRES_PASSWORD")
    args = parser.parse_args(argv)
    if args.database.lower() in {"rag", "rag_qabug"}:
        parser.error("Use a project-specific specification database")

    import psycopg
    password = os.environ.get(args.password_env) or getpass.getpass("PostgreSQL password: ")
    with psycopg.connect(host=args.host, port=args.port, dbname=args.database,
                         user=args.user, password=password, connect_timeout=10,
                         application_name="dic-ei-integrity-readonly") as conn:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute("SELECT project_code FROM public.project_info LIMIT 1")
            row = cur.fetchone()
            if not row or str(row[0]).upper() != args.project:
                raise RuntimeError("Database identity mismatch")
            if not schema_ready(cur):
                print(json.dumps({"project": args.project, "status": "SCHEMA_INCOMPLETE"}, indent=2))
                return 2
            stage1_v2 = stage1_v2_schema_ready(cur)
            names = [
                "ei_requirement_units", "ei_content_relations",
                "ei_version_mappings", "ei_unresolved_relation_anchors",
                "ei_version_backfill_receipts", "ei_relation_review_audit",
            ]
            metrics: dict[str, int] = {}
            for name in names:
                # Only controlled, hard-coded table names are interpolated.
                cur.execute(f"SELECT COUNT(*) FROM public.{name}")
                metrics[name] = int(cur.fetchone()[0])
            cur.execute("""SELECT COUNT(*) FROM public.ei_content_relations
                           WHERE status='REVIEW_REQUIRED'""")
            metrics["unreviewed_content_relations"] = int(cur.fetchone()[0])
            cur.execute("""SELECT reason, COUNT(*) FROM public.ei_unresolved_relation_anchors
                           WHERE reason <> 'RESOLVED' GROUP BY reason""")
            reasons = {str(reason): int(count) for reason, count in cur.fetchall()}
            cur.execute("""SELECT COUNT(*) FROM public.specification_versions sv
                           JOIN public.specifications sp ON sp.id=sv.specification_id
                           WHERE sp.project_code=%s AND sv.branch_name='MAIN'
                             AND NOT EXISTS (
                                 SELECT 1 FROM public.ei_version_backfill_receipts r
                                 WHERE r.version_id=sv.id)
                        """, (args.project,))
            metrics["main_versions_without_receipts"] = int(cur.fetchone()[0])
            cur.execute("""SELECT COUNT(*) FROM public.ei_version_backfill_receipts r
                           JOIN public.specification_versions sv ON sv.id=r.version_id
                           WHERE r.source_chunk_count<>(
                               SELECT COUNT(*) FROM public.contents c
                               WHERE c.document_id=sv.document_id)
                        """)
            metrics["stale_chunk_count_receipts"] = int(cur.fetchone()[0])
            if stage1_v2:
                cur.execute("SELECT COUNT(*) FROM public.ei_requirement_enrichments")
                metrics["stage1_v2_enriched_units"] = int(cur.fetchone()[0])
                cur.execute("""SELECT COUNT(*) FROM public.ei_version_backfill_receipts r
                               WHERE r.extractor_version='requirement-units-v1'
                               AND NOT EXISTS (
                                   SELECT 1 FROM public.ei_stage1_enrichment_receipts e
                                   WHERE e.version_id=r.version_id AND e.extractor_version='requirement-units-v2'
                               )""")
                metrics["legacy_versions_needing_enrichment"] = int(cur.fetchone()[0])

    print(json.dumps({"project": args.project, "database": args.database,
                      "status": "OK" if stage1_v2 else "STAGE1_V2_SCHEMA_MISSING", "counts": metrics,
                      "unresolved_reasons": reasons}, ensure_ascii=False, indent=2))
    return 0 if stage1_v2 else 3


if __name__ == "__main__":
    raise SystemExit(main())
