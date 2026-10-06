from __future__ import annotations

from typing import Any

from app.database.connection import DatabaseConnection
from app.storage.specification_change_manager import SpecificationChangeManager
from app.storage.primary_document_policy import PrimarySpecificationDocumentPolicy


class SpecificationHistoryService:
    """Read/compare service used by the desktop Version History page."""

    def __init__(self, database_connection: DatabaseConnection) -> None:
        self.db = database_connection
        self.change_manager = SpecificationChangeManager()

    def list_specifications(self) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        specification_id,
                        specification_key,
                        project_code,
                        region_scope,
                        spec_code,
                        spec_type,
                        spec_subtype,
                        canonical_name,
                        specification_status,
                        current_version,
                        current_version_id,
                        current_document_id,
                        current_version_status,
                        version_count,
                        history_count,
                        updated_at
                    FROM public.specification_catalog
                    ORDER BY canonical_name, specification_key
                    """
                )
                columns = [item.name for item in cur.description]
                return [dict(zip(columns, row)) for row in cur.fetchall()]

    def list_versions(self, specification_id: int) -> list[dict[str, Any]]:
        primary_d = PrimarySpecificationDocumentPolicy.sql_primary_predicate("d")
        with self.db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    WITH primary_versions AS (
                        SELECT
                            sv.id AS specification_version_id,
                            sv.specification_id,
                            sv.version_label,
                            sv.version_sort_key,
                            sv.version_seq,
                            sv.document_id,
                            sv.status,
                            sv.change_summary,
                            sv.created_at,
                            COALESCE(d.file_name, sv.document_id) AS file_name,
                            COALESCE(d.metadata, '{{}}'::jsonb) AS document_metadata
                        FROM public.specification_versions sv
                        LEFT JOIN public.documents d ON d.document_id = sv.document_id
                        WHERE sv.specification_id = %s
                          AND sv.branch_name = 'MAIN'
                          AND {primary_d}
                    ), ordered AS (
                        SELECT
                            pv.*,
                            LEAD(pv.specification_version_id) OVER (
                                ORDER BY pv.version_sort_key DESC NULLS LAST, pv.version_seq DESC, pv.specification_version_id DESC
                            ) AS previous_version_id,
                            LEAD(pv.version_label) OVER (
                                ORDER BY pv.version_sort_key DESC NULLS LAST, pv.version_seq DESC, pv.specification_version_id DESC
                            ) AS previous_version_label
                        FROM primary_versions pv
                    ), preferred AS (
                        SELECT pv.specification_version_id
                        FROM primary_versions pv
                        JOIN public.specifications sp ON sp.id = pv.specification_id
                        ORDER BY
                            CASE WHEN pv.specification_version_id = sp.current_version_id THEN 0 ELSE 1 END,
                            pv.version_sort_key DESC NULLS LAST, pv.version_seq DESC, pv.specification_version_id DESC
                        LIMIT 1
                    )
                    SELECT
                        o.specification_version_id,
                        o.specification_id,
                        o.version_label,
                        o.version_sort_key,
                        o.version_seq,
                        o.previous_version_id,
                        o.previous_version_label,
                        o.document_id,
                        o.status,
                        (o.specification_version_id = (SELECT specification_version_id FROM preferred)) AS is_current,
                        CASE
                            WHEN o.specification_version_id = (SELECT specification_version_id FROM preferred) THEN 'CURRENT'
                            ELSE 'HISTORY'
                        END AS version_role,
                        COALESCE(cs.added_count, 0) AS added_count,
                        COALESCE(cs.modified_count, 0) AS modified_count,
                        COALESCE(cs.removed_count, 0) AS removed_count,
                        COALESCE(cs.moved_count, 0) AS moved_count,
                        o.change_summary,
                        o.created_at,
                        o.file_name,
                        'PRIMARY_CONTENT'::text AS evidence_role
                    FROM ordered o
                    LEFT JOIN public.specification_change_sets cs
                      ON cs.specification_id = o.specification_id
                     AND cs.from_version_id = o.previous_version_id
                     AND cs.to_version_id = o.specification_version_id
                     AND cs.is_adjacent IS TRUE
                     AND cs.status = 'READY'
                     AND cs.algorithm_version = 'section-structural-v1'
                    ORDER BY o.version_sort_key DESC NULLS LAST, o.version_seq DESC, o.specification_version_id DESC
                    """,
                    (specification_id,),
                )
                columns = [item.name for item in cur.description]
                return [dict(zip(columns, row)) for row in cur.fetchall()]

    def list_change_timeline(self, specification_id: int) -> list[dict[str, Any]]:
        """Return adjacent READY body-to-body change sets in newest-to-oldest order."""
        primary_fd = PrimarySpecificationDocumentPolicy.sql_primary_predicate("fd")
        primary_td = PrimarySpecificationDocumentPolicy.sql_primary_predicate("td")
        with self.db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT
                        cs.id AS change_set_id,
                        cs.specification_id,
                        cs.from_version_id,
                        fv.version_label AS from_version_label,
                        cs.to_version_id,
                        tv.version_label AS to_version_label,
                        cs.added_count,
                        cs.modified_count,
                        cs.removed_count,
                        cs.moved_count,
                        cs.unchanged_count,
                        cs.summary_text,
                        cs.algorithm_version,
                        cs.created_at,
                        cs.updated_at
                    FROM public.specification_change_sets cs
                    JOIN public.specification_versions fv ON fv.id = cs.from_version_id
                    JOIN public.specification_versions tv ON tv.id = cs.to_version_id
                    LEFT JOIN public.documents fd ON fd.document_id = fv.document_id
                    LEFT JOIN public.documents td ON td.document_id = tv.document_id
                    WHERE cs.specification_id = %s
                      AND cs.is_adjacent IS TRUE
                      AND cs.status = 'READY'
                      AND cs.algorithm_version = 'section-structural-v1'
                      AND {primary_fd}
                      AND {primary_td}
                    ORDER BY tv.version_sort_key DESC NULLS LAST, tv.version_seq DESC
                    """,
                    (specification_id,),
                )
                columns = [item.name for item in cur.description]
                return [dict(zip(columns, row)) for row in cur.fetchall()]

    def list_change_details(self, change_set_id: int) -> list[dict[str, Any]]:
        """Return auditable change rows for one persisted comparison set."""
        with self.db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        id,
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
                        old_page_number,
                        new_page_number,
                        old_sort_order,
                        new_sort_order,
                        similarity_score,
                        old_text,
                        new_text,
                        details,
                        created_at
                    FROM public.specification_changes
                    WHERE change_set_id = %s
                    ORDER BY
                        CASE change_type
                            WHEN 'MODIFIED' THEN 1
                            WHEN 'ADDED' THEN 2
                            WHEN 'REMOVED' THEN 3
                            WHEN 'MOVED' THEN 4
                            ELSE 9
                        END,
                        COALESCE(new_sort_order, old_sort_order, 0),
                        id
                    """,
                    (change_set_id,),
                )
                columns = [item.name for item in cur.description]
                return [dict(zip(columns, row)) for row in cur.fetchall()]

    def compare_versions(
        self,
        *,
        specification_id: int,
        from_version_id: int,
        to_version_id: int,
        force: bool = False,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        with self.db.connect() as conn:
            try:
                with conn.cursor() as cur:
                    summary = self.change_manager.ensure_comparison(
                        cur=cur,
                        specification_id=specification_id,
                        from_version_id=from_version_id,
                        to_version_id=to_version_id,
                        is_adjacent=False,
                        force=force,
                    )
                    cur.execute(
                        """
                        SELECT
                            id,
                            change_type,
                            entity_type,
                            entity_key,
                            chapter_id,
                            chapter_title,
                            section_id,
                            section_title,
                            old_page_number,
                            new_page_number,
                            old_sort_order,
                            new_sort_order,
                            similarity_score,
                            old_text,
                            new_text,
                            details
                        FROM public.specification_changes
                        WHERE change_set_id = %s
                        ORDER BY
                            CASE change_type
                                WHEN 'MODIFIED' THEN 1
                                WHEN 'ADDED' THEN 2
                                WHEN 'REMOVED' THEN 3
                                WHEN 'MOVED' THEN 4
                                ELSE 9
                            END,
                            COALESCE(new_sort_order, old_sort_order, 0),
                            id
                        """,
                        (summary.change_set_id,),
                    )
                    columns = [item.name for item in cur.description]
                    changes = [dict(zip(columns, row)) for row in cur.fetchall()]
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        return (
            {
                "change_set_id": summary.change_set_id,
                "from_version_id": summary.from_version_id,
                "to_version_id": summary.to_version_id,
                "from_version_label": summary.from_version_label,
                "to_version_label": summary.to_version_label,
                "added_count": summary.added_count,
                "modified_count": summary.modified_count,
                "removed_count": summary.removed_count,
                "moved_count": summary.moved_count,
                "unchanged_count": summary.unchanged_count,
                "total_change_count": summary.total_change_count,
                "summary_text": summary.summary_text,
            },
            changes,
        )
