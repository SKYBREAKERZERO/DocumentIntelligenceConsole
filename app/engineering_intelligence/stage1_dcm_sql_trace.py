"""Stage 1 v1.24 DCM: exact Pipeline JSON -> version-scoped SQL source review.

This creates *candidates* only. It neither certifies the native PDF layout
nor interprets phase changes as verified engineering transitions. A caller must
independently verify the database version, source binary hash and source
backfill receipt before describing the SQL snapshot as database-verified.
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Iterable

from .atomic import extract_atomic_candidates
from .condition_scope import extract_requirement_scope
from .pipeline_json_review import review_pipeline_documents
from .requirements import RequirementUnit, normalize_text
from .stage1_review_evidence import verify_review_pointers

TRACE_SCHEMA = "stage1-dcm-sql-trace-v1"
_DCM_CATEGORIES = frozenset(("DCM_SOURCE", "DCM_RELATED_UI_SOURCE"))


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()


@dataclass(frozen=True, slots=True)
class SqlTraceContent:
    content_id: int
    document_id: str
    section_id: str
    page_number: int | None
    text: str


def trace_dcm_sql_sources(
    report: dict[str, Any], documents: list[dict[str, Any]], *,
    project: str, document_id: str, version_id: int,
    sql_contents: Iterable[SqlTraceContent], sql_units: Iterable[RequirementUnit] = (),
    max_rows: int = 20_000, max_units: int = 80_000,
    max_chars: int = 30_000_000, max_pointers: int = 500,
) -> dict[str, Any]:
    """Fail closed on input drift; report exact, non-semantic SQL joins.

    A verified SQL snapshot can be supplied by the read-only PostgreSQL CLI.
    Passing arbitrary in-memory rows alone is NOT proof of actual DB contents.
    """
    if (project not in ("21MM", "24MM") or not isinstance(document_id, str)
            or not document_id or type(version_id) is not int or version_id < 1
            or not all(type(n) is int and n > 0 for n in
                       (max_rows, max_units, max_chars, max_pointers))):
        raise ValueError("Invalid DCM SQL trace scope or budgets")
    if report != review_pipeline_documents(documents):
        raise ValueError("Pipeline diagnostics drift; regenerate original report")
    verify_review_pointers(report, documents)
    eligible = [item for item in documents if
                item["document"].get("document_id") == document_id
                and (item["document"].get("project_code") or
                     item["metadata"].get("project_code")) == project]
    if len(eligible) != 1 or eligible[0]["metadata"].get("document_role") == "SUPPORTING":
        raise ValueError("No unique primary pipeline document for SQL version")
    selected = eligible[0]
    doc_hash = _digest(project + "\0" + document_id)[:20]
    pointers = [p for p in report["evidence_queue"]
                if p.get("document_hash") == doc_hash and p.get("category") in _DCM_CATEGORIES]
    if len(pointers) > max_pointers:
        raise ValueError("DCM evidence budget exceeded")
    raw_rows = tuple(sql_contents)
    if len(raw_rows) > max_rows or sum(len(r.text) for r in raw_rows) > max_chars:
        raise ValueError("SQL snapshot budget exceeded")
    if (len({r.content_id for r in raw_rows}) != len(raw_rows)
            or any(type(r.content_id) is not int or r.content_id < 1 or
                   r.document_id != document_id or not isinstance(r.section_id, str) or
                   (r.page_number is not None and
                    (type(r.page_number) is not int or r.page_number < 1)) or
                   not isinstance(r.text, str) for r in raw_rows)):
        raise ValueError("Invalid, duplicate or foreign SQL content")
    rows_by_id = {r.content_id: r for r in raw_rows}
    sql_units = tuple(sql_units)
    if (len(sql_units) > max_units or len({u.unit_key for u in sql_units}) != len(sql_units)
            or any(not isinstance(u.unit_key, str) or not re.fullmatch(r"[a-f0-9]{64}", u.unit_key) for u in sql_units)):
        raise ValueError("SQL requirement unit budget or identity conflict")
    units_by_id: dict[int, list[RequirementUnit]] = defaultdict(list)
    for unit in sql_units:
        row = rows_by_id.get(unit.content_id)
        if (row is None or unit.document_id != document_id
                or unit.section_id != row.section_id or unit.page_number != row.page_number
                or not 0 <= unit.start_offset < unit.end_offset <= len(row.text)
                or row.text[unit.start_offset:unit.end_offset] != unit.text
                or hashlib.sha256(normalize_text(unit.text).encode("utf-8")).hexdigest() != unit.text_hash):
            raise ValueError("SQL requirement source span or fingerprint drift")
        units_by_id[unit.content_id].append(unit)

    # Index exact text. Hash is just a lookup accelerator, NEVER proof of match.
    by_text: dict[str, list[SqlTraceContent]] = defaultdict(list)
    for row in raw_rows:
        by_text[_digest(row.text)].append(row)
    source_positions: dict[tuple[str, int | None, str], int] = Counter(
        (c["section_id"] if isinstance(c.get("section_id"), str) else "",
         c.get("page_number") if type(c.get("page_number")) is int else None,
         c["text"])
        for c in selected["contents"]
    )
    entries: list[dict[str, Any]] = []
    for pointer in pointers:
        source = selected["contents"][pointer["content_index"] - 1]
        body = source["text"]
        page = source.get("page_number")
        page = page if type(page) is int else None
        section = source.get("section_id") if isinstance(source.get("section_id"), str) else ""
        candidates = [r for r in by_text.get(_digest(body), ()) if r.text == body]
        compatible = [r for r in candidates if r.page_number == page and r.section_id == section]
        # Never fill an uncertain provenance hole through fuzzy matching.
        if source_positions[(section, page, body)] != 1:
            state = "AMBIGUOUS_PIPELINE_SOURCE"
        elif not candidates:
            state = "SQL_CONTENT_NOT_FOUND"
        elif not any(r.page_number == page for r in candidates):
            state = "PAGE_MISMATCH"
        elif not any(r.section_id == section for r in candidates):
            state = "SECTION_MISMATCH"
        elif len(compatible) != 1:
            state = "AMBIGUOUS_SQL_CONTENT"
        else:
            state = "EXACT_SQL_CONTENT_ONLY"
        entry: dict[str, Any] = {
            "review_key": pointer["review_key"],
            "pointer_category": pointer["category"],
            "content_index": pointer["content_index"],
            "source_status": state,
            "sql_content_id": None,
            "requirement_unit_key": None,
            "unit_status": "NOT_BOUND",
            "atomic_candidate_count": 0,
            "atomic_review_required_count": 0,
            "scope_status_counts": {},
            "condition_action_scope_verified": False,
            "state_transition_verified": False,
        }
        if state == "EXACT_SQL_CONTENT_ONLY":
            row = compatible[0]
            entry["sql_content_id"] = row.content_id
            exact = [u for u in units_by_id.get(row.content_id, ()) if
                     u.start_offset == pointer["start_offset"] and
                     u.end_offset == pointer["end_offset"]]
            if len(exact) > 1:
                entry["unit_status"] = "AMBIGUOUS_REQUIREMENT_UNIT"
            elif len(exact) == 1:
                unit = exact[0]
                entry["requirement_unit_key"] = unit.unit_key
                entry["unit_status"] = "EXACT_SQL_UNIT_REVIEW_ONLY"
                atoms = extract_atomic_candidates(unit)
                entry["atomic_candidate_count"] = len(atoms)
                entry["atomic_review_required_count"] = sum(
                    atom.review_status == "REVIEW_REQUIRED" for atom in atoms)
                entry["scope_status_counts"] = dict(sorted(Counter(
                    extract_requirement_scope(unit, atom).status for atom in atoms
                ).items()))
            elif units_by_id.get(row.content_id):
                entry["unit_status"] = "SQL_UNIT_OFFSETS_NOT_EXACT"
            else:
                entry["unit_status"] = "NO_SQL_REQUIREMENT_UNIT"
        entries.append(entry)
    status_counts = Counter(e["source_status"] for e in entries)
    unit_counts = Counter(e["unit_status"] for e in entries)
    doc_record = next(r for r in report["documents"] if r["document_hash"] == doc_hash)
    complete = (report.get("evidence_queue_truncated") == 0
                and doc_record["dcm_probe_complete"]
                and len(entries) == (doc_record["dcm_lines_sampled"] +
                                     doc_record["dcm_related_ui_lines_sampled"]))
    return {
        "schema": TRACE_SCHEMA, "status": "SQL_SOURCE_CANDIDATES_REVIEW_ONLY",
        "document_hash": doc_hash, "project": project, "version_id": version_id,
        "dcm_evidence_count": len(entries), "dcm_evidence_complete": complete,
        "sql_content_status_counts": dict(sorted(status_counts.items())),
        "sql_requirement_status_counts": dict(sorted(unit_counts.items())),
        "entries": entries,
        "pipeline_json_evidence_rechecked": True,
        "sql_rows_authenticated_by_caller": False,
        "native_pdf_layout_verified": False, "semantic_relations_approved": False,
        "database_modified": False, "qdrant_modified": False,
    }
