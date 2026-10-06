from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from app.model.block import (
    BlockType,
    DocumentBlock,
)
from app.model.document import Document
from app.model.page import Page


class RowFilter:
    """
    XLSX 行过滤器。

    负责：
        - 删除空行
        - 删除仅包含空白字符的行
        - 可选删除重复行
        - 可选删除备注 / 注释行
        - 可选删除合计 / 小计行
        - 可选删除只包含 Excel Error 的行
        - 清理 Cell 文本
        - 保留 Cell 列位置
        - 保留各 Sheet 内原始行顺序
        - 保留非 TABLE Block
        - 重建 Document.pages
        - 同步 logical_page_number
        - 更新 Sheet metadata
        - 更新 RowFilter metadata

    不负责：
        - Sheet 过滤
        - 表头识别
        - 表格区域识别
        - Chapter / Section 建模
        - Chunk
        - Token 统计

    设计原则：
        XLSX 是结构化数据。

        默认不删除重复数据行，因为：

            A | 100
            A | 100

        可能是两条合法业务记录，而不是脏数据。

        同时默认保留行首 / 行尾的空 Cell，
        防止列位置发生偏移。
    """

    # ==================================================
    # Excel Errors
    # ==================================================

    _EXCEL_ERROR_VALUES = {
        "#NULL!",
        "#DIV/0!",
        "#VALUE!",
        "#REF!",
        "#NAME?",
        "#NUM!",
        "#N/A",
        "#GETTING_DATA",
        "#SPILL!",
        "#CALC!",
        "#FIELD!",
        "#BLOCKED!",
        "#UNKNOWN!",
    }

    # ==================================================
    # Comment Patterns
    # ==================================================

    _DEFAULT_COMMENT_PATTERNS = (
        re.compile(
            r"^(?:note|notes|comment|comments)"
            r"\s*[:：]?",
            re.IGNORECASE,
        ),
        re.compile(
            r"^(?:備考|注記|注意|メモ|コメント)"
            r"\s*[:：]?",
            re.IGNORECASE,
        ),
        re.compile(
            r"^(?:备注|说明|注释|注意事项)"
            r"\s*[:：]?",
            re.IGNORECASE,
        ),
    )

    # ==================================================
    # Summary Patterns
    # ==================================================

    _DEFAULT_SUMMARY_PATTERNS = (
        re.compile(
            r"^(?:total|subtotal|grand total)\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"^(?:合計|小計|総計)\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"^(?:合计|小计|总计)\b",
            re.IGNORECASE,
        ),
    )

    # ==================================================
    # Constructor
    # ==================================================

    def __init__(
        self,
        *,
        remove_empty_rows: bool = True,

        # XLSX 中重复行可能是合法业务数据。
        # 因此默认关闭。
        remove_duplicate_rows: bool = False,

        remove_error_only_rows: bool = True,
        remove_comment_rows: bool = False,
        remove_summary_rows: bool = False,

        minimum_non_empty_cells: int = 1,
        minimum_text_length: int = 1,
        maximum_text_length: int | None = None,

        duplicate_scope: str = "sheet",
        case_sensitive_duplicates: bool = True,

        strip_cells: bool = True,
        collapse_internal_spaces: bool = True,

        comment_patterns: Iterable[
            str | re.Pattern[str]
        ] | None = None,

        summary_patterns: Iterable[
            str | re.Pattern[str]
        ] | None = None,

        rebuild_pages: bool = True,
        reassign_block_order: bool = True,

        # 默认禁止裁剪边界空 Cell。
        # 避免：
        #
        #     ["", "B", "C"]
        #
        # 变成：
        #
        #     ["B", "C"]
        #
        # 导致列位置改变。
        trim_empty_boundaries: bool = False,
    ) -> None:

        if minimum_non_empty_cells < 0:
            raise ValueError(
                "minimum_non_empty_cells "
                "cannot be negative."
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

        if duplicate_scope not in {
            "sheet",
            "workbook",
        }:
            raise ValueError(
                "duplicate_scope must be either "
                "'sheet' or 'workbook'."
            )

        self.remove_empty_rows = (
            remove_empty_rows
        )

        self.remove_duplicate_rows = (
            remove_duplicate_rows
        )

        self.remove_error_only_rows = (
            remove_error_only_rows
        )

        self.remove_comment_rows = (
            remove_comment_rows
        )

        self.remove_summary_rows = (
            remove_summary_rows
        )

        self.minimum_non_empty_cells = (
            minimum_non_empty_cells
        )

        self.minimum_text_length = (
            minimum_text_length
        )

        self.maximum_text_length = (
            maximum_text_length
        )

        self.duplicate_scope = (
            duplicate_scope
        )

        self.case_sensitive_duplicates = (
            case_sensitive_duplicates
        )

        self.strip_cells = (
            strip_cells
        )

        self.collapse_internal_spaces = (
            collapse_internal_spaces
        )

        self.rebuild_pages = (
            rebuild_pages
        )

        self.reassign_block_order = (
            reassign_block_order
        )

        self.trim_empty_boundaries = (
            trim_empty_boundaries
        )

        self.comment_patterns = (
            self._compile_patterns(
                comment_patterns,
                defaults=(
                    self._DEFAULT_COMMENT_PATTERNS
                ),
            )
        )

        self.summary_patterns = (
            self._compile_patterns(
                summary_patterns,
                defaults=(
                    self._DEFAULT_SUMMARY_PATTERNS
                ),
            )
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

        original_block_count = len(
            document.blocks
        )

        retained_blocks: list[
            DocumentBlock
        ] = []

        # ==============================================
        # Duplicate State
        # ==============================================

        workbook_seen: set[str] = set()

        sheet_seen: dict[
            int,
            set[str],
        ] = {}

        # ==============================================
        # Statistics
        # ==============================================

        processed_row_count = 0

        retained_row_count = 0

        skipped_non_table_count = 0

        removed_empty_count = 0

        removed_duplicate_count = 0

        removed_error_count = 0

        removed_comment_count = 0

        removed_summary_count = 0

        removed_short_count = 0

        removed_long_count = 0

        # ==============================================
        # Process Blocks
        # ==============================================

        for block in sorted(
            document.blocks,
            key=self._block_sort_key,
        ):

            # ==========================================
            # Non TABLE Block
            # ==========================================
            #
            # RowFilter 的职责只是处理 Row。
            #
            # 不能因为 Block 不是 TABLE 就直接删除。
            # 后续 XLSX Loader 如果加入：
            #
            #     IMAGE
            #     CHART
            #     TEXTBOX
            #
            # 此处依然能够兼容。

            if (
                block.block_type
                != BlockType.TABLE
            ):

                skipped_non_table_count += 1

                retained_blocks.append(
                    block.model_copy(
                        deep=True
                    )
                )

                continue

            processed_row_count += 1

            # ==========================================
            # Normalize Cells
            # ==========================================

            normalized_cells = [
                self._normalize_cell(
                    cell
                )
                for cell
                in block.cells
            ]

            # ==========================================
            # Preserve Column Positions
            # ==========================================

            if self.trim_empty_boundaries:

                normalized_cells = (
                    self._trim_empty_boundaries(
                        normalized_cells
                    )
                )

            # ==========================================
            # Non Empty Cells
            # ==========================================

            non_empty_cells = [
                cell
                for cell
                in normalized_cells
                if cell
            ]

            # ==========================================
            # Empty Row
            # ==========================================

            if (
                self.remove_empty_rows
                and not non_empty_cells
            ):

                removed_empty_count += 1

                continue

            if (
                len(
                    non_empty_cells
                )
                < self.minimum_non_empty_cells
            ):

                removed_empty_count += 1

                continue

            # ==========================================
            # Build Row Text
            # ==========================================

            row_text = (
                self._build_row_text(
                    normalized_cells
                )
            )

            # ==========================================
            # Minimum Length
            # ==========================================

            if (
                len(
                    row_text
                )
                < self.minimum_text_length
            ):

                removed_short_count += 1

                continue

            # ==========================================
            # Maximum Length
            # ==========================================

            if (
                self.maximum_text_length
                is not None
                and len(
                    row_text
                )
                > self.maximum_text_length
            ):

                removed_long_count += 1

                continue

            # ==========================================
            # Excel Error Only Row
            # ==========================================

            if (
                self.remove_error_only_rows
                and self._is_error_only_row(
                    non_empty_cells
                )
            ):

                removed_error_count += 1

                continue

            # ==========================================
            # Comment Row
            # ==========================================

            if (
                self.remove_comment_rows
                and self._matches_patterns(
                    row_text,
                    self.comment_patterns,
                )
            ):

                removed_comment_count += 1

                continue

            # ==========================================
            # Summary Row
            # ==========================================

            if (
                self.remove_summary_rows
                and self._matches_patterns(
                    row_text,
                    self.summary_patterns,
                )
            ):

                removed_summary_count += 1

                continue

            # ==========================================
            # Duplicate Row
            # ==========================================

            if self.remove_duplicate_rows:

                duplicate_key = (
                    self._build_duplicate_key(
                        normalized_cells
                    )
                )

                sheet_index = (
                    self._resolve_sheet_index(
                        block
                    )
                )

                if (
                    self.duplicate_scope
                    == "workbook"
                ):

                    if (
                        duplicate_key
                        in workbook_seen
                    ):

                        removed_duplicate_count += 1

                        continue

                    workbook_seen.add(
                        duplicate_key
                    )

                else:

                    seen = (
                        sheet_seen.setdefault(
                            sheet_index,
                            set(),
                        )
                    )

                    if duplicate_key in seen:

                        removed_duplicate_count += 1

                        continue

                    seen.add(
                        duplicate_key
                    )

            # ==========================================
            # Update Block
            # ==========================================

            updated_block = (
                block.model_copy(
                    deep=True
                )
            )

            updated_block.cells = (
                normalized_cells
            )

            updated_block.text = (
                row_text
            )

            updated_block.metadata = {
                **updated_block.metadata,

                "row_filter_status": (
                    "RETAINED"
                ),

                "non_empty_cell_count": (
                    len(
                        non_empty_cells
                    )
                ),

                "normalized_column_count": (
                    len(
                        normalized_cells
                    )
                ),

                "column_position_preserved": (
                    not self.trim_empty_boundaries
                ),
            }

            retained_blocks.append(
                updated_block
            )

            retained_row_count += 1

        # ==============================================
        # Reassign Block Order
        # ==============================================

        if self.reassign_block_order:

            retained_blocks = sorted(
                retained_blocks,
                key=self._block_sort_key,
            )

            for (
                order,
                block,
            ) in enumerate(
                retained_blocks
            ):

                block.order = (
                    order
                )

        document.blocks = (
            retained_blocks
        )

        # ==============================================
        # Rebuild Logical Pages
        # ==============================================

        if self.rebuild_pages:

            self._rebuild_pages(
                document
            )

        # ==============================================
        # Sheet Metadata
        # ==============================================

        self._update_sheet_metadata(
            document
        )

        # ==============================================
        # Filter Metadata
        # ==============================================

        document.metadata.update(
            {
                "row_filter": (
                    "RowFilter"
                ),

                "row_filter_status": (
                    "SUCCESS"
                ),

                "row_filter_original_block_count": (
                    original_block_count
                ),

                "row_filter_retained_block_count": (
                    len(
                        retained_blocks
                    )
                ),

                "row_filter_removed_block_count": (
                    original_block_count
                    - len(
                        retained_blocks
                    )
                ),

                "row_filter_processed_row_count": (
                    processed_row_count
                ),

                "row_filter_retained_row_count": (
                    retained_row_count
                ),

                "row_filter_skipped_non_table_count": (
                    skipped_non_table_count
                ),

                "row_filter_removed_empty_count": (
                    removed_empty_count
                ),

                "row_filter_removed_duplicate_count": (
                    removed_duplicate_count
                ),

                "row_filter_removed_error_count": (
                    removed_error_count
                ),

                "row_filter_removed_comment_count": (
                    removed_comment_count
                ),

                "row_filter_removed_summary_count": (
                    removed_summary_count
                ),

                "row_filter_removed_short_count": (
                    removed_short_count
                ),

                "row_filter_removed_long_count": (
                    removed_long_count
                ),

                "row_filter_preserve_column_positions": (
                    not self.trim_empty_boundaries
                ),
            }
        )

        return document

    # ==================================================
    # Normalize Cell
    # ==================================================

    def _normalize_cell(
        self,
        value: Any,
    ) -> str:
        """
        标准化单个 Excel Cell。

        注意：

            不使用：

                str(value or "")

            因为：

                value = 0

            时会错误变成空字符串。
        """

        if value is None:

            normalized = ""

        else:

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

        # Non-breaking Space
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

        # Normalize Newline
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

        # Collapse Internal Spaces
        if self.collapse_internal_spaces:

            lines = [
                " ".join(
                    line.split()
                )
                for line
                in normalized.splitlines()
                if line.strip()
            ]

            normalized = (
                " / ".join(
                    lines
                )
            )

        # Strip
        if self.strip_cells:

            normalized = (
                normalized.strip()
            )

        return normalized

    # ==================================================
    # Trim Empty Boundaries
    # ==================================================

    @staticmethod
    def _trim_empty_boundaries(
        cells: list[str],
    ) -> list[str]:
        """
        可选删除首尾空 Cell。

        默认不会调用。

        XLSX 中启用此功能可能改变列位置，
        因此仅作为兼容选项保留。
        """

        if not cells:
            return []

        start = 0

        end = len(
            cells
        )

        while (
            start < end
            and not cells[
                start
            ]
        ):

            start += 1

        while (
            end > start
            and not cells[
                end - 1
            ]
        ):

            end -= 1

        return cells[
            start:end
        ]

    # ==================================================
    # Build Row Text
    # ==================================================

    @staticmethod
    def _build_row_text(
        cells: list[str],
    ) -> str:
        """
        构造可检索 Row 文本。

        空 Cell 位置仍然通过 "|" 保留下来。

        Example:

            ["", "A", "B"]

        ->

            "| A | B"
        """

        return (
            " | ".join(
                cells
            ).strip()
        )

    # ==================================================
    # Excel Error Row
    # ==================================================

    @classmethod
    def _is_error_only_row(
        cls,
        cells: list[str],
    ) -> bool:

        if not cells:
            return False

        return all(
            cell.upper()
            in cls._EXCEL_ERROR_VALUES
            for cell
            in cells
        )

    # ==================================================
    # Duplicate Key
    # ==================================================

    def _build_duplicate_key(
        self,
        cells: list[str],
    ) -> str:
        """
        Duplicate Key 保留空 Cell。

        因此：

            ["A", "", "B"]

        与：

            ["A", "B", ""]

        不会被认为是同一行。
        """

        if self.case_sensitive_duplicates:

            values = (
                cells
            )

        else:

            values = [
                value.casefold()
                for value
                in cells
            ]

        return "\x1f".join(
            values
        )

    # ==================================================
    # Resolve Sheet Index
    # ==================================================

    @staticmethod
    def _resolve_sheet_index(
        block: DocumentBlock,
    ) -> int:

        metadata = (
            block.metadata
            or {}
        )

        sheet_index = (
            metadata.get(
                "sheet_index"
            )
        )

        if sheet_index is None:

            sheet_index = (
                block.table_index
            )

        if (
            sheet_index is None
            and block.page_number
            is not None
        ):

            sheet_index = (
                int(
                    block.page_number
                )
                - 1
            )

        if sheet_index is None:

            return 0

        try:

            return max(
                int(
                    sheet_index
                ),
                0,
            )

        except (
            TypeError,
            ValueError,
        ):

            return 0

    # ==================================================
    # Resolve Row Number
    # ==================================================

    @staticmethod
    def _resolve_row_number(
        block: DocumentBlock,
    ) -> int:

        metadata = (
            block.metadata
            or {}
        )

        row_number = (
            metadata.get(
                "row_number"
            )
        )

        if row_number is not None:

            try:

                return max(
                    int(
                        row_number
                    ),
                    0,
                )

            except (
                TypeError,
                ValueError,
            ):

                pass

        if (
            block.row_index
            is not None
        ):

            return (
                int(
                    block.row_index
                )
                + 1
            )

        return max(
            int(
                block.order
            )
            + 1,
            0,
        )

    # ==================================================
    # Block Sort
    # ==================================================

    @classmethod
    def _block_sort_key(
        cls,
        block: DocumentBlock,
    ) -> tuple[
        int,
        int,
        int,
    ]:

        sheet_index = (
            cls._resolve_sheet_index(
                block
            )
        )

        row_number = (
            cls._resolve_row_number(
                block
            )
        )

        return (
            sheet_index,
            row_number,
            int(
                block.order
            ),
        )

    # ==================================================
    # Pattern Match
    # ==================================================

    @staticmethod
    def _matches_patterns(
        text: str,
        patterns: tuple[
            re.Pattern[str],
            ...,
        ],
    ) -> bool:

        return any(
            pattern.search(
                text
            )
            is not None
            for pattern
            in patterns
        )

    # ==================================================
    # Compile Patterns
    # ==================================================

    @staticmethod
    def _compile_patterns(
        patterns: Iterable[
            str | re.Pattern[str]
        ] | None,
        *,
        defaults: tuple[
            re.Pattern[str],
            ...,
        ],
    ) -> tuple[
        re.Pattern[str],
        ...,
    ]:

        compiled = list(
            defaults
        )

        if patterns is None:

            return tuple(
                compiled
            )

        for pattern in patterns:

            if isinstance(
                pattern,
                re.Pattern,
            ):

                compiled.append(
                    pattern
                )

                continue

            compiled.append(
                re.compile(
                    str(
                        pattern
                    ),
                    re.IGNORECASE,
                )
            )

        return tuple(
            compiled
        )

    # ==================================================
    # Rebuild Pages
    # ==================================================

    @classmethod
    def _rebuild_pages(
        cls,
        document: Document,
    ) -> None:
        """
        每个保留 Sheet 构建一个逻辑 Page。

        原始：

            sheet_index

        保留。

        新的：

            page_number
            logical_page_number

        从 1 连续编号。
        """

        blocks_by_sheet: dict[
            int,
            list[DocumentBlock],
        ] = {}

        for block in document.blocks:

            sheet_index = (
                cls._resolve_sheet_index(
                    block
                )
            )

            blocks_by_sheet.setdefault(
                sheet_index,
                [],
            ).append(
                block
            )

        rebuilt_pages: list[
            Page
        ] = []

        for (
            logical_page_number,
            sheet_index,
        ) in enumerate(
            sorted(
                blocks_by_sheet
            ),
            start=1,
        ):

            sheet_blocks = sorted(
                blocks_by_sheet[
                    sheet_index
                ],
                key=cls._block_sort_key,
            )

            text = "\n".join(
                str(
                    block.text
                    or ""
                ).strip()
                for block
                in sheet_blocks
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

            for block in sheet_blocks:

                block.page_number = (
                    logical_page_number
                )

                block.metadata.update(
                    {
                        "sheet_index": (
                            sheet_index
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
    # Sheet Metadata
    # ==================================================

    @classmethod
    def _update_sheet_metadata(
        cls,
        document: Document,
    ) -> None:

        block_counts: dict[
            int,
            int,
        ] = {}

        row_counts: dict[
            int,
            int,
        ] = {}

        character_counts: dict[
            int,
            int,
        ] = {}

        sheet_names: dict[
            int,
            str,
        ] = {}

        for block in document.blocks:

            sheet_index = (
                cls._resolve_sheet_index(
                    block
                )
            )

            block_counts[
                sheet_index
            ] = (
                block_counts.get(
                    sheet_index,
                    0,
                )
                + 1
            )

            character_counts[
                sheet_index
            ] = (
                character_counts.get(
                    sheet_index,
                    0,
                )
                + len(
                    block.text
                    or ""
                )
            )

            if (
                block.block_type
                == BlockType.TABLE
            ):

                row_counts[
                    sheet_index
                ] = (
                    row_counts.get(
                        sheet_index,
                        0,
                    )
                    + 1
                )

            raw_sheet_name = (
                block.metadata.get(
                    "sheet_name"
                )
            )

            if (
                raw_sheet_name
                and sheet_index
                not in sheet_names
            ):

                sheet_names[
                    sheet_index
                ] = str(
                    raw_sheet_name
                )

        # ==============================================
        # Existing Sheet Metadata
        # ==============================================

        raw_sheets = (
            document.metadata.get(
                "sheets",
                [],
            )
        )

        record_map: dict[
            int,
            dict[str, Any],
        ] = {}

        if isinstance(
            raw_sheets,
            list,
        ):

            for record in raw_sheets:

                if not isinstance(
                    record,
                    dict,
                ):

                    continue

                sheet_index = (
                    record.get(
                        "sheet_index"
                    )
                )

                if sheet_index is None:

                    continue

                try:

                    sheet_index = int(
                        sheet_index
                    )

                except (
                    TypeError,
                    ValueError,
                ):

                    continue

                record_map[
                    sheet_index
                ] = dict(
                    record
                )

        # ==============================================
        # Supplement Missing Metadata
        # ==============================================

        for sheet_index in block_counts:

            record = (
                record_map.setdefault(
                    sheet_index,
                    {
                        "sheet_index": (
                            sheet_index
                        ),
                    },
                )
            )

            if (
                sheet_index
                in sheet_names
                and not record.get(
                    "sheet_name"
                )
            ):

                record[
                    "sheet_name"
                ] = (
                    sheet_names[
                        sheet_index
                    ]
                )

        # ==============================================
        # Build Updated Records
        # ==============================================

        updated_sheets: list[
            dict[str, Any]
        ] = []

        logical_page_number = 1

        for sheet_index in sorted(
            block_counts
        ):

            record = dict(
                record_map.get(
                    sheet_index,
                    {
                        "sheet_index": (
                            sheet_index
                        ),
                    },
                )
            )

            record[
                "sheet_index"
            ] = (
                sheet_index
            )

            record[
                "non_empty_row_count"
            ] = (
                row_counts.get(
                    sheet_index,
                    0,
                )
            )

            record[
                "block_count"
            ] = (
                block_counts.get(
                    sheet_index,
                    0,
                )
            )

            record[
                "character_count"
            ] = (
                character_counts.get(
                    sheet_index,
                    0,
                )
            )

            record[
                "logical_page_number"
            ] = (
                logical_page_number
            )

            record[
                "status"
            ] = "SUCCESS"

            updated_sheets.append(
                record
            )

            logical_page_number += 1

        document.metadata[
            "sheets"
        ] = (
            updated_sheets
        )

        document.metadata[
            "block_count"
        ] = len(
            document.blocks
        )

        document.metadata[
            "non_empty_row_count"
        ] = sum(
            row_counts.values()
        )

        document.metadata[
            "processed_sheet_count"
        ] = len(
            block_counts
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
                "RowFilter expects an "
                "app.model.document.Document "
                "instance."
            )

        file_type = str(
            document.file_type
            or ""
        ).strip().lower()

        if file_type != "xlsx":

            raise ValueError(
                "RowFilter only accepts "
                "XLSX documents. "
                f"Received file_type: "
                f"{document.file_type}"
            )