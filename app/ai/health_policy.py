from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


SEVERITY_OK = "OK"
SEVERITY_WARNING = "WARNING"
SEVERITY_ERROR = "ERROR"

# These conditions normally mean the derived vector index is behind PostgreSQL.
# They are recoverable by the incremental SQL -> Qdrant sync and should not make
# a healthy RAG stack look like a service outage.
SYNC_REPAIRABLE_ISSUE_CODES: frozenset[str] = frozenset(
    {
        "QDRANT_POINTS_MISSING",
        "QDRANT_ORPHAN_POINTS",
        "CONTENT_HASH_DRIFT",
        "EMBEDDING_STALE",
        "EMBEDDING_STATE_MISSING",
        "EMBEDDING_STATE_CONTENT_HASH_DRIFT",
        "INDEX_HASH_DRIFT",
        "EMBEDDING_STATE_NOT_CURRENT",
        "PAYLOAD_EMBEDDING_CONTRACT_MISMATCH",
        "PAYLOAD_METADATA_DRIFT",
        "PAYLOAD_SOURCE_VIEW_DRIFT",
        "PAYLOAD_INDEX_MISSING",
        "COLLECTION_NOT_GREEN",
        "QDRANT_SCROLL_COUNT_MISMATCH",
        "VERSION_MARKER_CONFLICT_SUSPECTED",
        "CLASSIFICATION_FIELDS_EMPTY",
        "ISSUE_KEY_MISSING",
        "ISSUE_STATUS_MISSING",
    }
)

# Contract failures can make retrieval unsafe or semantically incorrect. Keep
# these as hard errors even when the underlying services are connected.
CRITICAL_ISSUE_CODES: frozenset[str] = frozenset(
    {
        "VECTOR_DIMENSION_MISMATCH",
        "DISTANCE_MISMATCH",
        "PAYLOAD_INDEX_TYPE_MISMATCH",
        "POINT_ID_INVALID",
        "POINT_ID_MISMATCH",
        "PAYLOAD_REQUIRED_FIELD_MISSING",
        "PAYLOAD_TYPE_MISMATCH",
        "DUPLICATE_CHUNK_KEY",
        "WRONG_SCOPE",
        "NON_CURRENT_POINT",
        "EMBEDDING_STATE_POINT_MISMATCH",
        "EMBEDDING_STATE_DIMENSION_MISMATCH",
    }
)


@dataclass(frozen=True, slots=True)
class RuntimeHealthDecision:
    severity: str
    message: str


def is_valid_empty_scope(*, sql_count: int | None, collection_exists: bool) -> bool:
    """An absent Qdrant collection is valid when the authoritative SQL scope is empty."""

    return sql_count == 0 and not collection_exists


def classify_quality_issue(*, code: str, reported_severity: str) -> str:
    normalized_code = str(code or "").strip().upper()
    normalized_severity = str(reported_severity or "").strip().upper()
    if normalized_code in CRITICAL_ISSUE_CODES:
        return SEVERITY_ERROR
    if normalized_code in SYNC_REPAIRABLE_ISSUE_CODES:
        return SEVERITY_WARNING
    # Unknown validator errors stay conservative. Unknown warnings remain warnings.
    return SEVERITY_ERROR if normalized_severity == "ERROR" else SEVERITY_WARNING


def classify_runtime(
    *,
    dependencies_ok: bool,
    critical_errors: Iterable[str] = (),
    sync_warnings: Iterable[str] = (),
) -> RuntimeHealthDecision:
    errors = tuple(item for item in critical_errors if str(item).strip())
    warnings = tuple(item for item in sync_warnings if str(item).strip())
    if not dependencies_ok:
        return RuntimeHealthDecision(SEVERITY_ERROR, "Dependency failure · inspect Ollama/PostgreSQL/Qdrant")
    if errors:
        return RuntimeHealthDecision(SEVERITY_ERROR, "Data/vector contract failure · repair required")
    if warnings:
        return RuntimeHealthDecision(SEVERITY_WARNING, "Index sync required · retrieval remains available")
    return RuntimeHealthDecision(SEVERITY_OK, "Ready")


def qabug_chunk_fanout(*, records: int, chunks: int) -> float:
    if records <= 0:
        return 0.0
    return float(chunks) / float(records)
