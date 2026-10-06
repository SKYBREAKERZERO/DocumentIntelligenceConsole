from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from app.analyzer.import_resilience_policy import ImportResiliencePolicy
from app.model.chapter import Chapter
from app.model.content import Content
from app.model.document import Document
from app.parser.structured_text_parser import StructuredTextParser


class ImageParser:
    """
    企业级 PNG / JPG / JPEG OCR Parser。

    默认目标：
        普通截图中的 OCR 噪声只能影响 Content，
        不应轻易污染 Chapter / Section 结构。

    两种模式：

    1. screenshot_fallback
       普通截图默认使用：
           Image -> Chapter 1 = 文件名 -> OCR text = Content

    2. structured_text
       只有检测到高置信度文档结构时，才调用 StructuredTextParser。
       例如：1 Introduction / 1.1 Purpose / 2 Functional Summary。
    """

    SUPPORTED_FILE_TYPES = {"png", "jpg", "jpeg"}

    _NUMBERED_HEADING_PATTERN = re.compile(
        r"""
        ^\s*
        (?P<id>\d+(?:\.\d+)*)
        (?:[\.．、:：]\s*|\s+)
        (?P<title>\S.*)
        \s*$
        """,
        re.VERBOSE,
    )

    _MARKDOWN_HEADING_PATTERN = re.compile(
        r"^\s*(?P<marks>\#{1,6})\s+(?P<title>\S.*)$"
    )

    _TIME_PATTERN = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?$")
    _RESOLUTION_PATTERN = re.compile(r"^\d{2,5}\s*[x×X]\s*\d{2,5}$")
    _CODE_PATTERN = re.compile(r"^[#＃]\s*\d+$")
    _PLACEHOLDER_PATTERN = re.compile(r"^[{＜<\[].*[}＞>\]]$")

    def __init__(
        self,
        *,
        enable_structured_text_detection: bool = True,
        minimum_numbered_heading_count: int = 3,
        minimum_markdown_heading_count: int = 2,
        maximum_heading_title_length: int = 180,
        fallback_chapter_id: str = "1",
    ) -> None:

        if minimum_numbered_heading_count < 2:
            raise ValueError(
                "minimum_numbered_heading_count must be at least 2."
            )

        if minimum_markdown_heading_count < 2:
            raise ValueError(
                "minimum_markdown_heading_count must be at least 2."
            )

        if maximum_heading_title_length <= 0:
            raise ValueError(
                "maximum_heading_title_length must be greater than 0."
            )

        if not str(fallback_chapter_id or "").strip():
            raise ValueError("fallback_chapter_id cannot be empty.")

        self.enable_structured_text_detection = bool(
            enable_structured_text_detection
        )
        self.minimum_numbered_heading_count = int(
            minimum_numbered_heading_count
        )
        self.minimum_markdown_heading_count = int(
            minimum_markdown_heading_count
        )
        self.maximum_heading_title_length = int(
            maximum_heading_title_length
        )
        self.fallback_chapter_id = str(fallback_chapter_id).strip()

        self._parser = StructuredTextParser(parser_name="ImageParser")

    def parse(self, document: Document) -> Document:
        self._validate_document(document)

        text = self._collect_ocr_text(document)
        if not text:
            loader_warning_code = str(
                document.metadata.get("loader_warning_code")
                or "IMAGE_OCR_NO_READABLE_TEXT"
            ).strip()
            loader_warning_message = str(
                document.metadata.get("loader_warning_message")
                or (
                    "Image OCR detected no readable text at the configured "
                    "confidence threshold; stored as metadata-only and excluded "
                    "from Qdrant."
                )
            ).strip()

            return ImportResiliencePolicy.mark_metadata_only(
                document,
                warning_code=loader_warning_code,
                parser_name="ImageParser",
                warning_message=loader_warning_message,
            )

        diagnostics = self._analyze_structure_signal(text)

        use_structured_parser = (
            self.enable_structured_text_detection
            and diagnostics["image_parser_high_confidence_structure"]
        )

        if use_structured_parser:
            parsed_document = self._parser.parse(document)
            parsed_document.metadata.update(
                {
                    "parser": "ImageParser",
                    "parser_status": "SUCCESS",
                    "image_parser_mode": "structured_text",
                    "image_parser_strategy": (
                        "high_confidence_structure_gate_then_structured_text_parser"
                    ),
                    **diagnostics,
                    "chapter_count": len(parsed_document.chapters),
                    "section_count": len(parsed_document.sections),
                    "content_count": len(parsed_document.contents),
                }
            )
            return parsed_document

        return self._build_screenshot_fallback(
            document=document,
            text=text,
            diagnostics=diagnostics,
        )

    def _build_screenshot_fallback(
        self,
        *,
        document: Document,
        text: str,
        diagnostics: dict[str, Any],
    ) -> Document:

        document.chapters.clear()
        document.sections.clear()
        document.contents.clear()

        chapter_title = self._resolve_fallback_chapter_title(document)
        page_number = self._resolve_first_page_number(document)

        chapter = Chapter(
            id=self.fallback_chapter_id,
            title_jp=chapter_title,
            title_en=None,
            level=1,
            sort_order=1,
            page_number=page_number,
            metadata={
                "generated_fallback": True,
                "source_format": str(document.file_type or "").strip().lower(),
                "fallback_reason": (
                    "insufficient_high_confidence_structure_signal"
                ),
            },
        )

        document.chapters.append(chapter)
        document.contents.append(
            Content(
                chapter_id=chapter.id,
                section_id=None,
                text=text,
                page_number=page_number,
            )
        )

        document.metadata.update(
            {
                "parser": "ImageParser",
                "parser_status": "SUCCESS",
                "image_parser_mode": "screenshot_fallback",
                "image_parser_strategy": (
                    "screenshot_safe_single_chapter_with_high_confidence_structure_gate"
                ),
                **diagnostics,
                "fallback_chapter_count": 1,
                "chapter_count": 1,
                "section_count": 0,
                "content_count": 1,
            }
        )

        return document

    def _analyze_structure_signal(self, text: str) -> dict[str, Any]:
        lines = [
            self._normalize_line(line)
            for line in str(text or "").splitlines()
        ]
        lines = [line for line in lines if line]

        numbered_headings: list[tuple[str, str]] = []
        markdown_heading_count = 0
        rejected_numbered_candidate_count = 0

        for line in lines:
            markdown_match = self._MARKDOWN_HEADING_PATTERN.fullmatch(line)
            if markdown_match is not None:
                title = markdown_match.group("title").strip()
                if self._is_meaningful_heading_title(title):
                    markdown_heading_count += 1
                continue

            numbered_match = self._NUMBERED_HEADING_PATTERN.fullmatch(line)
            if numbered_match is None:
                continue

            heading_id = numbered_match.group("id").strip()
            title = numbered_match.group("title").strip()

            if not self._is_reliable_numbered_heading_candidate(
                heading_id=heading_id,
                title=title,
                full_line=line,
            ):
                rejected_numbered_candidate_count += 1
                continue

            numbered_headings.append((heading_id, title))

        numbered_heading_count = len(numbered_headings)
        has_hierarchy = self._has_numbered_hierarchy(numbered_headings)
        has_sequential_top_level = self._has_sequential_top_level_headings(
            numbered_headings
        )

        numbered_structure_confident = (
            numbered_heading_count >= self.minimum_numbered_heading_count
            and (has_hierarchy or has_sequential_top_level)
        )
        markdown_structure_confident = (
            markdown_heading_count >= self.minimum_markdown_heading_count
        )
        high_confidence_structure = (
            numbered_structure_confident or markdown_structure_confident
        )

        return {
            "image_parser_structured_detection_enabled": (
                self.enable_structured_text_detection
            ),
            "image_parser_ocr_nonempty_line_count": len(lines),
            "image_parser_numbered_heading_candidate_count": (
                numbered_heading_count
            ),
            "image_parser_rejected_numbered_candidate_count": (
                rejected_numbered_candidate_count
            ),
            "image_parser_markdown_heading_candidate_count": (
                markdown_heading_count
            ),
            "image_parser_numbered_hierarchy_detected": has_hierarchy,
            "image_parser_sequential_top_level_detected": (
                has_sequential_top_level
            ),
            "image_parser_high_confidence_structure": (
                high_confidence_structure
            ),
        }

    def _is_reliable_numbered_heading_candidate(
        self,
        *,
        heading_id: str,
        title: str,
        full_line: str,
    ) -> bool:

        normalized_id = str(heading_id or "").strip()
        normalized_title = self._normalize_line(title)
        normalized_line = self._normalize_line(full_line)

        if not normalized_id or not normalized_title:
            return False

        for part in normalized_id.split("."):
            if len(part) > 1 and part.startswith("0"):
                return False

        if len(normalized_title) > self.maximum_heading_title_length:
            return False

        if self._TIME_PATTERN.fullmatch(normalized_line):
            return False
        if self._RESOLUTION_PATTERN.fullmatch(normalized_line):
            return False
        if self._CODE_PATTERN.fullmatch(normalized_line):
            return False
        if self._PLACEHOLDER_PATTERN.fullmatch(normalized_title):
            return False
        if not self._is_meaningful_heading_title(normalized_title):
            return False

        return True

    @staticmethod
    def _has_numbered_hierarchy(
        headings: list[tuple[str, str]],
    ) -> bool:
        ids = {heading_id for heading_id, _ in headings}
        for heading_id in ids:
            if "." not in heading_id:
                continue
            parent_id = heading_id.rsplit(".", maxsplit=1)[0]
            if parent_id in ids:
                return True
        return False

    @staticmethod
    def _has_sequential_top_level_headings(
        headings: list[tuple[str, str]],
    ) -> bool:
        values: list[int] = []
        for heading_id, _ in headings:
            if "." in heading_id or not heading_id.isdigit():
                continue
            value = int(heading_id)
            if value > 0:
                values.append(value)

        if len(values) < 2:
            return False

        unique_values = sorted(set(values))
        return any(
            right == left + 1
            for left, right in zip(unique_values, unique_values[1:])
        )

    @classmethod
    def _collect_ocr_text(cls, document: Document) -> str:
        page_texts: list[str] = []
        for page in document.pages or []:
            text = cls._normalize_text(getattr(page, "text", ""))
            if text:
                page_texts.append(text)

        if page_texts:
            return "\n".join(page_texts).strip()

        blocks = sorted(
            document.blocks or [],
            key=lambda block: (
                int(getattr(block, "page_number", 1) or 1),
                int(getattr(block, "order", 0) or 0),
            ),
        )

        block_texts: list[str] = []
        for block in blocks:
            text = cls._normalize_text(getattr(block, "text", ""))
            if text:
                block_texts.append(text)

        return "\n".join(block_texts).strip()

    @staticmethod
    def _resolve_fallback_chapter_title(document: Document) -> str:
        file_name = str(document.file_name or "").strip()
        if not file_name:
            return "Image"
        return Path(file_name).stem or "Image"

    @staticmethod
    def _resolve_first_page_number(document: Document) -> int:
        for page in document.pages or []:
            page_number = getattr(page, "page_number", None)
            if page_number is not None:
                return int(page_number)
        return 1

    @staticmethod
    def _is_meaningful_heading_title(title: str) -> bool:
        normalized = str(title or "").strip()
        if not normalized:
            return False
        if re.fullmatch(r"[\W_]+", normalized):
            return False
        return any(
            char.isalpha()
            or ("\u3040" <= char <= "\u30ff")
            or ("\u3400" <= char <= "\u9fff")
            for char in normalized
        )

    @staticmethod
    def _normalize_line(value: Any) -> str:
        return " ".join(
            str(value or "")
            .replace("\u3000", " ")
            .replace("\xa0", " ")
            .split()
        ).strip()

    @staticmethod
    def _normalize_text(value: Any) -> str:
        if value is None:
            return ""

        text = (
            str(value)
            .replace("\r\n", "\n")
            .replace("\r", "\n")
            .replace("\u200b", "")
            .replace("\ufeff", "")
        )

        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return "\n".join(lines).strip()

    @classmethod
    def _validate_document(cls, document: Document) -> None:
        if document is None:
            raise ValueError("Document cannot be None.")

        if not isinstance(document, Document):
            raise TypeError(
                "ImageParser expects an app.model.document.Document instance."
            )

        file_type = str(document.file_type or "").strip().lower()
        if file_type not in cls.SUPPORTED_FILE_TYPES:
            raise ValueError(
                "ImageParser only accepts PNG/JPG/JPEG documents. "
                f"Received: {document.file_type}"
            )
