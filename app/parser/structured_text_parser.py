from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from app.model.block import BlockType, DocumentBlock
from app.model.chapter import Chapter
from app.model.content import Content
from app.model.document import Document
from app.model.section import Section
from app.analyzer.import_resilience_policy import ImportResiliencePolicy


class StructuredTextParser:
    """
    TXT / OCR image 共用的轻量结构解析器。

    支持的标题形式：

        1 Overview
        1.1 Purpose
        1.1.1 Detail

        １ Overview
        １．１ Purpose

        # Overview
        ## Purpose
        ### Detail

    无标题普通文本也不会丢失：
        自动建立一个 Chapter，并将正文写入 Content。

    设计原则：
        - 不依赖 DOCXParser，避免 file_type="docx" 限制。
        - 不过度猜测普通短句是否为标题。
        - 保留 Chapter / Section / Content 统一模型。
    """

    _NUMBERED_HEADING_PATTERN = re.compile(
        r"""
        ^
        (?P<number>
            [0-9０-９]+
            (?:
                [\.．]
                [0-9０-９]+
            )*
        )
        [\s　]+
        (?P<title>.+?)
        $
        """,
        re.VERBOSE,
    )

    _MARKDOWN_HEADING_PATTERN = re.compile(
        r"""
        ^
        (?P<marks>\#{1,6})
        [\s　]+
        (?P<title>.+?)
        $
        """,
        re.VERBOSE,
    )

    _FULLWIDTH_TRANSLATION = str.maketrans(
        {
            "０": "0",
            "１": "1",
            "２": "2",
            "３": "3",
            "４": "4",
            "５": "5",
            "６": "6",
            "７": "7",
            "８": "8",
            "９": "9",
            "．": ".",
        }
    )

    def __init__(
        self,
        *,
        parser_name: str = "StructuredTextParser",
    ) -> None:

        self.parser_name = str(
            parser_name
        ).strip() or "StructuredTextParser"

    def parse(
        self,
        document: Document,
    ) -> Document:

        self._validate_document(
            document
        )

        document.chapters.clear()
        document.sections.clear()
        document.contents.clear()

        source_blocks = [
            block
            for block
            in sorted(
                document.blocks,
                key=lambda item: item.order,
            )
            if str(
                block.text
                or ""
            ).strip()
        ]

        if not source_blocks:
            return ImportResiliencePolicy.mark_metadata_only(
                document,
                warning_code="TEXT_NO_EXTRACTABLE_BLOCKS",
                parser_name=self.parser_name,
                warning_message="Document contains no extractable text blocks; stored as metadata-only and excluded from Qdrant.",
            )

        chapter_map: dict[
            str,
            Chapter,
        ] = {}

        section_map: dict[
            str,
            Section,
        ] = {}

        current_chapter: Chapter | None = None
        current_section: Section | None = None

        content_buffer: list[str] = []
        content_page_number: int | None = None

        heading_count = 0
        numbered_heading_count = 0
        markdown_heading_count = 0
        fallback_chapter_count = 0

        markdown_counters = [
            0,
            0,
            0,
            0,
            0,
            0,
        ]

        def ensure_fallback_chapter(
            *,
            page_number: int = 1,
        ) -> Chapter:

            nonlocal fallback_chapter_count

            fallback_id = "1"

            if fallback_id in chapter_map:
                return chapter_map[
                    fallback_id
                ]

            title = (
                Path(
                    document.file_name
                ).stem.strip()
                or "Document"
            )

            chapter = Chapter(
                id=fallback_id,
                title_jp=title,
                title_en=None,
                level=1,
                page_number=max(
                    int(
                        page_number
                        or 1
                    ),
                    1,
                ),
                metadata={
                    "generated_fallback": True,
                    "source_format": (
                        document.file_type
                    ),
                },
            )

            document.chapters.append(
                chapter
            )

            chapter_map[
                fallback_id
            ] = chapter

            fallback_chapter_count += 1

            return chapter

        def flush_content() -> None:

            nonlocal content_buffer
            nonlocal content_page_number
            nonlocal current_chapter

            if not content_buffer:
                return

            text = "\n".join(
                content_buffer
            ).strip()

            content_buffer = []

            if not text:
                content_page_number = None
                return

            if current_chapter is None:
                current_chapter = (
                    ensure_fallback_chapter(
                        page_number=(
                            content_page_number
                            or 1
                        )
                    )
                )

            document.contents.append(
                Content(
                    chapter_id=(
                        current_chapter.id
                    ),
                    section_id=(
                        current_section.id
                        if current_section
                        else None
                    ),
                    text=text,
                    page_number=(
                        content_page_number
                        or 1
                    ),
                )
            )

            content_page_number = None

        for block in source_blocks:

            text = self._normalize_text(
                block.text
            )

            if not text:
                continue

            heading = self._detect_heading(
                block=block,
                markdown_counters=(
                    markdown_counters
                ),
            )

            if heading is None:

                if content_page_number is None:
                    content_page_number = (
                        block.page_number
                        or 1
                    )

                content_buffer.append(
                    text
                )

                continue

            flush_content()

            heading_count += 1

            heading_id = heading[
                "id"
            ]

            heading_title = heading[
                "title"
            ]

            heading_level = int(
                heading[
                    "level"
                ]
            )

            heading_kind = heading[
                "kind"
            ]

            if heading_kind == "numbered":
                numbered_heading_count += 1
            else:
                markdown_heading_count += 1

            if heading_level == 1:

                chapter = chapter_map.get(
                    heading_id
                )

                if chapter is None:

                    chapter = Chapter(
                        id=heading_id,
                        title_jp=heading_title,
                        title_en=None,
                        level=1,
                        page_number=(
                            block.page_number
                            or 1
                        ),
                        metadata={
                            "heading_kind": (
                                heading_kind
                            ),
                        },
                    )

                    document.chapters.append(
                        chapter
                    )

                    chapter_map[
                        heading_id
                    ] = chapter

                elif chapter.metadata.get(
                    "generated_fallback",
                    False,
                ):

                    chapter.title_jp = (
                        heading_title
                    )

                    chapter.page_number = (
                        block.page_number
                        or chapter.page_number
                        or 1
                    )

                    chapter.metadata.pop(
                        "generated_fallback",
                        None,
                    )

                    chapter.metadata[
                        "heading_kind"
                    ] = heading_kind

                current_chapter = chapter
                current_section = None

                continue

            chapter_id = (
                heading_id.split(
                    ".",
                    1,
                )[0]
            )

            if (
                not chapter_id
                or not chapter_id.isdigit()
            ):
                chapter_id = (
                    current_chapter.id
                    if current_chapter
                    else "1"
                )

            if chapter_id not in chapter_map:

                fallback = Chapter(
                    id=chapter_id,
                    title_jp=(
                        f"Chapter {chapter_id}"
                    ),
                    title_en=None,
                    level=1,
                    page_number=(
                        block.page_number
                        or 1
                    ),
                    metadata={
                        "generated_fallback": True,
                        "source_format": (
                            document.file_type
                        ),
                    },
                )

                document.chapters.append(
                    fallback
                )

                chapter_map[
                    chapter_id
                ] = fallback

                fallback_chapter_count += 1

            current_chapter = chapter_map[
                chapter_id
            ]

            normalized_section_id = (
                self._normalize_section_id(
                    heading_id=heading_id,
                    chapter_id=chapter_id,
                    section_map=section_map,
                )
            )

            section = section_map.get(
                normalized_section_id
            )

            if section is None:

                section = Section(
                    id=normalized_section_id,
                    title_jp=heading_title,
                    title_en=None,
                    level=max(
                        heading_level,
                        2,
                    ),
                    chapter_id=chapter_id,
                    parent_section_id=None,
                    page_number=(
                        block.page_number
                        or 1
                    ),
                    metadata={
                        "heading_kind": (
                            heading_kind
                        ),
                    },
                )

                document.sections.append(
                    section
                )

                section_map[
                    normalized_section_id
                ] = section

            current_section = section

        flush_content()

        # 完全由标题组成、没有正文时也保留结构；
        # 普通无标题文本会由 flush_content() 建 fallback Chapter。
        if (
            not document.chapters
            and source_blocks
        ):
            ensure_fallback_chapter(
                page_number=(
                    source_blocks[0]
                    .page_number
                    or 1
                )
            )

        document.metadata.update(
            {
                "parser": self.parser_name,
                "parser_status": "SUCCESS",
                "heading_count": heading_count,
                "numbered_heading_count": (
                    numbered_heading_count
                ),
                "markdown_heading_count": (
                    markdown_heading_count
                ),
                "fallback_chapter_count": (
                    fallback_chapter_count
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

        return document

    @classmethod
    def _detect_heading(
        cls,
        *,
        block: DocumentBlock,
        markdown_counters: list[int],
    ) -> dict[
        str,
        Any,
    ] | None:

        text = cls._normalize_text(
            block.text
        )

        if not text:
            return None

        numbered = (
            cls._NUMBERED_HEADING_PATTERN
            .match(
                text
            )
        )

        if numbered is not None:

            heading_id = (
                numbered.group(
                    "number"
                )
                .translate(
                    cls._FULLWIDTH_TRANSLATION
                )
                .strip(".")
            )

            title = (
                numbered.group(
                    "title"
                )
                .strip()
            )

            if (
                not heading_id
                or not title
            ):
                return None

            level = len(
                [
                    part
                    for part
                    in heading_id.split(".")
                    if part
                ]
            )

            return {
                "id": heading_id,
                "title": title,
                "level": max(
                    level,
                    1,
                ),
                "kind": "numbered",
            }

        markdown = (
            cls._MARKDOWN_HEADING_PATTERN
            .match(
                text
            )
        )

        if markdown is not None:

            marks = markdown.group(
                "marks"
            )

            title = (
                markdown.group(
                    "title"
                )
                .strip()
            )

            if not title:
                return None

            level = min(
                max(
                    len(marks),
                    1,
                ),
                len(
                    markdown_counters
                ),
            )

            markdown_counters[
                level - 1
            ] += 1

            for index in range(
                level,
                len(
                    markdown_counters
                ),
            ):
                markdown_counters[
                    index
                ] = 0

            for index in range(
                level
            ):
                if (
                    markdown_counters[
                        index
                    ]
                    == 0
                ):
                    markdown_counters[
                        index
                    ] = 1

            heading_id = ".".join(
                str(
                    markdown_counters[
                        index
                    ]
                )
                for index in range(
                    level
                )
            )

            return {
                "id": heading_id,
                "title": title,
                "level": level,
                "kind": "markdown",
            }

        # Loader 可以显式标记 HEADING。
        # 对非编号的 HEADING 只做保守处理：
        # level=1 -> 生成 Markdown 风格 Chapter ID
        # level>1 -> 生成对应 Section ID
        if (
            block.block_type
            == BlockType.HEADING
            and block.level
        ):

            level = max(
                int(
                    block.level
                ),
                1,
            )

            level = min(
                level,
                len(
                    markdown_counters
                ),
            )

            markdown_counters[
                level - 1
            ] += 1

            for index in range(
                level,
                len(
                    markdown_counters
                ),
            ):
                markdown_counters[
                    index
                ] = 0

            for index in range(
                level
            ):
                if (
                    markdown_counters[
                        index
                    ]
                    == 0
                ):
                    markdown_counters[
                        index
                    ] = 1

            heading_id = ".".join(
                str(
                    markdown_counters[
                        index
                    ]
                )
                for index in range(
                    level
                )
            )

            return {
                "id": heading_id,
                "title": text,
                "level": level,
                "kind": "explicit",
            }

        return None

    @staticmethod
    def _normalize_section_id(
        *,
        heading_id: str,
        chapter_id: str,
        section_map: dict[
            str,
            Section,
        ],
    ) -> str:

        normalized = str(
            heading_id
        ).strip(".")

        if (
            normalized.startswith(
                f"{chapter_id}."
            )
            and "." in normalized
        ):
            candidate = normalized
        else:
            candidate = (
                f"{chapter_id}.1"
            )

        if candidate not in section_map:
            return candidate

        # 同じ ID が既にある場合は衝突を避ける。
        index = 2

        while (
            f"{candidate}.{index}"
            in section_map
        ):
            index += 1

        return (
            f"{candidate}.{index}"
        )

    @staticmethod
    def _normalize_text(
        text: str,
    ) -> str:

        if not text:
            return ""

        # 行の中の連続空白だけを正規化。
        return " ".join(
            str(
                text
            ).strip().split()
        )

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
                "StructuredTextParser expects "
                "app.model.document.Document."
            )

        if not document.blocks:
            raise ValueError(
                "Document contains no blocks."
            )
