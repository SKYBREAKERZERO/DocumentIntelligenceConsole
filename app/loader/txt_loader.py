from __future__ import annotations

import codecs
import re
from pathlib import Path

from app.loader.base_loader import BaseLoader
from app.model.block import BlockType, DocumentBlock
from app.model.document import Document
from app.model.page import Page


class TXTLoaderError(RuntimeError):
    """TXT 文件加载异常。"""


class TXTLoader(BaseLoader):
    """
    TXT Loader。

    支持：
        - UTF-8
        - UTF-8 BOM
        - UTF-16 LE / BE
        - CP932
        - Shift-JIS

    映射：
        non-empty line -> DocumentBlock
        whole file     -> one logical Page
    """

    SUPPORTED_EXTENSIONS = {
        ".txt",
    }

    _NUMBERED_HEADING_PATTERN = re.compile(
        r"""
        ^
        [0-9０-９]+
        (?:
            [\.．]
            [0-9０-９]+
        )*
        [\s　]+
        .+
        $
        """,
        re.VERBOSE,
    )

    _MARKDOWN_HEADING_PATTERN = re.compile(
        r"^\#{1,6}[\s　]+.+$"
    )

    def __init__(
        self,
        *,
        keep_blank_lines: bool = False,
    ) -> None:

        self.keep_blank_lines = bool(
            keep_blank_lines
        )

    def load(
        self,
        file_path: str | Path,
    ) -> Document:

        path = self._validate_input_path(
            file_path
        )

        try:
            raw = path.read_bytes()

            text, encoding = self._decode(
                raw
            )

            # Normalize line endings at loader boundary.
            text = (
                text.replace(
                    "\r\n",
                    "\n",
                )
                .replace(
                    "\r",
                    "\n",
                )
            )

            lines = text.split(
                "\n"
            )

            blocks: list[
                DocumentBlock
            ] = []

            order = 0
            non_empty_line_count = 0

            for line_number, raw_line in enumerate(
                lines,
                start=1,
            ):

                line = raw_line.strip()

                if not line:

                    if not self.keep_blank_lines:
                        continue

                    # Blank lines are not useful as blocks.
                    continue

                non_empty_line_count += 1

                block_type = (
                    BlockType.PARAGRAPH
                )

                level = None

                if self._MARKDOWN_HEADING_PATTERN.match(
                    line
                ):

                    hashes = len(
                        line.split(
                            None,
                            1,
                        )[0]
                    )

                    block_type = (
                        BlockType.HEADING
                    )

                    level = max(
                        min(
                            hashes,
                            6,
                        ),
                        1,
                    )

                elif self._NUMBERED_HEADING_PATTERN.match(
                    line
                ):

                    number_part = (
                        line.split(
                            None,
                            1,
                        )[0]
                    )

                    normalized = (
                        number_part
                        .replace(
                            "．",
                            ".",
                        )
                    )

                    level = max(
                        len(
                            normalized.split(
                                "."
                            )
                        ),
                        1,
                    )

                    block_type = (
                        BlockType.HEADING
                    )

                blocks.append(
                    DocumentBlock(
                        id=(
                            f"txt-line-"
                            f"{line_number:08d}"
                        ),
                        block_type=block_type,
                        text=line,
                        level=level,
                        order=order,
                        page_number=1,
                        source="txt",
                        metadata={
                            "line_number": (
                                line_number
                            ),
                            "encoding": (
                                encoding
                            ),
                        },
                    )
                )

                order += 1

            no_readable_text = not blocks

            page_text = "\n".join(
                block.text
                for block in blocks
                if block.text
            ).strip()

            return Document(
                file_name=path.name,
                file_type="txt",
                pages=[
                    Page(
                        page_number=1,
                        text=page_text,
                    )
                ],
                blocks=blocks,
                chapters=[],
                sections=[],
                contents=[],
                metadata={
                    "source_format": "txt",
                    "loader": "TXTLoader",
                    "loader_status": (
                        "WARNING_NO_EXTRACTABLE_TEXT"
                        if no_readable_text
                        else "SUCCESS"
                    ),
                    "loader_warning_code": (
                        "TXT_NO_READABLE_TEXT"
                        if no_readable_text
                        else None
                    ),
                    "encoding": encoding,
                    "byte_count": len(
                        raw
                    ),
                    "line_count": len(
                        lines
                    ),
                    "non_empty_line_count": (
                        non_empty_line_count
                    ),
                    "block_count": len(
                        blocks
                    ),
                    "character_count": len(
                        page_text
                    ),
                },
            )

        except TXTLoaderError:
            raise

        except Exception as exc:
            raise TXTLoaderError(
                f"Failed to load TXT file "
                f"'{path.name}': {exc}"
            ) from exc

    @classmethod
    def _decode(
        cls,
        raw: bytes,
    ) -> tuple[
        str,
        str,
    ]:

        if not raw:
            return "", "utf-8"

        # BOM-aware paths first.
        if raw.startswith(
            codecs.BOM_UTF8
        ):
            return (
                raw.decode(
                    "utf-8-sig"
                ),
                "utf-8-sig",
            )

        if (
            raw.startswith(
                codecs.BOM_UTF16_LE
            )
            or raw.startswith(
                codecs.BOM_UTF16_BE
            )
        ):
            return (
                raw.decode(
                    "utf-16"
                ),
                "utf-16",
            )

        # Prefer strict UTF-8 before Japanese legacy encodings.
        candidates = (
            "utf-8",
            "cp932",
            "shift_jis",
        )

        for encoding in candidates:

            try:
                return (
                    raw.decode(
                        encoding
                    ),
                    encoding,
                )

            except UnicodeDecodeError:
                continue

        raise TXTLoaderError(
            "Unable to decode TXT file. "
            "Supported encodings: UTF-8, UTF-16, "
            "CP932, Shift-JIS."
        )

    @classmethod
    def _validate_input_path(
        cls,
        file_path: str | Path,
    ) -> Path:

        path = Path(
            file_path
        ).expanduser()

        if not path.exists():
            raise FileNotFoundError(
                f"TXT file not found: {path}"
            )

        if not path.is_file():
            raise IsADirectoryError(
                f"Input path is not a file: {path}"
            )

        if path.suffix.lower() not in (
            cls.SUPPORTED_EXTENSIONS
        ):
            raise ValueError(
                "TXTLoader only accepts .txt files. "
                f"Received: "
                f"{path.suffix or '<no extension>'}"
            )

        return path
