from __future__ import annotations

import logging
import math
from typing import Sequence
from uuid import UUID

from qdrant_client import (
    QdrantClient,
)

from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    FilterSelector,
    MatchValue,
    PointStruct,
    VectorParams,
)

from app.vector.base import (
    VectorRecord,
    VectorSearchResult,
    VectorStore,
    VectorStoreError,
    VectorStoreValidationError,
)


LOGGER = logging.getLogger(
    "document_ingestion.vector.qdrant"
)


class QdrantVectorStoreError(
    VectorStoreError
):
    """
    Qdrant Vector Store 异常。
    """


class QdrantVectorStore(
    VectorStore
):
    """
    Qdrant Vector Store 实现。

    当前设计：

        PostgreSQL
            =
        Source of Truth

        Qdrant
            =
        Vector Search Index

    Qdrant Payload 保存：

        content_id
        document_id
        section_id
        page_number
        chunk_index
        token_count
        model_name

    完整正文仍然以 PostgreSQL contents 为准。
    """

    def __init__(
        self,
        *,
        url: str = "http://127.0.0.1:6333",
        collection_name: str = "document_chunks",
        dimension: int = 1024,
        api_key: str | None = None,
        timeout: int = 30,
    ) -> None:

        self.url = self._normalize_required_string(
            url,
            field_name="url",
        )

        self.collection_name = (
            self._normalize_required_string(
                collection_name,
                field_name="collection_name",
            )
        )

        self.dimension = (
            self._normalize_positive_int(
                dimension,
                field_name="dimension",
            )
        )

        self.timeout = (
            self._normalize_positive_int(
                timeout,
                field_name="timeout",
            )
        )

        try:
            self.client = (
                QdrantClient(
                    url=self.url,
                    api_key=api_key,
                    timeout=self.timeout,
                )
            )

        except Exception as exc:
            raise QdrantVectorStoreError(
                "Failed to initialize "
                f"Qdrant client: {exc}"
            ) from exc

    # ==================================================
    # Collection
    # ==================================================

    def ensure_collection(
        self,
    ) -> None:
        """
        Collection 不存在时创建。

        当前使用：
            COSINE

        BGE-M3 Dense Embedding：
            1024 dimensions
        """

        try:
            exists = (
                self.client
                .collection_exists(
                    collection_name=(
                        self.collection_name
                    )
                )
            )

            if exists:
                self._validate_collection()

                return

            self.client.create_collection(
                collection_name=(
                    self.collection_name
                ),
                vectors_config=VectorParams(
                    size=self.dimension,
                    distance=Distance.COSINE,
                ),
            )

            LOGGER.info(
                "Qdrant collection created | "
                "collection=%s | "
                "dimension=%s",
                self.collection_name,
                self.dimension,
            )

        except QdrantVectorStoreError:
            raise

        except Exception as exc:
            raise QdrantVectorStoreError(
                "Failed to ensure Qdrant "
                f"collection "
                f"'{self.collection_name}': "
                f"{exc}"
            ) from exc

    # ==================================================
    # Upsert
    # ==================================================

    def upsert(
        self,
        records: Sequence[
            VectorRecord
        ],
    ) -> int:
        """
        批量 Upsert。

        同一 Point ID：
            Update

        新 Point ID：
            Insert
        """

        normalized_records = (
            self._validate_records(
                records
            )
        )

        if not normalized_records:
            return 0

        points = [
            PointStruct(
                id=record.id,
                vector=record.vector,
                payload=record.payload,
            )
            for record in normalized_records
        ]

        try:
            self.client.upsert(
                collection_name=(
                    self.collection_name
                ),
                points=points,
                wait=True,
            )

        except Exception as exc:
            raise QdrantVectorStoreError(
                "Failed to upsert vectors "
                f"into collection "
                f"'{self.collection_name}': "
                f"{exc}"
            ) from exc

        LOGGER.info(
            "Qdrant vectors upserted | "
            "collection=%s | "
            "count=%s",
            self.collection_name,
            len(points),
        )

        return len(
            points
        )

    # ==================================================
    # Search
    # ==================================================

    def search(
        self,
        query_vector: Sequence[float],
        *,
        limit: int = 10,
        document_id: str | None = None,
    ) -> list[VectorSearchResult]:
        """
        Cosine Similarity Search。

        可选：

            document_id

        用于只在某个文档范围内检索。
        """

        vector = self._validate_vector(
            query_vector
        )

        if limit <= 0:
            raise VectorStoreValidationError(
                "limit must be greater than 0."
            )

        query_filter = None

        if document_id is not None:
            normalized_document_id = (
                self._normalize_required_string(
                    document_id,
                    field_name="document_id",
                )
            )

            query_filter = Filter(
                must=[
                    FieldCondition(
                        key="document_id",
                        match=MatchValue(
                            value=(
                                normalized_document_id
                            )
                        ),
                    )
                ]
            )

        try:
            response = (
                self.client
                .query_points(
                    collection_name=(
                        self.collection_name
                    ),
                    query=vector,
                    query_filter=query_filter,
                    limit=limit,
                    with_payload=True,
                    with_vectors=False,
                )
            )

        except Exception as exc:
            raise QdrantVectorStoreError(
                "Failed to search Qdrant "
                f"collection "
                f"'{self.collection_name}': "
                f"{exc}"
            ) from exc

        results: list[
            VectorSearchResult
        ] = []

        for point in response.points:
            results.append(
                VectorSearchResult(
                    id=point.id,
                    score=float(
                        point.score
                    ),
                    payload=dict(
                        point.payload
                        or {}
                    ),
                )
            )

        return results

    # ==================================================
    # Delete
    # ==================================================

    def delete_by_document(
        self,
        document_id: str,
    ) -> None:

        normalized_document_id = (
            self._normalize_required_string(
                document_id,
                field_name="document_id",
            )
        )

        try:
            self.client.delete(
                collection_name=(
                    self.collection_name
                ),
                points_selector=FilterSelector(
                    filter=Filter(
                        must=[
                            FieldCondition(
                                key="document_id",
                                match=MatchValue(
                                    value=(
                                        normalized_document_id
                                    )
                                ),
                            )
                        ]
                    )
                ),
                wait=True,
            )

        except Exception as exc:
            raise QdrantVectorStoreError(
                "Failed to delete vectors "
                f"for document "
                f"'{normalized_document_id}': "
                f"{exc}"
            ) from exc

        LOGGER.info(
            "Qdrant document vectors deleted | "
            "document_id=%s",
            normalized_document_id,
        )

    # ==================================================
    # Count
    # ==================================================

    def count(
        self,
    ) -> int:

        try:
            result = (
                self.client.count(
                    collection_name=(
                        self.collection_name
                    ),
                    exact=True,
                )
            )

        except Exception as exc:
            raise QdrantVectorStoreError(
                "Failed to count Qdrant "
                f"vectors: {exc}"
            ) from exc

        return int(
            result.count
        )

    # ==================================================
    # Collection Validation
    # ==================================================

    def _validate_collection(
        self,
    ) -> None:
        """
        验证已存在 Collection 与当前配置兼容。

        当前 QdrantVectorStore 使用：

            unnamed dense vector
            COSINE distance
            self.dimension dimensions

        如果 Collection 已存在但配置不一致，
        禁止继续写入或检索。
        """

        try:
            info = (
                self.client
                .get_collection(
                    collection_name=(
                        self.collection_name
                    )
                )
            )

        except Exception as exc:
            raise QdrantVectorStoreError(
                "Failed to inspect Qdrant "
                f"collection: {exc}"
            ) from exc

        vectors_config = (
            info.config
            .params
            .vectors
        )

        # 当前实现只支持单个 unnamed dense vector。
        #
        # Named vectors 的 Qdrant 配置通常表现为：
        #
        #     dict[str, VectorParams]
        #
        # 不能误当作单 VectorParams 使用。
        if isinstance(
            vectors_config,
            dict,
        ):
            raise QdrantVectorStoreError(
                "Qdrant collection uses named vectors, "
                "but QdrantVectorStore currently expects "
                "a single unnamed dense vector. "
                f"collection={self.collection_name}"
            )

        actual_dimension = getattr(
            vectors_config,
            "size",
            None,
        )

        if actual_dimension is None:
            raise QdrantVectorStoreError(
                "Unable to determine Qdrant collection "
                "vector dimension. "
                f"collection={self.collection_name}"
            )

        if int(
            actual_dimension
        ) != self.dimension:
            raise QdrantVectorStoreError(
                "Qdrant collection vector "
                "dimension mismatch. "
                f"collection="
                f"{self.collection_name}, "
                f"expected={self.dimension}, "
                f"actual={actual_dimension}"
            )

        actual_distance = getattr(
            vectors_config,
            "distance",
            None,
        )

        if actual_distance is None:
            raise QdrantVectorStoreError(
                "Unable to determine Qdrant collection "
                "distance metric. "
                f"collection={self.collection_name}"
            )

        if not self._is_cosine_distance(
            actual_distance
        ):
            raise QdrantVectorStoreError(
                "Qdrant collection distance mismatch. "
                f"collection={self.collection_name}, "
                f"expected={Distance.COSINE}, "
                f"actual={actual_distance}"
            )

    # ==================================================
    # Validation
    # ==================================================

    def _validate_records(
        self,
        records: Sequence[
            VectorRecord
        ],
    ) -> list[VectorRecord]:

        if records is None:
            raise VectorStoreValidationError(
                "records cannot be None."
            )

        if isinstance(
            records,
            (str, bytes),
        ):
            raise VectorStoreValidationError(
                "records must be a sequence "
                "of VectorRecord objects."
            )

        normalized: list[
            VectorRecord
        ] = []

        seen_ids: set[
            int | str
        ] = set()

        for index, record in enumerate(
            records
        ):
            if not isinstance(
                record,
                VectorRecord,
            ):
                raise VectorStoreValidationError(
                    "Invalid VectorRecord "
                    f"at index {index}."
                )

            normalized_id = (
                self._validate_point_id(
                    record.id
                )
            )

            if normalized_id in seen_ids:
                raise VectorStoreValidationError(
                    "Duplicate VectorRecord id "
                    f"in the same upsert batch: "
                    f"{normalized_id}"
                )

            normalized_vector = (
                self._validate_vector(
                    record.vector
                )
            )

            if not isinstance(
                record.payload,
                dict,
            ):
                raise VectorStoreValidationError(
                    "VectorRecord payload "
                    "must be dict."
                )

            seen_ids.add(
                normalized_id
            )

            # VectorRecord 是 frozen dataclass。
            #
            # 如果 ID 或 vector 经规范化，
            # 创建新的标准记录，不修改调用方对象。
            normalized.append(
                VectorRecord(
                    id=normalized_id,
                    vector=normalized_vector,
                    payload=dict(
                        record.payload
                    ),
                )
            )

        return normalized

    def _validate_vector(
        self,
        vector: Sequence[float],
    ) -> list[float]:

        if vector is None:
            raise VectorStoreValidationError(
                "vector cannot be None."
            )

        if isinstance(
            vector,
            (str, bytes),
        ):
            raise VectorStoreValidationError(
                "vector must be a sequence "
                "of numeric values."
            )

        try:
            normalized = [
                float(value)
                for value in vector
            ]

        except (
            TypeError,
            ValueError,
        ) as exc:
            raise VectorStoreValidationError(
                "vector must contain "
                "numeric values."
            ) from exc

        if (
            len(normalized)
            != self.dimension
        ):
            raise VectorStoreValidationError(
                "Vector dimension mismatch. "
                f"expected={self.dimension}, "
                f"actual={len(normalized)}"
            )

        if not all(
            math.isfinite(value)
            for value in normalized
        ):
            raise VectorStoreValidationError(
                "vector cannot contain "
                "NaN or infinite values."
            )

        return normalized

    @staticmethod
    def _validate_point_id(
        point_id: int | str,
    ) -> int | str:
        """
        验证 Qdrant Point ID。

        Qdrant Point ID 使用：

            unsigned 64-bit integer
            或 UUID string

        不接受任意业务字符串。

        EmbeddingRecord / Content 的业务 ID
        如果不是 UUID，需要在 Adapter 层转换为稳定 UUID，
        不在 VectorStore 中静默重写。
        """

        if isinstance(
            point_id,
            bool,
        ):
            raise VectorStoreValidationError(
                "VectorRecord id cannot be bool."
            )

        if isinstance(
            point_id,
            int,
        ):
            if (
                point_id < 0
                or point_id
                > 18_446_744_073_709_551_615
            ):
                raise VectorStoreValidationError(
                    "Integer VectorRecord id must be "
                    "within unsigned 64-bit range."
                )

            return point_id

        if isinstance(
            point_id,
            str,
        ):
            normalized = point_id.strip()

            if not normalized:
                raise VectorStoreValidationError(
                    "VectorRecord id cannot be empty."
                )

            try:
                parsed = UUID(
                    normalized
                )

            except ValueError as exc:
                raise VectorStoreValidationError(
                    "String VectorRecord id must be "
                    "a valid UUID for Qdrant. "
                    f"Received: {normalized!r}"
                ) from exc

            return str(
                parsed
            )

        raise VectorStoreValidationError(
            "VectorRecord id must be "
            "an int or UUID string."
        )

    @staticmethod
    def _is_cosine_distance(
        distance: object,
    ) -> bool:
        """
        兼容 Qdrant enum / string 表示。
        """

        if distance == Distance.COSINE:
            return True

        value = getattr(
            distance,
            "value",
            distance,
        )

        return str(
            value
        ).strip().lower() == "cosine"

    @staticmethod
    def _normalize_positive_int(
        value: int,
        *,
        field_name: str,
    ) -> int:
        """
        严格校验正整数配置。

        防止：

            1024.5 -> int(...) -> 1024

        这类静默截断。
        """

        if (
            isinstance(value, bool)
            or not isinstance(
                value,
                int,
            )
        ):
            raise VectorStoreValidationError(
                f"{field_name} must be an integer."
            )

        if value <= 0:
            raise VectorStoreValidationError(
                f"{field_name} must be greater than 0."
            )

        return value

    @staticmethod
    def _normalize_required_string(
        value: str,
        *,
        field_name: str,
    ) -> str:

        normalized = str(
            value
            or ""
        ).strip()

        if not normalized:
            raise VectorStoreValidationError(
                f"{field_name} "
                "cannot be empty."
            )

        return normalized