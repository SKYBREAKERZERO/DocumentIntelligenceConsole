from __future__ import annotations

import logging
from dataclasses import dataclass
from threading import RLock
from typing import Sequence

import numpy as np
from sentence_transformers import SentenceTransformer


LOGGER = logging.getLogger(
    "document_ingestion.embedding"
)


class EmbeddingClientError(RuntimeError):
    """
    Embedding Client 基础异常。
    """


class EmbeddingModelLoadError(
    EmbeddingClientError
):
    """
    Embedding 模型加载失败。
    """


class EmbeddingGenerationError(
    EmbeddingClientError
):
    """
    Embedding 生成失败。
    """


class EmbeddingValidationError(
    EmbeddingClientError
):
    """
    Embedding 输入或输出校验失败。
    """


@dataclass(frozen=True)
class EmbeddingClientConfig:
    """
    Embedding Client 配置。

    Attributes:
        model_name:
            Hugging Face / SentenceTransformer 模型名。

        device:
            运行设备。

            示例：
                cpu
                cuda
                cuda:0

            None:
                由 sentence-transformers 自动决定。

        batch_size:
            批量 Embedding 数量。

        normalize_embeddings:
            是否执行 L2 Normalization。

            对后续 cosine similarity 检索推荐开启。

        expected_dimension:
            预期向量维度。

            BAAI/bge-m3 dense embedding 为 1024。

        show_progress_bar:
            是否显示模型 encode 进度条。

            Worker / 服务环境建议 False。

        trust_remote_code:
            是否允许执行模型仓库中的自定义代码。

            企业环境默认关闭。
    """

    model_name: str = "BAAI/bge-m3"

    device: str | None = None

    batch_size: int = 32

    normalize_embeddings: bool = True

    expected_dimension: int = 1024

    show_progress_bar: bool = False

    trust_remote_code: bool = False


@dataclass(frozen=True)
class EmbeddingResult:
    """
    单条 Embedding 结果。
    """

    vector: list[float]

    dimension: int

    model_name: str


class EmbeddingClient:
    """
    Dense Embedding Client。

    当前职责：

        text
          ↓
        SentenceTransformer
          ↓
        Dense Vector

    不负责：

        - PostgreSQL 查询
        - embedding_status 更新
        - Qdrant 写入
        - pgvector 写入
        - Chunk 获取
        - Worker 调度

    这些职责由：

        EmbeddingRepository
        EmbeddingService
        EmbeddingWorker
        VectorStore

    分别承担。
    """

    def __init__(
        self,
        config: EmbeddingClientConfig | None = None,
    ) -> None:

        self.config = (
            config
            or EmbeddingClientConfig()
        )

        self._validate_config()

        self._model: (
            SentenceTransformer | None
        ) = None

        self._model_lock = RLock()

    # ==================================================
    # Public API
    # ==================================================

    def embed_text(
        self,
        text: str,
    ) -> EmbeddingResult:
        """
        对单个文本生成 Dense Embedding。

        Args:
            text:
                Chunk 正文。

        Returns:
            EmbeddingResult
        """

        normalized_text = (
            self._normalize_text(
                text
            )
        )

        vectors = self.embed_texts(
            [
                normalized_text
            ]
        )

        if len(vectors) != 1:
            raise EmbeddingGenerationError(
                "Embedding model returned "
                "unexpected result count "
                "for single text."
            )

        return EmbeddingResult(
            vector=vectors[0],
            dimension=len(
                vectors[0]
            ),
            model_name=(
                self.config.model_name
            ),
        )

    def embed_texts(
        self,
        texts: Sequence[str],
    ) -> list[list[float]]:
        """
        批量生成 Dense Embedding。

        Args:
            texts:
                Chunk 文本集合。

        Returns:
            list[list[float]]

        Notes:
            这是 EmbeddingWorker 应优先调用的方法。

            不建议 Worker：

                for chunk:
                    embed_text()

            推荐：

                batch
                    ↓
                embed_texts()
                    ↓
                model.encode(batch)

            GPU / CPU 吞吐都会更高。
        """

        normalized_texts = (
            self._normalize_texts(
                texts
            )
        )

        if not normalized_texts:
            return []

        model = self._get_model()

        try:
            embeddings = model.encode(
                normalized_texts,
                batch_size=(
                    self.config.batch_size
                ),
                show_progress_bar=(
                    self.config
                    .show_progress_bar
                ),
                convert_to_numpy=True,
                normalize_embeddings=(
                    self.config
                    .normalize_embeddings
                ),
            )

        except Exception as exc:
            raise EmbeddingGenerationError(
                "Failed to generate "
                "embeddings using model "
                f"'{self.config.model_name}': "
                f"{exc}"
            ) from exc

        vectors = (
            self._convert_embeddings(
                embeddings
            )
        )

        self._validate_result_count(
            input_count=len(
                normalized_texts
            ),
            output_count=len(
                vectors
            ),
        )

        for vector in vectors:
            self._validate_vector(
                vector
            )

        return vectors

    def warmup(
        self,
    ) -> None:
        """
        主动加载模型。

        默认模型采用 lazy loading。

        Worker 启动时可提前：

            client.warmup()

        避免第一个 Batch 才发生模型加载。
        """

        self._get_model()

        LOGGER.info(
            "Embedding model warmed up | "
            "model=%s | "
            "dimension=%s",
            self.config.model_name,
            self.config.expected_dimension,
        )

    def get_dimension(
        self,
    ) -> int:
        """
        返回当前 Embedding 维度。
        """

        return (
            self.config.expected_dimension
        )

    def get_model_name(
        self,
    ) -> str:
        """
        返回模型名称。
        """

        return self.config.model_name

    def get_model_info(
        self,
    ) -> dict[str, object]:
        """
        返回安全模型信息。

        可用于：
            - 启动日志
            - Health Check
            - Debug
            - Worker metadata
        """

        return {
            "model_name": (
                self.config.model_name
            ),
            "device": (
                self.config.device
                or "auto"
            ),
            "batch_size": (
                self.config.batch_size
            ),
            "dimension": (
                self.config
                .expected_dimension
            ),
            "normalize_embeddings": (
                self.config
                .normalize_embeddings
            ),
            "loaded": (
                self._model is not None
            ),
        }

    # ==================================================
    # Model Lifecycle
    # ==================================================

    def _get_model(
        self,
    ) -> SentenceTransformer:
        """
        Lazy Loading + Thread-safe 初始化。

        模型只加载一次。
        """

        if self._model is not None:
            return self._model

        with self._model_lock:
            if self._model is not None:
                return self._model

            self._model = (
                self._load_model()
            )

            return self._model

    def _load_model(
        self,
    ) -> SentenceTransformer:
        """
        加载 SentenceTransformer 模型。
        """

        LOGGER.info(
            "Loading embedding model | "
            "model=%s | "
            "device=%s",
            self.config.model_name,
            (
                self.config.device
                or "auto"
            ),
        )

        try:
            model = SentenceTransformer(
                self.config.model_name,
                device=self.config.device,
                trust_remote_code=(
                    self.config
                    .trust_remote_code
                ),
            )

        except Exception as exc:
            raise EmbeddingModelLoadError(
                "Failed to load embedding "
                f"model "
                f"'{self.config.model_name}': "
                f"{exc}"
            ) from exc

        try:
            dimension = (
                model
                .get_embedding_dimension()
            )

        except Exception as exc:
            raise EmbeddingModelLoadError(
                "Failed to determine "
                "embedding dimension for "
                f"model "
                f"'{self.config.model_name}': "
                f"{exc}"
            ) from exc

        if dimension is None:
            raise EmbeddingModelLoadError(
                "Embedding model did not "
                "report its output dimension."
            )

        if (
            int(dimension)
            != self.config.expected_dimension
        ):
            raise EmbeddingModelLoadError(
                "Embedding dimension mismatch. "
                f"model={self.config.model_name}, "
                f"expected="
                f"{self.config.expected_dimension}, "
                f"actual={dimension}"
            )

        LOGGER.info(
            "Embedding model loaded | "
            "model=%s | "
            "dimension=%s",
            self.config.model_name,
            dimension,
        )

        return model

    # ==================================================
    # Input Processing
    # ==================================================

    @staticmethod
    def _normalize_text(
        text: str,
    ) -> str:
        """
        规范化单个文本。

        当前仅做最小清洗。

        不在 Embedding Client 中做：
            - 文档过滤
            - Header/Footer 删除
            - Chunk
            - Unicode 大规模重写

        因为这些已经属于你的 ingestion pipeline。
        """

        if text is None:
            raise EmbeddingValidationError(
                "Embedding text "
                "cannot be None."
            )

        if not isinstance(
            text,
            str,
        ):
            text = str(
                text
            )

        normalized = (
            text.strip()
        )

        if not normalized:
            raise EmbeddingValidationError(
                "Embedding text "
                "cannot be empty."
            )

        return normalized

    @classmethod
    def _normalize_texts(
        cls,
        texts: Sequence[str],
    ) -> list[str]:
        """
        校验并标准化 Batch。
        """

        if texts is None:
            raise EmbeddingValidationError(
                "Embedding texts "
                "cannot be None."
            )

        if isinstance(
            texts,
            str,
        ):
            raise EmbeddingValidationError(
                "embed_texts() expects "
                "a sequence of strings, "
                "not a single string."
            )

        normalized_texts: list[str] = []

        for index, text in enumerate(
            texts
        ):
            try:
                normalized = (
                    cls._normalize_text(
                        text
                    )
                )

            except EmbeddingValidationError as exc:
                raise (
                    EmbeddingValidationError(
                        "Invalid embedding text "
                        f"at index {index}: {exc}"
                    )
                ) from exc

            normalized_texts.append(
                normalized
            )

        return normalized_texts

    # ==================================================
    # Output Processing
    # ==================================================

    @staticmethod
    def _convert_embeddings(
        embeddings,
    ) -> list[list[float]]:
        """
        numpy / tensor-like 输出统一转成：

            list[list[float]]
        """

        try:
            array = np.asarray(
                embeddings,
                dtype=np.float32,
            )

        except Exception as exc:
            raise EmbeddingGenerationError(
                "Failed to convert embedding "
                f"output to numpy array: {exc}"
            ) from exc

        if array.ndim == 1:
            array = array.reshape(
                1,
                -1,
            )

        if array.ndim != 2:
            raise EmbeddingGenerationError(
                "Embedding output must be "
                "a 2-dimensional array. "
                f"Received ndim={array.ndim}"
            )

        return [
            row.astype(
                float
            ).tolist()
            for row in array
        ]

    @staticmethod
    def _validate_result_count(
        *,
        input_count: int,
        output_count: int,
    ) -> None:
        """
        保证：

            N texts
                ↓
            N vectors
        """

        if input_count != output_count:
            raise EmbeddingGenerationError(
                "Embedding result count "
                "does not match input count. "
                f"input={input_count}, "
                f"output={output_count}"
            )

    def _validate_vector(
        self,
        vector: list[float],
    ) -> None:
        """
        校验向量。

        包括：
            - 类型
            - 维度
            - NaN
            - Infinity
        """

        if not isinstance(
            vector,
            list,
        ):
            raise EmbeddingGenerationError(
                "Embedding vector "
                "must be a list."
            )

        dimension = len(
            vector
        )

        if (
            dimension
            != self.config.expected_dimension
        ):
            raise EmbeddingGenerationError(
                "Embedding vector dimension "
                "mismatch. "
                f"expected="
                f"{self.config.expected_dimension}, "
                f"actual={dimension}"
            )

        array = np.asarray(
            vector,
            dtype=np.float32,
        )

        if np.isnan(
            array
        ).any():
            raise EmbeddingGenerationError(
                "Embedding vector "
                "contains NaN."
            )

        if np.isinf(
            array
        ).any():
            raise EmbeddingGenerationError(
                "Embedding vector "
                "contains Infinity."
            )

    # ==================================================
    # Configuration Validation
    # ==================================================

    def _validate_config(
        self,
    ) -> None:
        """
        Client 配置校验。
        """

        if not str(
            self.config.model_name
            or ""
        ).strip():
            raise EmbeddingValidationError(
                "Embedding model_name "
                "cannot be empty."
            )

        if (
            self.config.batch_size
            <= 0
        ):
            raise EmbeddingValidationError(
                "Embedding batch_size "
                "must be greater than 0."
            )

        if (
            self.config.expected_dimension
            <= 0
        ):
            raise EmbeddingValidationError(
                "Embedding "
                "expected_dimension "
                "must be greater than 0."
            )