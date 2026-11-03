from __future__ import annotations

from typing import Any

from app.model.chapter import Chapter
from app.model.document import Document
from app.model.section import Section


class Deduplicator:
    """
    文档结构去重处理器。

    当前负责：

        - Chapter 去重
        - Section 去重
        - 合并重复结构中的缺失信息
        - 保留首次出现顺序
        - 写入去重统计 metadata

    不负责：

        - Content 文本去重
        - XLSX 重复业务行删除
        - Block 去重
        - Section 层级计算
        - Sort Order 分配

    去重原则：

        1. ID 相同视为同一结构对象。
        2. 保留第一次出现的对象和顺序。
        3. 后续重复对象只用于补充缺失字段。
        4. 已有非空字段不会被后续值强制覆盖。
        5. metadata 使用非破坏式合并。
    """

    def process(
        self,
        document: Document,
    ) -> Document:

        self._validate_document(
            document
        )

        original_chapter_count = len(
            document.chapters
        )

        original_section_count = len(
            document.sections
        )

        chapter_duplicate_count = (
            self.merge_chapters(
                document
            )
        )

        section_duplicate_count = (
            self.merge_sections(
                document
            )
        )

        document.metadata.update(
            {
                "deduplicator": "Deduplicator",
                "deduplicator_status": "SUCCESS",
                "chapter_count_before_dedup": (
                    original_chapter_count
                ),
                "chapter_count_after_dedup": len(
                    document.chapters
                ),
                "chapter_duplicate_count": (
                    chapter_duplicate_count
                ),
                "section_count_before_dedup": (
                    original_section_count
                ),
                "section_count_after_dedup": len(
                    document.sections
                ),
                "section_duplicate_count": (
                    section_duplicate_count
                ),
            }
        )

        return document

    def merge_chapters(
        self,
        document: Document,
    ) -> int:
        """
        合并重复 Chapter。

        Returns:
            被识别为重复项的 Chapter 数量。
        """

        unique: dict[str, Chapter] = {}
        result: list[Chapter] = []

        duplicate_count = 0

        for chapter in document.chapters:

            existing = unique.get(
                chapter.id
            )

            if existing is None:

                unique[
                    chapter.id
                ] = chapter

                result.append(
                    chapter
                )

                continue

            duplicate_count += 1

            self._merge_chapter(
                target=existing,
                source=chapter,
            )

        document.chapters = result

        return duplicate_count

    def merge_sections(
        self,
        document: Document,
    ) -> int:
        """
        合并重复 Section。

        Returns:
            被识别为重复项的 Section 数量。
        """

        unique: dict[str, Section] = {}
        result: list[Section] = []

        duplicate_count = 0

        for section in document.sections:

            existing = unique.get(
                section.id
            )

            if existing is None:

                unique[
                    section.id
                ] = section

                result.append(
                    section
                )

                continue

            duplicate_count += 1

            self._merge_section(
                target=existing,
                source=section,
            )

        document.sections = result

        return duplicate_count

    @classmethod
    def _merge_chapter(
        cls,
        *,
        target: Chapter,
        source: Chapter,
    ) -> None:
        """
        将重复 Chapter 的有效信息补充到首个 Chapter。

        已有非空值优先。
        """

        if (
            not target.title_jp
            and source.title_jp
        ):
            target.title_jp = (
                source.title_jp
            )

        if (
            not target.title_en
            and source.title_en
        ):
            target.title_en = (
                source.title_en
            )

        target.page_number = (
            cls._resolve_page_number(
                target.page_number,
                source.page_number,
            )
        )

        cls._merge_metadata(
            target=target.metadata,
            source=source.metadata,
        )

    @classmethod
    def _merge_section(
        cls,
        *,
        target: Section,
        source: Section,
    ) -> None:
        """
        将重复 Section 的有效信息补充到首个 Section。

        已有结构关系优先，不覆盖非空关系。
        """

        if (
            not target.chapter_id
            and source.chapter_id
        ):
            target.chapter_id = (
                source.chapter_id
            )

        if (
            not target.parent_section_id
            and source.parent_section_id
        ):
            target.parent_section_id = (
                source.parent_section_id
            )

        if (
            not target.title_jp
            and source.title_jp
        ):
            target.title_jp = (
                source.title_jp
            )

        if (
            not target.title_en
            and source.title_en
        ):
            target.title_en = (
                source.title_en
            )

        target.page_number = (
            cls._resolve_page_number(
                target.page_number,
                source.page_number,
            )
        )

        cls._merge_metadata(
            target=target.metadata,
            source=source.metadata,
        )

    @staticmethod
    def _resolve_page_number(
        current: int | None,
        incoming: int | None,
    ) -> int | None:
        """
        重复结构出现于多个页面时，
        使用最早出现的页码。
        """

        if current is None:
            return incoming

        if incoming is None:
            return current

        return min(
            current,
            incoming,
        )

    @staticmethod
    def _merge_metadata(
        *,
        target: dict[str, Any],
        source: dict[str, Any],
    ) -> None:
        """
        非破坏式合并 metadata。

        target 已存在的 key 保持不变。
        source 只补充 target 缺失的 key。
        """

        for key, value in source.items():

            if key not in target:
                target[
                    key
                ] = value

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
                "Deduplicator expects an "
                "app.model.document.Document instance."
            )