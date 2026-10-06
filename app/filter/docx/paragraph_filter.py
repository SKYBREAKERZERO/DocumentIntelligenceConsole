from __future__ import annotations

import re

from app.model.block import (
    BlockType,
    DocumentBlock,
)
from app.model.document import Document


class ParagraphFilter:
    """
    DOCX 段落清洗器。

    负责：
        - 清理 Paragraph / List / TextBox 文本
        - 清理空白字符
        - 删除不可见控制字符
        - 合并同一行中的重复空格
        - 删除连续重复段落
        - 删除纯页码段落
        - 保持 Block 原始顺序
        - 修改 blocks 后同步逻辑 Page

    不负责：
        - Heading 清洗
        - Table 清洗
        - Chapter / Section 识别
        - 标题合并
        - Chunk

    设计原则：
        DOCXParser 主要基于 document.blocks 解析，
        因此本过滤器直接修改 document.blocks。

        document.pages 仅作为同步后的逻辑文本视图。
    """

    # ==================================================
    # Filter Targets
    # ==================================================

    _FILTERABLE_BLOCK_TYPES = {
        BlockType.PARAGRAPH,
        BlockType.LIST,
        BlockType.TEXTBOX,
    }

    # ==================================================
    # Patterns
    # ==================================================

    _PAGE_NUMBER_PATTERNS = (
        re.compile(
            r"^\d+$"
        ),
        re.compile(
            r"^\d+\s*/\s*\d+$"
        ),
        re.compile(
            r"^page\s+\d+$",
            re.IGNORECASE,
        ),
        re.compile(
            r"^-\s*\d+\s*-$"
        ),
    )

    _CONTROL_CHARACTER_PATTERN = re.compile(
        r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]"
    )

    _MULTIPLE_SPACES_PATTERN = re.compile(
        r"[ \t]+"
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
        remove_page_numbers: bool = True,
        remove_duplicate_lines: bool = True,
        minimum_line_length: int = 1,
        rebuild_pages: bool = True,
    ) -> None:

        if minimum_line_length < 0:
            raise ValueError(
                "minimum_line_length cannot be negative."
            )

        self.remove_page_numbers = (
            remove_page_numbers
        )

        self.remove_duplicate_lines = (
            remove_duplicate_lines
        )

        self.minimum_line_length = (
            minimum_line_length
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
        清洗 DOCX 段落类 Block。

        Returns:
            修改后的同一个 Document。
        """

        self._validate_document(
            document
        )

        filtered_blocks: list[
            DocumentBlock
        ] = []

        removed_empty_count = 0

        removed_page_number_count = 0

        removed_duplicate_count = 0

        processed_paragraph_count = 0

        retained_paragraph_count = 0

        previous_text: str | None = None

        for block in sorted(
            document.blocks,
            key=lambda item: item.order,
        ):

            # ==========================================
            # Non Paragraph Block
            # ==========================================
            #
            # Heading:
            #     HeadingMerger 负责
            #
            # Table:
            #     TableFilter 负责
            #
            # Image / PageBreak:
            #     本 Filter 不处理

            if (
                block.block_type
                not in self._FILTERABLE_BLOCK_TYPES
            ):
                filtered_blocks.append(
                    block
                )

                # 非连续 Paragraph 之间，
                # 不做跨结构重复删除。
                previous_text = None

                continue

            processed_paragraph_count += 1

            # ==========================================
            # Normalize
            # ==========================================

            text = self._normalize_line(
                block.text
            )

            # ==========================================
            # Empty
            # ==========================================

            if not text:
                removed_empty_count += 1
                continue

            if (
                len(text)
                < self.minimum_line_length
            ):
                removed_empty_count += 1
                continue

            # ==========================================
            # Page Number
            # ==========================================

            if (
                self.remove_page_numbers
                and self._is_page_number(
                    text
                )
            ):
                removed_page_number_count += 1
                continue

            # ==========================================
            # Consecutive Duplicate
            # ==========================================

            if (
                self.remove_duplicate_lines
                and previous_text == text
            ):
                removed_duplicate_count += 1
                continue

            # ==========================================
            # Retain
            # ==========================================

            block.text = text

            filtered_blocks.append(
                block
            )

            previous_text = text

            retained_paragraph_count += 1

        # ==============================================
        # Reassign Order
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
                "paragraph_filter": (
                    "ParagraphFilter"
                ),
                "paragraph_filter_status": (
                    "SUCCESS"
                ),
                "paragraph_filter_processed": (
                    processed_paragraph_count
                ),
                "paragraph_filter_removed_empty": (
                    removed_empty_count
                ),
                "paragraph_filter_removed_page_numbers": (
                    removed_page_number_count
                ),
                "paragraph_filter_removed_duplicates": (
                    removed_duplicate_count
                ),
                "paragraph_filter_retained": (
                    retained_paragraph_count
                ),
                "block_count_after_paragraph_filter": (
                    len(document.blocks)
                ),
            }
        )

        return document

    # ==================================================
    # Normalize
    # ==================================================

    @classmethod
    def _normalize_line(
        cls,
        text: str,
    ) -> str:
        """
        标准化单个 Paragraph 文本。
        """

        if text is None:
            return ""

        normalized = str(
            text
        )

        normalized = normalized.replace(
            "\u3000",
            " ",
        )

        normalized = normalized.replace(
            "\xa0",
            " ",
        )

        for character in (
            cls._ZERO_WIDTH_CHARACTERS
        ):
            normalized = normalized.replace(
                character,
                "",
            )

        normalized = (
            cls._CONTROL_CHARACTER_PATTERN.sub(
                "",
                normalized,
            )
        )

        normalized = (
            cls._MULTIPLE_SPACES_PATTERN.sub(
                " ",
                normalized,
            )
        )

        return normalized.strip()

    # ==================================================
    # Page Number Detection
    # ==================================================

    @classmethod
    def _is_page_number(
        cls,
        line: str,
    ) -> bool:

        normalized = str(
            line
            or ""
        ).strip()

        if not normalized:
            return False

        return any(
            pattern.fullmatch(
                normalized
            )
            is not None
            for pattern
            in cls._PAGE_NUMBER_PATTERNS
        )

    # ==================================================
    # Block Order
    # ==================================================

    @staticmethod
    def _reassign_order(
        blocks: list[DocumentBlock],
    ) -> None:
        """
        Block 删除后重新建立连续 order。

        Example:

            0
            1
            4
            6

        ->

            0
            1
            2
            3
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
        根据清洗后的 blocks 重建 DOCX 逻辑 Page。

        DOCXLoader 当前采用：

            pages[0]

        表示整个 Word 文档的逻辑文本。

        因此 blocks 修改后必须同步，
        防止：

            document.blocks
                !=
            document.pages
        """

        if not document.pages:
            return

        text = "\n".join(
            block.text.strip()
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
                "ParagraphFilter expects an "
                "app.model.document.Document instance."
            )

        file_type = str(
            document.file_type
            or ""
        ).strip().lower()

        if file_type != "docx":
            raise ValueError(
                "ParagraphFilter only accepts "
                "DOCX documents. "
                f"Received file_type: "
                f"{document.file_type}"
            )

        if not document.blocks:
            raise ValueError(
                "DOCX document contains no blocks. "
                "DOCXLoader must populate "
                "document.blocks before "
                "ParagraphFilter."
            )