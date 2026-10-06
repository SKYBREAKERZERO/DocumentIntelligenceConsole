from __future__ import annotations

import math
import re

from app.model.document import Document


class TokenCounter:
    """
    轻量级 Token 数量估算器。

    负责：

        - 为最终 Content 估算 token_count
        - 支持中 / 日 / 英 / 数字 / 技术符号混合文本
        - 写入 TokenCounter metadata

    不负责：

        - 调用真实 Embedding Model Tokenizer
        - Chunk 分块
        - 截断文本
        - Embedding

    注意：

        token_count 是估算值，不是 BGE-M3、
        OpenAI 或其他具体模型 tokenizer 的精确结果。

    当前策略：

        CJK 字符：
            约 1 字符 = 1 Token

        英文：
            约 4 个字符 = 1 Token
            最少 1 Token

        数字：
            约 3 个数字 = 1 Token
            最少 1 Token

        标点 / 技术符号：
            每个约 1 Token
    """

    _TOKEN_PATTERN = re.compile(
        r"""
        (?P<cjk>
            [\u3040-\u30ff]
            |
            [\u31f0-\u31ff]
            |
            [\u3400-\u4dbf]
            |
            [\u4e00-\u9fff]
            |
            [\uf900-\ufaff]
            |
            [\uac00-\ud7af]
        )
        |
        (?P<english>
            [A-Za-z]+
            (?:
                ['’]
                [A-Za-z]+
            )*
        )
        |
        (?P<number>
            \d+
        )
        |
        (?P<symbol>
            [^\s]
        )
        """,
        re.VERBOSE,
    )

    def process(
        self,
        document: Document,
    ) -> Document:

        self._validate_document(
            document
        )

        total_token_count = 0
        zero_token_content_count = 0

        for content in document.contents:

            token_count = self.count(
                content.text
            )

            content.token_count = (
                token_count
            )

            total_token_count += (
                token_count
            )

            if token_count == 0:
                zero_token_content_count += 1

        document.metadata.update(
            {
                "token_counter": "TokenCounter",
                "token_counter_status": "SUCCESS",
                "token_count_method": (
                    "heuristic"
                ),
                "token_count_is_estimate": True,
                "token_count_content_count": len(
                    document.contents
                ),
                "token_count_total": (
                    total_token_count
                ),
                "token_count_zero_content_count": (
                    zero_token_content_count
                ),
            }
        )

        return document

    @classmethod
    def count(
        cls,
        text: str,
    ) -> int:
        """
        估算单段文本 Token 数量。

        Returns:
            >= 0 的整数。
        """

        if text is None:
            return 0

        if not isinstance(
            text,
            str,
        ):
            raise TypeError(
                "TokenCounter.count() expects "
                "text to be a string."
            )

        if not text.strip():
            return 0

        token_count = 0

        for match in cls._TOKEN_PATTERN.finditer(
            text
        ):

            value = match.group(0)

            # =====================
            # CJK
            # =====================

            if match.lastgroup == "cjk":

                token_count += 1

                continue

            # =====================
            # English
            # =====================

            if match.lastgroup == "english":

                # 英文 BPE / SentencePiece 类 tokenizer
                # 通常不是严格按单词切分。
                #
                # 使用约 4 characters / token
                # 作为轻量估算。
                token_count += max(
                    1,
                    math.ceil(
                        len(value) / 4
                    ),
                )

                continue

            # =====================
            # Number
            # =====================

            if match.lastgroup == "number":

                token_count += max(
                    1,
                    math.ceil(
                        len(value) / 3
                    ),
                )

                continue

            # =====================
            # Symbol
            # =====================

            if match.lastgroup == "symbol":

                token_count += 1

        return token_count

    @staticmethod
    def _validate_document(
        document: Document,
    ) -> None:

        if document is None:
            raise ValueError(
                "Document cannot be None."
            )

        if not isinstance(
            document,
            Document,
        ):
            raise TypeError(
                "TokenCounter expects an "
                "app.model.document.Document instance."
            )