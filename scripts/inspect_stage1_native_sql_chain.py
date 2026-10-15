"""v1.26 one-invocation native PDF -> pipeline JSON -> SQL -> unit review.

Read-only PostgreSQL REPEATABLE READ snapshot, no migrations or AI calls.
Redacted source locator output; all semantic decisions require human review.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.engineering_intelligence.filtered_json_diagnostics import read_limited_json
from app.engineering_intelligence.native_layout import inspect_pdf_native
from app.engineering_intelligence.pipeline_json_review import read_pipeline_files
from app.engineering_intelligence.stage1_native_fidelity import reconcile_pdf_pipeline
from app.engineering_intelligence.stage1_native_sql_chain import crosscheck_native_sql
from scripts.inspect_stage1_dcm_sql_trace import native_hash, read_only_dcm_trace


def audit_live_source_chain(
    cur, *, documents: list[dict], review: dict, pdf: Path, project: str,
    document_id: str, version_id: int, document_index: int = 1,
    max_file_bytes: int = 100_000_000, max_contents: int = 2_000,
    max_evidence: int = 500, include_table_geometry: bool = False,
) -> dict:
    """Full chain from freshly extracted native data and a caller's RO cursor.

    NOTE: transaction configuration and authenticated DB connection are caller
    responsibilities. This function by itself does not assert live provenance.
    """
    if (pdf.is_symlink() or pdf.suffix.lower() != ".pdf"
            or type(document_index) is not int or not 1 <= document_index <= len(documents)
            or not all(type(v) is int and v > 0 for v in
                       (version_id, max_file_bytes, max_contents, max_evidence))):
        raise ValueError("Invalid native/SQL scope or resource budgets")
    selected = documents[document_index - 1]
    if (selected["document"].get("document_id") != document_id
            or (selected["document"].get("project_code") or selected["metadata"].get("project_code")) != project):
        raise ValueError("Selected pipeline document does not match SQL identity")
    native = inspect_pdf_native(pdf, max_file_bytes=max_file_bytes,
                                include_table_geometry=include_table_geometry)
    # Recheck source bytes after PDF extraction: the text layer must belong
    # to the same binary that will be checked against the DB version hash.
    if native_hash(pdf, max_file_bytes=max_file_bytes) != native.file_sha256:
        raise ValueError("Native PDF changed during text extraction")
    native_audit = reconcile_pdf_pipeline(
        documents, review, native, document_index=document_index,
        max_contents=max_contents, max_entries=max_contents,
        max_lines=100_000,
    )
    # read_only_dcm_trace also gates project identity, source version ownership,
    # authoritative binary hash, backfill receipt, source digests and units.
    sql_trace = read_only_dcm_trace(
        cur, document_id=document_id, project=project, version_id=version_id,
        file_sha256=native.file_sha256, report=review, documents=documents,
    )
    result = crosscheck_native_sql(native_audit, sql_trace, max_evidence=max_evidence)
    # Runtime attestation is set only in main(), after a live PostgreSQL
    # transaction completes successfully and has a clean scope/commit exit.
    return result


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Read-only v1.26 PDF/JSON/PostgreSQL/DCM evidence crosswalk")
    p.add_argument("--pdf", type=Path, required=True, help="Original version-owned source PDF")
    p.add_argument("--json", type=Path, required=True, action="append")
    p.add_argument("--review", type=Path, required=True)
    p.add_argument("--document-index", type=int, default=1)
    p.add_argument("--project", choices=("21MM", "24MM"), required=True)
    p.add_argument("--database", required=True)
    p.add_argument("--document-id", required=True)
    p.add_argument("--version-id", type=int, required=True)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5432)
    p.add_argument("--user", required=True, help="Use a SELECT-only database user")
    p.add_argument("--password-env", default="POSTGRES_PASSWORD")
    p.add_argument("--max-file-bytes", type=int, default=100_000_000)
    p.add_argument("--max-contents", type=int, default=2_000)
    p.add_argument("--max-evidence", type=int, default=500)
    p.add_argument("--pdf-table-geometry", action="store_true")
    p.add_argument("--report-out", type=Path, required=True)
    try:
        a = p.parse_args(argv)
        paths = [a.pdf, a.review, *a.json]
        if (a.database.lower() != "rag_" + a.project.lower()
                or not 1 <= a.port <= 65535 or a.version_id < 1
                or any(getattr(a, key) < 1 for key in
                       ("max_file_bytes", "max_contents", "max_evidence"))
                or a.document_index < 1 or not a.document_id.strip()
                or any(path.is_symlink() for path in paths)
                or any(a.report_out.resolve() == path.resolve() for path in paths)):
            raise ValueError("Invalid scoped inputs or output destination")
        # Avoid expensive PDF/DB reads if a report destination already exists.
        if a.report_out.exists() or a.report_out.is_symlink():
            raise FileExistsError("Output already exists")
        documents = read_pipeline_files(a.json)
        review = read_limited_json(a.review, max_input_bytes=12_000_000)
        import psycopg  # lazy import; offline modules can run without a DB driver
        password = os.environ.get(a.password_env) or getpass.getpass("PostgreSQL password: ")
        with psycopg.connect(host=a.host, port=a.port, dbname=a.database,
                             user=a.user, password=password, connect_timeout=10,
                             application_name="dic-stage1-native-sql-readonly") as conn:
            with conn.cursor() as cur:
                cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                result = audit_live_source_chain(
                    cur, documents=documents, review=review, pdf=a.pdf,
                    project=a.project, document_id=a.document_id,
                    version_id=a.version_id, document_index=a.document_index,
                    max_file_bytes=a.max_file_bytes, max_contents=a.max_contents,
                    max_evidence=a.max_evidence,
                    include_table_geometry=a.pdf_table_geometry,
                )
        # A second check covers replacement during the SQL transaction.
        if native_hash(a.pdf, max_file_bytes=a.max_file_bytes) != result["native_file_sha256"]:
            raise ValueError("Native PDF changed during SQL review")
        result["live_sql_transaction_verified_by_cli"] = True
        result["native_pdf_ownership_hash_checked_by_cli"] = True
        # Create exclusively and never touch old reports.
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(a.report_out, flags, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                json.dump(result, out, ensure_ascii=False, indent=2)
                out.write("\n")
        except BaseException:
            a.report_out.unlink(missing_ok=True)
            raise
    except Exception as exc:
        print(json.dumps({"status": "NOT_VERIFIED", "error_type": type(exc).__name__}), file=sys.stderr)
        return 2
    print(json.dumps({"status": result["status"],
                      "evidence_count": result["evidence_count"],
                      "literal_candidates": result["literal_candidates"],
                      "blocked_count": result["blocked_count"],
                      "source_review_queue_complete": result["source_review_queue_complete"],
                      "semantic_relationships_approved": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
