from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Protocol, Sequence

from app.embedding.embedding_repository import (
    EmbeddingRepository,
    EmbeddingRepositoryError,
    PendingContent,
)

from app.embedding.embedding_service import (
    EmbeddingBatchResult,
    EmbeddingRecord,
    EmbeddingService,
    EmbeddingServiceError,
)


LOGGER = logging.getLogger(
    "document_ingestion.embedding.worker"
)


# ==================================================
# Exceptions
# ==================================================


class EmbeddingWorkerError(
    RuntimeError
):
    """
    Embedding Worker 基础异常。
    """


class EmbeddingWorkerValidationError(
    EmbeddingWorkerError
):
    """
    Worker 配置或参数异常。
    """


class VectorWriteError(
    EmbeddingWorkerError
):
    """
    Vector Store 写入失败。
    """


# ==================================================
# Vector Writer Contract
# ==================================================


class VectorWriter(
    Protocol
):
    """
    向量存储接口。

    Worker 不关心后端到底是：

        - Qdrant
        - pgvector
        - Milvus
        - Pinecone
        - Weaviate

    只要求实现：

        upsert(records)

    后续我们做 QdrantVectorStore 时，
    直接实现这个契约即可。
    """

    def upsert(
        self,
        records: Sequence[EmbeddingRecord],
    ) -> None:
        ...


# ==================================================
# Worker Configuration
# ==================================================


@dataclass(frozen=True)
class EmbeddingWorkerConfig:
    """
    Embedding Worker 配置。

    Attributes:
        batch_size:
            每次从 PostgreSQL claim 的 Chunk 数量。

        poll_interval_seconds:
            没任务时等待时间。

        failed_retry_delay_seconds:
            Batch 失败后的等待时间。

        stale_processing_minutes:
            PROCESSING 超过多少分钟视为失效任务。

        reset_stale_on_start:
            Worker 启动时是否自动恢复失效任务。

        mark_failed_on_error:
            Embedding / Vector Store 失败后是否标记 FAILED。

        dry_run:
            只验证 Embedding，不写 Vector Store。

            dry_run=True 时：
                PROCESSING → PENDING

            不会标记 COMPLETED。
    """

    batch_size: int = 32

    poll_interval_seconds: float = 2.0

    failed_retry_delay_seconds: float = 5.0

    stale_processing_minutes: int = 30

    reset_stale_on_start: bool = True

    mark_failed_on_error: bool = True

    dry_run: bool = False


# ==================================================
# Result Models
# ==================================================


@dataclass(frozen=True)
class EmbeddingWorkerBatchResult:
    """
    单个 Batch 的执行结果。
    """

    claimed_count: int

    generated_count: int

    completed_count: int

    failed_count: int

    reset_count: int

    has_work: bool

    dry_run: bool


@dataclass(frozen=True)
class EmbeddingWorkerRunResult:
    """
    Worker 一次 run() 的汇总结果。
    """

    batches: int

    claimed_count: int

    generated_count: int

    completed_count: int

    failed_count: int

    reset_count: int


# ==================================================
# Worker
# ==================================================


class EmbeddingWorker:
    """
    企业级 Embedding Worker。

    数据流：

        PostgreSQL.contents
                ↓
        embedding_status=PENDING
                ↓
        claim_pending_contents()
                ↓
            PROCESSING
                ↓
        EmbeddingService
                ↓
           BGE-M3
                ↓
          Dense Vector
                ↓
          VectorWriter
                ↓
          Qdrant / ...
                ↓
            COMPLETED

    异常：

        Embedding / Vector Store Error
                ↓
             FAILED

    Dry Run：

        PENDING
           ↓
        PROCESSING
           ↓
        Embedding
           ↓
        PENDING

    这样可以在没有 Qdrant 的情况下安全测试。
    """

    def __init__(
        self,
        *,
        service: EmbeddingService | None = None,
        repository: EmbeddingRepository | None = None,
        vector_writer: VectorWriter | None = None,
        config: EmbeddingWorkerConfig | None = None,
    ) -> None:

        self.repository = (
            repository
            or EmbeddingRepository()
        )

        self.service = (
            service
            or EmbeddingService(
                repository=self.repository
            )
        )

        self.vector_writer = (
            vector_writer
        )

        self.config = (
            config
            or EmbeddingWorkerConfig()
        )

        self._validate_config()

        self._stop_requested = False

    # ==================================================
    # Public API
    # ==================================================

    def run_once(
        self,
    ) -> EmbeddingWorkerBatchResult:
        """
        执行一个 Batch。

        推荐开发阶段首先使用这个方法。
        """

        try:
            contents = (
                self.repository
                .claim_pending_contents(
                    limit=(
                        self.config.batch_size
                    )
                )
            )

        except EmbeddingRepositoryError as exc:
            raise EmbeddingWorkerError(
                "Failed to claim embedding "
                f"tasks: {exc}"
            ) from exc

        if not contents:
            LOGGER.debug(
                "No pending embedding tasks."
            )

            return EmbeddingWorkerBatchResult(
                claimed_count=0,
                generated_count=0,
                completed_count=0,
                failed_count=0,
                reset_count=0,
                has_work=False,
                dry_run=self._is_dry_run(),
            )

        content_ids = [
            content.id
            for content in contents
        ]

        LOGGER.info(
            "Embedding batch started | "
            "claimed=%s | "
            "first_content_id=%s | "
            "last_content_id=%s",
            len(contents),
            content_ids[0],
            content_ids[-1],
        )

        # ==============================================
        # 1. Generate Embeddings
        # ==============================================

        try:
            batch_result = (
                self.service
                .embed_contents(
                    contents
                )
            )

        except Exception as exc:
            failed_count = (
                self._handle_batch_failure(
                    content_ids=content_ids,
                    exc=exc,
                    stage="embedding",
                )
            )

            return EmbeddingWorkerBatchResult(
                claimed_count=len(
                    contents
                ),
                generated_count=0,
                completed_count=0,
                failed_count=failed_count,
                reset_count=0,
                has_work=True,
                dry_run=self._is_dry_run(),
            )

        self._validate_batch_result(
            contents=contents,
            result=batch_result,
        )

        # ==============================================
        # 2. Dry Run
        # ==============================================

        if self._is_dry_run():
            reset_count = (
                self.repository
                .mark_pending(
                    content_ids
                )
            )

            LOGGER.info(
                "Embedding dry-run completed | "
                "generated=%s | "
                "reset_to_pending=%s",
                batch_result.generated_count,
                reset_count,
            )

            return EmbeddingWorkerBatchResult(
                claimed_count=len(
                    contents
                ),
                generated_count=(
                    batch_result.generated_count
                ),
                completed_count=0,
                failed_count=0,
                reset_count=reset_count,
                has_work=True,
                dry_run=True,
            )

        # ==============================================
        # 3. Vector Store
        # ==============================================

        try:
            self._write_vectors(
                batch_result.records
            )

        except Exception as exc:
            failed_count = (
                self._handle_batch_failure(
                    content_ids=content_ids,
                    exc=exc,
                    stage="vector_store",
                )
            )

            return EmbeddingWorkerBatchResult(
                claimed_count=len(
                    contents
                ),
                generated_count=(
                    batch_result.generated_count
                ),
                completed_count=0,
                failed_count=failed_count,
                reset_count=0,
                has_work=True,
                dry_run=False,
            )

        # ==============================================
        # 4. Mark COMPLETED
        # ==============================================

        try:
            completed_count = (
                self.repository
                .mark_completed(
                    content_ids
                )
            )

        except EmbeddingRepositoryError as exc:
            # Vector 已经写成功，但是状态更新失败。
            #
            # VectorStore 必须使用 upsert，
            # 所以后续 Retry 不会产生重复向量。
            raise EmbeddingWorkerError(
                "Vectors were written successfully "
                "but failed to mark contents as "
                f"COMPLETED: {exc}"
            ) from exc

        LOGGER.info(
            "Embedding batch completed | "
            "claimed=%s | "
            "generated=%s | "
            "completed=%s",
            len(contents),
            batch_result.generated_count,
            completed_count,
        )

        return EmbeddingWorkerBatchResult(
            claimed_count=len(
                contents
            ),
            generated_count=(
                batch_result.generated_count
            ),
            completed_count=(
                completed_count
            ),
            failed_count=0,
            reset_count=0,
            has_work=True,
            dry_run=False,
        )

    def run(
        self,
        *,
        max_batches: int | None = None,
    ) -> EmbeddingWorkerRunResult:
        """
        连续执行 Worker。

        Args:
            max_batches:
                最大 Batch 数。

                None:
                    一直运行直到没有任务
                    或收到 stop()。

                推荐初次测试：
                    max_batches=1

                批处理：
                    max_batches=10
        """

        self._validate_max_batches(
            max_batches
        )

        self._stop_requested = False

        if (
            self.config
            .reset_stale_on_start
        ):
            self._recover_stale_tasks()

        batches = 0

        claimed_count = 0
        generated_count = 0
        completed_count = 0
        failed_count = 0
        reset_count = 0

        LOGGER.info(
            "Embedding worker started | "
            "batch_size=%s | "
            "dry_run=%s | "
            "max_batches=%s",
            self.config.batch_size,
            self._is_dry_run(),
            max_batches,
        )

        while not self._stop_requested:
            if (
                max_batches is not None
                and batches >= max_batches
            ):
                break

            result = (
                self.run_once()
            )

            if not result.has_work:
                break

            batches += 1

            claimed_count += (
                result.claimed_count
            )

            generated_count += (
                result.generated_count
            )

            completed_count += (
                result.completed_count
            )

            failed_count += (
                result.failed_count
            )

            reset_count += (
                result.reset_count
            )

        result = EmbeddingWorkerRunResult(
            batches=batches,
            claimed_count=claimed_count,
            generated_count=generated_count,
            completed_count=completed_count,
            failed_count=failed_count,
            reset_count=reset_count,
        )

        LOGGER.info(
            "Embedding worker finished | "
            "batches=%s | "
            "claimed=%s | "
            "generated=%s | "
            "completed=%s | "
            "failed=%s | "
            "reset=%s",
            result.batches,
            result.claimed_count,
            result.generated_count,
            result.completed_count,
            result.failed_count,
            result.reset_count,
        )

        return result

    def run_forever(
        self,
    ) -> None:
        """
        常驻 Worker。

        未来用于：

            Docker
            ECS
            Kubernetes
            Windows Service

        没任务时不会退出，而是 sleep 后继续轮询。
        """

        self._stop_requested = False

        if (
            self.config
            .reset_stale_on_start
        ):
            self._recover_stale_tasks()

        LOGGER.info(
            "Embedding worker running forever | "
            "batch_size=%s | "
            "poll_interval=%s | "
            "dry_run=%s",
            self.config.batch_size,
            self.config.poll_interval_seconds,
            self._is_dry_run(),
        )

        while not self._stop_requested:
            try:
                result = (
                    self.run_once()
                )

                if not result.has_work:
                    time.sleep(
                        self.config
                        .poll_interval_seconds
                    )

            except KeyboardInterrupt:
                LOGGER.info(
                    "Embedding worker interrupted."
                )

                self.stop()

            except Exception:
                LOGGER.exception(
                    "Embedding worker iteration "
                    "failed."
                )

                time.sleep(
                    self.config
                    .failed_retry_delay_seconds
                )

    def stop(
        self,
    ) -> None:
        """
        请求 Worker 优雅停止。
        """

        self._stop_requested = True

        LOGGER.info(
            "Embedding worker stop requested."
        )

    # ==================================================
    # Vector Store
    # ==================================================

    def _write_vectors(
        self,
        records: Sequence[EmbeddingRecord],
    ) -> None:
        """
        写入 Vector Store。

        只有成功之后才允许：

            PROCESSING → COMPLETED
        """

        if self.vector_writer is None:
            raise VectorWriteError(
                "Vector writer is not configured. "
                "Use dry_run=True until Qdrant "
                "or another vector store is configured."
            )

        if not records:
            return

        try:
            self.vector_writer.upsert(
                records
            )

        except Exception as exc:
            raise VectorWriteError(
                "Failed to write embeddings "
                f"to vector store: {exc}"
            ) from exc

    # ==================================================
    # Failure Handling
    # ==================================================

    def _handle_batch_failure(
        self,
        *,
        content_ids: Sequence[int],
        exc: Exception,
        stage: str,
    ) -> int:
        """
        Batch 失败处理。

        默认：

            PROCESSING
                ↓
            FAILED

        如果：
            mark_failed_on_error=False

        则：

            PROCESSING
                ↓
            PENDING
        """

        LOGGER.exception(
            "Embedding batch failed | "
            "stage=%s | "
            "count=%s | "
            "error=%s",
            stage,
            len(content_ids),
            exc,
        )

        try:
            if (
                self.config
                .mark_failed_on_error
            ):
                updated = (
                    self.repository
                    .mark_failed(
                        content_ids
                    )
                )

            else:
                updated = (
                    self.repository
                    .mark_pending(
                        content_ids
                    )
                )

        except Exception as status_exc:
            raise EmbeddingWorkerError(
                "Embedding batch failed and "
                "status recovery also failed. "
                f"original_error={exc}; "
                f"status_error={status_exc}"
            ) from status_exc

        return int(
            updated
        )

    # ==================================================
    # Recovery
    # ==================================================

    def _recover_stale_tasks(
        self,
    ) -> int:
        """
        恢复 Worker 崩溃留下的 PROCESSING。
        """

        try:
            count = (
                self.repository
                .reset_stale_processing(
                    older_than_minutes=(
                        self.config
                        .stale_processing_minutes
                    )
                )
            )

        except EmbeddingRepositoryError as exc:
            raise EmbeddingWorkerError(
                "Failed to recover stale "
                f"embedding tasks: {exc}"
            ) from exc

        if count:
            LOGGER.warning(
                "Recovered stale embedding "
                "tasks | count=%s",
                count,
            )

        return count

    # ==================================================
    # Validation
    # ==================================================

    def _validate_config(
        self,
    ) -> None:

        if (
            self.config.batch_size
            <= 0
        ):
            raise (
                EmbeddingWorkerValidationError(
                    "batch_size must be "
                    "greater than 0."
                )
            )

        if (
            self.config.batch_size
            > 10000
        ):
            raise (
                EmbeddingWorkerValidationError(
                    "batch_size cannot "
                    "exceed 10000."
                )
            )

        if (
            self.config
            .poll_interval_seconds
            < 0
        ):
            raise (
                EmbeddingWorkerValidationError(
                    "poll_interval_seconds "
                    "cannot be negative."
                )
            )

        if (
            self.config
            .failed_retry_delay_seconds
            < 0
        ):
            raise (
                EmbeddingWorkerValidationError(
                    "failed_retry_delay_seconds "
                    "cannot be negative."
                )
            )

        if (
            self.config
            .stale_processing_minutes
            <= 0
        ):
            raise (
                EmbeddingWorkerValidationError(
                    "stale_processing_minutes "
                    "must be greater than 0."
                )
            )

    @staticmethod
    def _validate_max_batches(
        max_batches: int | None,
    ) -> None:

        if max_batches is None:
            return

        if not isinstance(
            max_batches,
            int,
        ):
            raise (
                EmbeddingWorkerValidationError(
                    "max_batches must "
                    "be integer or None."
                )
            )

        if max_batches <= 0:
            raise (
                EmbeddingWorkerValidationError(
                    "max_batches must "
                    "be greater than 0."
                )
            )

    @staticmethod
    def _validate_batch_result(
        *,
        contents: Sequence[
            PendingContent
        ],
        result: EmbeddingBatchResult,
    ) -> None:
        """
        确认 Embedding 输入输出一一对应。
        """

        if (
            result.generated_count
            != len(contents)
        ):
            raise EmbeddingWorkerError(
                "Embedding result count "
                "mismatch. "
                f"claimed={len(contents)}, "
                f"generated="
                f"{result.generated_count}"
            )

        if (
            len(result.records)
            != len(contents)
        ):
            raise EmbeddingWorkerError(
                "Embedding record count "
                "mismatch."
            )

        source_ids = {
            content.id
            for content in contents
        }

        result_ids = {
            record.content_id
            for record in result.records
        }

        if source_ids != result_ids:
            raise EmbeddingWorkerError(
                "Embedding content IDs "
                "do not match claimed IDs."
            )

    def _is_dry_run(
        self,
    ) -> bool:
        """
        没有 VectorWriter 时自动视为 Dry Run。

        防止开发阶段误把没有落库的
        Embedding 标记成 COMPLETED。
        """

        return (
            self.config.dry_run
            or self.vector_writer is None
        )