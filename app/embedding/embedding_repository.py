from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, Sequence

from app.database.connection import (
    DatabaseConnection,
    DatabaseConnectionError,
)


LOGGER = logging.getLogger(
    "document_ingestion.embedding.repository"
)


class EmbeddingRepositoryError(
    RuntimeError
):
    """
    Embedding Repository 基础异常。
    """


class EmbeddingRepositoryValidationError(
    EmbeddingRepositoryError
):
    """
    Repository 输入参数校验异常。
    """


class EmbeddingStatus:
    """
    Embedding 状态常量。

    当前状态流：

        PENDING
            ↓
        PROCESSING
            ↓
        COMPLETED

    异常：

        PROCESSING
            ↓
        FAILED
    """

    PENDING = "PENDING"

    PROCESSING = "PROCESSING"

    COMPLETED = "COMPLETED"

    FAILED = "FAILED"

    ALL = {
        PENDING,
        PROCESSING,
        COMPLETED,
        FAILED,
    }


@dataclass(frozen=True)
class PendingContent:
    """
    待 Embedding 的 Chunk 数据。

    与 PostgreSQL contents 表保持对应。
    """

    id: int

    document_id: str

    section_id: str

    content: str

    page_number: int | None

    chunk_index: int

    token_count: int


@dataclass(frozen=True)
class EmbeddingStatusCount:
    """
    Embedding 状态统计。
    """

    status: str

    count: int


class EmbeddingRepository:
    """
    Embedding PostgreSQL Repository。

    职责：

        - 查询 PENDING Chunk
        - 抢占任务并设置 PROCESSING
        - 更新 COMPLETED
        - 更新 FAILED
        - 重置状态
        - 查询任务统计

    不负责：

        - 调用 Embedding Model
        - 生成 Vector
        - 写 Qdrant
        - Retry 调度
        - Worker Loop

    这些职责分别属于：

        EmbeddingClient
        EmbeddingService
        EmbeddingWorker
        VectorStore
    """

    def __init__(
        self,
        database_connection: (
            DatabaseConnection | None
        ) = None,
    ) -> None:

        self.db = (
            database_connection
            or DatabaseConnection()
        )

    # ==================================================
    # Public Query API
    # ==================================================

    def get_pending_contents(
        self,
        *,
        limit: int = 100,
    ) -> list[PendingContent]:
        """
        查询 PENDING 状态 Chunk。

        注意：
            此方法只查询，不修改状态。

        更推荐 Worker 使用：

            claim_pending_contents()

        避免并发 Worker 重复处理同一 Chunk。
        """

        self._validate_limit(
            limit
        )

        sql = """
            SELECT
                id,
                document_id,
                section_id,
                content,
                page_number,
                chunk_index,
                COALESCE(token_count, 0)
            FROM contents
            WHERE embedding_status = %s
            ORDER BY id
            LIMIT %s
        """

        try:
            with self.db.connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        sql,
                        (
                            EmbeddingStatus.PENDING,
                            limit,
                        ),
                    )

                    rows = cur.fetchall()

        except Exception as exc:
            raise EmbeddingRepositoryError(
                "Failed to query pending "
                f"embedding contents: {exc}"
            ) from exc

        return [
            self._row_to_pending_content(
                row
            )
            for row in rows
        ]

    def claim_pending_contents(
        self,
        *,
        limit: int = 100,
    ) -> list[PendingContent]:
        """
        原子领取 PENDING Chunk。

        使用：

            FOR UPDATE SKIP LOCKED

        支持多个 Worker 并发运行时：

            Worker A
            Worker B
            Worker C

        不会领取到同一批 Chunk。

        领取成功后状态：

            PENDING
                ↓
            PROCESSING
        """

        self._validate_limit(
            limit
        )

        select_sql = """
            SELECT
                id,
                document_id,
                section_id,
                content,
                page_number,
                chunk_index,
                COALESCE(token_count, 0)
            FROM contents
            WHERE embedding_status = %s
            ORDER BY id
            FOR UPDATE SKIP LOCKED
            LIMIT %s
        """

        update_sql = """
            UPDATE contents
            SET
                embedding_status = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ANY(%s)
        """

        try:
            with self.db.connect() as conn:
                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            select_sql,
                            (
                                EmbeddingStatus.PENDING,
                                limit,
                            ),
                        )

                        rows = (
                            cur.fetchall()
                        )

                        if not rows:
                            conn.commit()

                            return []

                        content_ids = [
                            int(
                                row[0]
                            )
                            for row in rows
                        ]

                        cur.execute(
                            update_sql,
                            (
                                EmbeddingStatus.PROCESSING,
                                content_ids,
                            ),
                        )

                    conn.commit()

                except Exception:
                    conn.rollback()
                    raise

        except DatabaseConnectionError:
            raise

        except Exception as exc:
            raise EmbeddingRepositoryError(
                "Failed to claim pending "
                f"embedding contents: {exc}"
            ) from exc

        contents = [
            self._row_to_pending_content(
                row
            )
            for row in rows
        ]

        LOGGER.info(
            "Embedding contents claimed | "
            "count=%s",
            len(contents),
        )

        return contents

    # ==================================================
    # Status Update API
    # ==================================================

    def mark_completed(
        self,
        content_ids: Sequence[int],
    ) -> int:
        """
        将 Chunk 标记为 COMPLETED。

        Returns:
            实际更新行数。
        """

        return self._update_status(
            content_ids=content_ids,
            status=(
                EmbeddingStatus.COMPLETED
            ),
        )

    def mark_failed(
        self,
        content_ids: Sequence[int],
    ) -> int:
        """
        将 Chunk 标记为 FAILED。
        """

        return self._update_status(
            content_ids=content_ids,
            status=(
                EmbeddingStatus.FAILED
            ),
        )

    def mark_pending(
        self,
        content_ids: Sequence[int],
    ) -> int:
        """
        重置 Chunk 为 PENDING。

        用途：

            - Retry
            - 手工重跑
            - 模型升级重新向量化
        """

        return self._update_status(
            content_ids=content_ids,
            status=(
                EmbeddingStatus.PENDING
            ),
        )

    def mark_processing(
        self,
        content_ids: Sequence[int],
    ) -> int:
        """
        手工设置 PROCESSING。

        正常情况下 Worker 应优先使用：

            claim_pending_contents()
        """

        return self._update_status(
            content_ids=content_ids,
            status=(
                EmbeddingStatus.PROCESSING
            ),
        )

    # ==================================================
    # Recovery API
    # ==================================================

    def reset_stale_processing(
        self,
        *,
        older_than_minutes: int = 30,
    ) -> int:
        """
        将长时间停留在 PROCESSING 的任务
        重置为 PENDING。

        典型场景：

            Worker 获取任务
                ↓
            PROCESSING
                ↓
            Worker 崩溃
                ↓
            永久卡在 PROCESSING

        Recovery Job 可以调用此方法。
        """

        if older_than_minutes <= 0:
            raise (
                EmbeddingRepositoryValidationError(
                    "older_than_minutes must "
                    "be greater than 0."
                )
            )

        sql = """
            UPDATE contents
            SET
                embedding_status = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE embedding_status = %s
              AND updated_at
                  < CURRENT_TIMESTAMP
                    - (%s * INTERVAL '1 minute')
        """

        try:
            with self.db.connect() as conn:
                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            sql,
                            (
                                EmbeddingStatus.PENDING,
                                EmbeddingStatus.PROCESSING,
                                older_than_minutes,
                            ),
                        )

                        updated_count = (
                            cur.rowcount
                        )

                    conn.commit()

                except Exception:
                    conn.rollback()
                    raise

        except Exception as exc:
            raise EmbeddingRepositoryError(
                "Failed to reset stale "
                f"embedding tasks: {exc}"
            ) from exc

        if updated_count:
            LOGGER.warning(
                "Stale embedding tasks reset | "
                "count=%s | "
                "older_than_minutes=%s",
                updated_count,
                older_than_minutes,
            )

        return int(
            updated_count
        )

    def reset_failed(
        self,
        *,
        limit: int | None = None,
    ) -> int:
        """
        FAILED → PENDING。

        可用于人工重试。
        """

        if (
            limit is not None
            and limit <= 0
        ):
            raise (
                EmbeddingRepositoryValidationError(
                    "limit must be greater "
                    "than 0 when provided."
                )
            )

        if limit is None:
            sql = """
                UPDATE contents
                SET
                    embedding_status = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE embedding_status = %s
            """

            params = (
                EmbeddingStatus.PENDING,
                EmbeddingStatus.FAILED,
            )

        else:
            sql = """
                UPDATE contents
                SET
                    embedding_status = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id IN
                (
                    SELECT id
                    FROM contents
                    WHERE embedding_status = %s
                    ORDER BY id
                    LIMIT %s
                )
            """

            params = (
                EmbeddingStatus.PENDING,
                EmbeddingStatus.FAILED,
                limit,
            )

        try:
            with self.db.connect() as conn:
                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            sql,
                            params,
                        )

                        updated_count = (
                            cur.rowcount
                        )

                    conn.commit()

                except Exception:
                    conn.rollback()
                    raise

        except Exception as exc:
            raise EmbeddingRepositoryError(
                "Failed to reset failed "
                f"embedding tasks: {exc}"
            ) from exc

        return int(
            updated_count
        )

    # ==================================================
    # Statistics API
    # ==================================================

    def count_by_status(
        self,
    ) -> list[EmbeddingStatusCount]:
        """
        返回所有 Embedding 状态数量。
        """

        sql = """
            SELECT
                embedding_status,
                COUNT(*)
            FROM contents
            GROUP BY embedding_status
            ORDER BY embedding_status
        """

        try:
            with self.db.connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        sql
                    )

                    rows = cur.fetchall()

        except Exception as exc:
            raise EmbeddingRepositoryError(
                "Failed to query embedding "
                f"status statistics: {exc}"
            ) from exc

        return [
            EmbeddingStatusCount(
                status=str(
                    row[0]
                ),
                count=int(
                    row[1]
                ),
            )
            for row in rows
        ]

    def count_pending(
        self,
    ) -> int:
        """
        返回 PENDING Chunk 数量。
        """

        return self._count_status(
            EmbeddingStatus.PENDING
        )

    def count_processing(
        self,
    ) -> int:
        """
        返回 PROCESSING Chunk 数量。
        """

        return self._count_status(
            EmbeddingStatus.PROCESSING
        )

    def count_completed(
        self,
    ) -> int:
        """
        返回 COMPLETED Chunk 数量。
        """

        return self._count_status(
            EmbeddingStatus.COMPLETED
        )

    def count_failed(
        self,
    ) -> int:
        """
        返回 FAILED Chunk 数量。
        """

        return self._count_status(
            EmbeddingStatus.FAILED
        )

    # ==================================================
    # Document-level API
    # ==================================================

    def reset_document(
        self,
        document_id: str,
    ) -> int:
        """
        将指定文档的所有 Chunk 重置为 PENDING。

        用于：

            - 文档重新 Embedding
            - 模型升级
            - Vector DB 重建
        """

        normalized_document_id = (
            str(
                document_id
                or ""
            ).strip()
        )

        if not normalized_document_id:
            raise (
                EmbeddingRepositoryValidationError(
                    "document_id cannot "
                    "be empty."
                )
            )

        sql = """
            UPDATE contents
            SET
                embedding_status = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE document_id = %s
        """

        try:
            with self.db.connect() as conn:
                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            sql,
                            (
                                EmbeddingStatus.PENDING,
                                normalized_document_id,
                            ),
                        )

                        updated_count = (
                            cur.rowcount
                        )

                    conn.commit()

                except Exception:
                    conn.rollback()
                    raise

        except Exception as exc:
            raise EmbeddingRepositoryError(
                "Failed to reset embedding "
                "status for document "
                f"'{normalized_document_id}': "
                f"{exc}"
            ) from exc

        return int(
            updated_count
        )

    # ==================================================
    # Internal Update Helpers
    # ==================================================

    def _update_status(
        self,
        *,
        content_ids: Sequence[int],
        status: str,
    ) -> int:
        """
        批量更新 Embedding 状态。
        """

        normalized_ids = (
            self._normalize_content_ids(
                content_ids
            )
        )

        self._validate_status(
            status
        )

        if not normalized_ids:
            return 0

        sql = """
            UPDATE contents
            SET
                embedding_status = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ANY(%s)
        """

        try:
            with self.db.connect() as conn:
                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            sql,
                            (
                                status,
                                normalized_ids,
                            ),
                        )

                        updated_count = (
                            cur.rowcount
                        )

                    conn.commit()

                except Exception:
                    conn.rollback()
                    raise

        except Exception as exc:
            raise EmbeddingRepositoryError(
                "Failed to update embedding "
                f"status to '{status}': {exc}"
            ) from exc

        LOGGER.debug(
            "Embedding status updated | "
            "status=%s | "
            "requested=%s | "
            "updated=%s",
            status,
            len(normalized_ids),
            updated_count,
        )

        return int(
            updated_count
        )

    def _count_status(
        self,
        status: str,
    ) -> int:
        """
        查询单一状态数量。
        """

        self._validate_status(
            status
        )

        sql = """
            SELECT COUNT(*)
            FROM contents
            WHERE embedding_status = %s
        """

        try:
            with self.db.connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        sql,
                        (
                            status,
                        ),
                    )

                    row = (
                        cur.fetchone()
                    )

        except Exception as exc:
            raise EmbeddingRepositoryError(
                "Failed to count embedding "
                f"status '{status}': {exc}"
            ) from exc

        if not row:
            return 0

        return int(
            row[0]
        )

    # ==================================================
    # Validation
    # ==================================================

    @staticmethod
    def _validate_limit(
        limit: int,
    ) -> None:

        if not isinstance(
            limit,
            int,
        ):
            raise (
                EmbeddingRepositoryValidationError(
                    "limit must be integer."
                )
            )

        if limit <= 0:
            raise (
                EmbeddingRepositoryValidationError(
                    "limit must be greater "
                    "than 0."
                )
            )

        if limit > 10000:
            raise (
                EmbeddingRepositoryValidationError(
                    "limit cannot exceed "
                    "10000."
                )
            )

    @staticmethod
    def _validate_status(
        status: str,
    ) -> None:

        if status not in (
            EmbeddingStatus.ALL
        ):
            raise (
                EmbeddingRepositoryValidationError(
                    "Invalid embedding status: "
                    f"{status}. "
                    "Supported values: "
                    + ", ".join(
                        sorted(
                            EmbeddingStatus.ALL
                        )
                    )
                )
            )

    @staticmethod
    def _normalize_content_ids(
        content_ids: Sequence[int],
    ) -> list[int]:
        """
        规范化并去重 content_id。
        """

        if content_ids is None:
            raise (
                EmbeddingRepositoryValidationError(
                    "content_ids cannot "
                    "be None."
                )
            )

        normalized_ids: list[int] = []

        seen: set[int] = set()

        for raw_id in content_ids:
            try:
                content_id = int(
                    raw_id
                )

            except (
                TypeError,
                ValueError,
            ) as exc:
                raise (
                    EmbeddingRepositoryValidationError(
                        "content_id must "
                        f"be integer: {raw_id}"
                    )
                ) from exc

            if content_id <= 0:
                raise (
                    EmbeddingRepositoryValidationError(
                        "content_id must be "
                        f"greater than 0: "
                        f"{content_id}"
                    )
                )

            if content_id in seen:
                continue

            seen.add(
                content_id
            )

            normalized_ids.append(
                content_id
            )

        return normalized_ids

    @staticmethod
    def _row_to_pending_content(
        row,
    ) -> PendingContent:
        """
        PostgreSQL Row →
        PendingContent。
        """

        return PendingContent(
            id=int(
                row[0]
            ),
            document_id=str(
                row[1]
            ),
            section_id=str(
                row[2]
            ),
            content=str(
                row[3]
            ),
            page_number=(
                int(
                    row[4]
                )
                if row[4] is not None
                else None
            ),
            chunk_index=(
                int(
                    row[5]
                    or 0
                )
            ),
            token_count=(
                int(
                    row[6]
                    or 0
                )
            ),
        )