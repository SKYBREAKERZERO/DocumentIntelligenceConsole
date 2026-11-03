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


class SheetFilter:
    """
    XLSX Worksheet 过滤器。

    负责：
        - 删除空 Sheet
        - 删除隐藏或 veryHidden Sheet
        - 按 Sheet 名称排除无关工作表
        - 按 Sheet 名称显式保留目标工作表
        - 删除对应的 DocumentBlock
        - 重建逻辑 Page
        - 重新分配 Block order
        - 修正 Block page_number
        - 保留原始 sheet_index / sheet_name
        - 更新 document.metadata["sheets"]
        - 保持 Sheet metadata 可重复执行

    不负责：
        - 删除空行
        - 删除合计行、注释行
        - 表头识别
        - 表格区域识别
        - Chapter / Section 建模
        - Chunk
        - Token 统计

    设计原则：
        document.blocks 是 XLSX 的结构数据源。

        document.metadata["sheets"] 用于保存 Sheet 属性，
        但不能假设 metadata 永远完整。

        因此：
            metadata + blocks
        必须联合构建 Sheet 信息。
    """

    # ==================================================
    # Default Excluded Sheet Names
    # ==================================================

    DEFAULT_EXCLUDED_NAME_PATTERNS = (
        re.compile(
            r"^(?:cover|表紙|封面)$",
            re.IGNORECASE,
        ),
        re.compile(
            r"^(?:toc|contents?|目次|目录)$",
            re.IGNORECASE,
        ),
        re.compile(
            r"^(?:revision|revision history|change history|"
            r"改訂履歴|改版履歴|変更履歴|变更履历)$",
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
        remove_empty_sheets: bool = True,
        remove_hidden_sheets: bool = True,
        remove_very_hidden_sheets: bool = True,
        exclude_default_names: bool = False,
        include_sheet_names: Iterable[str] | None = None,
        exclude_sheet_names: Iterable[str] | None = None,
        exclude_name_patterns: Iterable[
            str | re.Pattern[str]
        ] | None = None,
        case_sensitive: bool = False,
        minimum_non_empty_rows: int = 1,
        retain_blocks_without_sheet: bool = True,
        rebuild_pages: bool = True,
        reassign_block_order: bool = True,
    ) -> None:

        if minimum_non_empty_rows < 0:
            raise ValueError(
                "minimum_non_empty_rows cannot be negative."
            )

        self.remove_empty_sheets = (
            remove_empty_sheets
        )

        self.remove_hidden_sheets = (
            remove_hidden_sheets
        )

        self.remove_very_hidden_sheets = (
            remove_very_hidden_sheets
        )

        self.exclude_default_names = (
            exclude_default_names
        )

        self.case_sensitive = (
            case_sensitive
        )

        self.minimum_non_empty_rows = (
            minimum_non_empty_rows
        )

        self.retain_blocks_without_sheet = (
            retain_blocks_without_sheet
        )

        self.rebuild_pages = (
            rebuild_pages
        )

        self.reassign_block_order = (
            reassign_block_order
        )

        self.include_sheet_names = (
            self._normalize_name_set(
                include_sheet_names
            )
        )

        self.exclude_sheet_names = (
            self._normalize_name_set(
                exclude_sheet_names
            )
        )

        patterns: list[
            re.Pattern[str]
        ] = []

        if exclude_default_names:

            patterns.extend(
                self.DEFAULT_EXCLUDED_NAME_PATTERNS
            )

        if exclude_name_patterns:

            flags = (
                0
                if case_sensitive
                else re.IGNORECASE
            )

            for pattern in exclude_name_patterns:

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

        self.exclude_name_patterns = tuple(
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

        # ==============================================
        # Collect Sheet Records
        # ==============================================

        sheet_records = (
            self._collect_sheet_records(
                document
            )
        )

        retained_sheet_indexes: set[
            int
        ] = set()

        retained_sheet_names: set[
            str
        ] = set()

        removed_sheets: list[
            dict[str, Any]
        ] = []

        # ==============================================
        # Decide Sheets
        # ==============================================

        for record in sheet_records:

            keep, reason = (
                self._should_keep_sheet(
                    record
                )
            )

            sheet_index = int(
                record[
                    "sheet_index"
                ]
            )

            sheet_name = str(
                record.get(
                    "sheet_name",
                    f"Sheet{sheet_index + 1}",
                )
            )

            if keep:

                retained_sheet_indexes.add(
                    sheet_index
                )

                retained_sheet_names.add(
                    self._normalize_name(
                        sheet_name
                    )
                )

                continue

            removed_sheets.append(
                {
                    "sheet_index": (
                        sheet_index
                    ),
                    "sheet_name": (
                        sheet_name
                    ),
                    "sheet_state": (
                        record.get(
                            "sheet_state",
                            "visible",
                        )
                    ),
                    "reason": (
                        reason
                    ),
                }
            )

        # ==============================================
        # Filter Blocks
        # ==============================================

        original_block_count = len(
            document.blocks
        )

        document.blocks = [
            block
            for block
            in document.blocks
            if self._block_belongs_to_retained_sheet(
                block=block,
                retained_sheet_indexes=(
                    retained_sheet_indexes
                ),
                retained_sheet_names=(
                    retained_sheet_names
                ),
                retain_without_sheet=(
                    self.retain_blocks_without_sheet
                ),
            )
        ]

        # ==============================================
        # Block Order
        # ==============================================

        if self.reassign_block_order:

            self._reassign_block_order(
                document
            )

        # ==============================================
        # Rebuild Pages
        # ==============================================

        if self.rebuild_pages:

            self._rebuild_pages(
                document=document,
                retained_sheet_indexes=(
                    retained_sheet_indexes
                ),
            )

        # ==============================================
        # Metadata
        # ==============================================

        self._update_sheet_metadata(
            document=document,
            retained_sheet_indexes=(
                retained_sheet_indexes
            ),
            removed_sheets=(
                removed_sheets
            ),
            sheet_records=(
                sheet_records
            ),
        )

        document.metadata.update(
            {
                "sheet_filter": (
                    "SheetFilter"
                ),

                "sheet_filter_status": (
                    "SUCCESS"
                ),

                "sheet_filter_original_count": (
                    len(
                        sheet_records
                    )
                ),

                "sheet_filter_retained_count": (
                    len(
                        retained_sheet_indexes
                    )
                ),

                "sheet_filter_removed_count": (
                    len(
                        removed_sheets
                    )
                ),

                "sheet_filter_removed_sheets": (
                    removed_sheets
                ),

                "sheet_filter_removed_block_count": (
                    original_block_count
                    - len(
                        document.blocks
                    )
                ),
            }
        )

        return document

    # ==================================================
    # Keep / Remove Decision
    # ==================================================

    def _should_keep_sheet(
        self,
        record: dict[str, Any],
    ) -> tuple[
        bool,
        str | None,
    ]:

        sheet_name = str(
            record.get(
                "sheet_name",
                "",
            )
        ).strip()

        sheet_state = str(
            record.get(
                "sheet_state",
                "visible",
            )
        ).strip()

        normalized_state = (
            sheet_state.casefold()
        )

        non_empty_row_count = (
            self._safe_int(
                record.get(
                    "non_empty_row_count",
                    0,
                ),
                default=0,
            )
        )

        normalized_name = (
            self._normalize_name(
                sheet_name
            )
        )

        # ==============================================
        # Explicit Include
        # ==============================================

        if (
            self.include_sheet_names
            and normalized_name
            not in self.include_sheet_names
        ):

            return (
                False,
                "not_in_include_list",
            )

        # ==============================================
        # Explicit Exclude
        # ==============================================

        if (
            normalized_name
            in self.exclude_sheet_names
        ):

            return (
                False,
                "excluded_sheet_name",
            )

        # ==============================================
        # Hidden
        # ==============================================

        if (
            normalized_state == "hidden"
            and self.remove_hidden_sheets
        ):

            return (
                False,
                "hidden_sheet",
            )

        # ==============================================
        # Very Hidden
        # ==============================================

        if (
            normalized_state == "veryhidden"
            and self.remove_very_hidden_sheets
        ):

            return (
                False,
                "very_hidden_sheet",
            )

        # ==============================================
        # Empty Sheet
        # ==============================================

        if (
            self.remove_empty_sheets
            and non_empty_row_count == 0
        ):

            return (
                False,
                "empty_sheet",
            )

        # ==============================================
        # Minimum Rows
        # ==============================================

        if (
            non_empty_row_count
            < self.minimum_non_empty_rows
        ):

            return (
                False,
                "insufficient_non_empty_rows",
            )

        # ==============================================
        # Name Pattern
        # ==============================================

        if (
            self._matches_excluded_pattern(
                sheet_name
            )
        ):

            return (
                False,
                "excluded_name_pattern",
            )

        return (
            True,
            None,
        )

    # ==================================================
    # Collect Sheet Records
    # ==================================================

    def _collect_sheet_records(
        self,
        document: Document,
    ) -> list[
        dict[str, Any]
    ]:
        """
        联合 metadata 和 blocks 构建 Sheet Records。

        不能采用：

            metadata 有记录
            -> 直接返回 metadata

        因为 metadata 可能只包含部分 Sheet。
        """

        record_map: dict[
            int,
            dict[str, Any],
        ] = {}

        # ==============================================
        # Existing Metadata
        # ==============================================

        raw_sheets = (
            document.metadata.get(
                "sheets",
                [],
            )
        )

        if isinstance(
            raw_sheets,
            list,
        ):

            for raw_record in raw_sheets:

                if not isinstance(
                    raw_record,
                    dict,
                ):

                    continue

                if (
                    "sheet_index"
                    not in raw_record
                ):

                    continue

                sheet_index = (
                    self._safe_int(
                        raw_record.get(
                            "sheet_index"
                        ),
                        default=-1,
                    )
                )

                if sheet_index < 0:
                    continue

                record = dict(
                    raw_record
                )

                record[
                    "sheet_index"
                ] = (
                    sheet_index
                )

                record.setdefault(
                    "sheet_name",
                    f"Sheet{sheet_index + 1}",
                )

                record.setdefault(
                    "sheet_state",
                    "visible",
                )

                record_map[
                    sheet_index
                ] = (
                    record
                )

        # ==============================================
        # Current Block Statistics
        # ==============================================

        row_counts: dict[
            int,
            int,
        ] = {}

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

            sheet_index = (
                self._resolve_sheet_index(
                    block,
                    allow_missing=True,
                )
            )

            if sheet_index is None:
                continue

            metadata = (
                block.metadata
                or {}
            )

            record = (
                record_map.setdefault(
                    sheet_index,
                    {
                        "sheet_index": (
                            sheet_index
                        ),
                        "sheet_name": (
                            metadata.get(
                                "sheet_name",
                                f"Sheet{sheet_index + 1}",
                            )
                        ),
                        "sheet_state": (
                            metadata.get(
                                "sheet_state",
                                "visible",
                            )
                        ),
                    },
                )
            )

            # Metadata 中默认值可以由 Block 补充。
            if (
                not record.get(
                    "sheet_name"
                )
                and metadata.get(
                    "sheet_name"
                )
            ):

                record[
                    "sheet_name"
                ] = (
                    metadata[
                        "sheet_name"
                    ]
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

        # ==============================================
        # Refresh Statistics
        # ==============================================

        for (
            sheet_index,
            record,
        ) in record_map.items():

            record[
                "block_count"
            ] = (
                block_counts.get(
                    sheet_index,
                    0,
                )
            )

            # XLSX 当前 Row 使用 TABLE Block，
            # 因此以 TABLE 数作为 non_empty_row_count。
            #
            # 如果某个旧 Loader 没有 TABLE 类型，
            # 则回退到 block_count。
            table_row_count = (
                row_counts.get(
                    sheet_index,
                    0,
                )
            )

            block_count = (
                block_counts.get(
                    sheet_index,
                    0,
                )
            )

            record[
                "non_empty_row_count"
            ] = (
                table_row_count
                if table_row_count > 0
                else block_count
            )

            record[
                "character_count"
            ] = (
                character_counts.get(
                    sheet_index,
                    0,
                )
            )

        return [
            record_map[
                sheet_index
            ]
            for sheet_index
            in sorted(
                record_map
            )
        ]

    # ==================================================
    # Resolve Sheet Index
    # ==================================================

    @staticmethod
    def _resolve_sheet_index(
        block: DocumentBlock,
        *,
        allow_missing: bool = False,
    ) -> int | None:

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

        if sheet_index is not None:

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

                if allow_missing:
                    return None

                return 0

        # page_number 是最后 fallback。
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

        if allow_missing:
            return None

        return 0

    # ==================================================
    # Block Retention
    # ==================================================

    def _block_belongs_to_retained_sheet(
        self,
        *,
        block: DocumentBlock,
        retained_sheet_indexes: set[int],
        retained_sheet_names: set[str],
        retain_without_sheet: bool,
    ) -> bool:

        sheet_index = (
            self._resolve_sheet_index(
                block,
                allow_missing=True,
            )
        )

        if sheet_index is not None:

            return (
                sheet_index
                in retained_sheet_indexes
            )

        metadata = (
            block.metadata
            or {}
        )

        sheet_name = (
            metadata.get(
                "sheet_name"
            )
        )

        if sheet_name is None:

            return bool(
                retain_without_sheet
            )

        normalized_name = (
            self._normalize_name(
                str(
                    sheet_name
                )
            )
        )

        return (
            normalized_name
            in retained_sheet_names
        )

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
            order,
            block,
        ) in enumerate(
            sorted_blocks
        ):

            block.order = (
                order
            )

        document.blocks = (
            sorted_blocks
        )

    # ==================================================
    # Block Sort Key
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
            or 0
        )

        metadata = (
            block.metadata
            or {}
        )

        row_number = (
            metadata.get(
                "row_number"
            )
        )

        if row_number is None:

            if (
                block.row_index
                is not None
            ):

                row_number = (
                    int(
                        block.row_index
                    )
                    + 1
                )

            else:

                row_number = 0

        try:

            resolved_row_number = (
                int(
                    row_number
                )
            )

        except (
            TypeError,
            ValueError,
        ):

            resolved_row_number = 0

        return (
            sheet_index,
            resolved_row_number,
            int(
                block.order
            ),
        )

    # ==================================================
    # Rebuild Pages
    # ==================================================

    @classmethod
    def _rebuild_pages(
        cls,
        *,
        document: Document,
        retained_sheet_indexes: set[int],
    ) -> None:
        """
        每个保留 Sheet 对应一个逻辑 Page。

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
                    block,
                    allow_missing=True,
                )
            )

            if sheet_index is None:
                continue

            if (
                sheet_index
                not in retained_sheet_indexes
            ):
                continue

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
                retained_sheet_indexes
            ),
            start=1,
        ):

            sheet_blocks = sorted(
                blocks_by_sheet.get(
                    sheet_index,
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
    # Update Sheet Metadata
    # ==================================================

    @classmethod
    def _update_sheet_metadata(
        cls,
        *,
        document: Document,
        retained_sheet_indexes: set[int],
        removed_sheets: list[
            dict[str, Any]
        ],
        sheet_records: list[
            dict[str, Any]
        ],
    ) -> None:

        # ==============================================
        # Current Block Statistics
        # ==============================================

        blocks_by_sheet: dict[
            int,
            list[DocumentBlock],
        ] = {}

        for block in document.blocks:

            sheet_index = (
                cls._resolve_sheet_index(
                    block,
                    allow_missing=True,
                )
            )

            if sheet_index is None:
                continue

            blocks_by_sheet.setdefault(
                sheet_index,
                [],
            ).append(
                block
            )

        # ==============================================
        # Rebuild Metadata
        # ==============================================

        retained_records: list[
            dict[str, Any]
        ] = []

        logical_page_number = 1

        for record in sorted(
            sheet_records,
            key=lambda item: int(
                item[
                    "sheet_index"
                ]
            ),
        ):

            sheet_index = int(
                record[
                    "sheet_index"
                ]
            )

            if (
                sheet_index
                not in retained_sheet_indexes
            ):

                continue

            updated = dict(
                record
            )

            sheet_blocks = (
                blocks_by_sheet.get(
                    sheet_index,
                    [],
                )
            )

            row_count = sum(
                1
                for block
                in sheet_blocks
                if (
                    block.block_type
                    == BlockType.TABLE
                )
            )

            updated[
                "logical_page_number"
            ] = (
                logical_page_number
            )

            updated[
                "block_count"
            ] = (
                len(
                    sheet_blocks
                )
            )

            updated[
                "non_empty_row_count"
            ] = (
                row_count
                if row_count > 0
                else len(
                    sheet_blocks
                )
            )

            updated[
                "character_count"
            ] = sum(
                len(
                    block.text
                    or ""
                )
                for block
                in sheet_blocks
            )

            updated[
                "status"
            ] = "SUCCESS"

            retained_records.append(
                updated
            )

            logical_page_number += 1

        document.metadata[
            "sheets"
        ] = (
            retained_records
        )

        document.metadata[
            "processed_sheet_count"
        ] = (
            len(
                retained_records
            )
        )

        # ==============================================
        # Idempotent skipped_sheet_count
        # ==============================================

        previous_filter_removed_count = (
            cls._safe_int(
                document.metadata.get(
                    "sheet_filter_removed_count",
                    0,
                ),
                default=0,
            )
        )

        current_skipped_count = (
            cls._safe_int(
                document.metadata.get(
                    "skipped_sheet_count",
                    0,
                ),
                default=0,
            )
        )

        loader_skipped_count = max(
            current_skipped_count
            - previous_filter_removed_count,
            0,
        )

        document.metadata[
            "skipped_sheet_count"
        ] = (
            loader_skipped_count
            + len(
                removed_sheets
            )
        )

        document.metadata[
            "block_count"
        ] = (
            len(
                document.blocks
            )
        )

        document.metadata[
            "non_empty_row_count"
        ] = sum(
            int(
                record.get(
                    "non_empty_row_count",
                    0,
                )
                or 0
            )
            for record
            in retained_records
        )

        document.metadata[
            "page_count"
        ] = (
            len(
                document.pages
            )
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
    # Name Pattern
    # ==================================================

    def _matches_excluded_pattern(
        self,
        sheet_name: str,
    ) -> bool:

        return any(
            pattern.search(
                sheet_name
            )
            is not None
            for pattern
            in self.exclude_name_patterns
        )

    # ==================================================
    # Normalize Name Set
    # ==================================================

    def _normalize_name_set(
        self,
        values: Iterable[str] | None,
    ) -> set[str]:

        if values is None:
            return set()

        normalized_names: set[
            str
        ] = set()

        for value in values:

            normalized = (
                self._normalize_name(
                    str(
                        value
                    )
                )
            )

            if normalized:

                normalized_names.add(
                    normalized
                )

        return normalized_names

    # ==================================================
    # Normalize Name
    # ==================================================

    def _normalize_name(
        self,
        value: str,
    ) -> str:

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
        ).strip()

        if self.case_sensitive:

            return normalized

        return (
            normalized.casefold()
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
                "SheetFilter expects an "
                "app.model.document.Document "
                "instance."
            )

        file_type = str(
            document.file_type
            or ""
        ).strip().lower()

        if file_type != "xlsx":

            raise ValueError(
                "SheetFilter only accepts "
                "XLSX documents. "
                f"Received file_type: "
                f"{document.file_type}"
            )