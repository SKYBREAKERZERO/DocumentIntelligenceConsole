from __future__ import annotations

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
)


class Content(BaseModel):
    """
    文档内容单元。

    Content 可以属于：
        - Chapter
        - Section
        - Chapter + Section

    在 Parser 中间阶段也允许：
        chapter_id=None
        section_id=None

    后续由 ContentFilter 决定是否删除孤立内容。
    """

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
    )

    # =====================
    # Structure
    # =====================

    chapter_id: str | None = None

    section_id: str | None = None

    # =====================
    # Content
    # =====================

    text: str

    # =====================
    # Location
    # =====================

    page_number: int | None = Field(
        default=None,
        ge=1,
    )

    # =====================
    # Metadata / Source Evidence
    # =====================

    # Parser-specific structured location metadata (sheet/row/cell/table,
    # source block identity, etc.).  Kept on Content so later relation
    # extraction can produce auditable evidence before Qdrant indexing.
    metadata: dict[str, object] = Field(
        default_factory=dict,
    )

    # =====================
    # Chunk
    # =====================

    chunk_index: int = Field(
        default=0,
        ge=0,
    )

    # =====================
    # Token
    # =====================

    token_count: int = Field(
        default=0,
        ge=0,
    )