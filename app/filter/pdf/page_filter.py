from __future__ import annotations

import re
import unicodedata

from app.model.document import Document
from app.model.page import Page


class PageFilter:
    """
    企业级 PDF 页面过滤器。

    负责：
        - 删除空页面
        - 保守识别并删除目录页
        - 识别“页码被 PDF 文本层拆成独立行”的目录
        - 识别无 Contents/目次 标题的前置高密度目录页
        - 保持原始 page_number 不变
        - 输出 TOC 识别诊断 Metadata

    不负责：
        - 页眉页脚过滤
        - Chapter / Section 解析
        - 标题合并
        - Chunk
        - Token Count

    TOC 判断优先级：

        1. 显式 Contents / Table of Contents / 目次
        2. 点线目录：
               1 Introduction ........ 3
        3. 同行页码：
               1 Introduction 3
        4. 分离页码：
               1 Introduction
               3
        5. 仅限文档前部的“高密度标题 + 低正文密度”目录

    设计原则：
        - 正文页优先保留。
        - 最后的 dense-heading fallback 只允许作用于文档前几页。
        - 不会仅因“标题很多”就删除任意正文页。
        - 不重新编号 Page。
    """

    # ==================================================
    # Numbered Heading
    # ==================================================

    _NUMBERED_TITLE_PATTERN = re.compile(
        r"""
        ^
        [0-9]+
        (?:
            \.[0-9]+
        )*
        \.?
        (?:\s+|$)
        """,
        re.VERBOSE,
    )

    _NUMBER_PREFIX_PATTERN = re.compile(
        r"""
        ^
        (?P<number>
            [0-9]+
            (?:
                \.[0-9]+
            )*
        )
        \.?
        (?:\s+|$)
        """,
        re.VERBOSE,
    )

    # ==================================================
    # TOC Markers
    # ==================================================

    _TOC_HEADING_PATTERNS = (
        re.compile(
            r"^contents?$",
            re.IGNORECASE,
        ),
        re.compile(
            r"^table\s+of\s+contents$",
            re.IGNORECASE,
        ),
        re.compile(
            r"^contents?\s*/\s*目次$",
            re.IGNORECASE,
        ),
        re.compile(
            r"^目次$"
        ),
        re.compile(
            r"^目录$"
        ),
        re.compile(
            r"^目録$"
        ),
    )

    # ==================================================
    # Dotted TOC Entry
    # ==================================================

    _TOC_DOTTED_ENTRY_PATTERN = re.compile(
        r"""
        ^
        .+?
        \s*
        [.…·・]{2,}
        \s*
        \d{1,4}
        $
        """,
        re.VERBOSE,
    )

    # ==================================================
    # Trailing Page Reference
    # ==================================================

    _TRAILING_PAGE_REFERENCE_PATTERN = re.compile(
        r"""
        ^
        .+?
        \s+
        \d{1,4}
        $
        """,
        re.VERBOSE,
    )

    # ==================================================
    # Page Number Only
    # ==================================================

    _PAGE_NUMBER_PATTERN = re.compile(
        r"^\d{1,4}$"
    )

    # ==================================================
    # Narrative Signals
    # ==================================================

    _SENTENCE_END_PATTERN = re.compile(
        r"""
        [
            。！？!?
        ]
        $
        """,
        re.VERBOSE,
    )

    _SENTENCE_LIKE_PATTERN = re.compile(
        r"""
        (?:
            [。！？!?]
            |
            \.\s*$
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
        remove_empty_pages: bool = True,
        remove_toc_pages: bool = True,
        minimum_toc_entries: int = 5,
        toc_entry_ratio: float = 0.45,
        front_matter_page_limit: int = 3,
        dense_heading_minimum: int = 10,
        dense_heading_ratio: float = 0.30,
        maximum_narrative_ratio: float = 0.25,
    ) -> None:

        if minimum_toc_entries < 1:
            raise ValueError(
                "minimum_toc_entries must be at least 1."
            )

        if not (
            0.0
            < toc_entry_ratio
            <= 1.0
        ):
            raise ValueError(
                "toc_entry_ratio must be between 0 and 1."
            )

        if front_matter_page_limit < 1:
            raise ValueError(
                "front_matter_page_limit must be at least 1."
            )

        if dense_heading_minimum < 2:
            raise ValueError(
                "dense_heading_minimum must be at least 2."
            )

        if not (
            0.0
            < dense_heading_ratio
            <= 1.0
        ):
            raise ValueError(
                "dense_heading_ratio must be between 0 and 1."
            )

        if not (
            0.0
            <= maximum_narrative_ratio
            <= 1.0
        ):
            raise ValueError(
                "maximum_narrative_ratio must be between 0 and 1."
            )

        self.remove_empty_pages = bool(
            remove_empty_pages
        )

        self.remove_toc_pages = bool(
            remove_toc_pages
        )

        self.minimum_toc_entries = int(
            minimum_toc_entries
        )

        self.toc_entry_ratio = float(
            toc_entry_ratio
        )

        self.front_matter_page_limit = int(
            front_matter_page_limit
        )

        self.dense_heading_minimum = int(
            dense_heading_minimum
        )

        self.dense_heading_ratio = float(
            dense_heading_ratio
        )

        self.maximum_narrative_ratio = float(
            maximum_narrative_ratio
        )

    # ==================================================
    # Public API
    # ==================================================

    def filter(
        self,
        document: Document,
    ) -> Document:
        """
        过滤 PDF 空页和目录页。

        非 PDF Document 保持兼容行为：
            原样返回。
        """

        self._validate_document(
            document
        )

        if (
            str(
                document.file_type
                or ""
            ).strip().lower()
            != "pdf"
        ):
            return document

        retained_pages: list[Page] = []

        removed_empty_count = 0
        removed_toc_count = 0

        removed_toc_page_numbers: list[int] = []
        toc_detection_details: list[dict[str, object]] = []

        original_page_count = len(
            document.pages
        )

        for (
            page_position,
            page,
        ) in enumerate(
            document.pages,
            start=1,
        ):

            text = str(
                page.text
                or ""
            ).strip()

            # ==========================================
            # Empty Page
            # ==========================================

            if not text:

                if self.remove_empty_pages:

                    removed_empty_count += 1

                    continue

                retained_pages.append(
                    page
                )

                continue

            lines = self._extract_lines(
                text
            )

            if not lines:

                if self.remove_empty_pages:

                    removed_empty_count += 1

                    continue

                retained_pages.append(
                    page
                )

                continue

            # ==========================================
            # TOC
            # ==========================================

            toc_result = (
                self._analyze_toc_page(
                    lines,
                    page_position=(
                        page_position
                    ),
                    original_page_count=(
                        original_page_count
                    ),
                )
            )

            if (
                self.remove_toc_pages
                and toc_result[
                    "is_toc"
                ]
            ):

                removed_toc_count += 1

                removed_toc_page_numbers.append(
                    int(
                        page.page_number
                    )
                )

                toc_detection_details.append(
                    {
                        "page_number": (
                            page.page_number
                        ),
                        **toc_result,
                    }
                )

                continue

            retained_pages.append(
                page
            )

        document.pages = (
            retained_pages
        )

        # ==============================================
        # Metadata
        # ==============================================

        document.metadata.update(
            {
                "page_filter": (
                    "PageFilter"
                ),

                "page_filter_status": (
                    "SUCCESS"
                ),

                "page_filter_strategy": (
                    "multi_signal_toc_numeric_table_safe_v2"
                ),

                "page_filter_original_count": (
                    original_page_count
                ),

                "page_filter_removed_empty": (
                    removed_empty_count
                ),

                "page_filter_removed_toc": (
                    removed_toc_count
                ),

                "page_filter_removed_toc_pages": (
                    removed_toc_page_numbers
                ),

                "page_filter_toc_detection_details": (
                    toc_detection_details
                ),

                "page_filter_retained_count": len(
                    retained_pages
                ),

                "page_filter_front_matter_page_limit": (
                    self.front_matter_page_limit
                ),

                "page_filter_dense_heading_minimum": (
                    self.dense_heading_minimum
                ),

                "page_filter_dense_heading_ratio": (
                    self.dense_heading_ratio
                ),

                "page_filter_numeric_only_heading_guard": (
                    True
                ),
            }
        )

        return document

    # ==================================================
    # Compatibility API
    # ==================================================

    def _looks_like_toc_page(
        self,
        lines: list[str],
        *,
        page_position: int | None = None,
        original_page_count: int | None = None,
    ) -> bool:
        """
        保留原 private API 行为兼容。

        当没有 page_position 时，
        只使用通用 TOC 信号，不启用前置页 fallback。
        """

        return bool(
            self._analyze_toc_page(
                lines,
                page_position=(
                    page_position
                ),
                original_page_count=(
                    original_page_count
                ),
            )[
                "is_toc"
            ]
        )

    # ==================================================
    # TOC Detection
    # ==================================================

    def _analyze_toc_page(
        self,
        lines: list[str],
        *,
        page_position: int | None,
        original_page_count: int | None,
    ) -> dict[str, object]:

        empty_result = {
            "is_toc": False,
            "reason": None,
            "line_count": 0,
            "numbered_title_count": 0,
            "dotted_entry_count": 0,
            "inline_page_reference_count": 0,
            "standalone_page_number_count": 0,
            "paired_page_reference_count": 0,
            "toc_candidate_count": 0,
            "candidate_ratio": 0.0,
            "numbered_heading_ratio": 0.0,
            "narrative_line_count": 0,
            "narrative_ratio": 0.0,
            "short_line_ratio": 0.0,
            "outline_depth_count": 0,
            "has_toc_heading": False,
        }

        if not lines:
            return empty_result

        normalized_lines = [
            self._normalize_line(
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
        ]

        if not normalized_lines:
            return empty_result

        has_toc_heading = any(
            self._is_toc_heading(
                line
            )
            for line
            in normalized_lines[
                :8
            ]
        )

        numbered_title_count = 0
        dotted_entry_count = 0
        inline_page_reference_count = 0
        standalone_page_number_count = 0
        toc_candidate_count = 0
        narrative_line_count = 0
        short_line_count = 0

        outline_depths: set[int] = set()

        for line in normalized_lines:

            is_standalone_page_number = (
                self._is_page_number_only(
                    line
                )
            )

            if is_standalone_page_number:
                standalone_page_number_count += 1

            is_numbered = (
                self.is_title(
                    line
                )
            )

            is_dotted = (
                self._looks_like_dotted_toc_entry(
                    line
                )
            )

            is_inline_page_reference = (
                not is_standalone_page_number
                and self._looks_like_page_reference(
                    line
                )
            )

            if is_numbered:

                numbered_title_count += 1

                depth = (
                    self._resolve_outline_depth(
                        line
                    )
                )

                if depth is not None:
                    outline_depths.add(
                        depth
                    )

            if is_dotted:
                dotted_entry_count += 1

            if is_inline_page_reference:
                inline_page_reference_count += 1

            if (
                is_dotted
                or (
                    is_numbered
                    and is_inline_page_reference
                )
            ):
                toc_candidate_count += 1

            if self._looks_like_narrative_line(
                line
            ):
                narrative_line_count += 1

            if len(
                line
            ) <= 80:
                short_line_count += 1

        paired_page_reference_count = (
            self._count_separated_page_references(
                normalized_lines
            )
        )

        # “标题行 + 独立页码”也属于强 TOC candidate。
        toc_candidate_count += (
            paired_page_reference_count
        )

        line_count = len(
            normalized_lines
        )

        candidate_ratio = (
            toc_candidate_count
            / line_count
            if line_count
            else 0.0
        )

        numbered_heading_ratio = (
            numbered_title_count
            / line_count
            if line_count
            else 0.0
        )

        narrative_ratio = (
            narrative_line_count
            / line_count
            if line_count
            else 0.0
        )

        short_line_ratio = (
            short_line_count
            / line_count
            if line_count
            else 0.0
        )

        # ==============================================
        # 1. Explicit TOC Heading
        # ==============================================

        if has_toc_heading:

            if (
                toc_candidate_count
                >= 3
            ):

                return self._toc_result(
                    reason=(
                        "explicit_toc_heading_with_entries"
                    ),
                    line_count=line_count,
                    numbered_title_count=(
                        numbered_title_count
                    ),
                    dotted_entry_count=(
                        dotted_entry_count
                    ),
                    inline_page_reference_count=(
                        inline_page_reference_count
                    ),
                    standalone_page_number_count=(
                        standalone_page_number_count
                    ),
                    paired_page_reference_count=(
                        paired_page_reference_count
                    ),
                    toc_candidate_count=(
                        toc_candidate_count
                    ),
                    candidate_ratio=(
                        candidate_ratio
                    ),
                    numbered_heading_ratio=(
                        numbered_heading_ratio
                    ),
                    narrative_line_count=(
                        narrative_line_count
                    ),
                    narrative_ratio=(
                        narrative_ratio
                    ),
                    short_line_ratio=(
                        short_line_ratio
                    ),
                    outline_depth_count=len(
                        outline_depths
                    ),
                    has_toc_heading=True,
                )

            if (
                numbered_title_count
                >= self.minimum_toc_entries
            ):

                return self._toc_result(
                    reason=(
                        "explicit_toc_heading_with_numbered_titles"
                    ),
                    line_count=line_count,
                    numbered_title_count=(
                        numbered_title_count
                    ),
                    dotted_entry_count=(
                        dotted_entry_count
                    ),
                    inline_page_reference_count=(
                        inline_page_reference_count
                    ),
                    standalone_page_number_count=(
                        standalone_page_number_count
                    ),
                    paired_page_reference_count=(
                        paired_page_reference_count
                    ),
                    toc_candidate_count=(
                        toc_candidate_count
                    ),
                    candidate_ratio=(
                        candidate_ratio
                    ),
                    numbered_heading_ratio=(
                        numbered_heading_ratio
                    ),
                    narrative_line_count=(
                        narrative_line_count
                    ),
                    narrative_ratio=(
                        narrative_ratio
                    ),
                    short_line_ratio=(
                        short_line_ratio
                    ),
                    outline_depth_count=len(
                        outline_depths
                    ),
                    has_toc_heading=True,
                )

        # ==============================================
        # 2. Strong Dotted / Inline TOC
        # ==============================================

        if (
            toc_candidate_count
            >= self.minimum_toc_entries
            and candidate_ratio
            >= self.toc_entry_ratio
            and (
                dotted_entry_count
                >= 2
                or inline_page_reference_count
                >= 4
                or paired_page_reference_count
                >= 3
            )
        ):

            return self._toc_result(
                reason=(
                    "high_toc_candidate_ratio"
                ),
                line_count=line_count,
                numbered_title_count=(
                    numbered_title_count
                ),
                dotted_entry_count=(
                    dotted_entry_count
                ),
                inline_page_reference_count=(
                    inline_page_reference_count
                ),
                standalone_page_number_count=(
                    standalone_page_number_count
                ),
                paired_page_reference_count=(
                    paired_page_reference_count
                ),
                toc_candidate_count=(
                    toc_candidate_count
                ),
                candidate_ratio=(
                    candidate_ratio
                ),
                numbered_heading_ratio=(
                    numbered_heading_ratio
                ),
                narrative_line_count=(
                    narrative_line_count
                ),
                narrative_ratio=(
                    narrative_ratio
                ),
                short_line_ratio=(
                    short_line_ratio
                ),
                outline_depth_count=len(
                    outline_depths
                ),
                has_toc_heading=(
                    has_toc_heading
                ),
            )

        # ==============================================
        # 3. Front Matter: Separated Page References
        # ==============================================

        is_front_matter = (
            page_position is not None
            and page_position
            <= self.front_matter_page_limit
        )

        if (
            is_front_matter
            and numbered_title_count
            >= self.minimum_toc_entries
            and paired_page_reference_count
            >= 3
            and narrative_ratio
            <= 0.35
        ):

            return self._toc_result(
                reason=(
                    "front_matter_separated_page_references"
                ),
                line_count=line_count,
                numbered_title_count=(
                    numbered_title_count
                ),
                dotted_entry_count=(
                    dotted_entry_count
                ),
                inline_page_reference_count=(
                    inline_page_reference_count
                ),
                standalone_page_number_count=(
                    standalone_page_number_count
                ),
                paired_page_reference_count=(
                    paired_page_reference_count
                ),
                toc_candidate_count=(
                    toc_candidate_count
                ),
                candidate_ratio=(
                    candidate_ratio
                ),
                numbered_heading_ratio=(
                    numbered_heading_ratio
                ),
                narrative_line_count=(
                    narrative_line_count
                ),
                narrative_ratio=(
                    narrative_ratio
                ),
                short_line_ratio=(
                    short_line_ratio
                ),
                outline_depth_count=len(
                    outline_depths
                ),
                has_toc_heading=(
                    has_toc_heading
                ),
            )

        # ==============================================
        # 4. Front Matter Dense Heading Fallback
        # ==============================================
        #
        # 解决 Word -> PDF 常见情况：
        #
        #   目录页在文本层中丢失点线和页码，
        #   只剩大量 1 / 1.1 / 1.1.1 标题。
        #
        # 为降低误删风险：
        #   - 只检查最前面的页面
        #   - 标题数量必须高
        #   - 标题比例必须高
        #   - 正文句子比例必须低
        #   - 大部分行必须是短行
        #   - 至少出现两个层级深度

        if (
            is_front_matter
            and numbered_title_count
            >= self.dense_heading_minimum
            and numbered_heading_ratio
            >= self.dense_heading_ratio
            and narrative_ratio
            <= self.maximum_narrative_ratio
            and short_line_ratio
            >= 0.65
            and len(
                outline_depths
            )
            >= 2
        ):

            return self._toc_result(
                reason=(
                    "front_matter_dense_numbered_headings"
                ),
                line_count=line_count,
                numbered_title_count=(
                    numbered_title_count
                ),
                dotted_entry_count=(
                    dotted_entry_count
                ),
                inline_page_reference_count=(
                    inline_page_reference_count
                ),
                standalone_page_number_count=(
                    standalone_page_number_count
                ),
                paired_page_reference_count=(
                    paired_page_reference_count
                ),
                toc_candidate_count=(
                    toc_candidate_count
                ),
                candidate_ratio=(
                    candidate_ratio
                ),
                numbered_heading_ratio=(
                    numbered_heading_ratio
                ),
                narrative_line_count=(
                    narrative_line_count
                ),
                narrative_ratio=(
                    narrative_ratio
                ),
                short_line_ratio=(
                    short_line_ratio
                ),
                outline_depth_count=len(
                    outline_depths
                ),
                has_toc_heading=(
                    has_toc_heading
                ),
            )

        # ==============================================
        # Retain
        # ==============================================

        return {
            "is_toc": False,
            "reason": None,
            "line_count": (
                line_count
            ),
            "numbered_title_count": (
                numbered_title_count
            ),
            "dotted_entry_count": (
                dotted_entry_count
            ),
            "inline_page_reference_count": (
                inline_page_reference_count
            ),
            "standalone_page_number_count": (
                standalone_page_number_count
            ),
            "paired_page_reference_count": (
                paired_page_reference_count
            ),
            "toc_candidate_count": (
                toc_candidate_count
            ),
            "candidate_ratio": round(
                candidate_ratio,
                4,
            ),
            "numbered_heading_ratio": round(
                numbered_heading_ratio,
                4,
            ),
            "narrative_line_count": (
                narrative_line_count
            ),
            "narrative_ratio": round(
                narrative_ratio,
                4,
            ),
            "short_line_ratio": round(
                short_line_ratio,
                4,
            ),
            "outline_depth_count": len(
                outline_depths
            ),
            "has_toc_heading": (
                has_toc_heading
            ),
        }

    # ==================================================
    # Result
    # ==================================================

    @staticmethod
    def _toc_result(
        *,
        reason: str,
        line_count: int,
        numbered_title_count: int,
        dotted_entry_count: int,
        inline_page_reference_count: int,
        standalone_page_number_count: int,
        paired_page_reference_count: int,
        toc_candidate_count: int,
        candidate_ratio: float,
        numbered_heading_ratio: float,
        narrative_line_count: int,
        narrative_ratio: float,
        short_line_ratio: float,
        outline_depth_count: int,
        has_toc_heading: bool,
    ) -> dict[str, object]:

        return {
            "is_toc": True,
            "reason": (
                reason
            ),
            "line_count": (
                line_count
            ),
            "numbered_title_count": (
                numbered_title_count
            ),
            "dotted_entry_count": (
                dotted_entry_count
            ),
            "inline_page_reference_count": (
                inline_page_reference_count
            ),
            "standalone_page_number_count": (
                standalone_page_number_count
            ),
            "paired_page_reference_count": (
                paired_page_reference_count
            ),
            "toc_candidate_count": (
                toc_candidate_count
            ),
            "candidate_ratio": round(
                candidate_ratio,
                4,
            ),
            "numbered_heading_ratio": round(
                numbered_heading_ratio,
                4,
            ),
            "narrative_line_count": (
                narrative_line_count
            ),
            "narrative_ratio": round(
                narrative_ratio,
                4,
            ),
            "short_line_ratio": round(
                short_line_ratio,
                4,
            ),
            "outline_depth_count": (
                outline_depth_count
            ),
            "has_toc_heading": (
                has_toc_heading
            ),
        }

    # ==================================================
    # Public Compatibility API
    # ==================================================

    @classmethod
    def is_title(
        cls,
        line: str,
    ) -> bool:
        """
        判断是否为编号标题。

        支持：

            1 Introduction
            1. Introduction
            1.2 Purpose
            1.2.3 Detail
        """

        normalized = cls._normalize_line(
            line
        )

        if not normalized:
            return False

        # 纯数字行绝不能作为 Heading。
        #
        # 这对 PDF 表格非常关键，例如 Absolute Volume 表：
        #
        #   0
        #   22
        #   35
        #   44
        #   70
        #
        # 这些是 Cell 值，不是：
        #
        #   Chapter 0
        #   Chapter 22
        #
        # 同时也避免 _count_separated_page_references() 把连续数字
        # 错误解释为 “标题 + 页码”。
        if cls._PAGE_NUMBER_PATTERN.fullmatch(
            normalized
        ):
            return False

        return bool(
            cls._NUMBERED_TITLE_PATTERN.match(
                normalized
            )
        )

    # ==================================================
    # Separated Page References
    # ==================================================

    @classmethod
    def _count_separated_page_references(
        cls,
        lines: list[str],
    ) -> int:
        """
        识别 PDF 文本层把目录页码拆成独立行的情况。

        Example:

            1 Introduction
            3

            1.1 Purpose
            5

        允许页码与标题之间存在一个很短的附加行，
        但不会跨很远进行匹配。
        """

        count = 0

        for index, line in enumerate(
            lines
        ):

            if not cls.is_title(
                line
            ):
                continue

            for offset in (
                1,
                2,
            ):

                target_index = (
                    index
                    + offset
                )

                if target_index >= len(
                    lines
                ):
                    break

                candidate = lines[
                    target_index
                ]

                if cls._is_page_number_only(
                    candidate
                ):

                    count += 1
                    break

                # 中间如果已经是另一个编号标题，
                # 不再跨过去寻找页码。
                if cls.is_title(
                    candidate
                ):
                    break

                # 只允许跨一个短行。
                if len(
                    candidate
                ) > 80:
                    break

        return count

    # ==================================================
    # Narrative
    # ==================================================

    @classmethod
    def _looks_like_narrative_line(
        cls,
        line: str,
    ) -> bool:

        normalized = cls._normalize_line(
            line
        )

        if not normalized:
            return False

        # 编号标题不作为正文句子。
        if cls.is_title(
            normalized
        ):
            return False

        if len(
            normalized
        ) >= 120:
            return True

        if (
            len(
                normalized
            )
            >= 45
            and cls._SENTENCE_LIKE_PATTERN.search(
                normalized
            )
        ):
            return True

        return False

    # ==================================================
    # Outline Depth
    # ==================================================

    @classmethod
    def _resolve_outline_depth(
        cls,
        line: str,
    ) -> int | None:

        normalized = cls._normalize_line(
            line
        )

        match = (
            cls._NUMBER_PREFIX_PATTERN.match(
                normalized
            )
        )

        if match is None:
            return None

        number = match.group(
            "number"
        )

        return (
            number.count(
                "."
            )
            + 1
        )

    # ==================================================
    # TOC Helpers
    # ==================================================

    @classmethod
    def _is_toc_heading(
        cls,
        line: str,
    ) -> bool:

        normalized = cls._normalize_line(
            line
        )

        return any(
            pattern.fullmatch(
                normalized
            )
            is not None
            for pattern
            in cls._TOC_HEADING_PATTERNS
        )

    @classmethod
    def _looks_like_dotted_toc_entry(
        cls,
        line: str,
    ) -> bool:

        normalized = cls._normalize_line(
            line
        )

        return bool(
            cls._TOC_DOTTED_ENTRY_PATTERN.fullmatch(
                normalized
            )
        )

    @classmethod
    def _looks_like_page_reference(
        cls,
        line: str,
    ) -> bool:

        normalized = cls._normalize_line(
            line
        )

        if not normalized:
            return False

        if cls._looks_like_dotted_toc_entry(
            normalized
        ):
            return True

        return bool(
            cls._TRAILING_PAGE_REFERENCE_PATTERN.fullmatch(
                normalized
            )
        )

    @classmethod
    def _is_page_number_only(
        cls,
        line: str,
    ) -> bool:

        normalized = cls._normalize_line(
            line
        )

        return bool(
            cls._PAGE_NUMBER_PATTERN.fullmatch(
                normalized
            )
        )

    # ==================================================
    # Lines
    # ==================================================

    @classmethod
    def _extract_lines(
        cls,
        text: str,
    ) -> list[str]:

        if not text:
            return []

        return [
            normalized
            for raw_line
            in str(
                text
            ).splitlines()
            if (
                normalized := cls._normalize_line(
                    raw_line
                )
            )
        ]

    # ==================================================
    # Normalize
    # ==================================================

    @staticmethod
    def _normalize_line(
        text: str,
    ) -> str:

        if not text:
            return ""

        normalized = (
            unicodedata.normalize(
                "NFKC",
                str(
                    text
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
                "PageFilter expects an "
                "app.model.document.Document instance."
            )
