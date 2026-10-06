from __future__ import annotations

from pathlib import Path

from app.builder.json_builder import JsonBuilder
from app.analyzer.specification_classifier import SpecificationClassifier

from app.filter.common.content_filter import ContentFilter
from app.filter.docx.paragraph_filter import ParagraphFilter
from app.filter.docx.table_filter import TableFilter

from app.loader.docx_loader import DOCXLoader

from app.normalizer.unicode_normalizer import UnicodeNormalizer

from app.parser.docx_parser import DOCXParser

from app.processor.chunker import Chunker
from app.processor.deduplicator import Deduplicator
from app.processor.heading_merger import HeadingMerger
from app.processor.section_hierarchy import SectionHierarchyBuilder
from app.processor.sort_order_assigner import SortOrderAssigner
from app.processor.title_sentence_corrector import (
    TitleSentenceCorrector,
)
from app.processor.token_counter import TokenCounter

from app.storage.postgres_storage import PostgresStorage


class DOCXPipeline:
    """
    DOCX 文档摄取 Pipeline。

    流程：
        1. 加载 DOCX
        2. Unicode 标准化
        3. 清理段落
        4. 清理表格文本
        5. 合并跨行标题
        6. 解析章节、节和正文
        7. 修正标题文本
        8. 去重
        9. 建立 Section 层级
        10. 清理无效正文
        11. Chunk 分块
        12. 分配排序字段和 Chunk Index
        13. 统计最终 Chunk Token
        14. 写入 Pipeline Metadata
        15. 输出 JSON
        16. 保存 PostgreSQL
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

        self.loader = DOCXLoader()

        # =====================
        # Normalizer
        # =====================

        self.unicode_normalizer = (
            UnicodeNormalizer()
        )

        # =====================
        # DOCX Filters
        # =====================

        self.paragraph_filter = (
            ParagraphFilter()
        )

        self.table_filter = (
            TableFilter()
        )

        self.content_filter = (
            ContentFilter()
        )

        # =====================
        # Parser
        # =====================

        self.parser = DOCXParser()

        # =====================
        # Processors
        # =====================

        self.heading_merger = (
            HeadingMerger()
        )

        self.title_sentence_corrector = (
            TitleSentenceCorrector()
        )

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
        # JSON-only 模式：
        #
        #     save_database=False
        #
        # 时不应该读取数据库 Secret、
        # 创建数据库连接配置或产生任何 DB 副作用。
        self.storage: PostgresStorage | None = (
            None
        )

    def run(
        self,
        file_path: str | Path,
        output: str | Path,
    ):
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
        # 3. Paragraph Filter
        # =====================

        document = (
            self.paragraph_filter.filter(
                document
            )
        )

        # =====================
        # 4. Table Filter
        # =====================

        document = (
            self.table_filter.filter(
                document
            )
        )

        # =====================
        # 5. Heading Merge
        # =====================

        document = (
            self.heading_merger.process(
                document
            )
        )

        # =====================
        # 6. Parse
        # =====================

        document = self.parser.parse(
            document
        )

        # =====================
        # 7. Title Correction
        # =====================

        document = (
            self.title_sentence_corrector.process(
                document
            )
        )

        # =====================
        # 8. Deduplicate
        # =====================

        document = (
            self.deduplicator.process(
                document
            )
        )

        # =====================
        # 9. Section Hierarchy
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
        # 10. Content Filter
        # =====================

        document = (
            self.content_filter.filter(
                document
            )
        )

        # =====================
        # 11. Chunk
        # =====================

        document = (
            self.chunker.process(
                document
            )
        )

        # =====================
        # 12. Sort Order
        # =====================
        #
        # 必须放在 Chunker 后。
        #
        # Chunker 会将一个原始 Content
        # 拆成多个最终 Content。
        #
        # 最终 chunk_index / sort_order
        # 应当针对 Chunk 后的数据进行分配。

        document = (
            self.sort_order_assigner.process(
                document
            )
        )

        # =====================
        # 13. Token Count
        # =====================

        document = (
            self.token_counter.process(
                document
            )
        )

        # =====================
        # 14. Metadata
        # =====================

        document.metadata.update(
            {
                "pipeline": "DOCXPipeline",
                "pipeline_status": "SUCCESS",
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
        # 15. JSON
        # =====================

        if self.save_json_enabled:
            output_path = Path(
                output
            ).expanduser()

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
        # 16. PostgreSQL
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

        只有实际要求：

            save_database=True

        并运行到 PostgreSQL 输出阶段时，
        才实例化 PostgresStorage。
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
                f"DOCX file not found: {path}"
            )

        if not path.is_file():
            raise IsADirectoryError(
                f"Input path is not a file: {path}"
            )

        if path.name.startswith(
            "~$"
        ):
            raise ValueError(
                "Temporary Word file is not supported: "
                f"{path.name}"
            )

        if path.suffix.lower() != ".docx":
            raise ValueError(
                "DOCXPipeline only accepts .docx files. "
                f"Received: "
                f"{path.suffix or '<no extension>'}"
            )

        return path