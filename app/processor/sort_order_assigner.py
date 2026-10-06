from __future__ import annotations

from app.model.document import Document


class SortOrderAssigner:
    """
    文档结构排序值分配器。

    负责：
        - 按当前列表中的实际出现顺序分配 sort_order
        - Chapter、Section 分别独立编号
        - 可选为 Content 重新分配 chunk_index
        - 写入处理统计信息

    不负责：
        - 修改章节编号
        - 修改父子关系
        - 重新排序文档结构
        - 判断标题是否合法

    示例：

        Sections 当前顺序：

            2.2
            2.2.1
            2.2.1.1

        分配结果：

            2.2       sort_order=1
            2.2.1     sort_order=2
            2.2.1.1   sort_order=3
    """

    def __init__(
        self,
        *,
        start_order: int = 1,
        assign_content_chunk_index: bool = True,
    ) -> None:

        if start_order < 0:
            raise ValueError(
                "start_order cannot be negative."
            )

        self.start_order = start_order
        self.assign_content_chunk_index = (
            assign_content_chunk_index
        )

    def process(
        self,
        document: Document,
    ) -> Document:

        self._validate_document(
            document
        )

        chapter_count = self._assign_chapter_order(
            document
        )

        section_count = self._assign_section_order(
            document
        )

        content_count = 0

        if self.assign_content_chunk_index:
            content_count = (
                self._assign_content_chunk_indexes(
                    document
                )
            )

        document.metadata.update(
            {
                "sort_order_assigner": (
                    "SortOrderAssigner"
                ),
                "sort_order_assigner_status": (
                    "SUCCESS"
                ),
                "chapter_sort_order_count": (
                    chapter_count
                ),
                "section_sort_order_count": (
                    section_count
                ),
                "content_chunk_index_count": (
                    content_count
                ),
            }
        )

        return document

    def _assign_chapter_order(
        self,
        document: Document,
    ) -> int:

        order = self.start_order

        for chapter in document.chapters:
            chapter.sort_order = order
            order += 1

        return len(
            document.chapters
        )

    def _assign_section_order(
        self,
        document: Document,
    ) -> int:

        order = self.start_order

        for section in document.sections:
            section.sort_order = order
            order += 1

        return len(
            document.sections
        )

    @staticmethod
    def _assign_content_chunk_indexes(
        document: Document,
    ) -> int:
        """
        按 document.contents 当前顺序，为每个 section
        独立分配 chunk_index。

        同一个 section：

            chunk_index = 0
            chunk_index = 1
            chunk_index = 2

        不同 section 重新从 0 开始。
        """

        counters: dict[
            tuple[str | None, str | None],
            int
        ] = {}

        assigned_count = 0

        for content in document.contents:
            key = (
                content.chapter_id,
                content.section_id,
            )

            current_index = counters.get(
                key,
                0,
            )

            content.chunk_index = (
                current_index
            )

            counters[key] = (
                current_index + 1
            )

            assigned_count += 1

        return assigned_count

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
                "SortOrderAssigner expects an "
                "app.model.document.Document instance."
            )