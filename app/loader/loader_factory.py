from __future__ import annotations

from pathlib import Path

from app.loader.base_loader import BaseLoader
from app.loader.docx_loader import DOCXLoader
from app.loader.excel_loader import ExcelLoader
from app.loader.pdf_loader import PDFLoader
from app.loader.pptx_loader import PPTXLoader


class LoaderFactory:
    """
    Loader 工厂。

    根据文件扩展名返回对应 Loader。

    支持：
        .pdf
        .docx
        .pptx
        .xlsx

    不支持：
        .ppt
        .xls

    不负责：
        - 文件解析
        - Pipeline 选择
        - 内容过滤
        - JSON / PostgreSQL 保存
    """

    _SUPPORTED_EXTENSIONS = {
        ".pdf",
        ".docx",
        ".pptx",
        ".xlsx",
    }

    @classmethod
    def get_loader(
        cls,
        file_path: str | Path,
    ) -> BaseLoader:
        """
        根据文件扩展名返回 Loader。

        Args:
            file_path:
                输入文件路径。

        Returns:
            对应的 BaseLoader 实例。

        Raises:
            ValueError:
                file_path 为空或文件格式不支持。
        """

        if file_path is None:
            raise ValueError(
                "file_path cannot be None."
            )

        path = Path(
            file_path
        ).expanduser()

        if not str(path).strip():
            raise ValueError(
                "file_path cannot be empty."
            )

        extension = (
            path.suffix
            .strip()
            .lower()
        )

        if not extension:
            raise ValueError(
                "Input file has no extension: "
                f"{path.name}"
            )

        if extension not in cls._SUPPORTED_EXTENSIONS:
            raise ValueError(
                "Unsupported file format: "
                f"{extension}. "
                "Supported formats: "
                ".pdf, .docx, .pptx, .xlsx"
            )

        if extension == ".pdf":
            return PDFLoader()

        if extension == ".docx":
            return DOCXLoader()

        if extension == ".pptx":
            return PPTXLoader()

        if extension == ".xlsx":
            return ExcelLoader()

        # 理论上不会进入这里，
        # 用于防止未来修改 _SUPPORTED_EXTENSIONS
        # 后忘记补 Loader 映射。
        raise RuntimeError(
            "LoaderFactory configuration error: "
            f"no loader mapped for {extension}"
        )

    @classmethod
    def supports(
        cls,
        file_path: str | Path,
    ) -> bool:
        """
        判断文件扩展名是否受支持。

        Example:

            LoaderFactory.supports("test.pdf")
            -> True

            LoaderFactory.supports("test.xls")
            -> False
        """

        if file_path is None:
            return False

        extension = (
            Path(file_path)
            .suffix
            .strip()
            .lower()
        )

        return (
            extension
            in cls._SUPPORTED_EXTENSIONS
        )

    @classmethod
    def supported_extensions(
        cls,
    ) -> tuple[str, ...]:
        """
        返回受支持的文件扩展名。
        """

        return tuple(
            sorted(
                cls._SUPPORTED_EXTENSIONS
            )
        )