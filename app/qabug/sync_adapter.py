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

    def _load_sql_fingerprints(self):
        """Yield keyset-paged fingerprints; retain no corpus text or global maps."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT coalesce(max(id),0) AS ceiling FROM public.qabug_chunks")
                ceiling=int(cur.fetchone()["ceiling"])
                last=0
                while last<ceiling:
                    cur.execute("""SELECT c.id AS content_id,c.qdrant_point_id,c.content_hash,c.index_hash,
                        c.embedding_status,c.embedded_content_hash,c.embedding_model,c.embedding_version
                        FROM public.qabug_chunks c JOIN public.qabug_records r ON r.id=c.record_id
                        WHERE c.is_current AND r.is_current AND r.is_queryable AND length(btrim(c.content))>0
                          AND c.id>%s AND c.id<=%s ORDER BY c.id LIMIT 512""",(last,ceiling))
                    page=[dict(row) for row in cur.fetchall()]
                    if not page:break
                    last=int(page[-1]["content_id"])
                    yield from page

    def preview(self,*,progress=None,cancel_check=None) -> QdrantSyncPlan:
        from itertools import islice
        from app.qabug.streaming_sync import needs_sync,ELIGIBLE
        def check():
            if cancel_check and cancel_check():raise QdrantSyncCancelled("QA/BUG preview cancelled")
        check()
        exists=self.qdrant.collection_exists(self.collection_name)
        client=self.qdrant._client() if exists else None
        q_count=self.qdrant.count_collection(self.collection_name) if exists else 0
        new_ids=[];changed_ids=[];unchanged_ids=[];source_count=0
        iterator=iter(self._load_sql_fingerprints())
        try:
            while True:
                check();page=list(islice(iterator,512))
                if not page:break
                source_count+=len(page)
                points=client.retrieve(self.collection_name,ids=[int(r["qdrant_point_id"]) for r in page],with_payload=True,with_vectors=False) if client else []
                by_id={int(p.id):dict(p.payload or {}) for p in points}
                for row in page:
                    payload=by_id.get(int(row["qdrant_point_id"]))
                    target=new_ids if payload is None else changed_ids if needs_sync(row,payload,self.embedding_model,self.embedding_version) else unchanged_ids
                    target.append(int(row["content_id"]))
                if progress:progress({"stage":"qabug_plan_sql","percent":35,"message":f"已检查 {source_count} 个 QA/BUG 片段"})
        finally:
            if hasattr(iterator,"close"):iterator.close()
        stale_ids=[]
        if exists:
            offset=None;seen=set()
            with self._connect() as conn,conn.cursor() as cur:
                while True:
                    check();page,next_offset=self.qdrant.scroll_page(self.collection_name,limit=512,offset=offset)
                    numeric=[int(p["id"]) for p in page if str(p["id"]).isdigit() and int(p["id"])<=2**63-1]
                    cur.execute(f"SELECT c.qdrant_point_id FROM public.qabug_chunks c JOIN public.qabug_records r ON r.id=c.record_id WHERE c.qdrant_point_id=ANY(%s) AND {ELIGIBLE}",(numeric,))
                    eligible={str(r["qdrant_point_id"]) for r in cur.fetchall()}
                    stale_ids.extend(p["id"] for p in page if str(p["id"]) not in eligible)
                    if next_offset is None:break
                    if repr(next_offset) in seen:raise RuntimeError("Repeated QA/BUG collection page")
                    seen.add(repr(next_offset));offset=next_offset
        with self._connect() as conn,conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM public.qabug_embedding_state WHERE qdrant_collection=%s",(self.collection_name,))
            state_count=int(cur.fetchone()["n"])
        if progress:progress({"stage":"qabug_plan_done","percent":100,"message":"QA/BUG 分页核对完成"})
        return QdrantSyncPlan(project_code="QABUG",database_name=self.postgres.database,collection_name=self.collection_name,
            source_count=source_count,qdrant_count=q_count,state_count=state_count,new_ids=tuple(new_ids),changed_ids=tuple(changed_ids),
            unchanged_ids=tuple(unchanged_ids),stale_ids=tuple(stale_ids),queued_delete_ids=(),drift_delta=q_count-source_count)

    def _reset_for_full_rebuild(self):
        raise RuntimeError("Use sync(full_rebuild=True); batch recalculation preserves the collection")

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
            # Full rebuild recalculates current points in batches without dropping
            # the collection or hiding all already usable enterprise evidence.
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
                    percent = min(95, 10 + (85 * done / max(1, total)))
                    progress({
                        "stage": "qabug_embed",
                        "percent": percent,
                        "message": f"QA/BUG 已处理 {done}/{total} · {key}",
                    })

            def manager_checkpoint(event):
                with self._connect() as db,db.cursor() as cur:
                    if event["phase"]=="LOCK_ACQUIRED":
                        cur.execute("""UPDATE public.qabug_qdrant_sync_runs SET status='INTERRUPTED',finished_at=now(),
                            error_message='Previous process ended; completed batches retained'
                            WHERE collection_name=%s AND id<>%s AND status='RUNNING'""",(self.collection_name,run_id))
                    else:
                        cur.execute("UPDATE public.qabug_qdrant_sync_runs SET upserted_count=%s,deleted_count=%s WHERE id=%s",
                            (event["upserted"],event.get("deleted",0),run_id))
                    db.commit()

            report = manager.sync_from_postgres(
                postgres_profile=self.postgres,
                password=self.postgres_password,
                embedding_model=self.embedding_model,
                embedding_version=self.embedding_version,
                batch_size=self.batch_size,
                progress=manager_progress,
                prune_stale=True,
                cancel_check=cancel_check,
                force_reembed=full_rebuild,
                checkpoint=manager_checkpoint,
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
                    SET status=%s, upserted_count=%s, deleted_count=%s,
                        qdrant_after_count=%s, finished_at=CURRENT_TIMESTAMP
                    WHERE id=%s
                    """,
                    (report.status, report.points_upserted, report.stale_points_deleted, final_count, run_id),
                )
                conn.commit()
            if progress:
                progress({"stage": "qabug_done", "percent": 100, "message": "QA/BUG 同步完成" if report.status=="READY" else "来源发生变化，已保存批次，请再次增量核对"})
            return QdrantSyncResult(
                run_id=run_id,
                mode=mode,
                status=report.status,
                plan=plan,
                upserted_count=report.points_upserted,
                deleted_count=report.stale_points_deleted,
                failed_count=report.source_changed_count,
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
