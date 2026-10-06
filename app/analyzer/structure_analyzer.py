from __future__ import annotations

import re

from app.analyzer.title_detector import (
    TitleDetector,
)
from app.analyzer.title_joiner import (
    TitleJoiner,
)
from app.analyzer.title_normalizer import (
    TitleNormalizer,
)
from app.model.chapter import Chapter
from app.model.content import Content
from app.model.document import Document
from app.model.section import Section


class StructureAnalyzerError(
    RuntimeError
):
    """
    文档结构分析异常。
    """


class StructureAnalyzer:
    """
    企业级编号式 PDF 文档结构分析器。

    数据流：

        Page
          ↓
        TitleNormalizer
          ↓
        Numeric-Safe Title Join
          ↓
        Composite Heading Split
          ↓
        TitleDetector
          ↓
        Chapter / Section / Content

    负责：

        - 标准化页面文本
        - 合并 PDF 抽取造成的普通标题断行
        - 防止纯数字表格 Cell 与下一行被误拼成 Heading
        - 检测 Chapter / Section
        - 识别局部编号结构：

              (1) 接続 connection
              1ACC OFF ⇒ ACC ON の接続操作

          并映射成：

              4.1
              4.1.1

        - 拆分被 PDF/TitleJoiner 合并的复合标题：

              4 状態遷移 State transition
              (1) 接続 connection
              1ACC OFF ...

        - 建立 Chapter、Section、Content
        - 根据 Section ID 修正 Chapter 归属
        - 在缺失 Chapter 标题时建立占位 Chapter
        - 保持正文 Page Number
        - 防止重复 analyze() 累积旧结果

    不负责：

        - PDF 文件读取
        - 页面过滤
        - 页眉页脚删除
        - Section parent_section_id 最终构建
        - Sort Order
        - Chunk
        - Token Count
        - JSON
        - PostgreSQL

    设计原则：

        1. 纯数字行优先视为正文/表格值，不视为 Heading。
        2. 已进入 Section 后，同 Chapter 的可疑重复一级标题不应
           随意清空 current_section。
        3. Chapter 标题更新采用保守质量判断，不再简单“越长越好”。
        4. 局部编号只在有明确 Chapter / local parent 上下文时生效，
           避免把普通表格数字误识别成 Section。
    """

    # ==================================================
    # Numeric Guard
    # ==================================================

    _PURE_NUMERIC_LINE_PATTERN = re.compile(
        r"""
        ^
        [0-9０-９]+
        (?:
            [\.．]
            [0-9０-９]+
        )*
        [\.．]?
        $
        """,
        re.VERBOSE,
    )

    # ==================================================
    # Explicit Chapter Prefix
    # ==================================================

    _EXPLICIT_CHAPTER_PREFIX_PATTERN = re.compile(
        r"""
        ^
        \s*
        (?P<id>[0-9]+)
        (?:
            [\.．、:：]
            \s*
            |
            \s+
        )
        (?P<title>\S.*)
        $
        """,
        re.VERBOSE,
    )

    # PDF/Word 常见紧凑章标题：
    #
    #   6.性能要件 Performance requirements
    #   6．性能要件
    #   6:性能要件
    #
    # 仅匹配“单一 Chapter 数字 + 分隔符 + 标题”。
    # 不匹配：
    #
    #   2.2.3 Playback volume
    #
    # 因为那属于标准 Section。
    _COMPACT_CHAPTER_PATTERN = re.compile(
        r"""
        ^
        \s*
        (?P<id>[0-9]+)
        [\.．、:：]
        (?P<title>
            [^\d\s]
            .*
        )
        $
        """,
        re.VERBOSE,
    )

    # ==================================================
    # Local Outline
    # ==================================================
    #
    # Example:
    #
    #   (1) 接続 connection
    #   （2）切断 disconnection

    _LOCAL_PARENT_PATTERN = re.compile(
        r"""
        ^
        \s*
        [\(（]
        (?P<number>[1-9][0-9]?)
        [\)）]
        \s*
        (?P<title>.+?)
        \s*
        $
        """,
        re.VERBOSE,
    )

    # Local child only applies when current_section was created
    # from _LOCAL_PARENT_PATTERN.
    #
    # Example:
    #
    #   1ACC OFF ⇒ ACC ON の接続操作
    #   2他モードへの切替
    #
    # It intentionally requires the number to be immediately followed
    # by a non-digit/non-punctuation textual character.

    _LOCAL_CHILD_PATTERN = re.compile(
        r"""
        ^
        \s*
        (?P<number>[1-9][0-9]?)
        (?P<title>
            (?:
                [A-Za-z]
                |
                [\u3040-\u30ff]
                |
                [\u3400-\u9fff]
            )
            .+
        )
        \s*
        $
        """,
        re.VERBOSE,
    )

    # Embedded "(1) ..." inside an explicit Chapter line.
    _EMBEDDED_LOCAL_PARENT_PATTERN = re.compile(
        r"""
        \s+
        (?=
            [\(（]
            [1-9][0-9]?
            [\)）]
        )
        """,
        re.VERBOSE,
    )

    # Embedded compact child after a local parent title.
    #
    # Example:
    #
    #   (1) 接続 connection 1ACC OFF ⇒ ACC ON の接続操作
    #
    # Split before:
    #
    #   1ACC OFF ...

    _EMBEDDED_LOCAL_CHILD_PATTERN = re.compile(
        r"""
        \s+
        (?=
            [1-9][0-9]?
            (?:
                [A-Za-z]
                |
                [\u3040-\u30ff]
                |
                [\u3400-\u9fff]
            )
        )
        """,
        re.VERBOSE,
    )

    # ==================================================
    # Title Quality
    # ==================================================

    _TABLE_SYMBOL_ONLY_PATTERN = re.compile(
        r"""
        ^
        [\s
         ○●◎△▲▽▼
         ×✕✖
         \-_=+*/\\|:;,.，。．
         ()（）\[\]［］
         {}｛｝
         <>＜＞
        ]+
        $
        """,
        re.VERBOSE,
    )

    _EMBEDDED_STRUCTURE_MARKER_PATTERN = re.compile(
        r"""
        (?:
            [\(（]
            [1-9][0-9]?
            [\)）]
            |
            \s
            [1-9][0-9]?
            (?=
                [A-Za-z]
                |
                [\u3040-\u30ff]
                |
                [\u3400-\u9fff]
            )
        )
        """,
        re.VERBOSE,
    )

    # ==================================================
    # Init
    # ==================================================

    def __init__(
        self,
        *,
        title_detector: TitleDetector | None = None,
        title_normalizer: TitleNormalizer | None = None,
        title_joiner: TitleJoiner | None = None,
    ) -> None:

        self.title_detector = (
            title_detector
            or TitleDetector()
        )

        self.title_normalizer = (
            title_normalizer
            or TitleNormalizer()
        )

        self.title_joiner = (
            title_joiner
            or TitleJoiner()
        )

    # ==================================================
    # Public API
    # ==================================================

    def analyze(
        self,
        document: Document,
    ) -> Document:
        """
        分析 Document.pages 并建立文档结构。
        """

        self._validate_document(
            document
        )

        # 幂等。
        document.chapters.clear()
        document.sections.clear()
        document.contents.clear()

        current_chapter: Chapter | None = None
        current_section: Section | None = None

        # 局部编号父 Section，例如：
        #
        #   4.3  ← (3) モード切替
        #
        # 其下可以连续出现：
        #
        #   1他モードから切替
        #   2他モードへの切替
        #
        # current_section 在识别第一个 child 后会变成 4.3.1，
        # 因此必须单独保存 local parent。
        current_local_parent_section: Section | None = None

        # 每个 Chapter 的编号体系模式。
        #
        # unknown:
        #     尚未确定。
        #
        # local:
        #     使用：
        #         (1) xxx
        #         1xxx
        #
        # standard:
        #     已出现标准 dotted Section：
        #         2.2.2 xxx
        #         2.3 xxx
        #
        # 一旦进入 standard，不允许再被正文中的：
        #
        #     (1) ...
        #     (2) ...
        #
        # 误切回 local outline。
        chapter_outline_mode: dict[str, str] = {}

        chapter_map: dict[
            str,
            Chapter,
        ] = {}

        section_map: dict[
            str,
            Section,
        ] = {}

        content_buffer: list[str] = []

        content_page_number: (
            int
            | None
        ) = None

        discarded_preamble_count = 0
        synthetic_chapter_count = 0

        detected_heading_count = 0
        detector_heading_count = 0
        local_outline_heading_count = 0

        rejected_chapter_heading_count = 0
        rejected_duplicate_chapter_heading_count = 0

        numeric_guard_line_count = 0
        numeric_safe_join_boundary_count = 0

        composite_heading_split_count = 0
        local_parent_section_count = 0
        local_child_section_count = 0

        conservative_title_update_count = 0

        split_chapter_restore_count = 0
        compact_chapter_normalization_count = 0
        rejected_implausible_chapter_transition_count = 0

        # ==============================================
        # Content Flush
        # ==============================================

        def flush_content() -> None:
            """
            将当前正文缓冲区写入 Document。

            page_number 使用正文开始页。
            """

            nonlocal content_buffer
            nonlocal content_page_number
            nonlocal discarded_preamble_count

            if not content_buffer:

                content_page_number = None

                return

            text = "\n".join(
                content_buffer
            ).strip()

            content_buffer = []

            page_number = (
                content_page_number
            )

            content_page_number = None

            if not text:
                return

            if current_chapter is None:

                discarded_preamble_count += 1

                return

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

                    text=(
                        text
                    ),

                    page_number=(
                        page_number
                    ),
                )
            )

        # ==============================================
        # Section Upsert
        # ==============================================

        def ensure_section(
            *,
            section_id: str,
            chapter_id: str,
            title: str,
            level: int,
            page_number: int | None,
            metadata: dict | None = None,
        ) -> Section:

            nonlocal conservative_title_update_count

            section = (
                section_map.get(
                    section_id
                )
            )

            if section is None:

                section = Section(
                    id=(
                        section_id
                    ),

                    chapter_id=(
                        chapter_id
                    ),

                    parent_section_id=None,

                    title_jp=(
                        title
                    ),

                    title_en=None,

                    level=(
                        level
                    ),

                    page_number=(
                        page_number
                    ),

                    metadata=(
                        dict(
                            metadata
                            or {}
                        )
                    ),
                )

                document.sections.append(
                    section
                )

                section_map[
                    section_id
                ] = section

                return (
                    section
                )

            section.chapter_id = (
                chapter_id
            )

            if (
                self._update_title_if_better(
                    existing=(
                        section
                    ),
                    title=(
                        title
                    ),
                )
            ):

                conservative_title_update_count += 1

            if (
                section.page_number
                is None
            ):

                section.page_number = (
                    page_number
                )

            if metadata:

                section.metadata.update(
                    metadata
                )

            return (
                section
            )

        # ==============================================
        # Main
        # ==============================================

        try:

            for page in (
                document.pages
            ):

                page_number = (
                    page.page_number
                )

                # ======================================
                # 1. Normalize
                # ======================================

                normalized_text = (
                    self.title_normalizer
                    .normalize(
                        page.text
                    )
                )

                if not normalized_text:
                    continue

                raw_lines = (
                    normalized_text.splitlines()
                )

                # ======================================
                # 2. Numeric-Safe Join
                # ======================================
                #
                # 不允许：
                #
                #   1
                #   device TRACK UP ...
                #
                # 被 TitleJoiner 拼成：
                #
                #   1 device TRACK UP ...
                #
                # 这正是 PDF 表格导致 false Chapter 的典型来源。

                (
                    lines,
                    join_boundary_count,
                ) = self._join_titles_numeric_safe(
                    raw_lines
                )

                numeric_safe_join_boundary_count += (
                    join_boundary_count
                )

                # ======================================
                # 3. Composite Split
                # ======================================

                logical_lines: list[
                    str
                ] = []

                for line in lines:

                    expanded = (
                        self._expand_composite_outline_line(
                            line
                        )
                    )

                    if len(
                        expanded
                    ) > 1:

                        composite_heading_split_count += (
                            len(
                                expanded
                            )
                            - 1
                        )

                    logical_lines.extend(
                        expanded
                    )

                # ======================================
                # 3.5 Restore Split Chapter Headings
                # ======================================
                #
                # Word -> PDF 常见：
                #
                #     2
                #     再生機能 Playback function
                #
                # Numeric-safe join 为了保护表格数字，故意不合并纯数字。
                # 这里用更强条件恢复真正 Chapter：
                #
                #   - 下一行必须是高质量标题
                #   - 或后续短窗口内出现同 Chapter 的 Section
                #   - 不恢复 "2" + "デバイス" 这类弱表格文本

                (
                    logical_lines,
                    restored_split_chapter_count,
                ) = self._restore_split_chapter_headings(
                    logical_lines,
                    current_chapter_id=(
                        current_chapter.id
                        if current_chapter
                        is not None
                        else None
                    ),
                )

                split_chapter_restore_count += (
                    restored_split_chapter_count
                )

                # ======================================
                # 4. Analyze
                # ======================================

                for (
                    line_index,
                    raw_line,
                ) in enumerate(
                    logical_lines
                ):

                    line = (
                        str(
                            raw_line
                            or ""
                        ).strip()
                    )

                    if not line:
                        continue

                    # ==================================
                    # Compact Chapter Normalization
                    # ==================================
                    #
                    # Example:
                    #
                    #   6.性能要件 Performance requirements
                    #
                    # ->
                    #
                    #   6 性能要件 Performance requirements
                    #
                    # 仅在 Chapter 跳转合理时转换，防止表格中的
                    # “69.xxx” 一类数字误变成 Chapter。

                    normalized_compact_chapter = (
                        self._normalize_compact_chapter_heading(
                            line=line,
                            current_chapter_id=(
                                current_chapter.id
                                if current_chapter
                                is not None
                                else None
                            ),
                            following_lines=(
                                logical_lines[
                                    line_index
                                    + 1:
                                    line_index
                                    + 16
                                ]
                            ),
                        )
                    )

                    if (
                        normalized_compact_chapter
                        != line
                    ):

                        line = (
                            normalized_compact_chapter
                        )

                        compact_chapter_normalization_count += 1

                    # ==================================
                    # Pure Numeric Guard
                    # ==================================
                    #
                    # 保留数字作为正文/表格值，
                    # 但不交给 TitleDetector。

                    if self._is_pure_numeric_line(
                        line
                    ):

                        numeric_guard_line_count += 1

                        if (
                            content_page_number
                            is None
                        ):

                            content_page_number = (
                                page_number
                            )

                        content_buffer.append(
                            line
                        )

                        continue

                    # ==================================
                    # Local Parent Heading
                    # ==================================

                    local_parent = (
                        self._detect_local_parent_heading(
                            line
                        )
                    )

                    if (
                        local_parent
                        is not None
                        and current_chapter
                        is not None
                        and chapter_outline_mode.get(
                            current_chapter.id,
                            "unknown",
                        )
                        != "standard"
                    ):

                        flush_content()

                        chapter_outline_mode[
                            current_chapter.id
                        ] = "local"

                        local_number = (
                            local_parent[
                                "number"
                            ]
                        )

                        local_title = (
                            local_parent[
                                "title"
                            ]
                        )

                        section_id = (
                            f"{current_chapter.id}."
                            f"{local_number}"
                        )

                        current_section = (
                            ensure_section(
                                section_id=(
                                    section_id
                                ),

                                chapter_id=(
                                    current_chapter.id
                                ),

                                title=(
                                    local_title
                                ),

                                level=2,

                                page_number=(
                                    page_number
                                ),

                                metadata={
                                    "source": (
                                        "pdf_local_outline"
                                    ),

                                    "local_outline_parent": (
                                        True
                                    ),

                                    "local_outline_number": (
                                        local_number
                                    ),
                                },
                            )
                        )

                        current_local_parent_section = (
                            current_section
                        )

                        detected_heading_count += 1
                        local_outline_heading_count += 1
                        local_parent_section_count += 1

                        continue

                    # ==================================
                    # Local Child Heading
                    # ==================================
                    #
                    # 只允许挂在明确 local parent 下，
                    # 防止普通表格：
                    #
                    #   1Apple
                    #   2Bluetooth
                    #
                    # 被误判。

                    local_child = (
                        self._detect_local_child_heading(
                            line
                        )
                    )

                    if (
                        local_child
                        is not None
                        and current_chapter
                        is not None
                        and chapter_outline_mode.get(
                            current_chapter.id,
                            "unknown",
                        )
                        == "local"
                        and current_local_parent_section
                        is not None
                        and current_local_parent_section.metadata.get(
                            "local_outline_parent"
                        )
                    ):

                        flush_content()

                        child_number = (
                            local_child[
                                "number"
                            ]
                        )

                        child_title = (
                            local_child[
                                "title"
                            ]
                        )

                        section_id = (
                            f"{current_local_parent_section.id}."
                            f"{child_number}"
                        )

                        current_section = (
                            ensure_section(
                                section_id=(
                                    section_id
                                ),

                                chapter_id=(
                                    current_chapter.id
                                ),

                                title=(
                                    child_title
                                ),

                                level=3,

                                page_number=(
                                    page_number
                                ),

                                metadata={
                                    "source": (
                                        "pdf_local_outline"
                                    ),

                                    "local_outline_child": (
                                        True
                                    ),

                                    "local_outline_number": (
                                        child_number
                                    ),
                                },
                            )
                        )

                        detected_heading_count += 1
                        local_outline_heading_count += 1
                        local_child_section_count += 1

                        continue

                    # ==================================
                    # Standard Detector
                    # ==================================

                    result = (
                        self.title_detector
                        .detect(
                            line
                        )
                    )

                    # ==================================
                    # Normal Content
                    # ==================================

                    if result is None:

                        if (
                            content_page_number
                            is None
                        ):

                            content_page_number = (
                                page_number
                            )

                        content_buffer.append(
                            line
                        )

                        continue

                    title_id = str(
                        result.get(
                            "id",
                            "",
                        )
                    ).strip()

                    title = str(
                        result.get(
                            "title",
                            "",
                        )
                    ).strip()

                    try:

                        level = int(
                            result.get(
                                "level",
                                0,
                            )
                        )

                    except (
                        TypeError,
                        ValueError,
                    ):

                        level = 0

                    if (
                        not title_id
                        or not title
                        or level < 1
                    ):

                        # 不把无效 detector 结果当 Heading，
                        # 原文继续作为 Content 保留。

                        if (
                            content_page_number
                            is None
                        ):

                            content_page_number = (
                                page_number
                            )

                        content_buffer.append(
                            line
                        )

                        continue

                    # ==================================
                    # Chapter Guard
                    # ==================================

                    if level == 1:

                        if not self._is_reliable_chapter_heading(
                            line=line,
                            title_id=(
                                title_id
                            ),
                            title=(
                                title
                            ),
                            following_lines=(
                                logical_lines[
                                    line_index
                                    + 1:
                                    line_index
                                    + 16
                                ]
                            ),
                            current_chapter_id=(
                                current_chapter.id
                                if current_chapter
                                is not None
                                else None
                            ),
                        ):

                            rejected_chapter_heading_count += 1

                            if (
                                title_id.isdigit()
                                and current_chapter
                                is not None
                                and current_chapter.id.isdigit()
                                and int(
                                    title_id
                                )
                                not in {
                                    int(
                                        current_chapter.id
                                    ),
                                    int(
                                        current_chapter.id
                                    )
                                    + 1,
                                }
                            ):

                                rejected_implausible_chapter_transition_count += 1

                            if (
                                content_page_number
                                is None
                            ):

                                content_page_number = (
                                    page_number
                                )

                            content_buffer.append(
                                line
                            )

                            continue

                        # 同一个 Chapter 已经进入 Section 后，
                        # 再次遇到同 ID 的一级标题必须非常谨慎。
                        #
                        # 常见误识别：
                        #
                        #   current = 1.4.2
                        #   table cell = 1
                        #   next line  = PLAY / TRACK ...
                        #
                        # numeric-safe join 已经挡掉大多数情况，
                        # 这里再做第二层保护。

                        if (
                            current_chapter
                            is not None
                            and current_section
                            is not None
                            and current_chapter.id
                            == title_id
                            and title_id
                            in chapter_map
                        ):

                            rejected_duplicate_chapter_heading_count += 1

                            if (
                                content_page_number
                                is None
                            ):

                                content_page_number = (
                                    page_number
                                )

                            content_buffer.append(
                                line
                            )

                            continue

                    # 标题已经通过验证，才真正 flush。
                    flush_content()

                    detected_heading_count += 1
                    detector_heading_count += 1

                    # ==================================
                    # Chapter
                    # ==================================

                    if level == 1:

                        chapter = (
                            chapter_map.get(
                                title_id
                            )
                        )

                        if chapter is None:

                            chapter = Chapter(
                                id=(
                                    title_id
                                ),

                                title_jp=(
                                    title
                                ),

                                title_en=None,

                                level=1,

                                page_number=(
                                    page_number
                                ),

                                metadata={
                                    "source": (
                                        "pdf_numbered_heading"
                                    ),

                                    "explicit_heading": (
                                        True
                                    ),
                                },
                            )

                            document.chapters.append(
                                chapter
                            )

                            chapter_map[
                                title_id
                            ] = chapter

                        else:

                            if chapter.metadata.get(
                                "synthetic"
                            ):

                                chapter.title_jp = (
                                    title
                                )

                                chapter.page_number = (
                                    page_number
                                )

                                chapter.metadata.pop(
                                    "synthetic",
                                    None,
                                )

                                chapter.metadata.pop(
                                    "synthetic_reason",
                                    None,
                                )

                                chapter.metadata.update(
                                    {
                                        "source": (
                                            "pdf_numbered_heading"
                                        ),

                                        "explicit_heading": (
                                            True
                                        ),
                                    }
                                )

                                conservative_title_update_count += 1

                            elif self._update_title_if_better(
                                existing=(
                                    chapter
                                ),
                                title=(
                                    title
                                ),
                            ):

                                conservative_title_update_count += 1

                        current_chapter = (
                            chapter
                        )

                        chapter_outline_mode.setdefault(
                            current_chapter.id,
                            "unknown",
                        )

                        current_section = (
                            None
                        )

                        current_local_parent_section = (
                            None
                        )

                        continue

                    # ==================================
                    # Standard Section
                    # ==================================

                    chapter_id = (
                        self._resolve_section_chapter_id(
                            section_id=(
                                title_id
                            ),

                            current_chapter=(
                                current_chapter
                            ),
                        )
                    )

                    if not chapter_id:

                        # 防御性：保留标题原文为 Content，
                        # 不静默丢弃。

                        if (
                            content_page_number
                            is None
                        ):

                            content_page_number = (
                                page_number
                            )

                        content_buffer.append(
                            line
                        )

                        continue

                    # ==================================
                    # Ensure Chapter
                    # ==================================

                    chapter = (
                        chapter_map.get(
                            chapter_id
                        )
                    )

                    if chapter is None:

                        chapter = (
                            self._create_synthetic_chapter(
                                chapter_id=(
                                    chapter_id
                                ),

                                page_number=(
                                    page_number
                                ),
                            )
                        )

                        document.chapters.append(
                            chapter
                        )

                        chapter_map[
                            chapter_id
                        ] = chapter

                        synthetic_chapter_count += 1

                    current_chapter = (
                        chapter
                    )

                    chapter_outline_mode[
                        chapter_id
                    ] = "standard"

                    current_local_parent_section = (
                        None
                    )

                    current_section = (
                        ensure_section(
                            section_id=(
                                title_id
                            ),

                            chapter_id=(
                                chapter_id
                            ),

                            title=(
                                title
                            ),

                            level=(
                                level
                            ),

                            page_number=(
                                page_number
                            ),

                            metadata={
                                "source": (
                                    "pdf_numbered_heading"
                                ),

                                "explicit_heading": (
                                    True
                                ),
                            },
                        )
                    )

                # ======================================
                # 5. Page Boundary
                # ======================================
                #
                # Page Number 是 citation 语义。
                # 每页末尾 flush，Chapter / Section 上下文继续。

                flush_content()

            flush_content()

        except StructureAnalyzerError:
            raise

        except Exception as exc:

            raise StructureAnalyzerError(
                "Failed to analyze document "
                f"structure for "
                f"'{document.file_name}': {exc}"
            ) from exc

        # ==============================================
        # Metadata
        # ==============================================

        document.metadata.update(
            {
                "structure_analyzer": (
                    "StructureAnalyzer"
                ),

                "structure_analyzer_status": (
                    "SUCCESS"
                ),

                "structure_analyzer_strategy": (
                    "numeric_safe_sequential_chapter_restore_with_compact_heading_v3"
                ),

                "detected_heading_count": (
                    detected_heading_count
                ),

                "detector_heading_count": (
                    detector_heading_count
                ),

                "local_outline_heading_count": (
                    local_outline_heading_count
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

                "synthetic_chapter_count": (
                    synthetic_chapter_count
                ),

                "discarded_preamble_count": (
                    discarded_preamble_count
                ),

                "structure_numeric_guard_line_count": (
                    numeric_guard_line_count
                ),

                "structure_numeric_safe_join_boundary_count": (
                    numeric_safe_join_boundary_count
                ),

                "structure_composite_heading_split_count": (
                    composite_heading_split_count
                ),

                "structure_rejected_chapter_heading_count": (
                    rejected_chapter_heading_count
                ),

                "structure_rejected_duplicate_chapter_heading_count": (
                    rejected_duplicate_chapter_heading_count
                ),

                "structure_local_parent_section_count": (
                    local_parent_section_count
                ),

                "structure_local_child_section_count": (
                    local_child_section_count
                ),

                "structure_conservative_title_update_count": (
                    conservative_title_update_count
                ),

                "structure_split_chapter_restore_count": (
                    split_chapter_restore_count
                ),

                "structure_compact_chapter_normalization_count": (
                    compact_chapter_normalization_count
                ),

                "structure_rejected_implausible_chapter_transition_count": (
                    rejected_implausible_chapter_transition_count
                ),

                "structure_standard_outline_chapter_count": sum(
                    1
                    for mode
                    in chapter_outline_mode.values()
                    if mode
                    == "standard"
                ),

                "structure_local_outline_chapter_count": sum(
                    1
                    for mode
                    in chapter_outline_mode.values()
                    if mode
                    == "local"
                ),

                "structure_unknown_outline_chapter_count": sum(
                    1
                    for mode
                    in chapter_outline_mode.values()
                    if mode
                    == "unknown"
                ),
            }
        )

        return (
            document
        )

    # ==================================================
    # Restore Split Chapter Headings
    # ==================================================

    @classmethod
    def _restore_split_chapter_headings(
        cls,
        lines: list[str],
        *,
        current_chapter_id: str | None = None,
    ) -> tuple[
        list[str],
        int,
    ]:
        """
        恢复 PDF 拆开的真正 Chapter：

            2
            再生機能 Playback function

        ->

            2 再生機能 Playback function

        关键安全约束：

            1. 如果已经有当前 Chapter，
               candidate 必须是：
                   - 当前 Chapter
                   - 当前 Chapter + 1
               或者：
                   - 后续出现同编号 dotted Section

            2. 因此下面这种 Absolute Volume 表格不会被恢复：

                   current chapter = 2
                   69
                   * ...車載機からVOL 値変更...

               因为：
                   69 != 2
                   69 != 3
                   后续也不存在 69.x Section

            3. 真正的：

                   current chapter = 1
                   2
                   再生機能 Playback function

               可以正常恢复。
        """

        if not lines:

            return (
                [],
                0,
            )

        restored: list[str] = []
        restored_count = 0

        index = 0

        while index < len(
            lines
        ):

            current = str(
                lines[
                    index
                ]
                or ""
            ).strip()

            if (
                cls._is_integer_only_line(
                    current
                )
                and index
                + 1
                < len(
                    lines
                )
            ):

                next_line = str(
                    lines[
                        index
                        + 1
                    ]
                    or ""
                ).strip()

                if cls._looks_like_split_chapter_title(
                    next_line
                ):

                    chapter_id = (
                        current.rstrip(
                            ".．"
                        )
                    )

                    following_lines = [
                        str(
                            item
                            or ""
                        ).strip()
                        for item
                        in lines[
                            index
                            + 2:
                            index
                            + 16
                        ]
                    ]

                    strong_title = (
                        cls._is_strong_chapter_title(
                            next_line
                        )
                    )

                    has_matching_section = (
                        cls._has_matching_section_ahead(
                            chapter_id=(
                                chapter_id
                            ),
                            lines=(
                                following_lines
                            ),
                        )
                    )

                    plausible_transition = (
                        cls._is_plausible_chapter_transition(
                            candidate_chapter_id=(
                                chapter_id
                            ),
                            current_chapter_id=(
                                current_chapter_id
                            ),
                            following_lines=(
                                following_lines
                            ),
                        )
                    )

                    if (
                        plausible_transition
                        and (
                            strong_title
                            or has_matching_section
                        )
                    ):

                        restored.append(
                            f"{chapter_id} "
                            f"{next_line}"
                        )

                        restored_count += 1
                        index += 2

                        continue

            restored.append(
                current
            )

            index += 1

        return (
            restored,
            restored_count,
        )

    @classmethod
    def _normalize_compact_chapter_heading(
        cls,
        *,
        line: str,
        current_chapter_id: str | None,
        following_lines: list[str],
    ) -> str:
        """
        将：

            6.性能要件 Performance requirements

        规范成：

            6 性能要件 Performance requirements

        仅在 Chapter 跳转合理时处理。

        不处理：

            2.2.3 Playback volume specification
        """

        normalized = str(
            line
            or ""
        ).strip()

        match = (
            cls._COMPACT_CHAPTER_PATTERN.fullmatch(
                normalized
            )
        )

        if match is None:

            return (
                normalized
            )

        chapter_id = (
            match.group(
                "id"
            ).strip()
        )

        title = (
            match.group(
                "title"
            ).strip()
        )

        if not cls._looks_like_meaningful_title(
            title
        ):

            return (
                normalized
            )

        if not cls._is_plausible_chapter_transition(
            candidate_chapter_id=(
                chapter_id
            ),
            current_chapter_id=(
                current_chapter_id
            ),
            following_lines=(
                following_lines
            ),
        ):

            return (
                normalized
            )

        return (
            f"{chapter_id} "
            f"{title}"
        )

    @classmethod
    def _is_plausible_chapter_transition(
        cls,
        *,
        candidate_chapter_id: str,
        current_chapter_id: str | None,
        following_lines: list[str] | None = None,
    ) -> bool:
        """
        Chapter 跳转合理性。

        允许：

            current=None -> 任意候选
            1 -> 1
            1 -> 2
            5 -> 6

        非连续跳号只有在后续出现同 Chapter 的 dotted Section 时允许：

            2 -> 4
            后面出现 4.1 / 4.2
                -> 可以接受

        拒绝：

            2 -> 69
            且后面没有 69.x
        """

        candidate = str(
            candidate_chapter_id
            or ""
        ).strip()

        current = str(
            current_chapter_id
            or ""
        ).strip()

        if not candidate.isdigit():

            return False

        if not current:

            return True

        if not current.isdigit():

            return bool(
                cls._has_matching_section_ahead(
                    chapter_id=(
                        candidate
                    ),
                    lines=(
                        following_lines
                        or []
                    ),
                )
            )

        candidate_number = int(
            candidate
        )

        current_number = int(
            current
        )

        if candidate_number in {
            current_number,
            current_number + 1,
        }:

            return True

        return cls._has_matching_section_ahead(
            chapter_id=(
                candidate
            ),
            lines=(
                following_lines
                or []
            ),
        )

    @classmethod
    def _looks_like_split_chapter_title(
        cls,
        title: str,
    ) -> bool:

        normalized = str(
            title
            or ""
        ).strip()

        if not cls._looks_like_meaningful_title(
            normalized
        ):

            return False

        if len(
            normalized
        ) > 120:

            return False

        if cls._PURE_NUMERIC_LINE_PATTERN.fullmatch(
            normalized
        ):

            return False

        # 已经自带编号的行不再跟前面的纯数字拼接。
        if re.match(
            r"^[0-9]+(?:\.[0-9]+)+",
            normalized,
        ):

            return False

        if cls._LOCAL_PARENT_PATTERN.fullmatch(
            normalized
        ):

            return False

        return True

    @classmethod
    def _is_strong_chapter_title(
        cls,
        title: str,
    ) -> bool:
        """
        强 Chapter title 信号。

        目标：
            "再生機能 Playback function" -> True
            "はじめに Introduction"       -> True
            "デバイス"                    -> False

        单一短词标题仍可通过后续同 Chapter Section 证据恢复。
        """

        normalized = str(
            title
            or ""
        ).strip()

        if not normalized:

            return False

        # 日文/中文 + ASCII alphabet 的双语标题。
        has_cjk = any(
            (
                "\u3040"
                <= ch
                <= "\u30ff"
            )
            or (
                "\u3400"
                <= ch
                <= "\u9fff"
            )
            for ch
            in normalized
        )

        has_ascii_alpha = bool(
            re.search(
                r"[A-Za-z]",
                normalized,
            )
        )

        if (
            has_cjk
            and has_ascii_alpha
        ):

            return True

        # 英文多词标题。
        ascii_words = re.findall(
            r"[A-Za-z][A-Za-z0-9_-]*",
            normalized,
        )

        if len(
            ascii_words
        ) >= 2:

            return True

        # 信息量较高的纯日文/中文标题。
        compact = re.sub(
            r"\s+",
            "",
            normalized,
        )

        if (
            has_cjk
            and len(
                compact
            )
            >= 8
        ):

            return True

        return False

    @classmethod
    def _has_matching_section_ahead(
        cls,
        *,
        chapter_id: str,
        lines: list[str],
    ) -> bool:

        prefix = (
            f"{chapter_id}."
        )

        for raw_line in lines:

            line = str(
                raw_line
                or ""
            ).strip()

            if not line:
                continue

            if line.startswith(
                prefix
            ):

                match = re.match(
                    r"""
                    ^
                    (?P<id>
                        [0-9]+
                        (?:\.[0-9]+)+
                    )
                    (?:[\.．、:：]\s*|\s+)
                    \S
                    """,
                    line,
                    re.VERBOSE,
                )

                if (
                    match is not None
                    and match.group(
                        "id"
                    ).split(
                        ".",
                        maxsplit=1,
                    )[0]
                    == chapter_id
                ):

                    return True

        return False

    @classmethod
    def _is_integer_only_line(
        cls,
        line: str,
    ) -> bool:

        normalized = str(
            line
            or ""
        ).strip()

        return bool(
            re.fullmatch(
                r"[0-9０-９]+[\.．]?",
                normalized,
            )
        )

    # ==================================================
    # Numeric-Safe Title Join
    # ==================================================

    def _join_titles_numeric_safe(
        self,
        lines: list[str],
    ) -> tuple[
        list[str],
        int,
    ]:
        """
        对非纯数字区间调用 TitleJoiner。

        纯数字行作为 join boundary 单独保留。

        Example:

            1
            device TRACK UP
            1.1 Coverage

        旧行为可能得到：

            1 device TRACK UP

        新行为：

            1
            device TRACK UP
            1.1 Coverage
        """

        result: list[str] = []

        buffer: list[str] = []

        boundary_count = 0

        def flush_buffer() -> None:

            nonlocal buffer

            if not buffer:
                return

            joined = (
                self.title_joiner.join(
                    buffer
                )
            )

            result.extend(
                joined
            )

            buffer = []

        for raw_line in lines:

            line = str(
                raw_line
                or ""
            ).strip()

            if not line:
                continue

            if self._is_pure_numeric_line(
                line
            ):

                flush_buffer()

                result.append(
                    line
                )

                boundary_count += 1

                continue

            buffer.append(
                line
            )

        flush_buffer()

        return (
            result,
            boundary_count,
        )

    # ==================================================
    # Composite Heading Split
    # ==================================================

    @classmethod
    def _expand_composite_outline_line(
        cls,
        line: str,
    ) -> list[str]:
        """
        拆分被 PDF 提取/TitleJoiner 合并的复合结构。

        Example:

            4 状態遷移 State transition (1) 接続 connection 1ACC OFF ...

        ->

            4 状態遷移 State transition
            (1) 接続 connection
            1ACC OFF ...
        """

        normalized = str(
            line
            or ""
        ).strip()

        if not normalized:

            return []

        parts: list[str] = []

        # ==============================================
        # A. Chapter + Local Parent
        # ==============================================

        chapter_match = (
            cls._EXPLICIT_CHAPTER_PREFIX_PATTERN.fullmatch(
                normalized
            )
        )

        if chapter_match is not None:

            embedded_match = (
                cls._EMBEDDED_LOCAL_PARENT_PATTERN.search(
                    normalized
                )
            )

            if embedded_match is not None:

                split_index = (
                    embedded_match.end()
                )

                left = (
                    normalized[
                        :embedded_match.start()
                    ].strip()
                )

                right = (
                    normalized[
                        split_index:
                    ].strip()
                )

                if (
                    left
                    and right
                ):

                    parts.append(
                        left
                    )

                    normalized = (
                        right
                    )

        # ==============================================
        # B. Local Parent + Local Child
        # ==============================================

        if cls._LOCAL_PARENT_PATTERN.fullmatch(
            normalized
        ):

            child_match = (
                cls._EMBEDDED_LOCAL_CHILD_PATTERN.search(
                    normalized
                )
            )

            if child_match is not None:

                left = (
                    normalized[
                        :child_match.start()
                    ].strip()
                )

                right = (
                    normalized[
                        child_match.end():
                    ].strip()
                )

                if (
                    left
                    and right
                ):

                    parts.append(
                        left
                    )

                    parts.append(
                        right
                    )

                    return (
                        parts
                    )

        parts.append(
            normalized
        )

        return (
            parts
        )

    # ==================================================
    # Local Parent / Child
    # ==================================================

    @classmethod
    def _detect_local_parent_heading(
        cls,
        line: str,
    ) -> dict[str, str] | None:

        normalized = str(
            line
            or ""
        ).strip()

        match = (
            cls._LOCAL_PARENT_PATTERN.fullmatch(
                normalized
            )
        )

        if match is None:
            return None

        number = (
            match.group(
                "number"
            ).strip()
        )

        title = (
            match.group(
                "title"
            ).strip()
        )

        if not cls._looks_like_meaningful_title(
            title
        ):
            return None

        return {
            "number": (
                number
            ),
            "title": (
                title
            ),
        }

    @classmethod
    def _detect_local_child_heading(
        cls,
        line: str,
    ) -> dict[str, str] | None:

        normalized = str(
            line
            or ""
        ).strip()

        match = (
            cls._LOCAL_CHILD_PATTERN.fullmatch(
                normalized
            )
        )

        if match is None:
            return None

        number = (
            match.group(
                "number"
            ).strip()
        )

        title = (
            match.group(
                "title"
            ).strip()
        )

        if not cls._looks_like_meaningful_title(
            title
        ):
            return None

        return {
            "number": (
                number
            ),
            "title": (
                title
            ),
        }

    # ==================================================
    # Chapter Reliability
    # ==================================================

    @classmethod
    def _is_reliable_chapter_heading(
        cls,
        *,
        line: str,
        title_id: str,
        title: str,
        following_lines: list[str] | None = None,
        current_chapter_id: str | None = None,
    ) -> bool:
        """
        一级 Chapter 可靠性判断。

        除标题质量外，还要求 Chapter 跳转合理。

        这样可以同时保证：

            PASS:
                1 はじめに
                2 再生機能
                3 設定
                4 状態遷移
                5 その他
                6 性能要件

            REJECT:
                current chapter = 2
                69 * ...車載機からVOL...

        非连续 Chapter 仅在后续有同编号 Section 证据时允许。
        """

        normalized_id = str(
            title_id
            or ""
        ).strip()

        normalized_title = str(
            title
            or ""
        ).strip()

        normalized_line = str(
            line
            or ""
        ).strip()

        if not normalized_id.isdigit():

            return False

        if not cls._looks_like_meaningful_title(
            normalized_title
        ):

            return False

        if len(
            normalized_title
        ) > 180:

            return False

        match = (
            cls._EXPLICIT_CHAPTER_PREFIX_PATTERN.fullmatch(
                normalized_line
            )
        )

        if match is None:

            return False

        if (
            match.group(
                "id"
            ).strip()
            != normalized_id
        ):

            return False

        if cls._EMBEDDED_STRUCTURE_MARKER_PATTERN.search(
            normalized_title
        ):

            return False

        if not cls._is_plausible_chapter_transition(
            candidate_chapter_id=(
                normalized_id
            ),
            current_chapter_id=(
                current_chapter_id
            ),
            following_lines=(
                following_lines
                or []
            ),
        ):

            return False

        if cls._is_strong_chapter_title(
            normalized_title
        ):

            return True

        # 弱短标题仍需同 Chapter Section 证据。
        return cls._has_matching_section_ahead(
            chapter_id=(
                normalized_id
            ),
            lines=(
                following_lines
                or []
            ),
        )

    # ==================================================
    # Pure Numeric
    # ==================================================

    @classmethod
    def _is_pure_numeric_line(
        cls,
        line: str,
    ) -> bool:

        normalized = str(
            line
            or ""
        ).strip()

        return bool(
            cls._PURE_NUMERIC_LINE_PATTERN.fullmatch(
                normalized
            )
        )

    # ==================================================
    # Meaningful Title
    # ==================================================

    @classmethod
    def _looks_like_meaningful_title(
        cls,
        title: str,
    ) -> bool:

        normalized = str(
            title
            or ""
        ).strip()

        if not normalized:
            return False

        if cls._TABLE_SYMBOL_ONLY_PATTERN.fullmatch(
            normalized
        ):
            return False

        if len(
            normalized
        ) > 240:
            return False

        return any(
            character.isalpha()
            or (
                "\u3040"
                <= character
                <= "\u30ff"
            )
            or (
                "\u3400"
                <= character
                <= "\u9fff"
            )
            for character
            in normalized
        )

    # ==================================================
    # Chapter Resolution
    # ==================================================

    @staticmethod
    def _resolve_section_chapter_id(
        *,
        section_id: str,
        current_chapter: Chapter | None,
    ) -> str | None:
        """
        Section ID 第一段优先。
        """

        normalized_id = str(
            section_id
            or ""
        ).strip()

        if not normalized_id:

            return (
                current_chapter.id
                if current_chapter
                else None
            )

        if "." in normalized_id:

            chapter_id = (
                normalized_id
                .split(
                    ".",
                    maxsplit=1,
                )[0]
                .strip()
            )

            if chapter_id:
                return (
                    chapter_id
                )

        if current_chapter is not None:

            return (
                current_chapter.id
            )

        return None

    # ==================================================
    # Synthetic Chapter
    # ==================================================

    @staticmethod
    def _create_synthetic_chapter(
        *,
        chapter_id: str,
        page_number: int | None,
    ) -> Chapter:
        """
        Section 出现但 Chapter 标题缺失时建立占位 Chapter。
        """

        return Chapter(
            id=(
                chapter_id
            ),

            title_jp=(
                f"Chapter {chapter_id}"
            ),

            title_en=None,

            level=1,

            page_number=(
                page_number
            ),

            metadata={
                "synthetic": (
                    True
                ),

                "synthetic_reason": (
                    "chapter_heading_missing"
                ),

                "source": (
                    "pdf_synthetic_chapter"
                ),
            },
        )

    # ==================================================
    # Conservative Title Update
    # ==================================================

    @classmethod
    def _update_title_if_better(
        cls,
        *,
        existing: Chapter | Section,
        title: str,
    ) -> bool:
        """
        重复标题时进行保守更新。

        不再使用：

            len(new) > len(old)

        这种规则。

        因为 PDF 表格误识别最容易产生“更长但更错”的标题。

        Returns:
            是否发生更新。
        """

        normalized_title = str(
            title
            or ""
        ).strip()

        if not cls._looks_like_meaningful_title(
            normalized_title
        ):

            return False

        existing_title = str(
            existing.title_jp
            or ""
        ).strip()

        if not existing_title:

            existing.title_jp = (
                normalized_title
            )

            return True

        if (
            normalized_title
            == existing_title
        ):

            return False

        # synthetic placeholder 可以被真实标题替代。
        if (
            getattr(
                existing,
                "metadata",
                {},
            ).get(
                "synthetic"
            )
        ):

            existing.title_jp = (
                normalized_title
            )

            return True

        old_score = (
            cls._title_quality_score(
                existing_title
            )
        )

        new_score = (
            cls._title_quality_score(
                normalized_title
            )
        )

        # 必须显著更好才更新。
        if (
            new_score
            >= old_score
            + 2
        ):

            existing.title_jp = (
                normalized_title
            )

            return True

        return False

    @classmethod
    def _title_quality_score(
        cls,
        title: str,
    ) -> int:

        normalized = str(
            title
            or ""
        ).strip()

        if not normalized:
            return -100

        score = 0

        if cls._looks_like_meaningful_title(
            normalized
        ):
            score += 4

        length = len(
            normalized
        )

        if (
            3
            <= length
            <= 120
        ):
            score += 2

        elif length > 180:
            score -= 4

        if cls._EMBEDDED_STRUCTURE_MARKER_PATTERN.search(
            normalized
        ):

            score -= 4

        if cls._TABLE_SYMBOL_ONLY_PATTERN.fullmatch(
            normalized
        ):

            score -= 10

        # 一行里过多 "|" 往往来自表格串行化。
        if normalized.count(
            "|"
        ) >= 3:

            score -= 3

        return (
            score
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
                "StructureAnalyzer expects an "
                "app.model.document.Document instance."
            )

        if not str(
            document.file_name
            or ""
        ).strip():

            raise ValueError(
                "Document file_name cannot be empty."
            )

        if (
            str(
                document.file_type
                or ""
            ).strip().lower()
            != "pdf"
        ):

            raise ValueError(
                "StructureAnalyzer only accepts "
                "PDF documents. "
                f"Received file_type: "
                f"{document.file_type}"
            )

        if not document.pages:

            raise ValueError(
                "PDF document contains no pages."
            )

        if not any(
            str(
                page.text
                or ""
            ).strip()
            for page
            in document.pages
        ):

            raise ValueError(
                "PDF document contains no analyzable text."
            )
