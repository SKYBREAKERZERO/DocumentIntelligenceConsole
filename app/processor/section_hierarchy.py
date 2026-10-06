from __future__ import annotations

from collections import defaultdict

from app.model.document import Document
from app.model.section import Section


class SectionHierarchyBuilder:
    """
    企业级 Section 层级关系构建器。

    负责：
        - 为 document.sections 建立 parent_section_id
        - 优先根据 Section ID 的点号层级寻找父节点
        - 当 ID 层级不完整或非标准时，
          回退到“同 Chapter、前序、较低 level”的最近 Section
        - 防止跨 Chapter parent
        - 防止 self-parent
        - 防止 forward-parent
        - 防止 parent level >= child level
        - 对异常结构输出诊断 Metadata

    示例：

        1.2
        1.2.3
        1.2.3.4

    得到：

        1.2
            parent = None

        1.2.3
            parent = 1.2

        1.2.3.4
            parent = 1.2.3

    如果直接父节点不存在：

        1.2
        1.2.3.4

    则寻找最近的现存祖先：

        1.2.3.4
            parent = 1.2

    如果 Section ID 本身无法表达层级，例如：

        chapter=1
        Section A      level=2
        Section B      level=3

    则按文档顺序回退为：

        Section B
            parent = Section A

    不负责：
        - 创建缺失 Section
        - 修改 Section ID
        - 修改 Chapter ID
        - 修改 Section level
        - Section 排序
        - Chapter 建模

    设计原则：
        1. parent 必须已经存在于 document.sections。
        2. parent 必须与 child 属于同一 Chapter。
        3. parent 必须位于 child 之前。
        4. parent.level 必须小于 child.level。
        5. ID 结构优先，level + 文档顺序仅作为安全回退。
    """

    def process(
        self,
        document: Document,
    ) -> Document:

        self._validate_document(
            document
        )

        # ==============================================
        # Empty Structure
        # ==============================================

        if not document.sections:

            document.metadata.update(
                {
                    "section_hierarchy_builder": (
                        "SectionHierarchyBuilder"
                    ),

                    "section_hierarchy_status": (
                        "SUCCESS"
                    ),

                    "section_hierarchy_strategy": (
                        "same_chapter_dotted_id_then_previous_level"
                    ),

                    "section_hierarchy_section_count": (
                        0
                    ),

                    "section_hierarchy_parent_count": (
                        0
                    ),

                    "section_hierarchy_root_count": (
                        0
                    ),

                    "section_hierarchy_direct_parent_count": (
                        0
                    ),

                    "section_hierarchy_ancestor_fallback_count": (
                        0
                    ),

                    "section_hierarchy_level_fallback_count": (
                        0
                    ),

                    "section_hierarchy_duplicate_key_count": (
                        0
                    ),

                    "section_hierarchy_invalid_level_count": (
                        0
                    ),

                    "section_hierarchy_rejected_cross_chapter_count": (
                        0
                    ),

                    "section_hierarchy_rejected_forward_parent_count": (
                        0
                    ),

                    "section_hierarchy_rejected_level_count": (
                        0
                    ),
                }
            )

            return document

        # ==============================================
        # Normalize Existing parent_section_id
        # ==============================================
        #
        # Builder 每次都重新构建 parent，
        # 保证重复执行 process() 时结果幂等。

        for section in document.sections:

            section.parent_section_id = (
                None
            )

        # ==============================================
        # Build Index
        # ==============================================

        # 同一个 Chapter 内使用：
        #
        #   (chapter_id, section_id)
        #
        # 作为内部唯一定位键。
        #
        # 理论上 Parser 已保证 ID 稳定，
        # 但这里仍统计重复 key 作为防御性诊断。

        section_map: dict[
            tuple[
                str | None,
                str,
            ],
            Section,
        ] = {}

        position_map: dict[
            tuple[
                str | None,
                str,
            ],
            int,
        ] = {}

        duplicate_key_count = 0

        for index, section in enumerate(
            document.sections
        ):

            key = self._section_key(
                section
            )

            if key in section_map:

                duplicate_key_count += 1

                # 保留第一条。
                #
                # 第一条最接近 source order，
                # 也避免后面的重复 Section
                # 反向成为前面节点的 parent。
                continue

            section_map[
                key
            ] = section

            position_map[
                key
            ] = index

        # ==============================================
        # Runtime State
        # ==============================================

        previous_by_chapter: defaultdict[
            str | None,
            list[
                tuple[
                    int,
                    Section,
                ]
            ],
        ] = defaultdict(
            list
        )

        assigned_parent_count = 0
        root_section_count = 0

        direct_parent_count = 0
        ancestor_fallback_count = 0
        level_fallback_count = 0

        invalid_level_count = 0

        rejected_cross_chapter_count = 0
        rejected_forward_parent_count = 0
        rejected_level_count = 0

        # ==============================================
        # Parent Assignment
        # ==============================================

        for current_index, section in enumerate(
            document.sections
        ):

            child_level = (
                self._normalize_level(
                    section.level
                )
            )

            if child_level < 2:

                invalid_level_count += 1

            immediate_parent_id = (
                self.find_parent(
                    section.id
                )
            )

            (
                dotted_parent_id,
                dotted_parent_mode,
                rejected,
            ) = self._find_existing_dotted_parent(
                section=section,

                current_index=(
                    current_index
                ),

                section_map=(
                    section_map
                ),

                position_map=(
                    position_map
                ),
            )

            rejected_cross_chapter_count += (
                rejected[
                    "cross_chapter"
                ]
            )

            rejected_forward_parent_count += (
                rejected[
                    "forward_parent"
                ]
            )

            rejected_level_count += (
                rejected[
                    "invalid_level"
                ]
            )

            parent_id = (
                dotted_parent_id
            )

            if parent_id is not None:

                if (
                    dotted_parent_mode
                    == "direct"
                ):

                    direct_parent_count += 1

                else:

                    ancestor_fallback_count += 1

            else:

                # ======================================
                # Level Fallback
                # ======================================
                #
                # 用于：
                #
                #   - 非数字 Section ID
                #   - 不完整 Section ID
                #   - Parser 自动生成的特殊 ID
                #
                # 仅允许：
                #
                #   same chapter
                #   previous section
                #   lower level

                parent_id = (
                    self._find_previous_level_parent(
                        section=section,

                        previous_sections=(
                            previous_by_chapter[
                                section.chapter_id
                            ]
                        ),
                    )
                )

                if parent_id is not None:

                    level_fallback_count += 1

            # ==========================================
            # Final Assignment
            # ==========================================

            if parent_id is None:

                section.parent_section_id = (
                    None
                )

                root_section_count += 1

            else:

                section.parent_section_id = (
                    parent_id
                )

                assigned_parent_count += 1

            previous_by_chapter[
                section.chapter_id
            ].append(
                (
                    current_index,
                    section,
                )
            )

        # ==============================================
        # Final Safety Validation
        # ==============================================

        invalid_parent_reference_count = (
            self._count_invalid_parent_references(
                sections=(
                    document.sections
                )
            )
        )

        self_parent_count = sum(
            1
            for section
            in document.sections
            if (
                section.parent_section_id
                is not None
                and section.parent_section_id
                == section.id
            )
        )

        # ==============================================
        # Metadata
        # ==============================================

        document.metadata.update(
            {
                "section_hierarchy_builder": (
                    "SectionHierarchyBuilder"
                ),

                "section_hierarchy_status": (
                    "SUCCESS"
                ),

                "section_hierarchy_strategy": (
                    "same_chapter_dotted_id_then_previous_level"
                ),

                "section_hierarchy_section_count": len(
                    document.sections
                ),

                "section_hierarchy_parent_count": (
                    assigned_parent_count
                ),

                "section_hierarchy_root_count": (
                    root_section_count
                ),

                "section_hierarchy_direct_parent_count": (
                    direct_parent_count
                ),

                "section_hierarchy_ancestor_fallback_count": (
                    ancestor_fallback_count
                ),

                "section_hierarchy_level_fallback_count": (
                    level_fallback_count
                ),

                "section_hierarchy_duplicate_key_count": (
                    duplicate_key_count
                ),

                "section_hierarchy_invalid_level_count": (
                    invalid_level_count
                ),

                "section_hierarchy_rejected_cross_chapter_count": (
                    rejected_cross_chapter_count
                ),

                "section_hierarchy_rejected_forward_parent_count": (
                    rejected_forward_parent_count
                ),

                "section_hierarchy_rejected_level_count": (
                    rejected_level_count
                ),

                "section_hierarchy_invalid_parent_reference_count": (
                    invalid_parent_reference_count
                ),

                "section_hierarchy_self_parent_count": (
                    self_parent_count
                ),
            }
        )

        return document

    # ==================================================
    # Find Existing Dotted Parent
    # ==================================================

    def _find_existing_dotted_parent(
        self,
        *,
        section: Section,
        current_index: int,
        section_map: dict[
            tuple[
                str | None,
                str,
            ],
            Section,
        ],
        position_map: dict[
            tuple[
                str | None,
                str,
            ],
            int,
        ],
    ) -> tuple[
        str | None,
        str | None,
        dict[
            str,
            int,
        ],
    ]:
        """
        根据 Section ID：

            1.2.3.4

        从：

            1.2.3
            1.2
            1

        逐级向上寻找最近的合法 Section。

        返回：
            parent_id
            mode:
                direct
                ancestor
                None
            rejected diagnostics
        """

        rejected = {
            "cross_chapter": 0,
            "forward_parent": 0,
            "invalid_level": 0,
        }

        immediate_parent_id = (
            self.find_parent(
                section.id
            )
        )

        candidate_id = (
            immediate_parent_id
        )

        while candidate_id is not None:

            if candidate_id == section.id:

                return (
                    None,
                    None,
                    rejected,
                )

            # ==========================================
            # Same Chapter Lookup
            # ==========================================

            candidate_key = (
                section.chapter_id,
                candidate_id,
            )

            candidate = section_map.get(
                candidate_key
            )

            if candidate is not None:

                # ======================================
                # Position Safety
                # ======================================

                candidate_position = (
                    position_map.get(
                        candidate_key
                    )
                )

                if (
                    candidate_position
                    is None
                    or candidate_position
                    >= current_index
                ):

                    rejected[
                        "forward_parent"
                    ] += 1

                # ======================================
                # Level Safety
                # ======================================

                elif (
                    self._normalize_level(
                        candidate.level
                    )
                    >= self._normalize_level(
                        section.level
                    )
                ):

                    rejected[
                        "invalid_level"
                    ] += 1

                else:

                    mode = (
                        "direct"
                        if candidate_id
                        == immediate_parent_id
                        else "ancestor"
                    )

                    return (
                        candidate.id,
                        mode,
                        rejected,
                    )

            # ==========================================
            # Cross-Chapter Diagnostic
            # ==========================================
            #
            # 只做诊断，不使用跨 Chapter candidate。

            for (
                candidate_chapter_id,
                stored_section_id,
            ), stored_section in (
                section_map.items()
            ):

                if (
                    stored_section_id
                    != candidate_id
                ):
                    continue

                if (
                    candidate_chapter_id
                    == section.chapter_id
                ):
                    continue

                rejected[
                    "cross_chapter"
                ] += 1

                break

            candidate_id = (
                self.find_parent(
                    candidate_id
                )
            )

        return (
            None,
            None,
            rejected,
        )

    # ==================================================
    # Previous-Level Fallback
    # ==================================================

    @classmethod
    def _find_previous_level_parent(
        cls,
        *,
        section: Section,
        previous_sections: list[
            tuple[
                int,
                Section,
            ]
        ],
    ) -> str | None:
        """
        在同一 Chapter 的前序 Section 中，
        从后往前找最近的较低 level Section。

        Example:

            A level=2
            B level=3

        B.parent = A

        但：

            A level=3
            B level=2

        B 不会挂到 A。
        """

        child_level = (
            cls._normalize_level(
                section.level
            )
        )

        for (
            _position,
            candidate,
        ) in reversed(
            previous_sections
        ):

            if (
                candidate.id
                == section.id
            ):

                continue

            if (
                candidate.chapter_id
                != section.chapter_id
            ):

                continue

            candidate_level = (
                cls._normalize_level(
                    candidate.level
                )
            )

            if (
                candidate_level
                < child_level
            ):

                return (
                    candidate.id
                )

        return None

    # ==================================================
    # Parent Parser
    # ==================================================

    @staticmethod
    def find_parent(
        section_id: str,
    ) -> str | None:
        """
        根据 Section ID 返回语法上的直接父级 ID。

        Examples:

            1
                -> None

            1.2
                -> 1

            1.2.3
                -> 1.2

            1.2.3.4
                -> 1.2.3

        注意：
            这里只解析字符串层级。

            是否：
                - 真实存在
                - 同 Chapter
                - 出现在 child 之前
                - level 合法

            由 process() / _find_existing_dotted_parent()
            负责校验。
        """

        if section_id is None:
            return None

        normalized_id = str(
            section_id
        ).strip().strip(
            "."
        )

        if not normalized_id:
            return None

        parts = [
            part.strip()
            for part
            in normalized_id.split(
                "."
            )
            if part.strip()
        ]

        if len(parts) <= 1:
            return None

        return ".".join(
            parts[:-1]
        )

    # ==================================================
    # Invalid Parent Reference Validation
    # ==================================================

    @staticmethod
    def _count_invalid_parent_references(
        *,
        sections: list[
            Section
        ],
    ) -> int:
        """
        最终结构完整性检查。

        parent_section_id 必须指向：
            - 同一 Chapter
            - document.sections 中真实存在的 Section
        """

        valid_keys = {
            (
                section.chapter_id,
                section.id,
            )
            for section
            in sections
        }

        invalid_count = 0

        for section in sections:

            parent_id = (
                section.parent_section_id
            )

            if parent_id is None:
                continue

            parent_key = (
                section.chapter_id,
                parent_id,
            )

            if (
                parent_key
                not in valid_keys
            ):

                invalid_count += 1

        return (
            invalid_count
        )

    # ==================================================
    # Section Key
    # ==================================================

    @staticmethod
    def _section_key(
        section: Section,
    ) -> tuple[
        str | None,
        str,
    ]:

        section_id = str(
            section.id
        ).strip()

        return (
            section.chapter_id,
            section_id,
        )

    # ==================================================
    # Level
    # ==================================================

    @staticmethod
    def _normalize_level(
        value,
    ) -> int:

        try:

            level = int(
                value
            )

        except (
            TypeError,
            ValueError,
        ):

            return 1

        return max(
            level,
            1,
        )

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
                "SectionHierarchyBuilder expects an "
                "app.model.document.Document instance."
            )
