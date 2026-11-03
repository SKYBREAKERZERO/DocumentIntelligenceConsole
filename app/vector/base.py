from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Sequence


class VectorStoreError(RuntimeError):
    """
    Vector Store 基础异常。
    """


class VectorStoreValidationError(
    VectorStoreError
):
    """
    Vector Store 参数校验异常。
    """


@dataclass(frozen=True)
class VectorRecord:
    """
    标准向量记录。

    Attributes:
        id:
            Vector Store Point ID。

        vector:
            Dense Vector。

        payload:
            向量关联 Metadata。
    """

    id: int | str

    vector: list[float]

    payload: dict[str, Any]


@dataclass(frozen=True)
class VectorSearchResult:
    """
    向量检索结果。
    """

    id: int | str

    score: float

    payload: dict[str, Any]


class VectorStore(
    ABC
):
    """
    Vector Store 抽象接口。

    上层：

        EmbeddingWorker
        RAG Search

    不需要知道后端到底是：

        Qdrant
        pgvector
        Milvus
        Pinecone
    """

    @abstractmethod
    def ensure_collection(
        self,
    ) -> None:
        """
        确保 Collection / Index 存在。
        """

        raise NotImplementedError

    @abstractmethod
    def upsert(
        self,
        records: Sequence[
            VectorRecord
        ],
    ) -> int:
        """
        插入或更新 Vector。

        Returns:
            实际提交数量。
        """

        raise NotImplementedError

    @abstractmethod
    def search(
        self,
        query_vector: Sequence[float],
        *,
        limit: int = 10,
        document_id: str | None = None,
    ) -> list[VectorSearchResult]:
        """
        Vector Similarity Search。
        """

        raise NotImplementedError

    @abstractmethod
    def delete_by_document(
        self,
        document_id: str,
    ) -> None:
        """
        删除指定文档对应的 Vector。
        """

        raise NotImplementedError

    @abstractmethod
    def count(
        self,
    ) -> int:
        """
        返回当前 Vector 数量。
        """

        raise NotImplementedError