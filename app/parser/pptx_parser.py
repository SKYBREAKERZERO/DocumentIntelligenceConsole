from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from typing import Any

from app.model.block import BlockType, DocumentBlock
from app.model.chapter import Chapter
from app.model.content import Content
from app.model.document import Document
from app.model.section import Section
from app.analyzer.import_resilience_policy import ImportResiliencePolicy


class PPTXParserError(RuntimeError):
    """PPTX 结构解析异常。"""


class PPTXParser:
    """
    企业级 PPTX 双模式结构解析器。

    支持两种 PPTX：

    1. Presentation Mode
       --------------------------------
       适合普通演示资料：

           Slide
               -> Chapter

           Slide Title
               -> Chapter Title

           Secondary Heading
               -> Section

           Text / List / Table / Image / Chart
               -> Content

       例如：

           【走行規制】 2⃣ 法規の特定...
           【HVAC】 4⃣ 法規の整理...

       这类 Slide Title 本身具有语义，继续保持 Slide -> Chapter。

    2. Specification Mode
       --------------------------------
       适合“规格书被保存成 PPTX”的文档：

           Slide Title:
               1 page
               2 page
               3 page
               ...

       这种标题只代表物理/逻辑页，不代表文档结构。

       Parser 会忽略 page-placeholder title，并扫描正文中的编号标题：

           1 Introduction
               -> Chapter 1

           1.1 Purpose
               -> Section 1.1

           1.2.1 Target system
               -> Section 1.2.1

           3.1.3.Wi-Fi接続仕様
               -> Section 3.1.3

       同时：
           - TOC / 目次前置页先建立 Canonical Outline，再从正文排除
           - Canonical Outline 用于验证 Chapter / Section，阻止编号列表误建结构
           - Change History / 変更履歴作为 Back Matter 独立保存
           - 表格内容不参与 Heading 识别，避免版本号/章节号误建结构
           - 普通 numbered list 不允许跨 Chapter 反向切换
           - 每个 Slide 末尾 flush Content，保持 page_number citation 语义

    Auto Mode 判断：

        placeholder_slide_title_ratio >= threshold
        AND
        detected_numbered_heading_count >= minimum

            -> specification

        否则：

            -> presentation

    设计原则：

        - 不根据文件名判断模式
        - 保留现有公开构造参数
        - 新参数全部带默认值
        - 不在 Parser 内 Chunk
        - 不在 Parser 内 Token Count
        - 不修改原始 Document.blocks
        - Table / Image / Chart 序列化保持原有接口
        - Section parent_section_id 留给 SectionHierarchyBuilder
    """

    # ==================================================
    # Content Block Types
    # ==================================================

    _CONTENT_BLOCK_TYPES = {
        BlockType.HEADING,
        BlockType.PARAGRAPH,
        BlockType.LIST,
        BlockType.TEXTBOX,
        BlockType.TABLE,
        BlockType.IMAGE,
        BlockType.UNKNOWN,
    }

    # ==================================================
    # Auto Mode
    # ==================================================

    _PAGE_PLACEHOLDER_PATTERN = re.compile(
        r"""
        ^
        \s*
        \d+
        \s*
        page
        \s*
        $
        """,
        re.IGNORECASE | re.VERBOSE,
    )

    # ==================================================
    # Specification Heading
    # ==================================================
    #
    # Supports:
    #
    #   1 Introduction
    #   1.1 Purpose
    #   1.2.1 Target system
    #   3.1.3.Wi-Fi接続仕様
    #   3.2.5.8. Phone widget integration
    #   6.性能要件
    #
    # Important:
    #   id does not include the separator dot before title.

    _SPEC_HEADING_PATTERN = re.compile(
        r"""
        ^
        \s*
        (?P<id>
            \d+
            (?:
                \.\d+
            )*
        )
        (?:
            [\.．、:：]
            \s*
            |
            \s+
        )
        (?P<title>
            \S
            .*
        )
        \s*
        $
        """,
        re.VERBOSE,
    )

    # ==================================================
    # TOC / Back Matter
    # ==================================================

    _TOC_PATTERNS = (
        re.compile(
            r"^目次(?:\s*/\s*table\s+of\s+contents)?$",
            re.IGNORECASE,
        ),
        re.compile(
            r"^table\s+of\s+contents$",
            re.IGNORECASE,
        ),
        re.compile(
            r"^contents?$",
            re.IGNORECASE,
        ),
        re.compile(
            r"^目录$",
            re.IGNORECASE,
        ),
    )

    _CHANGE_HISTORY_PATTERN = re.compile(
        r"""
        ^
        (?:
            change
            \s*
            history
            |
            revision
            \s*
            history
            |
            変更履歴
            |
            改訂履歴
        )
        (?:
            \s*
            [\(（]
            \d+
            [\)）]
        )?
        \s*
        $
        """,
        re.IGNORECASE | re.VERBOSE,
    )

    # ==================================================
    # Init
    # ==================================================

    def __init__(
        self,
        *,
        use_original_slide_number_as_chapter_id: bool = True,
        create_section_from_secondary_heading: bool = True,
        create_default_section_when_missing: bool = False,
        include_slide_title_in_content: bool = False,
        include_secondary_heading_in_content: bool = False,
        include_image_blocks: bool = True,
        include_chart_blocks: bool = True,
        include_table_headers: bool = True,
        merge_adjacent_content_blocks: bool = True,
        content_separator: str = "\n",
        table_cell_separator: str = " | ",
        default_slide_title_prefix: str = "Slide",
        maximum_title_length: int = 300,
        mode: str = "auto",
        specification_placeholder_title_ratio: float = 0.80,
        specification_minimum_numbered_heading_count: int = 4,
        specification_toc_front_slide_limit: int = 3,
        skip_toc_slides_in_specification_mode: bool = True,
        create_back_matter_chapter: bool = True,
        back_matter_chapter_id: str = "BACKMATTER",
        back_matter_chapter_title: str = "Change History / 変更履歴",
    ) -> None:

        if not content_separator:
            raise ValueError(
                "content_separator cannot be empty."
            )

        if not table_cell_separator:
            raise ValueError(
                "table_cell_separator cannot be empty."
            )

        if not default_slide_title_prefix.strip():
            raise ValueError(
                "default_slide_title_prefix cannot be empty."
            )

        if maximum_title_length <= 0:
            raise ValueError(
                "maximum_title_length must be greater than 0."
            )

        normalized_mode = str(
            mode
            or ""
        ).strip().lower()

        if normalized_mode not in {
            "auto",
            "presentation",
            "specification",
        }:
            raise ValueError(
                "mode must be one of: "
                "'auto', 'presentation', 'specification'."
            )

        if not (
            0.0
            < specification_placeholder_title_ratio
            <= 1.0
        ):
            raise ValueError(
                "specification_placeholder_title_ratio "
                "must be between 0 and 1."
            )

        if specification_minimum_numbered_heading_count < 1:
            raise ValueError(
                "specification_minimum_numbered_heading_count "
                "must be at least 1."
            )

        if specification_toc_front_slide_limit < 1:
            raise ValueError(
                "specification_toc_front_slide_limit "
                "must be at least 1."
            )

        if create_back_matter_chapter:
            if not str(
                back_matter_chapter_id
                or ""
            ).strip():
                raise ValueError(
                    "back_matter_chapter_id cannot be empty."
                )

            if not str(
                back_matter_chapter_title
                or ""
            ).strip():
                raise ValueError(
                    "back_matter_chapter_title cannot be empty."
                )

        self.use_original_slide_number_as_chapter_id = bool(
            use_original_slide_number_as_chapter_id
        )

        self.create_section_from_secondary_heading = bool(
            create_section_from_secondary_heading
        )

        self.create_default_section_when_missing = bool(
            create_default_section_when_missing
        )

        self.include_slide_title_in_content = bool(
            include_slide_title_in_content
        )

        self.include_secondary_heading_in_content = bool(
            include_secondary_heading_in_content
        )

        self.include_image_blocks = bool(
            include_image_blocks
        )

        self.include_chart_blocks = bool(
            include_chart_blocks
        )

        self.include_table_headers = bool(
            include_table_headers
        )

        self.merge_adjacent_content_blocks = bool(
            merge_adjacent_content_blocks
        )

        self.content_separator = (
            content_separator
        )

        self.table_cell_separator = (
            table_cell_separator
        )

        self.default_slide_title_prefix = (
            default_slide_title_prefix.strip()
        )

        self.maximum_title_length = int(
            maximum_title_length
        )

        self.mode = normalized_mode

        self.specification_placeholder_title_ratio = float(
            specification_placeholder_title_ratio
        )

        self.specification_minimum_numbered_heading_count = int(
            specification_minimum_numbered_heading_count
        )

        self.specification_toc_front_slide_limit = int(
            specification_toc_front_slide_limit
        )

        self.skip_toc_slides_in_specification_mode = bool(
            skip_toc_slides_in_specification_mode
        )

        self.create_back_matter_chapter = bool(
            create_back_matter_chapter
        )

        self.back_matter_chapter_id = str(
            back_matter_chapter_id
        ).strip()

        self.back_matter_chapter_title = str(
            back_matter_chapter_title
        ).strip()

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

        # 幂等：重复 parse() 不累积旧结构。
        document.chapters.clear()
        document.sections.clear()
        document.contents.clear()

        blocks_by_slide = (
            self._group_blocks_by_slide(
                document.blocks
            )
        )

        slide_records = (
            self._collect_slide_records(
                document
            )
        )

        if (
            not blocks_by_slide
            and not slide_records
        ):
            return ImportResiliencePolicy.mark_metadata_only(
                document,
                warning_code="PPTX_NO_PARSEABLE_SLIDES",
                parser_name="PPTXParser",
                warning_message="PPTX contains no parseable slides; stored as metadata-only and excluded from Qdrant.",
            )

        slide_indexes = sorted(
            set(
                blocks_by_slide
            )
            | set(
                slide_records
            )
        )

        (
            parser_mode,
            mode_diagnostics,
        ) = self._resolve_parser_mode(
            slide_indexes=(
                slide_indexes
            ),
            blocks_by_slide=(
                blocks_by_slide
            ),
            slide_records=(
                slide_records
            ),
        )

        if parser_mode == "specification":

            self._parse_specification_mode(
                document=document,
                slide_indexes=slide_indexes,
                blocks_by_slide=blocks_by_slide,
                slide_records=slide_records,
                mode_diagnostics=mode_diagnostics,
            )

        else:

            self._parse_presentation_mode(
                document=document,
                slide_indexes=slide_indexes,
                blocks_by_slide=blocks_by_slide,
                slide_records=slide_records,
                mode_diagnostics=mode_diagnostics,
            )

        return (
            document
        )

    # ==================================================
    # Presentation Mode
    # ==================================================

    def _parse_presentation_mode(
        self,
        *,
        document: Document,
        slide_indexes: list[int],
        blocks_by_slide: dict[
            int,
            list[
                DocumentBlock
            ],
        ],
        slide_records: dict[
            int,
            dict[
                str,
                Any,
            ],
        ],
        mode_diagnostics: dict[
            str,
            Any,
        ],
    ) -> None:
        """
        保留原有 Slide -> Chapter 行为。
        """

        parsed_slide_count = 0
        generated_section_count = 0
        generated_content_count = 0
        chapter_root_content_count = 0
        skipped_empty_content_count = 0

        global_section_order = 0

        for (
            chapter_order,
            slide_index,
        ) in enumerate(
            slide_indexes,
            start=1,
        ):

            slide_blocks = sorted(
                blocks_by_slide.get(
                    slide_index,
                    [],
                ),
                key=self._block_sort_key,
            )

            slide_record = (
                slide_records.get(
                    slide_index,
                    {},
                )
            )

            slide_number = (
                self._resolve_slide_number(
                    slide_index=(
                        slide_index
                    ),
                    slide_record=(
                        slide_record
                    ),
                    blocks=(
                        slide_blocks
                    ),
                )
            )

            chapter_id = (
                self._build_chapter_id(
                    slide_number=(
                        slide_number
                    ),
                    chapter_order=(
                        chapter_order
                    ),
                )
            )

            slide_title_block = (
                self._find_slide_title_block(
                    slide_blocks
                )
            )

            chapter_title = (
                self._resolve_chapter_title(
                    slide_number=(
                        slide_number
                    ),
                    slide_record=(
                        slide_record
                    ),
                    title_block=(
                        slide_title_block
                    ),
                    blocks=(
                        slide_blocks
                    ),
                )
            )

            page_number = (
                self._resolve_page_number(
                    slide_blocks
                )
            )

            if page_number is None:

                page_number = (
                    slide_number
                )

            chapter = Chapter(
                id=(
                    chapter_id
                ),
                title_jp=(
                    chapter_title
                ),
                title_en=None,
                level=1,
                sort_order=(
                    chapter_order
                ),
                page_number=(
                    page_number
                ),
                metadata={
                    "source": (
                        "pptx"
                    ),
                    "pptx_parser_mode": (
                        "presentation"
                    ),
                    "slide_index": (
                        slide_index
                    ),
                    "slide_number": (
                        slide_number
                    ),
                    "logical_page_number": (
                        page_number
                    ),
                    "block_count": len(
                        slide_blocks
                    ),
                    "hidden": bool(
                        slide_record.get(
                            "hidden",
                            False,
                        )
                    ),
                },
            )

            document.chapters.append(
                chapter
            )

            parsed_slide_count += 1

            current_section: (
                Section
                | None
            ) = None

            section_sequence = 0

            content_buffer: list[
                str
            ] = []

            content_page_number = (
                page_number
            )

            def save_content() -> None:

                nonlocal content_buffer
                nonlocal generated_content_count
                nonlocal chapter_root_content_count
                nonlocal skipped_empty_content_count

                text = (
                    self.content_separator
                    .join(
                        content_buffer
                    )
                    .strip()
                )

                content_buffer = []

                if not text:

                    skipped_empty_content_count += 1

                    return

                section_id = (
                    current_section.id
                    if current_section
                    is not None
                    else None
                )

                document.contents.append(
                    Content(
                        chapter_id=(
                            chapter_id
                        ),
                        section_id=(
                            section_id
                        ),
                        text=(
                            text
                        ),
                        page_number=(
                            content_page_number
                        ),
                    )
                )

                generated_content_count += 1

                if section_id is None:

                    chapter_root_content_count += 1

            for block in (
                slide_blocks
            ):

                if block is slide_title_block:

                    if self.include_slide_title_in_content:

                        title_text = (
                            self._normalize_text(
                                block.text
                            )
                        )

                        if title_text:

                            content_buffer.append(
                                title_text
                            )

                    continue

                if self._is_secondary_heading(
                    block
                ):

                    save_content()

                    if not self.create_section_from_secondary_heading:

                        if self.include_secondary_heading_in_content:

                            heading_text = (
                                self._normalize_text(
                                    block.text
                                )
                            )

                            if heading_text:

                                content_buffer.append(
                                    heading_text
                                )

                        continue

                    section_sequence += 1
                    global_section_order += 1

                    section_id = (
                        f"{chapter_id}."
                        f"{section_sequence}"
                    )

                    section_title = (
                        self._normalize_title(
                            block.text
                        )
                    )

                    if not section_title:

                        section_title = (
                            f"{chapter_title} - "
                            f"Section "
                            f"{section_sequence}"
                        )

                    current_section = (
                        Section(
                            id=(
                                section_id
                            ),
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
                                global_section_order
                            ),
                            page_number=(
                                block.page_number
                                or page_number
                            ),
                            metadata={
                                "source": (
                                    "pptx"
                                ),
                                "pptx_parser_mode": (
                                    "presentation"
                                ),
                                "slide_index": (
                                    slide_index
                                ),
                                "slide_number": (
                                    slide_number
                                ),
                                "shape_index": (
                                    block.metadata.get(
                                        "shape_index"
                                    )
                                ),
                                "visual_index": (
                                    block.metadata.get(
                                        "visual_index"
                                    )
                                ),
                                "block_id": (
                                    block.id
                                ),
                            },
                        )
                    )

                    document.sections.append(
                        current_section
                    )

                    generated_section_count += 1

                    if self.include_secondary_heading_in_content:

                        content_buffer.append(
                            section_title
                        )

                    continue

                content_text = (
                    self._build_content_text(
                        block
                    )
                )

                if not content_text:
                    continue

                content_page_number = (
                    block.page_number
                    or page_number
                )

                if self.merge_adjacent_content_blocks:

                    content_buffer.append(
                        content_text
                    )

                else:

                    save_content()

                    content_buffer.append(
                        content_text
                    )

                    save_content()

            save_content()

            if (
                self.create_default_section_when_missing
                and section_sequence
                == 0
            ):

                default_section_id = (
                    f"{chapter_id}.1"
                )

                default_section = (
                    Section(
                        id=(
                            default_section_id
                        ),
                        chapter_id=(
                            chapter_id
                        ),
                        parent_section_id=None,
                        title_jp=(
                            chapter_title
                        ),
                        title_en=None,
                        level=2,
                        sort_order=(
                            global_section_order
                            + 1
                        ),
                        page_number=(
                            page_number
                        ),
                        metadata={
                            "source": (
                                "pptx"
                            ),
                            "pptx_parser_mode": (
                                "presentation"
                            ),
                            "slide_index": (
                                slide_index
                            ),
                            "slide_number": (
                                slide_number
                            ),
                            "generated": (
                                True
                            ),
                            "reason": (
                                "slide_without_secondary_heading"
                            ),
                        },
                    )
                )

                document.sections.append(
                    default_section
                )

                global_section_order += 1
                generated_section_count += 1

                for content in (
                    document.contents
                ):

                    if (
                        content.chapter_id
                        == chapter_id
                        and not content.section_id
                    ):

                        content.section_id = (
                            default_section_id
                        )

        document.metadata.update(
            {
                "parser": (
                    "PPTXParser"
                ),
                "parser_status": (
                    "SUCCESS"
                ),
                "pptx_parser_mode": (
                    "presentation"
                ),
                "pptx_parser_mode_reason": (
                    mode_diagnostics.get(
                        "reason"
                    )
                ),
                "pptx_placeholder_slide_title_count": (
                    mode_diagnostics[
                        "placeholder_title_count"
                    ]
                ),
                "pptx_meaningful_slide_title_count": (
                    mode_diagnostics[
                        "meaningful_title_count"
                    ]
                ),
                "pptx_placeholder_slide_title_ratio": (
                    mode_diagnostics[
                        "placeholder_title_ratio"
                    ]
                ),
                "pptx_detected_numbered_heading_count": (
                    mode_diagnostics[
                        "numbered_heading_count"
                    ]
                ),
                "pptx_parsed_slide_count": (
                    parsed_slide_count
                ),
                "pptx_generated_section_count": (
                    generated_section_count
                ),
                "pptx_generated_content_count": (
                    generated_content_count
                ),
                "pptx_chapter_root_content_count": (
                    chapter_root_content_count
                ),
                "pptx_skipped_empty_content_count": (
                    skipped_empty_content_count
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

    # ==================================================
    # Specification Mode
    # ==================================================

    def _parse_specification_mode(
        self,
        *,
        document: Document,
        slide_indexes: list[int],
        blocks_by_slide: dict[
            int,
            list[
                DocumentBlock
            ],
        ],
        slide_records: dict[
            int,
            dict[
                str,
                Any,
            ],
        ],
        mode_diagnostics: dict[
            str,
            Any,
        ],
    ) -> None:
        """
        从 Slide 正文中恢复规格书 Chapter / Section。
        """

        chapter_map: dict[
            str,
            Chapter,
        ] = {}

        section_map: dict[
            str,
            Section,
        ] = {}

        current_chapter: (
            Chapter
            | None
        ) = None

        current_section: (
            Section
            | None
        ) = None

        parsed_slide_count = 0
        generated_content_count = 0
        chapter_root_content_count = 0
        skipped_empty_content_count = 0

        semantic_chapter_count = 0
        semantic_section_count = 0
        synthetic_chapter_count = 0
        repeated_heading_count = 0
        rejected_heading_candidate_count = 0
        discarded_preamble_content_count = 0

        toc_slide_count = 0
        toc_slide_numbers: list[
            int
        ] = []

        toc_guided_accept_count = 0
        toc_guided_reject_count = 0

        back_matter_slide_count = 0
        back_matter_section_count = 0

        global_chapter_order = 0
        global_section_order = 0

        back_matter_chapter: (
            Chapter
            | None
        ) = None

        # ==============================================
        # Canonical Outline from TOC
        # ==============================================
        #
        # CarPlay 规格书这类 PPTX 的前置目录本身包含稳定结构：
        #
        #   1 Introduction
        #   1.1 Purpose
        #   ...
        #   4 Arbitration...
        #   5 Software Update
        #   6 Performance requirement
        #   7 Exclusive control...
        #
        # 目录页不作为正文，但先提取为 Canonical Outline。
        #
        # 后续正文中：
        #
        #   4.基本音声(Main Audio)...
        #
        # 虽然正则上像 Chapter 4，但标题与 Canonical Chapter 4
        # 不兼容，因此只能作为普通正文/编号列表。
        #
        # 真正：
        #
        #   4 CarPlayと車載機間の同一機能調停...
        #
        # 则可以在页面中部直接被接受，不再依赖“前 12 行”启发式。

        (
            canonical_outline,
            toc_slide_indexes,
            toc_slide_numbers,
        ) = self._build_specification_canonical_outline(
            slide_indexes=(
                slide_indexes
            ),
            blocks_by_slide=(
                blocks_by_slide
            ),
            slide_records=(
                slide_records
            ),
        )

        toc_slide_count = len(
            toc_slide_indexes
        )

        canonical_chapter_titles = {
            heading_id: title
            for heading_id, title
            in canonical_outline.items()
            if "." not in heading_id
        }

        canonical_section_titles = {
            heading_id: title
            for heading_id, title
            in canonical_outline.items()
            if "." in heading_id
        }

        # ==============================================
        # Upsert Chapter
        # ==============================================

        def ensure_chapter(
            *,
            chapter_id: str,
            title: str,
            page_number: int | None,
            metadata: dict[
                str,
                Any,
            ],
            synthetic: bool = False,
        ) -> Chapter:

            nonlocal global_chapter_order
            nonlocal semantic_chapter_count
            nonlocal synthetic_chapter_count

            existing = (
                chapter_map.get(
                    chapter_id
                )
            )

            normalized_title = (
                self._normalize_title(
                    title
                )
            )

            if existing is not None:

                if (
                    existing.metadata.get(
                        "synthetic"
                    )
                    and normalized_title
                ):

                    existing.title_jp = (
                        normalized_title
                    )

                    existing.metadata.pop(
                        "synthetic",
                        None,
                    )

                    existing.metadata.pop(
                        "synthetic_reason",
                        None,
                    )

                    existing.metadata.update(
                        metadata
                    )

                return (
                    existing
                )

            global_chapter_order += 1

            chapter = Chapter(
                id=(
                    chapter_id
                ),
                title_jp=(
                    normalized_title
                    or (
                        f"Chapter "
                        f"{chapter_id}"
                    )
                ),
                title_en=None,
                level=1,
                sort_order=(
                    global_chapter_order
                ),
                page_number=(
                    page_number
                ),
                metadata={
                    "source": (
                        "pptx"
                    ),
                    "pptx_parser_mode": (
                        "specification"
                    ),
                    **metadata,
                },
            )

            if synthetic:

                chapter.metadata.update(
                    {
                        "synthetic": (
                            True
                        ),
                        "synthetic_reason": (
                            "section_before_chapter_heading"
                        ),
                    }
                )

                synthetic_chapter_count += 1

            else:

                semantic_chapter_count += 1

            document.chapters.append(
                chapter
            )

            chapter_map[
                chapter_id
            ] = chapter

            return (
                chapter
            )

        # ==============================================
        # Upsert Section
        # ==============================================

        def ensure_section(
            *,
            section_id: str,
            chapter_id: str,
            title: str,
            level: int,
            page_number: int | None,
            metadata: dict[
                str,
                Any,
            ],
        ) -> tuple[
            Section,
            bool,
        ]:

            nonlocal global_section_order
            nonlocal semantic_section_count

            existing = (
                section_map.get(
                    section_id
                )
            )

            normalized_title = (
                self._normalize_title(
                    title
                )
            )

            if existing is not None:

                return (
                    existing,
                    False,
                )

            global_section_order += 1

            section = Section(
                id=(
                    section_id
                ),
                chapter_id=(
                    chapter_id
                ),
                parent_section_id=None,
                title_jp=(
                    normalized_title
                    or (
                        f"Section "
                        f"{section_id}"
                    )
                ),
                title_en=None,
                level=(
                    max(
                        int(
                            level
                        ),
                        2,
                    )
                ),
                sort_order=(
                    global_section_order
                ),
                page_number=(
                    page_number
                ),
                metadata={
                    "source": (
                        "pptx"
                    ),
                    "pptx_parser_mode": (
                        "specification"
                    ),
                    **metadata,
                },
            )

            document.sections.append(
                section
            )

            section_map[
                section_id
            ] = section

            semantic_section_count += 1

            return (
                section,
                True,
            )

        # ==============================================
        # Slide Loop
        # ==============================================

        for (
            slide_position,
            slide_index,
        ) in enumerate(
            slide_indexes,
            start=1,
        ):

            slide_blocks = sorted(
                blocks_by_slide.get(
                    slide_index,
                    [],
                ),
                key=self._block_sort_key,
            )

            slide_record = (
                slide_records.get(
                    slide_index,
                    {},
                )
            )

            slide_number = (
                self._resolve_slide_number(
                    slide_index=(
                        slide_index
                    ),
                    slide_record=(
                        slide_record
                    ),
                    blocks=(
                        slide_blocks
                    ),
                )
            )

            page_number = (
                self._resolve_page_number(
                    slide_blocks
                )
            )

            if page_number is None:

                page_number = (
                    slide_number
                )

            parsed_slide_count += 1

            slide_text_lines = (
                self._collect_spec_text_lines(
                    slide_blocks
                )
            )

            # ==========================================
            # TOC
            # ==========================================

            if (
                self.skip_toc_slides_in_specification_mode
                and slide_index
                in toc_slide_indexes
            ):

                continue

            # ==========================================
            # Back Matter / Change History
            # ==========================================

            change_history_title = (
                self._find_change_history_title(
                    slide_text_lines
                )
            )

            if (
                change_history_title
                and self.create_back_matter_chapter
            ):

                if back_matter_chapter is None:

                    global_chapter_order += 1

                    back_matter_chapter = (
                        Chapter(
                            id=(
                                self.back_matter_chapter_id
                            ),
                            title_jp=(
                                self.back_matter_chapter_title
                            ),
                            title_en=None,
                            level=1,
                            sort_order=(
                                global_chapter_order
                            ),
                            page_number=(
                                page_number
                            ),
                            metadata={
                                "source": (
                                    "pptx"
                                ),
                                "pptx_parser_mode": (
                                    "specification"
                                ),
                                "synthetic": (
                                    True
                                ),
                                "back_matter": (
                                    True
                                ),
                                "back_matter_type": (
                                    "change_history"
                                ),
                            },
                        )
                    )

                    document.chapters.append(
                        back_matter_chapter
                    )

                    chapter_map[
                        back_matter_chapter.id
                    ] = (
                        back_matter_chapter
                    )

                current_chapter = (
                    back_matter_chapter
                )

                global_section_order += 1

                back_matter_section_id = (
                    f"{self.back_matter_chapter_id}."
                    f"{slide_number}"
                )

                current_section = (
                    Section(
                        id=(
                            back_matter_section_id
                        ),
                        chapter_id=(
                            back_matter_chapter.id
                        ),
                        parent_section_id=None,
                        title_jp=(
                            change_history_title
                        ),
                        title_en=None,
                        level=2,
                        sort_order=(
                            global_section_order
                        ),
                        page_number=(
                            page_number
                        ),
                        metadata={
                            "source": (
                                "pptx"
                            ),
                            "pptx_parser_mode": (
                                "specification"
                            ),
                            "back_matter": (
                                True
                            ),
                            "back_matter_type": (
                                "change_history"
                            ),
                            "slide_index": (
                                slide_index
                            ),
                            "slide_number": (
                                slide_number
                            ),
                        },
                    )
                )

                document.sections.append(
                    current_section
                )

                section_map[
                    current_section.id
                ] = current_section

                back_matter_slide_count += 1
                back_matter_section_count += 1

                content_parts: list[
                    str
                ] = []

                for block in (
                    slide_blocks
                ):

                    content_text = (
                        self._build_content_text(
                            block
                        )
                    )

                    if not content_text:
                        continue

                    content_text = (
                        self._remove_page_placeholder_lines(
                            content_text
                        )
                    )

                    if not content_text:
                        continue

                    content_parts.append(
                        content_text
                    )

                text = (
                    self.content_separator
                    .join(
                        content_parts
                    )
                    .strip()
                )

                if text:

                    document.contents.append(
                        Content(
                            chapter_id=(
                                current_chapter.id
                            ),
                            section_id=(
                                current_section.id
                            ),
                            text=(
                                text
                            ),
                            page_number=(
                                page_number
                            ),
                        )
                    )

                    generated_content_count += 1

                else:

                    skipped_empty_content_count += 1

                continue

            # ==========================================
            # Semantic Body
            # ==========================================

            content_buffer: list[
                str
            ] = []

            content_page_number = (
                page_number
            )

            semantic_line_position = 0

            def flush_content() -> None:

                nonlocal content_buffer
                nonlocal generated_content_count
                nonlocal chapter_root_content_count
                nonlocal skipped_empty_content_count
                nonlocal discarded_preamble_content_count

                text = (
                    self.content_separator
                    .join(
                        content_buffer
                    )
                    .strip()
                )

                content_buffer = []

                if not text:

                    skipped_empty_content_count += 1

                    return

                if current_chapter is None:

                    discarded_preamble_content_count += 1

                    return

                section_id = (
                    current_section.id
                    if current_section
                    is not None
                    else None
                )

                document.contents.append(
                    Content(
                        chapter_id=(
                            current_chapter.id
                        ),
                        section_id=(
                            section_id
                        ),
                        text=(
                            text
                        ),
                        page_number=(
                            content_page_number
                        ),
                    )
                )

                generated_content_count += 1

                if section_id is None:

                    chapter_root_content_count += 1

            for block in (
                slide_blocks
            ):

                # --------------------------------------
                # Table / Image / Chart:
                # Preserve as content, never Heading.
                # --------------------------------------

                if (
                    block.block_type
                    == BlockType.TABLE
                    or block.block_type
                    == BlockType.IMAGE
                    or str(
                        block.metadata.get(
                            "content_kind",
                            "",
                        )
                    ).strip().lower()
                    == "chart"
                ):

                    content_text = (
                        self._build_content_text(
                            block
                        )
                    )

                    if content_text:

                        content_page_number = (
                            block.page_number
                            or page_number
                        )

                        content_buffer.append(
                            content_text
                        )

                    continue

                text = (
                    self._normalize_text(
                        block.text
                    )
                )

                if not text:
                    continue

                lines = (
                    text.splitlines()
                )

                for raw_line in (
                    lines
                ):

                    line = (
                        self._normalize_text(
                            raw_line
                        )
                    )

                    if not line:
                        continue

                    if self._is_page_placeholder_title(
                        line
                    ):

                        continue

                    semantic_line_position += 1

                    heading = (
                        self._detect_spec_heading(
                            line
                        )
                    )

                    if heading is None:

                        content_line = (
                            self._format_spec_content_line(
                                block=(
                                    block
                                ),
                                text=(
                                    line
                                ),
                            )
                        )

                        if content_line:

                            content_page_number = (
                                block.page_number
                                or page_number
                            )

                            content_buffer.append(
                                content_line
                            )

                        continue

                    heading_id = (
                        heading[
                            "id"
                        ]
                    )

                    heading_title = (
                        heading[
                            "title"
                        ]
                    )

                    heading_level = (
                        heading[
                            "level"
                        ]
                    )

                    # ==================================
                    # Chapter Heading
                    # ==================================

                    if heading_level == 1:

                        if not self._is_reliable_spec_chapter_heading(
                            heading_id=(
                                heading_id
                            ),
                            heading_title=(
                                heading_title
                            ),
                            current_chapter=(
                                current_chapter
                            ),
                            current_section=(
                                current_section
                            ),
                            chapter_map=(
                                chapter_map
                            ),
                            semantic_line_position=(
                                semantic_line_position
                            ),
                            canonical_chapter_titles=(
                                canonical_chapter_titles
                            ),
                        ):

                            rejected_heading_candidate_count += 1

                            if canonical_chapter_titles:

                                toc_guided_reject_count += 1

                            content_line = (
                                self._format_spec_content_line(
                                    block=(
                                        block
                                    ),
                                    text=(
                                        line
                                    ),
                                )
                            )

                            if content_line:

                                content_buffer.append(
                                    content_line
                                )

                            continue

                        if (
                            heading_id
                            in canonical_chapter_titles
                            and self._titles_compatible_with_outline(
                                canonical_chapter_titles[
                                    heading_id
                                ],
                                heading_title,
                            )
                        ):

                            toc_guided_accept_count += 1

                        # Same Chapter repeated inside an active Section:
                        # repeated page header should not reset context.
                        existing_chapter = (
                            chapter_map.get(
                                heading_id
                            )
                        )

                        if (
                            current_chapter
                            is not None
                            and current_section
                            is not None
                            and current_chapter.id
                            == heading_id
                            and existing_chapter
                            is not None
                            and self._titles_equivalent(
                                existing_chapter.title_jp,
                                heading_title,
                            )
                        ):

                            repeated_heading_count += 1

                            continue

                        flush_content()

                        current_chapter = (
                            ensure_chapter(
                                chapter_id=(
                                    heading_id
                                ),
                                title=(
                                    heading_title
                                ),
                                page_number=(
                                    block.page_number
                                    or page_number
                                ),
                                metadata={
                                    "slide_index": (
                                        slide_index
                                    ),
                                    "slide_number": (
                                        slide_number
                                    ),
                                    "semantic_heading": (
                                        True
                                    ),
                                },
                                synthetic=False,
                            )
                        )

                        current_section = (
                            None
                        )

                        continue

                    # ==================================
                    # Section Heading
                    # ==================================

                    chapter_id = (
                        heading_id.split(
                            ".",
                            maxsplit=1,
                        )[0]
                    )

                    if (
                        current_chapter
                        is not None
                        and chapter_id
                        != current_chapter.id
                    ):

                        # Do not let a numbered body/list line jump
                        # into another Chapter.
                        rejected_heading_candidate_count += 1

                        content_line = (
                            self._format_spec_content_line(
                                block=(
                                    block
                                ),
                                text=(
                                    line
                                ),
                            )
                        )

                        if content_line:

                            content_buffer.append(
                                content_line
                            )

                        continue

                    canonical_section_title = (
                        canonical_section_titles.get(
                            heading_id
                        )
                    )

                    if (
                        canonical_section_title
                        is not None
                        and not self._titles_compatible_with_outline(
                            canonical_section_title,
                            heading_title,
                        )
                    ):

                        rejected_heading_candidate_count += 1
                        toc_guided_reject_count += 1

                        content_line = (
                            self._format_spec_content_line(
                                block=(
                                    block
                                ),
                                text=(
                                    line
                                ),
                            )
                        )

                        if content_line:

                            content_buffer.append(
                                content_line
                            )

                        continue

                    if canonical_section_title is not None:

                        toc_guided_accept_count += 1

                    existing_section = (
                        section_map.get(
                            heading_id
                        )
                    )

                    if (
                        existing_section
                        is not None
                        and not self._titles_equivalent(
                            existing_section.title_jp,
                            heading_title,
                        )
                    ):

                        # Same numeric ID but different sentence:
                        # likely numbered body text, not heading.
                        rejected_heading_candidate_count += 1

                        content_line = (
                            self._format_spec_content_line(
                                block=(
                                    block
                                ),
                                text=(
                                    line
                                ),
                            )
                        )

                        if content_line:

                            content_buffer.append(
                                content_line
                            )

                        continue

                    flush_content()

                    if current_chapter is None:

                        current_chapter = (
                            ensure_chapter(
                                chapter_id=(
                                    chapter_id
                                ),
                                title=(
                                    f"Chapter "
                                    f"{chapter_id}"
                                ),
                                page_number=(
                                    block.page_number
                                    or page_number
                                ),
                                metadata={
                                    "slide_index": (
                                        slide_index
                                    ),
                                    "slide_number": (
                                        slide_number
                                    ),
                                },
                                synthetic=True,
                            )
                        )

                    (
                        current_section,
                        created,
                    ) = ensure_section(
                        section_id=(
                            heading_id
                        ),
                        chapter_id=(
                            chapter_id
                        ),
                        title=(
                            heading_title
                        ),
                        level=(
                            heading_level
                        ),
                        page_number=(
                            block.page_number
                            or page_number
                        ),
                        metadata={
                            "slide_index": (
                                slide_index
                            ),
                            "slide_number": (
                                slide_number
                            ),
                            "semantic_heading": (
                                True
                            ),
                        },
                    )

                    if not created:

                        repeated_heading_count += 1

            # Page boundary: preserve citation page.
            flush_content()

        document.metadata.update(
            {
                "parser": (
                    "PPTXParser"
                ),
                "parser_status": (
                    "SUCCESS"
                ),
                "pptx_parser_mode": (
                    "specification"
                ),
                "pptx_parser_strategy": (
                    "toc_guided_canonical_outline_dual_mode_v2"
                ),
                "pptx_parser_mode_reason": (
                    mode_diagnostics.get(
                        "reason"
                    )
                ),
                "pptx_placeholder_slide_title_count": (
                    mode_diagnostics[
                        "placeholder_title_count"
                    ]
                ),
                "pptx_meaningful_slide_title_count": (
                    mode_diagnostics[
                        "meaningful_title_count"
                    ]
                ),
                "pptx_placeholder_slide_title_ratio": (
                    mode_diagnostics[
                        "placeholder_title_ratio"
                    ]
                ),
                "pptx_detected_numbered_heading_count": (
                    mode_diagnostics[
                        "numbered_heading_count"
                    ]
                ),
                "pptx_parsed_slide_count": (
                    parsed_slide_count
                ),
                "pptx_generated_section_count": len(
                    document.sections
                ),
                "pptx_generated_content_count": (
                    generated_content_count
                ),
                "pptx_chapter_root_content_count": (
                    chapter_root_content_count
                ),
                "pptx_skipped_empty_content_count": (
                    skipped_empty_content_count
                ),
                "pptx_spec_semantic_chapter_count": (
                    semantic_chapter_count
                ),
                "pptx_spec_semantic_section_count": (
                    semantic_section_count
                ),
                "pptx_spec_synthetic_chapter_count": (
                    synthetic_chapter_count
                ),
                "pptx_spec_repeated_heading_count": (
                    repeated_heading_count
                ),
                "pptx_spec_rejected_heading_candidate_count": (
                    rejected_heading_candidate_count
                ),
                "pptx_spec_discarded_preamble_content_count": (
                    discarded_preamble_content_count
                ),
                "pptx_spec_toc_slide_count": (
                    toc_slide_count
                ),
                "pptx_spec_toc_slide_numbers": (
                    toc_slide_numbers
                ),
                "pptx_spec_canonical_outline_count": len(
                    canonical_outline
                ),
                "pptx_spec_canonical_chapter_count": len(
                    canonical_chapter_titles
                ),
                "pptx_spec_canonical_section_count": len(
                    canonical_section_titles
                ),
                "pptx_spec_toc_guided_accept_count": (
                    toc_guided_accept_count
                ),
                "pptx_spec_toc_guided_reject_count": (
                    toc_guided_reject_count
                ),
                "pptx_spec_back_matter_slide_count": (
                    back_matter_slide_count
                ),
                "pptx_spec_back_matter_section_count": (
                    back_matter_section_count
                ),
                "pptx_spec_back_matter_chapter_id": (
                    self.back_matter_chapter_id
                    if back_matter_chapter
                    is not None
                    else None
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

    # ==================================================
    # Mode Detection
    # ==================================================

    def _resolve_parser_mode(
        self,
        *,
        slide_indexes: list[int],
        blocks_by_slide: dict[
            int,
            list[
                DocumentBlock
            ],
        ],
        slide_records: dict[
            int,
            dict[
                str,
                Any,
            ],
        ],
    ) -> tuple[
        str,
        dict[
            str,
            Any,
        ],
    ]:

        placeholder_title_count = 0
        meaningful_title_count = 0
        title_count = 0
        numbered_heading_count = 0

        for slide_index in (
            slide_indexes
        ):

            blocks = sorted(
                blocks_by_slide.get(
                    slide_index,
                    [],
                ),
                key=self._block_sort_key,
            )

            record = (
                slide_records.get(
                    slide_index,
                    {},
                )
            )

            title_block = (
                self._find_slide_title_block(
                    blocks
                )
            )

            slide_number = (
                self._resolve_slide_number(
                    slide_index=(
                        slide_index
                    ),
                    slide_record=(
                        record
                    ),
                    blocks=(
                        blocks
                    ),
                )
            )

            title = (
                self._resolve_chapter_title(
                    slide_number=(
                        slide_number
                    ),
                    slide_record=(
                        record
                    ),
                    title_block=(
                        title_block
                    ),
                    blocks=(
                        blocks
                    ),
                )
            )

            if title:

                title_count += 1

                if self._is_page_placeholder_title(
                    title
                ):

                    placeholder_title_count += 1

                else:

                    meaningful_title_count += 1

            for block in blocks:

                if (
                    block.block_type
                    == BlockType.TABLE
                    or block.block_type
                    == BlockType.IMAGE
                ):

                    continue

                if (
                    str(
                        block.metadata.get(
                            "content_kind",
                            "",
                        )
                    ).strip().lower()
                    == "chart"
                ):

                    continue

                text = (
                    self._normalize_text(
                        block.text
                    )
                )

                if not text:
                    continue

                for line in (
                    text.splitlines()
                ):

                    if self._is_page_placeholder_title(
                        line
                    ):

                        continue

                    if self._detect_spec_heading(
                        line
                    ):

                        numbered_heading_count += 1

        placeholder_ratio = (
            placeholder_title_count
            / title_count
            if title_count
            else 0.0
        )

        diagnostics = {
            "placeholder_title_count": (
                placeholder_title_count
            ),
            "meaningful_title_count": (
                meaningful_title_count
            ),
            "title_count": (
                title_count
            ),
            "placeholder_title_ratio": round(
                placeholder_ratio,
                4,
            ),
            "numbered_heading_count": (
                numbered_heading_count
            ),
            "reason": None,
        }

        if self.mode == "presentation":

            diagnostics[
                "reason"
            ] = (
                "explicit_presentation_mode"
            )

            return (
                "presentation",
                diagnostics,
            )

        if self.mode == "specification":

            diagnostics[
                "reason"
            ] = (
                "explicit_specification_mode"
            )

            return (
                "specification",
                diagnostics,
            )

        if (
            placeholder_ratio
            >= self.specification_placeholder_title_ratio
            and numbered_heading_count
            >= self.specification_minimum_numbered_heading_count
        ):

            diagnostics[
                "reason"
            ] = (
                "high_page_placeholder_ratio_"
                "with_numbered_spec_headings"
            )

            return (
                "specification",
                diagnostics,
            )

        diagnostics[
            "reason"
        ] = (
            "meaningful_slide_titles_or_"
            "insufficient_spec_heading_signal"
        )

        return (
            "presentation",
            diagnostics,
        )

    # ==================================================
    # Specification Heading Detection
    # ==================================================

    @classmethod
    def _detect_spec_heading(
        cls,
        line: str,
    ) -> dict[
        str,
        Any,
    ] | None:

        normalized = (
            cls._normalize_outline_line(
                line
            )
        )

        if not normalized:

            return None

        if cls._is_page_placeholder_title(
            normalized
        ):

            return None

        match = (
            cls._SPEC_HEADING_PATTERN.fullmatch(
                normalized
            )
        )

        if match is None:

            return None

        heading_id = (
            match.group(
                "id"
            ).strip()
        )

        title = (
            match.group(
                "title"
            ).strip()
        )

        if not heading_id:
            return None

        if not cls._looks_like_meaningful_heading_title(
            title
        ):
            return None

        parts = (
            heading_id.split(
                "."
            )
        )

        if len(parts) > 8:
            return None

        if any(
            not part.isdigit()
            for part
            in parts
        ):
            return None

        # Huge numeric values are almost always version/data cells.
        if any(
            len(part) > 4
            for part
            in parts
        ):
            return None

        return {
            "id": (
                heading_id
            ),
            "title": (
                title
            ),
            "level": len(
                parts
            ),
        }

    @classmethod
    def _is_reliable_spec_chapter_heading(
        cls,
        *,
        heading_id: str,
        heading_title: str,
        current_chapter: Chapter | None,
        current_section: Section | None,
        chapter_map: dict[
            str,
            Chapter,
        ],
        semantic_line_position: int,
        canonical_chapter_titles: dict[
            str,
            str,
        ] | None = None,
    ) -> bool:
        """
        Specification Mode Chapter 判定。

        优先级：

            1. 如果 TOC 已建立 Canonical Chapter：
               - ID 在 Canonical 中：
                   标题必须与 Canonical 兼容。
               - ID 不在 Canonical 中：
                   当 Canonical Chapter 足够完整时直接拒绝。

            2. 没有可用 Canonical Outline 时，
               才回退到原启发式：

                   - 首章靠近 Slide 前部
                   - 禁止反向跳 Chapter
                   - 顺序 Chapter 优先
                   - 大跨度仅允许强语义标题

        Canonical 优先的目的：

            current Chapter = 3

            "4.基本音声(Main Audio)..."
                -> ID=4，但标题与 TOC Chapter 4 不匹配
                -> False

            "4 CarPlayと車載機間の同一機能調停..."
                -> 与 TOC Chapter 4 匹配
                -> True

            "5 ソフトウェア更新対応..."
            "6 性能要件..."
                -> 即使位于页面中部也直接 True
        """

        normalized_id = str(
            heading_id
            or ""
        ).strip()

        normalized_title = str(
            heading_title
            or ""
        ).strip()

        if not normalized_id.isdigit():

            return False

        if not cls._looks_like_meaningful_heading_title(
            normalized_title
        ):

            return False

        canonical_chapters = (
            canonical_chapter_titles
            or {}
        )

        if canonical_chapters:

            canonical_title = (
                canonical_chapters.get(
                    normalized_id
                )
            )

            if canonical_title is not None:

                return cls._titles_compatible_with_outline(
                    canonical_title,
                    normalized_title,
                )

            # 目录中已经存在多个稳定 Chapter 时，
            # 未出现在 Canonical Outline 的新一级编号
            # 高概率是正文编号列表。
            if len(
                canonical_chapters
            ) >= 3:

                return False

        candidate = int(
            normalized_id
        )

        # First semantic Chapter:
        # require it to appear near the top of a Slide.
        if current_chapter is None:

            return (
                semantic_line_position
                <= 12
            )

        current_id = str(
            current_chapter.id
            or ""
        )

        # Back matter context should not switch back automatically.
        if not current_id.isdigit():

            return False

        current = int(
            current_id
        )

        # Same Chapter repeated inside active Section:
        # only allow if title is exactly the known Chapter title.
        if candidate == current:

            known = (
                chapter_map.get(
                    normalized_id
                )
            )

            if (
                known is not None
                and cls._titles_equivalent(
                    known.title_jp,
                    normalized_title,
                )
            ):

                return True

            return (
                current_section
                is None
                and semantic_line_position
                <= 12
            )

        # Never jump backwards because numbered lists such as:
        #
        #   1. Resume playback...
        #
        # are common inside Chapter 2/3.
        if candidate < current:

            return False

        # New Chapter should normally start near slide top.
        if semantic_line_position > 12:

            return False

        # Sequential Chapter is safest.
        if candidate == current + 1:

            return True

        # Allow forward gaps only for a strong semantic title.
        return cls._looks_like_strong_chapter_title(
            normalized_title
        )

    @classmethod
    def _looks_like_strong_chapter_title(
        cls,
        title: str,
    ) -> bool:

        normalized = (
            cls._normalize_outline_line(
                title
            )
        )

        if not normalized:
            return False

        if (
            "/" in normalized
            and len(
                normalized
            ) >= 8
        ):

            return True

        has_cjk = any(
            (
                "\u3040"
                <= char
                <= "\u30ff"
            )
            or (
                "\u3400"
                <= char
                <= "\u9fff"
            )
            for char
            in normalized
        )

        has_ascii = bool(
            re.search(
                r"[A-Za-z]",
                normalized,
            )
        )

        if (
            has_cjk
            and has_ascii
        ):

            return True

        words = re.findall(
            r"[A-Za-z][A-Za-z0-9_-]*",
            normalized,
        )

        return len(
            words
        ) >= 2

    @staticmethod
    def _looks_like_meaningful_heading_title(
        title: str,
    ) -> bool:

        normalized = str(
            title
            or ""
        ).strip()

        if not normalized:
            return False

        if len(
            normalized
        ) > 300:
            return False

        if re.fullmatch(
            r"[\W_]+",
            normalized,
        ):
            return False

        return any(
            char.isalpha()
            or (
                "\u3040"
                <= char
                <= "\u30ff"
            )
            or (
                "\u3400"
                <= char
                <= "\u9fff"
            )
            for char
            in normalized
        )

    # ==================================================
    # Canonical Outline from TOC
    # ==================================================

    def _build_specification_canonical_outline(
        self,
        *,
        slide_indexes: list[int],
        blocks_by_slide: dict[
            int,
            list[
                DocumentBlock
            ],
        ],
        slide_records: dict[
            int,
            dict[
                str,
                Any,
            ],
        ],
    ) -> tuple[
        dict[
            str,
            str,
        ],
        set[int],
        list[int],
    ]:
        """
        预扫描 Specification PPTX 的 TOC Slide。

        Returns:

            canonical_outline:
                {
                    "1": "Introduction",
                    "1.1": "目的 / Purpose",
                    "3.5.1.2": "オーディオのDucking対応 / Audio Ducking Support",
                    "4": "CarPlayと車載機間の同一機能調停 / ...",
                    "5": "ソフトウェア更新対応 / Software Update",
                    ...
                }

            toc_slide_indexes:
                需要从正文跳过的 slide_index 集合。

            toc_slide_numbers:
                诊断用逻辑页码。
        """

        canonical_outline: dict[
            str,
            str,
        ] = {}

        toc_slide_indexes: set[
            int
        ] = set()

        toc_slide_numbers: list[
            int
        ] = []

        for (
            slide_position,
            slide_index,
        ) in enumerate(
            slide_indexes,
            start=1,
        ):

            blocks = sorted(
                blocks_by_slide.get(
                    slide_index,
                    [],
                ),
                key=self._block_sort_key,
            )

            record = (
                slide_records.get(
                    slide_index,
                    {},
                )
            )

            slide_number = (
                self._resolve_slide_number(
                    slide_index=(
                        slide_index
                    ),
                    slide_record=(
                        record
                    ),
                    blocks=(
                        blocks
                    ),
                )
            )

            lines = (
                self._collect_spec_text_lines(
                    blocks
                )
            )

            if not self._looks_like_toc_slide(
                lines=(
                    lines
                ),
                slide_position=(
                    slide_position
                ),
            ):

                continue

            toc_slide_indexes.add(
                slide_index
            )

            toc_slide_numbers.append(
                slide_number
            )

            for raw_line in lines:

                heading = (
                    self._detect_spec_heading(
                        raw_line
                    )
                )

                if heading is None:

                    continue

                heading_id = str(
                    heading[
                        "id"
                    ]
                ).strip()

                heading_title = (
                    self._normalize_title(
                        heading[
                            "title"
                        ]
                    )
                )

                if (
                    not heading_id
                    or not heading_title
                ):

                    continue

                existing_title = (
                    canonical_outline.get(
                        heading_id
                    )
                )

                if existing_title is None:

                    canonical_outline[
                        heading_id
                    ] = heading_title

                    continue

                # 同一个 ID 在连续 TOC 页重复时，
                # 只在标题实质等价时保持原值。
                # 不让后续修订注记覆盖 Canonical title。
                if self._titles_compatible_with_outline(
                    existing_title,
                    heading_title,
                ):

                    continue

        return (
            canonical_outline,
            toc_slide_indexes,
            toc_slide_numbers,
        )

    @classmethod
    def _titles_compatible_with_outline(
        cls,
        canonical_title: Any,
        candidate_title: Any,
    ) -> bool:
        """
        判断正文标题是否与 TOC Canonical title 兼容。

        支持：

            TOC:
                他機能との排他制御 /
                Exclusive control with other function

            Body:
                他機能との排他制御(1)/
                Exclusive control with other function(1)

            -> True

        以及：

            TOC:
                ソフトウェア更新対応 / Software Update

            Body:
                ソフトウェア更新対応 / Support of software update

            -> True

        但：

            TOC Chapter 4:
                CarPlayと車載機間の同一機能調停 / Arbitration...

            Body numbered list:
                基本音声(Main Audio)と基本音声(Main Buffered Audio)...

            -> False
        """

        canonical = (
            cls._normalize_outline_title_for_match(
                canonical_title
            )
        )

        candidate = (
            cls._normalize_outline_title_for_match(
                candidate_title
            )
        )

        if (
            not canonical
            or not candidate
        ):

            return False

        if canonical == candidate:

            return True

        if cls._titles_equivalent(
            canonical,
            candidate,
        ):

            return True

        canonical_parts = (
            cls._split_bilingual_title_parts(
                canonical_title
            )
        )

        candidate_parts = (
            cls._split_bilingual_title_parts(
                candidate_title
            )
        )

        # 任一语言侧稳定一致即可。
        for left in canonical_parts:

            if len(
                left
            ) < 3:

                continue

            for right in candidate_parts:

                if len(
                    right
                ) < 3:

                    continue

                if left == right:

                    return True

                # 日文/中文标题部分通常稳定，
                # 英译可能有 Software Update / Support of software update
                # 这种措辞差异。
                if (
                    cls._contains_cjk(
                        left
                    )
                    and cls._contains_cjk(
                        right
                    )
                    and (
                        left in right
                        or right in left
                    )
                ):

                    return True

        return False

    @classmethod
    def _normalize_outline_title_for_match(
        cls,
        value: Any,
    ) -> str:

        normalized = (
            cls._normalize_outline_line(
                value
            ).casefold()
        )

        if not normalized:

            return ""

        # 去除正文中用于“第 1 页/第 2 页”区分的局部编号：
        #
        #   Exclusive control with other function(1)
        #   Exclusive control with other function(2)
        #
        # TOC 通常只写总标题。
        normalized = re.sub(
            r"""
            [\(（]
            \s*
            \d+
            \s*
            [\)）]
            """,
            "",
            normalized,
            flags=re.VERBOSE,
        )

        normalized = re.sub(
            r"""
            [\s
             /／
             :：
             .．
             _\-
             ,，、
             \(\)（）]
            +
            """,
            "",
            normalized,
            flags=re.VERBOSE,
        )

        return (
            normalized.strip()
        )

    @classmethod
    def _split_bilingual_title_parts(
        cls,
        value: Any,
    ) -> list[str]:

        raw = (
            cls._normalize_outline_line(
                value
            ).casefold()
        )

        if not raw:

            return []

        raw = re.sub(
            r"""
            [\(（]
            \s*
            \d+
            \s*
            [\)）]
            """,
            "",
            raw,
            flags=re.VERBOSE,
        )

        parts = re.split(
            r"\s*[／/]\s*",
            raw,
        )

        result: list[str] = []

        for part in parts:

            compact = re.sub(
                r"""
                [\s
                 :：
                 .．
                 _\-
                 ,，、
                 \(\)（）]
                +
                """,
                "",
                part,
                flags=re.VERBOSE,
            ).strip()

            if compact:

                result.append(
                    compact
                )

        if not result:

            fallback = (
                cls._normalize_outline_title_for_match(
                    raw
                )
            )

            if fallback:

                result.append(
                    fallback
                )

        return (
            result
        )

    @staticmethod
    def _contains_cjk(
        value: str,
    ) -> bool:

        return any(
            (
                "\u3040"
                <= char
                <= "\u30ff"
            )
            or (
                "\u3400"
                <= char
                <= "\u9fff"
            )
            for char
            in str(
                value
                or ""
            )
        )

    # ==================================================
    # TOC
    # ==================================================

    def _looks_like_toc_slide(
        self,
        *,
        lines: list[str],
        slide_position: int,
    ) -> bool:

        normalized_lines = [
            self._normalize_outline_line(
                line
            )
            for line
            in lines
        ]

        normalized_lines = [
            line
            for line
            in normalized_lines
            if line
            and not self._is_page_placeholder_title(
                line
            )
        ]

        if not normalized_lines:

            return False

        for line in (
            normalized_lines[
                :10
            ]
        ):

            if any(
                pattern.fullmatch(
                    line
                )
                is not None
                for pattern
                in self._TOC_PATTERNS
            ):

                return True

        # TOC continuation:
        # only in very early slides.
        if (
            slide_position
            > self.specification_toc_front_slide_limit
        ):

            return False

        heading_count = sum(
            1
            for line
            in normalized_lines
            if self._detect_spec_heading(
                line
            )
            is not None
        )

        heading_ratio = (
            heading_count
            / len(
                normalized_lines
            )
        )

        return (
            heading_count
            >= 10
            and heading_ratio
            >= 0.55
        )

    # ==================================================
    # Back Matter
    # ==================================================

    @classmethod
    def _find_change_history_title(
        cls,
        lines: list[str],
    ) -> str | None:

        for line in (
            lines[
                :12
            ]
        ):

            normalized = (
                cls._normalize_outline_line(
                    line
                )
            )

            if not normalized:
                continue

            if cls._is_page_placeholder_title(
                normalized
            ):

                continue

            if cls._CHANGE_HISTORY_PATTERN.fullmatch(
                normalized
            ):

                return (
                    normalized
                )

        return None

    # ==================================================
    # Specification Text Helpers
    # ==================================================

    @classmethod
    def _collect_spec_text_lines(
        cls,
        blocks: list[
            DocumentBlock
        ],
    ) -> list[str]:

        lines: list[
            str
        ] = []

        for block in (
            blocks
        ):

            if (
                block.block_type
                == BlockType.IMAGE
            ):

                continue

            text = (
                cls._normalize_text(
                    block.text
                )
            )

            if not text:
                continue

            lines.extend(
                text.splitlines()
            )

        return (
            lines
        )

    @classmethod
    def _remove_page_placeholder_lines(
        cls,
        text: str,
    ) -> str:

        if not text:
            return ""

        lines = [
            line
            for line
            in str(
                text
            ).splitlines()
            if not cls._is_page_placeholder_title(
                line
            )
        ]

        return "\n".join(
            lines
        ).strip()

    @classmethod
    def _format_spec_content_line(
        cls,
        *,
        block: DocumentBlock,
        text: str,
    ) -> str:

        normalized = (
            cls._normalize_text(
                text
            )
        )

        if not normalized:
            return ""

        if block.block_type == BlockType.LIST:

            level = int(
                block.metadata.get(
                    "paragraph_level",
                    block.level
                    or 1,
                )
                or 1
            )

            indentation = (
                "  "
                * max(
                    level - 1,
                    0,
                )
            )

            return (
                f"{indentation}- "
                f"{normalized}"
            )

        return (
            normalized
        )

    @classmethod
    def _titles_equivalent(
        cls,
        left: Any,
        right: Any,
    ) -> bool:

        left_normalized = (
            cls._normalize_outline_line(
                str(
                    left
                    or ""
                )
            ).casefold()
        )

        right_normalized = (
            cls._normalize_outline_line(
                str(
                    right
                    or ""
                )
            ).casefold()
        )

        if (
            not left_normalized
            or not right_normalized
        ):

            return False

        if (
            left_normalized
            == right_normalized
        ):

            return True

        # Minor punctuation/space variation.
        compact_left = re.sub(
            r"[\s/／:：._-]+",
            "",
            left_normalized,
        )

        compact_right = re.sub(
            r"[\s/／:：._-]+",
            "",
            right_normalized,
        )

        return (
            compact_left
            == compact_right
        )

    # ==================================================
    # Content conversion
    # ==================================================

    def _build_content_text(
        self,
        block: DocumentBlock,
    ) -> str:

        if (
            block.block_type
            not in self._CONTENT_BLOCK_TYPES
        ):

            return ""

        content_kind = str(
            block.metadata.get(
                "content_kind",
                "",
            )
        ).strip().lower()

        if (
            block.block_type
            == BlockType.IMAGE
            and not self.include_image_blocks
        ):

            return ""

        if (
            content_kind
            == "chart"
            and not self.include_chart_blocks
        ):

            return ""

        if (
            block.block_type
            == BlockType.TABLE
        ):

            return (
                self._format_table_row(
                    block
                )
            )

        if (
            block.block_type
            == BlockType.IMAGE
        ):

            return (
                self._format_image_block(
                    block
                )
            )

        if (
            content_kind
            == "chart"
        ):

            return (
                self._format_chart_block(
                    block
                )
            )

        text = (
            self._normalize_text(
                block.text
            )
        )

        if not text:
            return ""

        if (
            block.block_type
            == BlockType.LIST
        ):

            level = int(
                block.metadata.get(
                    "paragraph_level",
                    block.level
                    or 1,
                )
                or 1
            )

            indentation = (
                "  "
                * max(
                    level - 1,
                    0,
                )
            )

            return (
                f"{indentation}- "
                f"{text}"
            )

        return (
            text
        )

    def _format_table_row(
        self,
        block: DocumentBlock,
    ) -> str:

        cells = [
            self._normalize_text(
                cell
            )
            for cell
            in block.cells
        ]

        cells = (
            self._trim_empty_boundaries(
                cells
            )
        )

        if not any(
            cells
        ):

            return ""

        row_index = (
            block.row_index
        )

        is_header_candidate = bool(
            block.metadata.get(
                "is_header_candidate",
                False,
            )
        )

        if (
            is_header_candidate
            and not self.include_table_headers
        ):

            return ""

        row_text = (
            self.table_cell_separator
            .join(
                cells
            )
            .strip()
        )

        if not row_text:
            return ""

        if is_header_candidate:

            return (
                f"[Table header] "
                f"{row_text}"
            )

        if row_index is not None:

            return (
                f"[Table row "
                f"{row_index + 1}] "
                f"{row_text}"
            )

        return (
            row_text
        )

    @staticmethod
    def _format_image_block(
        block: DocumentBlock,
    ) -> str:

        text = (
            PPTXParser._normalize_text(
                block.text
            )
        )

        image_filename = (
            block.metadata.get(
                "image_filename"
            )
        )

        alt_text = (
            PPTXParser._normalize_text(
                block.metadata.get(
                    "alt_text",
                    "",
                )
            )
        )

        description = (
            alt_text
            or text
        )

        if description:

            return (
                f"[Image] "
                f"{description}"
            )

        if image_filename:

            return (
                f"[Image] "
                f"{image_filename}"
            )

        return ""

    @staticmethod
    def _format_chart_block(
        block: DocumentBlock,
    ) -> str:

        text = (
            PPTXParser._normalize_text(
                block.text
            )
        )

        metadata = (
            block.metadata
            or {}
        )

        title = (
            PPTXParser._normalize_text(
                metadata.get(
                    "chart_title",
                    "",
                )
            )
        )

        series = (
            metadata.get(
                "chart_series",
                [],
            )
        )

        categories = (
            metadata.get(
                "chart_categories",
                [],
            )
        )

        parts: list[
            str
        ] = []

        if title:

            parts.append(
                f"Chart title: "
                f"{title}"
            )

        if text:

            parts.append(
                text
            )

        if categories:

            parts.append(
                "Categories: "
                + ", ".join(
                    str(
                        value
                    )
                    for value
                    in categories
                )
            )

        if isinstance(
            series,
            list,
        ):

            for item in (
                series
            ):

                if not isinstance(
                    item,
                    dict,
                ):

                    continue

                name = (
                    item.get(
                        "name"
                    )
                )

                values = (
                    item.get(
                        "values",
                        [],
                    )
                )

                if (
                    name
                    or values
                ):

                    parts.append(
                        f"Series "
                        f"{name or ''}: "
                        + ", ".join(
                            str(
                                value
                            )
                            for value
                            in values
                        )
                    )

        if not parts:

            return ""

        return (
            "[Chart]\n"
            + "\n".join(
                parts
            )
        )

    # ==================================================
    # Slide and Chapter
    # ==================================================

    def _build_chapter_id(
        self,
        *,
        slide_number: int,
        chapter_order: int,
    ) -> str:

        if self.use_original_slide_number_as_chapter_id:

            return str(
                slide_number
            )

        return str(
            chapter_order
        )

    def _resolve_chapter_title(
        self,
        *,
        slide_number: int,
        slide_record: dict[
            str,
            Any,
        ],
        title_block: (
            DocumentBlock
            | None
        ),
        blocks: list[
            DocumentBlock
        ],
    ) -> str:

        if title_block is not None:

            title = (
                self._normalize_title(
                    title_block.text
                )
            )

            if title:

                return (
                    title
                )

        metadata_title = (
            self._normalize_title(
                slide_record.get(
                    "title",
                    "",
                )
            )
        )

        if metadata_title:

            return (
                metadata_title
            )

        for block in (
            blocks
        ):

            text = (
                self._normalize_title(
                    block.text
                )
            )

            if text:

                return (
                    text
                )

        return (
            f"{self.default_slide_title_prefix} "
            f"{slide_number}"
        )

    @staticmethod
    def _find_slide_title_block(
        blocks: list[
            DocumentBlock
        ],
    ) -> (
        DocumentBlock
        | None
    ):

        for block in (
            blocks
        ):

            if (
                block.block_type
                == BlockType.HEADING
                and block.level
                == 1
            ):

                return (
                    block
                )

        return None

    def _normalize_title(
        self,
        value: Any,
    ) -> str:

        text = (
            self._normalize_text(
                value
            )
        )

        if (
            len(
                text
            )
            <= self.maximum_title_length
        ):

            return (
                text
            )

        return (
            text[
                :self.maximum_title_length
            ].rstrip()
            + "..."
        )

    # ==================================================
    # Secondary Headings
    # ==================================================

    @staticmethod
    def _is_secondary_heading(
        block: DocumentBlock,
    ) -> bool:

        if (
            block.block_type
            != BlockType.HEADING
        ):

            return False

        if (
            block.level
            is None
        ):

            return False

        return (
            block.level
            >= 2
        )

    # ==================================================
    # Group and Records
    # ==================================================

    @classmethod
    def _group_blocks_by_slide(
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

        for block in (
            blocks
        ):

            slide_index = (
                cls._resolve_slide_index(
                    block
                )
            )

            groups[
                slide_index
            ].append(
                block
            )

        return dict(
            groups
        )

    @staticmethod
    def _collect_slide_records(
        document: Document,
    ) -> dict[
        int,
        dict[
            str,
            Any,
        ],
    ]:

        raw_records = (
            document.metadata.get(
                "slides",
                [],
            )
        )

        records: dict[
            int,
            dict[
                str,
                Any,
            ],
        ] = {}

        if not isinstance(
            raw_records,
            list,
        ):

            return (
                records
            )

        for raw_record in (
            raw_records
        ):

            if not isinstance(
                raw_record,
                dict,
            ):

                continue

            slide_index = (
                raw_record.get(
                    "slide_index"
                )
            )

            if slide_index is None:

                continue

            records[
                int(
                    slide_index
                )
            ] = dict(
                raw_record
            )

        return (
            records
        )

    # ==================================================
    # Resolvers
    # ==================================================

    @staticmethod
    def _resolve_slide_index(
        block: DocumentBlock,
    ) -> int:

        metadata = (
            block.metadata
            or {}
        )

        slide_index = (
            metadata.get(
                "slide_index"
            )
        )

        if slide_index is not None:

            return int(
                slide_index
            )

        slide_number = (
            metadata.get(
                "slide_number"
            )
        )

        if slide_number is not None:

            return (
                int(
                    slide_number
                )
                - 1
            )

        if (
            block.page_number
            is not None
        ):

            return (
                int(
                    block.page_number
                )
                - 1
            )

        return 0

    @staticmethod
    def _resolve_slide_number(
        *,
        slide_index: int,
        slide_record: dict[
            str,
            Any,
        ],
        blocks: list[
            DocumentBlock
        ],
    ) -> int:

        record_number = (
            slide_record.get(
                "slide_number"
            )
        )

        if record_number is not None:

            return int(
                record_number
            )

        for block in (
            blocks
        ):

            value = (
                block.metadata.get(
                    "slide_number"
                )
            )

            if value is not None:

                return int(
                    value
                )

        return (
            slide_index
            + 1
        )

    @staticmethod
    def _resolve_page_number(
        blocks: list[
            DocumentBlock
        ],
    ) -> int | None:

        for block in (
            blocks
        ):

            if (
                block.page_number
                is not None
            ):

                return int(
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
        int,
        int,
    ]:

        metadata = (
            block.metadata
            or {}
        )

        slide_index = (
            cls._resolve_slide_index(
                block
            )
        )

        visual_index = int(
            metadata.get(
                "visual_index",
                0,
            )
            or 0
        )

        paragraph_or_row_index = int(
            metadata.get(
                "paragraph_index",
                metadata.get(
                    "table_row_index",
                    0,
                ),
            )
            or 0
        )

        return (
            slide_index,
            visual_index,
            paragraph_or_row_index,
            block.order,
        )

    # ==================================================
    # Text
    # ==================================================

    @staticmethod
    def _trim_empty_boundaries(
        values: list[
            str
        ],
    ) -> list[
        str
    ]:

        if not values:

            return []

        start = 0
        end = len(
            values
        )

        while (
            start
            < end
            and not values[
                start
            ]
        ):

            start += 1

        while (
            end
            > start
            and not values[
                end
                - 1
            ]
        ):

            end -= 1

        return (
            values[
                start:end
            ]
        )

    @staticmethod
    def _is_page_placeholder_title(
        value: Any,
    ) -> bool:

        normalized = (
            PPTXParser._normalize_outline_line(
                str(
                    value
                    or ""
                )
            )
        )

        return bool(
            PPTXParser
            ._PAGE_PLACEHOLDER_PATTERN
            .fullmatch(
                normalized
            )
        )

    @staticmethod
    def _normalize_outline_line(
        value: Any,
    ) -> str:

        if value is None:

            return ""

        normalized = (
            unicodedata.normalize(
                "NFKC",
                str(
                    value
                ),
            )
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

        return (
            " ".join(
                normalized.split()
            ).strip()
        )

    @staticmethod
    def _normalize_text(
        value: Any,
    ) -> str:

        if value is None:

            return ""

        normalized = str(
            value
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

        lines = [
            " ".join(
                line.split()
            )
            for line
            in normalized.splitlines()
            if line.strip()
        ]

        return (
            "\n".join(
                lines
            ).strip()
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
                "PPTXParser expects an "
                "app.model.document.Document instance."
            )

        if (
            str(
                document.file_type
                or ""
            ).strip().lower()
            != "pptx"
        ):

            raise ValueError(
                "PPTXParser only accepts PPTX documents. "
                f"Received file_type: "
                f"{document.file_type}"
            )

        # Content availability is handled by ImportResiliencePolicy in parse().
        return None
