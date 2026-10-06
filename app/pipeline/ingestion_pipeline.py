from __future__ import annotations

from pathlib import Path
from typing import Any

from app.pipeline.docx_pipeline import DOCXPipeline
from app.pipeline.pdf_pipeline import PDFPipeline
from app.pipeline.pptx_pipeline import PPTXPipeline
from app.pipeline.xlsx_pipeline import XLSXPipeline


class IngestionPipeline:
    """
    文档摄取统一入口。

    IngestionPipeline 本身不负责：

        - 文件内容加载
        - Page / Block 过滤
        - Chapter / Section 解析
        - Chunk
        - Token Count
        - JSON 输出
        - PostgreSQL 保存

    它只负责：

        File
            ↓
        Format Detection
            ↓
        Format-specific Pipeline

    支持：

        .pdf
            -> PDFPipeline

        .docx
            -> DOCXPipeline

        .pptx
            -> PPTXPipeline

        .xlsx
            -> XLSXPipeline
    """

    SUPPORTED_EXTENSIONS = {
        ".pdf",
        ".docx",
        ".pptx",
        ".xlsx",
    }

    def __init__(
        self,
    ) -> None:

        # 延迟创建具体 Pipeline。
        #
        # 这样启动 IngestionPipeline 时，
        # 不会一次性初始化所有格式相关组件，
        # 也不会产生不必要的数据库等副作用。
        self._pipelines: dict[
            str,
            Any,
        ] = {}

    def run(
        self,
        file_path: str | Path,
        output: str | Path,
    ):
        """
        根据文件扩展名选择对应 Pipeline 并执行。

        Args:
            file_path:
                输入文件路径。

            output:
                JSON 等输出路径。

        Returns:
            最终统一 Document。
        """

        input_path = self._validate_input_path(
            file_path
        )

        extension = (
            input_path.suffix.lower()
        )

        pipeline = self._get_pipeline(
            extension
        )

        return pipeline.run(
            input_path,
            output,
        )

    def _get_pipeline(
        self,
        extension: str,
    ):
        """
        获取指定格式 Pipeline。

        Pipeline 使用 lazy initialization，
        同一 IngestionPipeline 实例中会复用已经创建的实例。
        """

        if extension in self._pipelines:
            return self._pipelines[
                extension
            ]

        pipeline = self._create_pipeline(
            extension
        )

        self._pipelines[
            extension
        ] = pipeline

        return pipeline

    @staticmethod
    def _create_pipeline(
        extension: str,
    ):
        """
        创建格式专用 Pipeline。
        """

        if extension == ".pdf":
            return PDFPipeline()

        if extension == ".docx":
            return DOCXPipeline()

        if extension == ".pptx":
            return PPTXPipeline()

        if extension == ".xlsx":
            return XLSXPipeline()

        raise ValueError(
            "Unsupported document format: "
            f"{extension or '<no extension>'}"
        )

    @classmethod
    def _validate_input_path(
        cls,
        file_path: str | Path,
    ) -> Path:

        if file_path is None:
            raise ValueError(
                "file_path cannot be None."
            )

        if not str(
            file_path
        ).strip():
            raise ValueError(
                "file_path cannot be empty."
            )

        path = Path(
            file_path
        ).expanduser()

        if not path.exists():
            raise FileNotFoundError(
                f"Input file not found: {path}"
            )

        if not path.is_file():
            raise IsADirectoryError(
                f"Input path is not a file: {path}"
            )

        if path.name.startswith(
            "~$"
        ):
            raise ValueError(
                "Temporary Office file is not supported: "
                f"{path.name}"
            )

        extension = path.suffix.lower()

        if extension not in cls.SUPPORTED_EXTENSIONS:
            supported = ", ".join(
                sorted(
                    cls.SUPPORTED_EXTENSIONS
                )
            )

            raise ValueError(
                "Unsupported document format. "
                f"Supported formats: {supported}. "
                f"Received: "
                f"{extension or '<no extension>'}"
            )

        return path

    @classmethod
    def supports(
        cls,
        file_path: str | Path,
    ) -> bool:
        """
        判断文件格式是否被统一 Pipeline 支持。
        """

        if file_path is None:
            return False

        extension = Path(
            file_path
        ).suffix.lower()

        return (
            extension
            in cls.SUPPORTED_EXTENSIONS
        )