from __future__ import annotations

from pathlib import Path

from app.builder.json_builder import JsonBuilder
from app.analyzer.specification_classifier import SpecificationClassifier
from app.filter.common.content_filter import ContentFilter
from app.loader.txt_loader import TXTLoader
from app.normalizer.unicode_normalizer import UnicodeNormalizer
from app.parser.txt_parser import TXTParser
from app.processor.chunker import Chunker
from app.processor.deduplicator import Deduplicator
from app.processor.section_hierarchy import SectionHierarchyBuilder
from app.processor.sort_order_assigner import SortOrderAssigner
from app.processor.token_counter import TokenCounter
from app.storage.postgres_storage import PostgresStorage


class TXTPipeline:
    """
    TXT ingestion pipeline.

    Flow:
        TXTLoader
        -> UnicodeNormalizer
        -> TXTParser
        -> Deduplicator
        -> SectionHierarchyBuilder
        -> ContentFilter
        -> Chunker
        -> SortOrderAssigner
        -> TokenCounter
        -> JSON / PostgreSQL
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

        self.loader = TXTLoader()
        self.unicode_normalizer = UnicodeNormalizer()
        self.parser = TXTParser()
        self.deduplicator = Deduplicator()

        self.section_hierarchy = (
            SectionHierarchyBuilder()
        )

        self.content_filter = ContentFilter()

        self.specification_classifier = SpecificationClassifier(project_code=project_code)

        self.chunker = Chunker(
            max_length=chunk_max_length
        )

        self.sort_order_assigner = (
            SortOrderAssigner()
        )

        self.token_counter = TokenCounter()

        self.builder = (
            JsonBuilder()
            if self.save_json_enabled
            else None
        )

        # Lazy DB initialization:
        # no DB object is created when PostgreSQL output is disabled.
        self.storage = (
            PostgresStorage(project_code=self.project_code)
            if self.save_database_enabled
            else None
        )

    def run(
        self,
        file_path: str | Path,
        output: str | Path,
    ):

        input_path = (
            self._validate_input_path(
                file_path
            )
        )

        output_path = Path(
            output
        ).expanduser()

        document = self.loader.load(
            input_path
        )

        document = (
            self.unicode_normalizer.process(
                document
            )
        )

        document = self.parser.parse(
            document
        )

        document = self.deduplicator.process(
            document
        )

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

        document = self.content_filter.filter(
            document
        )

        document = self.chunker.process(
            document
        )

        # Must run after Chunker so the final chunks receive
        # deterministic ordering.
        document = (
            self.sort_order_assigner.process(
                document
            )
        )

        document = self.token_counter.process(
            document
        )

        document.metadata.update(
            {
                "pipeline": "TXTPipeline",
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
            }
        )

        if self.save_json_enabled:

            assert self.builder is not None

            output_path.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            json_data = self.builder.build(
                document
            )

            self.builder.save(
                json_data,
                str(
                    output_path
                ),
            )

        if self.save_database_enabled:

            assert self.storage is not None

            self.storage.save(
                document
            )

        return document

    @staticmethod
    def _validate_input_path(
        file_path: str | Path,
    ) -> Path:

        path = Path(
            file_path
        ).expanduser()

        if not path.exists():
            raise FileNotFoundError(
                f"TXT file not found: {path}"
            )

        if not path.is_file():
            raise IsADirectoryError(
                f"Input path is not a file: {path}"
            )

        if path.suffix.lower() != ".txt":
            raise ValueError(
                "TXTPipeline only accepts .txt files. "
                f"Received: "
                f"{path.suffix or '<no extension>'}"
            )

        return path
