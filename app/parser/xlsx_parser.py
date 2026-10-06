from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from app.model.block import BlockType, DocumentBlock
from app.model.chapter import Chapter
from app.model.content import Content
from app.model.document import Document
from app.model.section import Section
from app.analyzer.import_resilience_policy import ImportResiliencePolicy
from app.analyzer.issue_register_policy import IssueRegisterPolicy, IssueHeaderMatch


class XLSXParserError(RuntimeError):
    """XLSX 结构解析异常。"""


class XLSXParser:
    """
    企业级 XLSX 结构解析器。

    默认映射：

        Worksheet
            -> Chapter

        Sheet 内连续数据区域
            -> Section

        数据区域第一行
            -> Section 标题候选

        数据区域
            -> Content

    连续区域判断：

        row 1
        row 2
        row 3
        row 7
        row 8

    将识别为两个区域：

        region 1: row 1-3
        region 2: row 7-8

    RAG 序列化策略：

        Loader 层：
            - 完整保留 Cell 列位置
            - 完整保留公式字符串
            - 完整保留原始 DocumentBlock.cells

        Parser 层：
            - 不修改原始 Block / cells
            - Formula 不进入 Section title / Content text
            - Excel error literal 不进入 RAG text
            - 压缩空 Cell
            - 有可信表头时生成：
                Header: Value
            - 无可信表头时生成：
                Column B: Value
            - 避免：
                " |  |  |  |  | "
              这种低价值 RAG 文本

    公式示例：

        =TODAY()
        =VLOOKUP(J3,#REF!,2,FALSE)

    会继续保留在原始 block.cells / Section.metadata["header_cells"]，
    但不会进入最终 Section.title_jp / Content.text。

    不负责：
        - 加载 XLSX
        - 删除隐藏 Sheet
        - 删除空行
        - Unicode 标准化
        - Content Chunk
        - Token 统计
        - JSON / PostgreSQL 保存
    """

    # ==================================================
    # Header Vocabulary
    # ==================================================

    _GENERIC_HEADER_VALUES = {
        "id",
        "no",
        "no.",
        "number",
        "name",
        "title",
        "description",
        "value",
        "type",
        "status",
        "date",
        "comment",
        "remarks",
        "note",
        "category",
        "result",
        "owner",
        "responsible",
        "項番",
        "番号",
        "名称",
        "項目",
        "内容",
        "説明",
        "値",
        "種別",
        "状態",
        "日付",
        "備考",
        "注記",
        "判定",
        "結果",
        "担当",
        "序号",
        "编号",
        "名称",
        "项目",
        "内容",
        "说明",
        "状态",
        "日期",
        "备注",
        "结果",
    }

    # ==================================================
    # Excel Formula / Error
    # ==================================================

    _EXCEL_ERROR_VALUES = {
        "#NULL!",
        "#DIV/0!",
        "#VALUE!",
        "#REF!",
        "#NAME?",
        "#NUM!",
        "#N/A",
        "#SPILL!",
        "#CALC!",
        "#FIELD!",
        "#GETTING_DATA",
        "#BLOCKED!",
        "#UNKNOWN!",
        "#CONNECT!",
    }

    _FORMULA_PREFIX_PATTERN = re.compile(
        r"^\s*="
    )

    # ==================================================
    # Init
    # ==================================================

    def __init__(
        self,
        *,
        first_row_as_header: bool = True,
        detect_multiple_regions: bool = True,
        maximum_row_gap: int = 1,
        include_header_in_content: bool = True,
        include_single_row_regions: bool = True,
        maximum_section_title_length: int = 200,
        content_row_separator: str = "\n",
        cell_separator: str = " | ",
        exclude_formulas_from_rag: bool = True,
        exclude_excel_errors_from_rag: bool = True,
        use_column_labels_for_unlabeled_values: bool = True,
    ) -> None:

        if maximum_row_gap < 1:
            raise ValueError(
                "maximum_row_gap must be at least 1."
            )

        if maximum_section_title_length <= 0:
            raise ValueError(
                "maximum_section_title_length must be greater than 0."
            )

        if not content_row_separator:
            raise ValueError(
                "content_row_separator cannot be empty."
            )

        if not cell_separator:
            raise ValueError(
                "cell_separator cannot be empty."
            )

        self.first_row_as_header = (
            bool(first_row_as_header)
        )

        self.detect_multiple_regions = (
            bool(detect_multiple_regions)
        )

        self.maximum_row_gap = (
            int(maximum_row_gap)
        )

        self.include_header_in_content = (
            bool(include_header_in_content)
        )

        self.include_single_row_regions = (
            bool(include_single_row_regions)
        )

        self.maximum_section_title_length = (
            int(maximum_section_title_length)
        )

        self.content_row_separator = (
            content_row_separator
        )

        self.cell_separator = (
            cell_separator
        )

        self.exclude_formulas_from_rag = (
            bool(exclude_formulas_from_rag)
        )

        self.exclude_excel_errors_from_rag = (
            bool(exclude_excel_errors_from_rag)
        )

        self.use_column_labels_for_unlabeled_values = (
            bool(use_column_labels_for_unlabeled_values)
        )

    # ==================================================
    # Public API
    # ==================================================

    def parse(
        self,
        document: Document,
    ) -> Document:

        self._validate_document(
            document
        )

        # Enterprise import resilience: workbook templates / image-only sheets
        # may legitimately yield no RAG rows after filtering. Persist them as
        # metadata-only audit records instead of failing the batch.
        if not document.blocks:
            return ImportResiliencePolicy.mark_metadata_only(
                document,
                warning_code="XLSX_NO_BLOCKS",
                parser_name="XLSXParser",
                warning_message="XLSX contains no blocks after filtering; stored as metadata-only and excluded from Qdrant.",
            )

        if not any(
            block.block_type == BlockType.TABLE
            and (str(block.text or "").strip() or any(str(cell).strip() for cell in block.cells))
            for block in document.blocks
        ):
            return ImportResiliencePolicy.mark_metadata_only(
                document,
                warning_code="XLSX_NO_EXTRACTABLE_ROWS",
                parser_name="XLSXParser",
                warning_message="XLSX contains no extractable rows; stored as metadata-only and excluded from Qdrant.",
            )

        # 幂等：重复 parse() 不累积旧结构。
        document.chapters.clear()
        document.sections.clear()
        document.contents.clear()

        sheet_groups = (
            self._group_blocks_by_sheet(
                document.blocks
            )
        )

        if not sheet_groups:
            return ImportResiliencePolicy.mark_metadata_only(
                document,
                warning_code="XLSX_NO_PARSEABLE_SHEET_BLOCKS",
                parser_name="XLSXParser",
                warning_message="XLSX contains no parseable sheet blocks; stored as metadata-only and excluded from Qdrant.",
            )

        parsed_sheet_count = 0
        parsed_region_count = 0
        skipped_region_count = 0
        generated_content_count = 0

        formula_cell_count = 0
        excel_error_cell_count = 0
        rag_filtered_formula_cell_count = 0
        rag_filtered_error_cell_count = 0
        rag_empty_row_count = 0
        rag_semantic_pair_row_count = 0
        rag_sparse_row_count = 0

        # 先统计原始 workbook block 中的公式/错误。
        for blocks in sheet_groups.values():
            for block in blocks:
                for cell in block.cells:
                    normalized = (
                        self._normalize_text(
                            cell
                        )
                    )

                    if self._is_formula(
                        normalized
                    ):
                        formula_cell_count += 1

                    elif self._is_excel_error(
                        normalized
                    ):
                        excel_error_cell_count += 1

        for (
            chapter_order,
            sheet_index,
        ) in enumerate(
            sorted(
                sheet_groups
            ),
            start=1,
        ):

            sheet_blocks = sorted(
                sheet_groups[
                    sheet_index
                ],
                key=self._block_sort_key,
            )

            if not sheet_blocks:
                continue

            sheet_name = (
                self._resolve_sheet_name(
                    sheet_blocks,
                    sheet_index,
                )
            )

            chapter_id = str(
                chapter_order
            )

            chapter = Chapter(
                id=chapter_id,

                title_jp=(
                    sheet_name
                ),

                title_en=None,

                level=1,

                sort_order=(
                    chapter_order
                ),

                # XLSX 的 page_number 是 Sheet 的逻辑页序号，
                # 不是纸张打印页码。
                page_number=(
                    self._resolve_page_number(
                        sheet_blocks
                    )
                ),

                metadata={
                    "source": (
                        "xlsx"
                    ),

                    "sheet_index": (
                        sheet_index
                    ),

                    "sheet_name": (
                        sheet_name
                    ),

                    "block_count": len(
                        sheet_blocks
                    ),

                    "page_number_semantics": (
                        "logical_sheet_number"
                    ),
                },
            )

            document.chapters.append(
                chapter
            )

            parsed_sheet_count += 1

            regions = (
                self._split_into_regions(
                    sheet_blocks
                )
            )

            section_index = 0

            for (
                region_index,
                region_blocks,
            ) in enumerate(
                regions,
                start=1,
            ):

                if not region_blocks:
                    continue

                if (
                    len(
                        region_blocks
                    )
                    == 1
                    and not self.include_single_row_regions
                ):
                    skipped_region_count += 1
                    continue

                section_index += 1

                section_id = (
                    f"{chapter_id}."
                    f"{section_index}"
                )

                header_block = (
                    region_blocks[
                        0
                    ]
                    if self.first_row_as_header
                    else None
                )

                section_title = (
                    self._build_section_title(
                        sheet_name=(
                            sheet_name
                        ),

                        region_index=(
                            region_index
                        ),

                        header_block=(
                            header_block
                        ),
                    )
                )

                raw_header_cells = (
                    list(
                        header_block.cells
                    )
                    if header_block
                    is not None
                    else []
                )

                rag_header_cells = (
                    self._clean_cells_for_rag(
                        raw_header_cells
                    )
                )

                issue_header_match = IssueRegisterPolicy.detect_headers(
                    rag_header_cells,
                    file_name=document.file_name,
                    sheet_name=sheet_name,
                )
                if issue_header_match is not None:
                    IssueRegisterPolicy.apply_document(
                        document,
                        fields=issue_header_match.recognized_fields,
                        confidence=issue_header_match.confidence,
                    )

                section = Section(
                    id=section_id,

                    chapter_id=(
                        chapter_id
                    ),

                    parent_section_id=None,

                    title_jp=(
                        section_title
                    ),

                    title_en=None,

                    level=2,

                    sort_order=(
                        parsed_region_count
                        + 1
                    ),

                    page_number=(
                        self._resolve_page_number(
                            region_blocks
                        )
                    ),

                    metadata={
                        "source": (
                            "xlsx"
                        ),

                        "sheet_index": (
                            sheet_index
                        ),

                        "sheet_name": (
                            sheet_name
                        ),

                        "region_index": (
                            region_index
                        ),

                        "first_row_number": (
                            self._resolve_row_number(
                                region_blocks[
                                    0
                                ]
                            )
                        ),

                        "last_row_number": (
                            self._resolve_row_number(
                                region_blocks[
                                    -1
                                ]
                            )
                        ),

                        "row_count": len(
                            region_blocks
                        ),

                        # 原始 header cells 完整保留。
                        "header_cells": (
                            raw_header_cells
                        ),

                        # RAG 侧 header 是清洗后的稀疏表示。
                        "rag_header_cells": (
                            rag_header_cells
                        ),
                    },
                )

                document.sections.append(
                    section
                )

                parsed_region_count += 1

                if issue_header_match is not None:
                    issue_contents, content_stats = self._build_issue_register_contents(
                        region_blocks=region_blocks,
                        header_block=header_block,
                        header_match=issue_header_match,
                        chapter_id=chapter_id,
                        section_id=section_id,
                        sheet_index=sheet_index,
                        sheet_name=sheet_name,
                        region_index=region_index,
                    )
                    rag_filtered_formula_cell_count += content_stats["filtered_formula_cells"]
                    rag_filtered_error_cell_count += content_stats["filtered_error_cells"]
                    rag_empty_row_count += content_stats["empty_rows"]
                    rag_semantic_pair_row_count += content_stats["semantic_pair_rows"]
                    rag_sparse_row_count += content_stats["sparse_rows"]
                    document.contents.extend(issue_contents)
                    generated_content_count += len(issue_contents)
                    continue

                (
                    content_text,
                    content_stats,
                ) = self._build_region_content(
                    region_blocks=(
                        region_blocks
                    ),

                    header_block=(
                        header_block
                    ),
                )

                rag_filtered_formula_cell_count += (
                    content_stats[
                        "filtered_formula_cells"
                    ]
                )

                rag_filtered_error_cell_count += (
                    content_stats[
                        "filtered_error_cells"
                    ]
                )

                rag_empty_row_count += (
                    content_stats[
                        "empty_rows"
                    ]
                )

                rag_semantic_pair_row_count += (
                    content_stats[
                        "semantic_pair_rows"
                    ]
                )

                rag_sparse_row_count += (
                    content_stats[
                        "sparse_rows"
                    ]
                )

                if not content_text:
                    continue

                content = Content(
                    chapter_id=(
                        chapter_id
                    ),

                    section_id=(
                        section_id
                    ),

                    text=(
                        content_text
                    ),

                    page_number=(
                        self._resolve_page_number(
                            region_blocks
                        )
                    ),
                )

                if hasattr(
                    content,
                    "metadata",
                ):
                    content.metadata.update(
                        {
                            "source": (
                                "xlsx"
                            ),

                            "sheet_index": (
                                sheet_index
                            ),

                            "sheet_name": (
                                sheet_name
                            ),

                            "region_index": (
                                region_index
                            ),

                            "first_row_number": (
                                self._resolve_row_number(
                                    region_blocks[
                                        0
                                    ]
                                )
                            ),

                            "last_row_number": (
                                self._resolve_row_number(
                                    region_blocks[
                                        -1
                                    ]
                                )
                            ),

                            "page_number_semantics": (
                                "logical_sheet_number"
                            ),
                        }
                    )

                document.contents.append(
                    content
                )

                generated_content_count += 1

        # ==============================================
        # Metadata
        # ==============================================

        document.metadata.update(
            {
                "parser": (
                    "XLSXParser"
                ),

                "parser_status": (
                    "SUCCESS"
                ),

                "xlsx_parser_strategy": (
                    "sheet_region_semantic_sparse_rag"
                ),

                "xlsx_parsed_sheet_count": (
                    parsed_sheet_count
                ),

                "xlsx_parsed_region_count": (
                    parsed_region_count
                ),

                "xlsx_skipped_region_count": (
                    skipped_region_count
                ),

                "chapter_count": len(
                    document.chapters
                ),

                "section_count": len(
                    document.sections
                ),

                "content_count": len(
                    document.contents
                ),

                "xlsx_generated_content_count": (
                    generated_content_count
                ),

                "xlsx_formula_cell_count": (
                    formula_cell_count
                ),

                "xlsx_excel_error_cell_count": (
                    excel_error_cell_count
                ),

                "xlsx_rag_filtered_formula_cell_count": (
                    rag_filtered_formula_cell_count
                ),

                "xlsx_rag_filtered_error_cell_count": (
                    rag_filtered_error_cell_count
                ),

                "xlsx_rag_empty_row_count": (
                    rag_empty_row_count
                ),

                "xlsx_rag_semantic_pair_row_count": (
                    rag_semantic_pair_row_count
                ),

                "xlsx_rag_sparse_row_count": (
                    rag_sparse_row_count
                ),

                "xlsx_exclude_formulas_from_rag": (
                    self.exclude_formulas_from_rag
                ),

                "xlsx_exclude_excel_errors_from_rag": (
                    self.exclude_excel_errors_from_rag
                ),

                "xlsx_raw_cells_preserved": (
                    True
                ),

                "xlsx_page_number_semantics": (
                    "logical_sheet_number"
                ),
            }
        )

        return document

    # ==================================================
    # Sheet Grouping
    # ==================================================

    @classmethod
    def _group_blocks_by_sheet(
        cls,
        blocks: list[
            DocumentBlock
        ],
    ) -> dict[
        int,
        list[
            DocumentBlock
        ],
    ]:

        groups: dict[
            int,
            list[
                DocumentBlock
            ],
        ] = defaultdict(
            list
        )

        for block in blocks:

            if (
                block.block_type
                != BlockType.TABLE
            ):
                continue

            sheet_index = (
                cls._resolve_sheet_index(
                    block
                )
            )

            groups[
                sheet_index
            ].append(
                block
            )

        return dict(
            groups
        )

    # ==================================================
    # Region Detection
    # ==================================================

    def _split_into_regions(
        self,
        blocks: list[
            DocumentBlock
        ],
    ) -> list[
        list[
            DocumentBlock
        ]
    ]:
        """
        根据原始行号断层识别多个数据区域。

        maximum_row_gap=1:

            row 1 -> row 2
                同一区域

            row 2 -> row 4
                新区域
        """

        if not blocks:
            return []

        if not self.detect_multiple_regions:
            return [
                blocks
            ]

        regions: list[
            list[
                DocumentBlock
            ]
        ] = []

        current_region: list[
            DocumentBlock
        ] = []

        previous_row_number: (
            int
            | None
        ) = None

        for block in blocks:

            row_number = (
                self._resolve_row_number(
                    block
                )
            )

            if (
                previous_row_number
                is not None
                and row_number
                - previous_row_number
                > self.maximum_row_gap
            ):

                if current_region:

                    regions.append(
                        current_region
                    )

                current_region = []

            current_region.append(
                block
            )

            previous_row_number = (
                row_number
            )

        if current_region:

            regions.append(
                current_region
            )

        return regions

    # ==================================================
    # Section Title
    # ==================================================

    def _build_section_title(
        self,
        *,
        sheet_name: str,
        region_index: int,
        header_block: (
            DocumentBlock
            | None
        ),
    ) -> str:

        if header_block is None:

            return (
                f"{sheet_name} - "
                f"Table {region_index}"
            )

        # Section title 使用 RAG-safe cells。
        # 原始公式仍保留在 Section.metadata["header_cells"]。
        header_cells = (
            self._clean_cells_for_rag(
                list(
                    header_block.cells
                )
            )
        )

        non_empty_header_cells = [
            cell
            for cell
            in header_cells
            if cell
        ]

        if not non_empty_header_cells:

            return (
                f"{sheet_name} - "
                f"Table {region_index}"
            )

        header_text = (
            self.cell_separator.join(
                non_empty_header_cells
            )
        )

        if (
            len(
                header_text
            )
            > self.maximum_section_title_length
        ):

            header_text = (
                header_text[
                    :self.maximum_section_title_length
                ].rstrip()
                + "..."
            )

        if self._looks_like_header(
            non_empty_header_cells
        ):

            return (
                header_text
            )

        return (
            f"{sheet_name} - "
            f"{header_text}"
        )

    # ==================================================
    # Header Detection
    # ==================================================

    @classmethod
    def _looks_like_header(
        cls,
        cells: list[
            str
        ],
    ) -> bool:

        if not cells:
            return False

        normalized_cells = [
            cell.strip().casefold()
            for cell
            in cells
            if cell.strip()
        ]

        if not normalized_cells:
            return False

        generic_match_count = sum(
            1
            for cell
            in normalized_cells
            if cell
            in cls._GENERIC_HEADER_VALUES
        )

        if generic_match_count > 0:
            return True

        # 两个短值经常只是：
        #
        #   Function Spec Checksheet | TMC
        #
        # 不能仅凭“多列短文本”判定为表头。
        #
        # >= 3 个非空短 Cell 才作为弱表头信号。
        if (
            len(
                normalized_cells
            )
            >= 3
            and all(
                len(
                    cell
                )
                <= 50
                for cell
                in normalized_cells
            )
        ):

            return True

        return False

    # ==================================================
    # Region Content
    # ==================================================


    def _build_issue_register_contents(
        self,
        *,
        region_blocks: list[DocumentBlock],
        header_block: DocumentBlock | None,
        header_match: IssueHeaderMatch,
        chapter_id: str,
        section_id: str,
        sheet_index: int,
        sheet_name: str,
        region_index: int,
    ) -> tuple[list[Content], dict[str, int]]:
        """Serialize one Jira/QA/BUG spreadsheet row into one RAG Content.

        Row-level content is critical for issue retrieval: aggregating hundreds of
        tickets into one spreadsheet region destroys exact status/key semantics
        and makes vector chunks cross issue boundaries.  The original workbook
        blocks remain untouched; this only changes the RAG projection.
        """
        stats = {
            "filtered_formula_cells": 0,
            "filtered_error_cells": 0,
            "empty_rows": 0,
            "semantic_pair_rows": 0,
            "sparse_rows": 0,
        }
        result: list[Content] = []
        if not region_blocks:
            return result, stats
        for block in region_blocks:
            if header_block is not None and block is header_block:
                continue
            cleaned: list[str] = []
            for raw in block.cells:
                value = self._normalize_text(raw)
                if self.exclude_formulas_from_rag and self._is_formula(value):
                    cleaned.append("")
                    stats["filtered_formula_cells"] += 1
                    continue
                if self.exclude_excel_errors_from_rag and self._is_excel_error(value):
                    cleaned.append("")
                    stats["filtered_error_cells"] += 1
                    continue
                cleaned.append(value)
            if not any(cleaned):
                stats["empty_rows"] += 1
                continue
            issue_meta = IssueRegisterPolicy.row_metadata(header_match, cleaned)
            if not issue_meta:
                stats["sparse_rows"] += 1
                continue
            row_number = self._resolve_row_number(block)
            text = IssueRegisterPolicy.serialize_row(issue_meta, row_number=row_number)
            if not text:
                stats["sparse_rows"] += 1
                continue
            metadata: dict[str, object] = {
                "source": "xlsx",
                "artifact_type": IssueRegisterPolicy.ARTIFACT_TYPE,
                "document_role": IssueRegisterPolicy.ROLE,
                "qa_index_policy": IssueRegisterPolicy.QA_INDEX_POLICY,
                "sheet_index": sheet_index,
                "sheet_name": sheet_name,
                "region_index": region_index,
                "row_number": row_number,
                "issue_fields": list(issue_meta),
                "issue_register_confidence": header_match.confidence,
                "page_number_semantics": "logical_sheet_number",
            }
            metadata.update(issue_meta)
            result.append(
                Content(
                    chapter_id=chapter_id,
                    section_id=section_id,
                    text=text,
                    page_number=self._resolve_page_number([block]),
                    metadata=metadata,
                )
            )
            stats["semantic_pair_rows"] += 1
        return result, stats

    def _build_region_content(
        self,
        *,
        region_blocks: list[
            DocumentBlock
        ],
        header_block: (
            DocumentBlock
            | None
        ),
    ) -> tuple[
        str,
        dict[
            str,
            int,
        ],
    ]:

        stats = {
            "filtered_formula_cells": 0,
            "filtered_error_cells": 0,
            "empty_rows": 0,
            "semantic_pair_rows": 0,
            "sparse_rows": 0,
        }

        if not region_blocks:

            return (
                "",
                stats,
            )

        content_blocks = list(
            region_blocks
        )

        if (
            header_block
            is not None
            and not self.include_header_in_content
        ):

            content_blocks = (
                content_blocks[
                    1:
                ]
            )

        if not content_blocks:

            return (
                "",
                stats,
            )

        raw_header_cells = (
            list(
                header_block.cells
            )
            if header_block
            is not None
            else []
        )

        rag_header_cells = (
            self._clean_cells_for_rag(
                raw_header_cells
            )
        )

        # 只有可信表头才用于 Header: Value 键值序列化。
        tabular_header_cells = (
            rag_header_cells
            if self._looks_like_header(
                [
                    cell
                    for cell
                    in rag_header_cells
                    if cell
                ]
            )
            else []
        )

        rows: list[
            str
        ] = []

        for block in content_blocks:

            (
                row_text,
                row_stats,
            ) = self._format_content_row(
                block=(
                    block
                ),

                header_cells=(
                    tabular_header_cells
                ),

                is_header=(
                    header_block
                    is not None
                    and block
                    is header_block
                ),
            )

            for key in stats:
                stats[
                    key
                ] += row_stats[
                    key
                ]

            if row_text:

                rows.append(
                    row_text
                )

        return (
            self.content_row_separator.join(
                rows
            ).strip(),
            stats,
        )

    # ==================================================
    # Content Row
    # ==================================================

    def _format_content_row(
        self,
        *,
        block: DocumentBlock,
        header_cells: list[
            str
        ],
        is_header: bool,
    ) -> tuple[
        str,
        dict[
            str,
            int,
        ],
    ]:

        stats = {
            "filtered_formula_cells": 0,
            "filtered_error_cells": 0,
            "empty_rows": 0,
            "semantic_pair_rows": 0,
            "sparse_rows": 0,
        }

        raw_cells = [
            self._normalize_text(
                cell
            )
            for cell
            in block.cells
        ]

        cleaned_cells: list[
            str
        ] = []

        for cell in raw_cells:

            if (
                self.exclude_formulas_from_rag
                and self._is_formula(
                    cell
                )
            ):

                cleaned_cells.append(
                    ""
                )

                stats[
                    "filtered_formula_cells"
                ] += 1

                continue

            if (
                self.exclude_excel_errors_from_rag
                and self._is_excel_error(
                    cell
                )
            ):

                cleaned_cells.append(
                    ""
                )

                stats[
                    "filtered_error_cells"
                ] += 1

                continue

            cleaned_cells.append(
                cell
            )

        if not any(
            cleaned_cells
        ):

            stats[
                "empty_rows"
            ] += 1

            return (
                "",
                stats,
            )

        row_number = (
            self._resolve_row_number(
                block
            )
        )

        # ==============================================
        # Header Row
        # ==============================================

        if is_header:

            values = [
                value
                for value
                in cleaned_cells
                if value
            ]

            if not values:

                stats[
                    "empty_rows"
                ] += 1

                return (
                    "",
                    stats,
                )

            stats[
                "sparse_rows"
            ] += 1

            return (
                f"[Header row {row_number}] "
                + self.cell_separator.join(
                    values
                ),
                stats,
            )

        # ==============================================
        # Semantic Header: Value
        # ==============================================

        if (
            header_cells
            and len(
                header_cells
            )
            == len(
                cleaned_cells
            )
        ):

            pairs: list[
                str
            ] = []

            unlabeled_values: list[
                str
            ] = []

            for (
                column_index,
                (
                    header,
                    value,
                ),
            ) in enumerate(
                zip(
                    header_cells,
                    cleaned_cells,
                ),
                start=1,
            ):

                if not value:
                    continue

                normalized_header = (
                    self._normalize_text(
                        header
                    )
                )

                if normalized_header:

                    if (
                        value
                        == normalized_header
                    ):

                        pairs.append(
                            value
                        )

                    else:

                        pairs.append(
                            f"{normalized_header}: {value}"
                        )

                else:

                    unlabeled_values.append(
                        self._format_unlabeled_value(
                            column_index=(
                                column_index
                            ),
                            value=(
                                value
                            ),
                        )
                    )

            serialized_values = (
                pairs
                + unlabeled_values
            )

            if serialized_values:

                stats[
                    "semantic_pair_rows"
                ] += 1

                return (
                    f"[Row {row_number}] "
                    + self.cell_separator.join(
                        serialized_values
                    ),
                    stats,
                )

        # ==============================================
        # Sparse Row
        # ==============================================
        #
        # 没有可信表头时，不再输出几十个：
        #
        #   |  |  |  |
        #
        # 仅输出有值 Cell，并用 Column A/B/... 保存位置语义。

        sparse_values: list[
            str
        ] = []

        non_empty_count = sum(
            1
            for value
            in cleaned_cells
            if value
        )

        for (
            column_index,
            value,
        ) in enumerate(
            cleaned_cells,
            start=1,
        ):

            if not value:
                continue

            # 只有一个非空 Cell 时，
            # 直接输出 Value，减少无意义 Column 标签。
            if (
                non_empty_count
                == 1
            ):

                sparse_values.append(
                    value
                )

            else:

                sparse_values.append(
                    self._format_unlabeled_value(
                        column_index=(
                            column_index
                        ),
                        value=(
                            value
                        ),
                    )
                )

        if not sparse_values:

            stats[
                "empty_rows"
            ] += 1

            return (
                "",
                stats,
            )

        stats[
            "sparse_rows"
        ] += 1

        return (
            f"[Row {row_number}] "
            + self.cell_separator.join(
                sparse_values
            ),
            stats,
        )

    # ==================================================
    # RAG Cell Cleaning
    # ==================================================

    def _clean_cells_for_rag(
        self,
        cells: list[
            Any
        ],
    ) -> list[
        str
    ]:

        cleaned: list[
            str
        ] = []

        for cell in cells:

            value = (
                self._normalize_text(
                    cell
                )
            )

            if (
                self.exclude_formulas_from_rag
                and self._is_formula(
                    value
                )
            ):

                cleaned.append(
                    ""
                )

                continue

            if (
                self.exclude_excel_errors_from_rag
                and self._is_excel_error(
                    value
                )
            ):

                cleaned.append(
                    ""
                )

                continue

            cleaned.append(
                value
            )

        return (
            cleaned
        )

    # ==================================================
    # Formula / Error Detection
    # ==================================================

    @classmethod
    def _is_formula(
        cls,
        value: str,
    ) -> bool:

        if not value:
            return False

        return bool(
            cls._FORMULA_PREFIX_PATTERN.match(
                value
            )
        )

    @classmethod
    def _is_excel_error(
        cls,
        value: str,
    ) -> bool:

        if not value:
            return False

        normalized = (
            value.strip().upper()
        )

        return (
            normalized
            in cls._EXCEL_ERROR_VALUES
        )

    # ==================================================
    # Column Label
    # ==================================================

    def _format_unlabeled_value(
        self,
        *,
        column_index: int,
        value: str,
    ) -> str:

        if not self.use_column_labels_for_unlabeled_values:

            return (
                value
            )

        return (
            f"Column "
            f"{self._column_label(column_index)}: "
            f"{value}"
        )

    @staticmethod
    def _column_label(
        column_index: int,
    ) -> str:
        """
        1 -> A
        26 -> Z
        27 -> AA
        """

        if column_index <= 0:

            raise ValueError(
                "column_index must be greater than 0."
            )

        result: list[
            str
        ] = []

        value = int(
            column_index
        )

        while value > 0:

            value, remainder = divmod(
                value - 1,
                26,
            )

            result.append(
                chr(
                    ord(
                        "A"
                    )
                    + remainder
                )
            )

        return "".join(
            reversed(
                result
            )
        )

    # ==================================================
    # Resolvers
    # ==================================================

    @staticmethod
    def _resolve_sheet_index(
        block: DocumentBlock,
    ) -> int:

        metadata = (
            block.metadata
            or {}
        )

        sheet_index = metadata.get(
            "sheet_index"
        )

        if sheet_index is None:

            sheet_index = (
                block.table_index
            )

        if sheet_index is None:

            return 0

        return int(
            sheet_index
        )

    @classmethod
    def _resolve_sheet_name(
        cls,
        blocks: list[
            DocumentBlock
        ],
        sheet_index: int,
    ) -> str:

        for block in blocks:

            metadata = (
                block.metadata
                or {}
            )

            sheet_name = (
                metadata.get(
                    "sheet_name"
                )
            )

            if sheet_name:

                normalized = (
                    cls._normalize_text(
                        str(
                            sheet_name
                        )
                    )
                )

                if normalized:

                    return (
                        normalized
                    )

        return (
            f"Sheet "
            f"{sheet_index + 1}"
        )

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

            return int(
                row_number
            )

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

        return (
            block.order
            + 1
        )

    @staticmethod
    def _resolve_page_number(
        blocks: list[
            DocumentBlock
        ],
    ) -> int | None:

        for block in blocks:

            if (
                block.page_number
                is not None
            ):

                return (
                    block.page_number
                )

        return None

    @classmethod
    def _block_sort_key(
        cls,
        block: DocumentBlock,
    ) -> tuple[
        int,
        int,
    ]:

        return (
            cls._resolve_row_number(
                block
            ),
            block.order,
        )

    # ==================================================
    # Text
    # ==================================================

    @staticmethod
    def _normalize_text(
        text: Any,
    ) -> str:

        if text is None:
            return ""

        normalized = str(
            text
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

        normalized = (
            normalized.replace(
                "\u200b",
                "",
            )
        )

        normalized = (
            normalized.replace(
                "\ufeff",
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

        normalized = (
            " / ".join(
                " ".join(
                    line.split()
                )
                for line
                in normalized.splitlines()
                if line.strip()
            )
        )

        return (
            normalized.strip()
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
                "XLSXParser expects an "
                "app.model.document.Document instance."
            )

        file_type = str(
            document.file_type
            or ""
        ).strip().lower()

        if file_type != "xlsx":

            raise ValueError(
                "XLSXParser only accepts XLSX documents. "
                f"Received file_type: "
                f"{document.file_type}"
            )

        # Content availability is handled by ImportResiliencePolicy in parse().
        return None
