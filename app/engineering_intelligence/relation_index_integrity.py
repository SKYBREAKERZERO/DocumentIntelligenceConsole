"""Read-only Stage 2 v1.41 index-coverage and source-integrity audit.

An index that contains ten valid rows is not necessarily a complete index.
Verify the immutable source digest including content blocks yielding zero units,
backfill receipt, every indexed source span, and count before reporting PASS.
All reads must run inside the caller's REPEATABLE READ READ ONLY snapshot.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

from app.engineering_intelligence.deterministic_read_models import verify_requirement_span
from app.engineering_intelligence.persistence import require_complete_backfill
from app.storage.primary_document_policy import PrimarySpecificationDocumentPolicy


def _fingerprint_add(digest: Any, content_id: Any, section: Any, page: Any, text: Any) -> None:
    for part in (str(content_id), str(section), str(page), str(text or "")):
        data = part.encode("utf-8", "surrogatepass")
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)


def inspect_relation_index(cur: Any, *, project: str, spec_code: str,
                           max_chunks: int = 10000, max_units: int = 50000) -> dict[str, Any]:
    """Return BLOCKED unless the entire chosen SPEC_BODY extraction is intact.

    This is an audit snapshot, not an authorization to auto-approve a relation,
    nor a promise that no missing semantic edges exist. Strict bounds avoid
    presenting a truncated sample as complete coverage.
    """
    if (project.upper() in {"QABUG", "RAG_QABUG"}
            or not re.fullmatch(r"[A-Za-z0-9_-]{2,40}", project)
            or not re.fullmatch(r"\d{2,6}", spec_code)
            or not 1 <= max_chunks <= 100000 or not 1 <= max_units <= 100000):
        raise ValueError("Invalid project/code or resource bounds")
    issues: list[str] = []
    report: dict[str, Any] = {
        "schema": "dic-relation-index-integrity-v1", "project": project,
        "spec_code": spec_code, "status": "BLOCKED", "issues": issues,
        "production_acceptance": "NOT_VERIFIED", "read_only": True,
    }
    body_ver = PrimarySpecificationDocumentPolicy.sql_preferred_body_version_id("sp")
    cur.execute(f"""
        SELECT sp.id, sv.id, sv.document_id, sv.version_label,
               d.file_name, d.metadata
        FROM public.specifications sp
        JOIN public.specification_versions sv ON sv.id={body_ver}
        JOIN public.documents d ON d.document_id=sv.document_id
        WHERE sp.project_code=%s AND sp.spec_code=%s
        ORDER BY sp.id LIMIT 2
    """, (project, spec_code))
    selections = cur.fetchall()
    if len(selections) != 1:
        issues.append("BODY_VERSION_MISSING_OR_AMBIGUOUS")
        return report
    spec_id, version_id, document_id, version_label, filename, metadata = selections[0]
    if not PrimarySpecificationDocumentPolicy.is_primary(
            file_name=str(filename), metadata=metadata or {}):
        issues.append("NON_PRIMARY_DOCUMENT")
        return report
    report.update({"specification_id": int(spec_id), "version_id": int(version_id),
                   "document_id": str(document_id), "version_label": str(version_label)})
    try:
        require_complete_backfill(cur, int(version_id))
    except (ValueError, RuntimeError) as exc:
        issues.append("INCOMPLETE_BACKFILL_RECEIPT:" + type(exc).__name__)
        return report
    cur.execute("""SELECT source_chunk_count,source_unit_count,max_content_id,source_digest
                   FROM public.ei_version_backfill_receipts WHERE version_id=%s""", (version_id,))
    receipt = cur.fetchone()
    if not receipt:
        issues.append("MISSING_SOURCE_RECEIPT")
        return report
    expected_chunks, expected_units, expected_max, expected_digest = receipt
    if int(expected_chunks) > max_chunks or int(expected_units) > max_units:
        issues.append("RESOURCE_LIMIT_EXCEEDED")
        return report
    digest = hashlib.sha256()
    last_content = chunks = 0
    while True:
        cur.execute("""SELECT id,section_id,page_number,content FROM public.contents
                       WHERE document_id=%s AND id>%s ORDER BY id LIMIT 256""",
                    (document_id, last_content))
        rows = cur.fetchall()
        if not rows:
            break
        if chunks + len(rows) > max_chunks:
            issues.append("CHUNK_SCAN_TRUNCATED")
            return report
        for content_id, section, page, text in rows:
            _fingerprint_add(digest, content_id, section, page, text)
            last_content = int(content_id)
            chunks += 1
    if (chunks != int(expected_chunks) or last_content != int(expected_max)
            or digest.hexdigest() != str(expected_digest).strip()):
        issues.append("SOURCE_DIGEST_OR_CHUNK_MISMATCH")
        return report

    last_unit = indexed = rejected = 0
    while True:
        cur.execute("""SELECT u.id,u.document_id,u.version_id,u.content_id,
                              u.start_offset,u.end_offset,u.raw_text,u.text_hash,
                              u.review_status,c.content
                       FROM public.ei_requirement_units u
                       JOIN public.contents c ON c.id=u.content_id AND c.document_id=u.document_id
                       WHERE u.version_id=%s AND u.id>%s
                       ORDER BY u.id LIMIT 256""", (version_id, last_unit))
        rows = cur.fetchall()
        if not rows:
            break
        if indexed + len(rows) > max_units:
            issues.append("UNIT_SCAN_TRUNCATED")
            return report
        for (unit_id, unit_doc, unit_version, content_id, start, end,
             raw, text_hash, status, chunk) in rows:
            last_unit = int(unit_id)
            indexed += 1
            rejected += status == "REJECTED"
            if (str(unit_doc) != str(document_id) or int(unit_version) != int(version_id)
                    or not verify_requirement_span({
                        "source_start": start, "source_end": end,
                        "source_text": raw, "source_hash": text_hash,
                        "source_chunk": chunk,
                    }, "source")):
                issues.append("UNIT_SOURCE_SPAN_OR_HASH_MISMATCH")
                report["first_invalid_unit_id"] = last_unit
                return report
    report.update({"source_chunks": chunks, "indexed_units": indexed,
                   "rejected_units": rejected, "source_digest": digest.hexdigest()})
    if indexed != int(expected_units):
        issues.append("UNIT_COUNT_MISMATCH")
        return report
    report["status"] = "PASS"
    report["production_acceptance"] = "DATABASE_SNAPSHOT_ONLY"
    return report
