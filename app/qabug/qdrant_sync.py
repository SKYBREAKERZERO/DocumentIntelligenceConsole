from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from app.enterprise.qdrant_service import QdrantService

QABUG_COLLECTION = "rag_qabug_current_chunks"
QABUG_PAYLOAD_INDEX_CONTRACT: dict[str, str] = {
    "record_type": "keyword",
    "record_key": "keyword",
    "issue_key": "keyword",
    "issue_id": "keyword",
    "project_key": "keyword",
    "issue_type": "keyword",
    "status": "keyword",
    "priority": "keyword",
    "resolution": "keyword",
    "component": "keyword",
    "assignee": "keyword",
    "reporter": "keyword",
    "release_version": "keyword",
    "affected_version": "keyword",
    "software_version": "keyword",
    "grade": "keyword",
    "destination": "keyword",
    "model": "keyword",
    "section_type": "keyword",
    "source_file": "keyword",
}


class QABugQdrantError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class QABugQdrantReport:
    collection: str
    vector_dimension: int
    indexed_fields: int
    points_upserted: int = 0
    points_skipped: int = 0
    stale_points_deleted: int = 0
    warnings: tuple[str, ...] = ()
    scanned_count: int = 0
    unchanged_count: int = 0
    source_changed_count: int = 0
    limited: bool = False
    snapshot_upper_id: int = 0

    @property
    def status(self):
        return "PARTIAL" if self.source_changed_count or self.limited else "READY"


class QABugQdrantManager:
    def __init__(
        self,
        *,
        qdrant_service: QdrantService,
        collection_name: str = QABUG_COLLECTION,
        vector_dimension: int = 1024,
    ) -> None:
        self.qdrant = qdrant_service
        self.collection_name = collection_name
        self.vector_dimension = int(vector_dimension)

    def ensure_contract(self, *, repair_mismatched: bool = False):
        self.qdrant.ensure_collection(self.collection_name, self.vector_dimension)
        return self.qdrant.ensure_payload_indexes(
            self.collection_name,
            QABUG_PAYLOAD_INDEX_CONTRACT,
            repair_mismatched=repair_mismatched,
        )

    def sync_from_postgres(
        self,
        *,
        postgres_profile,
        password: str,
        embedding_model: str = "BAAI/bge-m3",
        embedding_version: str = "current-index-v1",
        batch_size: int = 16,
        max_chunks: int | None = None,
        progress: Callable[[int, int, str], None] | None = None,
        prune_stale: bool = True,
        cancel_check: Callable[[], bool] | None = None,
        force_reembed: bool = False,
        checkpoint: Callable[[dict], None] | None = None,
    ) -> QABugQdrantReport:
        from app.qabug.streaming_sync import sync
        return sync(self,postgres_profile=postgres_profile,password=password,embedding_model=embedding_model,
            embedding_version=embedding_version,batch_size=batch_size,max_chunks=max_chunks,progress=progress,
            prune_stale=prune_stale,cancel_check=cancel_check,force_reembed=force_reembed,checkpoint=checkpoint)
