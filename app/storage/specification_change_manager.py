from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from app.storage.primary_document_policy import PrimarySpecificationDocumentPolicy


DIFF_ALGORITHM_VERSION = "section-structural-v1"


@dataclass(frozen=True, slots=True)
class SectionSnapshot:
    section_id: str
    chapter_id: str | None
    parent_section_id: str | None
    chapter_title: str | None
    section_title: str | None
    sort_order: int
    page_number: int | None
    text: str
    content_ids: tuple[int, ...]

    @property
    def text_hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ChangeRecord:
    change_type: str
    entity_key: str
    old: SectionSnapshot | None
    new: SectionSnapshot | None
    similarity_score: float | None
    details: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ChangeSummary:
    change_set_id: int
    specification_id: int
    from_version_id: int
    to_version_id: int
    from_version_label: str
    to_version_label: str
    added_count: int
    modified_count: int
    removed_count: int
    moved_count: int
    unchanged_count: int
    total_change_count: int
    summary_text: str


class SpecificationChangeError(RuntimeError):
    """Version-diff persistence / comparison error."""


class SpecificationChangeManager:
    """Deterministic Specification Version Diff engine.

    The engine compares immutable document snapshots at section granularity.
    It does not depend on LLM output, so ADDED / REMOVED / MODIFIED / MOVED
    remain auditable facts.  An LLM summary can be layered on top later.

    Comparisons are cached in PostgreSQL.  The ingestion path marks adjacent
    version comparisons automatically; the GUI can also create arbitrary
    cross-version comparisons on demand.
    """

    def ensure_comparison(
        self,
        *,
        cur: Any,
        specification_id: int,
        from_version_id: int,
        to_version_id: int,
        is_adjacent: bool = False,
        force: bool = False,
    ) -> ChangeSummary:
        if from_version_id == to_version_id:
            raise SpecificationChangeError("Cannot compare a version with itself.")

        from_row = self._load_version(cur, from_version_id)
        to_row = self._load_version(cur, to_version_id)
        if from_row["specification_id"] != specification_id:
            raise SpecificationChangeError("from_version_id does not belong to the specification.")
        if to_row["specification_id"] != specification_id:
            raise SpecificationChangeError("to_version_id does not belong to the specification.")

        if not force:
            existing = self._load_existing_summary(
                cur,
                specification_id=specification_id,
                from_version_id=from_version_id,
                to_version_id=to_version_id,
            )
            if existing is not None:
                if is_adjacent:
                    cur.execute(
                        """
                        UPDATE public.specification_change_sets
                        SET is_adjacent = TRUE,
                            updated_at = NOW()
                        WHERE id = %s
                        """,
                        (existing.change_set_id,),
                    )
                return existing

        old_sections = self._load_sections(cur, from_row["document_id"])
        new_sections = self._load_sections(cur, to_row["document_id"])
        changes, unchanged_count = self.compare_sections(old_sections, new_sections)

        counts = {
            "ADDED": 0,
            "MODIFIED": 0,
            "REMOVED": 0,
            "MOVED": 0,
        }
        for change in changes:
            counts[change.change_type] += 1

        summary_text = self._summary_text(
            from_row["version_label"],
            to_row["version_label"],
            counts,
        )

        cur.execute(
            """
            INSERT INTO public.specification_change_sets (
                specification_id,
                from_version_id,
                to_version_id,
                branch_name,
                algorithm_version,
                is_adjacent,
                status,
                added_count,
                modified_count,
                removed_count,
                moved_count,
                unchanged_count,
                summary_text,
                created_at,
                updated_at
            )
            VALUES (
                %s, %s, %s, 'MAIN', %s, %s, 'READY',
                %s, %s, %s, %s, %s, %s, NOW(), NOW()
            )
            ON CONFLICT (
                specification_id,
                from_version_id,
                to_version_id,
                algorithm_version
            )
            DO UPDATE SET
                is_adjacent = EXCLUDED.is_adjacent OR public.specification_change_sets.is_adjacent,
                status = 'READY',
                added_count = EXCLUDED.added_count,
                modified_count = EXCLUDED.modified_count,
                removed_count = EXCLUDED.removed_count,
                moved_count = EXCLUDED.moved_count,
                unchanged_count = EXCLUDED.unchanged_count,
                summary_text = EXCLUDED.summary_text,
                updated_at = NOW()
            RETURNING id
            """,
            (
                specification_id,
                from_version_id,
                to_version_id,
                DIFF_ALGORITHM_VERSION,
                bool(is_adjacent),
                counts["ADDED"],
                counts["MODIFIED"],
                counts["REMOVED"],
                counts["MOVED"],
                unchanged_count,
                summary_text,
            ),
        )
        change_set_id = int(cur.fetchone()[0])

        cur.execute(
            "DELETE FROM public.specification_changes WHERE change_set_id = %s",
            (change_set_id,),
        )
        for change in changes:
            self._insert_change(
                cur,
                change_set_id=change_set_id,
                specification_id=specification_id,
                from_version_id=from_version_id,
                to_version_id=to_version_id,
                change=change,
            )

        if is_adjacent:
            cur.execute(
                """
                UPDATE public.specification_versions
                SET change_summary = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (summary_text, to_version_id),
            )

        return ChangeSummary(
            change_set_id=change_set_id,
            specification_id=specification_id,
            from_version_id=from_version_id,
            to_version_id=to_version_id,
            from_version_label=from_row["version_label"],
            to_version_label=to_row["version_label"],
            added_count=counts["ADDED"],
            modified_count=counts["MODIFIED"],
            removed_count=counts["REMOVED"],
            moved_count=counts["MOVED"],
            unchanged_count=unchanged_count,
            total_change_count=sum(counts.values()),
            summary_text=summary_text,
        )

    def mark_not_adjacent(
        self,
        *,
        cur: Any,
        specification_id: int,
        from_version_id: int | None,
        to_version_id: int | None,
    ) -> None:
        if from_version_id is None or to_version_id is None:
            return
        cur.execute(
            """
            UPDATE public.specification_change_sets
            SET is_adjacent = FALSE,
                updated_at = NOW()
            WHERE specification_id = %s
              AND from_version_id = %s
              AND to_version_id = %s
            """,
            (specification_id, from_version_id, to_version_id),
        )

    @classmethod
    def compare_sections(
        cls,
        old_sections: list[SectionSnapshot],
        new_sections: list[SectionSnapshot],
    ) -> tuple[list[ChangeRecord], int]:
        old_by_key = {cls._primary_key(item): item for item in old_sections}
        new_by_key = {cls._primary_key(item): item for item in new_sections}

        changes: list[ChangeRecord] = []
        unchanged_count = 0
        matched_old: set[str] = set()
        matched_new: set[str] = set()

        # First pass: exact structural identity.
        for key in sorted(set(old_by_key) & set(new_by_key)):
            old = old_by_key[key]
            new = new_by_key[key]
            matched_old.add(key)
            matched_new.add(key)
            if old.text_hash == new.text_hash:
                if cls._location_changed(old, new):
                    changes.append(
                        ChangeRecord(
                            change_type="MOVED",
                            entity_key=key,
                            old=old,
                            new=new,
                            similarity_score=1.0,
                            details=cls._change_details(old, new),
                        )
                    )
                else:
                    unchanged_count += 1
            else:
                ratio = cls._text_similarity(old.text, new.text)
                changes.append(
                    ChangeRecord(
                        change_type="MODIFIED",
                        entity_key=key,
                        old=old,
                        new=new,
                        similarity_score=ratio,
                        details=cls._change_details(old, new),
                    )
                )

        unmatched_old = [
            item for key, item in old_by_key.items() if key not in matched_old
        ]
        unmatched_new = [
            item for key, item in new_by_key.items() if key not in matched_new
        ]

        # Second pass: recover renamed/moved sections when parser IDs changed.
        fuzzy_pairs: list[tuple[float, int, int]] = []
        for oi, old in enumerate(unmatched_old):
            for ni, new in enumerate(unmatched_new):
                score = cls._fuzzy_score(old, new)
                if score >= 0.72:
                    fuzzy_pairs.append((score, oi, ni))
        fuzzy_pairs.sort(reverse=True)

        used_old: set[int] = set()
        used_new: set[int] = set()
        for score, oi, ni in fuzzy_pairs:
            if oi in used_old or ni in used_new:
                continue
            old = unmatched_old[oi]
            new = unmatched_new[ni]
            used_old.add(oi)
            used_new.add(ni)
            change_type = "MOVED" if old.text_hash == new.text_hash else "MODIFIED"
            changes.append(
                ChangeRecord(
                    change_type=change_type,
                    entity_key=cls._fallback_entity_key(old, new),
                    old=old,
                    new=new,
                    similarity_score=score,
                    details=cls._change_details(old, new),
                )
            )

        for oi, old in enumerate(unmatched_old):
            if oi in used_old:
                continue
            changes.append(
                ChangeRecord(
                    change_type="REMOVED",
                    entity_key=cls._primary_key(old),
                    old=old,
                    new=None,
                    similarity_score=None,
                    details=cls._change_details(old, None),
                )
            )

        for ni, new in enumerate(unmatched_new):
            if ni in used_new:
                continue
            changes.append(
                ChangeRecord(
                    change_type="ADDED",
                    entity_key=cls._primary_key(new),
                    old=None,
                    new=new,
                    similarity_score=None,
                    details=cls._change_details(None, new),
                )
            )

        order = {"MODIFIED": 0, "ADDED": 1, "REMOVED": 2, "MOVED": 3}
        changes.sort(
            key=lambda item: (
                order.get(item.change_type, 9),
                (item.new or item.old).sort_order if (item.new or item.old) else 0,
                item.entity_key,
            )
        )
        return changes, unchanged_count

    @staticmethod
    def _load_version(cur: Any, version_id: int) -> dict[str, Any]:
        cur.execute(
            """
            SELECT sv.id, sv.specification_id, sv.version_label, sv.document_id,
                   COALESCE(d.file_name, sv.document_id), COALESCE(d.metadata, '{}'::jsonb)
            FROM public.specification_versions sv
            LEFT JOIN public.documents d ON d.document_id = sv.document_id
            WHERE sv.id = %s
            """,
            (version_id,),
        )
        row = cur.fetchone()
        if not row:
            raise SpecificationChangeError(f"Specification version {version_id} does not exist.")
        file_name = str(row[4] or "")
        metadata = dict(row[5] or {})
        if not PrimarySpecificationDocumentPolicy.is_primary(file_name=file_name, metadata=metadata):
            raise SpecificationChangeError(
                f"Specification version {version_id} is a supporting/non-primary artefact "
                f"({file_name or row[3]}). Version comparison requires specification body documents."
            )
        return {
            "id": int(row[0]),
            "specification_id": int(row[1]),
            "version_label": str(row[2]),
            "document_id": str(row[3]),
            "file_name": file_name,
            "document_role": PrimarySpecificationDocumentPolicy.PRIMARY_ROLE,
        }

    @classmethod
    def _load_sections(cls, cur: Any, document_id: str) -> list[SectionSnapshot]:
        cur.execute(
            """
            SELECT
                s.section_id,
                s.chapter_id,
                s.parent_section_id,
                COALESCE(NULLIF(ch.title_jp, ''), ch.title_en),
                COALESCE(NULLIF(s.title_jp, ''), s.title_en),
                COALESCE(s.sort_order, 0),
                s.page_number
            FROM public.sections s
            LEFT JOIN public.chapters ch
              ON ch.document_id = s.document_id
             AND ch.chapter_id = s.chapter_id
            WHERE s.document_id = %s
            ORDER BY COALESCE(s.sort_order, 0), s.id
            """,
            (document_id,),
        )
        section_rows = cur.fetchall()

        cur.execute(
            """
            SELECT id, section_id, content
            FROM public.contents
            WHERE document_id = %s
            ORDER BY COALESCE(sort_order, 0), chunk_index, id
            """,
            (document_id,),
        )
        contents: dict[str, list[tuple[int, str]]] = {}
        for content_id, section_id, text in cur.fetchall():
            contents.setdefault(str(section_id), []).append((int(content_id), str(text or "")))

        snapshots: list[SectionSnapshot] = []
        for row in section_rows:
            section_id = str(row[0])
            section_contents = contents.get(section_id, [])
            text = "\n".join(item[1] for item in section_contents).strip()
            snapshots.append(
                SectionSnapshot(
                    section_id=section_id,
                    chapter_id=str(row[1]) if row[1] is not None else None,
                    parent_section_id=str(row[2]) if row[2] is not None else None,
                    chapter_title=str(row[3]) if row[3] is not None else None,
                    section_title=str(row[4]) if row[4] is not None else None,
                    sort_order=int(row[5] or 0),
                    page_number=int(row[6]) if row[6] is not None else None,
                    text=text,
                    content_ids=tuple(item[0] for item in section_contents),
                )
            )
        return snapshots

    def _load_existing_summary(
        self,
        cur: Any,
        *,
        specification_id: int,
        from_version_id: int,
        to_version_id: int,
    ) -> ChangeSummary | None:
        cur.execute(
            """
            SELECT
                cs.id,
                fv.version_label,
                tv.version_label,
                cs.added_count,
                cs.modified_count,
                cs.removed_count,
                cs.moved_count,
                cs.unchanged_count,
                cs.summary_text
            FROM public.specification_change_sets cs
            JOIN public.specification_versions fv ON fv.id = cs.from_version_id
            JOIN public.specification_versions tv ON tv.id = cs.to_version_id
            WHERE cs.specification_id = %s
              AND cs.from_version_id = %s
              AND cs.to_version_id = %s
              AND cs.algorithm_version = %s
              AND cs.status = 'READY'
            LIMIT 1
            """,
            (
                specification_id,
                from_version_id,
                to_version_id,
                DIFF_ALGORITHM_VERSION,
            ),
        )
        row = cur.fetchone()
        if not row:
            return None
        return ChangeSummary(
            change_set_id=int(row[0]),
            specification_id=specification_id,
            from_version_id=from_version_id,
            to_version_id=to_version_id,
            from_version_label=str(row[1]),
            to_version_label=str(row[2]),
            added_count=int(row[3] or 0),
            modified_count=int(row[4] or 0),
            removed_count=int(row[5] or 0),
            moved_count=int(row[6] or 0),
            unchanged_count=int(row[7] or 0),
            total_change_count=int(row[3] or 0) + int(row[4] or 0) + int(row[5] or 0) + int(row[6] or 0),
            summary_text=str(row[8] or ""),
        )

    @staticmethod
    def _insert_change(
        cur: Any,
        *,
        change_set_id: int,
        specification_id: int,
        from_version_id: int,
        to_version_id: int,
        change: ChangeRecord,
    ) -> None:
        old = change.old
        new = change.new
        cur.execute(
            """
            INSERT INTO public.specification_changes (
                change_set_id,
                specification_id,
                from_version_id,
                to_version_id,
                change_type,
                entity_type,
                entity_key,
                chapter_id,
                chapter_title,
                section_id,
                section_title,
                old_content_ids,
                new_content_ids,
                old_text,
                new_text,
                old_hash,
                new_hash,
                old_page_number,
                new_page_number,
                old_sort_order,
                new_sort_order,
                similarity_score,
                details,
                created_at
            )
            VALUES (
                %s, %s, %s, %s, %s, 'SECTION', %s,
                %s, %s, %s, %s,
                CAST(%s AS jsonb), CAST(%s AS jsonb),
                %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                CAST(%s AS jsonb), NOW()
            )
            """,
            (
                change_set_id,
                specification_id,
                from_version_id,
                to_version_id,
                change.change_type,
                change.entity_key,
                (new or old).chapter_id if (new or old) else None,
                (new or old).chapter_title if (new or old) else None,
                (new or old).section_id if (new or old) else None,
                (new or old).section_title if (new or old) else None,
                json.dumps(list(old.content_ids) if old else [], ensure_ascii=False),
                json.dumps(list(new.content_ids) if new else [], ensure_ascii=False),
                old.text if old else None,
                new.text if new else None,
                old.text_hash if old else None,
                new.text_hash if new else None,
                old.page_number if old else None,
                new.page_number if new else None,
                old.sort_order if old else None,
                new.sort_order if new else None,
                change.similarity_score,
                json.dumps(change.details, ensure_ascii=False, default=str),
            ),
        )

    @classmethod
    def _primary_key(cls, item: SectionSnapshot) -> str:
        chapter = cls._normalize_key_text(item.chapter_id or item.chapter_title or "ROOT")
        section = cls._normalize_key_text(item.section_id)
        if section:
            return f"{chapter}|{section}"
        title = cls._normalize_key_text(item.section_title or "UNTITLED")
        return f"{chapter}|TITLE:{title}"

    @classmethod
    def _fallback_entity_key(cls, old: SectionSnapshot, new: SectionSnapshot) -> str:
        raw = "|".join(
            [
                cls._primary_key(old),
                cls._primary_key(new),
                cls._normalize_key_text(new.section_title or old.section_title or ""),
            ]
        )
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
        return f"MATCH:{digest}"

    @staticmethod
    def _location_changed(old: SectionSnapshot, new: SectionSnapshot) -> bool:
        return (
            old.chapter_id != new.chapter_id
            or old.parent_section_id != new.parent_section_id
            or old.sort_order != new.sort_order
        )

    @classmethod
    def _fuzzy_score(cls, old: SectionSnapshot, new: SectionSnapshot) -> float:
        old_title = cls._normalize_key_text(old.section_title or "")
        new_title = cls._normalize_key_text(new.section_title or "")
        title_score = SequenceMatcher(None, old_title, new_title, autojunk=False).ratio() if (old_title or new_title) else 0.0

        old_text = cls._normalize_body(old.text)
        new_text = cls._normalize_body(new.text)
        text_score = cls._text_similarity(old_text, new_text) if (old_text or new_text) else 0.0

        # Content similarity dominates; titles make renames/moves recoverable.
        return max(text_score, (text_score * 0.75) + (title_score * 0.25))

    @classmethod
    def _change_details(
        cls,
        old: SectionSnapshot | None,
        new: SectionSnapshot | None,
    ) -> dict[str, Any]:
        # Keep the persisted diff bounded for very large spreadsheet/manual sections.
        # Full old/new section text is still stored separately for side-by-side review.
        old_lines = (old.text if old else "").splitlines()[:4000]
        new_lines = (new.text if new else "").splitlines()[:4000]
        matcher = SequenceMatcher(None, old_lines, new_lines, autojunk=False)
        removed: list[str] = []
        added: list[str] = []
        operations: list[dict[str, Any]] = []

        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            old_block = old_lines[i1:i2]
            new_block = new_lines[j1:j2]
            removed.extend(old_block)
            added.extend(new_block)
            if len(operations) < 100:
                operations.append(
                    {
                        "op": tag.upper(),
                        "old_start": i1 + 1,
                        "old_end": i2,
                        "new_start": j1 + 1,
                        "new_end": j2,
                        "old_lines": [line[:1000] for line in old_block[:40]],
                        "new_lines": [line[:1000] for line in new_block[:40]],
                    }
                )

        return {
            "old_section_id": old.section_id if old else None,
            "new_section_id": new.section_id if new else None,
            "old_title": old.section_title if old else None,
            "new_title": new.section_title if new else None,
            "removed_line_count": len(removed),
            "added_line_count": len(added),
            "removed_lines_preview": [line[:1000] for line in removed[:100]],
            "added_lines_preview": [line[:1000] for line in added[:100]],
            "operations": operations,
        }

    @staticmethod
    def _summary_text(from_label: str, to_label: str, counts: dict[str, int]) -> str:
        return (
            f"{from_label} → {to_label}: "
            f"+{counts['ADDED']} added, "
            f"~{counts['MODIFIED']} modified, "
            f"-{counts['REMOVED']} removed, "
            f"↔{counts['MOVED']} moved"
        )

    @staticmethod
    def _text_similarity(left: str, right: str) -> float:
        if left == right:
            return 1.0
        # SequenceMatcher is deterministic but quadratic in the worst case.
        # A bounded normalized sample keeps ingestion latency predictable while
        # preserving enough signal for section rename/move recovery.
        left_sample = str(left or "")[:12000]
        right_sample = str(right or "")[:12000]
        return SequenceMatcher(None, left_sample, right_sample, autojunk=False).ratio()

    @staticmethod
    def _normalize_key_text(value: str) -> str:
        text = unicodedata.normalize("NFKC", str(value or "")).upper()
        text = re.sub(r"\s+", " ", text).strip()
        return text

    @staticmethod
    def _normalize_body(value: str) -> str:
        text = unicodedata.normalize("NFKC", str(value or ""))
        text = re.sub(r"\s+", " ", text).strip()
        return text
