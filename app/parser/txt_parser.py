from __future__ import annotations

from app.analyzer.import_resilience_policy import ImportResiliencePolicy
from app.model.document import Document
from app.parser.structured_text_parser import StructuredTextParser


class TXTParser:
    """TXT structure parser."""

    def __init__(
        self,
    ) -> None:

        self._parser = (
            StructuredTextParser(
                parser_name="TXTParser"
            )
        )

    def parse(
        self,
        document: Document,
    ) -> Document:

        if (
            document.file_type.lower()
            != "txt"
        ):
            raise ValueError(
                "TXTParser only accepts TXT documents. "
                f"Received: {document.file_type}"
            )

        if not any(
            str(getattr(block, "text", "") or "").strip()
            for block in document.blocks
        ):
            return ImportResiliencePolicy.mark_metadata_only(
                document,
                warning_code="TXT_NO_READABLE_TEXT",
                parser_name="TXTParser",
                warning_message=(
                    "TXT contains no readable text; stored as metadata-only "
                    "and excluded from Qdrant."
                ),
            )

        return self._parser.parse(
            document
        )
