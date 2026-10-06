from __future__ import annotations

import re

from app.model.document import Document


class ContentFilter:
    """
    企业级正文过滤器。

    负责：
        - 保留 Chapter 级正文
        - 保留 Section 级正文
        - 删除空正文
        - 删除没有 Chapter / Section 归属的孤立正文
        - 删除纯结构编号噪声
        - 删除纯标点 / 分隔符噪声
        - 输出详细过滤统计 Metadata

    典型需要删除的结构噪声：

        "1"
        "2.2"
        "4.6.5"
        "2.2."
        "-----"

    典型需要保留的内容：

        "1. Local user profile function"
        "HTTP 401 Unauthorized"
        "Version 2.2"
        "2.2 V"
        "1 Introduction"
        "2026/08/26"

    设计原则：
        1. 保守过滤。
        2. 只删除高置信度噪声。
        3. 不根据长度单独删除正文。
        4. 不删除包含实际文字语义的内容。
        5. 不修改 Chapter / Section 归属关系。
        6. 不负责 Chunk / Token / 排序。
    """

    # ==================================================
    # Structural Noise
    # ==================================================

    # 纯层级编号：
    #
    #   1
    #   2.2
    #   4.6.5
    #   2.2.
    #
    # 不匹配：
    #
    #   2.2 V
    #   Version 2.2
    #   1 Introduction
    #
    _PURE_STRUCTURE_NUMBER_PATTERN = re.compile(
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

    # 纯标点 / 装饰性分隔符。
    #
    # Example:
    #
    #   -----
    #   .....
    #   ====
    #   ****
    #
    _PURE_PUNCTUATION_PATTERN = re.compile(
        r"""
        ^
        [\s
         \-_=~*#·•・
         .,，。．
         :：;；
         /\\|
         ()（）\[\]［］
         {}｛｝
         <>＜＞
         +]+
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

    # ==================================================
    # Public API
    # ==================================================

    def filter(
        self,
        document: Document,
    ) -> Document:

        self._validate_document(
            document
        )

        filtered_contents = []

        removed_empty_count = 0
        removed_orphan_count = 0
        removed_structural_noise_count = 0
        removed_punctuation_noise_count = 0

        # ==============================================
        # Structure Index
        # ==============================================
        #
        # 用于判断：
        #
        #   text = "2.2"
        #
        # 是否只是文档结构编号，而不是业务正文。

        chapter_ids = {
            self._normalize_structure_id(
                chapter.id
            )
            for chapter
            in document.chapters
            if getattr(
                chapter,
                "id",
                None,
            )
        }

        section_ids = {
            self._normalize_structure_id(
                section.id
            )
            for section
            in document.sections
            if getattr(
                section,
                "id",
                None,
            )
        }

        all_structure_ids = (
            chapter_ids
            | section_ids
        )

        # ==============================================
        # Filter
        # ==============================================

        for content in (
            document.contents
        ):

            # ==========================================
            # Normalize Text
            # ==========================================

            text = self._normalize_text(
                getattr(
                    content,
                    "text",
                    "",
                )
            )

            # ==========================================
            # Remove Empty Content
            # ==========================================

            if not text:

                removed_empty_count += 1

                continue

            # ==========================================
            # Remove Orphan Content
            # ==========================================
            #
            # 允许：
            #
            #   chapter_id 有值
            #   section_id = None
            #
            # -> Chapter 级正文
            #
            # 允许：
            #
            #   section_id 有值
            #
            # -> Section 级正文
            #
            # 删除：
            #
            #   chapter_id = None
            #   section_id = None

            if (
                not content.chapter_id
                and not content.section_id
            ):

                removed_orphan_count += 1

                continue

            # ==========================================
            # Remove Pure Punctuation Noise
            # ==========================================

            if self._is_punctuation_noise(
                text
            ):

                removed_punctuation_noise_count += 1

                continue

            # ==========================================
            # Remove Structural Number Noise
            # ==========================================

            if self._is_structural_number_noise(
                text=text,

                chapter_id=(
                    content.chapter_id
                ),

                section_id=(
                    content.section_id
                ),

                all_structure_ids=(
                    all_structure_ids
                ),
            ):

                removed_structural_noise_count += 1

                continue

            # ==========================================
            # Keep
            # ==========================================

            content.text = (
                text
            )

            filtered_contents.append(
                content
            )

        # ==============================================
        # Replace Contents
        # ==============================================

        document.contents = (
            filtered_contents
        )

        # ==============================================
        # Metadata
        # ==============================================

        total_removed_count = (
            removed_empty_count
            + removed_orphan_count
            + removed_structural_noise_count
            + removed_punctuation_noise_count
        )

        document.metadata.update(
            {
                "content_filter": (
                    "ContentFilter"
                ),

                "content_filter_status": (
                    "SUCCESS"
                ),

                "content_filter_strategy": (
                    "conservative_structural_noise_filter"
                ),

                "content_filter_removed_empty": (
                    removed_empty_count
                ),

                "content_filter_removed_orphan": (
                    removed_orphan_count
                ),

                "content_filter_removed_structural_noise": (
                    removed_structural_noise_count
                ),

                "content_filter_removed_punctuation_noise": (
                    removed_punctuation_noise_count
                ),

                "content_filter_removed_total": (
                    total_removed_count
                ),

                "content_filter_retained_count": len(
                    filtered_contents
                ),
            }
        )

        return document

    # ==================================================
    # Structural Number Noise
    # ==================================================

    @classmethod
    def _is_structural_number_noise(
        cls,
        *,
        text: str,
        chapter_id: str | None,
        section_id: str | None,
        all_structure_ids: set[str],
    ) -> bool:
        """
        判断正文是否只是结构编号。

        高置信度删除条件：

            1. text 必须完全是纯数字层级形式
            2. 并且满足以下任一条件：

                A. text 正好是已存在的 Chapter / Section ID
                B. text 是当前 Section 的祖先结构 ID
                C. text 是当前 Chapter ID
                D. text 是极短的单个结构数字

        Example:

            section_id = 2.2.1.4
            text       = 2.2

        -> True

            section_id = 4.1.1
            text       = 1

        -> True

            text = "2.2 V"

        -> False
        """

        normalized_text = (
            cls._normalize_structure_id(
                text
            )
        )

        if not normalized_text:

            return False

        if not (
            cls._PURE_STRUCTURE_NUMBER_PATTERN.fullmatch(
                text.strip()
            )
        ):

            return False

        # 防止异常大数字被当成结构编号。
        if len(
            normalized_text
        ) > 32:

            return False

        # ==============================================
        # A. Existing Structure ID
        # ==============================================

        if (
            normalized_text
            in all_structure_ids
        ):

            return True

        # ==============================================
        # B. Ancestor of Current Section
        # ==============================================

        normalized_section_id = (
            cls._normalize_structure_id(
                section_id
            )
            if section_id
            else ""
        )

        if normalized_section_id:

            if (
                normalized_section_id
                == normalized_text
            ):

                return True

            if (
                normalized_section_id.startswith(
                    normalized_text
                    + "."
                )
            ):

                return True

        # ==============================================
        # C. Current Chapter ID
        # ==============================================

        normalized_chapter_id = (
            cls._normalize_structure_id(
                chapter_id
            )
            if chapter_id
            else ""
        )

        if (
            normalized_chapter_id
            and normalized_text
            == normalized_chapter_id
        ):

            return True

        # ==============================================
        # D. Extremely Short Single Structural Number
        # ==============================================
        #
        # 单独正文：
        #
        #   "1"
        #   "2"
        #
        # 在已经具有 Chapter / Section 归属的 RAG 文档中，
        # 基本没有检索价值。
        #
        # 但为了避免误删：
        #
        #   "10"
        #   "100"
        #
        # 这种可能有业务含义的纯数字，
        # 这里只处理单个数字字符。

        if re.fullmatch(
            r"[0-9]",
            normalized_text,
        ):

            return True

        return False

    # ==================================================
    # Punctuation Noise
    # ==================================================

    @classmethod
    def _is_punctuation_noise(
        cls,
        text: str,
    ) -> bool:

        if not text:

            return False

        if len(
            text
        ) > 64:

            return False

        return bool(
            cls._PURE_PUNCTUATION_PATTERN.fullmatch(
                text
            )
        )

    # ==================================================
    # Normalize Structure ID
    # ==================================================

    @classmethod
    def _normalize_structure_id(
        cls,
        value,
    ) -> str:

        if value is None:

            return ""

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

        normalized = re.sub(
            r"\.+",
            ".",
            normalized,
        )

        return (
            normalized
        )

    # ==================================================
    # Normalize Text
    # ==================================================

    @staticmethod
    def _normalize_text(
        value,
    ) -> str:
        """
        ContentFilter 不修改正文内部结构。

        这里只去除 Content 两端空白。
        """

        if value is None:

            return ""

        return str(
            value
        ).strip()

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
                "ContentFilter expects an "
                "app.model.document.Document instance."
            )
