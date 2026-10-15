"""Stage1 v1.26: conservative PDF/JSON/PostgreSQL/requirement crosswalk.

This module combines *freshly computed* review results. It is not a verifier
of arbitrary report files, database authentication, OCR or semantic rules.
A source candidate is eligible only if all four literal anchors are unique.
"""
from __future__ import annotations

from collections import Counter
from typing import Any

CHAIN_SCHEMA = "stage1-native-sql-chain-v1"
_NATIVE_OK = frozenset(("UNIQUE_NATIVE_LINE_CANDIDATE", "UNIQUE_NATIVE_MULTILINE_CANDIDATE"))


def crosscheck_native_sql(
    native_audit: dict[str, Any], sql_trace: dict[str, Any], *,
    max_evidence: int = 500,
) -> dict[str, Any]:
    """Join native/source and SQL snapshots on stable pipeline index.

    Inputs must be produced from the *same fresh documents* by their respective
    v1.24 and v1.25 builders; flags from untrusted JSON alone are not proof.
    No private source text, filename or raw document ID is emitted.
    """
    if type(max_evidence) is not int or not 1 <= max_evidence <= 10_000:
        raise ValueError("Invalid crosswalk evidence budget")
    if (native_audit.get("schema") != "stage1-native-pipeline-fidelity-v1"
            or sql_trace.get("schema") != "stage1-dcm-sql-trace-v1"
            or native_audit.get("document_hash") != sql_trace.get("document_hash")
            or not native_audit.get("pipeline_json_review_replayed")
            or not sql_trace.get("pipeline_json_evidence_rechecked")
            or not isinstance(native_audit.get("native_file_sha256"), str)
            or len(native_audit["native_file_sha256"]) != 64):
        raise ValueError("Native and SQL evidence identities or provenance disagree")

    native_rows = native_audit.get("entries")
    sql_rows = sql_trace.get("entries")
    if not isinstance(native_rows, list) or not isinstance(sql_rows, list):
        raise ValueError("Native/SQL evidence queues missing")
    if len(sql_rows) > max_evidence or len(native_rows) > 2_000:
        raise ValueError("Crosswalk evidence budget exceeded")
    # No reconstruction from a partially sampled native report. All source
    # indices have to be represented exactly once; otherwise absent means
    # "not audited", not "native evidence missing".
    native_index: dict[int, dict[str, Any]] = {}
    for row in native_rows:
        ix = row.get("content_index") if isinstance(row, dict) else None
        if (type(ix) is not int or ix < 1 or ix in native_index
                or not isinstance(row.get("text_alignment_status"), str)
                or not isinstance(row.get("section_tree_status"), str)):
            raise ValueError("Duplicate or malformed native content index")
        native_index[ix] = row
    if (len(native_rows) != native_audit.get("contents_sampled", -1) - native_audit.get("entries_omitted", -1)
            or len(native_rows) + native_audit.get("entries_omitted", -1) > native_audit.get("pipeline_content_count", -1)):
        raise ValueError("Native report count mismatch")
    if any(i not in native_index for i in range(1, len(native_rows) + 1)):
        raise ValueError("Native report sampled indices inconsistent")

    seen_keys: set[str] = set()
    records: list[dict[str, Any]] = []
    for row in sql_rows:
        if not isinstance(row, dict):
            raise ValueError("Malformed SQL review entry")
        key, index = row.get("review_key"), row.get("content_index")
        if (not isinstance(key, str) or len(key) != 64 or key in seen_keys
                or type(index) is not int or index < 1):
            raise ValueError("Invalid or duplicate DCM review key")
        seen_keys.add(key)
        native = native_index.get(index)
        native_status = native["text_alignment_status"] if native is not None else "NATIVE_CONTENT_NOT_SAMPLED"
        section_status = native["section_tree_status"] if native is not None else "NOT_CHECKED"
        sql_status = row.get("source_status")
        unit_status = row.get("unit_status")
        if not isinstance(sql_status, str) or not isinstance(unit_status, str):
            raise ValueError("Missing SQL source or unit state")
        # Strict priority prevents a native+SQL source match from masking a
        # truncated review queue or missing source section.
        if native is None:
            chain = "NATIVE_CONTENT_NOT_SAMPLED"
        elif section_status != "SECTION_ID_IN_EXPORTED_TREE":
            chain = "EXPORTED_SECTION_NOT_RESOLVED"
        elif native_status not in _NATIVE_OK:
            chain = "NATIVE_ANCHOR_NOT_UNIQUE"
        elif sql_status != "EXACT_SQL_CONTENT_ONLY":
            chain = "SQL_CONTENT_NOT_EXACT_UNIQUE"
        elif unit_status != "EXACT_SQL_UNIT_REVIEW_ONLY":
            chain = "SQL_REQUIREMENT_SPAN_NOT_EXACT"
        else:
            chain = "FOUR_LAYER_LITERAL_CANDIDATE_REVIEW_ONLY"
        # Keep original SQL content ID only if all four layers are eligible;
        # no partial source binding is ever presented as an end-to-end result.
        exact = chain == "FOUR_LAYER_LITERAL_CANDIDATE_REVIEW_ONLY"
        records.append({
            "review_key": key,
            "content_index": index,
            "native_alignment_status": native_status,
            "exported_section_status": section_status,
            "sql_content_status": sql_status,
            "sql_unit_status": unit_status,
            "chain_status": chain,
            "sql_content_id": row.get("sql_content_id") if exact else None,
            "requirement_unit_key": row.get("requirement_unit_key") if exact else None,
            "native_locator": native.get("native_locator") if exact else None,
            "requirement_semantics_verified": False,
            "dcm_state_transition_approved": False,
        })
    completeness = (
        native_audit.get("contents_not_sampled") == 0
        and native_audit.get("entries_omitted") == 0
        and native_audit.get("pipeline_content_count") == len(native_index)
        and native_audit.get("crosspage_candidate_queue_complete") is True
        and native_audit.get("pipeline_evidence_queue_complete") is True
        and sql_trace.get("dcm_evidence_complete") is True
        and sql_trace.get("dcm_evidence_count") == len(records)
    )
    status_counts = Counter(row["chain_status"] for row in records)
    return {
        "schema": CHAIN_SCHEMA,
        "status": "FOUR_LAYER_SOURCE_REVIEW_ONLY",
        "document_hash": native_audit["document_hash"],
        "native_file_sha256": native_audit["native_file_sha256"],
        "project": sql_trace["project"],
        "version_id": sql_trace["version_id"],
        "pipeline_content_count": native_audit["pipeline_content_count"],
        "evidence_count": len(records),
        "literal_candidates": status_counts.get("FOUR_LAYER_LITERAL_CANDIDATE_REVIEW_ONLY", 0),
        "blocked_count": len(records) - status_counts.get("FOUR_LAYER_LITERAL_CANDIDATE_REVIEW_ONLY", 0),
        "chain_status_counts": dict(sorted(status_counts.items())),
        "source_review_queue_complete": completeness,
        "all_four_layer_literal_candidates": (bool(records) and completeness and
            status_counts.get("FOUR_LAYER_LITERAL_CANDIDATE_REVIEW_ONLY", 0) == len(records)),
        "native_pdf_has_no_text_layer": native_audit.get("native_pdf_has_no_text_layer"),
        "crosspage_continuity_candidates_total": native_audit.get("crosspage_continuity_candidates_total"),
        "entries": records,
        "live_sql_transaction_verified_by_cli": False,
        "native_pdf_ownership_hash_checked_by_cli": False,
        "physical_table_header_verified": False,
        "bilingual_equivalence_verified": False,
        "condition_action_scope_verified": False,
        "human_gold_acceptance_complete": False,
        "semantic_relationships_approved": False,
        "database_modified": False,
        "qdrant_modified": False,
    }
