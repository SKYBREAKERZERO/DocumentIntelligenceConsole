from __future__ import annotations

from pathlib import Path

from app.builder.json_builder import JsonBuilder
from app.analyzer.specification_classifier import SpecificationClassifier

from app.filter.common.content_filter import ContentFilter
from app.filter.xlsx.row_filter import RowFilter
from app.filter.xlsx.sheet_filter import SheetFilter

from app.loader.xlsx_loader import XLSXLoader

from app.normalizer.unicode_normalizer import UnicodeNormalizer

from app.parser.xlsx_parser import XLSXParser

from app.processor.chunker import Chunker
from app.processor.deduplicator import Deduplicator
from app.processor.section_hierarchy import SectionHierarchyBuilder
from app.processor.sort_order_assigner import SortOrderAssigner
from app.processor.token_counter import TokenCounter

from app.storage.postgres_storage import PostgresStorage


class XLSXPipeline:
    """
    XLSX 文档摄取 Pipeline。

    流程：
        1. 加载 XLSX
        2. Unicode 标准化
        3. 过滤无效 Sheet
        4. 清理无效行
        5. 解析 Sheet、数据区域和正文
        6. 去重
        7. 建立 Section 层级
        8. 清理无效正文
        9. Chunk 分块
        10. 分配排序字段和 Chunk Index
        11. 统计最终 Chunk Token
        12. 写入 Pipeline Metadata
        13. 输出 JSON
        14. 保存 PostgreSQL

    默认结构映射：

        Worksheet
            -> Chapter

        连续数据区域
            -> Section

        数据行
            -> Content
    """

    def __init__(
        self,
        *,
        chunk_max_length: int = 1000,
        save_json: bool = True,
        save_database: bool = True,
        project_code: str | None = None,

        # =====================
        # Loader options
        # =====================

        loader_read_only: bool = True,
        loader_data_only: bool = False,

        # =====================
        # Sheet options
        # =====================

        include_hidden_sheets: bool = False,
        include_very_hidden_sheets: bool = False,
        exclude_default_sheet_names: bool = False,

        # =====================
        # Row filter options
        # =====================

        remove_duplicate_rows: bool = False,
        remove_comment_rows: bool = False,
        remove_summary_rows: bool = False,

        # =====================
        # Parser options
        # =====================

        first_row_as_header: bool = True,
        include_header_in_content: bool = True,
        detect_multiple_regions: bool = True,
        maximum_row_gap: int = 1,
    ) -> None:

        if chunk_max_length <= 0:
            raise ValueError(
                "chunk_max_length must be greater than 0."
            )

        if maximum_row_gap < 1:
            raise ValueError(
                "maximum_row_gap must be at least 1."
            )

        self.save_json_enabled = bool(
            save_json
        )

        self.save_database_enabled = bool(
            save_database
        )

        self.project_code = project_code

        # =====================
        # Loader
        # =====================
        #
        # Loader 层尽量无损读取 Sheet。
        #
        # 是否最终保留 hidden / veryHidden Sheet，
        # 统一交给 SheetFilter 决定。
        #
        # 这样：
        #
        #     - Loader metadata 更完整
        #     - SheetFilter 可以记录删除原因
        #     - 不会在 Loader 阶段提前丢失结构信息

        self.loader = XLSXLoader(
            read_only=loader_read_only,
            data_only=loader_data_only,
            include_hidden_sheets=True,
            include_very_hidden_sheets=True,
        )

        # =====================
        # Normalizer
        # =====================

        self.unicode_normalizer = (
            UnicodeNormalizer()
        )

        # =====================
        # XLSX Filters
        # =====================

        self.sheet_filter = SheetFilter(
            remove_empty_sheets=True,
            remove_hidden_sheets=(
                not include_hidden_sheets
            ),
            remove_very_hidden_sheets=(
                not include_very_hidden_sheets
            ),
            exclude_default_names=(
                exclude_default_sheet_names
            ),
            minimum_non_empty_rows=1,
        )

        self.row_filter = RowFilter(
            remove_empty_rows=True,
            remove_duplicate_rows=(
                remove_duplicate_rows
            ),
            remove_error_only_rows=True,
            remove_comment_rows=(
                remove_comment_rows
            ),
            remove_summary_rows=(
                remove_summary_rows
            ),
            minimum_non_empty_cells=1,
            minimum_text_length=1,
            duplicate_scope="sheet",

            # 表格业务数据通常大小写有意义。
            #
            # 例如：
            #
            #     abc
            #     ABC
            #
            # 不应默认视为完全相同的业务行。
            case_sensitive_duplicates=True,

            strip_cells=True,
            collapse_internal_spaces=True,
            rebuild_pages=True,
            reassign_block_order=True,
        )

        self.content_filter = (
            ContentFilter()
        )

        # =====================
        # Parser
        # =====================

        self.parser = XLSXParser(
            first_row_as_header=(
                first_row_as_header
            ),
            detect_multiple_regions=(
                detect_multiple_regions
            ),
            maximum_row_gap=(
                maximum_row_gap
            ),
            include_header_in_content=(
                include_header_in_content
            ),
            include_single_row_regions=True,
        )

        # =====================
        # Common Processors
        # =====================

        self.deduplicator = (
            Deduplicator()
        )

        self.section_hierarchy = (
            SectionHierarchyBuilder()
        )

        self.specification_classifier = SpecificationClassifier(project_code=project_code)

        self.chunker = Chunker(
            max_length=chunk_max_length
        )

        self.sort_order_assigner = (
            SortOrderAssigner()
        )

        self.token_counter = (
            TokenCounter()
        )

        # =====================
        # Output
        # =====================

        self.builder = JsonBuilder()

        # PostgreSQL 必须延迟初始化。
        #
        # save_database=False 时：
        #
        #     - 不创建 PostgresStorage
        #     - 不读取数据库 Secret
        #     - 不触发数据库配置验证
        self.storage: PostgresStorage | None = None

    def run(
        self,
        file_path: str | Path,
        output: str | Path,
    ):
        """
        执行 XLSX 摄取。

        Args:
            file_path:
                XLSX 输入路径。

            output:
                JSON 输出路径。

                save_json=False 时，
                不会读取或验证该路径。

        Returns:
            解析、过滤、分块并完成
            Token 统计后的 Document。
        """

        input_path = self._validate_input_path(
            file_path
        )

        # =====================
        # 1. Load
        # =====================

        document = self.loader.load(
            str(input_path)
        )

        # =====================
        # 2. Unicode Normalize
        # =====================

        document = (
            self.unicode_normalizer.process(
                document
            )
        )

        # =====================
        # 3. Sheet Filter
        # =====================

        document = (
            self.sheet_filter.filter(
                document
            )
        )

        # =====================
        # 4. Row Filter
        # =====================

        document = (
            self.row_filter.filter(
                document
            )
        )

        # =====================
        # 5. Parse
        # =====================

        document = self.parser.parse(
            document
        )

        # =====================
        # 6. Deduplicate
        # =====================

        document = (
            self.deduplicator.process(
                document
            )
        )

        # =====================
        # 7. Section Hierarchy
        # =====================

        document = (
            self.section_hierarchy.process(
                document
            )
        )

        # =====================
        # Specification Classification
        # =====================

        document = self.specification_classifier.process(
            document
        )

        # =====================
        # 8. Content Filter
        # =====================

        document = (
            self.content_filter.filter(
                document
            )
        )

        # =====================
        # 9. Chunk
        # =====================

        document = (
            self.chunker.process(
                document
            )
        )

        # =====================
        # 10. Sort Order
        # =====================
        #
        # SortOrderAssigner 必须放在 Chunker 后。
        #
        # 一个 XLSX 数据行生成的 Content
        # 可能因为文本过长而被拆成：
        #
        #     Content 1
        #         -> Chunk 1
        #         -> Chunk 2
        #
        #     Content 2
        #         -> Chunk 1
        #         -> Chunk 2
        #
        # 最终 chunk_index 应针对最终 Content
        # 集合统一分配，而不是在拆分前分配。

        document = (
            self.sort_order_assigner.process(
                document
            )
        )

        # =====================
        # 11. Token Count
        # =====================

        document = (
            self.token_counter.process(
                document
            )
        )

        # =====================
        # 12. Metadata
        # =====================

        document.metadata.update(
            {
                "pipeline": "XLSXPipeline",
                "pipeline_status": ("SUCCESS_WITH_WARNING" if (document.metadata.get("import_warning_codes") or []) else "SUCCESS"),
                "chapter_count": len(
                    document.chapters
                ),
                "section_count": len(
                    document.sections
                ),
                "content_count": len(
                    document.contents
                ),
                "page_count": len(
                    document.pages
                ),
                "block_count": len(
                    document.blocks
                ),
                "save_json": (
                    self.save_json_enabled
                ),
                "save_database": (
                    self.save_database_enabled
                ),
            }
        )

        # =====================
        # 13. JSON
        # =====================

        if self.save_json_enabled:
            output_path = (
                self._validate_output_path(
                    output
                )
            )

            output_path.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            json_data = self.builder.build(
                document
            )

            self.builder.save(
                json_data,
                str(output_path),
            )

        # =====================
        # 14. PostgreSQL
        # =====================

        if self.save_database_enabled:
            storage = self._get_storage()

            storage.save(
                document
            )

        return document

    def _get_storage(
        self,
    ) -> PostgresStorage:
        """
        延迟创建 PostgreSQL Storage。

        只有真正执行：

            save_database=True

        的数据库保存阶段时才初始化。
        """

        if self.storage is None:
            self.storage = (
                PostgresStorage(project_code=self.project_code)
            )

        return self.storage

    @staticmethod
    def _validate_input_path(
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
                f"XLSX file not found: {path}"
            )

        if not path.is_file():
            raise IsADirectoryError(
                f"Input path is not a file: {path}"
            )

        if path.name.startswith(
            "~$"
        ):
            raise ValueError(
                "Temporary Excel file "
                "is not supported: "
                f"{path.name}"
            )

        if path.suffix.lower() != ".xlsx":
            raise ValueError(
                "XLSXPipeline only accepts "
                ".xlsx files. "
                f"Received: "
                f"{path.suffix or '<no extension>'}"
            )

        return path

    @staticmethod
    def _validate_output_path(
        output: str | Path,
    ) -> Path:

        if output is None:
            raise ValueError(
                "output cannot be None when "
                "save_json=True."
            )

        if not str(
            output
        ).strip():
            raise ValueError(
                "output cannot be empty when "
                "save_json=True."
            )

        return Path(
            output
        ).expanduser()