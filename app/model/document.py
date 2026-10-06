from __future__ import annotations

from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
)

from app.model.block import DocumentBlock
from app.model.chapter import Chapter
from app.model.content import Content
from app.model.page import Page
from app.model.section import Section


class Document(BaseModel):
    """
    文档统一数据模型。

    数据处理阶段：

        Loader
            ↓
        pages / blocks
            ↓
        Parser
            ↓
        chapters / sections / contents
            ↓
        Filter / Chunk / Token
            ↓
        JSON / PostgreSQL / Vector Store

    不同文件格式可以使用不同的原始结构：

        PDF
            主要使用 pages

        DOCX
            主要使用 blocks
            同时维护逻辑 pages

        PPTX
            blocks + pages

        XLSX
            blocks + pages
    """

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
    )

    # =====================
    # Basic Information
    # =====================

    file_name: str = Field(
        min_length=1,
    )

    file_type: str = Field(
        min_length=1,
    )

    # =====================
    # Raw / Logical Pages
    # =====================

    pages: list[Page] = Field(
        default_factory=list,
    )

    # =====================
    # Raw Structured Blocks
    # =====================

    blocks: list[DocumentBlock] = Field(
        default_factory=list,
    )

    # =====================
    # Structure
    # =====================

    chapters: list[Chapter] = Field(
        default_factory=list,
    )

    sections: list[Section] = Field(
        default_factory=list,
    )

    # =====================
    # Processed Contents
    # =====================

    contents: list[Content] = Field(
        default_factory=list,
    )

    # =====================
    # Metadata
    # =====================

    metadata: dict[str, Any] = Field(
        default_factory=dict,
    )