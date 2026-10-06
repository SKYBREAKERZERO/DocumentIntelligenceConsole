from __future__ import annotations

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
)


class Page(BaseModel):
    """
    文档逻辑 Page。

    对不同文件格式含义：

        PDF:
            原始 PDF Page

        DOCX:
            逻辑文本 Page

        PPTX:
            一个 Slide 对应一个逻辑 Page

        XLSX:
            一个 Sheet 对应一个逻辑 Page

    page_number 使用 1-based 编号。
    """

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
    )

    # =====================
    # Location
    # =====================

    page_number: int = Field(
        ge=1,
    )

    # =====================
    # Content
    # =====================

    text: str