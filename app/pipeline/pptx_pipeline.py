from __future__ import annotations

from pathlib import Path

from app.builder.json_builder import JsonBuilder
from app.analyzer.specification_classifier import SpecificationClassifier

from app.filter.common.content_filter import ContentFilter
from app.filter.pptx.shape_filter import ShapeFilter
from app.filter.pptx.slide_filter import SlideFilter

from app.loader.pptx_loader import PPTXLoader

from app.normalizer.unicode_normalizer import UnicodeNormalizer

from app.parser.pptx_parser import PPTXParser

from app.processor.chunker import Chunker
from app.processor.deduplicator import Deduplicator
from app.processor.section_hierarchy import SectionHierarchyBuilder
from app.processor.sort_order_assigner import SortOrderAssigner
from app.processor.token_counter import TokenCounter

from app.storage.postgres_storage import PostgresStorage


class PPTXPipeline:
    """
    PPTX 文档摄取 Pipeline。

    流程：
        1. 加载 PPTX
        2. Unicode 标准化
        3. 过滤无效 Slide
        4. 清理 Shape / Block
        5. 解析 Slide、Section 和正文
        6. 去重
        7. 建立 Section 层级
        8. 清理无效正文
        9. Chunk 分块
        10. 分配排序字段和 Chunk Index
        11. Token 统计
        12. 写入 Pipeline Metadata
        13. 构建 JSON
        14. 保存 PostgreSQL

    默认结构映射：

        Slide
            -> Chapter

        Slide Title
            -> Chapter Title

        Secondary Heading
            -> Section

        Paragraph / List / Table / Chart / Image Caption
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

        include_hidden_slides: bool = False,
        include_empty_slides: bool = True,
        include_images: bool = True,
        include_charts: bool = True,
        include_empty_image_blocks: bool = True,
        include_empty_chart_blocks: bool = True,
        extract_chart_data: bool = True,
        extract_text_per_paragraph: bool = True,
        extract_table_per_row: bool = True,
        recursive_group_shapes: bool = True,
        reading_order: str = "visual",

        # =====================
        # Slide filter options
        # =====================

        remove_hidden_slides: bool = True,
        remove_empty_slides: bool = True,
        exclude_default_slide_titles: bool = False,
        minimum_slide_block_count: int = 1,
        minimum_slide_text_length: int = 1,

        # =====================
        # Shape filter options
        # =====================

        remove_duplicate_blocks: bool = True,
        remove_page_numbers: bool = True,
        remove_dates: bool = False,
        remove_version_lines: bool = True,
        remove_static_noise: bool = True,
        remove_repeated_headers_footers: bool = True,

        # =====================
        # Parser options
        # =====================

        use_original_slide_number_as_chapter_id: bool = True,
        create_section_from_secondary_heading: bool = True,
        create_default_section_when_missing: bool = False,
        include_slide_title_in_content: bool = False,
        include_secondary_heading_in_content: bool = False,
        include_image_blocks_in_content: bool = True,
        include_chart_blocks_in_content: bool = True,
        include_table_headers: bool = True,
        merge_adjacent_content_blocks: bool = True,
    ) -> None:

        if chunk_max_length <= 0:
            raise ValueError(
                "chunk_max_length must be greater than 0."
            )

        if minimum_slide_block_count < 0:
            raise ValueError(
                "minimum_slide_block_count "
                "must be greater than or equal to 0."
            )

        if minimum_slide_text_length < 0:
            raise ValueError(
                "minimum_slide_text_length "
                "must be greater than or equal to 0."
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

        self.loader = PPTXLoader(
            include_hidden_slides=(
                include_hidden_slides
            ),
            include_empty_slides=(
                include_empty_slides
            ),
            include_images=(
                include_images
            ),
            include_charts=(
                include_charts
            ),
            include_empty_image_blocks=(
                include_empty_image_blocks
            ),
            include_empty_chart_blocks=(
                include_empty_chart_blocks
            ),
            extract_chart_data=(
                extract_chart_data
            ),
            extract_text_per_paragraph=(
                extract_text_per_paragraph
            ),
            extract_table_per_row=(
                extract_table_per_row
            ),
            recursive_group_shapes=(
                recursive_group_shapes
            ),
            reading_order=(
                reading_order
            ),
        )

        # =====================
        # Normalizer
        # =====================

        self.unicode_normalizer = (
            UnicodeNormalizer()
        )

        # =====================
        # PPTX Filters
        # =====================

        self.slide_filter = SlideFilter(
            remove_hidden_slides=(
                remove_hidden_slides
            ),
            remove_empty_slides=(
                remove_empty_slides
            ),
            minimum_block_count=(
                minimum_slide_block_count
            ),
            minimum_text_length=(
                minimum_slide_text_length
            ),
            exclude_default_titles=(
                exclude_default_slide_titles
            ),
            rebuild_pages=True,
            reassign_block_order=True,
        )

        self.shape_filter = ShapeFilter(
            remove_empty_text_blocks=True,
            remove_empty_table_rows=True,
            remove_empty_image_blocks=False,
            remove_empty_chart_blocks=False,
            remove_duplicate_blocks=(
                remove_duplicate_blocks
            ),
            duplicate_scope="slide",
            remove_page_numbers=(
                remove_page_numbers
            ),
            remove_dates=(
                remove_dates
            ),
            remove_version_lines=(
                remove_version_lines
            ),
            remove_static_noise=(
                remove_static_noise
            ),
            remove_repeated_headers_footers=(
                remove_repeated_headers_footers
            ),
            clean_text=True,
            clean_cells=True,
            rebuild_pages=True,
            reassign_block_order=True,
        )

        self.content_filter = (
            ContentFilter()
        )

        # =====================
        # Parser
        # =====================

        self.parser = PPTXParser(
            use_original_slide_number_as_chapter_id=(
                use_original_slide_number_as_chapter_id
            ),
            create_section_from_secondary_heading=(
                create_section_from_secondary_heading
            ),
            create_default_section_when_missing=(
                create_default_section_when_missing
            ),
            include_slide_title_in_content=(
                include_slide_title_in_content
            ),
            include_secondary_heading_in_content=(
                include_secondary_heading_in_content
            ),
            include_image_blocks=(
                include_image_blocks_in_content
            ),
            include_chart_blocks=(
                include_chart_blocks_in_content
            ),
            include_table_headers=(
                include_table_headers
            ),
            merge_adjacent_content_blocks=(
                merge_adjacent_content_blocks
            ),
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

        # PostgreSQL 延迟初始化。
        #
        # save_database=False 时：
        #
        #     - 不创建 PostgresStorage
        #     - 不读取数据库 Secret
        #     - 不建立数据库相关资源
        self.storage: PostgresStorage | None = None

    def run(
        self,
        file_path: str | Path,
        output: str | Path,
    ):
        """
        执行 PPTX 摄取。

        Args:
            file_path:
                PPTX 输入路径。

            output:
                JSON 输出路径。

                save_json=False 时，
                不会访问该路径。

        Returns:
            解析、过滤、分块并完成 Token
            统计后的 Document。
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
        # 3. Slide Filter
        # =====================

        document = (
            self.slide_filter.filter(
                document
            )
        )

        # =====================
        # 4. Shape Filter
        # =====================

        document = (
            self.shape_filter.filter(
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
        # 必须在 Chunker 后执行。
        #
        # 一个 Parser Content 可能被 Chunker
        # 拆成多个最终 Content。
        #
        # 因此 chunk_index / sort_order
        # 应针对最终 Chunk 重新分配。

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
                "pipeline": "PPTXPipeline",
                "pipeline_status": "SUCCESS",
                "page_count": len(
                    document.pages
                ),
                "block_count": len(
                    document.blocks
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

        仅当：

            save_database=True

        并真正执行数据库输出阶段时，
        才创建 PostgresStorage。
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
                f"PPTX file not found: {path}"
            )

        if not path.is_file():
            raise IsADirectoryError(
                f"Input path is not a file: {path}"
            )

        if path.name.startswith(
            "~$"
        ):
            raise ValueError(
                "Temporary PowerPoint file "
                "is not supported: "
                f"{path.name}"
            )

        if path.suffix.lower() != ".pptx":
            raise ValueError(
                "PPTXPipeline only accepts "
                ".pptx files. "
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