from __future__ import annotations

import math
import re

from collections import Counter
from collections.abc import Iterable
from typing import Any

from app.model.block import (
    BlockType,
    DocumentBlock,
)
from app.model.document import Document
from app.model.page import Page


class ShapeFilterError(
    RuntimeError
):
    """
    PPTX Shape 过滤异常。
    """


class ShapeFilter:
    """
    PPTX Shape / Block 过滤器。

    负责：
        - 删除空文本块
        - 删除空表格行
        - 删除无有效信息的图片或图表块
        - 删除重复文本块
        - 删除边缘区域页码、日期、版本号、公司页脚
        - 删除跨页高频重复页眉页脚
        - 清洗文本和单元格
        - 保留 Slide 内视觉顺序
        - 重新分配 Block order
        - 重建 Page
        - 更新 metadata

    不负责：
        - 删除整个 Slide
        - OCR 图片
        - SmartArt 深层解析
        - Chapter / Section 建模
        - Chunk
        - Token 统计

    设计原则：
        - document.blocks 是 PPTX 的结构数据源
        - document.pages 是逻辑文本视图
        - 页眉页脚类噪声仅在 Slide 边缘区域删除
        - 去重主要针对文本 Block
        - Table / Image / Chart 不进行普通文本式去重
    """

    # ==================================================
    # Page Number
    # ==================================================

    _PAGE_NUMBER_PATTERNS = (
        re.compile(
            r"^\d+$"
        ),
        re.compile(
            r"^\d+\s*/\s*\d+$"
        ),
        re.compile(
            r"^page\s+\d+"
            r"(?:\s+of\s+\d+)?$",
            re.IGNORECASE,
        ),
        re.compile(
            r"^-\s*\d+\s*-$"
        ),
    )

    # ==================================================
    # Date
    # ==================================================

    _DATE_PATTERNS = (
        re.compile(
            r"^(?:19|20)\d{2}"
            r"[./-]\d{1,2}"
            r"[./-]\d{1,2}$"
        ),
        re.compile(
            r"^\d{1,2}"
            r"[./-]\d{1,2}"
            r"[./-](?:19|20)\d{2}$"
        ),
    )

    # ==================================================
    # Version
    # ==================================================

    _VERSION_PATTERNS = (
        re.compile(
            r"^[vVｖＶ]\s*"
            r"\d+(?:\.\d+)+$"
        ),
        re.compile(
            r"^(?:ver|version)"
            r"\.?\s*"
            r"\d+(?:\.\d+)+$",
            re.IGNORECASE,
        ),
        re.compile(
            r"^rev(?:ision)?"
            r"\.?\s*"
            r"[A-Za-z0-9._-]+$",
            re.IGNORECASE,
        ),
    )

    # ==================================================
    # Static Noise
    # ==================================================

    _STATIC_NOISE_PATTERNS = (
        re.compile(
            r"^TOYOTA\s+MOTOR\s+CORPORATION"
            r"(?:\s+[vVｖＶ]?"
            r"\d+(?:\.\d+)*)?$",
            re.IGNORECASE,
        ),
        re.compile(
            r"^DENSO(?:\s+TEN)?"
            r"\s+CORPORATION"
            r"(?:\s+[vVｖＶ]?"
            r"\d+(?:\.\d+)*)?$",
            re.IGNORECASE,
        ),
        re.compile(
            r"^CONFIDENTIAL$",
            re.IGNORECASE,
        ),
        re.compile(
            r"^PROPRIETARY$",
            re.IGNORECASE,
        ),
        re.compile(
            r"^INTERNAL\s+USE\s+ONLY$",
            re.IGNORECASE,
        ),
        re.compile(
            r"^COPYRIGHT\b.*$",
            re.IGNORECASE,
        ),
    )

    # ==================================================
    # Ignore Patterns
    # ==================================================

    _DEFAULT_IGNORE_PATTERNS = (
        re.compile(
            r"^(?:click to add title"
            r"|click to add text)$",
            re.IGNORECASE,
        ),
        re.compile(
            r"^(?:タイトルを入力"
            r"|テキストを入力)$",
            re.IGNORECASE,
        ),
        re.compile(
            r"^(?:单击此处添加标题"
            r"|单击此处添加文本)$",
            re.IGNORECASE,
        ),
    )

    # ==================================================
    # Text Block Types
    # ==================================================

    _TEXT_BLOCK_TYPES = {
        BlockType.HEADING,
        BlockType.PARAGRAPH,
        BlockType.LIST,
        BlockType.TEXTBOX,
    }

    # ==================================================
    # Constructor
    # ==================================================

    def __init__(
        self,
        *,
        remove_empty_text_blocks: bool = True,
        remove_empty_table_rows: bool = True,
        remove_empty_image_blocks: bool = False,
        remove_empty_chart_blocks: bool = False,
        remove_duplicate_blocks: bool = True,
        duplicate_scope: str = "slide",
        case_sensitive_duplicates: bool = False,
        remove_page_numbers: bool = True,
        remove_dates: bool = False,
        remove_version_lines: bool = True,
        remove_static_noise: bool = True,
        remove_repeated_headers_footers: bool = True,
        repeated_edge_ratio: float = 0.4,
        minimum_edge_repetition: int = 3,
        header_position_ratio: float = 0.15,
        footer_position_ratio: float = 0.85,
        minimum_text_length: int = 1,
        maximum_text_length: int | None = None,
        ignore_patterns: Iterable[
            str | re.Pattern[str]
        ] | None = None,
        clean_text: bool = True,
        clean_cells: bool = True,
        rebuild_pages: bool = True,
        reassign_block_order: bool = True,
    ) -> None:

        if duplicate_scope not in {
            "slide",
            "presentation",
        }:
            raise ValueError(
                "duplicate_scope must be either "
                "'slide' or 'presentation'."
            )

        if not (
            0
            < repeated_edge_ratio
            <= 1
        ):
            raise ValueError(
                "repeated_edge_ratio must be "
                "between 0 and 1."
            )

        if minimum_edge_repetition < 1:
            raise ValueError(
                "minimum_edge_repetition "
                "must be at least 1."
            )

        if not (
            0
            <= header_position_ratio
            < 1
        ):
            raise ValueError(
                "header_position_ratio must be "
                "between 0 and 1."
            )

        if not (
            0
            < footer_position_ratio
            <= 1
        ):
            raise ValueError(
                "footer_position_ratio must be "
                "between 0 and 1."
            )

        if (
            header_position_ratio
            >= footer_position_ratio
        ):
            raise ValueError(
                "header_position_ratio must be "
                "less than footer_position_ratio."
            )

        if minimum_text_length < 0:
            raise ValueError(
                "minimum_text_length "
                "cannot be negative."
            )

        if (
            maximum_text_length is not None
            and maximum_text_length <= 0
        ):
            raise ValueError(
                "maximum_text_length "
                "must be greater than 0."
            )

        if (
            maximum_text_length is not None
            and maximum_text_length
            < minimum_text_length
        ):
            raise ValueError(
                "maximum_text_length cannot "
                "be less than minimum_text_length."
            )

        self.remove_empty_text_blocks = (
            remove_empty_text_blocks
        )

        self.remove_empty_table_rows = (
            remove_empty_table_rows
        )

        self.remove_empty_image_blocks = (
            remove_empty_image_blocks
        )

        self.remove_empty_chart_blocks = (
            remove_empty_chart_blocks
        )

        self.remove_duplicate_blocks = (
            remove_duplicate_blocks
        )

        self.duplicate_scope = (
            duplicate_scope
        )

        self.case_sensitive_duplicates = (
            case_sensitive_duplicates
        )

        self.remove_page_numbers = (
            remove_page_numbers
        )

        self.remove_dates = (
            remove_dates
        )

        self.remove_version_lines = (
            remove_version_lines
        )

        self.remove_static_noise = (
            remove_static_noise
        )

        self.remove_repeated_headers_footers = (
            remove_repeated_headers_footers
        )

        self.repeated_edge_ratio = (
            repeated_edge_ratio
        )

        self.minimum_edge_repetition = (
            minimum_edge_repetition
        )

        self.header_position_ratio = (
            header_position_ratio
        )

        self.footer_position_ratio = (
            footer_position_ratio
        )

        self.minimum_text_length = (
            minimum_text_length
        )

        self.maximum_text_length = (
            maximum_text_length
        )

        self.clean_text = clean_text

        self.clean_cells = clean_cells

        self.rebuild_pages = (
            rebuild_pages
        )

        self.reassign_block_order = (
            reassign_block_order
        )

        patterns: list[
            re.Pattern[str]
        ] = list(
            self._DEFAULT_IGNORE_PATTERNS
        )

        if ignore_patterns:

            for pattern in ignore_patterns:

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
                            re.IGNORECASE,
                        )
                    )

        self.ignore_patterns = tuple(
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

            original_block_count = len(
                document.blocks
            )

            repeated_edge_texts: set[str] = (
                set()
            )

            if (
                self.remove_repeated_headers_footers
            ):
                repeated_edge_texts = (
                    self._find_repeated_edge_texts(
                        document
                    )
                )

            presentation_seen: set[str] = (
                set()
            )

            slide_seen: dict[
                int,
                set[str],
            ] = {}

            retained_blocks: list[
                DocumentBlock
            ] = []

            removed_empty_text_count = 0

            removed_empty_table_count = 0

            removed_empty_image_count = 0

            removed_empty_chart_count = 0

            removed_duplicate_count = 0

            removed_page_number_count = 0

            removed_date_count = 0

            removed_version_count = 0

            removed_static_noise_count = 0

            removed_repeated_edge_count = 0

            removed_pattern_count = 0

            removed_short_count = 0

            removed_long_count = 0

            for block in sorted(
                document.blocks,
                key=self._block_sort_key,
            ):

                updated_block = (
                    block.model_copy(
                        deep=True
                    )
                )

                # ======================================
                # Normalize Text
                # ======================================

                if self.clean_text:
                    updated_block.text = (
                        self._normalize_text(
                            updated_block.text
                        )
                    )

                # ======================================
                # Normalize Cells
                # ======================================

                if (
                    self.clean_cells
                    and updated_block.cells
                ):

                    updated_block.cells = [
                        self._normalize_text(
                            cell
                        )
                        for cell
                        in updated_block.cells
                    ]

                    updated_block.cells = (
                        self._trim_empty_boundaries(
                            updated_block.cells
                        )
                    )

                    if (
                        updated_block.block_type
                        == BlockType.TABLE
                    ):
                        updated_block.text = (
                            " | ".join(
                                updated_block.cells
                            ).strip()
                        )

                text = (
                    updated_block.text.strip()
                )

                content_kind = str(
                    updated_block.metadata.get(
                        "content_kind",
                        "",
                    )
                ).strip().lower()

                # ======================================
                # Empty Text Blocks
                # ======================================

                if (
                    updated_block.block_type
                    in self._TEXT_BLOCK_TYPES
                    and not text
                    and self.remove_empty_text_blocks
                ):
                    removed_empty_text_count += 1
                    continue

                # ======================================
                # Empty Table
                # ======================================

                if (
                    updated_block.block_type
                    == BlockType.TABLE
                    and not any(
                        updated_block.cells
                    )
                    and self.remove_empty_table_rows
                ):
                    removed_empty_table_count += 1
                    continue

                # ======================================
                # Empty Image
                # ======================================

                if (
                    updated_block.block_type
                    == BlockType.IMAGE
                    and not text
                    and self.remove_empty_image_blocks
                ):
                    removed_empty_image_count += 1
                    continue

                # ======================================
                # Empty Chart
                # ======================================

                if (
                    content_kind == "chart"
                    and not text
                    and self.remove_empty_chart_blocks
                ):
                    removed_empty_chart_count += 1
                    continue

                # ======================================
                # Important Empty Non-text Block
                # ======================================
                #
                # Image / Chart 可能没有 text，
                # 但 metadata 中仍然存在重要信息。
                #
                # 此类 Block 默认保留。

                if not text:
                    retained_blocks.append(
                        updated_block
                    )
                    continue

                # ======================================
                # Text Length
                # ======================================

                if (
                    len(text)
                    < self.minimum_text_length
                ):
                    removed_short_count += 1
                    continue

                if (
                    self.maximum_text_length
                    is not None
                    and len(text)
                    > self.maximum_text_length
                ):
                    removed_long_count += 1
                    continue

                # ======================================
                # Explicit Ignore Patterns
                # ======================================

                if self._matches_patterns(
                    text,
                    self.ignore_patterns,
                ):
                    removed_pattern_count += 1
                    continue

                # ======================================
                # Edge Position
                # ======================================

                is_edge_block = (
                    self._is_edge_block(
                        updated_block,
                        document,
                    )
                )

                # ======================================
                # Page Number
                # ======================================
                #
                # 只在边缘区域删除。
                #
                # 避免 Slide 正文中：
                #
                #   1
                #
                # 被误当成页码删除。

                if (
                    self.remove_page_numbers
                    and is_edge_block
                    and self._matches_patterns(
                        text,
                        self._PAGE_NUMBER_PATTERNS,
                    )
                ):
                    removed_page_number_count += 1
                    continue

                # ======================================
                # Date
                # ======================================

                if (
                    self.remove_dates
                    and is_edge_block
                    and self._matches_patterns(
                        text,
                        self._DATE_PATTERNS,
                    )
                ):
                    removed_date_count += 1
                    continue

                # ======================================
                # Version
                # ======================================

                if (
                    self.remove_version_lines
                    and is_edge_block
                    and self._matches_patterns(
                        text,
                        self._VERSION_PATTERNS,
                    )
                ):
                    removed_version_count += 1
                    continue

                # ======================================
                # Static Noise
                # ======================================

                if (
                    self.remove_static_noise
                    and is_edge_block
                    and self._matches_patterns(
                        text,
                        self._STATIC_NOISE_PATTERNS,
                    )
                ):
                    removed_static_noise_count += 1
                    continue

                # ======================================
                # Repeated Header / Footer
                # ======================================

                normalized_key = (
                    self._normalize_duplicate_key(
                        text
                    )
                )

                if (
                    repeated_edge_texts
                    and normalized_key
                    in repeated_edge_texts
                    and is_edge_block
                ):
                    removed_repeated_edge_count += 1
                    continue

                # ======================================
                # Duplicate
                # ======================================
                #
                # 只对真正的文本类 Block 去重。
                #
                # TABLE / IMAGE / CHART 不使用普通
                # text key 去重，否则可能误删结构数据。

                if (
                    self.remove_duplicate_blocks
                    and updated_block.block_type
                    in self._TEXT_BLOCK_TYPES
                ):

                    slide_index = (
                        self._resolve_slide_index(
                            updated_block
                        )
                    )

                    if (
                        self.duplicate_scope
                        == "presentation"
                    ):

                        if (
                            normalized_key
                            in presentation_seen
                        ):
                            removed_duplicate_count += 1
                            continue

                        presentation_seen.add(
                            normalized_key
                        )

                    else:

                        seen = (
                            slide_seen.setdefault(
                                slide_index,
                                set(),
                            )
                        )

                        if normalized_key in seen:
                            removed_duplicate_count += 1
                            continue

                        seen.add(
                            normalized_key
                        )

                # ======================================
                # Retain
                # ======================================

                updated_block.metadata.update(
                    {
                        "shape_filter_status": (
                            "RETAINED"
                        ),
                        "normalized_text_length": (
                            len(
                                text
                            )
                        ),
                    }
                )

                retained_blocks.append(
                    updated_block
                )

            # ==========================================
            # Reassign Block Order
            # ==========================================

            if self.reassign_block_order:

                for order, block in enumerate(
                    retained_blocks
                ):
                    block.order = order

            document.blocks = (
                retained_blocks
            )

            # ==========================================
            # Rebuild Pages
            # ==========================================

            if self.rebuild_pages:
                self._rebuild_pages(
                    document
                )

            # ==========================================
            # Update Slide Metadata
            # ==========================================

            self._update_slide_metadata(
                document
            )

            # ==========================================
            # Filter Metadata
            # ==========================================

            document.metadata.update(
                {
                    "shape_filter": (
                        "ShapeFilter"
                    ),

                    "shape_filter_status": (
                        "SUCCESS"
                    ),

                    "shape_filter_original_block_count": (
                        original_block_count
                    ),

                    "shape_filter_retained_block_count": (
                        len(
                            retained_blocks
                        )
                    ),

                    "shape_filter_removed_block_count": (
                        original_block_count
                        - len(
                            retained_blocks
                        )
                    ),

                    "shape_filter_removed_empty_text_count": (
                        removed_empty_text_count
                    ),

                    "shape_filter_removed_empty_table_count": (
                        removed_empty_table_count
                    ),

                    "shape_filter_removed_empty_image_count": (
                        removed_empty_image_count
                    ),

                    "shape_filter_removed_empty_chart_count": (
                        removed_empty_chart_count
                    ),

                    "shape_filter_removed_duplicate_count": (
                        removed_duplicate_count
                    ),

                    "shape_filter_removed_page_number_count": (
                        removed_page_number_count
                    ),

                    "shape_filter_removed_date_count": (
                        removed_date_count
                    ),

                    "shape_filter_removed_version_count": (
                        removed_version_count
                    ),

                    "shape_filter_removed_static_noise_count": (
                        removed_static_noise_count
                    ),

                    "shape_filter_removed_repeated_edge_count": (
                        removed_repeated_edge_count
                    ),

                    "shape_filter_removed_pattern_count": (
                        removed_pattern_count
                    ),

                    "shape_filter_removed_short_count": (
                        removed_short_count
                    ),

                    "shape_filter_removed_long_count": (
                        removed_long_count
                    ),

                    "shape_filter_repeated_edge_candidate_count": (
                        len(
                            repeated_edge_texts
                        )
                    ),
                }
            )

            return document

        except ShapeFilterError:
            raise

        except Exception as exc:
            raise ShapeFilterError(
                "Failed to filter PPTX "
                f"shapes: {exc}"
            ) from exc

    # ==================================================
    # Repeated Header / Footer
    # ==================================================

    def _find_repeated_edge_texts(
        self,
        document: Document,
    ) -> set[str]:

        slide_indexes = {
            self._resolve_slide_index(
                block
            )
            for block
            in document.blocks
        }

        slide_count = len(
            slide_indexes
        )

        if slide_count < 3:
            return set()

        counter: Counter[str] = (
            Counter()
        )

        per_slide_seen: dict[
            int,
            set[str],
        ] = {}

        for block in document.blocks:

            text = self._normalize_text(
                block.text
            )

            if not text:
                continue

            if not self._is_edge_block(
                block,
                document,
            ):
                continue

            slide_index = (
                self._resolve_slide_index(
                    block
                )
            )

            key = (
                self._normalize_duplicate_key(
                    text
                )
            )

            if not key:
                continue

            seen = (
                per_slide_seen.setdefault(
                    slide_index,
                    set(),
                )
            )

            if key in seen:
                continue

            seen.add(
                key
            )

            counter[
                key
            ] += 1

        # ==============================================
        # Threshold
        # ==============================================
        #
        # 使用 ceil：
        #
        # 8 slides * 0.4 = 3.2
        #
        # 必须至少出现 4 张 Slide。
        #
        # 不能使用 int(3.2)=3。

        threshold = max(
            self.minimum_edge_repetition,
            math.ceil(
                slide_count
                * self.repeated_edge_ratio
            ),
        )

        return {
            text
            for text, count
            in counter.items()
            if count >= threshold
        }

    # ==================================================
    # Edge Detection
    # ==================================================

    def _is_edge_block(
        self,
        block: DocumentBlock,
        document: Document,
    ) -> bool:

        metadata = (
            block.metadata
            or {}
        )

        top = metadata.get(
            "top_emu"
        )

        height = metadata.get(
            "height_emu"
        )

        slide_height = (
            document.metadata.get(
                "slide_height_emu"
            )
        )

        if (
            top is None
            or slide_height is None
        ):
            return False

        try:

            top_value = float(
                top
            )

            height_value = float(
                height
                or 0
            )

            slide_height_value = float(
                slide_height
            )

        except (
            TypeError,
            ValueError,
        ):
            return False

        if slide_height_value <= 0:
            return False

        top_ratio = (
            top_value
            / slide_height_value
        )

        bottom_ratio = (
            (
                top_value
                + height_value
            )
            / slide_height_value
        )

        return (
            top_ratio
            <= self.header_position_ratio
            or bottom_ratio
            >= self.footer_position_ratio
        )

    # ==================================================
    # Rebuild Pages
    # ==================================================

    @classmethod
    def _rebuild_pages(
        cls,
        document: Document,
    ) -> None:

        blocks_by_slide: dict[
            int,
            list[DocumentBlock],
        ] = {}

        for block in document.blocks:

            slide_index = (
                cls._resolve_slide_index(
                    block
                )
            )

            blocks_by_slide.setdefault(
                slide_index,
                [],
            ).append(
                block
            )

        rebuilt_pages: list[
            Page
        ] = []

        for (
            logical_page_number,
            slide_index,
        ) in enumerate(
            sorted(
                blocks_by_slide
            ),
            start=1,
        ):

            slide_blocks = sorted(
                blocks_by_slide[
                    slide_index
                ],
                key=cls._block_sort_key,
            )

            text = "\n".join(
                block.text.strip()
                for block
                in slide_blocks
                if (
                    block.text
                    and block.text.strip()
                )
            ).strip()

            rebuilt_pages.append(
                Page(
                    page_number=(
                        logical_page_number
                    ),
                    text=text,
                )
            )

            for block in slide_blocks:

                block.page_number = (
                    logical_page_number
                )

                block.metadata[
                    "logical_page_number"
                ] = (
                    logical_page_number
                )

        document.pages = (
            rebuilt_pages
        )

    # ==================================================
    # Slide Metadata
    # ==================================================

    @classmethod
    def _update_slide_metadata(
        cls,
        document: Document,
    ) -> None:

        block_counts: dict[
            int,
            int,
        ] = {}

        character_counts: dict[
            int,
            int,
        ] = {}

        for block in document.blocks:

            slide_index = (
                cls._resolve_slide_index(
                    block
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

        raw_records = (
            document.metadata.get(
                "slides",
                [],
            )
        )

        if isinstance(
            raw_records,
            list,
        ):

            updated_records: list[
                dict[str, Any]
            ] = []

            logical_page_number = 1

            for record in raw_records:

                if not isinstance(
                    record,
                    dict,
                ):
                    continue

                slide_index = (
                    record.get(
                        "slide_index"
                    )
                )

                if slide_index is None:
                    continue

                try:
                    slide_index = int(
                        slide_index
                    )

                except (
                    TypeError,
                    ValueError,
                ):
                    continue

                if (
                    slide_index
                    not in block_counts
                ):
                    continue

                updated = dict(
                    record
                )

                updated[
                    "logical_page_number"
                ] = (
                    logical_page_number
                )

                updated[
                    "block_count"
                ] = (
                    block_counts[
                        slide_index
                    ]
                )

                updated[
                    "character_count"
                ] = (
                    character_counts[
                        slide_index
                    ]
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
            block_counts
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
    # Resolve Slide Index
    # ==================================================

    @staticmethod
    def _resolve_slide_index(
        block: DocumentBlock,
    ) -> int:

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

            try:
                return int(
                    slide_index
                )

            except (
                TypeError,
                ValueError,
            ):
                pass

        slide_number = (
            metadata.get(
                "slide_number"
            )
        )

        if slide_number is not None:

            try:
                return max(
                    int(
                        slide_number
                    )
                    - 1,
                    0,
                )

            except (
                TypeError,
                ValueError,
            ):
                pass

        if (
            block.page_number
            is not None
        ):

            try:
                return max(
                    int(
                        block.page_number
                    )
                    - 1,
                    0,
                )

            except (
                TypeError,
                ValueError,
            ):
                pass

        return 0

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
        )

        try:
            visual_index = int(
                metadata.get(
                    "visual_index",
                    0,
                )
                or 0
            )

        except (
            TypeError,
            ValueError,
        ):
            visual_index = 0

        raw_paragraph_or_row_index = (
            metadata.get(
                "paragraph_index",
                metadata.get(
                    "table_row_index",
                    0,
                ),
            )
        )

        try:
            paragraph_or_row_index = (
                int(
                    raw_paragraph_or_row_index
                    or 0
                )
            )

        except (
            TypeError,
            ValueError,
        ):
            paragraph_or_row_index = 0

        return (
            slide_index,
            visual_index,
            paragraph_or_row_index,
            block.order,
        )

    # ==================================================
    # Duplicate Key
    # ==================================================

    def _normalize_duplicate_key(
        self,
        text: str,
    ) -> str:

        normalized = (
            self._normalize_text(
                text
            )
        )

        if (
            not self.case_sensitive_duplicates
        ):
            normalized = (
                normalized.casefold()
            )

        return normalized

    # ==================================================
    # Match Patterns
    # ==================================================

    @staticmethod
    def _matches_patterns(
        text: str,
        patterns: Iterable[
            re.Pattern[str]
        ],
    ) -> bool:

        return any(
            pattern.fullmatch(
                text
            )
            is not None
            for pattern
            in patterns
        )

    # ==================================================
    # Trim Cells
    # ==================================================

    @staticmethod
    def _trim_empty_boundaries(
        values: list[str],
    ) -> list[str]:

        if not values:
            return []

        start = 0

        end = len(
            values
        )

        while (
            start < end
            and not values[
                start
            ]
        ):
            start += 1

        while (
            end > start
            and not values[
                end - 1
            ]
        ):
            end -= 1

        return values[
            start:end
        ]

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

        # Full-width Space
        normalized = (
            normalized.replace(
                "\u3000",
                " ",
            )
        )

        # NBSP
        normalized = (
            normalized.replace(
                "\xa0",
                " ",
            )
        )

        # Zero-width Characters
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

        normalized = (
            normalized.replace(
                "\r\n",
                "\n",
            )
        )

        normalized = (
            normalized.replace(
                "\r",
                "\n",
            )
        )

        lines = [
            " ".join(
                line.split()
            )
            for line
            in normalized.splitlines()
            if line.strip()
        ]

        return "\n".join(
            lines
        ).strip()

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
                "ShapeFilter expects an "
                "app.model.document.Document "
                "instance."
            )

        file_type = str(
            document.file_type
            or ""
        ).strip().lower()

        if file_type != "pptx":
            raise ValueError(
                "ShapeFilter only accepts "
                "PPTX documents. "
                f"Received file_type: "
                f"{document.file_type}"
            )

        if not document.blocks:
            raise ValueError(
                "PPTX document contains "
                "no blocks."
            )