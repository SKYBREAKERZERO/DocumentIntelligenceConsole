from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from app.model.block import BlockType, DocumentBlock
from app.model.document import Document
from app.model.page import Page


class SlideFilterError(RuntimeError):
    """
    PPTX Slide 过滤异常。
    """


class SlideFilter:
    """
    PPTX Slide 过滤器。

    负责：
        - 删除隐藏 Slide
        - 删除空 Slide
        - 删除 Block 数过少的 Slide
        - 删除文本长度过短的 Slide
        - 根据 Slide 编号白名单 / 黑名单过滤
        - 根据标题白名单 / 黑名单过滤
        - 根据标题正则排除封面、目录、修订履历等页面
        - 删除被过滤 Slide 对应的 DocumentBlock
        - 重建逻辑 Page
        - 重新分配 Block order
        - 修正 Block page_number
        - 保留原始 slide_index / slide_number
        - 更新 document.metadata["slides"]
        - 保持 metadata 统计可重复执行

    不负责：
        - Shape 内容清洗
        - 文本框去重
        - 表格行过滤
        - Chapter / Section 建模
        - Chunk
        - Token 统计
    """

    # ==================================================
    # Default Excluded Titles
    # ==================================================

    DEFAULT_EXCLUDED_TITLE_PATTERNS = (
        re.compile(
            r"^(?:cover|title|表紙|封面)$",
            re.IGNORECASE,
        ),
        re.compile(
            r"^(?:table\s+of\s+contents|contents?|toc|目次|目录)$",
            re.IGNORECASE,
        ),
        re.compile(
            r"^(?:revision\s+history|change\s+history|"
            r"改訂履歴|変更履歴|改版履歴|变更履历)$",
            re.IGNORECASE,
        ),
        re.compile(
            r"^(?:template|テンプレート|模板)$",
            re.IGNORECASE,
        ),
        re.compile(
            r"^(?:sample|サンプル|示例)$",
            re.IGNORECASE,
        ),
    )

    # ==================================================
    # Constructor
    # ==================================================

    def __init__(
        self,
        *,
        remove_hidden_slides: bool = True,
        remove_empty_slides: bool = True,
        minimum_block_count: int = 1,
        minimum_text_length: int = 1,
        include_slide_numbers: Iterable[int] | None = None,
        exclude_slide_numbers: Iterable[int] | None = None,
        include_titles: Iterable[str] | None = None,
        exclude_titles: Iterable[str] | None = None,
        exclude_default_titles: bool = False,
        exclude_title_patterns: Iterable[
            str | re.Pattern[str]
        ] | None = None,
        case_sensitive: bool = False,
        retain_slides_without_metadata: bool = True,
        rebuild_pages: bool = True,
        reassign_block_order: bool = True,
    ) -> None:

        if minimum_block_count < 0:
            raise ValueError(
                "minimum_block_count cannot be negative."
            )

        if minimum_text_length < 0:
            raise ValueError(
                "minimum_text_length cannot be negative."
            )

        self.remove_hidden_slides = (
            remove_hidden_slides
        )

        self.remove_empty_slides = (
            remove_empty_slides
        )

        self.minimum_block_count = (
            minimum_block_count
        )

        self.minimum_text_length = (
            minimum_text_length
        )

        self.case_sensitive = (
            case_sensitive
        )

        self.retain_slides_without_metadata = (
            retain_slides_without_metadata
        )

        self.rebuild_pages = (
            rebuild_pages
        )

        self.reassign_block_order = (
            reassign_block_order
        )

        self.include_slide_numbers = {
            int(value)
            for value
            in (
                include_slide_numbers
                or []
            )
        }

        self.exclude_slide_numbers = {
            int(value)
            for value
            in (
                exclude_slide_numbers
                or []
            )
        }

        self.include_titles = (
            self._normalize_text_set(
                include_titles
            )
        )

        self.exclude_titles = (
            self._normalize_text_set(
                exclude_titles
            )
        )

        patterns: list[
            re.Pattern[str]
        ] = []

        if exclude_default_titles:
            patterns.extend(
                self.DEFAULT_EXCLUDED_TITLE_PATTERNS
            )

        if exclude_title_patterns:

            flags = (
                0
                if case_sensitive
                else re.IGNORECASE
            )

            for pattern in exclude_title_patterns:

                if isinstance(
                    pattern,
                    re.Pattern,
                ):
                    patterns.append(
                        pattern
                    )

                else:
                    patterns.append(
                        re.compile(
                            str(
                                pattern
                            ),
                            flags,
                        )
                    )

        self.exclude_title_patterns = tuple(
            patterns
        )

    # ==================================================
    # Public API
    # ==================================================

    def filter(
        self,
        document: Document,
    ) -> Document:

        self._validate_document(
            document
        )

        try:

            # ==========================================
            # Collect Slide Records
            # ==========================================

            slide_records = (
                self._collect_slide_records(
                    document
                )
            )

            # ==========================================
            # Group Blocks
            # ==========================================

            blocks_by_slide = (
                self._group_blocks_by_slide(
                    document.blocks
                )
            )

            retained_slide_indexes: set[
                int
            ] = set()

            removed_slides: list[
                dict[str, Any]
            ] = []

            # ==========================================
            # Filter Slides
            # ==========================================

            for record in slide_records:

                slide_index = int(
                    record[
                        "slide_index"
                    ]
                )

                slide_blocks = (
                    blocks_by_slide.get(
                        slide_index,
                        [],
                    )
                )

                keep, reason = (
                    self._should_keep_slide(
                        record=record,
                        blocks=slide_blocks,
                    )
                )

                if keep:

                    retained_slide_indexes.add(
                        slide_index
                    )

                    continue

                removed_slides.append(
                    {
                        "slide_index": (
                            slide_index
                        ),
                        "slide_number": (
                            record.get(
                                "slide_number",
                                slide_index + 1,
                            )
                        ),
                        "title": (
                            record.get(
                                "title"
                            )
                        ),
                        "hidden": bool(
                            record.get(
                                "hidden",
                                False,
                            )
                        ),
                        "reason": (
                            reason
                        ),
                    }
                )

            # ==========================================
            # Filter Blocks
            # ==========================================

            original_block_count = len(
                document.blocks
            )

            document.blocks = [
                block
                for block
                in document.blocks
                if self._block_is_retained(
                    block=block,
                    retained_slide_indexes=(
                        retained_slide_indexes
                    ),
                    retain_without_metadata=(
                        self.retain_slides_without_metadata
                    ),
                )
            ]

            # ==========================================
            # Block Order
            # ==========================================

            if self.reassign_block_order:

                self._reassign_block_order(
                    document
                )

            # ==========================================
            # Rebuild Pages
            # ==========================================

            if self.rebuild_pages:

                self._rebuild_pages(
                    document=document,
                    retained_slide_indexes=(
                        retained_slide_indexes
                    ),
                    slide_records=(
                        slide_records
                    ),
                )

            # ==========================================
            # Metadata
            # ==========================================

            self._update_slide_metadata(
                document=document,
                retained_slide_indexes=(
                    retained_slide_indexes
                ),
                removed_slides=(
                    removed_slides
                ),
                slide_records=(
                    slide_records
                ),
            )

            document.metadata.update(
                {
                    "slide_filter": (
                        "SlideFilter"
                    ),
                    "slide_filter_status": (
                        "SUCCESS"
                    ),
                    "slide_filter_original_count": (
                        len(
                            slide_records
                        )
                    ),
                    "slide_filter_retained_count": (
                        len(
                            retained_slide_indexes
                        )
                    ),
                    "slide_filter_removed_count": (
                        len(
                            removed_slides
                        )
                    ),
                    "slide_filter_removed_block_count": (
                        original_block_count
                        - len(
                            document.blocks
                        )
                    ),
                    "slide_filter_removed_slides": (
                        removed_slides
                    ),
                }
            )

            return document

        except SlideFilterError:
            raise

        except Exception as exc:
            raise SlideFilterError(
                "Failed to filter PPTX "
                f"slides: {exc}"
            ) from exc

    # ==================================================
    # Slide Decision
    # ==================================================

    def _should_keep_slide(
        self,
        *,
        record: dict[str, Any],
        blocks: list[DocumentBlock],
    ) -> tuple[
        bool,
        str | None,
    ]:

        slide_index = int(
            record.get(
                "slide_index",
                0,
            )
        )

        slide_number = int(
            record.get(
                "slide_number",
                slide_index + 1,
            )
        )

        hidden = bool(
            record.get(
                "hidden",
                False,
            )
        )

        title = self._normalize_text(
            record.get(
                "title",
                "",
            )
        )

        normalized_title = (
            self._normalize_key(
                title
            )
        )

        block_count = len(
            blocks
        )

        text_length = sum(
            len(
                self._normalize_text(
                    block.text
                )
            )
            for block
            in blocks
        )

        # ==============================================
        # Include Slide Numbers
        # ==============================================

        if (
            self.include_slide_numbers
            and slide_number
            not in self.include_slide_numbers
        ):
            return (
                False,
                "not_in_slide_number_include_list",
            )

        # ==============================================
        # Include Titles
        # ==============================================

        if (
            self.include_titles
            and normalized_title
            not in self.include_titles
        ):
            return (
                False,
                "not_in_title_include_list",
            )

        # ==============================================
        # Exclude Slide Numbers
        # ==============================================

        if (
            slide_number
            in self.exclude_slide_numbers
        ):
            return (
                False,
                "excluded_slide_number",
            )

        # ==============================================
        # Exclude Titles
        # ==============================================

        if (
            normalized_title
            in self.exclude_titles
        ):
            return (
                False,
                "excluded_slide_title",
            )

        # ==============================================
        # Hidden Slide
        # ==============================================

        if (
            hidden
            and self.remove_hidden_slides
        ):
            return (
                False,
                "hidden_slide",
            )

        # ==============================================
        # Empty Slide
        # ==============================================

        if (
            self.remove_empty_slides
            and block_count == 0
        ):
            return (
                False,
                "empty_slide",
            )

        # ==============================================
        # Minimum Block Count
        # ==============================================

        if (
            block_count
            < self.minimum_block_count
        ):
            return (
                False,
                "insufficient_block_count",
            )

        # ==============================================
        # Minimum Text Length
        # ==============================================

        if (
            text_length
            < self.minimum_text_length
        ):
            return (
                False,
                "insufficient_text_length",
            )

        # ==============================================
        # Excluded Title Pattern
        # ==============================================

        if (
            title
            and self._matches_excluded_title_pattern(
                title
            )
        ):
            return (
                False,
                "excluded_title_pattern",
            )

        return (
            True,
            None,
        )

    # ==================================================
    # Collect Slide Records
    # ==================================================

    def _collect_slide_records(
        self,
        document: Document,
    ) -> list[
        dict[str, Any]
    ]:
        """
        收集 Slide metadata。

        优先使用：

            document.metadata["slides"]

        同时使用：

            document.blocks

        补全 metadata 中缺失的 Slide。

        这样 metadata["slides"] 即使不完整，
        也不会导致合法 Block 被误删。
        """

        raw_records = (
            document.metadata.get(
                "slides",
                [],
            )
        )

        record_map: dict[
            int,
            dict[str, Any],
        ] = {}

        # ==============================================
        # Metadata Records
        # ==============================================

        if isinstance(
            raw_records,
            list,
        ):

            for raw_record in raw_records:

                if not isinstance(
                    raw_record,
                    dict,
                ):
                    continue

                if (
                    "slide_index"
                    not in raw_record
                ):
                    continue

                slide_index = int(
                    raw_record[
                        "slide_index"
                    ]
                )

                record = dict(
                    raw_record
                )

                record[
                    "slide_index"
                ] = (
                    slide_index
                )

                record.setdefault(
                    "slide_number",
                    slide_index + 1,
                )

                record.setdefault(
                    "title",
                    None,
                )

                record.setdefault(
                    "hidden",
                    False,
                )

                record_map[
                    slide_index
                ] = record

        # ==============================================
        # Current Block Statistics
        # ==============================================

        block_counts: dict[
            int,
            int,
        ] = {}

        character_counts: dict[
            int,
            int,
        ] = {}

        # ==============================================
        # Supplement From Blocks
        # ==============================================

        for block in document.blocks:

            metadata = (
                block.metadata
                or {}
            )

            slide_index = (
                metadata.get(
                    "slide_index"
                )
            )

            if slide_index is None:

                if (
                    block.page_number
                    is None
                ):
                    continue

                slide_index = (
                    int(
                        block.page_number
                    )
                    - 1
                )

            slide_index = int(
                slide_index
            )

            record = (
                record_map.setdefault(
                    slide_index,
                    {
                        "slide_index": (
                            slide_index
                        ),
                        "slide_number": (
                            metadata.get(
                                "slide_number",
                                slide_index + 1,
                            )
                        ),
                        "title": None,
                        "hidden": False,
                    },
                )
            )

            block_counts[
                slide_index
            ] = (
                block_counts.get(
                    slide_index,
                    0,
                )
                + 1
            )

            character_counts[
                slide_index
            ] = (
                character_counts.get(
                    slide_index,
                    0,
                )
                + len(
                    block.text
                    or ""
                )
            )

            # Metadata 中没有标题时，
            # 从一级 Heading Block 回退识别。
            if (
                not record.get(
                    "title"
                )
                and block.block_type
                == BlockType.HEADING
                and block.level == 1
                and block.text
            ):
                record[
                    "title"
                ] = (
                    block.text
                )

        # ==============================================
        # Refresh Statistics
        # ==============================================

        for (
            slide_index,
            record,
        ) in record_map.items():

            record[
                "block_count"
            ] = (
                block_counts.get(
                    slide_index,
                    0,
                )
            )

            record[
                "character_count"
            ] = (
                character_counts.get(
                    slide_index,
                    0,
                )
            )

        return [
            record_map[
                slide_index
            ]
            for slide_index
            in sorted(
                record_map
            )
        ]

    # ==================================================
    # Group Blocks By Slide
    # ==================================================

    @classmethod
    def _group_blocks_by_slide(
        cls,
        blocks: list[
            DocumentBlock
        ],
    ) -> dict[
        int,
        list[DocumentBlock],
    ]:

        groups: dict[
            int,
            list[DocumentBlock],
        ] = {}

        for block in blocks:

            slide_index = (
                cls._resolve_slide_index(
                    block,
                    allow_missing=True,
                )
            )

            if slide_index is None:
                continue

            groups.setdefault(
                slide_index,
                [],
            ).append(
                block
            )

        return groups

    # ==================================================
    # Block Retention
    # ==================================================

    @classmethod
    def _block_is_retained(
        cls,
        *,
        block: DocumentBlock,
        retained_slide_indexes: set[int],
        retain_without_metadata: bool,
    ) -> bool:

        slide_index = (
            cls._resolve_slide_index(
                block,
                allow_missing=True,
            )
        )

        if slide_index is None:

            return bool(
                retain_without_metadata
            )

        return (
            slide_index
            in retained_slide_indexes
        )

    # ==================================================
    # Resolve Slide Index
    # ==================================================

    @staticmethod
    def _resolve_slide_index(
        block: DocumentBlock,
        *,
        allow_missing: bool = False,
    ) -> int | None:

        metadata = (
            block.metadata
            or {}
        )

        slide_index = (
            metadata.get(
                "slide_index"
            )
        )

        if slide_index is not None:

            return int(
                slide_index
            )

        if (
            block.page_number
            is not None
        ):

            return max(
                int(
                    block.page_number
                )
                - 1,
                0,
            )

        if allow_missing:
            return None

        return 0

    # ==================================================
    # Reassign Block Order
    # ==================================================

    @classmethod
    def _reassign_block_order(
        cls,
        document: Document,
    ) -> None:

        sorted_blocks = sorted(
            document.blocks,
            key=cls._block_sort_key,
        )

        for (
            new_order,
            block,
        ) in enumerate(
            sorted_blocks
        ):

            block.order = (
                new_order
            )

        document.blocks = (
            sorted_blocks
        )

    # ==================================================
    # Rebuild Pages
    # ==================================================

    @classmethod
    def _rebuild_pages(
        cls,
        *,
        document: Document,
        retained_slide_indexes: set[int],
        slide_records: list[
            dict[str, Any]
        ],
    ) -> None:
        """
        每个保留 Slide 对应一个逻辑 Page。

        page_number：
            重新从 1 连续编号。

        slide_index：
            保留原始 0-based Slide Index。

        slide_number：
            保留原始 PowerPoint Slide Number。
        """

        blocks_by_slide = (
            cls._group_blocks_by_slide(
                document.blocks
            )
        )

        record_map = {
            int(
                record[
                    "slide_index"
                ]
            ): record
            for record
            in slide_records
        }

        rebuilt_pages: list[
            Page
        ] = []

        for (
            logical_page_number,
            slide_index,
        ) in enumerate(
            sorted(
                retained_slide_indexes
            ),
            start=1,
        ):

            slide_blocks = sorted(
                blocks_by_slide.get(
                    slide_index,
                    [],
                ),
                key=cls._block_sort_key,
            )

            text = "\n".join(
                str(
                    block.text
                    or ""
                ).strip()
                for block
                in slide_blocks
                if str(
                    block.text
                    or ""
                ).strip()
            ).strip()

            rebuilt_pages.append(
                Page(
                    page_number=(
                        logical_page_number
                    ),
                    text=text,
                )
            )

            slide_record = (
                record_map.get(
                    slide_index
                )
            )

            original_slide_number = (
                int(
                    slide_record.get(
                        "slide_number",
                        slide_index + 1,
                    )
                )
                if slide_record
                else slide_index + 1
            )

            for block in slide_blocks:

                block.page_number = (
                    logical_page_number
                )

                block.metadata.update(
                    {
                        "slide_index": (
                            slide_index
                        ),
                        "slide_number": (
                            original_slide_number
                        ),
                        "logical_page_number": (
                            logical_page_number
                        ),
                    }
                )

        document.pages = (
            rebuilt_pages
        )

    # ==================================================
    # Update Slide Metadata
    # ==================================================

    @classmethod
    def _update_slide_metadata(
        cls,
        *,
        document: Document,
        retained_slide_indexes: set[int],
        removed_slides: list[
            dict[str, Any]
        ],
        slide_records: list[
            dict[str, Any]
        ],
    ) -> None:

        blocks_by_slide = (
            cls._group_blocks_by_slide(
                document.blocks
            )
        )

        updated_records: list[
            dict[str, Any]
        ] = []

        logical_page_number = 1

        for record in sorted(
            slide_records,
            key=lambda item: int(
                item[
                    "slide_index"
                ]
            ),
        ):

            slide_index = int(
                record[
                    "slide_index"
                ]
            )

            if (
                slide_index
                not in retained_slide_indexes
            ):
                continue

            updated = dict(
                record
            )

            slide_blocks = (
                blocks_by_slide.get(
                    slide_index,
                    [],
                )
            )

            updated[
                "logical_page_number"
            ] = (
                logical_page_number
            )

            updated[
                "block_count"
            ] = len(
                slide_blocks
            )

            updated[
                "character_count"
            ] = sum(
                len(
                    block.text
                    or ""
                )
                for block
                in slide_blocks
            )

            updated[
                "status"
            ] = "SUCCESS"

            updated_records.append(
                updated
            )

            logical_page_number += 1

        document.metadata[
            "slides"
        ] = (
            updated_records
        )

        document.metadata[
            "processed_slide_count"
        ] = len(
            retained_slide_indexes
        )

        # ==============================================
        # Idempotent skipped_slide_count
        # ==============================================
        #
        # 第一次：
        #
        #   loader skipped = 2
        #   filter removed = 3
        #   -> 5
        #
        # 第二次重复执行：
        #
        #   先减掉上一次 filter removed，
        #   防止变成 8。

        previous_removed_count = int(
            document.metadata.get(
                "slide_filter_removed_count",
                0,
            )
            or 0
        )

        current_skipped_count = int(
            document.metadata.get(
                "skipped_slide_count",
                0,
            )
            or 0
        )

        loader_skipped_count = max(
            current_skipped_count
            - previous_removed_count,
            0,
        )

        document.metadata[
            "skipped_slide_count"
        ] = (
            loader_skipped_count
            + len(
                removed_slides
            )
        )

        document.metadata[
            "block_count"
        ] = len(
            document.blocks
        )

        document.metadata[
            "page_count"
        ] = len(
            document.pages
        )

        document.metadata[
            "character_count"
        ] = sum(
            len(
                page.text
                or ""
            )
            for page
            in document.pages
        )

    # ==================================================
    # Sort Key
    # ==================================================

    @classmethod
    def _block_sort_key(
        cls,
        block: DocumentBlock,
    ) -> tuple[
        int,
        int,
        int,
        int,
    ]:

        metadata = (
            block.metadata
            or {}
        )

        slide_index = (
            cls._resolve_slide_index(
                block
            )
            or 0
        )

        visual_index = (
            cls._safe_int(
                metadata.get(
                    "visual_index",
                    0,
                ),
                default=0,
            )
        )

        paragraph_or_row_index = (
            cls._safe_int(
                metadata.get(
                    "paragraph_index",
                    metadata.get(
                        "table_row_index",
                        0,
                    ),
                ),
                default=0,
            )
        )

        return (
            slide_index,
            visual_index,
            paragraph_or_row_index,
            int(
                block.order
            ),
        )

    # ==================================================
    # Title Pattern
    # ==================================================

    def _matches_excluded_title_pattern(
        self,
        title: str,
    ) -> bool:

        return any(
            pattern.search(
                title
            )
            is not None
            for pattern
            in self.exclude_title_patterns
        )

    # ==================================================
    # Normalize Set
    # ==================================================

    def _normalize_text_set(
        self,
        values: Iterable[str] | None,
    ) -> set[str]:

        if values is None:
            return set()

        normalized_values: set[
            str
        ] = set()

        for value in values:

            normalized = (
                self._normalize_key(
                    value
                )
            )

            if normalized:

                normalized_values.add(
                    normalized
                )

        return normalized_values

    # ==================================================
    # Normalize Key
    # ==================================================

    def _normalize_key(
        self,
        value: Any,
    ) -> str:

        normalized = (
            self._normalize_text(
                value
            )
        )

        if self.case_sensitive:
            return normalized

        return (
            normalized.casefold()
        )

    # ==================================================
    # Normalize Text
    # ==================================================

    @staticmethod
    def _normalize_text(
        value: Any,
    ) -> str:

        if value is None:
            return ""

        normalized = str(
            value
        )

        normalized = (
            normalized.replace(
                "\u3000",
                " ",
            )
        )

        normalized = (
            normalized.replace(
                "\xa0",
                " ",
            )
        )

        for character in (
            "\u200b",
            "\u200c",
            "\u200d",
            "\u2060",
            "\ufeff",
        ):

            normalized = (
                normalized.replace(
                    character,
                    "",
                )
            )

        normalized = " ".join(
            normalized.split()
        )

        return (
            normalized.strip()
        )

    # ==================================================
    # Safe Int
    # ==================================================

    @staticmethod
    def _safe_int(
        value: Any,
        *,
        default: int = 0,
    ) -> int:

        try:

            return int(
                value
            )

        except (
            TypeError,
            ValueError,
        ):

            return default

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
                "SlideFilter expects an "
                "app.model.document.Document "
                "instance."
            )

        file_type = str(
            document.file_type
            or ""
        ).strip().lower()

        if file_type != "pptx":

            raise ValueError(
                "SlideFilter only accepts "
                "PPTX documents. "
                f"Received file_type: "
                f"{document.file_type}"
            )