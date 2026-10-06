from __future__ import annotations

from pathlib import Path

from app.builder.json_builder import JsonBuilder
from app.analyzer.specification_classifier import SpecificationClassifier

from app.filter.common.content_filter import ContentFilter
from app.filter.pdf.header_footer_filter import HeaderFooterFilter
from app.filter.pdf.page_filter import PageFilter

from app.loader.pdf_loader import PDFLoader

from app.parser.pdf_parser import PDFParser

from app.processor.chunker import Chunker
from app.processor.deduplicator import Deduplicator
from app.processor.section_hierarchy import SectionHierarchyBuilder
from app.processor.sort_order_assigner import SortOrderAssigner
from app.processor.token_counter import TokenCounter

from app.storage.postgres_storage import PostgresStorage


class PDFPipeline:
    """
    PDF 文档摄取 Pipeline。

    流程：
        1. 加载 PDF
        2. 过滤空页和目录页
        3. 清理页眉页脚
        4. 解析章节、节和正文
        5. 去重
        6. 建立 Section 层级
        7. 清理无效正文
        8. Chunk 分块
        9. 分配排序字段和 Chunk Index
        10. 统计最终 Chunk Token
        11. 写入 Pipeline Metadata
        12. 输出 JSON
        13. 保存 PostgreSQL
    """

    def __init__(
        self,
        *,
        chunk_max_length: int = 1000,
        save_json: bool = True,
        save_database: bool = True,
        project_code: str | None = None,
    ) -> None:

        if chunk_max_length <= 0:
            raise ValueError(
                "chunk_max_length must be greater than 0."
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

        self.loader = PDFLoader()

        # =====================
        # PDF Filters
        # =====================

        self.page_filter = PageFilter()

        self.header_footer_filter = (
            HeaderFooterFilter()
        )

        self.content_filter = ContentFilter()

        # =====================
        # Parser
        # =====================

        self.parser = PDFParser()

        # =====================
        # Common Processors
        # =====================

        self.deduplicator = Deduplicator()

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

        self.token_counter = TokenCounter()

        # =====================
        # Output
        # =====================

        self.builder = JsonBuilder()

        # PostgreSQL 延迟初始化。
        #
        # save_database=False 时，
        # PDFPipeline 不应该初始化任何数据库资源。
        self.storage: PostgresStorage | None = None

    def run(
        self,
        file_path: str | Path,
        output: str | Path,
    ):
        """
        执行 PDF 摄取。

        Args:
            file_path:
                PDF 输入路径。

            output:
                JSON 输出路径。
                save_json=False 时不会访问此路径。

        Returns:
            完成解析、分块和 Token 统计后的 Document。
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
        # 2. Page Filter
        # =====================

        document = self.page_filter.filter(
            document
        )

        # =====================
        # 3. Header / Footer
        # =====================

        document = (
            self.header_footer_filter.filter(
                document
            )
        )

        # =====================
        # 4. Parse
        # =====================

        document = self.parser.parse(
            document
        )

        # =====================
        # 5. Deduplicate
        # =====================

        document = self.deduplicator.process(
            document
        )

        # =====================
        # 6. Section Hierarchy
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
        # 7. Content Filter
        # =====================

        document = (
            self.content_filter.filter(
                document
            )
        )

        # =====================
        # 8. Chunk
        # =====================

        document = self.chunker.process(
            document
        )

        # =====================
        # 9. Sort Order
        # =====================
        #
        # 必须在 Chunker 后执行。
        #
        # Chunker 会把一个原始 Content
        # 拆成多个最终 Content。
        #
        # 因此最终 chunk_index / sort_order
        # 应针对拆分完成后的 Content 分配。

        document = (
            self.sort_order_assigner.process(
                document
            )
        )

        # =====================
        # 10. Token Count
        # =====================

        document = self.token_counter.process(
            document
        )

        # =====================
        # 11. Metadata
        # =====================

        document.metadata.update(
            {
                "pipeline": "PDFPipeline",
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
                "save_json": (
                    self.save_json_enabled
                ),
                "save_database": (
                    self.save_database_enabled
                ),
            }
        )

        # =====================
        # 12. JSON
        # =====================

        if self.save_json_enabled:
            output_path = self._validate_output_path(
                output
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
        # 13. PostgreSQL
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

        并真正运行到数据库保存阶段时，
        才初始化 PostgresStorage。
        """

        if self.storage is None:
            self.storage = PostgresStorage(project_code=self.project_code)

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
                f"PDF file not found: {path}"
            )

        if not path.is_file():
            raise IsADirectoryError(
                f"Input path is not a file: {path}"
            )

        if path.suffix.lower() != ".pdf":
            raise ValueError(
                "PDFPipeline only accepts .pdf files. "
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