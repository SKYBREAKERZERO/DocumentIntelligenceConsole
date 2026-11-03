"""Stage 2: review full original two-ended content evidence with safe defaults.

Default preview does not write. --apply requires a trusted PostgreSQL
SECURITY DEFINER authority, dedicated reviewer login, expected state, both
prior-preview evidence hashes and a substantive reason.
"""
from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path
import sys

# Run from source root or arbitrary working directory on Windows.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.engineering_intelligence.content_relation_review import (
    load_review_case, list_relation_queue, review_content_relation_secured,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit and review a two-ended engineering relation")
    parser.add_argument("--database", required=True)
    parser.add_argument("--project", required=True, help="Exact project_info.project_code, not QA-BUG")
    options = parser.add_mutually_exclusive_group(required=True)
    options.add_argument("--candidate-id", type=int)
    options.add_argument("--list", action="store_true", help="List bounded candidate IDs in this project")
    parser.add_argument("--limit", type=int, default=50, help="1..200; only with --list")
    parser.add_argument("--status", choices=("CONFIRMED", "REJECTED", "REVIEW_REQUIRED"))
    parser.add_argument("--expected-status", choices=("REVIEW_REQUIRED", "CONFIRMED", "REJECTED"))
    parser.add_argument("--source-sha256", help="Exact SOURCE SHA-256 copied from prior preview")
    parser.add_argument("--target-sha256", help="Exact TARGET SHA-256 copied from prior preview")
    parser.add_argument("--reason")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5432)
    parser.add_argument("--user", required=True, help="Dedicated authenticated reviewer login (not postgres)")
    parser.add_argument("--password-env", default="POSTGRES_PASSWORD")
    parser.add_argument("--apply", action="store_true", help="Commit via dic_review restricted DB function")
    args = parser.parse_args(argv)
    if ((args.candidate_id is not None and args.candidate_id < 1) or
            args.database.strip().lower() in {"rag", "rag_qabug"}):
        parser.error("Valid candidate ID and an explicit engineering specification database required")
    if args.list and (args.apply or not 1 <= args.limit <= 200 or args.reason or args.expected_status or args.source_sha256 or args.target_sha256):
        parser.error("--list is read-only; --limit must be 1..200")
    if args.project.upper() in {"QABUG", "RAG_QABUG"}:
        parser.error("QA/BUG database does not own engineering relation decisions")
    if args.apply and not all((args.status, args.expected_status, args.reason, args.source_sha256, args.target_sha256)):
        parser.error("--apply requires --status, --expected-status, --reason and both preview SHA-256 hashes")
    if not args.apply and (args.reason or args.expected_status or args.source_sha256 or args.target_sha256):
        parser.error("Preview is read-only; mutation options require --apply")
    if args.user.lower() in {"postgres", "root"} and args.apply:
        parser.error("--apply requires a distinct reviewer login, never a superuser")

    import psycopg

    password = os.environ.get(args.password_env) or getpass.getpass("PostgreSQL password: ")
    with psycopg.connect(
        host=args.host, port=args.port, dbname=args.database, user=args.user,
        password=password, connect_timeout=10, application_name="dic-stage2-content-review",
    ) as conn:
        with conn.cursor() as cur:
            if not args.apply:
                cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            cur.execute("SELECT project_code FROM public.project_info ORDER BY project_code LIMIT 2")
            identities = cur.fetchall()
            if len(identities) != 1 or str(identities[0][0]).upper() != args.project.upper():
                raise RuntimeError("Database project identity mismatch; review refused")
            if args.list:
                for item in list_relation_queue(cur, project=args.project,
                                                status=args.status or "REVIEW_REQUIRED", limit=args.limit):
                    print(f"ID={item['candidate_id']} {item['status']} "
                          f"{item['source_code']} {item['source_version']} §{item['source_section']} "
                          f"-> {item['target_code']} {item['target_version']} §{item['target_section']} "
                          f"current={item['current_both']} primary={item['primary_both']} "
                          f"audit={item['last_audit_status']}")
                conn.rollback()
                return 0
            case = load_review_case(
                cur, candidate_id=args.candidate_id, project=args.project,
            ) if not args.apply else review_content_relation_secured(
                cur, candidate_id=args.candidate_id, project=args.project,
                expected_status=args.expected_status, new_status=args.status,
                reason=args.reason, source_hash=args.source_sha256, target_hash=args.target_sha256,
            )
            print(f"Candidate {case.candidate_id} [{case.status}] {case.relation_type} score={case.score}")
            print(f"Last audited transition ID={case.last_audit_id or 0} (save BEFORE attempting any write)")
            for name, endpoint in (("SOURCE", case.source), ("TARGET", case.target)):
                print(f"\n{name} {endpoint.specification_code} {endpoint.version} | {endpoint.file_name}")
                print(f"document={endpoint.document_id} content={endpoint.content_id} section={endpoint.section_id} "
                      f"page={endpoint.page_number} offsets=[{endpoint.start_offset},{endpoint.end_offset}) "
                      f"sha256={endpoint.text_hash}")
                print(repr(endpoint.raw_text))  # repr escapes terminal control characters; never truncate evidence
            print("\nCandidate extraction rationale:", case.rationale)
        if not args.apply:
            conn.rollback()
    if args.apply:
        print(f"REVIEW COMMITTED: {case.status} -> {args.status}")
    else:
        print("DRY RUN. No changes were made. Supply --apply with expected state to review.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
