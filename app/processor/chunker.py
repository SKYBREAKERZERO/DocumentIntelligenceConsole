from __future__ import annotations

import re
from collections import defaultdict

from app.model.content import Content
from app.model.document import Document


class Chunker:
    """
    企业级 Content 字符分块处理器。

    负责：
        - 将超过 max_length 的 Content 拆分
        - 尽量在自然边界处分块，而不是固定位置硬切
        - 防止产生极短尾块
        - 保留原 Chapter / Section / Page 信息
        - 为最终 Content 分配连续 chunk_index
        - 写入详细 Chunker metadata

    不负责：
        - Token 统计
        - Semantic Chunking
        - Embedding
        - Content 有效性过滤
        - Section 排序
        - JSON / PostgreSQL 保存

    分块策略：

        1. Paragraph boundary
        2. Sentence boundary
        3. Punctuation boundary
        4. Whitespace boundary
        5. Hard split fallback

    默认：

        max_length = 1000
        min_length = 150

    注意：
        max_length / min_length 均为 Python 字符数量，
        不是 Token 数量。

    设计目标：
        - 每个 Chunk <= max_length
        - 尽可能避免：

              "t"
              "e."
              "olchain."

          这种极短尾块
        - 不跨原 Content 合并
        - 不跨 Chapter / Section 合并
        - 保持现有 Pipeline 公共接口兼容

    兼容旧代码：

        Chunker(
            max_length=1000
        )

    仍然有效。
    """

    # ==================================================
    # Sentence Boundary
    # ==================================================

    _SENTENCE_BOUNDARY_PATTERN = re.compile(
        r"""
        (?:
            (?<=[。！？!?])
            |
            (?<=[.;；])
        )
        \s+
        """,
        re.VERBOSE,
    )

    # ==================================================
    # Init
    # ==================================================

    def __init__(
        self,
        max_length: int = 1000,
        min_length: int = 150,
    ) -> None:

        if not isinstance(
            max_length,
            int,
        ):
            raise TypeError(
                "max_length must be an integer."
            )

        if not isinstance(
            min_length,
            int,
        ):
            raise TypeError(
                "min_length must be an integer."
            )

        if max_length <= 0:
            raise ValueError(
                "max_length must be greater than 0."
            )

        if min_length < 0:
            raise ValueError(
                "min_length cannot be negative."
            )

        if min_length >= max_length:
            raise ValueError(
                "min_length must be smaller than max_length."
            )

        self.max_length = (
            max_length
        )

        self.min_length = (
            min_length
        )

    # ==================================================
    # Public API
    # ==================================================

    def process(
        self,
        document: Document,
    ) -> Document:

        self._validate_document(
            document
        )

        original_content_count = len(
            document.contents
        )

        new_contents: list[
            Content
        ] = []

        split_content_count = 0
        generated_chunk_count = 0

        merged_short_tail_count = 0
        rebalanced_short_tail_count = 0
        hard_split_count = 0
        empty_content_count = 0

        # ==============================================
        # Chunk Index
        # ==============================================
        #
        # 同一个 Chapter / Section scope 下连续编号：
        #
        #   Content A -> 0, 1
        #   Content B -> 2, 3
        #
        # 而不是每个原始 Content 都重新从 0 开始。

        next_chunk_index: defaultdict[
            tuple[
                str | None,
                str | None,
            ],
            int,
        ] = defaultdict(
            int
        )

        # ==============================================
        # Process Contents
        # ==============================================

        for content in document.contents:

            scope = (
                content.chapter_id,
                content.section_id,
            )

            text = self._normalize_text(
                content.text
            )

            if not text:

                empty_content_count += 1

                continue

            (
                chunks,
                split_stats,
            ) = self._split_text(
                text
            )

            if len(
                chunks
            ) > 1:

                split_content_count += 1

            merged_short_tail_count += (
                split_stats[
                    "merged_short_tail_count"
                ]
            )

            rebalanced_short_tail_count += (
                split_stats[
                    "rebalanced_short_tail_count"
                ]
            )

            hard_split_count += (
                split_stats[
                    "hard_split_count"
                ]
            )

            # ==========================================
            # Generate Final Contents
            # ==========================================

            for chunk_text in chunks:

                if not chunk_text:
                    continue

                new_content = (
                    content.model_copy()
                )

                new_content.text = (
                    chunk_text
                )

                new_content.chunk_index = (
                    next_chunk_index[
                        scope
                    ]
                )

                next_chunk_index[
                    scope
                ] += 1

                new_contents.append(
                    new_content
                )

                generated_chunk_count += 1

        # ==============================================
        # Replace Contents
        # ==============================================

        document.contents = (
            new_contents
        )

        # ==============================================
        # Metadata
        # ==============================================

        document.metadata.update(
            {
                "chunker": (
                    "Chunker"
                ),

                "chunker_status": (
                    "SUCCESS"
                ),

                "chunk_strategy": (
                    "character_boundary_aware_with_short_tail_control"
                ),

                "chunk_max_length": (
                    self.max_length
                ),

                "chunk_min_length": (
                    self.min_length
                ),

                "chunk_original_content_count": (
                    original_content_count
                ),

                "chunk_split_content_count": (
                    split_content_count
                ),

                "chunk_final_content_count": len(
                    new_contents
                ),

                "chunk_generated_count": (
                    generated_chunk_count
                ),

                "chunk_merged_short_tail_count": (
                    merged_short_tail_count
                ),

                "chunk_rebalanced_short_tail_count": (
                    rebalanced_short_tail_count
                ),

                "chunk_hard_split_count": (
                    hard_split_count
                ),

                "chunk_empty_content_count": (
                    empty_content_count
                ),
            }
        )

        return document

    # ==================================================
    # Split Text
    # ==================================================

    def _split_text(
        self,
        text: str,
    ) -> tuple[
        list[str],
        dict[str, int],
    ]:
        """
        将单个 Content 拆成最终 Chunk。

        保证：
            - 每个 Chunk <= max_length
            - 优先自然边界
            - 最后一个 Chunk 太短时尝试合并
            - 无法直接合并时尝试重新平衡
        """

        normalized_text = (
            self._normalize_text(
                text
            )
        )

        if not normalized_text:

            return (
                [],
                {
                    "merged_short_tail_count": 0,
                    "rebalanced_short_tail_count": 0,
                    "hard_split_count": 0,
                },
            )

        if (
            len(
                normalized_text
            )
            <= self.max_length
        ):

            return (
                [
                    normalized_text
                ],
                {
                    "merged_short_tail_count": 0,
                    "rebalanced_short_tail_count": 0,
                    "hard_split_count": 0,
                },
            )

        chunks: list[
            str
        ] = []

        remaining = (
            normalized_text
        )

        hard_split_count = 0

        # ==============================================
        # Main Split Loop
        # ==============================================

        while (
            len(
                remaining
            )
            > self.max_length
        ):

            split_at = (
                self._find_best_split(
                    text=remaining,
                    limit=self.max_length,
                )
            )

            if (
                split_at <= 0
                or split_at
                > self.max_length
            ):

                split_at = (
                    self.max_length
                )

                hard_split_count += 1

            chunk_text = (
                remaining[
                    :split_at
                ].strip()
            )

            remaining = (
                remaining[
                    split_at:
                ].strip()
            )

            # 防御性处理：
            # 极端空白输入不能造成死循环。
            if not chunk_text:

                split_at = (
                    self.max_length
                )

                chunk_text = (
                    remaining[
                        :split_at
                    ].strip()
                )

                remaining = (
                    remaining[
                        split_at:
                    ].strip()
                )

                hard_split_count += 1

            if chunk_text:

                chunks.append(
                    chunk_text
                )

        if remaining:

            chunks.append(
                remaining
            )

        # ==============================================
        # Short Tail Control
        # ==============================================

        (
            chunks,
            merged_short_tail_count,
            rebalanced_short_tail_count,
        ) = self._control_short_tail(
            chunks
        )

        # ==============================================
        # Final Safety
        # ==============================================

        final_chunks: list[
            str
        ] = []

        for chunk in chunks:

            normalized_chunk = (
                chunk.strip()
            )

            if not normalized_chunk:
                continue

            # 理论上不会发生。
            # 这里防止任何未来逻辑修改导致超长 Chunk。
            if (
                len(
                    normalized_chunk
                )
                <= self.max_length
            ):

                final_chunks.append(
                    normalized_chunk
                )

                continue

            # 最后的保险 hard split。
            for start in range(
                0,
                len(
                    normalized_chunk
                ),
                self.max_length,
            ):

                piece = (
                    normalized_chunk[
                        start:
                        start
                        + self.max_length
                    ].strip()
                )

                if piece:

                    final_chunks.append(
                        piece
                    )

                    hard_split_count += 1

        return (
            final_chunks,
            {
                "merged_short_tail_count": (
                    merged_short_tail_count
                ),

                "rebalanced_short_tail_count": (
                    rebalanced_short_tail_count
                ),

                "hard_split_count": (
                    hard_split_count
                ),
            },
        )

    # ==================================================
    # Short Tail
    # ==================================================

    def _control_short_tail(
        self,
        chunks: list[str],
    ) -> tuple[
        list[str],
        int,
        int,
    ]:
        """
        防止最后一个 Chunk 过短。

        Strategy A:
            previous + tail <= max_length
            -> 直接合并

        Strategy B:
            无法直接合并
            -> 重新平衡最后两个 Chunk

        Example:

            1000 chars
            3 chars

        不再保留 3-char tail，
        而是尝试变成：

            ~850 chars
            ~153 chars
        """

        if (
            len(
                chunks
            )
            < 2
        ):

            return (
                chunks,
                0,
                0,
            )

        tail = (
            chunks[
                -1
            ].strip()
        )

        if (
            not tail
            or len(
                tail
            )
            >= self.min_length
        ):

            return (
                chunks,
                0,
                0,
            )

        previous = (
            chunks[
                -2
            ].strip()
        )

        separator = (
            self._join_separator(
                previous,
                tail,
            )
        )

        # ==============================================
        # Direct Merge
        # ==============================================

        merged = (
            previous
            + separator
            + tail
        ).strip()

        if (
            len(
                merged
            )
            <= self.max_length
        ):

            chunks[
                -2
            ] = merged

            chunks.pop()

            return (
                chunks,
                1,
                0,
            )

        # ==============================================
        # Rebalance Last Two
        # ==============================================

        # 目标：
        #
        #   right >= min_length
        #   left <= max_length
        #
        desired_left_limit = min(
            self.max_length,

            max(
                self.min_length,
                len(
                    merged
                )
                - self.min_length,
            ),
        )

        split_at = (
            self._find_best_split(
                text=merged,
                limit=(
                    desired_left_limit
                ),
            )
        )

        if (
            split_at > 0
            and split_at
            < len(
                merged
            )
        ):

            left = (
                merged[
                    :split_at
                ].strip()
            )

            right = (
                merged[
                    split_at:
                ].strip()
            )

            if (
                left
                and right
                and len(
                    left
                )
                <= self.max_length
                and len(
                    right
                )
                <= self.max_length
                and len(
                    right
                )
                >= self.min_length
            ):

                chunks[
                    -2
                ] = left

                chunks[
                    -1
                ] = right

                return (
                    chunks,
                    0,
                    1,
                )

        # 无法安全重平衡时：
        #
        # 保留原 Chunk。
        #
        # 与其破坏 max_length，
        # 不如保留一个较短但合法的尾块。
        return (
            chunks,
            0,
            0,
        )

    # ==================================================
    # Best Split
    # ==================================================

    @classmethod
    def _find_best_split(
        cls,
        *,
        text: str,
        limit: int,
    ) -> int:
        """
        在 limit 以内寻找最合适的切分点。

        优先级：

            1. \n\n
            2. \n
            3. Sentence boundary
            4. 。！？!?;；
            5. ". " / "! " / "? "
            6. whitespace
            7. limit

        为避免产生异常短 Chunk，
        不接受过早的边界。
        """

        if not text:
            return 0

        if limit <= 0:
            return 0

        if len(
            text
        ) <= limit:

            return len(
                text
            )

        window = (
            text[
                :limit + 1
            ]
        )

        candidates: list[
            tuple[
                int,
                int,
            ]
        ] = []

        # candidate:
        #
        #   priority
        #   position
        #
        # priority 越小越优先。

        minimum_reasonable_position = int(
            limit
            * 0.55
        )

        # ==============================================
        # Paragraph Boundary
        # ==============================================

        for priority, delimiter in (
            (
                1,
                "\n\n",
            ),
            (
                2,
                "\n",
            ),
        ):

            index = (
                window.rfind(
                    delimiter
                )
            )

            if (
                index
                >= minimum_reasonable_position
            ):

                candidates.append(
                    (
                        priority,
                        index
                        + len(
                            delimiter
                        ),
                    )
                )

        # ==============================================
        # Sentence Boundary
        # ==============================================

        sentence_positions = [
            match.end()
            for match
            in cls._SENTENCE_BOUNDARY_PATTERN.finditer(
                window
            )
            if match.end()
            >= minimum_reasonable_position
        ]

        if sentence_positions:

            candidates.append(
                (
                    3,
                    sentence_positions[
                        -1
                    ],
                )
            )

        # ==============================================
        # Punctuation Boundary
        # ==============================================

        punctuation_minimum = int(
            limit
            * 0.60
        )

        for delimiter in (
            "。",
            "！",
            "？",
            "!",
            "?",
            "；",
            ";",
            ". ",
        ):

            index = (
                window.rfind(
                    delimiter
                )
            )

            if (
                index
                >= punctuation_minimum
            ):

                candidates.append(
                    (
                        4,
                        index
                        + len(
                            delimiter
                        ),
                    )
                )

        # ==============================================
        # Whitespace Boundary
        # ==============================================

        whitespace_minimum = int(
            limit
            * 0.70
        )

        whitespace_index = max(
            window.rfind(
                " "
            ),
            window.rfind(
                "\t"
            ),
        )

        if (
            whitespace_index
            >= whitespace_minimum
        ):

            candidates.append(
                (
                    5,
                    whitespace_index
                    + 1,
                )
            )

        # ==============================================
        # Select Best
        # ==============================================

        valid_candidates = [
            (
                priority,
                position,
            )
            for (
                priority,
                position,
            )
            in candidates
            if (
                0
                < position
                <= limit
            )
        ]

        if not valid_candidates:

            return (
                limit
            )

        # 先选最高优先级，
        # 同一优先级选最靠后的切点。
        best_priority = min(
            priority
            for (
                priority,
                _position,
            )
            in valid_candidates
        )

        best_positions = [
            position
            for (
                priority,
                position,
            )
            in valid_candidates
            if (
                priority
                == best_priority
            )
        ]

        return max(
            best_positions
        )

    # ==================================================
    # Separator
    # ==================================================

    @staticmethod
    def _join_separator(
        left: str,
        right: str,
    ) -> str:
        """
        合并短尾块时使用最小必要分隔符。
        """

        if not left or not right:
            return ""

        if (
            left.endswith(
                (
                    "\n",
                    " ",
                    "\t",
                )
            )
            or right.startswith(
                (
                    "\n",
                    " ",
                    "\t",
                )
            )
        ):

            return ""

        return "\n"

    # ==================================================
    # Normalize Text
    # ==================================================

    @staticmethod
    def _normalize_text(
        text: str | None,
    ) -> str:
        """
        Chunker 不修改正文语义。

        这里只去除 Content 最外层空白。
        """

        if text is None:
            return ""

        return str(
            text
        ).strip()

    # ==================================================
    # Validation
    # ==================================================

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
                "Chunker expects an "
                "app.model.document.Document instance."
            )
