from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

import psycopg
from psycopg.rows import dict_row

from app.enterprise.profiles import PostgresProfile, QdrantProfile
from app.enterprise.qdrant_service import QdrantService
from app.rag.qdrant_governance import PAYLOAD_INDEX_CONTRACT
from app.storage.primary_document_policy import PrimarySpecificationDocumentPolicy


LOGGER = logging.getLogger("document_intelligence_console.rag.qdrant_sync")

CURRENT_SOURCE_VIEW = "public.rag_current_chunks"
CURRENT_INDEX_VERSION = "current-index-v1"
DEFAULT_EMBEDDING_MODEL = "BAAI/bge-m3"
DEFAULT_VECTOR_DIMENSION = 1024
DEFAULT_BATCH_SIZE = 32


class QdrantSyncError(RuntimeError):
    pass


class QdrantSyncCancelled(QdrantSyncError):
    pass


@dataclass(frozen=True, slots=True)
class SyncSourceChunk:
    content_id: int
    document_id: str
    file_name: str
    file_type: str
    project_code: str
    project_name: str
    spec_type: str
    spec_subtype: str
    region_scope: str
    specification_id: int | None
    specification_key: str
    specification_code: str
    specification_name: str
    specification_version_id: int | None
    specification_version: str
    chapter_id: str
    chapter_title: str
    section_id: str
    section_title: str
    page_number: int | None
    chunk_index: int
    sort_order: int
    token_count: int
    artifact_type: str
    document_role: str
    issue_key: str
    issue_type: str
    issue_status: str
    priority: str
    component: str
    assignee: str
    root_cause: str
    steps_to_reproduce: str
    expected_result: str
    actual_result: str
    resolution: str
    fix_version: str
    affects_version: str
    qa_result: str
    test_case: str
    issue_updated: str
    content_hash: str
    index_hash: str
    content: str = ""


@dataclass(frozen=True, slots=True)
class QdrantSyncPlan:
    project_code: str
    database_name: str
    collection_name: str
    source_count: int
    qdrant_count: int
    state_count: int
    new_ids: tuple[int, ...]
    changed_ids: tuple[int, ...]
    unchanged_ids: tuple[int, ...]
    stale_ids: tuple[int, ...]
    queued_delete_ids: tuple[int, ...]
    drift_delta: int = 0
    warnings: tuple[str, ...] = ()

    @property
    def new_count(self) -> int:
        return len(self.new_ids)

    @property
    def changed_count(self) -> int:
        return len(self.changed_ids)

    @property
    def unchanged_count(self) -> int:
        return len(self.unchanged_ids)

    @property
    def stale_count(self) -> int:
        return len(self.stale_ids)

    @property
    def work_count(self) -> int:
        return self.new_count + self.changed_count


@dataclass(frozen=True, slots=True)
class QdrantSyncResult:
    run_id: int
    mode: str
    status: str
    plan: QdrantSyncPlan
    upserted_count: int
    deleted_count: int
    failed_count: int
    final_qdrant_count: int
    elapsed_seconds: float
    warnings: tuple[str, ...] = ()


ProgressCallback = Callable[[dict[str, Any]], None]
CancelCheck = Callable[[], bool]


class CurrentQdrantSyncService:
    """Synchronize PostgreSQL current chunks into a derived Qdrant index.

    PostgreSQL remains the source of truth. Qdrant payloads intentionally keep
    only retrieval metadata and a stable ``content_id`` pointer back to SQL; the
    canonical chunk text is not duplicated into Qdrant.
    """

    def __init__(
        self,
        *,
        postgres: PostgresProfile,
        postgres_password: str,
        qdrant: QdrantProfile,
        qdrant_api_key: str = "",
        project_code: str,
        collection_name: str | None = None,
        embedding_model: str = DEFAULT_EMBEDDING_MODEL,
        embedding_version: str = CURRENT_INDEX_VERSION,
        vector_dimension: int = DEFAULT_VECTOR_DIMENSION,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        self.postgres = postgres
        self.postgres_password = str(postgres_password or "")
        self.qdrant_profile = qdrant
        self.qdrant_api_key = str(qdrant_api_key or "")
        self.project_code = self._normalize_project_code(project_code)
        self.collection_name = (
            str(collection_name or "").strip()
            or self.default_collection_name(self.project_code)
        )
        self.embedding_model = str(embedding_model or DEFAULT_EMBEDDING_MODEL).strip()
        self.embedding_version = str(embedding_version or CURRENT_INDEX_VERSION).strip()
        self.vector_dimension = int(vector_dimension)
        self.batch_size = int(batch_size)
        self._embedding_client: Any | None = None
        self._schema_ready = False
        self._validate()
        self.qdrant = QdrantService(qdrant, self.qdrant_api_key)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @staticmethod
    def default_collection_name(project_code: str) -> str:
        code = CurrentQdrantSyncService._normalize_project_code(project_code)
        mapping = {
            "21MM": "rag_21mm_current_chunks",
            "24MM": "rag_24mm_current_chunks",
            "COMMON": "rag_common_current_chunks",
        }
        return mapping[code]

    def ensure_payload_index_contract(self, *, repair_mismatched: bool = False):
        """Ensure enterprise filter indexes without touching vector data."""
        result = self.qdrant.ensure_payload_indexes(
            self.collection_name,
            PAYLOAD_INDEX_CONTRACT,
            repair_mismatched=repair_mismatched,
        )
        if result.mismatched and not repair_mismatched:
            details = ", ".join(
                f"{item.field_name}:{item.actual_type}->{item.expected_type}"
                for item in result.mismatched
            )
            raise QdrantSyncError(
                "Qdrant payload index contract mismatch: " + details +
                ". Use Repair Payload Indexes before syncing."
            )
        return result

    def ensure_operational_schema(self) -> None:
        """Ensure sync metadata exists without replaying DDL on every click.

        v5.3.0 executed ALTER/CREATE INDEX statements for every Preview/Sync.
        On a populated ``embeddings`` table that first CREATE INDEX could make
        the UI appear stuck at "Building current-only sync plan".  v5.3.1
        performs a cheap schema_version check first and runs DDL only when the
        component is actually missing.
        """
        if self._schema_ready:
            return
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT version
                        FROM public.schema_version
                        WHERE component = 'document_ingestion_qdrant_current_sync'
                        """
                    )
                    row = cur.fetchone()
                    if row is not None and int(row.get("version") or 0) >= 1:
                        self._schema_ready = True
                        return
        except Exception:
            # Fall through to the idempotent bootstrap.  This also handles a
            # database that predates schema_version.
            pass

        statements = (
            """
            ALTER TABLE public.embeddings
                ADD COLUMN IF NOT EXISTS index_hash TEXT,
                ADD COLUMN IF NOT EXISTS source_view TEXT,
                ADD COLUMN IF NOT EXISTS last_synced_at TIMESTAMPTZ,
                ADD COLUMN IF NOT EXISTS is_current_index BOOLEAN NOT NULL DEFAULT TRUE
            """,
            """
            CREATE TABLE IF NOT EXISTS public.qdrant_sync_runs (
                id BIGSERIAL PRIMARY KEY,
                project_code TEXT NOT NULL,
                database_name TEXT NOT NULL,
                collection_name TEXT NOT NULL,
                source_view TEXT NOT NULL DEFAULT 'public.rag_current_chunks',
                mode TEXT NOT NULL,
                status TEXT NOT NULL,
                embedding_model TEXT NOT NULL,
                embedding_version TEXT NOT NULL,
                vector_dimension INTEGER NOT NULL,
                source_count INTEGER NOT NULL DEFAULT 0,
                qdrant_before_count INTEGER NOT NULL DEFAULT 0,
                new_count INTEGER NOT NULL DEFAULT 0,
                changed_count INTEGER NOT NULL DEFAULT 0,
                unchanged_count INTEGER NOT NULL DEFAULT 0,
                stale_count INTEGER NOT NULL DEFAULT 0,
                upserted_count INTEGER NOT NULL DEFAULT 0,
                deleted_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                qdrant_after_count INTEGER,
                drift_delta INTEGER NOT NULL DEFAULT 0,
                warnings JSONB NOT NULL DEFAULT '[]'::jsonb,
                error_message TEXT,
                started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                finished_at TIMESTAMPTZ
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_qdrant_sync_runs_scope_started
            ON public.qdrant_sync_runs(project_code, collection_name, started_at DESC)
            """,
            """
            INSERT INTO public.schema_version(component, version, name, applied_at)
            VALUES (
                'document_ingestion_qdrant_current_sync',
                1,
                'current-only-qdrant-sync-v1',
                NOW()
            )
            ON CONFLICT (component)
            DO UPDATE SET
                version = EXCLUDED.version,
                name = EXCLUDED.name,
                applied_at = EXCLUDED.applied_at
            """,
        )
        with self._connect() as conn:
            with conn.cursor() as cur:
                for statement in statements:
                    cur.execute(statement)
            conn.commit()
        self._schema_ready = True

    def preview(
        self,
        *,
        progress: ProgressCallback | None = None,
        cancel_check: CancelCheck | None = None,
    ) -> QdrantSyncPlan:
        self._emit_progress(progress, "plan_schema", 5, 100, "Checking sync schema…")
        self.ensure_operational_schema()
        self._check_cancel(cancel_check)

        self._emit_progress(progress, "plan_source", 20, 100, "Scanning current SQL chunk fingerprints…")
        source_rows = self._load_source_chunks(include_content=False)
        self._check_cancel(cancel_check)

        self._emit_progress(progress, "plan_state", 48, 100, "Loading PostgreSQL Qdrant sync state…")
        state = self._load_embedding_state()
        queued_delete_ids = self._load_pending_delete_ids()
        self._check_cancel(cancel_check)

        self._emit_progress(progress, "plan_delta", 66, 100, "Comparing current chunks with sync fingerprints…")
        source_ids = {row.content_id for row in source_rows}
        source_by_id = {row.content_id: row for row in source_rows}
        state_ids = set(state)

        new_ids: list[int] = []
        changed_ids: list[int] = []
        unchanged_ids: list[int] = []
        for index, content_id in enumerate(sorted(source_ids), start=1):
            if index % 2000 == 0:
                self._check_cancel(cancel_check)
            row = source_by_id[content_id]
            current = state.get(content_id)
            if current is None:
                new_ids.append(content_id)
                continue
            if (
                str(current.get("index_hash") or "") != row.index_hash
                or int(current.get("vector_dimension") or 0) != self.vector_dimension
                or str(current.get("qdrant_point_id") or "") != str(content_id)
                or not bool(current.get("is_current_index", True))
            ):
                changed_ids.append(content_id)
            else:
                unchanged_ids.append(content_id)

        stale_from_state = state_ids - source_ids
        stale_ids = sorted(stale_from_state | set(queued_delete_ids))

        self._emit_progress(progress, "plan_qdrant", 84, 100, "Reading Qdrant point count…")
        warnings: list[str] = []
        try:
            if self.qdrant.collection_exists(self.collection_name):
                qdrant_count = self.qdrant.count_collection(self.collection_name)
            else:
                qdrant_count = 0
        except Exception as exc:
            qdrant_count = -1
            warnings.append(
                f"Qdrant is not currently reachable for an exact count: {exc}"
            )

        state_count = len(state_ids)
        drift_delta = 0 if qdrant_count < 0 else qdrant_count - state_count
        if drift_delta:
            warnings.append(
                "Qdrant point count differs from PostgreSQL embedding state "
                f"by {drift_delta:+d}. Sync will process known deltas; Full Rebuild "
                "is the authoritative repair if the final count still differs."
            )

        plan = QdrantSyncPlan(
            project_code=self.project_code,
            database_name=self.postgres.database,
            collection_name=self.collection_name,
            source_count=len(source_rows),
            qdrant_count=qdrant_count,
            state_count=state_count,
            new_ids=tuple(new_ids),
            changed_ids=tuple(changed_ids),
            unchanged_ids=tuple(unchanged_ids),
            stale_ids=tuple(stale_ids),
            queued_delete_ids=tuple(sorted(queued_delete_ids)),
            drift_delta=drift_delta,
            warnings=tuple(warnings),
        )
        self._emit_progress(
            progress,
            "plan_done",
            100,
            100,
            f"Plan ready: SQL={plan.source_count}, New={plan.new_count}, Changed={plan.changed_count}, Stale={plan.stale_count}",
            source_count=plan.source_count,
            qdrant_count=plan.qdrant_count,
            state_count=plan.state_count,
            new_count=plan.new_count,
            changed_count=plan.changed_count,
            unchanged_count=plan.unchanged_count,
            stale_count=plan.stale_count,
            work_count=plan.work_count,
        )
        return plan

    def sync(
        self,
        *,
        full_rebuild: bool = False,
        progress: ProgressCallback | None = None,
        cancel_check: CancelCheck | None = None,
    ) -> QdrantSyncResult:
        """Run a resumable current-only sync with durable live progress.

        Every successfully upserted batch is committed to both Qdrant and the
        PostgreSQL embedding state immediately. If the desktop process is
        closed mid-run, the next invocation keeps those completed batches and
        only processes the remaining New/Changed ids.
        """
        self.ensure_operational_schema()
        mode = "FULL_REBUILD" if full_rebuild else "INCREMENTAL"
        started = time.perf_counter()
        lock_conn = self._connect()
        lock_key = self._sync_lock_key()
        run_id = 0
        plan: QdrantSyncPlan | None = None
        upserted = 0
        deleted = 0
        failed = 0
        warnings: list[str] = []

        try:
            if not self._acquire_lock(lock_conn, lock_key):
                raise QdrantSyncError(
                    "Another SQL → Qdrant sync is already running for this collection."
                )

            # If the previous desktop process was terminated without executing
            # its finally block, its RUNNING row is now a zombie. We own the
            # advisory lock, therefore no legitimate sync for this collection
            # can currently exist and it is safe to recover those rows.
            self._recover_interrupted_runs_locked(lock_conn)

            def plan_progress(payload: dict[str, Any]) -> None:
                if progress is None:
                    return
                mapped = dict(payload)
                mapped["percent"] = round(
                    min(15.0, max(0.0, float(payload.get("percent") or 0.0) * 0.15)),
                    1,
                )
                progress(mapped)

            plan = self.preview(progress=plan_progress, cancel_check=cancel_check)
            warnings.extend(plan.warnings)
            resume_detected = (
                not full_rebuild
                and plan.work_count > 0
                and plan.unchanged_count > 0
                and self._has_resume_candidate(plan)
            )
            run_id = self._create_run(mode, plan)

            if resume_detected:
                start_message = (
                    f"Resume detected: {plan.unchanged_count}/{plan.source_count} already indexed; "
                    f"{plan.work_count} remaining"
                )
            elif not full_rebuild and plan.work_count > 0 and plan.unchanged_count > 0:
                start_message = (
                    f"Incremental state: {plan.unchanged_count}/{plan.source_count} already indexed; "
                    f"{plan.work_count} to upsert"
                )
            else:
                start_message = (
                    f"{mode}: {plan.work_count} chunk(s) to upsert, "
                    f"{plan.stale_count} stale point(s)"
                )

            self._emit_progress(
                progress,
                "run_started",
                0,
                max(plan.work_count, 1),
                start_message,
                percent_override=15.0,
                run_id=run_id,
                mode=mode,
                status="RUNNING",
                source_count=plan.source_count,
                qdrant_count=plan.qdrant_count,
                qdrant_live_count=(plan.qdrant_count if plan.qdrant_count >= 0 else plan.state_count),
                new_count=plan.new_count,
                changed_count=plan.changed_count,
                unchanged_count=plan.unchanged_count,
                stale_count=plan.stale_count,
                upserted_count=0,
                deleted_count=0,
                work_count=plan.work_count,
                resume_detected=resume_detected,
            )

            self._check_cancel(cancel_check)

            if full_rebuild:
                self._emit_progress(
                    progress,
                    "rebuild",
                    0,
                    1,
                    "Recreating current-only Qdrant collection…",
                    percent_override=16.0,
                    run_id=run_id,
                )
                if self.qdrant.collection_exists(self.collection_name):
                    self.qdrant.delete_collection(self.collection_name)
                self._delete_collection_state()
                self.qdrant.ensure_collection(self.collection_name, self.vector_dimension)
                self.ensure_payload_index_contract()
                work_ids = self._load_source_ids()
                stale_ids: list[int] = []
                qdrant_live_base = 0
                new_id_set = set(work_ids)
            else:
                self.qdrant.ensure_collection(self.collection_name, self.vector_dimension)
                self.ensure_payload_index_contract()
                work_ids = list(plan.new_ids) + list(plan.changed_ids)
                stale_ids = list(plan.stale_ids)
                qdrant_live_base = plan.qdrant_count if plan.qdrant_count >= 0 else plan.state_count
                new_id_set = set(plan.new_ids)

            total_work = max(len(work_ids), 1)
            processed = 0
            new_upserted = 0
            for batch_ids in self._batches(work_ids, self.batch_size):
                self._check_cancel(cancel_check)
                rows = self._load_source_chunks(include_content=True, content_ids=batch_ids)
                if len(rows) != len(batch_ids):
                    loaded_ids = {row.content_id for row in rows}
                    missing = sorted(set(batch_ids) - loaded_ids)
                    raise QdrantSyncError(
                        f"Current source changed during sync; missing content ids: {missing[:10]}"
                    )
                vectors = self._embed_rows(rows)
                points = [
                    {
                        "id": row.content_id,
                        "vector": vector,
                        "payload": self._payload_for(row),
                    }
                    for row, vector in zip(rows, vectors, strict=True)
                ]
                self.qdrant.upsert_points(self.collection_name, points)
                self._upsert_embedding_state(rows)
                upserted += len(rows)
                processed += len(rows)
                new_upserted += sum(1 for content_id in batch_ids if content_id in new_id_set)
                qdrant_live_count = (
                    upserted if full_rebuild else max(0, qdrant_live_base + new_upserted)
                )
                self._update_run_progress(
                    run_id,
                    upserted_count=upserted,
                    deleted_count=deleted,
                    qdrant_after_count=qdrant_live_count,
                )
                overall_percent = 16.0 + (processed / total_work) * 76.0
                self._emit_progress(
                    progress,
                    "upsert",
                    processed,
                    total_work,
                    f"Embedded and upserted {processed}/{len(work_ids)} current chunks",
                    percent_override=overall_percent,
                    run_id=run_id,
                    mode=mode,
                    status="RUNNING",
                    source_count=plan.source_count,
                    qdrant_live_count=qdrant_live_count,
                    upserted_count=upserted,
                    deleted_count=deleted,
                    work_count=len(work_ids),
                )

            self._check_cancel(cancel_check)

            if stale_ids:
                self._emit_progress(
                    progress,
                    "delete",
                    0,
                    len(stale_ids),
                    f"Deleting {len(stale_ids)} stale point(s)…",
                    percent_override=93.0,
                    run_id=run_id,
                    upserted_count=upserted,
                )
                self.qdrant.delete_points(self.collection_name, stale_ids)
                self._delete_embedding_state(stale_ids)
                self._mark_delete_queue_processed(stale_ids)
                deleted = len(stale_ids)
                live_before_delete = (
                    upserted if full_rebuild else max(0, qdrant_live_base + new_upserted)
                )
                qdrant_live_count = max(0, live_before_delete - deleted)
                self._update_run_progress(
                    run_id,
                    upserted_count=upserted,
                    deleted_count=deleted,
                    qdrant_after_count=qdrant_live_count,
                )
                self._emit_progress(
                    progress,
                    "delete",
                    len(stale_ids),
                    len(stale_ids),
                    f"Deleted {len(stale_ids)} stale point(s)",
                    percent_override=97.0,
                    run_id=run_id,
                    upserted_count=upserted,
                    deleted_count=deleted,
                    qdrant_live_count=qdrant_live_count,
                )

            if full_rebuild:
                self._mark_all_delete_queue_processed()

            self._emit_progress(
                progress,
                "verify",
                1,
                1,
                "Verifying final Qdrant count…",
                percent_override=98.0,
                run_id=run_id,
                upserted_count=upserted,
                deleted_count=deleted,
            )
            final_count = self.qdrant.count_collection(self.collection_name)
            source_count = plan.source_count
            if full_rebuild:
                source_count = len(self._load_source_chunks(include_content=False))

            status = "READY"
            if final_count != source_count:
                status = "READY_WITH_DRIFT"
                warnings.append(
                    "Final Qdrant count does not match current SQL source: "
                    f"sql={source_count}, qdrant={final_count}. Run Preview and, "
                    "if necessary, Full Rebuild after ingestion activity is quiet."
                )

            elapsed = time.perf_counter() - started
            self._finish_run(
                run_id,
                status=status,
                upserted_count=upserted,
                deleted_count=deleted,
                failed_count=failed,
                qdrant_after_count=final_count,
                warnings=warnings,
            )
            self._emit_progress(
                progress,
                "done",
                1,
                1,
                f"Sync complete: {status}",
                percent_override=100.0,
                run_id=run_id,
                mode=mode,
                status=status,
                source_count=source_count,
                qdrant_live_count=final_count,
                upserted_count=upserted,
                deleted_count=deleted,
                work_count=len(work_ids),
            )
            return QdrantSyncResult(
                run_id=run_id,
                mode=mode,
                status=status,
                plan=plan,
                upserted_count=upserted,
                deleted_count=deleted,
                failed_count=failed,
                final_qdrant_count=final_count,
                elapsed_seconds=elapsed,
                warnings=tuple(warnings),
            )

        except QdrantSyncCancelled as exc:
            if run_id:
                self._finish_run(
                    run_id,
                    status="CANCELLED",
                    upserted_count=upserted,
                    deleted_count=deleted,
                    failed_count=failed,
                    qdrant_after_count=self._safe_qdrant_count(),
                    warnings=warnings,
                    error_message=str(exc),
                )
            raise
        except Exception as exc:
            failed = max(failed, 1)
            if run_id:
                self._finish_run(
                    run_id,
                    status="FAILED",
                    upserted_count=upserted,
                    deleted_count=deleted,
                    failed_count=failed,
                    qdrant_after_count=self._safe_qdrant_count(),
                    warnings=warnings,
                    error_message=str(exc),
                )
            raise
        finally:
            try:
                self._release_lock(lock_conn, lock_key)
            finally:
                lock_conn.close()

    def list_runs(self, *, limit: int = 50) -> list[dict[str, Any]]:
        self.ensure_operational_schema()
        # Safe startup/history recovery: if the advisory lock is free, any
        # persisted RUNNING row belongs to a process that no longer exists.
        self.recover_interrupted_runs()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        id,
                        mode,
                        status,
                        project_code,
                        database_name,
                        collection_name,
                        source_count,
                        new_count,
                        changed_count,
                        unchanged_count,
                        stale_count,
                        upserted_count,
                        deleted_count,
                        qdrant_after_count,
                        started_at,
                        finished_at,
                        error_message
                    FROM public.qdrant_sync_runs
                    WHERE project_code = %s
                      AND collection_name = %s
                    ORDER BY id DESC
                    LIMIT %s
                    """,
                    (self.project_code, self.collection_name, max(1, min(int(limit), 500))),
                )
                return list(cur.fetchall())

    # ------------------------------------------------------------------
    # Source + state
    # ------------------------------------------------------------------

    def _load_source_ids(self) -> list[int]:
        """Load only current PRIMARY content ids for full rebuild planning.

        Historical contract equivalent: document_role = 'SUPPORTING' is excluded.
        The governed SQL predicate also checks metadata ->> 'qa_index_policy' and
        rejects qa_index_policy = 'EXCLUDE' rows before current-index synchronization.
        v1.01 centralizes that rule in PrimarySpecificationDocumentPolicy and also
        catches legacy CoverSheet filenames that predate document_role metadata.
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                primary_d = PrimarySpecificationDocumentPolicy.sql_primary_predicate("d")
                cur.execute(
                    f"""
                    SELECT rc.content_id
                    FROM public.rag_current_chunks rc
                    JOIN public.documents d ON d.document_id = rc.document_id
                    WHERE rc.project_code = %s
                      AND LENGTH(BTRIM(rc.content)) > 0
                      AND {primary_d}
                    ORDER BY rc.content_id
                    """,
                    (self.project_code,),
                )
                return [int(row["content_id"]) for row in cur.fetchall()]

    def _load_source_chunks(
        self,
        *,
        include_content: bool,
        content_ids: Sequence[int] | None = None,
    ) -> list[SyncSourceChunk]:
        content_column = "rc.content" if include_content else "''::text AS content"
        id_clause = ""
        params: list[Any] = [self.project_code]
        if content_ids is not None:
            ids = [int(value) for value in content_ids]
            if not ids:
                return []
            id_clause = " AND rc.content_id = ANY(%s)"
            params.append(ids)

        sql = f"""
            SELECT
                rc.content_id,
                rc.document_id,
                COALESCE(rc.file_name, '') AS file_name,
                COALESCE(rc.file_type, '') AS file_type,
                COALESCE(rc.project_code, '') AS project_code,
                COALESCE(rc.project_name, '') AS project_name,
                COALESCE(rc.spec_type, '') AS spec_type,
                COALESCE(rc.spec_subtype, '') AS spec_subtype,
                COALESCE(rc.region_scope, '') AS region_scope,
                rc.specification_id,
                COALESCE(rc.specification_key, '') AS specification_key,
                COALESCE(rc.specification_code, '') AS specification_code,
                COALESCE(rc.specification_name, '') AS specification_name,
                rc.specification_version_id,
                COALESCE(rc.specification_version, '') AS specification_version,
                COALESCE(rc.chapter_id, '') AS chapter_id,
                COALESCE(rc.chapter_title, '') AS chapter_title,
                COALESCE(rc.section_id, '') AS section_id,
                COALESCE(rc.section_title, '') AS section_title,
                rc.page_number,
                COALESCE(rc.chunk_index, 0) AS chunk_index,
                COALESCE(rc.sort_order, 0) AS sort_order,
                COALESCE(rc.token_count, 0) AS token_count,
                COALESCE(cm.metadata ->> 'artifact_type', '') AS artifact_type,
                COALESCE(cm.metadata ->> 'document_role', dm.metadata ->> 'document_role', '') AS document_role,
                COALESCE(cm.metadata ->> 'issue_key', '') AS issue_key,
                COALESCE(cm.metadata ->> 'issue_type', '') AS issue_type,
                COALESCE(cm.metadata ->> 'status', '') AS issue_status,
                COALESCE(cm.metadata ->> 'priority', '') AS priority,
                COALESCE(cm.metadata ->> 'component', '') AS component,
                COALESCE(cm.metadata ->> 'assignee', '') AS assignee,
                COALESCE(cm.metadata ->> 'root_cause', '') AS root_cause,
                COALESCE(cm.metadata ->> 'steps_to_reproduce', '') AS steps_to_reproduce,
                COALESCE(cm.metadata ->> 'expected_result', '') AS expected_result,
                COALESCE(cm.metadata ->> 'actual_result', '') AS actual_result,
                COALESCE(cm.metadata ->> 'resolution', '') AS resolution,
                COALESCE(cm.metadata ->> 'fix_version', '') AS fix_version,
                COALESCE(cm.metadata ->> 'affects_version', '') AS affects_version,
                COALESCE(cm.metadata ->> 'qa_result', '') AS qa_result,
                COALESCE(cm.metadata ->> 'test_case', '') AS test_case,
                COALESCE(cm.metadata ->> 'updated', '') AS issue_updated,
                COALESCE(NULLIF(rc.content_hash, ''), md5(rc.content)) AS content_hash,
                {content_column}
            FROM public.rag_current_chunks rc
            LEFT JOIN public.contents cm ON cm.id = rc.content_id
            LEFT JOIN public.documents dm ON dm.document_id = rc.document_id
            WHERE rc.project_code = %s
              AND LENGTH(BTRIM(rc.content)) > 0
              AND {PrimarySpecificationDocumentPolicy.sql_primary_predicate("dm")}
              {id_clause}
            ORDER BY rc.content_id
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                rows = list(cur.fetchall())

        result: list[SyncSourceChunk] = []
        for row in rows:
            content_hash = str(row.get("content_hash") or "")
            index_hash = self._index_hash(row, content_hash)
            result.append(
                SyncSourceChunk(
                    content_id=int(row["content_id"]),
                    document_id=str(row.get("document_id") or ""),
                    file_name=str(row.get("file_name") or ""),
                    file_type=str(row.get("file_type") or ""),
                    project_code=str(row.get("project_code") or ""),
                    project_name=str(row.get("project_name") or ""),
                    spec_type=str(row.get("spec_type") or ""),
                    spec_subtype=str(row.get("spec_subtype") or ""),
                    region_scope=str(row.get("region_scope") or ""),
                    specification_id=(
                        int(row["specification_id"])
                        if row.get("specification_id") is not None
                        else None
                    ),
                    specification_key=str(row.get("specification_key") or ""),
                    specification_code=str(row.get("specification_code") or ""),
                    specification_name=str(row.get("specification_name") or ""),
                    specification_version_id=(
                        int(row["specification_version_id"])
                        if row.get("specification_version_id") is not None
                        else None
                    ),
                    specification_version=str(row.get("specification_version") or ""),
                    chapter_id=str(row.get("chapter_id") or ""),
                    chapter_title=str(row.get("chapter_title") or ""),
                    section_id=str(row.get("section_id") or ""),
                    section_title=str(row.get("section_title") or ""),
                    page_number=(
                        int(row["page_number"])
                        if row.get("page_number") is not None
                        else None
                    ),
                    chunk_index=int(row.get("chunk_index") or 0),
                    sort_order=int(row.get("sort_order") or 0),
                    token_count=int(row.get("token_count") or 0),
                    artifact_type=str(row.get("artifact_type") or ""),
                    document_role=str(row.get("document_role") or ""),
                    issue_key=str(row.get("issue_key") or ""),
                    issue_type=str(row.get("issue_type") or ""),
                    issue_status=str(row.get("issue_status") or ""),
                    priority=str(row.get("priority") or ""),
                    component=str(row.get("component") or ""),
                    assignee=str(row.get("assignee") or ""),
                    root_cause=str(row.get("root_cause") or ""),
                    steps_to_reproduce=str(row.get("steps_to_reproduce") or ""),
                    expected_result=str(row.get("expected_result") or ""),
                    actual_result=str(row.get("actual_result") or ""),
                    resolution=str(row.get("resolution") or ""),
                    fix_version=str(row.get("fix_version") or ""),
                    affects_version=str(row.get("affects_version") or ""),
                    qa_result=str(row.get("qa_result") or ""),
                    test_case=str(row.get("test_case") or ""),
                    issue_updated=str(row.get("issue_updated") or ""),
                    content_hash=content_hash,
                    index_hash=index_hash,
                    content=str(row.get("content") or ""),
                )
            )
        return result

    def _load_embedding_state(self) -> dict[int, dict[str, Any]]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        content_id,
                        qdrant_point_id,
                        content_hash,
                        index_hash,
                        vector_dimension,
                        is_current_index,
                        last_synced_at
                    FROM public.embeddings
                    WHERE qdrant_collection = %s
                      AND model_name = %s
                      AND model_version = %s
                    """,
                    (
                        self.collection_name,
                        self.embedding_model,
                        self.embedding_version,
                    ),
                )
                return {int(row["content_id"]): dict(row) for row in cur.fetchall()}

    def _load_pending_delete_ids(self) -> set[int]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT content_id
                    FROM public.vector_delete_queue
                    WHERE processed_at IS NULL
                    ORDER BY content_id
                    """
                )
                return {int(row["content_id"]) for row in cur.fetchall()}

    def _upsert_embedding_state(self, rows: Sequence[SyncSourceChunk]) -> None:
        """Persist one batch with set-based SQL instead of two UPDATEs per row."""
        if not rows:
            return
        content_ids = [row.content_id for row in rows]
        values = [
            (
                row.content_id,
                self.embedding_model,
                self.embedding_version,
                self.vector_dimension,
                self.collection_name,
                str(row.content_id),
                row.content_hash,
                row.index_hash,
                CURRENT_SOURCE_VIEW,
            )
            for row in rows
        ]
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE public.embeddings
                    SET is_current_index = FALSE, updated_at = NOW()
                    WHERE qdrant_collection = %s
                      AND content_id = ANY(%s)
                      AND NOT (model_name = %s AND model_version = %s)
                    """,
                    (
                        self.collection_name,
                        content_ids,
                        self.embedding_model,
                        self.embedding_version,
                    ),
                )
                cur.executemany(
                    """
                    INSERT INTO public.embeddings (
                        content_id, model_name, model_version, vector_dimension,
                        qdrant_collection, qdrant_point_id, content_hash, index_hash,
                        source_view, last_synced_at, is_current_index, created_at, updated_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        NOW(), TRUE, NOW(), NOW()
                    )
                    ON CONFLICT (content_id, model_name, model_version)
                    DO UPDATE SET
                        vector_dimension = EXCLUDED.vector_dimension,
                        qdrant_collection = EXCLUDED.qdrant_collection,
                        qdrant_point_id = EXCLUDED.qdrant_point_id,
                        content_hash = EXCLUDED.content_hash,
                        index_hash = EXCLUDED.index_hash,
                        source_view = EXCLUDED.source_view,
                        last_synced_at = NOW(),
                        is_current_index = TRUE,
                        updated_at = NOW()
                    """,
                    values,
                )
                cur.execute(
                    """
                    UPDATE public.contents c
                    SET
                        embedding_status = 'COMPLETED',
                        embedded_at = NOW(),
                        embedding_model = %s,
                        embedding_version = %s,
                        embedded_content_hash = src.content_hash,
                        embedding_error = NULL,
                        updated_at = NOW()
                    FROM (
                        SELECT *
                        FROM UNNEST(%s::bigint[], %s::text[])
                            AS u(content_id, content_hash)
                    ) src
                    WHERE c.id = src.content_id
                    """,
                    (
                        self.embedding_model,
                        self.embedding_version,
                        content_ids,
                        [row.content_hash for row in rows],
                    ),
                )
            conn.commit()

    def _delete_embedding_state(self, content_ids: Sequence[int]) -> None:
        ids = [int(value) for value in content_ids]
        if not ids:
            return
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM public.embeddings
                    WHERE qdrant_collection = %s
                      AND model_name = %s
                      AND model_version = %s
                      AND content_id = ANY(%s)
                    """,
                    (
                        self.collection_name,
                        self.embedding_model,
                        self.embedding_version,
                        ids,
                    ),
                )
            conn.commit()

    def _delete_collection_state(self) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM public.embeddings
                    WHERE qdrant_collection = %s
                    """,
                    (self.collection_name,),
                )
            conn.commit()

    def _mark_delete_queue_processed(self, content_ids: Sequence[int]) -> None:
        ids = [int(value) for value in content_ids]
        if not ids:
            return
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE public.vector_delete_queue
                    SET processed_at = NOW(), last_error = NULL
                    WHERE processed_at IS NULL
                      AND content_id = ANY(%s)
                    """,
                    (ids,),
                )
            conn.commit()

    def _mark_all_delete_queue_processed(self) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE public.vector_delete_queue
                    SET processed_at = NOW(), last_error = NULL
                    WHERE processed_at IS NULL
                    """
                )
            conn.commit()

    # ------------------------------------------------------------------
    # Embedding + payload
    # ------------------------------------------------------------------

    def _get_embedding_client(self) -> Any:
        if self._embedding_client is None:
            try:
                from app.embedding.embedding_client import (
                    EmbeddingClient,
                    EmbeddingClientConfig,
                )
            except Exception as exc:  # pragma: no cover - dependency failure path
                raise QdrantSyncError(f"Embedding runtime is unavailable: {exc}") from exc
            config = EmbeddingClientConfig(
                model_name=self.embedding_model,
                batch_size=self.batch_size,
                normalize_embeddings=True,
                expected_dimension=self.vector_dimension,
                show_progress_bar=False,
                trust_remote_code=False,
            )
            self._embedding_client = EmbeddingClient(config)
        return self._embedding_client

    def _embed_rows(self, rows: Sequence[SyncSourceChunk]) -> list[list[float]]:
        client = self._get_embedding_client()
        texts = [self._embedding_text(row) for row in rows]
        vectors = client.embed_texts(texts)
        if len(vectors) != len(rows):
            raise QdrantSyncError(
                f"Embedding result count mismatch: rows={len(rows)} vectors={len(vectors)}"
            )
        for vector in vectors:
            if len(vector) != self.vector_dimension:
                raise QdrantSyncError(
                    "Embedding dimension mismatch: "
                    f"expected={self.vector_dimension} actual={len(vector)}"
                )
            if not all(math.isfinite(float(value)) for value in vector):
                raise QdrantSyncError("Embedding contains NaN or infinite values.")
        return vectors

    def _embedding_text(self, row: SyncSourceChunk) -> str:
        context: list[str] = []
        if row.artifact_type == "ISSUE_REGISTER":
            context.append("Artifact: QA/BUG Issue Register")
            if row.issue_key:
                context.append(f"Issue Key: {row.issue_key}")
            if row.issue_type:
                context.append(f"Issue Type: {row.issue_type}")
            if row.issue_status:
                context.append(f"Status: {row.issue_status}")
            if row.priority:
                context.append(f"Priority: {row.priority}")
            if row.component:
                context.append(f"Component: {row.component}")
            if row.root_cause:
                context.append(f"Root Cause: {row.root_cause}")
        elif row.specification_code or row.specification_name:
            identity = " ".join(
                value
                for value in (row.specification_code, row.specification_name)
                if value
            )
            context.append(f"Specification: {identity}")
        else:
            context.append(f"Document: {row.file_name or row.document_id}")
        if row.specification_version:
            context.append(f"Version: {row.specification_version}")
        if row.chapter_title:
            context.append(f"Chapter: {row.chapter_title}")
        if row.section_title:
            context.append(f"Section: {row.section_title}")
        return "\n".join(context) + "\n\n" + row.content

    def _payload_for(self, row: SyncSourceChunk) -> dict[str, Any]:
        return {
            "content_id": row.content_id,
            "chunk_key": f"{row.document_id}:{row.section_id}:{row.chunk_index}",
            "content_hash": row.content_hash,
            "index_hash": row.index_hash,
            "project_code": row.project_code,
            "project_name": row.project_name,
            "document_id": row.document_id,
            "file_name": row.file_name,
            "file_type": row.file_type,
            "spec_type": row.spec_type,
            "spec_subtype": row.spec_subtype,
            "region_scope": row.region_scope,
            "specification_id": row.specification_id,
            "specification_key": row.specification_key,
            "spec_code": row.specification_code,
            "specification_name": row.specification_name,
            "specification_version_id": row.specification_version_id,
            "version": row.specification_version,
            "is_current": True,
            "chapter_id": row.chapter_id,
            "chapter_title": row.chapter_title,
            "section_id": row.section_id,
            "section_title": row.section_title,
            "page_number": row.page_number,
            "chunk_index": row.chunk_index,
            "sort_order": row.sort_order,
            "token_count": row.token_count,
            "artifact_type": row.artifact_type,
            "document_role": row.document_role or "PRIMARY",
            "evidence_role": "PRIMARY_CONTENT",
            "qa_index_policy": "INCLUDE",
            "supporting_document_type": "",
            "issue_key": row.issue_key,
            "issue_type": row.issue_type,
            "issue_status": row.issue_status,
            "priority": row.priority,
            "component": row.component,
            "assignee": row.assignee,
            "root_cause": row.root_cause,
            "steps_to_reproduce": row.steps_to_reproduce,
            "expected_result": row.expected_result,
            "actual_result": row.actual_result,
            "resolution": row.resolution,
            "fix_version": row.fix_version,
            "affects_version": row.affects_version,
            "qa_result": row.qa_result,
            "test_case": row.test_case,
            "issue_updated": row.issue_updated,
            "embedding_model": self.embedding_model,
            "embedding_version": self.embedding_version,
            "index_scope": "CURRENT",
            "source_view": CURRENT_SOURCE_VIEW,
        }

    def _index_hash(self, row: dict[str, Any], content_hash: str) -> str:
        identity = {
            "content_hash": content_hash,
            "document_id": str(row.get("document_id") or ""),
            "file_name": str(row.get("file_name") or ""),
            "specification_key": str(row.get("specification_key") or ""),
            "specification_code": str(row.get("specification_code") or ""),
            "specification_name": str(row.get("specification_name") or ""),
            "specification_version": str(row.get("specification_version") or ""),
            "chapter_id": str(row.get("chapter_id") or ""),
            "chapter_title": str(row.get("chapter_title") or ""),
            "section_id": str(row.get("section_id") or ""),
            "section_title": str(row.get("section_title") or ""),
            "page_number": row.get("page_number"),
            "chunk_index": int(row.get("chunk_index") or 0),
            "model": self.embedding_model,
            "embedding_version": self.embedding_version,
            "dimension": self.vector_dimension,
        }
        if str(row.get("artifact_type") or "").upper() == "ISSUE_REGISTER":
            identity.update({
                "artifact_type": "ISSUE_REGISTER",
                "document_role": str(row.get("document_role") or ""),
                "issue_key": str(row.get("issue_key") or ""),
                "issue_type": str(row.get("issue_type") or ""),
                "issue_status": str(row.get("issue_status") or ""),
                "priority": str(row.get("priority") or ""),
                "component": str(row.get("component") or ""),
                "assignee": str(row.get("assignee") or ""),
                "root_cause": str(row.get("root_cause") or ""),
                "steps_to_reproduce": str(row.get("steps_to_reproduce") or ""),
                "expected_result": str(row.get("expected_result") or ""),
                "actual_result": str(row.get("actual_result") or ""),
                "resolution": str(row.get("resolution") or ""),
                "fix_version": str(row.get("fix_version") or ""),
                "affects_version": str(row.get("affects_version") or ""),
                "qa_result": str(row.get("qa_result") or ""),
                "test_case": str(row.get("test_case") or ""),
                "issue_updated": str(row.get("issue_updated") or ""),
            })
        raw = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------
    # Run history + lock
    # ------------------------------------------------------------------

    def _sync_lock_key(self) -> str:
        return f"qdrant-sync:{self.postgres.database}:{self.collection_name}"

    def recover_interrupted_runs(self) -> int:
        """Mark orphaned RUNNING rows as INTERRUPTED when no sync owns the lock."""
        self.ensure_operational_schema()
        conn = self._connect()
        key = self._sync_lock_key()
        try:
            if not self._acquire_lock(conn, key):
                return 0
            return self._recover_interrupted_runs_locked(conn)
        finally:
            try:
                self._release_lock(conn, key)
            finally:
                conn.close()

    def _recover_interrupted_runs_locked(self, conn: Any) -> int:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE public.qdrant_sync_runs AS r
                SET
                    status = 'INTERRUPTED',
                    upserted_count = GREATEST(
                        r.upserted_count,
                        COALESCE((
                            SELECT COUNT(*)::INTEGER
                            FROM public.embeddings e
                            WHERE e.qdrant_collection = r.collection_name
                              AND e.model_name = r.embedding_model
                              AND e.model_version = r.embedding_version
                              AND e.last_synced_at IS NOT NULL
                              AND e.last_synced_at >= r.started_at
                              AND e.last_synced_at < COALESCE((
                                  SELECT MIN(r2.started_at)
                                  FROM public.qdrant_sync_runs r2
                                  WHERE r2.project_code = r.project_code
                                    AND r2.collection_name = r.collection_name
                                    AND r2.started_at > r.started_at
                              ), NOW() + INTERVAL '1 second')
                        ), 0)
                    ),
                    qdrant_after_count = COALESCE((
                        SELECT COUNT(*)::INTEGER
                        FROM public.embeddings e
                        WHERE e.qdrant_collection = r.collection_name
                          AND e.model_name = r.embedding_model
                          AND e.model_version = r.embedding_version
                          AND e.is_current_index IS TRUE
                    ), r.qdrant_after_count),
                    error_message = COALESCE(
                        NULLIF(r.error_message, ''),
                        'Previous sync process ended before a final status was recorded. Completed batches were retained and can be resumed safely.'
                    ),
                    finished_at = COALESCE(r.finished_at, NOW())
                WHERE r.project_code = %s
                  AND r.collection_name = %s
                  AND r.status = 'RUNNING'
                RETURNING r.id
                """,
                (self.project_code, self.collection_name),
            )
            rows = list(cur.fetchall())
        conn.commit()
        if rows:
            LOGGER.warning(
                "Recovered %d orphaned Qdrant sync run(s) as INTERRUPTED for %s",
                len(rows),
                self.collection_name,
            )
        return len(rows)

    def _has_resume_candidate(self, plan: QdrantSyncPlan) -> bool:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM public.qdrant_sync_runs
                        WHERE project_code = %s
                          AND collection_name = %s
                          AND embedding_model = %s
                          AND embedding_version = %s
                          AND vector_dimension = %s
                          AND source_count = %s
                          AND status = 'INTERRUPTED'
                          AND started_at > COALESCE((
                              SELECT MAX(ok.started_at)
                              FROM public.qdrant_sync_runs ok
                              WHERE ok.project_code = %s
                                AND ok.collection_name = %s
                                AND ok.embedding_model = %s
                                AND ok.embedding_version = %s
                                AND ok.vector_dimension = %s
                                AND ok.status IN ('READY', 'READY_WITH_DRIFT')
                          ), '-infinity'::timestamptz)
                    ) AS resumable
                    """,
                    (
                        self.project_code,
                        self.collection_name,
                        self.embedding_model,
                        self.embedding_version,
                        self.vector_dimension,
                        plan.source_count,
                        self.project_code,
                        self.collection_name,
                        self.embedding_model,
                        self.embedding_version,
                        self.vector_dimension,
                    ),
                )
                row = cur.fetchone() or {}
                return bool(row.get("resumable"))

    def _update_run_progress(
        self,
        run_id: int,
        *,
        upserted_count: int,
        deleted_count: int,
        qdrant_after_count: int,
    ) -> None:
        """Persist live batch progress so Sync History survives UI/process loss."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE public.qdrant_sync_runs
                    SET
                        upserted_count = %s,
                        deleted_count = %s,
                        qdrant_after_count = %s
                    WHERE id = %s
                      AND status = 'RUNNING'
                    """,
                    (
                        int(upserted_count),
                        int(deleted_count),
                        int(qdrant_after_count),
                        int(run_id),
                    ),
                )
            conn.commit()

    def _create_run(self, mode: str, plan: QdrantSyncPlan) -> int:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.qdrant_sync_runs (
                        project_code, database_name, collection_name, source_view,
                        mode, status, embedding_model, embedding_version,
                        vector_dimension, source_count, qdrant_before_count,
                        new_count, changed_count, unchanged_count, stale_count,
                        drift_delta, warnings
                    ) VALUES (
                        %s, %s, %s, %s, %s, 'RUNNING', %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s::jsonb
                    )
                    RETURNING id
                    """,
                    (
                        self.project_code,
                        self.postgres.database,
                        self.collection_name,
                        CURRENT_SOURCE_VIEW,
                        mode,
                        self.embedding_model,
                        self.embedding_version,
                        self.vector_dimension,
                        plan.source_count,
                        plan.qdrant_count,
                        plan.new_count,
                        plan.changed_count,
                        plan.unchanged_count,
                        plan.stale_count,
                        plan.drift_delta,
                        json.dumps(list(plan.warnings), ensure_ascii=False),
                    ),
                )
                row = cur.fetchone()
            conn.commit()
        return int(row["id"])

    def _finish_run(
        self,
        run_id: int,
        *,
        status: str,
        upserted_count: int,
        deleted_count: int,
        failed_count: int,
        qdrant_after_count: int,
        warnings: Sequence[str],
        error_message: str | None = None,
    ) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE public.qdrant_sync_runs
                    SET
                        status = %s,
                        upserted_count = %s,
                        deleted_count = %s,
                        failed_count = %s,
                        qdrant_after_count = %s,
                        warnings = %s::jsonb,
                        error_message = %s,
                        finished_at = NOW()
                    WHERE id = %s
                    """,
                    (
                        status,
                        int(upserted_count),
                        int(deleted_count),
                        int(failed_count),
                        int(qdrant_after_count),
                        json.dumps(list(warnings), ensure_ascii=False),
                        error_message,
                        int(run_id),
                    ),
                )
            conn.commit()

    def _acquire_lock(self, conn: Any, key: str) -> bool:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(hashtext(%s)) AS locked", (key,))
            row = cur.fetchone() or {}
            return bool(row.get("locked"))

    @staticmethod
    def _release_lock(conn: Any, key: str) -> None:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (key,))
        except Exception:
            LOGGER.exception("Unable to release Qdrant sync advisory lock")

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def _connect(self):
        if not self.postgres_password:
            raise QdrantSyncError("PostgreSQL password is not configured.")
        kwargs = self.postgres.dsn_kwargs()
        kwargs["password"] = self.postgres_password
        return psycopg.connect(**kwargs, row_factory=dict_row)

    def _safe_qdrant_count(self) -> int:
        try:
            if not self.qdrant.collection_exists(self.collection_name):
                return 0
            return self.qdrant.count_collection(self.collection_name)
        except Exception:
            return -1

    @staticmethod
    def _batches(values: Sequence[int], batch_size: int) -> Iterable[list[int]]:
        for index in range(0, len(values), batch_size):
            yield list(values[index : index + batch_size])

    @staticmethod
    def _emit_progress(
        callback: ProgressCallback | None,
        stage: str,
        current: int,
        total: int,
        message: str,
        *,
        percent_override: float | None = None,
        **extra: Any,
    ) -> None:
        if callback is None:
            return
        total = max(int(total), 1)
        current = max(0, min(int(current), total))
        percent = (
            float(percent_override)
            if percent_override is not None
            else current / total * 100
        )
        payload: dict[str, Any] = {
            "stage": stage,
            "current": current,
            "total": total,
            "percent": round(max(0.0, min(100.0, percent)), 1),
            "message": message,
        }
        payload.update(extra)
        callback(payload)

    @staticmethod
    def _check_cancel(cancel_check: CancelCheck | None) -> None:
        if cancel_check is not None and cancel_check():
            raise QdrantSyncCancelled("SQL → Qdrant sync was cancelled by the user.")

    def _validate(self) -> None:
        if not self.postgres.host or not self.postgres.database or not self.postgres.user:
            raise QdrantSyncError("PostgreSQL profile is incomplete.")
        if not self.collection_name:
            raise QdrantSyncError("Qdrant collection name cannot be empty.")
        if not self.embedding_model:
            raise QdrantSyncError("Embedding model cannot be empty.")
        if self.vector_dimension <= 0:
            raise QdrantSyncError("Vector dimension must be greater than 0.")
        if self.batch_size < 1 or self.batch_size > 512:
            raise QdrantSyncError("Batch size must be between 1 and 512.")

    @staticmethod
    def _normalize_project_code(value: str) -> str:
        normalized = str(value or "").strip().upper()
        if normalized == "COMMON_DATA":
            normalized = "COMMON"
        if normalized not in {"21MM", "24MM", "COMMON"}:
            raise QdrantSyncError(
                f"Unsupported processing scope for current index: {value!r}"
            )
        return normalized
