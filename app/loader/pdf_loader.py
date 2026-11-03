from __future__ import annotations

from pathlib import Path
from typing import Any

import fitz

from app.loader.base_loader import BaseLoader
from app.model.document import Document
from app.model.page import Page
from app.utils.exceptions import FileReadException


class PDFLoader(BaseLoader):
    """
    PDF 原始内容加载器。

    负责：
        - 校验 PDF 输入文件
        - 按原始页面顺序读取文本
        - 每个 PDF Page 映射为一个 Document Page
        - 保留原始 page_number
        - 保存 PDF 基础 metadata
        - 统计页面数、空页面数、字符数

    不负责：
        - 删除空页
        - 删除目录页
        - 页眉页脚过滤
        - Chapter / Section 建模
        - Chunk
        - Token 统计
        - JSON / PostgreSQL 保存

    设计原则：
        PDF 当前以 document.pages 作为原始文本数据源。

        PageFilter
            -> 删除空页 / 目录页

        HeaderFooterFilter
            -> 清理页眉页脚

        PDFParser
            -> 建立 Chapter / Section / Content

        因此 PDFLoader 不需要建立 DocumentBlock。
    """

    SUPPORTED_EXTENSIONS = {
        ".pdf",
    }

    # ==================================================
    # Public API
    # ==================================================

    def load(
        self,
        file_path: str,
    ) -> Document:
        """
        加载 PDF 文件。

        Args:
            file_path:
                PDF 文件路径。

        Returns:
            Document:
                原始 PDF 文档数据。
        """

        path = self._validate_path(
            file_path
        )

        try:

            with fitz.open(
                str(path)
            ) as pdf:

                # ======================================
                # Password Protected PDF
                # ======================================

                if pdf.needs_pass:

                    raise FileReadException(
                        "Password-protected PDF "
                        "is not supported: "
                        f"{path.name}"
                    )

                return self._build_document(
                    path=path,
                    pdf=pdf,
                )

        except FileReadException:
            raise

        except Exception as exc:

            raise FileReadException(
                "Failed to read PDF "
                f"'{path.name}'. "
                f"Reason: {exc}"
            ) from exc

    # ==================================================
    # Build Document
    # ==================================================

    @staticmethod
    def _build_document(
        *,
        path: Path,
        pdf,
    ) -> Document:
        """
        从 PyMuPDF Document 构建统一 Document。
        """

        pages: list[
            Page
        ] = []

        empty_page_count = 0
        character_count = 0

        # ==============================================
        # Pages
        # ==============================================

        for page_number, pdf_page in enumerate(
            pdf,
            start=1,
        ):

            try:

                text = (
                    pdf_page
                    .get_text()
                    .strip()
                )

            except Exception as exc:

                raise FileReadException(
                    "Failed to extract text "
                    f"from page {page_number} "
                    f"of '{path.name}'. "
                    f"Reason: {exc}"
                ) from exc

            if not text:

                empty_page_count += 1

            character_count += len(
                text
            )

            pages.append(
                Page(
                    page_number=(
                        page_number
                    ),
                    text=text,
                )
            )

        # ==============================================
        # PDF Metadata
        # ==============================================

        raw_pdf_metadata = (
            pdf.metadata
            or {}
        )

        pdf_metadata: dict[
            str,
            Any,
        ] = {
            str(key): value
            for key, value
            in raw_pdf_metadata.items()
        }

        # ==============================================
        # Document Metadata
        # ==============================================

        metadata: dict[
            str,
            Any,
        ] = {
            "source_format": (
                "pdf"
            ),

            "loader": (
                "PDFLoader"
            ),

            "loader_status": (
                "SUCCESS"
            ),

            "page_count": (
                len(
                    pages
                )
            ),

            "empty_page_count": (
                empty_page_count
            ),

            "non_empty_page_count": (
                len(
                    pages
                )
                - empty_page_count
            ),

            "character_count": (
                character_count
            ),

            "pdf_metadata": (
                pdf_metadata
            ),
        }

        # ==============================================
        # Document
        # ==============================================

        return Document(
            file_name=path.name,
            file_type="pdf",
            pages=pages,
            blocks=[],
            chapters=[],
            sections=[],
            contents=[],
            metadata=metadata,
        )

    # ==================================================
    # Path Validation
    # ==================================================

    @classmethod
    def _validate_path(
        cls,
        file_path: str,
    ) -> Path:
        """
        校验 PDF 文件路径。
        """

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
                "PDF file not found: "
                f"{path}"
            )

        if not path.is_file():

            raise IsADirectoryError(
                "Input path is not a file: "
                f"{path}"
            )

        # PDF 软件可能产生类似临时文件。
        if path.name.startswith(
            "~$"
        ):

            raise ValueError(
                "Temporary PDF file "
                "is not supported: "
                f"{path.name}"
            )

        extension = (
            path.suffix
            .strip()
            .lower()
        )

        if (
            extension
            not in cls.SUPPORTED_EXTENSIONS
        ):

            raise ValueError(
                "PDFLoader only accepts "
                ".pdf files. "
                f"Received: "
                f"{extension or '<no extension>'}"
            )

        return path