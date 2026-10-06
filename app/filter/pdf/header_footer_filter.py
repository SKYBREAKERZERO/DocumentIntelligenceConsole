from __future__ import annotations

import math
import re
import unicodedata

from collections import Counter

from app.model.document import Document


class HeaderFooterFilter:
    """
    PDF 页眉页脚过滤器。

    负责：
        - 扫描页面顶部和底部候选区域
        - 检测跨页高频重复页眉
        - 检测跨页高频重复页脚
        - 删除动态页码
        - 删除版本号
        - 删除常见静态版权 / Confidential 页脚
        - 保留正文区域文本
        - 保持 PDF 原始 Page Number

    示例：

        TOYOTA MOTOR CORPORATION
        DENSO CORPORATION
        CONFIDENTIAL
        v2.51
        Ver. 2.51
        41/48
        Page 41 of 48
        - 41 -

    设计原则：

        1. 只在页面边缘区域进行页眉页脚判断。
        2. 正文区域即使出现相同文本，也不删除。
        3. 高频重复检测按“出现过该文本的页面数”统计，
           而不是按同一页出现次数统计。
        4. Header 中不会把纯数字直接当页码，
           避免误删章节编号。
        5. Footer 中允许将纯数字识别为页码。
    """

    # ==================================================
    # Page Number
    # ==================================================

    _PAGE_NUMBER_PATTERNS = (
        # 41/48
        re.compile(
            r"^\d+\s*/\s*\d+$"
        ),

        # Page 41
        # Page 41 of 48
        re.compile(
            r"^page\s+\d+"
            r"(?:\s+of\s+\d+)?$",
            re.IGNORECASE,
        ),

        # - 41 -
        re.compile(
            r"^-\s*\d+\s*-$"
        ),
    )

    # Footer 区域额外允许：
    #
    # 41
    #
    # Header 不使用，
    # 避免误删：
    #
    # 1
    # Introduction
    _PLAIN_PAGE_NUMBER_PATTERN = re.compile(
        r"^\d+$"
    )

    # ==================================================
    # Version
    # ==================================================

    _VERSION_PATTERNS = (
        # v2.51
        # V 2.51
        re.compile(
            r"^v\s*\d+(?:\.\d+)+$",
            re.IGNORECASE,
        ),

        # Ver 2.51
        # Ver. 2.51
        # Version 2.51
        # Version: 2.51
        re.compile(
            r"^(?:ver|version)"
            r"\.?\s*:?\s*"
            r"\d+(?:\.\d+)+$",
            re.IGNORECASE,
        ),

        # Rev 2
        # Rev. 2.1
        # Revision 2.1
        re.compile(
            r"^(?:rev|revision)"
            r"\.?\s*:?\s*"
            r"\d+(?:\.\d+)*$",
            re.IGNORECASE,
        ),
    )

    # ==================================================
    # Static Footer
    # ==================================================

    _STATIC_FOOTER_PATTERNS = (
        # 保持现有项目兼容性
        re.compile(
            r"^TOYOTA\s+MOTOR\s+CORPORATION$",
            re.IGNORECASE,
        ),

        re.compile(
            r"^DENSO\s+CORPORATION$",
            re.IGNORECASE,
        ),

        # 通用企业文档 Footer
        re.compile(
            r"^CONFIDENTIAL$",
            re.IGNORECASE,
        ),

        re.compile(
            r"^PROPRIETARY$",
            re.IGNORECASE,
        ),

        re.compile(
            r"^INTERNAL\s+USE\s+ONLY$",
            re.IGNORECASE,
        ),

        re.compile(
            r"^FOR\s+INTERNAL\s+USE\s+ONLY$",
            re.IGNORECASE,
        ),

        re.compile(
            r"^ALL\s+RIGHTS\s+RESERVED\.?$",
            re.IGNORECASE,
        ),

        re.compile(
            r"^COPYRIGHT\b.*$",
            re.IGNORECASE,
        ),

        re.compile(
            r"^©.*$",
            re.IGNORECASE,
        ),
    )

    # ==================================================
    # Zero Width Characters
    # ==================================================

    _ZERO_WIDTH_CHARACTERS = (
        "\u200b",
        "\u200c",
        "\u200d",
        "\u2060",
        "\ufeff",
    )

    # ==================================================
    # Constructor
    # ==================================================

    def __init__(
        self,
        *,
        header_scan_lines: int = 3,
        footer_scan_lines: int = 5,
        repetition_ratio: float = 0.4,
        minimum_repetition: int = 3,
        remove_page_numbers: bool = True,
        remove_version_lines: bool = True,
        remove_static_footers: bool = True,
    ) -> None:

        if header_scan_lines < 1:
            raise ValueError(
                "header_scan_lines "
                "must be at least 1."
            )

        if footer_scan_lines < 1:
            raise ValueError(
                "footer_scan_lines "
                "must be at least 1."
            )

        if not (
            0
            < repetition_ratio
            <= 1
        ):
            raise ValueError(
                "repetition_ratio must be "
                "between 0 and 1."
            )

        if minimum_repetition < 1:
            raise ValueError(
                "minimum_repetition "
                "must be at least 1."
            )

        self.header_scan_lines = (
            header_scan_lines
        )

        self.footer_scan_lines = (
            footer_scan_lines
        )

        self.repetition_ratio = (
            repetition_ratio
        )

        self.minimum_repetition = (
            minimum_repetition
        )

        self.remove_page_numbers = (
            remove_page_numbers
        )

        self.remove_version_lines = (
            remove_version_lines
        )

        self.remove_static_footers = (
            remove_static_footers
        )

    # ==================================================
    # Public API
    # ==================================================

    def filter(
        self,
        document: Document,
    ) -> Document:
        """
        删除 PDF 页眉和页脚噪声。

        Returns:
            修改后的同一个 Document。
        """

        self._validate_document(
            document
        )

        pages_lines = [
            self._extract_lines(
                page.text
            )
            for page in document.pages
        ]

        # ==============================================
        # No Pages
        # ==============================================

        if not pages_lines:

            self._update_metadata(
                document=document,
                repeated_headers=set(),
                repeated_footers=set(),
                removed_repeated_count=0,
                removed_page_number_count=0,
                removed_version_count=0,
                removed_static_footer_count=0,
            )

            return document

        # ==============================================
        # Detect Repeated Header / Footer
        # ==============================================

        repeated_headers = (
            self._find_repeated_edge_lines(
                pages_lines=pages_lines,
                edge="header",
                scan_lines=(
                    self.header_scan_lines
                ),
            )
        )

        repeated_footers = (
            self._find_repeated_edge_lines(
                pages_lines=pages_lines,
                edge="footer",
                scan_lines=(
                    self.footer_scan_lines
                ),
            )
        )

        # ==============================================
        # Statistics
        # ==============================================

        removed_repeated_count = 0

        removed_page_number_count = 0

        removed_version_count = 0

        removed_static_footer_count = 0

        # ==============================================
        # Filter Pages
        # ==============================================

        for page, lines in zip(
            document.pages,
            pages_lines,
        ):

            filtered_lines: list[str] = []

            line_count = len(
                lines
            )

            footer_start_index = max(
                line_count
                - self.footer_scan_lines,
                0,
            )

            for index, line in enumerate(
                lines
            ):

                normalized = (
                    self._normalize_line(
                        line
                    )
                )

                if not normalized:
                    continue

                # ======================================
                # Edge Position
                # ======================================

                is_header_area = (
                    index
                    < self.header_scan_lines
                )

                is_footer_area = (
                    index
                    >= footer_start_index
                )

                # ======================================
                # Repeated Header
                # ======================================

                if (
                    is_header_area
                    and normalized
                    in repeated_headers
                ):
                    removed_repeated_count += 1
                    continue

                # ======================================
                # Repeated Footer
                # ======================================

                if (
                    is_footer_area
                    and normalized
                    in repeated_footers
                ):
                    removed_repeated_count += 1
                    continue

                # ======================================
                # Page Number
                # ======================================
                #
                # Header:
                #
                #     Page 3
                #     3/48
                #     - 3 -
                #
                # 可以删除。
                #
                # 但是：
                #
                #     3
                #
                # 不直接删除，
                # 因为可能是 Chapter Number。
                #
                # Footer:
                #
                #     3
                #
                # 可以作为纯页码删除。

                if (
                    self.remove_page_numbers
                    and (
                        is_header_area
                        or is_footer_area
                    )
                    and self._is_page_number(
                        normalized,
                        allow_plain_number=(
                            is_footer_area
                        ),
                    )
                ):
                    removed_page_number_count += 1
                    continue

                # ======================================
                # Version
                # ======================================

                if (
                    self.remove_version_lines
                    and (
                        is_header_area
                        or is_footer_area
                    )
                    and self._is_version_line(
                        normalized
                    )
                ):
                    removed_version_count += 1
                    continue

                # ======================================
                # Static Footer
                # ======================================

                if (
                    self.remove_static_footers
                    and is_footer_area
                    and self._is_static_footer(
                        normalized
                    )
                ):
                    removed_static_footer_count += 1
                    continue

                # ======================================
                # Retain
                # ======================================

                filtered_lines.append(
                    line.strip()
                )

            page.text = "\n".join(
                filtered_lines
            ).strip()

        # ==============================================
        # Metadata
        # ==============================================

        self._update_metadata(
            document=document,
            repeated_headers=(
                repeated_headers
            ),
            repeated_footers=(
                repeated_footers
            ),
            removed_repeated_count=(
                removed_repeated_count
            ),
            removed_page_number_count=(
                removed_page_number_count
            ),
            removed_version_count=(
                removed_version_count
            ),
            removed_static_footer_count=(
                removed_static_footer_count
            ),
        )

        return document

    # ==================================================
    # Repeated Edge Detection
    # ==================================================

    def _find_repeated_edge_lines(
        self,
        *,
        pages_lines: list[list[str]],
        edge: str,
        scan_lines: int,
    ) -> set[str]:
        """
        统计跨页面重复出现的页眉 / 页脚文本。

        每个文本在同一页最多计数一次。

        Example:

            Page 1:
                CONFIDENTIAL
                CONFIDENTIAL

            Page 2:
                CONFIDENTIAL

        Counter:

            CONFIDENTIAL = 2

        而不是 3。
        """

        if edge not in {
            "header",
            "footer",
        }:
            raise ValueError(
                f"Unsupported edge: {edge}"
            )

        counter: Counter[str] = (
            Counter()
        )

        eligible_page_count = 0

        for lines in pages_lines:

            if not lines:
                continue

            eligible_page_count += 1

            if edge == "header":

                candidates = (
                    lines[
                        :scan_lines
                    ]
                )

            else:

                candidates = (
                    lines[
                        -scan_lines:
                    ]
                )

            page_unique_lines: set[str] = (
                set()
            )

            for line in candidates:

                normalized = (
                    self._normalize_line(
                        line
                    )
                )

                if normalized:
                    page_unique_lines.add(
                        normalized
                    )

            counter.update(
                page_unique_lines
            )

        # ==============================================
        # Not Enough Pages
        # ==============================================

        if (
            eligible_page_count
            < self.minimum_repetition
        ):
            return set()

        # ==============================================
        # Threshold
        # ==============================================
        #
        # 使用 ceil，不能 int()。
        #
        # 例如：
        #
        #     8 pages
        #     ratio = 0.4
        #
        #     8 * 0.4 = 3.2
        #
        # 必须至少出现 4 页才达到 40%。
        #
        # 旧代码 int(3.2) = 3，
        # 实际只有 37.5%。

        ratio_threshold = math.ceil(
            eligible_page_count
            * self.repetition_ratio
        )

        threshold = max(
            self.minimum_repetition,
            ratio_threshold,
        )

        return {
            line
            for line, count
            in counter.items()
            if count >= threshold
        }

    # ==================================================
    # Line Extraction
    # ==================================================

    @staticmethod
    def _extract_lines(
        text: str,
    ) -> list[str]:

        if not text:
            return []

        return [
            line.strip()
            for line
            in str(
                text
            ).splitlines()
            if line.strip()
        ]

    # ==================================================
    # Normalize
    # ==================================================

    @classmethod
    def _normalize_line(
        cls,
        text: str,
    ) -> str:
        """
        为页眉页脚比较生成标准化文本。

        注意：
            此结果仅用于比较和判断。

            最终保留正文时仍使用原始文本，
            不会因为本函数破坏正文格式。
        """

        if not text:
            return ""

        normalized = unicodedata.normalize(
            "NFKC",
            str(
                text
            ),
        )

        normalized = normalized.replace(
            "\u3000",
            " ",
        )

        normalized = normalized.replace(
            "\xa0",
            " ",
        )

        for character in (
            cls._ZERO_WIDTH_CHARACTERS
        ):
            normalized = (
                normalized.replace(
                    character,
                    "",
                )
            )

        return " ".join(
            normalized.split()
        ).strip()

    # ==================================================
    # Page Number
    # ==================================================

    @classmethod
    def _is_page_number(
        cls,
        line: str,
        *,
        allow_plain_number: bool = False,
    ) -> bool:

        normalized = cls._normalize_line(
            line
        )

        if not normalized:
            return False

        if any(
            pattern.fullmatch(
                normalized
            )
            for pattern
            in cls._PAGE_NUMBER_PATTERNS
        ):
            return True

        if (
            allow_plain_number
            and cls._PLAIN_PAGE_NUMBER_PATTERN.fullmatch(
                normalized
            )
        ):
            return True

        return False

    # ==================================================
    # Version
    # ==================================================

    @classmethod
    def _is_version_line(
        cls,
        line: str,
    ) -> bool:

        normalized = cls._normalize_line(
            line
        )

        if not normalized:
            return False

        return any(
            pattern.fullmatch(
                normalized
            )
            for pattern
            in cls._VERSION_PATTERNS
        )

    # ==================================================
    # Static Footer
    # ==================================================

    @classmethod
    def _is_static_footer(
        cls,
        line: str,
    ) -> bool:

        normalized = cls._normalize_line(
            line
        )

        if not normalized:
            return False

        return any(
            pattern.fullmatch(
                normalized
            )
            for pattern
            in cls._STATIC_FOOTER_PATTERNS
        )

    # ==================================================
    # Metadata
    # ==================================================

    @staticmethod
    def _update_metadata(
        *,
        document: Document,
        repeated_headers: set[str],
        repeated_footers: set[str],
        removed_repeated_count: int,
        removed_page_number_count: int,
        removed_version_count: int,
        removed_static_footer_count: int,
    ) -> None:

        document.metadata.update(
            {
                "header_footer_filter": (
                    "HeaderFooterFilter"
                ),

                "header_footer_filter_status": (
                    "SUCCESS"
                ),

                "repeated_header_count": len(
                    repeated_headers
                ),

                "repeated_footer_count": len(
                    repeated_footers
                ),

                "removed_repeated_edge_line_count": (
                    removed_repeated_count
                ),

                "removed_page_number_count": (
                    removed_page_number_count
                ),

                "removed_version_line_count": (
                    removed_version_count
                ),

                "removed_static_footer_count": (
                    removed_static_footer_count
                ),
            }
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
                "HeaderFooterFilter expects an "
                "app.model.document.Document instance."
            )

        file_type = str(
            document.file_type
            or ""
        ).strip().lower()

        if file_type != "pdf":
            raise ValueError(
                "HeaderFooterFilter only accepts "
                "PDF documents. "
                f"Received file_type: "
                f"{document.file_type}"
            )