from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from psycopg.rows import dict_row

from app.enterprise.profiles import PostgresProfile, QdrantProfile
from app.enterprise.qdrant_service import QdrantService
from app.qabug.qdrant_sync import (
    QABUG_COLLECTION,
    QABUG_PAYLOAD_INDEX_CONTRACT,
    QABugQdrantManager,
)
from app.rag.qdrant_sync import (
    QdrantSyncCancelled,
    QdrantSyncPlan,
    QdrantSyncResult,
)


class QABugCurrentQdrantSyncService:
    """Adapter exposing the dedicated QA/BUG index through the shared sync UI.

    The specification sync path is based on ``public.rag_current_chunks`` and
    ``documents/chapters/sections/contents``. QA/BUG deliberately uses a
    different authoritative schema (``qabug_records`` / ``qabug_chunks``), so
    selecting QA/BUG must never fall through to the specification sync service.
    """

    project_code = "QABUG"

    def __init__(
        self,
        *,
        postgres: PostgresProfile,
        postgres_password: str,
        qdrant: QdrantProfile,
        qdrant_api_key: str = "",
        collection_name: str = QABUG_COLLECTION,
        embedding_model: str = "BAAI/bge-m3",
        embedding_version: str = "current-index-v1",
        vector_dimension: int = 1024,
        batch_size: int = 16,
    ) -> None:
        self.postgres = postgres
        self.postgres_password = str(postgres_password or "")
        self.qdrant_profile = qdrant
        self.qdrant_api_key = str(qdrant_api_key or "")
        self.collection_name = str(collection_name or QABUG_COLLECTION).strip() or QABUG_COLLECTION
        self.embedding_model = str(embedding_model or "BAAI/bge-m3").strip()
        self.embedding_version = str(embedding_version or "current-index-v1").strip()
        self.vector_dimension = int(vector_dimension)
        self.batch_size = max(1, int(batch_size))
        self.qdrant = QdrantService(qdrant, self.qdrant_api_key)

    @staticmethod
    def default_collection_name(_project_code: str = "QABUG") -> str:
        return QABUG_COLLECTION

    def _connect(self):
        import psycopg

        kwargs = self.postgres.dsn_kwargs()
        kwargs["password"] = self.postgres_password
        return psycopg.connect(**kwargs, row_factory=dict_row)

    def _ensure_run_table(self) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS public.qabug_qdrant_sync_runs (
                    id BIGSERIAL PRIMARY KEY,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    collection_name TEXT NOT NULL,
                    source_count BIGINT NOT NULL DEFAULT 0,
                    qdrant_before_count BIGINT NOT NULL DEFAULT 0,
                    new_count BIGINT NOT NULL DEFAULT 0,
                    changed_count BIGINT NOT NULL DEFAULT 0,
                    unchanged_count BIGINT NOT NULL DEFAULT 0,
                    stale_count BIGINT NOT NULL DEFAULT 0,
                    upserted_count BIGINT NOT NULL DEFAULT 0,
                    deleted_count BIGINT NOT NULL DEFAULT 0,
                    qdrant_after_count BIGINT,
                    embedding_model TEXT NOT NULL,
                    embedding_version TEXT NOT NULL,
                    vector_dimension INTEGER NOT NULL,
                    error_message TEXT,
                    started_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    finished_at TIMESTAMPTZ
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_qabug_qdrant_sync_runs_started
                ON public.qabug_qdrant_sync_runs(collection_name, started_at DESC)
                """
            )
            conn.commit()

    def ensure_payload_index_contract(self, *, repair_mismatched: bool = False):
        manager = QABugQdrantManager(
            qdrant_service=self.qdrant,
            collection_name=self.collection_name,
            vector_dimension=self.vector_dimension,
        )
        return manager.ensure_contract(repair_mismatched=repair_mismatched)

    def _load_sql_fingerprints(self) -> list[dict[str, Any]]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.id AS content_id, c.qdrant_point_id, c.content_hash, c.index_hash,
                       c.embedding_status, c.embedding_model, c.embedding_version
                FROM public.qabug_chunks c
                JOIN public.qabug_records r ON r.id=c.record_id
                WHERE c.is_current AND r.is_current AND r.is_queryable
                  AND LENGTH(BTRIM(c.content)) > 0
                ORDER BY c.id
                """
            )
            return [dict(row) for row in cur.fetchall()]

    def preview(
        self,
        *,
        progress: Callable[[dict[str, Any]], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> QdrantSyncPlan:
        def emit(stage: str, percent: float, message: str) -> None:
            if progress:
                progress({"stage": stage, "percent": percent, "message": message})

        if cancel_check and cancel_check():
            raise QdrantSyncCancelled("QA/BUG sync preview cancelled.")

        emit("qabug_plan_sql", 15, "Scanning QA/BUG current SQL chunks…")
        sql_rows = self._load_sql_fingerprints()
        if cancel_check and cancel_check():
            raise QdrantSyncCancelled("QA/BUG sync preview cancelled.")

        emit("qabug_plan_qdrant", 45, "Scanning QA/BUG Qdrant points…")
        q_rows = (
            self.qdrant.scroll_all_points(self.collection_name)
            if self.qdrant.collection_exists(self.collection_name)
            else []
        )
        q_by_point = {int(row["id"]): dict(row.get("payload") or {}) for row in q_rows}

        new_ids: list[int] = []
        changed_ids: list[int] = []
        unchanged_ids: list[int] = []
        eligible_point_ids: set[int] = set()
        for row in sql_rows:
            if cancel_check and cancel_check():
                raise QdrantSyncCancelled("QA/BUG sync preview cancelled.")
            content_id = int(row["content_id"])
            point_id = int(row["qdrant_point_id"])
            eligible_point_ids.add(point_id)
            payload = q_by_point.get(point_id)
            if payload is None:
                new_ids.append(content_id)
                continue
            q_hash = str(payload.get("content_hash") or "")
            q_index_hash = str(payload.get("index_hash") or "")
            q_model = str(payload.get("embedding_model") or "")
            q_version = str(payload.get("embedding_version") or "")
            if (
                q_hash != str(row.get("content_hash") or "")
                or q_index_hash != str(row.get("index_hash") or "")
                or q_model != self.embedding_model
                or q_version != self.embedding_version
            ):
                changed_ids.append(content_id)
            else:
                unchanged_ids.append(content_id)

        stale_ids = sorted(set(q_by_point) - eligible_point_ids)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM public.qabug_embedding_state")
            state_count = int(cur.fetchone()["n"])

        emit("qabug_plan_done", 100, "QA/BUG SQL ↔ Qdrant preview ready.")
        return QdrantSyncPlan(
            project_code="QABUG",
            database_name=self.postgres.database,
            collection_name=self.collection_name,
            source_count=len(sql_rows),
            qdrant_count=len(q_rows),
            state_count=state_count,
            new_ids=tuple(new_ids),
            changed_ids=tuple(changed_ids),
            unchanged_ids=tuple(unchanged_ids),
            stale_ids=tuple(stale_ids),
            queued_delete_ids=(),
            drift_delta=len(q_rows) - len(sql_rows),
            warnings=(),
        )

    def _reset_for_full_rebuild(self) -> None:
        if self.qdrant.collection_exists(self.collection_name):
            self.qdrant.delete_collection(self.collection_name)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE public.qabug_chunks c
                SET embedding_status='PENDING', embedded_content_hash=NULL,
                    embedded_at=NULL, qdrant_collection=%s, updated_at=CURRENT_TIMESTAMP
                FROM public.qabug_records r
                WHERE r.id=c.record_id AND c.is_current AND r.is_current AND r.is_queryable
                """,
                (self.collection_name,),
            )
            cur.execute("DELETE FROM public.qabug_embedding_state")
            conn.commit()

    def sync(
        self,
        *,
        full_rebuild: bool,
        progress: Callable[[dict[str, Any]], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> QdrantSyncResult:
        started = time.perf_counter()
        self._ensure_run_table()
        plan = self.preview(progress=progress, cancel_check=cancel_check)
        mode = "FULL_REBUILD" if full_rebuild else "INCREMENTAL"

        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.qabug_qdrant_sync_runs(
                    mode,status,collection_name,source_count,qdrant_before_count,
                    new_count,changed_count,unchanged_count,stale_count,
                    embedding_model,embedding_version,vector_dimension
                ) VALUES(%s,'RUNNING',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id
                """,
                (
                    mode, self.collection_name, plan.source_count, plan.qdrant_count,
                    plan.new_count, plan.changed_count, plan.unchanged_count, plan.stale_count,
                    self.embedding_model, self.embedding_version, self.vector_dimension,
                ),
            )
            run_id = int(cur.fetchone()["id"])
            conn.commit()

        try:
            if cancel_check and cancel_check():
                raise QdrantSyncCancelled("QA/BUG sync cancelled before embedding.")
            if full_rebuild:
                if progress:
                    progress({"stage": "qabug_rebuild", "percent": 8, "message": "Resetting QA/BUG derived vector index…"})
                self._reset_for_full_rebuild()

            manager = QABugQdrantManager(
                qdrant_service=self.qdrant,
                collection_name=self.collection_name,
                vector_dimension=self.vector_dimension,
            )

            def manager_progress(done: int, total: int, key: str) -> None:
                if cancel_check and cancel_check():
                    # The current embedding batch is allowed to finish safely;
                    # cancellation is checked again immediately afterwards.
                    return
                if progress:
                    percent = 10 + (85 * done / max(1, total))
                    progress({
                        "stage": "qabug_embed",
                        "percent": percent,
                        "message": f"QA/BUG embedding {done}/{total} · {key}",
                    })

            report = manager.sync_from_postgres(
                postgres_profile=self.postgres,
                password=self.postgres_password,
                embedding_model=self.embedding_model,
                embedding_version=self.embedding_version,
                batch_size=self.batch_size,
                progress=manager_progress,
                prune_stale=True,
            )
            if cancel_check and cancel_check():
                raise QdrantSyncCancelled("QA/BUG sync cancelled after current batch.")

            final_count = 0
            if self.qdrant.collection_exists(self.collection_name):
                for summary in self.qdrant.list_collections():
                    if summary.name == self.collection_name:
                        final_count = int(summary.points_count or 0)
                        break

            elapsed = time.perf_counter() - started
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE public.qabug_qdrant_sync_runs
                    SET status='READY', upserted_count=%s, deleted_count=%s,
                        qdrant_after_count=%s, finished_at=CURRENT_TIMESTAMP
                    WHERE id=%s
                    """,
                    (report.points_upserted, report.stale_points_deleted, final_count, run_id),
                )
                conn.commit()
            if progress:
                progress({"stage": "qabug_done", "percent": 100, "message": "QA/BUG SQL → Qdrant sync complete."})
            return QdrantSyncResult(
                run_id=run_id,
                mode=mode,
                status="READY",
                plan=plan,
                upserted_count=report.points_upserted,
                deleted_count=report.stale_points_deleted,
                failed_count=0,
                final_qdrant_count=final_count,
                elapsed_seconds=elapsed,
                warnings=report.warnings,
            )
        except Exception as exc:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE public.qabug_qdrant_sync_runs
                    SET status=%s, error_message=%s, finished_at=CURRENT_TIMESTAMP
                    WHERE id=%s
                    """,
                    ("CANCELLED" if isinstance(exc, QdrantSyncCancelled) else "FAILED", str(exc)[:4000], run_id),
                )
                conn.commit()
            raise

    def list_runs(self, *, limit: int = 50) -> list[dict[str, Any]]:
        self._ensure_run_table()
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, mode, status, 'QABUG'::TEXT AS project_code,
                       %s::TEXT AS database_name, collection_name, source_count,
                       new_count, changed_count, unchanged_count, stale_count,
                       upserted_count, deleted_count, qdrant_after_count,
                       started_at, finished_at, error_message
                FROM public.qabug_qdrant_sync_runs
                WHERE collection_name=%s
                ORDER BY id DESC LIMIT %s
                """,
                (self.postgres.database, self.collection_name, max(1, min(int(limit), 500))),
            )
            return [dict(row) for row in cur.fetchall()]
