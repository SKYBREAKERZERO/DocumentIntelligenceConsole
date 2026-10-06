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
    ) -> QABugQdrantReport:
        try:
            import psycopg
            from psycopg.rows import dict_row
            from qdrant_client.models import PointStruct
        except ImportError as exc:
            raise QABugQdrantError("psycopg and qdrant-client are required for QA/BUG vector sync.") from exc

        ensured = self.ensure_contract()
        if ensured.mismatched:
            details = ", ".join(f"{x.field_name}:{x.actual_type}->{x.expected_type}" for x in ensured.mismatched)
            raise QABugQdrantError(f"QA/BUG Qdrant payload index type mismatch: {details}. Run explicit Repair Payload Indexes.")

        from app.embedding.embedding_client import EmbeddingClient, EmbeddingClientConfig

        kwargs = postgres_profile.dsn_kwargs()
        kwargs["password"] = password
        client = self.qdrant._client()
        embedder = EmbeddingClient(EmbeddingClientConfig(
            model_name=embedding_model,
            expected_dimension=self.vector_dimension,
            batch_size=max(1, int(batch_size)),
            normalize_embeddings=True,
            show_progress_bar=False,
            trust_remote_code=False,
        ))
        # Qdrant is derived state. Do not trust SQL embedding_status alone:
        # a collection can be deleted/recreated or lose points while SQL still
        # says COMPLETED. Compare authoritative SQL fingerprints with live
        # Qdrant payloads so incremental sync also repairs missing/stale points.
        existing_points = (
            self.qdrant.scroll_all_points(self.collection_name)
            if self.qdrant.collection_exists(self.collection_name)
            else []
        )
        qdrant_by_id = {int(point["id"]): dict(point.get("payload") or {}) for point in existing_points}

        with psycopg.connect(**kwargs, row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.id AS chunk_id, c.qdrant_point_id, c.chunk_key, c.section_type,
                       c.content, c.content_hash, c.index_hash, c.metadata,
                       c.embedding_status, c.embedded_content_hash, c.embedding_model, c.embedding_version,
                       r.record_type, r.record_key, r.issue_key, r.issue_id, r.project_key, r.issue_type,
                       r.status_norm, r.status_raw, r.priority_norm, r.priority_raw,
                       r.resolution_norm, r.component, r.assignee, r.reporter,
                       r.release_plan_version, r.affected_version, r.software_version, r.grade,
                       r.destination, r.model, r.source_row_number, sf.source_file_name
                FROM public.qabug_chunks c
                JOIN public.qabug_records r ON r.id=c.record_id
                LEFT JOIN public.qabug_source_files sf ON sf.id=r.source_file_id
                WHERE c.is_current AND r.is_current AND r.is_queryable
                  AND LENGTH(BTRIM(c.content)) > 0
                ORDER BY c.id
                """
            )
            all_rows = [dict(r) for r in cur.fetchall()]
            rows = []
            for row in all_rows:
                point_id = int(row["qdrant_point_id"])
                payload = qdrant_by_id.get(point_id)
                needs_sync = (
                    payload is None
                    or str(payload.get("content_hash") or "") != str(row.get("content_hash") or "")
                    or str(payload.get("index_hash") or "") != str(row.get("index_hash") or "")
                    or str(payload.get("embedding_model") or "") != str(embedding_model)
                    or str(payload.get("embedding_version") or "") != str(embedding_version)
                    or str(row.get("embedding_status") or "") != "COMPLETED"
                    or str(row.get("embedded_content_hash") or "") != str(row.get("content_hash") or "")
                    or str(row.get("embedding_model") or "") != str(embedding_model)
                    or str(row.get("embedding_version") or "") != str(embedding_version)
                )
                if needs_sync:
                    rows.append(row)
            if max_chunks:
                rows = rows[: int(max_chunks)]
            total = len(rows)
            upserted = 0
            skipped = 0
            for start in range(0, total, max(1, int(batch_size))):
                batch = rows[start:start + max(1, int(batch_size))]
                texts = [str(r.get("content") or "") for r in batch]
                valid = [(idx, text) for idx, text in enumerate(texts) if text.strip()]
                if not valid:
                    skipped += len(batch)
                    continue
                vectors = embedder.embed_texts([text for _, text in valid])
                points = []
                updated_rows = []
                for (idx, _text), vector in zip(valid, vectors):
                    row = batch[idx]
                    payload = {
                        "artifact_type": "QABUG_REGISTER",
                        "record_type": str(row.get("record_type") or ""),
                        "record_key": str(row.get("record_key") or ""),
                        "issue_key": str(row.get("issue_key") or ""),
                        "issue_id": str(row.get("issue_id") or ""),
                        "project_key": str(row.get("project_key") or ""),
                        "issue_type": str(row.get("issue_type") or ""),
                        "status": str(row.get("status_norm") or "UNSPECIFIED"),
                        "status_raw": str(row.get("status_raw") or ""),
                        "priority": str(row.get("priority_norm") or "UNSPECIFIED"),
                        "priority_raw": str(row.get("priority_raw") or ""),
                        "resolution": str(row.get("resolution_norm") or "UNSPECIFIED"),
                        "component": str(row.get("component") or ""),
                        "assignee": str(row.get("assignee") or ""),
                        "reporter": str(row.get("reporter") or ""),
                        "release_version": str(row.get("release_plan_version") or ""),
                        "affected_version": str(row.get("affected_version") or ""),
                        "software_version": str(row.get("software_version") or ""),
                        "grade": str(row.get("grade") or ""),
                        "destination": str(row.get("destination") or ""),
                        "model": str(row.get("model") or ""),
                        "section_type": str(row.get("section_type") or "CONTENT"),
                        "source_file": str(row.get("source_file_name") or ""),
                        "source_row": int(row.get("source_row_number") or 0),
                        "content_hash": str(row.get("content_hash") or ""),
                        "index_hash": str(row.get("index_hash") or ""),
                        "embedding_model": embedding_model,
                        "embedding_version": embedding_version,
                        "index_scope": "CURRENT",
                        "source_view": "public.qabug_current_chunks",
                        "content": str(row.get("content") or ""),
                        "content_id": int(row.get("chunk_id")),
                    }
                    points.append(PointStruct(id=int(row["qdrant_point_id"]), vector=vector, payload=payload))
                    updated_rows.append(row)
                client.upsert(collection_name=self.collection_name, points=points, wait=True)
                with conn.cursor() as ucur:
                    for row in updated_rows:
                        ucur.execute(
                            """
                            UPDATE public.qabug_chunks
                            SET embedding_status='COMPLETED', embedding_model=%s, embedding_version=%s,
                                embedded_content_hash=content_hash, embedded_at=CURRENT_TIMESTAMP,
                                qdrant_collection=%s, updated_at=CURRENT_TIMESTAMP
                            WHERE id=%s
                            """,
                            (embedding_model, embedding_version, self.collection_name, row["chunk_id"]),
                        )
                        ucur.execute(
                            """
                            INSERT INTO public.qabug_embedding_state(chunk_id,qdrant_collection,qdrant_point_id,content_hash,index_hash,
                                embedding_model,embedding_version,vector_dimension,synced_at)
                            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,CURRENT_TIMESTAMP)
                            ON CONFLICT(chunk_id) DO UPDATE SET
                                qdrant_collection=EXCLUDED.qdrant_collection,qdrant_point_id=EXCLUDED.qdrant_point_id,
                                content_hash=EXCLUDED.content_hash,index_hash=EXCLUDED.index_hash,
                                embedding_model=EXCLUDED.embedding_model,embedding_version=EXCLUDED.embedding_version,
                                vector_dimension=EXCLUDED.vector_dimension,synced_at=CURRENT_TIMESTAMP
                            """,
                            (row["chunk_id"], self.collection_name, row["qdrant_point_id"], row["content_hash"], row["index_hash"], embedding_model, embedding_version, self.vector_dimension),
                        )
                conn.commit()
                upserted += len(points)
                if progress:
                    progress(upserted, total, str(updated_rows[-1].get("issue_key") or ""))

        stale_deleted = 0
        if prune_stale:
            # Qdrant is derived state. Any point no longer represented by a
            # current+queryable SQL chunk (including quarantined source rows) is
            # deleted in bounded batches. SQL remains the authority.
            with psycopg.connect(**kwargs, row_factory=dict_row) as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT c.qdrant_point_id
                    FROM public.qabug_chunks c
                    JOIN public.qabug_records r ON r.id=c.record_id
                    WHERE c.is_current AND r.is_current AND r.is_queryable
                    """
                )
                eligible = {int(row["qdrant_point_id"]) for row in cur.fetchall()}
            existing = {int(point["id"]) for point in self.qdrant.scroll_all_points(self.collection_name)}
            stale_ids = sorted(existing - eligible)
            for start in range(0, len(stale_ids), 512):
                stale_deleted += self.qdrant.delete_points(self.collection_name, stale_ids[start:start + 512])

        return QABugQdrantReport(
            collection=self.collection_name,
            vector_dimension=self.vector_dimension,
            indexed_fields=len(QABUG_PAYLOAD_INDEX_CONTRACT),
            points_upserted=upserted,
            points_skipped=skipped,
            stale_points_deleted=stale_deleted,
        )
