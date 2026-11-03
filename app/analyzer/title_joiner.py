from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence


class TitleJoiner:
    """
    PDF 标题断行合并器。

    主要用于修复 PDF 文本抽取产生的标题断行。

    Example:

        2 デバイス
        Setting

    ->

        2 デバイス Setting

    Example:

        2.3 ユーザープロファイル
        管理機能

    ->

        2.3 ユーザープロファイル管理機能

    禁止错误合并：

        2 Playback Function
        2.1 Start Method

    -> 保持两行。

    设计原则：

        false negative
            优于
        false positive

    即：
        宁可少合并，也不要把正文错误并入标题。
    """

    # ==================================================
    # Patterns
    # ==================================================

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
        (?P<title>\S.*)
        $
        """,
        re.VERBOSE,
    )

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

    _TABLE_LIKE_PATTERN = re.compile(
        r"[|\t]"
    )

    _URL_PATTERN = re.compile(
        r"""
        (?:
            https?://
            |
            www\.
        )
        """,
        re.IGNORECASE
        | re.VERBOSE,
    )

    _ENGLISH_TITLE_FRAGMENT_PATTERN = re.compile(
        r"""
        ^
        [A-Za-z0-9]
        [A-Za-z0-9
        \s
        /&
        ()
        \[\]
        _+
        ,:'’
        \-]*
        $
        """,
        re.VERBOSE,
    )

    _SENTENCE_ENDINGS = (
        "。",
        "！",
        "？",
        "!",
        "?",
        ";",
        "；",
        "ます",
        "です",
        "でした",
        "である",
        "となる",
        "とする",
    )

    # 明确具有正文性质的表达。
    #
    # 不放：
    #   機能
    #   方法
    #   設定
    #
    # 因为这些本身经常出现在标题中。
    _BODY_MARKERS = (
        "本仕様書では",
        "本仕様では",
        "以下に示す",
        "以下の通り",
        "以下のとおり",
        "を参照する",
        "を参照してください",
        "について説明",
        "の場合は",
        "の場合、",
        "こととする",
        "必要がある",
        "shall ",
        "must ",
        "should ",
        "is defined as",
        "are defined as",
    )

    _ENGLISH_CONNECTOR_WORDS = {
        "and",
        "or",
        "of",
        "for",
        "to",
        "with",
        "using",
        "from",
        "between",
        "by",
        "in",
        "on",
        "as",
    }

    _INCOMPLETE_ENDINGS = (
        "/",
        "-",
        "–",
        "—",
        ":",
        "：",
        "(",
        "（",
        "[",
        "［",
        "&",
        "+",
    )

    # ==================================================
    # Constructor
    # ==================================================

    def __init__(
        self,
        *,
        max_heading_length: int = 160,
        max_continuation_length: int = 60,
        max_continuation_lines: int = 2,
        merge_cjk_fragments: bool = True,
        merge_english_fragments: bool = True,
    ) -> None:

        if max_heading_length <= 0:
            raise ValueError(
                "max_heading_length must "
                "be greater than 0."
            )

        if max_continuation_length <= 0:
            raise ValueError(
                "max_continuation_length must "
                "be greater than 0."
            )

        if max_continuation_lines < 1:
            raise ValueError(
                "max_continuation_lines must "
                "be at least 1."
            )

        self.max_heading_length = (
            max_heading_length
        )

        self.max_continuation_length = (
            max_continuation_length
        )

        self.max_continuation_lines = (
            max_continuation_lines
        )

        self.merge_cjk_fragments = (
            merge_cjk_fragments
        )

        self.merge_english_fragments = (
            merge_english_fragments
        )

    # ==================================================
    # Public API
    # ==================================================

    def join(
        self,
        lines: Sequence[str],
    ) -> list[str]:
        """
        合并可能被 PDF 抽取断开的标题。

        Args:
            lines:
                TitleNormalizer 输出的文本行。

        Returns:
            处理后的文本行。
        """

        if lines is None:
            raise ValueError(
                "lines cannot be None."
            )

        if isinstance(
            lines,
            str,
        ):
            raise TypeError(
                "TitleJoiner.join() expects "
                "a sequence of lines, "
                "not a single string."
            )

        normalized_lines = [
            self._normalize_line(
                line
            )
            for line in lines
        ]

        normalized_lines = [
            line
            for line in normalized_lines
            if line
        ]

        if not normalized_lines:
            return []

        result: list[str] = []

        index = 0

        while index < len(
            normalized_lines
        ):
            current = (
                normalized_lines[
                    index
                ]
            )

            # 非编号标题完全不碰。
            if not self.is_possible_title(
                current
            ):
                result.append(
                    current
                )

                index += 1
                continue

            merged_line_count = 0

            while (
                index + 1
                < len(normalized_lines)
                and merged_line_count
                < self.max_continuation_lines
            ):
                next_line = (
                    normalized_lines[
                        index + 1
                    ]
                )

                if not self.should_merge(
                    current,
                    next_line,
                ):
                    break

                current = (
                    self._join_text(
                        current,
                        next_line,
                    )
                )

                index += 1
                merged_line_count += 1

            result.append(
                current
            )

            index += 1

        return result

    # ==================================================
    # Heading Detection
    # ==================================================

    @classmethod
    def is_possible_title(
        cls,
        line: str,
    ) -> bool:
        """
        判断当前行是否为编号标题。

        Examples:

            1 Title
            1.1 Title
            1.1.2 Title
            １．１ Title
        """

        normalized = (
            cls._normalize_line(
                line
            )
        )

        if not normalized:
            return False

        return bool(
            cls._NUMBERED_HEADING_PATTERN.match(
                normalized
            )
        )

    @classmethod
    def is_heading_line(
        cls,
        line: str,
    ) -> bool:
        """
        判断下一行是否已经是完整的新标题。

        与旧实现不同：

        下面全部返回 True：

            2 Next Chapter
            2.1 Next Section
            2.1.1 Next Subsection
        """

        return cls.is_possible_title(
            line
        )

    # ==================================================
    # Merge Decision
    # ==================================================

    def should_merge(
        self,
        current: str,
        next_line: str,
    ) -> bool:
        """
        判断下一行是否属于当前编号标题。

        执行顺序：

            1. Hard Reject
            2. Strong Positive Signal
            3. Language-specific fragment detection

        保守策略：

            不能较高置信度确认时返回 False。
        """

        current = self._normalize_line(
            current
        )

        next_line = self._normalize_line(
            next_line
        )

        if not current:
            return False

        if not next_line:
            return False

        if not self.is_possible_title(
            current
        ):
            return False

        # ==============================================
        # Hard Reject
        # ==============================================

        # 下一行已经是新的 Chapter / Section。
        if self.is_heading_line(
            next_line
        ):
            return False

        # 页码。
        if self._is_page_number(
            next_line
        ):
            return False

        # Bullet/List 通常属于正文。
        if self._BULLET_PATTERN.match(
            next_line
        ):
            return False

        # 表格形式。
        if self._TABLE_LIKE_PATTERN.search(
            next_line
        ):
            return False

        # URL。
        if self._URL_PATTERN.search(
            next_line
        ):
            return False

        # 当前标题已经太长。
        if len(
            current
        ) >= self.max_heading_length:
            return False

        # 下一段太长，更可能是正文。
        if len(
            next_line
        ) > self.max_continuation_length:
            return False

        # 合并后也不能无限增长。
        if (
            len(current)
            + len(next_line)
            + 1
            > self.max_heading_length
        ):
            return False

        if self._looks_like_sentence(
            next_line
        ):
            return False

        if self._contains_body_marker(
            next_line
        ):
            return False

        # ==============================================
        # Strong Positive
        # ==============================================

        # 当前标题明显没有结束。
        if self._current_heading_is_incomplete(
            current
        ):
            return True

        # ==============================================
        # English continuation
        # ==============================================

        if (
            self.merge_english_fragments
            and self._looks_like_english_title_fragment(
                next_line
            )
        ):
            return True

        # ==============================================
        # CJK continuation
        # ==============================================

        if (
            self.merge_cjk_fragments
            and self._looks_like_cjk_title_fragment(
                next_line
            )
        ):
            return True

        return False

    # ==================================================
    # Positive Signals
    # ==================================================

    @classmethod
    def _current_heading_is_incomplete(
        cls,
        current: str,
    ) -> bool:

        stripped = current.rstrip()

        if stripped.endswith(
            cls._INCOMPLETE_ENDINGS
        ):
            return True

        words = (
            stripped
            .lower()
            .split()
        )

        if not words:
            return False

        return (
            words[-1]
            in cls._ENGLISH_CONNECTOR_WORDS
        )

    @classmethod
    def _looks_like_english_title_fragment(
        cls,
        text: str,
    ) -> bool:

        if not (
            cls._ENGLISH_TITLE_FRAGMENT_PATTERN.fullmatch(
                text
            )
        ):
            return False

        words = [
            word
            for word in text.split()
            if word
        ]

        if not words:
            return False

        # 超长英文句子不当作标题续行。
        if len(words) > 10:
            return False

        # 标题 fragment 通常不会以句号结束。
        if text.endswith(
            (
                ".",
                "!",
                "?",
                ";",
            )
        ):
            return False

        return True

    @classmethod
    def _looks_like_cjk_title_fragment(
        cls,
        text: str,
    ) -> bool:

        if not cls._contains_cjk(
            text
        ):
            return False

        # 日中标题续行通常较短。
        if len(text) > 24:
            return False

        # 多个逗号更像正文。
        punctuation_count = sum(
            text.count(
                punctuation
            )
            for punctuation in (
                "、",
                "，",
                ",",
                "；",
                ";",
            )
        )

        if punctuation_count >= 2:
            return False

        return True

    # ==================================================
    # Negative Signals
    # ==================================================

    @classmethod
    def _looks_like_sentence(
        cls,
        text: str,
    ) -> bool:

        if text.endswith(
            cls._SENTENCE_ENDINGS
        ):
            return True

        punctuation_count = sum(
            text.count(
                punctuation
            )
            for punctuation in (
                ",",
                "，",
                ";",
                "；",
            )
        )

        return punctuation_count >= 2

    @classmethod
    def _contains_body_marker(
        cls,
        text: str,
    ) -> bool:

        normalized = text.lower()

        return any(
            marker.lower()
            in normalized
            for marker
            in cls._BODY_MARKERS
        )

    @classmethod
    def _is_page_number(
        cls,
        text: str,
    ) -> bool:

        normalized = (
            cls._normalize_line(
                text
            )
        )

        return any(
            pattern.fullmatch(
                normalized
            )
            is not None
            for pattern
            in cls._PAGE_NUMBER_PATTERNS
        )

    # ==================================================
    # Join
    # ==================================================

    @staticmethod
    def _join_text(
        current: str,
        next_line: str,
    ) -> str:

        first = current.strip()
        second = next_line.strip()

        if not first:
            return second

        if not second:
            return first

        # 日文 / 中文连续标题：
        #
        # ユーザープロファイル
        # 管理機能
        #
        # ->
        #
        # ユーザープロファイル管理機能

        if (
            TitleJoiner._is_cjk_character(
                first[-1]
            )
            and TitleJoiner._is_cjk_character(
                second[0]
            )
        ):
            return (
                first
                + second
            )

        # 英文或混合标题需要空格。
        return (
            f"{first} {second}"
        )

    # ==================================================
    # Text Helpers
    # ==================================================

    @staticmethod
    def _normalize_line(
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
            "\u00a0",
            " ",
        )

        normalized = normalized.replace(
            "\u200b",
            "",
        )

        normalized = normalized.replace(
            "\ufeff",
            "",
        )

        return " ".join(
            normalized.split()
        )

    @staticmethod
    def _contains_cjk(
        text: str,
    ) -> bool:

        return any(
            TitleJoiner._is_cjk_character(
                character
            )
            for character in text
        )

    @staticmethod
    def _is_cjk_character(
        character: str,
    ) -> bool:

        if not character:
            return False

        code = ord(
            character
        )

        return (
            0x3040 <= code <= 0x30FF
            or 0x3400 <= code <= 0x4DBF
            or 0x4E00 <= code <= 0x9FFF
            or 0xF900 <= code <= 0xFAFF
        )