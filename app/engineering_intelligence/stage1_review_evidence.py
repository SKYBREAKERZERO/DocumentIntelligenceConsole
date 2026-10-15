"""Source-exact, redacted pointers into an *exported* pipeline JSON record.

These records are not proof of the native PDF, an approved requirement, or an
engineering relationship. All offsets are Python Unicode code-point offsets
inside the exact `contents[index-1]["text"]` string. No source text, file name,
or raw arbitrary section identifier is serialized.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

POINTER_SCHEMA = "stage1-pipeline-source-pointer-v1"
_NUMERIC_SECTION = re.compile(r"[0-9]{1,3}(?:\.[0-9]{1,3}){0,6}\Z")


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()


def safe_section_id(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    # Non-number IDs can contain confidential titles, employee names and URLs.
    # Preserve only bounded engineering chapter numbers; all else is opaque.
    return value if _NUMERIC_SECTION.fullmatch(value) else "opaque:" + _sha(value)[:20]


def safe_page(value: object) -> int | None:
    return value if type(value) is int and 1 <= value <= 100_000 else None


def make_pointer(
    document_hash: str, content: dict[str, Any], content_index: int,
    start: int, end: int, *, category: str, status: str,
) -> dict[str, Any]:
    if not isinstance(document_hash, str) or not re.fullmatch(r"[a-f0-9]{20}", document_hash):
        raise ValueError("Invalid pipeline document hash")
    value = content.get("text")
    if (not isinstance(value, str) or type(content_index) is not int or content_index < 1
            or type(start) is not int or type(end) is not int
            or not 0 <= start < end <= len(value)
            or not isinstance(category, str) or not re.fullmatch(r"[A-Z_]{3,60}", category)
            or not isinstance(status, str) or not re.fullmatch(r"[A-Z_]{3,60}", status)):
        raise ValueError("Invalid source pointer")
    content_sha = _sha(value)
    evidence_sha = _sha(value[start:end])
    source_key = "\0".join((POINTER_SCHEMA, document_hash, str(content_index),
                            str(start), str(end), category, status,
                            content_sha, evidence_sha))
    return {
        "schema": POINTER_SCHEMA,
        "review_key": _sha(source_key),
        "document_hash": document_hash,
        "content_index": content_index,
        "section_id": safe_section_id(content.get("section_id")),
        "page": safe_page(content.get("page_number")),
        "start_offset": start,
        "end_offset": end,
        "content_sha256": content_sha,
        "evidence_sha256": evidence_sha,
        "category": category,
        "status": status,
        "decision": "UNREVIEWED",
        "native_source_verified": False,
        "requirement_relationship_approved": False,
    }


def verify_review_pointers(report: dict[str, Any], documents: list[dict[str, Any]]) -> dict[str, Any]:
    """Fail closed on reordered/mutated pipeline contents or edited pointers.

    This proves only exact compatibility with the provided *pipeline JSON*;
    untrusted JSON cannot certify the source PDF/DOCX or database provenance.
    """
    if report.get("schema") != "stage1-pipeline-review-v1":
        raise ValueError("Not a Stage1 pipeline diagnostic report")
    if report.get("document_count") != len(documents):
        raise ValueError("Document count mismatch")
    by_hash: dict[str, dict[str, Any]] = {}
    for item in documents:
        document, metadata = item["document"], item["metadata"]
        project = document.get("project_code") or metadata.get("project_code")
        if project not in ("21MM", "24MM", "COMMON") or not isinstance(document.get("document_id"), str):
            raise ValueError("Invalid document identity")
        h = _sha(project + "\0" + document["document_id"])[:20]
        if h in by_hash:
            raise ValueError("Ambiguous document hash")
        by_hash[h] = item
    pointers = report.get("evidence_queue")
    if not isinstance(pointers, list):
        raise ValueError("Evidence queue missing")
    seen: set[str] = set()
    for pointer in pointers:
        if not isinstance(pointer, dict):
            raise ValueError("Malformed evidence pointer")
        item = by_hash.get(pointer.get("document_hash"))
        if item is None:
            raise ValueError("Unknown evidence document")
        position = pointer.get("content_index")
        contents = item.get("contents")
        if type(position) is not int or not isinstance(contents, list) or not 1 <= position <= len(contents):
            raise ValueError("Invalid content index")
        regenerated = make_pointer(
            pointer["document_hash"], contents[position-1], position,
            pointer.get("start_offset"), pointer.get("end_offset"),
            category=pointer.get("category"), status=pointer.get("status"),
        )
        if pointer != regenerated or pointer["review_key"] in seen:
            raise ValueError("Evidence pointer drift or duplicate")
        seen.add(pointer["review_key"])
    if report.get("evidence_queue_count") != len(pointers):
        raise ValueError("Evidence pointer count mismatch")
    return {
        "status": "PIPELINE_JSON_EVIDENCE_RECHECKED",
        "verified_pointer_count": len(pointers),
        "source_native_file_verified": False,
        "requirement_relationship_approved": False,
        "database_modified": False,
        "qdrant_modified": False,
    }
