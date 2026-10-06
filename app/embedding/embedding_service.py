from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Sequence

from app.embedding.embedding_client import (
    EmbeddingClient,
    EmbeddingClientError,
)

from app.embedding.embedding_repository import (
    EmbeddingRepository,
    EmbeddingRepositoryError,
    PendingContent,
)


LOGGER = logging.getLogger(
    "document_ingestion.embedding.service"
)


class EmbeddingServiceError(
    RuntimeError
):
    """
    Embedding Service 基础异常。
    """


class EmbeddingServiceValidationError(
    EmbeddingServiceError
):
    """
    Service 参数校验异常。
    """


@dataclass(frozen=True)
class EmbeddingRecord:
    """
    已生成向量的 Chunk。

    这是后续 VectorStore 的标准输入对象。

    Attributes:
        content_id:
            PostgreSQL contents.id

        document_id:
            文档 ID

        section_id:
            Section ID

        page_number:
            页码 / Slide / Sheet 对应的统一定位字段

        chunk_index:
            Chunk Index

        token_count:
            Token 数量

        text:
            Chunk 原文

        vector:
            Dense Embedding

        dimension:
            向量维度

        model_name:
            Embedding 模型名称
    """

    content_id: int

    document_id: str

    section_id: str

    page_number: int | None

    chunk_index: int

    token_count: int

    text: str

    vector: list[float]

    dimension: int

    model_name: str


@dataclass(frozen=True)
class EmbeddingBatchResult:
    """
    一次 Batch Embedding 的结果。
    """

    records: list[EmbeddingRecord]

    requested_count: int

    generated_count: int

    model_name: str

    dimension: int


class EmbeddingService:
    """
    Embedding 业务服务。

    职责：

        PostgreSQL contents
            ↓
        EmbeddingRepository
            ↓
        PendingContent
            ↓
        EmbeddingClient
            ↓
        Dense Vector
            ↓
        EmbeddingRecord

    不负责：

        - Qdrant 写入
        - pgvector 写入
        - embedding_status 更新
        - Worker 循环
        - Retry 调度

    这些职责后续分别由：

        VectorStore
        EmbeddingWorker

    承担。
    """

    def __init__(
        self,
        *,
        client: EmbeddingClient | None = None,
        repository: EmbeddingRepository | None = None,
    ) -> None:

        self.client = (
            client
            or EmbeddingClient()
        )

        self.repository = (
            repository
            or EmbeddingRepository()
        )

    # ==================================================
    # Public API
    # ==================================================

    def embed_pending(
        self,
        *,
        limit: int = 32,
    ) -> EmbeddingBatchResult:
        """
        从 PostgreSQL 读取 PENDING Chunk，
        批量生成 Embedding。

        注意：
            此方法当前只读取，不修改数据库状态。

        Args:
            limit:
                一次处理最大 Chunk 数。

        Returns:
            EmbeddingBatchResult
        """

        self._validate_limit(
            limit
        )

        try:
            contents = (
                self.repository
                .get_pending_contents(
                    limit=limit
                )
            )

        except EmbeddingRepositoryError as exc:
            raise EmbeddingServiceError(
                "Failed to load pending "
                f"contents: {exc}"
            ) from exc

        if not contents:
            return EmbeddingBatchResult(
                records=[],
                requested_count=0,
                generated_count=0,
                model_name=(
                    self.client
                    .get_model_name()
                ),
                dimension=(
                    self.client
                    .get_dimension()
                ),
            )

        return self.embed_contents(
            contents
        )

    def embed_contents(
        self,
        contents: Sequence[PendingContent],
    ) -> EmbeddingBatchResult:
        """
        对指定 PendingContent 集合生成 Embedding。

        这是 Service 的核心方法。

        Args:
            contents:
                Repository 返回的 Chunk。

        Returns:
            EmbeddingBatchResult
        """

        normalized_contents = (
            self._validate_contents(
                contents
            )
        )

        if not normalized_contents:
            return EmbeddingBatchResult(
                records=[],
                requested_count=0,
                generated_count=0,
                model_name=(
                    self.client
                    .get_model_name()
                ),
                dimension=(
                    self.client
                    .get_dimension()
                ),
            )

        texts = [
            content.content
            for content in normalized_contents
        ]

        try:
            vectors = (
                self.client
                .embed_texts(
                    texts
                )
            )

        except EmbeddingClientError as exc:
            raise EmbeddingServiceError(
                "Failed to generate "
                f"embeddings: {exc}"
            ) from exc

        if (
            len(vectors)
            != len(normalized_contents)
        ):
            raise EmbeddingServiceError(
                "Embedding count mismatch. "
                f"contents={len(normalized_contents)}, "
                f"vectors={len(vectors)}"
            )

        model_name = (
            self.client
            .get_model_name()
        )

        dimension = (
            self.client
            .get_dimension()
        )

        records: list[
            EmbeddingRecord
        ] = []

        for content, vector in zip(
            normalized_contents,
            vectors,
            strict=True,
        ):
            if len(vector) != dimension:
                raise EmbeddingServiceError(
                    "Embedding dimension mismatch. "
                    f"content_id={content.id}, "
                    f"expected={dimension}, "
                    f"actual={len(vector)}"
                )

            record = EmbeddingRecord(
                content_id=content.id,
                document_id=content.document_id,
                section_id=content.section_id,
                page_number=content.page_number,
                chunk_index=content.chunk_index,
                token_count=content.token_count,
                text=content.content,
                vector=vector,
                dimension=dimension,
                model_name=model_name,
            )

            records.append(
                record
            )

        LOGGER.info(
            "Embedding batch generated | "
            "requested=%s | "
            "generated=%s | "
            "model=%s | "
            "dimension=%s",
            len(normalized_contents),
            len(records),
            model_name,
            dimension,
        )

        return EmbeddingBatchResult(
            records=records,
            requested_count=len(
                normalized_contents
            ),
            generated_count=len(
                records
            ),
            model_name=model_name,
            dimension=dimension,
        )

    def embed_single(
        self,
        content: PendingContent,
    ) -> EmbeddingRecord:
        """
        对单条 Chunk 生成 Embedding。

        主要用于：
            - Debug
            - 单条重试
            - 单元测试

        Worker 正常运行时仍推荐 Batch。
        """

        result = self.embed_contents(
            [
                content
            ]
        )

        if len(
            result.records
        ) != 1:
            raise EmbeddingServiceError(
                "Expected exactly one "
                "embedding result."
            )

        return result.records[0]

    def preview_pending(
        self,
        *,
        limit: int = 5,
    ) -> list[PendingContent]:
        """
        预览待处理 Chunk。

        不生成 Embedding。
        不修改状态。
        """

        self._validate_limit(
            limit
        )

        try:
            return (
                self.repository
                .get_pending_contents(
                    limit=limit
                )
            )

        except EmbeddingRepositoryError as exc:
            raise EmbeddingServiceError(
                "Failed to preview pending "
                f"contents: {exc}"
            ) from exc

    def get_status_summary(
        self,
    ) -> dict[str, int]:
        """
        获取当前 Embedding 状态统计。
        """

        try:
            counts = (
                self.repository
                .count_by_status()
            )

        except EmbeddingRepositoryError as exc:
            raise EmbeddingServiceError(
                "Failed to load embedding "
                f"status summary: {exc}"
            ) from exc

        return {
            item.status: item.count
            for item in counts
        }

    def warmup(
        self,
    ) -> None:
        """
        主动加载 Embedding 模型。
        """

        try:
            self.client.warmup()

        except EmbeddingClientError as exc:
            raise EmbeddingServiceError(
                "Embedding warmup failed: "
                f"{exc}"
            ) from exc

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
                EmbeddingServiceValidationError(
                    "limit must be integer."
                )
            )

        if limit <= 0:
            raise (
                EmbeddingServiceValidationError(
                    "limit must be greater "
                    "than 0."
                )
            )

        if limit > 10000:
            raise (
                EmbeddingServiceValidationError(
                    "limit cannot exceed "
                    "10000."
                )
            )

    @staticmethod
    def _validate_contents(
        contents: Sequence[PendingContent],
    ) -> list[PendingContent]:

        if contents is None:
            raise (
                EmbeddingServiceValidationError(
                    "contents cannot be None."
                )
            )

        if isinstance(
            contents,
            PendingContent,
        ):
            raise (
                EmbeddingServiceValidationError(
                    "embed_contents() expects "
                    "a sequence of "
                    "PendingContent."
                )
            )

        normalized: list[
            PendingContent
        ] = []

        seen_ids: set[int] = set()

        for index, content in enumerate(
            contents
        ):
            if not isinstance(
                content,
                PendingContent,
            ):
                raise (
                    EmbeddingServiceValidationError(
                        "Invalid content type "
                        f"at index {index}: "
                        f"{type(content).__name__}"
                    )
                )

            if content.id <= 0:
                raise (
                    EmbeddingServiceValidationError(
                        "content.id must be "
                        "greater than 0. "
                        f"Received: {content.id}"
                    )
                )

            if not str(
                content.document_id
                or ""
            ).strip():
                raise (
                    EmbeddingServiceValidationError(
                        "document_id cannot "
                        "be empty. "
                        f"content_id={content.id}"
                    )
                )

            if not str(
                content.section_id
                or ""
            ).strip():
                raise (
                    EmbeddingServiceValidationError(
                        "section_id cannot "
                        "be empty. "
                        f"content_id={content.id}"
                    )
                )

            if not str(
                content.content
                or ""
            ).strip():
                raise (
                    EmbeddingServiceValidationError(
                        "content text cannot "
                        "be empty. "
                        f"content_id={content.id}"
                    )
                )

            if content.id in seen_ids:
                continue

            seen_ids.add(
                content.id
            )

            normalized.append(
                content
            )

        return normalized