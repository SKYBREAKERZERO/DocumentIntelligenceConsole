from __future__ import annotations

from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
)


class Chapter(BaseModel):
    """
    文档 Chapter。

    示例：
        1  Introduction
        2  System Overview
        8  Performance
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