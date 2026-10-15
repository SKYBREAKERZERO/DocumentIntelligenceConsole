"""Explicit read-only DCM JSON -> version-owned PostgreSQL evidence crosswalk.

Requires the operator's *original native binary* as a source-hash gate. This
cannot validate the semantic meaning of the DCM text or native PDF geometry.
Output contains only hashes, bounded positions, statuses and numeric SQL IDs.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.engineering_intelligence.filtered_json_diagnostics import read_limited_json
from app.engineering_intelligence.persistence import load_version_units, require_complete_backfill
from app.engineering_intelligence.pipeline_json_review import read_pipeline_files
from app.engineering_intelligence.stage1_dcm_sql_trace import SqlTraceContent, trace_dcm_sql_sources
from app.engineering_intelligence.stage1_v3 import verify_live_source_snapshot


def native_hash(path: Path, *, max_file_bytes: int = 350_000_000) -> str:
    if (path.is_symlink() or not path.is_file() or
            path.suffix.lower() not in (".pdf", ".docx") or
            path.stat().st_size > max_file_bytes):
        raise ValueError("Invalid native PDF/DOCX input")
    digest = hashlib.sha256()
    with path.open("rb") as src:
        for block in iter(lambda: src.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_only_dcm_trace(
    cur, *, document_id: str, project: str, version_id: int,
    file_sha256: str, report: dict, documents: list,
    max_contents: int = 20_000, max_units: int = 80_000,
    max_chars: int = 30_000_000,
) -> dict:
    """All reads are expected inside caller's REPEATABLE READ READ ONLY txn."""
    cur.execute("SELECT project_code FROM public.project_info LIMIT 1")
    project_row = cur.fetchone()
    if not project_row or str(project_row[0]).upper() != project:
        raise ValueError("Project/database identity mismatch")
    cur.execute("""SELECT sv.document_id,d.source_hash
                   FROM public.specification_versions sv
                   JOIN public.specifications sp ON sp.id=sv.specification_id
                   JOIN public.documents d ON d.document_id=sv.document_id
                   WHERE sv.id=%s AND sp.project_code=%s""", (version_id, project))
    owned = cur.fetchone()
    if (owned is None or str(owned[0]) != document_id or
            not isinstance(owned[1], str) or
            owned[1].strip().lower() != file_sha256):
        raise ValueError("Version document or native source hash mismatch")
    require_complete_backfill(cur, version_id)
    verify_live_source_snapshot(cur, version_id=version_id, max_rows=max_contents)
    cur.execute("""SELECT COUNT(*), COALESCE(SUM(length(content)),0)
                   FROM public.contents WHERE document_id=%s""", (document_id,))
    count, size = cur.fetchone()
    if int(count) > max_contents or int(size) > max_chars:
        raise ValueError("SQL content snapshot budget exceeded")
    cur.execute("""SELECT id,section_id,page_number,content FROM public.contents
                   WHERE document_id=%s ORDER BY id LIMIT %s""",
                (document_id, max_contents + 1))
    rows = cur.fetchall()
    if len(rows) != int(count) or len(rows) > max_contents:
        raise ValueError("Source snapshot size changed or incomplete")
    sql_contents = [SqlTraceContent(int(cid), document_id, str(sid),
                      int(page) if page is not None else None, str(body or ""))
                    for cid, sid, page, body in rows]
    sql_units = load_version_units(cur, version_id, max_units=max_units + 1)
    result = trace_dcm_sql_sources(
        report, documents, project=project, document_id=document_id,
        version_id=version_id, sql_contents=sql_contents, sql_units=sql_units,
        max_rows=max_contents, max_units=max_units, max_chars=max_chars,
    )
    # Asserted by this explicitly invoked, transaction-bound PostgreSQL read,
    # not by unauthenticated JSON snapshots or the language model.
    result["sql_rows_authenticated_by_caller"] = True
    result["native_binary_matches_version_hash"] = True
    result["source_backfill_receipt_rechecked"] = True
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only DCM source -> SQL evidence candidates")
    parser.add_argument("--json", type=Path, action="append", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--file", type=Path, required=True, help="Original native PDF/DOCX; hashed locally")
    parser.add_argument("--project", choices=("21MM", "24MM"), required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--document-id", required=True, help="Exact version-owned SQL document ID")
    parser.add_argument("--version-id", type=int, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5432)
    parser.add_argument("--user", required=True, help="Use a least-privilege read-only DB user")
    parser.add_argument("--password-env", default="POSTGRES_PASSWORD")
    parser.add_argument("--max-contents", type=int, default=20_000)
    parser.add_argument("--max-units", type=int, default=80_000)
    parser.add_argument("--max-source-chars", type=int, default=30_000_000)
    parser.add_argument("--report-out", type=Path, required=True)
    args = parser.parse_args(argv)
    if (args.database.lower() != "rag_" + args.project.lower()
            or args.version_id < 1 or not 1 <= args.port <= 65535
            or not all(x > 0 for x in
                       (args.max_contents, args.max_units, args.max_source_chars))):
        parser.error("Invalid project-scoped SQL connection or budgets")
    paths = [*args.json, args.report, args.file]
    if any(args.report_out.resolve() == p.resolve() for p in paths):
        parser.error("Refusing to overwrite an input")
    try:
        documents = read_pipeline_files(args.json)
        report = read_limited_json(args.report, max_input_bytes=12_000_000)
        file_sha256 = native_hash(args.file)
        import psycopg
        secret = os.environ.get(args.password_env) or getpass.getpass("PostgreSQL password: ")
        with psycopg.connect(host=args.host, port=args.port, dbname=args.database,
                             user=args.user, password=secret, connect_timeout=10,
                             application_name="dic-stage1-dcm-trace-readonly") as conn:
            with conn.cursor() as cur:
                cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                result = read_only_dcm_trace(
                    cur, document_id=args.document_id, project=args.project,
                    version_id=args.version_id, file_sha256=file_sha256,
                    report=report, documents=documents,
                    max_contents=args.max_contents, max_units=args.max_units,
                    max_chars=args.max_source_chars)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(args.report_out, flags, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as writer:
                json.dump(result, writer, ensure_ascii=False, indent=2)
                writer.write("\n")
        except BaseException:
            args.report_out.unlink(missing_ok=True)
            raise
    except Exception as exc:  # DB driver errors must not dump credentials or source rows
        print(json.dumps({"status": "NOT_VERIFIED", "error_type": type(exc).__name__}), file=sys.stderr)
        return 2
    print(json.dumps({"status": result["status"], "dcm_evidence_count": result["dcm_evidence_count"],
                      "dcm_evidence_complete": result["dcm_evidence_complete"],
                      "sql_source_attested": True, "semantic_relations_approved": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
