from __future__ import annotations

import re
import unicodedata
from typing import TypedDict


class TitleDetectionResult(TypedDict):
    """
    标题检测结果。
    """

    id: str
    title: str
    level: int


class TitleDetector:
    """
    编号式标题检测器。

    支持：

        1 Introduction
        1.1 Purpose
        1.1.1 Detail Specification
        １．１ User Profile

    排除：

        2/48
        Page 12
        2018.12.20 Revision History
        2.9 Refer to 2.9 Now Playing
        明显正文句
        表格行
        URL
        Bullet/List

    职责：

        标准化文本
            ↓
        判断是否为编号标题
            ↓
        提取：
            id
            title
            level

    不负责：

        - 合并断行标题
        - 建立 Chapter / Section
        - 修正标题文本
        - 建立父子关系

    标题断行应由：

        TitleJoiner

    负责。
    """

    # ==================================================
    # Heading Pattern
    # ==================================================

    _TITLE_PATTERN = re.compile(
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
        (?P<title>
            \S.*
        )
        $
        """,
        re.VERBOSE,
    )

    # ==================================================
    # Page Numbers
    # ==================================================

    _PAGE_NUMBER_PATTERNS = (
        re.compile(
            r"^\d+\s*/\s*\d+$"
        ),
        re.compile(
            r"^page\s+\d+$",
            re.IGNORECASE,
        ),
        re.compile(
            r"^-\s*\d+\s*-$"
        ),
        re.compile(
            r"^\d+$"
        ),
    )

    # ==================================================
    # Date-like Heading ID
    # ==================================================

    _DATE_ID_PATTERN = re.compile(
        r"""
        ^
        (?:19|20)
        \d{2}
        \.
        \d{1,2}
        \.
        \d{1,2}
        $
        """,
        re.VERBOSE,
    )

    # ==================================================
    # Version / Revision Noise
    # ==================================================

    _VERSION_TITLE_PATTERN = re.compile(
        r"""
        ^
        (?:
            ver(?:sion)?
            |
            rev(?:ision)?
        )
        [\s._-]*
        \d
        """,
        re.IGNORECASE | re.VERBOSE,
    )

    # ==================================================
    # Invalid Body Expressions
    # ==================================================

    # 注意：
    #
    # 不使用：
    #   "参照"
    #   "以下"
    #   "場合"
    #
    # 这种过宽关键词。
    #
    # 因为：
    #
    #   参照方法
    #   例外の場合
    #
    # 有可能本身就是标题。
    #
    # 这里仅识别明显的正文表达。
    _BODY_MARKERS = (
        "を参照する",
        "を参照してください",
        "を参照下さい",
        "refer to ",
        "see section ",
        "see chapter ",
        "as described in ",
        "according to ",
        "以下に示す",
        "以下の通り",
        "以下のとおり",
        "の場合は",
        "の場合、",
        "に伴い、",
        "ものとする",
        "こととする",
        "必要がある",
        "shall ",
        "must ",
        "should ",
        "is defined as",
        "are defined as",
    )

    # ==================================================
    # Sentence Detection
    # ==================================================

    _STRONG_SENTENCE_ENDINGS = (
        "。",
        "！",
        "？",
        "!",
        "?",
        "；",
        ";",
        "です",
        "ます",
        "でした",
        "である",
        "となる",
        "とする",
        "ものとする",
    )

    # ==================================================
    # Structural Noise
    # ==================================================

    _BULLET_PATTERN = re.compile(
        r"""
        ^
        (?:
            [•●○■□◆◇・]
            |
            [-*+]
        )
        \s+
        """,
        re.VERBOSE,
    )

    _URL_PATTERN = re.compile(
        r"""
        (?:
            https?://
            |
            www\.
        )
        """,
        re.IGNORECASE | re.VERBOSE,
    )

    _TABLE_PATTERN = re.compile(
        r"[|\t]"
    )

    # ==================================================
    # Number Translation
    # ==================================================

    _NUMBER_TRANSLATION = str.maketrans(
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

    @classmethod
    def detect(
        cls,
        text: str,
    ) -> TitleDetectionResult | None:
        """
        检测编号标题。

        Returns:

            {
                "id": "2.3.1",
                "title": "User Profile",
                "level": 3,
            }

        无法确认是标题时返回 None。
        """

        normalized_text = (
            cls._normalize_text(
                text
            )
        )

        if not normalized_text:
            return None

        # ==============================================
        # Fast Reject
        # ==============================================

        if cls._is_page_number(
            normalized_text
        ):
            return None

        if cls._looks_like_table_row(
            normalized_text
        ):
            return None

        if cls._looks_like_url(
            normalized_text
        ):
            return None

        if cls._looks_like_bullet(
            normalized_text
        ):
            return None

        # ==============================================
        # Heading Pattern
        # ==============================================

        match = cls._TITLE_PATTERN.fullmatch(
            normalized_text
        )

        if match is None:
            return None

        raw_id = match.group(
            "number"
        )

        raw_title = match.group(
            "title"
        )

        heading_id = cls.normalize_number(
            raw_id
        )

        title = cls._normalize_text(
            raw_title
        )

        if not heading_id:
            return None

        if not title:
            return None

        # ==============================================
        # ID Validation
        # ==============================================

        if not cls._is_valid_heading_id(
            heading_id
        ):
            return None

        # ==============================================
        # Title Validation
        # ==============================================

        if not cls._is_valid_title(
            title
        ):
            return None

        level = (
            heading_id.count(".")
            + 1
        )

        return {
            "id": heading_id,
            "title": title,
            "level": level,
        }

    # ==================================================
    # Number Normalization
    # ==================================================

    @classmethod
    def normalize_number(
        cls,
        value: str,
    ) -> str:
        """
        标准化章节编号。

        Examples:

            １．２．３
                ->
            1.2.3

            1 . 2 . 3
                ->
            1.2.3
        """

        if value is None:
            return ""

        normalized = unicodedata.normalize(
            "NFKC",
            str(value),
        )

        normalized = normalized.translate(
            cls._NUMBER_TRANSLATION
        )

        normalized = re.sub(
            r"\s*\.\s*",
            ".",
            normalized,
        )

        normalized = normalized.strip(
            "."
        )

        return normalized

    # ==================================================
    # Heading ID Validation
    # ==================================================

    @classmethod
    def _is_valid_heading_id(
        cls,
        heading_id: str,
    ) -> bool:

        if not heading_id:
            return False

        # 日期：
        #
        # 2018.12.20 Revision History
        if cls._DATE_ID_PATTERN.fullmatch(
            heading_id
        ):
            return False

        parts = heading_id.split(
            "."
        )

        if not all(
            part.isdigit()
            for part in parts
        ):
            return False

        # 防止异常深度：
        #
        # 1.2.3.4.5.6.7.8...
        if len(parts) > 10:
            return False

        return True

    # ==================================================
    # Title Validation
    # ==================================================

    @classmethod
    def _is_valid_title(
        cls,
        title: str,
    ) -> bool:

        if not title:
            return False

        # 单字符标题仍可能合法，
        # 但纯符号不允许。
        if re.fullmatch(
            r"[\W_]+",
            title,
        ):
            return False

        # 防止超长正文被当标题。
        if len(title) > 160:
            return False

        if cls._VERSION_TITLE_PATTERN.match(
            title
        ):
            return False

        if cls._contains_body_marker(
            title
        ):
            return False

        if cls._looks_like_body_sentence(
            title
        ):
            return False

        return True

    # ==================================================
    # Text Normalization
    # ==================================================

    @classmethod
    def _normalize_text(
        cls,
        text: str,
    ) -> str:

        if text is None:
            return ""

        normalized = unicodedata.normalize(
            "NFKC",
            str(text),
        )

        normalized = normalized.replace(
            "\u3000",
            " ",
        )

        normalized = normalized.replace(
            "\xa0",
            " ",
        )

        normalized = normalized.replace(
            "\u200b",
            "",
        )

        normalized = normalized.replace(
            "\u200c",
            "",
        )

        normalized = normalized.replace(
            "\u200d",
            "",
        )

        normalized = normalized.replace(
            "\u2060",
            "",
        )

        normalized = normalized.replace(
            "\ufeff",
            "",
        )

        return " ".join(
            normalized.split()
        )

    # ==================================================
    # Noise Detection
    # ==================================================

    @classmethod
    def _is_page_number(
        cls,
        text: str,
    ) -> bool:

        return any(
            pattern.fullmatch(
                text
            )
            is not None
            for pattern
            in cls._PAGE_NUMBER_PATTERNS
        )

    @classmethod
    def _looks_like_table_row(
        cls,
        text: str,
    ) -> bool:

        return bool(
            cls._TABLE_PATTERN.search(
                text
            )
        )

    @classmethod
    def _looks_like_url(
        cls,
        text: str,
    ) -> bool:

        return bool(
            cls._URL_PATTERN.search(
                text
            )
        )

    @classmethod
    def _looks_like_bullet(
        cls,
        text: str,
    ) -> bool:

        return bool(
            cls._BULLET_PATTERN.match(
                text
            )
        )

    # ==================================================
    # Body Sentence Detection
    # ==================================================

    @classmethod
    def _contains_body_marker(
        cls,
        title: str,
    ) -> bool:

        normalized = title.lower()

        return any(
            marker.lower()
            in normalized
            for marker
            in cls._BODY_MARKERS
        )

    @classmethod
    def _looks_like_body_sentence(
        cls,
        title: str,
    ) -> bool:
        """
        检测明显正文。

        注意：

        不再简单：

            title.endswith(".")

        因为英文标题也有可能带句点。

        只有出现更强的正文信号时才拒绝。
        """

        if title.endswith(
            cls._STRONG_SENTENCE_ENDINGS
        ):
            return True

        punctuation_count = sum(
            title.count(
                mark
            )
            for mark in (
                ",",
                "，",
                ";",
                "；",
            )
        )

        if punctuation_count >= 2:
            return True

        # 很长 + 含英文句号，
        # 更可能是正文句。
        if (
            len(title) > 80
            and "." in title
        ):
            return True

        # 极长文本无论如何不作为标题。
        if len(title) > 120:
            return True

        return False