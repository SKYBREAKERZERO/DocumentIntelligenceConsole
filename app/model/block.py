from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class BlockType(StrEnum):

    HEADING = "heading"

    PARAGRAPH = "paragraph"

    TABLE = "table"

    LIST = "list"

    IMAGE = "image"

    TEXTBOX = "textbox"

    PAGE_BREAK = "page_break"

    UNKNOWN = "unknown"


class DocumentBlock(BaseModel):
    """
    文档统一 Block。

    一个 Block 表示文档中的最小结构单元。

    DOCX:
        Heading
        Paragraph
        Table Row

    PDF:
        Text Block

    PPTX:
        TextBox
        Table
        Image

    XLSX:
        Row
        Cell Group
    """

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
    )

    # =====================
    # Identity
    # =====================

    id: str | None = None

    order: int = Field(
        default=0,
        ge=0,
    )

    block_type: BlockType

    # =====================
    # Content
    # =====================

    text: str = ""

    # =====================
    # Heading
    # =====================

    level: int | None = Field(
        default=None,
        ge=1,
    )

    style_name: str | None = None

    # =====================
    # Location
    # =====================

    page_number: int | None = Field(
        default=None,
        ge=1,
    )

    # PDF / PPT 可用
    x0: float | None = None
    y0: float | None = None
    x1: float | None = None
    y1: float | None = None

    # =====================
    # Table
    # =====================

    table_index: int | None = Field(
        default=None,
        ge=0,
    )

    row_index: int | None = Field(
        default=None,
        ge=0,
    )

    cells: list[str] = Field(
        default_factory=list,
    )

    # =====================
    # Parser
    # =====================

    source: str | None = None

    confidence: float | None = Field(
        default=None,
        ge=0,
        le=1,
    )

    # =====================
    # Metadata
    # =====================

    metadata: dict[str, Any] = Field(
        default_factory=dict,
    )