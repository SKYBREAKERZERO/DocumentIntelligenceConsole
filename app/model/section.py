from __future__ import annotations

from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
)


class Section(BaseModel):
    """
    文档 Section。

    示例：

        4.1
        4.1.2
        4.1.2.1

    Section 可以：

        - 直接属于 Chapter
        - 属于另一个父 Section
        - 在 Parser 中间阶段暂时没有 Chapter
    """

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
    )

    # =====================
    # Identity
    # =====================

    id: str = Field(
        min_length=1,
    )

    # =====================
    # Relations
    # =====================

    chapter_id: str | None = Field(
        default=None,
        min_length=1,
    )

    parent_section_id: str | None = Field(
        default=None,
        min_length=1,
    )

    # =====================
    # Titles
    # =====================

    title_jp: str | None = None

    title_en: str | None = None

    # =====================
    # Structure
    # =====================

    level: int = Field(
        default=1,
        ge=1,
    )

    sort_order: int = Field(
        default=0,
        ge=0,
    )

    # =====================
    # Location
    # =====================

    page_number: int | None = Field(
        default=None,
        ge=1,
    )

    # =====================
    # Metadata
    # =====================

    metadata: dict[str, Any] = Field(
        default_factory=dict,
    )