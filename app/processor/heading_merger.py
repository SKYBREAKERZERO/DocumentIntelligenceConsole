from __future__ import annotations

import re

from app.model.block import BlockType, DocumentBlock
from app.model.document import Document


class HeadingMerger:
    """
    DOCX 跨行标题合并处理器。

    典型场景：

        Heading:
            4.5 User Authority

        下一段：
            Management

        合并后：
            4.5 User Authority Management

    保守原则：
        宁可不合并，也不把正文或下一个真实标题错误合并。
    """

    _NUMBERED_HEADING_PATTERN = re.compile(
        r"""
        ^
        [0-9]+
        (?:
            \.[0-9]+
        )*
        (?:\s+|$)
        """,
        re.VERBOSE,
    )

    _SENTENCE_ENDINGS = (
        "。",
        "！",
        "？",
        ".",
        "!",
        "?",
        "ます",
        "です",
        "した",
        "する。",
        "参照。",
    )

    _BODY_MARKERS = (
        "について",
        "以下",
        "本仕様書",
        "場合",
        "参照",
        "示す",
        "とする",
        "必要がある",
        "shall",
        "must",
        "should",
        "is defined",
        "are defined",
    )

    _CONTINUATION_WORDS = {
        "and",
        "or",
        "of",
        "for",
        "to",
        "with",
        "using",
        "from",
        "between",
        "management",
        "function",
        "setting",
        "settings",
        "specification",
        "overview",
        "profile",
        "flow",
    }

    def __init__(
        self,
        *,
        max_heading_length: int = 160,
        max_continuation_length: int = 60,
        rebuild_pages: bool = True,
    ) -> None:

        if max_heading_length <= 0:
            raise ValueError(
                "max_heading_length must be greater than 0."
            )

        if max_continuation_length <= 0:
            raise ValueError(
                "max_continuation_length must be greater than 0."
            )

        self.max_heading_length = max_heading_length
        self.max_continuation_length = max_continuation_length
        self.rebuild_pages = rebuild_pages

    def process(
        self,
        document: Document,
    ) -> Document:

        self._validate_document(document)

        source_blocks = sorted(
            document.blocks,
            key=lambda block: block.order,
        )

        merged_blocks: list[DocumentBlock] = []

        merged_count = 0
        index = 0

        while index < len(source_blocks):
            current = source_blocks[index].model_copy(
                deep=True
            )

            if (
                current.block_type == BlockType.HEADING
                and index + 1 < len(source_blocks)
            ):
                next_block = source_blocks[index + 1]

                if self._should_merge(
                    current=current,
                    next_block=next_block,
                ):
                    current.text = self._join_text(
                        current.text,
                        next_block.text,
                    )

                    current.metadata = {
                        **current.metadata,
                        "heading_merged": True,
                        "merged_block_orders": [
                            current.order,
                            next_block.order,
                        ],
                    }

                    merged_count += 1
                    index += 1

            merged_blocks.append(current)
            index += 1

        self._reassign_order(
            merged_blocks
        )

        document.blocks = merged_blocks

        if self.rebuild_pages:
            self._rebuild_logical_pages(
                document
            )

        document.metadata.update(
            {
                "heading_merger": "HeadingMerger",
                "heading_merger_status": "SUCCESS",
                "heading_merged_count": merged_count,
                "block_count_after_heading_merge": len(
                    document.blocks
                ),
            }
        )

        return document

    def _should_merge(
        self,
        *,
        current: DocumentBlock,
        next_block: DocumentBlock,
    ) -> bool:

        current_text = self._normalize_text(
            current.text
        )

        next_text = self._normalize_text(
            next_block.text
        )

        if not current_text or not next_text:
            return False

        # 表格、图片、分页符不允许并入标题。
        if next_block.block_type in {
            BlockType.TABLE,
            BlockType.IMAGE,
            BlockType.PAGE_BREAK,
        }:
            return False

        # 下一块如果是正式编号标题，不能合并。
        if self._has_number_prefix(next_text):
            return False

        # 两个 Heading 只有在层级一致且下一标题无编号时，
        # 才允许进一步判断。
        if next_block.block_type == BlockType.HEADING:
            if (
                current.level is not None
                and next_block.level is not None
                and current.level != next_block.level
            ):
                return False

        # 普通段落或列表可以作为标题续行。
        elif next_block.block_type not in {
            BlockType.PARAGRAPH,
            BlockType.LIST,
            BlockType.TEXTBOX,
        }:
            return False

        if len(current_text) >= self.max_heading_length:
            return False

        if len(next_text) > self.max_continuation_length:
            return False

        if self._looks_like_sentence(next_text):
            return False

        if self._contains_body_marker(next_text):
            return False

        if self._looks_like_table_text(next_text):
            return False

        # 当前标题以连接词、斜杠、连字符或冒号等结束，
        # 说明大概率还未完成。
        if self._current_heading_is_incomplete(
            current_text
        ):
            return True

        # 下一行是非常短的标题片段。
        if len(next_text) <= 20:
            return True

        # 英文标题续行特征。
        if self._is_english_title_fragment(
            next_text
        ):
            return True

        return False

    @classmethod
    def _current_heading_is_incomplete(
        cls,
        text: str,
    ) -> bool:

        stripped = text.rstrip()

        if stripped.endswith(
            (
                "/",
                "-",
                "–",
                "—",
                ":",
                "：",
                "(",
                "（",
                "&",
            )
        ):
            return True

        words = stripped.lower().split()

        if not words:
            return False

        return words[-1] in {
            "and",
            "or",
            "of",
            "for",
            "to",
            "with",
            "using",
            "from",
            "between",
        }

    @classmethod
    def _is_english_title_fragment(
        cls,
        text: str,
    ) -> bool:

        if not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9 /&()_\-]*",
            text,
        ):
            return False

        words = text.lower().split()

        if not words:
            return False

        if len(words) <= 5:
            return True

        return any(
            word in cls._CONTINUATION_WORDS
            for word in words
        )

    @classmethod
    def _looks_like_sentence(
        cls,
        text: str,
    ) -> bool:

        if text.endswith(
            cls._SENTENCE_ENDINGS
        ):
            return True

        # 逗号较多时更像正文。
        punctuation_count = sum(
            text.count(mark)
            for mark in (
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

        lower_text = text.lower()

        return any(
            marker.lower() in lower_text
            for marker in cls._BODY_MARKERS
        )

    @staticmethod
    def _looks_like_table_text(
        text: str,
    ) -> bool:

        return "|" in text or "\t" in text

    @classmethod
    def _has_number_prefix(
        cls,
        text: str,
    ) -> bool:

        return bool(
            cls._NUMBERED_HEADING_PATTERN.match(
                text
            )
        )

    @staticmethod
    def _join_text(
        first: str,
        second: str,
    ) -> str:

        first = first.strip()
        second = second.strip()

        if not first:
            return second

        if not second:
            return first

        # 日文断词时不额外增加空格。
        if (
            HeadingMerger._is_cjk_character(first[-1])
            and HeadingMerger._is_cjk_character(second[0])
        ):
            return first + second

        return f"{first} {second}"

    @staticmethod
    def _is_cjk_character(
        character: str,
    ) -> bool:

        if not character:
            return False

        code = ord(character)

        return (
            0x3040 <= code <= 0x30FF
            or 0x3400 <= code <= 0x4DBF
            or 0x4E00 <= code <= 0x9FFF
            or 0xF900 <= code <= 0xFAFF
        )

    @staticmethod
    def _normalize_text(
        text: str,
    ) -> str:

        if not text:
            return ""

        return " ".join(
            text.split()
        )

    @staticmethod
    def _reassign_order(
        blocks: list[DocumentBlock],
    ) -> None:

        for order, block in enumerate(blocks):
            block.order = order

    @staticmethod
    def _rebuild_logical_pages(
        document: Document,
    ) -> None:
        """
        DOCX 当前使用一个逻辑 Page。

        Heading Merge 后同步 pages[0].text，
        避免 blocks 与 pages 内容不一致。
        """

        if not document.pages:
            return

        text = "\n".join(
            block.text.strip()
            for block in document.blocks
            if block.text.strip()
        )

        document.pages[0].text = text

    @staticmethod
    def _validate_document(
        document: Document,
    ) -> None:

        if document is None:
            raise ValueError(
                "Document cannot be None."
            )

        if not isinstance(document, Document):
            raise TypeError(
                "HeadingMerger expects an "
                "app.model.document.Document instance."
            )

        if document.file_type.lower() != "docx":
            raise ValueError(
                "HeadingMerger only accepts DOCX documents. "
                f"Received file_type: {document.file_type}"
            )

        if not document.blocks:
            raise ValueError(
                "DOCX document contains no blocks."
            )