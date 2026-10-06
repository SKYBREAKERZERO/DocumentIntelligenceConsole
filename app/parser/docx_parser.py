from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from app.analyzer.title_detector import TitleDetector
from app.model.block import (
    BlockType,
    DocumentBlock,
)
from app.model.chapter import Chapter
from app.model.content import Content
from app.model.document import Document
from app.model.section import Section
from app.analyzer.import_resilience_policy import ImportResiliencePolicy


class DOCXParser:
    """
    DOCX 企业级结构解析器。

    负责：
        - 将 DOCXLoader 输出的 Document.blocks
          解析为 Chapter / Section / Content
        - 优先利用已经解析好的 Word Heading 层级
        - 显式编号标题与 Word Heading 层级冲突时，
          优先采用显式编号深度
        - 支持 Heading 1 -> Chapter
        - 支持 Heading 2+ -> Section
        - 保留 Section level，供后续 SectionHierarchyBuilder
          建立 parent_section_id
        - 对没有 Word Heading 的文档回退到编号标题识别
        - 防止重复 parse() 累积旧结构
        - 不伪造 DOCX 物理页码
        - 输出结构诊断 Metadata

    显式编号示例：
        1 Introduction
        1.1 Purpose
        1.1.1 Connection

    设计原则：
        1. 显式编号深度是最直接的结构证据。
        2. Word Heading level 是第二结构证据。
        3. 普通段落只有在文档完全没有 Word Heading 时，
           才允许 TitleDetector / 编号回退。
        4. 表格内容永远按正文处理，不参与标题识别。
        5. 无法可靠知道 DOCX 物理页码时使用 None，
           不统一伪造为 page_number=1。
    """

    # ==================================================
    # Numbered Heading
    # ==================================================

    _NUMBERED_TITLE_PATTERN = re.compile(
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

    # Heading level 的合理防御范围。
    _MIN_HEADING_LEVEL = 1
    _MAX_HEADING_LEVEL = 9

    def __init__(
        self,
        title_detector: TitleDetector | None = None,
    ) -> None:

        self.title_detector = (
            title_detector
            or TitleDetector()
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

        # 同一个 Document 重复执行 Parser 时，
        # 必须先清空旧结果，保证幂等。
        document.chapters.clear()
        document.sections.clear()
        document.contents.clear()

        if not document.blocks:
            return ImportResiliencePolicy.mark_metadata_only(
                document,
                warning_code="DOCX_NO_BLOCKS",
                parser_name="DOCXParser",
                warning_message="DOCX contains no structured blocks; stored as metadata-only and excluded from Qdrant.",
            )

        if not any(str(block.text or "").strip() for block in document.blocks):
            return ImportResiliencePolicy.mark_metadata_only(
                document,
                warning_code="DOCX_NO_EXTRACTABLE_TEXT",
                parser_name="DOCXParser",
                warning_message="DOCX contains no extractable text; stored as metadata-only and excluded from Qdrant.",
            )

        # ==============================================
        # Heading Diagnostics
        # ==============================================

        styled_heading_blocks = [
            block
            for block
            in document.blocks
            if (
                block.block_type
                == BlockType.HEADING
                and block.level
                is not None
            )
        ]

        styled_heading_count = len(
            styled_heading_blocks
        )

        styled_heading_levels = sorted(
            {
                self._normalize_heading_level(
                    block.level
                )
                for block
                in styled_heading_blocks
                if block.level
                is not None
            }
        )

        use_style_headings = (
            styled_heading_count
            > 0
        )

        # 典型异常：
        #
        # Word 中几十个标题全部被人为设置成 Heading 1。
        #
        # Parser 自己不能凭空猜出所有层级，
        # 但会在 metadata 中明确告警。
        flat_heading_warning = (
            styled_heading_count >= 4
            and styled_heading_levels == [1]
        )

        # ==============================================
        # Runtime State
        # ==============================================

        current_chapter: Chapter | None = None
        current_section: Section | None = None

        chapter_map: dict[
            str,
            Chapter,
        ] = {}

        # Section ID 在不同 Chapter 理论上可能重复，
        # 所以使用 (chapter_id, section_id) 做内部索引。
        section_map: dict[
            tuple[
                str,
                str,
            ],
            Section,
        ] = {}

        content_buffer: list[
            str
        ] = []

        buffer_page_number: int | None = (
            None
        )

        # 无编号 Heading 的自动编号状态。
        generated_counters: defaultdict[
            int,
            int,
        ] = defaultdict(int)

        # ==============================================
        # Metrics
        # ==============================================

        removed_invalid_heading_count = 0
        ignored_table_heading_count = 0

        generated_fallback_chapter_count = 0
        resolved_fallback_chapter_count = 0

        explicit_number_level_override_count = 0
        heading_level_conflict_count = 0

        discarded_preface_content_count = 0

        # ==============================================
        # Flush Content
        # ==============================================

        def flush_content() -> None:

            nonlocal content_buffer
            nonlocal buffer_page_number
            nonlocal discarded_preface_content_count

            if not content_buffer:
                return

            text = "\n".join(
                content_buffer
            ).strip()

            content_buffer = []

            if not text:
                buffer_page_number = None
                return

            # 没有合法 Chapter 的前置内容：
            #
            #   - 封面
            #   - revision history
            #   - copyright
            #
            # 当前设计不生成 orphan Content。
            if current_chapter is None:

                discarded_preface_content_count += 1

                buffer_page_number = None

                return

            document.contents.append(
                Content(
                    chapter_id=(
                        current_chapter.id
                    ),

                    section_id=(
                        current_section.id
                        if current_section
                        is not None
                        else None
                    ),

                    text=text,

                    # 不再：
                    #
                    #   buffer_page_number or 1
                    #
                    # 因为 DOCX 物理页码不可可靠获得。
                    page_number=(
                        buffer_page_number
                    ),
                )
            )

            buffer_page_number = None

        # ==============================================
        # Parse Blocks
        # ==============================================

        for block in sorted(
            document.blocks,
            key=lambda item: item.order,
        ):

            text = self._normalize_text(
                block.text
            )

            if not text:
                continue

            # ==========================================
            # Table
            # ==========================================
            #
            # Table row 永远属于正文，
            # 即使开头长得像：
            #
            #   1.2 | Description
            #
            # 也不能误判为 Section。

            if (
                block.block_type
                == BlockType.TABLE
            ):

                if self._looks_like_numbered_heading(
                    text
                ):
                    ignored_table_heading_count += 1

                if (
                    buffer_page_number
                    is None
                ):
                    buffer_page_number = (
                        block.page_number
                    )

                content_buffer.append(
                    text
                )

                continue

            # ==========================================
            # Heading Detection
            # ==========================================

            heading = self._detect_heading(
                block=block,

                use_style_headings=(
                    use_style_headings
                ),

                generated_counters=(
                    generated_counters
                ),

                current_chapter=(
                    current_chapter
                ),
            )

            # ==========================================
            # Normal Content
            # ==========================================

            if heading is None:

                if (
                    buffer_page_number
                    is None
                ):
                    buffer_page_number = (
                        block.page_number
                    )

                content_buffer.append(
                    text
                )

                continue

            # 新 Heading 前，先保存上一段正文。
            flush_content()

            heading_id = (
                self._normalize_number(
                    heading[
                        "id"
                    ]
                )
            )

            heading_title = (
                self._normalize_text(
                    heading[
                        "title"
                    ]
                )
            )

            heading_level = (
                self._normalize_heading_level(
                    heading[
                        "level"
                    ]
                )
            )

            if heading.get(
                "explicit_level_override",
                False,
            ):
                explicit_number_level_override_count += 1

            if heading.get(
                "level_conflict",
                False,
            ):
                heading_level_conflict_count += 1

            if (
                not heading_id
                or not heading_title
            ):

                removed_invalid_heading_count += 1

                continue

            # 显式编号同步到自动编号状态，
            # 避免下一条无编号 Heading 重新从 1 开始。
            self._sync_generated_counters(
                heading_id=(
                    heading_id
                ),

                level=(
                    heading_level
                ),

                counters=(
                    generated_counters
                ),
            )

            # ==========================================
            # Chapter
            # ==========================================

            if heading_level == 1:

                chapter = chapter_map.get(
                    heading_id
                )

                if chapter is None:

                    chapter = Chapter(
                        id=heading_id,

                        title_jp=(
                            heading_title
                        ),

                        title_en=None,

                        level=1,

                        page_number=(
                            block.page_number
                        ),

                        metadata={
                            "heading_level_source": (
                                heading.get(
                                    "level_source"
                                )
                            ),

                            "source_block_order": (
                                block.order
                            ),
                        },
                    )

                    document.chapters.append(
                        chapter
                    )

                    chapter_map[
                        heading_id
                    ] = chapter

                # 前面为了 Section 建过占位 Chapter，
                # 后面真实 Heading 1 到达后补全。
                elif chapter.metadata.get(
                    "generated_fallback",
                    False,
                ):

                    chapter.title_jp = (
                        heading_title
                    )

                    if (
                        block.page_number
                        is not None
                    ):
                        chapter.page_number = (
                            block.page_number
                        )

                    chapter.metadata.pop(
                        "generated_fallback",
                        None,
                    )

                    chapter.metadata[
                        "heading_level_source"
                    ] = heading.get(
                        "level_source"
                    )

                    resolved_fallback_chapter_count += 1

                # HeadingMerger 之后仍可能出现相同 ID，
                # 只做保守的“更完整标题”覆盖。
                elif (
                    heading_title
                    and len(
                        heading_title
                    )
                    > len(
                        chapter.title_jp
                        or ""
                    )
                ):

                    chapter.title_jp = (
                        heading_title
                    )

                current_chapter = (
                    chapter
                )

                # 新 Chapter 必须清除上一 Chapter 的 Section。
                current_section = None

                continue

            # ==========================================
            # Section
            # ==========================================

            chapter_id = (
                self._resolve_chapter_id(
                    heading_id=(
                        heading_id
                    ),

                    current_chapter=(
                        current_chapter
                    ),
                )
            )

            # Heading 2+ 出现在任何 Heading 1 之前。
            if chapter_id is None:

                chapter_id = (
                    self._generate_fallback_chapter_id(
                        chapter_map
                    )
                )

            # Explicit number 可能直接出现：
            #
            #   2.1 Purpose
            #
            # 但 Chapter 2 尚未出现。
            #
            # 建占位 Chapter，保证关系完整。
            if (
                chapter_id
                not in chapter_map
            ):

                fallback_chapter = Chapter(
                    id=(
                        chapter_id
                    ),

                    title_jp=(
                        f"Chapter {chapter_id}"
                    ),

                    title_en=None,

                    level=1,

                    page_number=(
                        block.page_number
                    ),

                    metadata={
                        "generated_fallback": (
                            True
                        ),

                        "heading_level_source": (
                            "generated_fallback"
                        ),

                        "source_block_order": (
                            block.order
                        ),
                    },
                )

                document.chapters.append(
                    fallback_chapter
                )

                chapter_map[
                    chapter_id
                ] = fallback_chapter

                generated_fallback_chapter_count += 1

            current_chapter = (
                chapter_map[
                    chapter_id
                ]
            )

            normalized_section_id = (
                self._ensure_section_id_belongs_to_chapter(
                    section_id=(
                        heading_id
                    ),

                    chapter_id=(
                        chapter_id
                    ),

                    level=(
                        heading_level
                    ),

                    section_map=(
                        section_map
                    ),
                )
            )

            section_key = (
                chapter_id,
                normalized_section_id,
            )

            section = section_map.get(
                section_key
            )

            if section is None:

                section = Section(
                    id=(
                        normalized_section_id
                    ),

                    title_jp=(
                        heading_title
                    ),

                    title_en=None,

                    level=(
                        heading_level
                    ),

                    chapter_id=(
                        chapter_id
                    ),

                    parent_section_id=None,

                    page_number=(
                        block.page_number
                    ),

                    metadata={
                        "heading_level_source": (
                            heading.get(
                                "level_source"
                            )
                        ),

                        "source_block_order": (
                            block.order
                        ),
                    },
                )

                document.sections.append(
                    section
                )

                section_map[
                    section_key
                ] = section

            elif (
                heading_title
                and len(
                    heading_title
                )
                > len(
                    section.title_jp
                    or ""
                )
            ):

                section.title_jp = (
                    heading_title
                )

            current_section = (
                section
            )

        # 最后一段正文。
        flush_content()

        # ==============================================
        # Metadata
        # ==============================================

        document.metadata.update(
            {
                "parser": (
                    "DOCXParser"
                ),

                "parser_status": (
                    "SUCCESS"
                ),

                "docx_heading_strategy": (
                    "word_structure"
                    if use_style_headings
                    else "numbered_text_fallback"
                ),

                "styled_heading_count": (
                    styled_heading_count
                ),

                "styled_heading_levels": (
                    styled_heading_levels
                ),

                "docx_flat_heading_warning": (
                    flat_heading_warning
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

                "ignored_table_heading_count": (
                    ignored_table_heading_count
                ),

                "removed_invalid_heading_count": (
                    removed_invalid_heading_count
                ),

                "generated_fallback_chapter_count": (
                    generated_fallback_chapter_count
                ),

                "resolved_fallback_chapter_count": (
                    resolved_fallback_chapter_count
                ),

                "explicit_number_level_override_count": (
                    explicit_number_level_override_count
                ),

                "heading_level_conflict_count": (
                    heading_level_conflict_count
                ),

                "discarded_preface_content_count": (
                    discarded_preface_content_count
                ),

                "physical_page_number_available": (
                    False
                ),
            }
        )

        return document

    # ==================================================
    # Heading Detection
    # ==================================================

    def _detect_heading(
        self,
        *,
        block: DocumentBlock,
        use_style_headings: bool,
        generated_counters: defaultdict[
            int,
            int,
        ],
        current_chapter: Chapter | None,
    ) -> dict[
        str,
        Any,
    ] | None:
        """
        返回统一 Heading：

            {
                "id": "1.2",
                "title": "Purpose",
                "level": 2,
                "level_source": "...",
                "explicit_level_override": False,
                "level_conflict": False,
            }

        关键原则：
            如果 Heading 文本自身明确写着：

                2.3.1 Authentication

            那么编号深度 level=3
            比错误的 Word Heading 1 更可信。
        """

        text = self._normalize_text(
            block.text
        )

        if not text:
            return None

        # Table / Image / PageBreak 永不参与 Heading。
        if block.block_type in {
            BlockType.TABLE,
            BlockType.IMAGE,
            BlockType.PAGE_BREAK,
        }:
            return None

        numbered_result = (
            self._parse_numbered_title(
                text
            )
        )

        # ==============================================
        # Word Heading
        # ==============================================

        if (
            block.block_type
            == BlockType.HEADING
            and block.level
            is not None
        ):

            word_level = (
                self._normalize_heading_level(
                    block.level
                )
            )

            # 显式编号优先。
            if (
                numbered_result
                is not None
            ):

                explicit_level = (
                    self._normalize_heading_level(
                        numbered_result[
                            "level"
                        ]
                    )
                )

                conflict = (
                    explicit_level
                    != word_level
                )

                numbered_result.update(
                    {
                        "level": (
                            explicit_level
                        ),

                        "level_source": (
                            "explicit_number"
                        ),

                        "explicit_level_override": (
                            conflict
                        ),

                        "level_conflict": (
                            conflict
                        ),
                    }
                )

                return numbered_result

            # 无显式编号时，
            # 使用 DOCXLoader 已经综合解析好的 block.level。
            generated_id = (
                self._generate_heading_id(
                    level=(
                        word_level
                    ),

                    counters=(
                        generated_counters
                    ),

                    current_chapter=(
                        current_chapter
                    ),
                )
            )

            return {
                "id": (
                    generated_id
                ),

                "title": (
                    text
                ),

                "level": (
                    word_level
                ),

                "level_source": (
                    block.metadata.get(
                        "heading_level_source"
                    )
                    or "word_heading"
                ),

                "explicit_level_override": (
                    False
                ),

                "level_conflict": bool(
                    block.metadata.get(
                        "heading_level_conflict",
                        False,
                    )
                ),
            }

        # ==============================================
        # Document already has Word Heading
        # ==============================================
        #
        # 一旦文档有可信的 Word Heading，
        # 普通 Paragraph 不再使用宽松 TitleDetector 猜标题。
        #
        # 这样可以避免正文：
        #
        #   2025/10/07 ...
        #   401 Unauthorized ...
        #
        # 被误判为章节。

        if use_style_headings:
            return None

        # ==============================================
        # No Word Heading -> fallback
        # ==============================================

        if block.block_type not in {
            BlockType.PARAGRAPH,
            BlockType.LIST,
            BlockType.TEXTBOX,
        }:
            return None

        if (
            numbered_result
            is not None
        ):

            numbered_result.update(
                {
                    "level_source": (
                        "explicit_number"
                    ),

                    "explicit_level_override": (
                        False
                    ),

                    "level_conflict": (
                        False
                    ),
                }
            )

            return numbered_result

        detector_result = (
            self.title_detector.detect(
                text
            )
        )

        if (
            detector_result
            is None
        ):
            return None

        detected_id = (
            self._normalize_number(
                detector_result[
                    "id"
                ]
            )
        )

        detected_title = (
            self._normalize_text(
                detector_result[
                    "title"
                ]
            )
        )

        detected_level = (
            self._normalize_heading_level(
                detector_result[
                    "level"
                ]
            )
        )

        if (
            not detected_id
            or not detected_title
        ):
            return None

        return {
            "id": (
                detected_id
            ),

            "title": (
                detected_title
            ),

            "level": (
                detected_level
            ),

            "level_source": (
                "title_detector"
            ),

            "explicit_level_override": (
                False
            ),

            "level_conflict": (
                False
            ),
        }

    # ==================================================
    # Numbered Title
    # ==================================================

    @classmethod
    def _parse_numbered_title(
        cls,
        text: str,
    ) -> dict[
        str,
        Any,
    ] | None:

        match = (
            cls._NUMBERED_TITLE_PATTERN.match(
                text
            )
        )

        if match is None:
            return None

        heading_id = (
            cls._normalize_number(
                match.group(
                    "number"
                )
            )
        )

        title = (
            cls._normalize_text(
                match.group(
                    "title"
                )
            )
        )

        if (
            not heading_id
            or not title
        ):
            return None

        if cls._is_invalid_numbered_title(
            heading_id=(
                heading_id
            ),

            title=(
                title
            ),
        ):
            return None

        return {
            "id": (
                heading_id
            ),

            "title": (
                title
            ),

            "level": (
                heading_id.count(
                    "."
                )
                + 1
            ),
        }

    # ==================================================
    # Invalid Numbered Heading
    # ==================================================

    @staticmethod
    def _is_invalid_numbered_title(
        *,
        heading_id: str,
        title: str,
    ) -> bool:

        # Table-like text.
        if "|" in title:
            return True

        if not title.strip():
            return True

        # Revision history / date.
        if re.search(
            r"\b\d{4}/\d{1,2}/\d{1,2}\b",
            title,
        ):
            return True

        # 纯符号。
        if re.fullmatch(
            r"[\W_]+",
            title,
        ):
            return True

        # Page number:
        #
        #   2 / 48
        #
        if re.fullmatch(
            r"\d+\s*/\s*\d+",
            f"{heading_id} {title}",
        ):
            return True

        return False

    # ==================================================
    # Generated Counter Sync
    # ==================================================

    @staticmethod
    def _sync_generated_counters(
        *,
        heading_id: str,
        level: int,
        counters: defaultdict[
            int,
            int,
        ],
    ) -> None:

        parts = [
            part
            for part
            in heading_id.split(
                "."
            )
            if part
        ]

        if not parts:
            return

        if not all(
            part.isdigit()
            for part
            in parts
        ):
            return

        # 清除更深层级。
        deeper_levels = [
            stored_level
            for stored_level
            in counters
            if stored_level
            > level
        ]

        for stored_level in (
            deeper_levels
        ):
            counters.pop(
                stored_level,
                None,
            )

        max_depth = min(
            level,
            len(parts),
        )

        for current_level in range(
            1,
            max_depth + 1,
        ):

            counters[
                current_level
            ] = int(
                parts[
                    current_level - 1
                ]
            )

    # ==================================================
    # Generate Heading ID
    # ==================================================

    @classmethod
    def _generate_heading_id(
        cls,
        *,
        level: int,
        counters: defaultdict[
            int,
            int,
        ],
        current_chapter: Chapter | None,
    ) -> str:

        level = (
            cls._normalize_heading_level(
                level
            )
        )

        # ==============================================
        # Heading 1
        # ==============================================

        if level == 1:

            counters[1] += 1

            # 新 Chapter 时清空所有子层级。
            for stored_level in list(
                counters.keys()
            ):

                if stored_level > 1:

                    counters.pop(
                        stored_level,
                        None,
                    )

            return str(
                counters[1]
            )

        # ==============================================
        # Heading 2+
        # ==============================================

        chapter_id = (
            current_chapter.id
            if current_chapter
            is not None
            else None
        )

        if not chapter_id:

            # 没有 Chapter 时保证计数合法。
            if counters[1] <= 0:
                counters[1] = 1

            chapter_id = str(
                counters[1]
            )

        # 当前层级递增。
        counters[
            level
        ] += 1

        # 清除更深层级。
        for stored_level in list(
            counters.keys()
        ):

            if stored_level > level:

                counters.pop(
                    stored_level,
                    None,
                )

        parts = [
            str(
                chapter_id
            ).strip().strip(
                "."
            )
        ]

        # 缺失中间层级时使用 1，
        # 不产生：
        #
        #   1.0.1
        #
        # 这种无效 ID。
        for current_level in range(
            2,
            level + 1,
        ):

            if (
                current_level
                == level
            ):

                value = (
                    counters[
                        current_level
                    ]
                )

            else:

                value = (
                    counters[
                        current_level
                    ]
                )

                if value <= 0:

                    value = 1

                    counters[
                        current_level
                    ] = 1

            parts.append(
                str(
                    value
                )
            )

        return ".".join(
            parts
        )

    # ==================================================
    # Resolve Chapter ID
    # ==================================================

    @staticmethod
    def _resolve_chapter_id(
        *,
        heading_id: str,
        current_chapter: Chapter | None,
    ) -> str | None:

        normalized = str(
            heading_id
        ).strip().strip(
            "."
        )

        if not normalized:

            return (
                current_chapter.id
                if current_chapter
                is not None
                else None
            )

        # 显式编号：
        #
        #   2.3.1
        #
        # -> Chapter 2
        if "." in normalized:

            return normalized.split(
                ".",
                maxsplit=1,
            )[0]

        # 非标准生成 ID 时，
        # 优先归属当前 Chapter。
        if (
            current_chapter
            is not None
        ):

            return (
                current_chapter.id
            )

        return None

    # ==================================================
    # Fallback Chapter ID
    # ==================================================

    @staticmethod
    def _generate_fallback_chapter_id(
        chapter_map: dict[
            str,
            Chapter,
        ],
    ) -> str:

        index = 1

        while (
            str(index)
            in chapter_map
        ):
            index += 1

        return str(
            index
        )

    # ==================================================
    # Ensure Section ID
    # ==================================================

    @staticmethod
    def _ensure_section_id_belongs_to_chapter(
        *,
        section_id: str,
        chapter_id: str,
        level: int,
        section_map: dict[
            tuple[
                str,
                str,
            ],
            Section,
        ],
    ) -> str:

        normalized_section_id = str(
            section_id
        ).strip().strip(
            "."
        )

        normalized_chapter_id = str(
            chapter_id
        ).strip().strip(
            "."
        )

        # 正常显式编号：
        #
        #   Chapter = 2
        #   Section = 2.1 / 2.1.1
        #
        if (
            normalized_section_id.startswith(
                normalized_chapter_id
                + "."
            )
            and normalized_section_id
            != normalized_chapter_id
        ):

            return (
                normalized_section_id
            )

        # 如果 ID 不属于 Chapter，
        # 建立稳定的 Chapter-prefixed ID。
        #
        # Example:
        #
        #   heading_id = A
        #   chapter_id = 2
        #
        # -> 2.A

        if normalized_section_id:

            base = (
                normalized_chapter_id
                + "."
                + normalized_section_id
            )

        else:

            base = (
                normalized_chapter_id
                + ".1"
            )

        candidate = (
            base
        )

        suffix = 2

        # 只在同一个 Chapter 内避免冲突。
        while (
            (
                normalized_chapter_id,
                candidate,
            )
            in section_map
        ):

            candidate = (
                f"{base}.{suffix}"
            )

            suffix += 1

        return candidate

    # ==================================================
    # Looks Like Numbered Heading
    # ==================================================

    @classmethod
    def _looks_like_numbered_heading(
        cls,
        text: str,
    ) -> bool:

        return (
            cls._parse_numbered_title(
                text
            )
            is not None
        )

    # ==================================================
    # Normalize Number
    # ==================================================

    @classmethod
    def _normalize_number(
        cls,
        value: str,
    ) -> str:

        normalized = str(
            value
        ).translate(
            cls._FULLWIDTH_TRANSLATION
        )

        normalized = (
            normalized.strip().strip(
                "."
            )
        )

        # 防止：
        #
        #   1..2
        #
        # 进入结构 ID。
        normalized = re.sub(
            r"\.+",
            ".",
            normalized,
        )

        return normalized

    # ==================================================
    # Normalize Heading Level
    # ==================================================

    @classmethod
    def _normalize_heading_level(
        cls,
        value,
    ) -> int:

        try:

            level = int(
                value
            )

        except (
            TypeError,
            ValueError,
        ):

            level = 1

        return max(
            cls._MIN_HEADING_LEVEL,
            min(
                level,
                cls._MAX_HEADING_LEVEL,
            ),
        )

    # ==================================================
    # Normalize Text
    # ==================================================

    @staticmethod
    def _normalize_text(
        text: str | None,
    ) -> str:

        if text is None:
            return ""

        normalized = str(
            text
        )

        normalized = normalized.replace(
            "\u3000",
            " ",
        )

        normalized = normalized.replace(
            "\xa0",
            " ",
        )

        return " ".join(
            normalized.split()
        ).strip()

    # ==================================================
    # Validate Document
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
                "DOCXParser expects an "
                "app.model.document.Document instance."
            )

        file_type = str(
            document.file_type
            or ""
        ).strip().lower()

        if file_type != "docx":

            raise ValueError(
                "DOCXParser only accepts DOCX documents. "
                f"Received file_type: "
                f"{document.file_type}"
            )

        # Content availability is handled by ImportResiliencePolicy in parse().
        return None
