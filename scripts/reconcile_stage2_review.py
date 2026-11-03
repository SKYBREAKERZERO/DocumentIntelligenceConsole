"""Read-only audit reconciliation after a lost PostgreSQL review response."""
from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.engineering_intelligence.review_outcome_reconciliation import reconcile_review_commit


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Stage 2 uncertain-COMMIT audit check")
    parser.add_argument("--database", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--candidate-id", required=True, type=int)
    parser.add_argument("--before-audit-id", required=True, type=int)
    parser.add_argument("--expected-status", required=True, choices=("REVIEW_REQUIRED", "CONFIRMED", "REJECTED"))
    parser.add_argument("--intended-status", required=True, choices=("REVIEW_REQUIRED", "CONFIRMED", "REJECTED"))
    parser.add_argument("--source-sha256", required=True)
    parser.add_argument("--target-sha256", required=True)
    parser.add_argument("--reason-sha256", required=True, help="SHA-256 of stripped original review reason UTF-8")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5432)
    parser.add_argument("--user", required=True, help="Same authenticated reviewer as original submission")
    parser.add_argument("--password-env", default="POSTGRES_PASSWORD")
    args = parser.parse_args(argv)
    if (not 1 <= args.port <= 65535 or args.candidate_id < 1 or args.before_audit_id < 0
            or args.database.lower() in {"rag", "rag_qabug"}
            or args.project.upper() in {"QABUG", "RAG_QABUG"}
            or args.user.lower() in {"root", "postgres"}):
        parser.error("Engineering database and restricted authenticated reviewer required")
    import psycopg

    password = os.environ.get(args.password_env) or getpass.getpass("Reviewer PostgreSQL password: ")
    with psycopg.connect(host=args.host, port=args.port, dbname=args.database,
                         user=args.user, password=password, connect_timeout=10,
                         application_name="dic-stage2-commit-reconciliation") as conn:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            cur.execute("SET LOCAL lock_timeout = '5s'")
            cur.execute("SET LOCAL statement_timeout = '60s'")
            cur.execute("SELECT current_database(), project_code FROM public.project_info")
            identities = cur.fetchall()
            if len(identities) != 1 or identities[0][0] != args.database or identities[0][1] != args.project:
                raise RuntimeError("Connected database/project identity mismatch")
            outcome = reconcile_review_commit(
                cur, candidate_id=args.candidate_id, project=args.project,
                before_audit_id=args.before_audit_id, expected_status=args.expected_status,
                intended_status=args.intended_status, source_hash=args.source_sha256,
                target_hash=args.target_sha256, reason_sha256=args.reason_sha256,
            )
        conn.rollback()
    print(json.dumps(outcome.as_dict(), ensure_ascii=False, indent=2))
    return 0 if outcome.status == "MATCHING_COMMIT_OBSERVED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
