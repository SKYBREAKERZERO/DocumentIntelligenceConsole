from __future__ import annotations

from app.model.document import Document


class DocumentValidator:
    """
    Document 结构完整性验证器。

    负责检查：

        - Chapter ID 是否重复
        - Section ID 是否重复
        - Section.chapter_id 是否有效
        - Section.parent_section_id 是否有效
        - Section 是否自引用
        - Parent Section 是否跨 Chapter
        - Content.chapter_id 是否有效
        - Content.section_id 是否有效
        - Content Chapter / Section 关系是否一致
        - 完全孤立的 Content
        - 真正为空的 Section

    不负责：

        - 修改 Document
        - 自动修复 ID
        - 自动删除重复结构
        - 建立 Section Hierarchy
        - ContentFilter
        - Chunk Index 重排

    返回：

        {
            "valid": bool,
            "errors": list[str],
            "warnings": list[str],
        }

    原则：

        errors:
            会导致 Document 结构引用不成立的问题。

        warnings:
            数据仍可使用，但值得检查的问题。
    """

    def validate(
        self,
        document: Document,
    ) -> dict[str, object]:

        self._validate_document(
            document
        )

        warnings: list[str] = []
        errors: list[str] = []

        # =========================
        # Chapter ID
        # =========================

        chapter_ids: set[str] = set()

        for chapter in document.chapters:

            chapter_id = chapter.id

            if chapter_id in chapter_ids:
                errors.append(
                    "Duplicate chapter detected: "
                    f"{chapter_id}"
                )

                continue

            chapter_ids.add(
                chapter_id
            )

        # =========================
        # Section ID
        # =========================

        section_ids: set[str] = set()

        for section in document.sections:

            section_id = section.id

            if section_id in section_ids:
                errors.append(
                    "Duplicate section detected: "
                    f"{section_id}"
                )

                continue

            section_ids.add(
                section_id
            )

        # =========================
        # Section Index
        # =========================

        section_map = {
            section.id: section
            for section in document.sections
        }

        # =========================
        # Section Chapter Check
        # =========================

        for section in document.sections:

            chapter_id = (
                section.chapter_id
            )

            if (
                chapter_id is not None
                and chapter_id not in chapter_ids
            ):
                errors.append(
                    "Section references missing chapter: "
                    f"section={section.id}, "
                    f"chapter={chapter_id}"
                )

        # =========================
        # Parent Section Check
        # =========================

        for section in document.sections:

            parent_id = (
                section.parent_section_id
            )

            if not parent_id:
                continue

            # -------------------------
            # Self reference
            # -------------------------

            if parent_id == section.id:
                errors.append(
                    "Section cannot reference itself "
                    "as parent: "
                    f"{section.id}"
                )

                continue

            # -------------------------
            # Missing parent
            # -------------------------

            parent = section_map.get(
                parent_id
            )

            if parent is None:
                errors.append(
                    "Section references missing parent: "
                    f"section={section.id}, "
                    f"parent={parent_id}"
                )

                continue

            # -------------------------
            # Cross Chapter
            # -------------------------

            if (
                section.chapter_id is not None
                and parent.chapter_id is not None
                and section.chapter_id
                != parent.chapter_id
            ):
                errors.append(
                    "Section parent belongs to "
                    "different chapter: "
                    f"section={section.id}, "
                    f"section_chapter="
                    f"{section.chapter_id}, "
                    f"parent={parent.id}, "
                    f"parent_chapter="
                    f"{parent.chapter_id}"
                )

        # =========================
        # Content Reference Check
        # =========================

        content_section_ids: set[str] = set()

        chapter_direct_content_ids: set[str] = (
            set()
        )

        for index, content in enumerate(
            document.contents
        ):

            chapter_id = (
                content.chapter_id
            )

            section_id = (
                content.section_id
            )

            # -------------------------
            # Completely orphan Content
            # -------------------------

            if (
                chapter_id is None
                and section_id is None
            ):
                errors.append(
                    "Content has neither chapter_id "
                    "nor section_id: "
                    f"content_index={index}"
                )

                continue

            # -------------------------
            # Chapter reference
            # -------------------------

            if chapter_id is not None:

                if chapter_id not in chapter_ids:
                    errors.append(
                        "Content references missing "
                        "chapter: "
                        f"content_index={index}, "
                        f"chapter={chapter_id}"
                    )

                if section_id is None:
                    chapter_direct_content_ids.add(
                        chapter_id
                    )

            # -------------------------
            # Section reference
            # -------------------------

            if section_id is not None:

                content_section_ids.add(
                    section_id
                )

                section = section_map.get(
                    section_id
                )

                if section is None:
                    errors.append(
                        "Content references missing "
                        "section: "
                        f"content_index={index}, "
                        f"section={section_id}"
                    )

                    continue

                # -------------------------
                # Chapter consistency
                # -------------------------

                if (
                    chapter_id is not None
                    and section.chapter_id
                    is not None
                    and chapter_id
                    != section.chapter_id
                ):
                    errors.append(
                        "Content chapter does not "
                        "match section chapter: "
                        f"content_index={index}, "
                        f"content_chapter="
                        f"{chapter_id}, "
                        f"section={section_id}, "
                        f"section_chapter="
                        f"{section.chapter_id}"
                    )

        # =========================
        # Child Section Index
        # =========================

        parent_section_ids = {
            section.parent_section_id
            for section in document.sections
            if section.parent_section_id
        }

        # =========================
        # Empty Section Check
        # =========================
        #
        # 只有同时满足：
        #
        #   - 没有直接 Content
        #   - 没有 Child Section
        #
        # 才认为 Section 真正为空。
        #
        # Example:
        #
        #   1.2
        #     └─ 1.2.1
        #          └─ Content
        #
        # 1.2 不应该被判定为空。

        for section in document.sections:

            has_direct_content = (
                section.id
                in content_section_ids
            )

            has_child_section = (
                section.id
                in parent_section_ids
            )

            if (
                not has_direct_content
                and not has_child_section
            ):
                warnings.append(
                    "Empty section: "
                    f"{section.id}"
                )

        # =========================
        # Empty Chapter Check
        # =========================

        section_chapter_ids = {
            section.chapter_id
            for section in document.sections
            if section.chapter_id
        }

        for chapter in document.chapters:

            has_section = (
                chapter.id
                in section_chapter_ids
            )

            has_direct_content = (
                chapter.id
                in chapter_direct_content_ids
            )

            if (
                not has_section
                and not has_direct_content
            ):
                warnings.append(
                    "Empty chapter: "
                    f"{chapter.id}"
                )

        # =========================
        # Result
        # =========================

        return {
            "valid": len(errors) == 0,
            "errors": errors,
            "warnings": warnings,
        }

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
                "DocumentValidator expects an "
                "app.model.document.Document instance."
            )