from __future__ import annotations

import re
import unicodedata


class TitleNormalizer:
    """
    PDF 结构分析前的文本标准化器。

    职责：
        - Unicode NFKC 标准化
        - 全角数字 / 英文 / 标点标准化
        - 清理不可见字符
        - 清理控制字符
        - 合并行内重复空格
        - 规范化章节编号中的点号和空格
        - 保留原始行边界

    不负责：
        - 判断 Chapter / Section
        - 合并断行标题
        - 推测正文是否属于标题
        - 建立章节层级

    标题断行合并统一交给：
        TitleJoiner
    """

    _CONTROL_CHARACTER_PATTERN = re.compile(
        r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]"
    )

    _MULTIPLE_HORIZONTAL_SPACES_PATTERN = re.compile(
        r"[ \t]+"
    )

    _NUMBER_DOT_SPACING_PATTERN = re.compile(
        r"(?<=\d)\s*\.\s*(?=\d)"
    )

    _LEADING_TRAILING_SPACES_PATTERN = re.compile(
        r"^[ \t]+|[ \t]+$"
    )

    _ZERO_WIDTH_CHARACTERS = (
        "\u200b",  # Zero Width Space
        "\u200c",  # Zero Width Non-Joiner
        "\u200d",  # Zero Width Joiner
        "\u2060",  # Word Joiner
        "\ufeff",  # BOM / Zero Width No-Break Space
    )

    def normalize(
        self,
        text: str,
    ) -> str:
        """
        标准化多行 PDF 文本，同时保留逻辑行边界。

        Args:
            text:
                PDF Page 提取出的原始文本。

        Returns:
            标准化后的多行文本。
        """

        if text is None:
            raise ValueError(
                "TitleNormalizer text cannot be None."
            )

        if not isinstance(
            text,
            str,
        ):
            raise TypeError(
                "TitleNormalizer expects a string. "
                f"Received: {type(text).__name__}"
            )

        if not text:
            return ""

        normalized_text = self._normalize_line_endings(
            text
        )

        normalized_lines: list[str] = []

        for raw_line in normalized_text.splitlines():

            line = self.normalize_line(
                raw_line
            )

            if not line:
                continue

            normalized_lines.append(
                line
            )

        return "\n".join(
            normalized_lines
        )

    @classmethod
    def normalize_line(
        cls,
        text: str,
    ) -> str:
        """
        标准化单行文本。

        Example:

            １．２　User　Profile

        ->

            1.2 User Profile
        """

        if not text:
            return ""

        normalized = unicodedata.normalize(
            "NFKC",
            text,
        )

        normalized = normalized.replace(
            "\u00a0",
            " ",
        )

        normalized = normalized.replace(
            "\u3000",
            " ",
        )

        for character in (
            cls._ZERO_WIDTH_CHARACTERS
        ):
            normalized = normalized.replace(
                character,
                "",
            )

        normalized = (
            cls._CONTROL_CHARACTER_PATTERN.sub(
                "",
                normalized,
            )
        )

        normalized = (
            cls._MULTIPLE_HORIZONTAL_SPACES_PATTERN.sub(
                " ",
                normalized,
            )
        )

        normalized = (
            cls._NUMBER_DOT_SPACING_PATTERN.sub(
                ".",
                normalized,
            )
        )

        normalized = (
            cls._LEADING_TRAILING_SPACES_PATTERN.sub(
                "",
                normalized,
            )
        )

        return normalized.strip()

    @staticmethod
    def _normalize_line_endings(
        text: str,
    ) -> str:
        """
        Windows / Unix / legacy Mac 换行统一为 \\n。
        """

        return (
            text
            .replace(
                "\r\n",
                "\n",
            )
            .replace(
                "\r",
                "\n",
            )
        )