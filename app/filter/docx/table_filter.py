from __future__ import annotations

import re

from app.model.block import (
    BlockType,
    DocumentBlock,
)
from app.model.document import Document


class TableFilter:
    """
    DOCX 表格文本清洗器。

    DOCXLoader 会将一个表格行转换成一个：

        BlockType.TABLE

    并保存：

        block.text
        block.cells
        block.table_index
        block.row_index

    本过滤器负责：
        - 清理 TABLE Block 的单元格文本
        - 删除空单元格
        - 删除连续重复单元格
        - 删除全空表格行
        - 删除明显无意义的表格行
        - 统一表格文本分隔符
        - 同步 block.cells 与 block.text
        - 保持 Table / Row 原始定位信息
        - 保持非表格 Block 原样
        - 修改 blocks 后同步逻辑 Page

    不负责：
        - Chapter / Section 识别
        - 表格结构重新建模
        - 合并跨行单元格
        - Heading 处理
        - Chunk
        - Token Count

    设计原则：
        document.blocks 是 DOCX Pipeline 的结构数据源。

        document.pages 只是逻辑文本视图。

        因此必须优先修改 blocks，
        然后再同步 pages。
    """

    # ==================================================
    # Patterns
    # ==================================================

    _SEPARATOR_PATTERN = re.compile(
        r"\s*\|\s*"
    )

    _CONTROL_CHARACTER_PATTERN = re.compile(
        r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]"
    )

    _MULTIPLE_SPACES_PATTERN = re.compile(
        r"[ \t]+"
    )

    _SYMBOL_ONLY_PATTERN = re.compile(
        r"^[\W_]+$"
    )

    _ZERO_WIDTH_CHARACTERS = (
        "\u200b",
        "\u200c",
        "\u200d",
        "\u2060",
        "\ufeff",
    )

    # ==================================================
    # Constructor
    # ==================================================

    def __init__(
        self,
        *,
        separator: str = " | ",
        remove_duplicate_cells: bool = True,
        remove_empty_cells: bool = True,
        minimum_non_empty_cells: int = 1,
        rebuild_pages: bool = True,
    ) -> None:

        if not separator:
            raise ValueError(
                "separator cannot be empty."
            )

        if minimum_non_empty_cells < 1:
            raise ValueError(
                "minimum_non_empty_cells "
                "must be at least 1."
            )

        self.separator = separator

        self.remove_duplicate_cells = (
            remove_duplicate_cells
        )

        self.remove_empty_cells = (
            remove_empty_cells
        )

        self.minimum_non_empty_cells = (
            minimum_non_empty_cells
        )

        self.rebuild_pages = (
            rebuild_pages
        )

    # ==================================================
    # Public API
    # ==================================================

    def filter(
        self,
        document: Document,
    ) -> Document:
        """
        清洗 DOCX TABLE Blocks。

        Returns:
            修改后的同一个 Document。
        """

        self._validate_document(
            document
        )

        filtered_blocks: list[
            DocumentBlock
        ] = []

        processed_table_rows = 0

        retained_table_rows = 0

        removed_table_rows = 0

        removed_cells = 0

        # 按逻辑顺序处理。
        source_blocks = sorted(
            document.blocks,
            key=lambda block: block.order,
        )

        for block in source_blocks:

            # ==========================================
            # Non Table Block
            # ==========================================

            if (
                block.block_type
                != BlockType.TABLE
            ):
                filtered_blocks.append(
                    block
                )
                continue

            processed_table_rows += 1

            # ==========================================
            # Read Cells
            # ==========================================
            #
            # block.cells 优先。
            #
            # DOCXLoader 已经提供结构化 cells，
            # 因此不应该优先重新 split block.text。
            #
            # 只有旧数据 / 非标准 Block 没有 cells
            # 时才退回 text 解析。

            source_cells = self._extract_cells(
                block
            )

            normalized_cells: list[str] = []

            previous_cell: str | None = None

            for cell in source_cells:

                normalized_cell = (
                    self._normalize_cell(
                        cell
                    )
                )

                # ======================================
                # Empty Cell
                # ======================================

                if not normalized_cell:

                    if self.remove_empty_cells:
                        removed_cells += 1
                        continue

                    normalized_cells.append(
                        ""
                    )

                    # 空 Cell 不作为重复判断基准。
                    previous_cell = None

                    continue

                # ======================================
                # Duplicate Cell
                # ======================================
                #
                # 仅删除连续重复值。
                #
                # 例如合并单元格可能被 python-docx
                # 返回：
                #
                #   Setting | Setting | Value
                #
                # 这里变成：
                #
                #   Setting | Value
                #
                # 不删除非连续重复：
                #
                #   ON | OFF | ON
                #
                # 必须完整保留。

                if (
                    self.remove_duplicate_cells
                    and previous_cell
                    == normalized_cell
                ):
                    removed_cells += 1
                    continue

                normalized_cells.append(
                    normalized_cell
                )

                previous_cell = (
                    normalized_cell
                )

            # ==========================================
            # Non Empty Cell Count
            # ==========================================

            non_empty_count = sum(
                1
                for cell in normalized_cells
                if cell
            )

            if (
                non_empty_count
                < self.minimum_non_empty_cells
            ):
                removed_table_rows += 1
                continue

            # ==========================================
            # Meaningless Row
            # ==========================================

            if self._is_meaningless_row(
                normalized_cells
            ):
                removed_table_rows += 1
                continue

            # ==========================================
            # Update Block
            # ==========================================

            block.cells = (
                normalized_cells
            )

            block.text = self.separator.join(
                normalized_cells
            ).strip()

            filtered_blocks.append(
                block
            )

            retained_table_rows += 1

        # ==============================================
        # Reassign Logical Block Order
        # ==============================================

        self._reassign_order(
            filtered_blocks
        )

        document.blocks = (
            filtered_blocks
        )

        # ==============================================
        # Rebuild Logical Page
        # ==============================================

        if self.rebuild_pages:
            self._rebuild_pages(
                document
            )

        # ==============================================
        # Metadata
        # ==============================================

        document.metadata.update(
            {
                "table_filter": (
                    "TableFilter"
                ),
                "table_filter_status": (
                    "SUCCESS"
                ),
                "table_filter_processed_rows": (
                    processed_table_rows
                ),
                "table_filter_retained_rows": (
                    retained_table_rows
                ),
                "table_filter_removed_rows": (
                    removed_table_rows
                ),
                "table_filter_removed_cells": (
                    removed_cells
                ),
                "block_count_after_table_filter": (
                    len(
                        document.blocks
                    )
                ),
            }
        )

        return document

    # ==================================================
    # Cell Extraction
    # ==================================================

    @classmethod
    def _extract_cells(
        cls,
        block: DocumentBlock,
    ) -> list[str]:
        """
        获取 Table Block 的 Cell。

        优先级：

            1. block.cells
            2. block.text fallback

        使用 cells 优先可以避免单元格正文中包含 "|"
        时被错误切分。
        """

        if block.cells:
            return [
                str(cell)
                for cell in block.cells
            ]

        text = str(
            block.text
            or ""
        ).strip()

        if not text:
            return []

        return cls._split_cells(
            text
        )

    # ==================================================
    # Compatibility Helpers
    # ==================================================

    @classmethod
    def _looks_like_table_row(
        cls,
        line: str,
    ) -> bool:
        """
        保留旧版 Helper 接口。

        当前正式判断使用 BlockType.TABLE。
        """

        return "|" in str(
            line
            or ""
        )

    @classmethod
    def _split_cells(
        cls,
        line: str,
    ) -> list[str]:

        if not line:
            return []

        return cls._SEPARATOR_PATTERN.split(
            line
        )

    # ==================================================
    # Cell Normalize
    # ==================================================

    @classmethod
    def _normalize_cell(
        cls,
        value: str,
    ) -> str:

        if value is None:
            return ""

        normalized = str(
            value
        )

        # 全角空格
        normalized = normalized.replace(
            "\u3000",
            " ",
        )

        # Non-breaking space
        normalized = normalized.replace(
            "\xa0",
            " ",
        )

        # Word Cell 内部换行统一为空格。
        normalized = normalized.replace(
            "\r\n",
            " ",
        )

        normalized = normalized.replace(
            "\r",
            " ",
        )

        normalized = normalized.replace(
            "\n",
            " ",
        )

        # Zero-width characters
        for character in (
            cls._ZERO_WIDTH_CHARACTERS
        ):
            normalized = (
                normalized.replace(
                    character,
                    "",
                )
            )

        # Control characters
        normalized = (
            cls._CONTROL_CHARACTER_PATTERN.sub(
                "",
                normalized,
            )
        )

        # Multiple spaces
        normalized = (
            cls._MULTIPLE_SPACES_PATTERN.sub(
                " ",
                normalized,
            )
        )

        return normalized.strip()

    # ==================================================
    # Meaningless Row
    # ==================================================

    @classmethod
    def _is_meaningless_row(
        cls,
        cells: list[str],
    ) -> bool:
        """
        判断表格行是否只有：

            ----
            ====
            ****
            .....
            ---- | ===

        等符号内容。
        """

        meaningful_cells = [
            str(cell).strip()
            for cell in cells
            if str(
                cell
                or ""
            ).strip()
        ]

        if not meaningful_cells:
            return True

        combined = "".join(
            meaningful_cells
        )

        if not combined:
            return True

        return bool(
            cls._SYMBOL_ONLY_PATTERN.fullmatch(
                combined
            )
        )

    # ==================================================
    # Block Order
    # ==================================================

    @staticmethod
    def _reassign_order(
        blocks: list[DocumentBlock],
    ) -> None:
        """
        删除无效 Table Block 后，
        重建连续逻辑 order。

        注意：
            table_index / row_index 不修改。

        因为它们表示原始 Word 表格位置，
        不是当前 Block 的逻辑排序。
        """

        for order, block in enumerate(
            blocks
        ):
            block.order = order

    # ==================================================
    # Page Synchronization
    # ==================================================

    @staticmethod
    def _rebuild_pages(
        document: Document,
    ) -> None:
        """
        根据清洗后的 blocks 同步 DOCX 逻辑 Page。

        当前 DOCXLoader 使用一个逻辑 Page：

            document.pages[0]

        blocks 才是结构数据源。
        """

        if not document.pages:
            return

        text = "\n".join(
            str(
                block.text
                or ""
            ).strip()
            for block in document.blocks
            if str(
                block.text
                or ""
            ).strip()
        )

        document.pages[0].text = (
            text.strip()
        )

    # ==================================================
    # Validation
    # ==================================================

    @staticmethod
    def _validate_document(
        document: Document,
    ) -> None:

        if document is None:
            raise ValueError(
                "Document cannot be None."
            )

        if not isinstance(
            document,
            Document,
        ):
            raise TypeError(
                "TableFilter expects an "
                "app.model.document.Document instance."
            )

        file_type = str(
            document.file_type
            or ""
        ).strip().lower()

        if file_type != "docx":
            raise ValueError(
                "TableFilter only accepts "
                "DOCX documents. "
                f"Received file_type: "
                f"{document.file_type}"
            )