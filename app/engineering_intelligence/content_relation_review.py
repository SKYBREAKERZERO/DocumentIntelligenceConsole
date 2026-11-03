"""Stage 2 Content Relation Graph: two-ended, source-exact human review.

The caller owns the PostgreSQL transaction.  This module never commits, and
it refuses to approve stale/non-primary/cross-project or drifted evidence.
Legacy self-declared reviewer writes are disabled. All status transitions must
pass the database-authenticated SECURITY DEFINER function.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from app.storage.primary_document_policy import PrimarySpecificationDocumentPolicy

from .persistence import ensure_schema_ready, require_complete_backfill
from .requirements import normalize_text
from .relations import ReferenceAnchor, find_reference_anchor
from .stage2_db_preflight import SEMANTIC_GUARD_SHA256, REFERENCE_HELPER_SHA256


class ContentRelationReviewError(ValueError):
    """A content-level proposal is not eligible for human review."""


@dataclass(frozen=True, slots=True)
class EvidenceEndpoint:
    specification_code: str
    version: str
    document_id: str
    file_name: str
    unit_key: str
    content_id: int
    section_id: str
    page_number: int | None
    start_offset: int
    end_offset: int
    raw_text: str
    text_hash: str


@dataclass(frozen=True, slots=True)
class ContentRelationReviewCase:
    candidate_id: int
    relation_type: str
    status: str
    score: float
    rationale: str
    source: EvidenceEndpoint
    target: EvidenceEndpoint
    review_identity_provenance: str | None = None
    reference_anchor: ReferenceAnchor | None = None
    last_audit_id: int | None = None


# Both documents must actually be the immutable primary version document.
# Joining original contents also guards against drift between extracted offsets
# and the authoritative, persisted source chunk.
_REVIEW_QUERY = """
SELECT r.id, r.relation_type, r.status, r.score, r.rationale,
       su.version_id AS source_version_id, tu.version_id AS target_version_id,
       src.project_code AS source_project, tgt.project_code AS target_project,
       src.current_version_id AS source_current, tgt.current_version_id AS target_current,
       su.specification_id AS source_spec_id, tu.specification_id AS target_spec_id,
       sv.specification_id AS source_version_spec_id, tv.specification_id AS target_version_spec_id,
       sv.document_id AS source_version_doc, tv.document_id AS target_version_doc,
       src.spec_code AS source_code, tgt.spec_code AS target_code,
       sv.version_label AS source_version, tv.version_label AS target_version,
       su.document_id AS source_document, tu.document_id AS target_document,
       sd.file_name AS source_file_name, td.file_name AS target_file_name,
       sd.metadata AS source_metadata, td.metadata AS target_metadata,
       su.unit_key AS source_unit_key, tu.unit_key AS target_unit_key,
       su.content_id AS source_content_id, tu.content_id AS target_content_id,
       su.section_id AS source_section, tu.section_id AS target_section,
       su.page_number AS source_page, tu.page_number AS target_page,
       su.start_offset AS source_start, tu.start_offset AS target_start,
       su.end_offset AS source_end, tu.end_offset AS target_end,
       su.raw_text AS source_text, tu.raw_text AS target_text,
       su.text_hash AS source_hash, tu.text_hash AS target_hash,
       sc.content AS source_chunk, tc.content AS target_chunk,
       sc.document_id AS source_chunk_doc, tc.document_id AS target_chunk_doc,
       audit.id AS last_audit_id, audit.new_status AS last_audit_status,
       audit.identity_provenance AS last_audit_provenance
FROM public.ei_content_relations r
JOIN public.ei_requirement_units su ON su.id = r.source_unit_id
JOIN public.ei_requirement_units tu ON tu.id = r.target_unit_id
JOIN public.specifications src ON src.id = su.specification_id
JOIN public.specifications tgt ON tgt.id = tu.specification_id
JOIN public.specification_versions sv ON sv.id = su.version_id
JOIN public.specification_versions tv ON tv.id = tu.version_id
JOIN public.documents sd ON sd.document_id = su.document_id
JOIN public.documents td ON td.document_id = tu.document_id
JOIN public.contents sc ON sc.id = su.content_id
JOIN public.contents tc ON tc.id = tu.content_id
LEFT JOIN LATERAL (
    SELECT a.id, a.new_status, to_jsonb(a)->>'identity_provenance' AS identity_provenance
    FROM public.ei_relation_review_audit a
    WHERE a.candidate_id = r.id ORDER BY a.id DESC LIMIT 1
) audit ON TRUE
WHERE r.id = %s
"""


def _read_dict(cur: Any) -> dict[str, Any] | None:
    row = cur.fetchone()
    if row is None:
        return None
    if isinstance(row, dict):
        return row
    # Psycopg defaults to tuple rows unless dict_row is explicitly enabled.
    return dict(zip((col.name if hasattr(col, "name") else col[0] for col in cur.description), row))


def _endpoint(row: dict[str, Any], prefix: str) -> EvidenceEndpoint:
    start, end = int(row[f"{prefix}_start"]), int(row[f"{prefix}_end"])
    text = str(row[f"{prefix}_text"])
    chunk = str(row[f"{prefix}_chunk"])
    digest = str(row[f"{prefix}_hash"]).strip()
    if not (0 <= start < end <= len(chunk)) or chunk[start:end] != text:
        raise ContentRelationReviewError(f"{prefix}: source offsets or original content drifted")
    if hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest() != digest:
        raise ContentRelationReviewError(f"{prefix}: source evidence hash mismatch")
    if str(row[f"{prefix}_chunk_doc"]) != str(row[f"{prefix}_document"]):
        raise ContentRelationReviewError(f"{prefix}: content/document ownership mismatch")
    if str(row[f"{prefix}_version_doc"]) != str(row[f"{prefix}_document"]):
        raise ContentRelationReviewError(f"{prefix}: unit does not belong to version's primary source")
    if int(row[f"{prefix}_version_spec_id"]) != int(row[f"{prefix}_spec_id"]):
        raise ContentRelationReviewError(f"{prefix}: version/specification identity mismatch")
    if not PrimarySpecificationDocumentPolicy.is_primary(
        file_name=str(row[f"{prefix}_file_name"] or ""),
        metadata=row[f"{prefix}_metadata"] or {},
    ):
        raise ContentRelationReviewError(f"{prefix}: cover/supporting document is not engineering evidence")
    return EvidenceEndpoint(
        specification_code=str(row[f"{prefix}_code"] or ""),
        version=str(row[f"{prefix}_version"] or ""),
        document_id=str(row[f"{prefix}_document"]),
        file_name=str(row[f"{prefix}_file_name"] or ""),
        unit_key=str(row[f"{prefix}_unit_key"]).strip(),
        content_id=int(row[f"{prefix}_content_id"]),
        section_id=str(row[f"{prefix}_section"] or ""),
        page_number=row[f"{prefix}_page"],
        start_offset=start,
        end_offset=end,
        raw_text=text,
        text_hash=digest,
    )


def load_review_case(
    cur: Any, *, candidate_id: int, project: str, for_update: bool = False,
) -> ContentRelationReviewCase:
    """Lock, verify and return full verbatim evidence for one relation.

    The database transaction must remain open until any review write commits.
    """
    if candidate_id <= 0 or not project or project.upper() == "QABUG":
        raise ContentRelationReviewError("Explicit positive candidate ID and engineering project required")
    ensure_schema_ready(cur)
    cur.execute(_REVIEW_QUERY + (" FOR UPDATE OF r, src, tgt" if for_update else ""), (candidate_id,))
    row = _read_dict(cur)
    if row is None:
        raise ContentRelationReviewError("Relation candidate not found")
    if row["source_project"] != project or row["target_project"] != project:
        raise ContentRelationReviewError("Cross-project relation review is forbidden")
    if (int(row["source_version_id"]) != int(row["source_current"] or 0) or
            int(row["target_version_id"]) != int(row["target_current"] or 0)):
        raise ContentRelationReviewError("Historical/non-current endpoint needs a new review candidate")
    if row["status"] == "STALE":
        raise ContentRelationReviewError("STALE candidate cannot be approved")
    if row["status"] not in {"REVIEW_REQUIRED", "CONFIRMED", "REJECTED"}:
        raise ContentRelationReviewError("Unknown relation status")
    if row["status"] != "REVIEW_REQUIRED" and row["last_audit_status"] != row["status"]:
        raise ContentRelationReviewError("Reviewed status lacks a matching audit event")
    if row["status"] == "REVIEW_REQUIRED" and row["last_audit_status"] not in (None, "REVIEW_REQUIRED"):
        raise ContentRelationReviewError("Review state/audit head mismatch")
    source = _endpoint(row, "source")
    target = _endpoint(row, "target")
    if source.unit_key == target.unit_key or not source.raw_text or not target.raw_text:
        raise ContentRelationReviewError("Invalid two-ended evidence")
    anchor = None
    if row["relation_type"] == "REFERENCES_SECTION":
        anchor = find_reference_anchor(source.raw_text, target.specification_code, target.section_id)
        if anchor is None:
            raise ContentRelationReviewError(
                "Explicit source SPEC/section anchor does not identify the actual target requirement"
            )
    for version_id in {int(row["source_version_id"]), int(row["target_version_id"])}:
        require_complete_backfill(cur, version_id)
    return ContentRelationReviewCase(
        candidate_id=int(row["id"]), relation_type=str(row["relation_type"]),
        status=str(row["status"]), score=float(row["score"]),
        rationale=str(row["rationale"]), source=source, target=target,
        review_identity_provenance=row.get("last_audit_provenance"),
        reference_anchor=anchor,
        last_audit_id=(int(row["last_audit_id"]) if row.get("last_audit_id") is not None else None),
    )


def review_content_relation(
    cur: Any, *, candidate_id: int, project: str, expected_status: str,
    new_status: str, actor: str, reason: str,
) -> ContentRelationReviewCase:
    """Permanently block the legacy client-side DML/self-declared actor path.

    Kept as a compatibility symbol so outdated call sites fail clearly instead
    of silently bypassing database-authenticated audit provenance. Use
    ``review_content_relation_secured`` with a dedicated reviewer login.
    """
    raise ContentRelationReviewError(
        "Legacy direct review writes are disabled; use database-authenticated secured review"
    )


def list_relation_queue(
    cur: Any, *, project: str, status: str = "REVIEW_REQUIRED", limit: int = 50,
) -> tuple[dict[str, Any], ...]:
    """Read bounded source/target identities; never assume a queue row is approvable.

    Preview each ID through load_review_case before any approval.  Rejected,
    historical and non-primary items remain visible for operator diagnostics.
    """
    if not project or project.upper() == "QABUG" or status not in {
        "REVIEW_REQUIRED", "CONFIRMED", "REJECTED", "STALE",
    } or not 1 <= limit <= 200:
        raise ContentRelationReviewError("Invalid queue scope, state or limit")
    ensure_schema_ready(cur)
    primary_source = PrimarySpecificationDocumentPolicy.sql_primary_predicate("sd")
    primary_target = PrimarySpecificationDocumentPolicy.sql_primary_predicate("td")
    cur.execute(
        f"""
        SELECT r.id AS candidate_id, r.status, r.relation_type, r.score,
               src.spec_code AS source_code, sv.version_label AS source_version,
               su.section_id AS source_section, su.page_number AS source_page,
               tgt.spec_code AS target_code, tv.version_label AS target_version,
               tu.section_id AS target_section, tu.page_number AS target_page,
               (su.version_id = src.current_version_id AND
                tu.version_id = tgt.current_version_id) AS current_both,
               ({primary_source} AND {primary_target}) AS primary_both,
               a.new_status AS last_audit_status
        FROM public.ei_content_relations r
        JOIN public.ei_requirement_units su ON su.id=r.source_unit_id
        JOIN public.ei_requirement_units tu ON tu.id=r.target_unit_id
        JOIN public.specifications src ON src.id=su.specification_id
        JOIN public.specifications tgt ON tgt.id=tu.specification_id
        JOIN public.specification_versions sv ON sv.id=su.version_id
        JOIN public.specification_versions tv ON tv.id=tu.version_id
        JOIN public.documents sd ON sd.document_id=su.document_id
        JOIN public.documents td ON td.document_id=tu.document_id
        LEFT JOIN LATERAL (
            SELECT au.new_status FROM public.ei_relation_review_audit au
            WHERE au.candidate_id=r.id ORDER BY au.id DESC LIMIT 1
        ) a ON TRUE
        WHERE src.project_code=%s AND tgt.project_code=%s AND r.status=%s
        ORDER BY r.id LIMIT %s
        """,
        (project, project, status, limit),
    )
    columns = tuple(col.name if hasattr(col, "name") else col[0] for col in cur.description)
    return tuple(row if isinstance(row, dict) else dict(zip(columns, row)) for row in cur.fetchall())


def _require_v137_semantic_db_gate(cur: Any) -> None:
    """Do not allow a v1.37 app to approve against an un-upgraded v1.33 DB.

    The catalog check repeats inside the write transaction, before invoking
    the authenticated definer function. Direct SQL callers are independently
    protected by the v1.37 trigger *once installed*.
    """
    cur.execute("""
        SELECT
          COALESCE((SELECT encode(sha256(convert_to(p.prosrc,'UTF8')),'hex')=%s
                    AND NOT p.prosecdef AND 'search_path=pg_catalog'=ANY(p.proconfig)
                    FROM pg_catalog.pg_proc p
                    WHERE p.oid=to_regprocedure('dic_review.guard_relation_write()')),false)
          AND
          COALESCE((SELECT encode(sha256(convert_to(p.prosrc,'UTF8')),'hex')=%s
                    AND NOT p.prosecdef AND p.provolatile='i'
                    AND 'search_path=pg_catalog'=ANY(p.proconfig)
                    FROM pg_catalog.pg_proc p
                    WHERE p.oid=to_regprocedure('dic_review.has_unambiguous_section_reference(text,text,text)')),false)
    """, (SEMANTIC_GUARD_SHA256, REFERENCE_HELPER_SHA256))
    answer = cur.fetchone()
    checked = (next(iter(answer.values())) if isinstance(answer, dict)
               else answer[0] if answer else None)
    if checked is not True:
        raise ContentRelationReviewError(
            "PostgreSQL v1.37 semantic review guard not installed/verified; approval refused"
        )


def review_content_relation_secured(
    cur: Any, *, candidate_id: int, project: str, expected_status: str,
    new_status: str, reason: str, source_hash: str, target_hash: str,
) -> ContentRelationReviewCase:
    """Apply via the v1.33 PostgreSQL SECURITY DEFINER gate, never client DML.

    Source/target hashes must come from an independently inspected preview.
    PostgreSQL binds the actual LOGIN as reviewer and appends audit atomically.
    The caller must commit/rollback the transaction; this function never does.
    """
    reason = reason.strip()
    if not 12 <= len(reason) <= 2000:
        raise ContentRelationReviewError("A substantive review reason (12..2000 chars) is required")
    allowed = {
        "REVIEW_REQUIRED": {"CONFIRMED", "REJECTED"},
        "CONFIRMED": {"REVIEW_REQUIRED"},
        "REJECTED": {"REVIEW_REQUIRED"},
    }
    if new_status not in allowed.get(expected_status, set()):
        raise ContentRelationReviewError("Invalid or duplicate review transition")
    if not (len(source_hash) == len(target_hash) == 64
            and all(ch in "0123456789abcdef" for ch in source_hash + target_hash)):
        raise ContentRelationReviewError("Two explicit source/target SHA-256 preconditions required")
    # Reviewer logins have SELECT only. The SECURITY DEFINER function acquires
    # all source/version/relationship row locks and repeats the proof gates.
    # Never issue client-side SELECT FOR UPDATE (requires table UPDATE grants).
    case = load_review_case(cur, candidate_id=candidate_id, project=project, for_update=False)
    if case.status != expected_status or case.source.text_hash != source_hash or case.target.text_hash != target_hash:
        raise ContentRelationReviewError("Candidate state or source evidence changed since preview")
    _require_v137_semantic_db_gate(cur)
    cur.execute(
        "SELECT dic_review.apply_content_relation_review(%s,%s,%s,%s,%s,%s,%s)",
        (candidate_id, project, expected_status, new_status, reason, source_hash, target_hash),
    )
    response = cur.fetchone()
    returned_id = response.get("apply_content_relation_review") if isinstance(response, dict) else (response[0] if response else None)
    if returned_id is None or int(returned_id) != candidate_id:
        raise ContentRelationReviewError("Controlled review did not return the expected candidate")
    return case
