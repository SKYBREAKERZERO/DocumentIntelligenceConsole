from __future__ import annotations

import re
import unicodedata

from app.model.block import DocumentBlock
from app.model.document import Document


class UnicodeNormalizer:
    """
    文档 Unicode 标准化处理器。

    负责：
        - 使用 NFKC 统一全角/半角字符
        - 全角数字转换为半角数字
        - 全角英文转换为半角英文
        - 全角句点转换为普通句点
        - 全角空格、NBSP 转换为普通空格
        - 清理不可见控制字符
        - 合并重复空白
        - 同时处理 pages、blocks、chapters、sections、contents

    示例：
        １．２．３
        -> 1.2.3

        Function Overvieｗ
        -> Function Overview

        Ｕｓｅｒ　Ｐｒｏｆｉｌｅ
        -> User Profile

    不负责：
        - 拼接跨行标题
        - 修复缺失字符
        - 章节识别
        - 内容分块
    """

    _CONTROL_CHARACTER_PATTERN = re.compile(
        r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]"
    )

    _MULTIPLE_HORIZONTAL_SPACES_PATTERN = re.compile(
        r"[ \t]+"
    )

    _MULTIPLE_NEWLINES_PATTERN = re.compile(
        r"\n{3,}"
    )

    _SPACE_BEFORE_PUNCTUATION_PATTERN = re.compile(
        r"\s+([,.;:!?，。；：！？])"
    )

    _SPACE_AFTER_OPENING_BRACKET_PATTERN = re.compile(
        r"([\(\[\{（［｛])\s+"
    )

    _SPACE_BEFORE_CLOSING_BRACKET_PATTERN = re.compile(
        r"\s+([\)\]\}）］｝])"
    )

    def __init__(
        self,
        *,
        normalize_pages: bool = True,
        normalize_blocks: bool = True,
        normalize_structure: bool = True,
        normalize_contents: bool = True,
    ) -> None:

        self.normalize_pages = normalize_pages
        self.normalize_blocks = normalize_blocks
        self.normalize_structure = normalize_structure
        self.normalize_contents = normalize_contents

    def process(
        self,
        document: Document,
    ) -> Document:

        self._validate_document(
            document
        )

        normalized_field_count = 0
        changed_field_count = 0

        if self.normalize_pages:
            for page in document.pages:
                original = page.text

                page.text = self.normalize_multiline(
                    page.text
                )

                normalized_field_count += 1

                if page.text != original:
                    changed_field_count += 1

        if self.normalize_blocks:
            for block in document.blocks:
                changed = self._normalize_block(
                    block
                )

                normalized_field_count += changed[0]
                changed_field_count += changed[1]

        if self.normalize_structure:
            for chapter in document.chapters:
                original_id = chapter.id
                original_title_jp = chapter.title_jp
                original_title_en = chapter.title_en

                chapter.id = self.normalize_identifier(
                    chapter.id
                )

                chapter.title_jp = self.normalize_line(
                    chapter.title_jp or ""
                ) or None

                chapter.title_en = self.normalize_line(
                    chapter.title_en or ""
                ) or None

                normalized_field_count += 3

                if chapter.id != original_id:
                    changed_field_count += 1

                if chapter.title_jp != original_title_jp:
                    changed_field_count += 1

                if chapter.title_en != original_title_en:
                    changed_field_count += 1

            for section in document.sections:
                original_id = section.id
                original_chapter_id = section.chapter_id
                original_parent_section_id = (
                    section.parent_section_id
                )
                original_title_jp = section.title_jp
                original_title_en = section.title_en

                section.id = self.normalize_identifier(
                    section.id
                )

                section.chapter_id = (
                    self.normalize_identifier(
                        section.chapter_id
                    )
                    if section.chapter_id
                    else None
                )

                section.parent_section_id = (
                    self.normalize_identifier(
                        section.parent_section_id
                    )
                    if section.parent_section_id
                    else None
                )

                section.title_jp = self.normalize_line(
                    section.title_jp or ""
                ) or None

                section.title_en = self.normalize_line(
                    section.title_en or ""
                ) or None

                normalized_field_count += 5

                if section.id != original_id:
                    changed_field_count += 1

                if section.chapter_id != original_chapter_id:
                    changed_field_count += 1

                if (
                    section.parent_section_id
                    != original_parent_section_id
                ):
                    changed_field_count += 1

                if section.title_jp != original_title_jp:
                    changed_field_count += 1

                if section.title_en != original_title_en:
                    changed_field_count += 1

        if self.normalize_contents:
            for content in document.contents:
                original_text = content.text
                original_chapter_id = content.chapter_id
                original_section_id = content.section_id

                content.text = self.normalize_multiline(
                    content.text
                )

                if content.chapter_id:
                    content.chapter_id = (
                        self.normalize_identifier(
                            content.chapter_id
                        )
                    )

                if content.section_id:
                    content.section_id = (
                        self.normalize_identifier(
                            content.section_id
                        )
                    )

                normalized_field_count += 3

                if content.text != original_text:
                    changed_field_count += 1

                if content.chapter_id != original_chapter_id:
                    changed_field_count += 1

                if content.section_id != original_section_id:
                    changed_field_count += 1

        document.metadata.update(
            {
                "unicode_normalizer": "UnicodeNormalizer",
                "unicode_normalizer_status": "SUCCESS",
                "unicode_normalized_field_count": (
                    normalized_field_count
                ),
                "unicode_changed_field_count": (
                    changed_field_count
                ),
                "unicode_normalization_form": "NFKC",
            }
        )

        return document

    def _normalize_block(
        self,
        block: DocumentBlock,
    ) -> tuple[int, int]:

        normalized_count = 0
        changed_count = 0

        original_text = block.text
        original_style_name = block.style_name
        original_cells = list(block.cells)

        block.text = self.normalize_multiline(
            block.text
        )

        block.style_name = (
            self.normalize_line(
                block.style_name
            )
            if block.style_name
            else None
        )

        # Loader 已经保留了表格 Cell 的真实列位置。
        # UnicodeNormalizer 只能标准化 Cell 文本，
        # 不能删除空 Cell，否则会导致列左移。
        block.cells = [
            self.normalize_line(
                cell
            )
            for cell in block.cells
        ]

        normalized_count += 3

        if block.text != original_text:
            changed_count += 1

        if block.style_name != original_style_name:
            changed_count += 1

        if block.cells != original_cells:
            changed_count += 1

        return normalized_count, changed_count

    @classmethod
    def normalize_identifier(
        cls,
        value: str,
    ) -> str:

        normalized = cls.normalize_line(
            value
        )

        normalized = normalized.replace(
            "．",
            ".",
        )

        normalized = re.sub(
            r"\s*\.\s*",
            ".",
            normalized,
        )

        return normalized.strip(".")

    @classmethod
    def normalize_line(
        cls,
        text: str,
    ) -> str:

        if not text:
            return ""

        normalized = unicodedata.normalize(
            "NFKC",
            text,
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
            "\u200b",
            "\u200c",
            "\u200d",
            "\u2060",
            "\ufeff",
        ):
            normalized = normalized.replace(
                character,
                "",
            )

        normalized = cls._CONTROL_CHARACTER_PATTERN.sub(
            "",
            normalized,
        )

        normalized = cls._MULTIPLE_HORIZONTAL_SPACES_PATTERN.sub(
            " ",
            normalized,
        )

        normalized = cls._SPACE_BEFORE_PUNCTUATION_PATTERN.sub(
            r"\1",
            normalized,
        )

        normalized = (
            cls._SPACE_AFTER_OPENING_BRACKET_PATTERN.sub(
                r"\1",
                normalized,
            )
        )

        normalized = (
            cls._SPACE_BEFORE_CLOSING_BRACKET_PATTERN.sub(
                r"\1",
                normalized,
            )
        )

        return normalized.strip()

    @classmethod
    def normalize_multiline(
        cls,
        text: str,
    ) -> str:

        if not text:
            return ""

        normalized_lines = []

        for raw_line in text.splitlines():
            line = cls.normalize_line(
                raw_line
            )

            if line:
                normalized_lines.append(line)

        normalized = "\n".join(
            normalized_lines
        )

        normalized = cls._MULTIPLE_NEWLINES_PATTERN.sub(
            "\n\n",
            normalized,
        )

        return normalized.strip()

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
                "UnicodeNormalizer expects an "
                "app.model.document.Document instance."
            )