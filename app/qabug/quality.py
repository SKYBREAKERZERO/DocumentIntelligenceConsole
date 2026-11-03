from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .qdrant_sync import QABUG_COLLECTION, QABUG_PAYLOAD_INDEX_CONTRACT


@dataclass(frozen=True, slots=True)
class QABugQualityReport:
    status: str
    sql_records: int
    sql_chunks: int
    qdrant_points: int
    missing_points: int
    orphan_points: int
    stale_points: int
    missing_payload_indexes: tuple[str, ...]
    mismatched_payload_indexes: tuple[str, ...]
    warnings: tuple[str, ...] = ()
    quarantined_records: int = 0
    source_integrity_failures: int = 0
    snapshot_stable: bool = True


class QABugDataQualityValidator:
    """Cross-store validator for the dedicated QA/BUG SQL/Qdrant domain."""

    def __init__(self, *, postgres_profile, password: str, qdrant_service, collection_name: str = QABUG_COLLECTION, cancel_check=None, progress=None) -> None:
        self.postgres = postgres_profile
        self.password = password
        self.qdrant = qdrant_service
        self.collection_name = collection_name
        self.cancel_check = cancel_check
        self.progress = progress

    def validate(self) -> QABugQualityReport:
        from app.qabug.streaming_quality import validate
        return validate(self)
