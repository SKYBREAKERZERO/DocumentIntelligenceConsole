from __future__ import annotations

import hashlib
import json
from typing import Any

from app.database.connection import DatabaseConnection
from app.model.document import Document
from app.analyzer.specification_version_resolver import SpecificationVersionResolver
from app.analyzer.supporting_document_policy import SupportingDocumentPolicy
from app.analyzer.import_resilience_policy import ImportResiliencePolicy
from app.normalizer.postgres_safe_sanitizer import PostgresSafeSanitizer
from app.project.project_registry import ProjectRegistry
from app.storage.project_database_router import ProjectDatabaseRouter
from app.storage.specification_version_manager import SpecificationVersionManager
from app.relations.manager import EngineeringRelationManager


class PostgresStorageError(RuntimeError):
    """PostgreSQL 存储层异常。"""


class PostgresStorage:
    """
    RAG-ready PostgreSQL persistence for Document Ingestion Platform.

    Public contract is intentionally unchanged:

        PostgresStorage(...).save(document) -> bool

    Design:
        documents -> chapters -> sections -> contents

    RAG-specific behavior:
        - contents.id remains stable when the same logical chunk is re-imported
        - logical chunk identity is (document_id, section_id, chunk_index)
        - content_hash is SHA-256 of the exact chunk text
        - changed chunk/payload metadata resets embedding_status to PENDING
        - unchanged chunks keep the existing embedding state
        - removed chunks are queued in vector_delete_queue before deletion
        - chapter-direct Content still uses <chapter_id>.ROOT synthetic sections
        - PostgreSQL remains the source of truth; Qdrant point ID should equal
          contents.id

    Transaction:
        A whole document save is atomic.
    """

    def __init__(
        self,
        database_connection: DatabaseConnection | None = None,
        *,
        project_code: str | None = None,
    ) -> None:
        self.project = (
            ProjectRegistry.resolve(project_code)
            if project_code is not None
            else None
        )

        if database_connection is not None:
            self.db = database_connection
        elif self.project is not None:
            _project, self.db = ProjectDatabaseRouter.create_connection(
                self.project.code
            )
        else:
            # Backward-compatible fallback for non-project-aware callers.
            # Production ingestion/JSON import now passes an explicit project.
            self.db = DatabaseConnection()

        self.specification_version_manager = SpecificationVersionManager(
            self.project.code if self.project is not None else None
        )
        self.engineering_relation_manager = EngineeringRelationManager()

    def save(self, document: Document) -> bool:
        self._validate_document(document)

        # Sanitize every persisted string before project/identity/hash validation.
        # PostgreSQL text/jsonb rejects U+0000 (seen in Canon/Adobe PDF metadata).
        # The sanitizer removes only DB-invalid characters and preserves business text.
        PostgresSafeSanitizer.sanitize_document(document)
        self._validate_project_assignment(document)

        # Normalize supporting-document identity even for JSON produced by older
        # releases. This prevents CoverSheet/CheckSheet artefacts from being
        # mistaken for another copy of the parent immutable SpecificationVersion.
        SupportingDocumentPolicy.apply(document)
        ImportResiliencePolicy.detach_primary_identity(document)

        # JSON produced by older versions may have project/spec taxonomy fields
        # but no stable specification_key/version metadata yet. Resolve it here
        # as a compatibility bridge before document_id is finalized. Supporting
        # documents intentionally bypass specification-version resolution.
        if (
            self.project is not None
            and self.project.uses_specification_taxonomy
            and not ImportResiliencePolicy.is_non_versioned(document)
        ):
            if not (document.metadata or {}).get("specification_key"):
                SpecificationVersionResolver(
                    project_code=self.project.code
                ).process(document)

        document_id = self._resolve_document_id(document)

        try:
            with self.db.connect() as conn:
                try:
                    with conn.cursor() as cur:
                        self._verify_project_database(cur)

                        version_plan = self.specification_version_manager.prepare(
                            cur=cur,
                            document=document,
                            document_id=document_id,
                        )

                        # Same-version localization handling may deliberately convert
                        # the incoming file to a non-primary TRANSLATION document and
                        # assign a collision-safe variant document_id. Re-resolve only
                        # after prepare() has made that deterministic decision.
                        planned_document_id = self._resolve_document_id(document)
                        if planned_document_id != document_id:
                            document_id = planned_document_id

                        # Exact same master/version/content is idempotent.  Do not
                        # touch the historical document snapshot again.  v5.2 may
                        # still backfill deterministic engineering relations when
                        # an older version has no current extraction run yet.
                        if version_plan is not None and version_plan.duplicate:
                            if version_plan.existing_version_id is not None and version_plan.existing_document_id:
                                relation_result = self._extract_engineering_relations_safe(
                                    cur=cur,
                                    document=document,
                                    specification_id=version_plan.specification_id,
                                    version_id=version_plan.existing_version_id,
                                    document_id=version_plan.existing_document_id,
                                )
                                if relation_result is not None:
                                    document.metadata["engineering_relation_summary"] = relation_result.summary_text
                                    document.metadata["engineering_relation_extraction_status"] = "READY"
                            conn.commit()
                            return True

                        self._save_document(
                            cur=cur,
                            document=document,
                            document_id=document_id,
                        )

                        chapter_ids = self._save_chapters(
                            cur=cur,
                            document=document,
                            document_id=document_id,
                        )

                        section_ids = self._save_sections(
                            cur=cur,
                            document=document,
                            document_id=document_id,
                            chapter_ids=chapter_ids,
                        )

                        root_section_ids = self._save_root_sections(
                            cur=cur,
                            document=document,
                            document_id=document_id,
                            chapter_ids=chapter_ids,
                            existing_section_ids=section_ids,
                        )
                        section_ids.update(root_section_ids)

                        # Upsert current chunks first.  IDs remain stable for the
                        # same logical chunk.  Stale chunks are queued/deleted
                        # inside this method before stale sections are removed.
                        self._replace_contents(
                            cur=cur,
                            document=document,
                            document_id=document_id,
                            chapter_ids=chapter_ids,
                            section_ids=section_ids,
                            delete_existing=False,
                        )

                        self._delete_stale_sections(
                            cur=cur,
                            document_id=document_id,
                            section_ids=section_ids,
                        )
                        self._delete_stale_chapters(
                            cur=cur,
                            document_id=document_id,
                            chapter_ids=chapter_ids,
                        )

                        version_id = self.specification_version_manager.finalize(
                            cur=cur,
                            plan=version_plan,
                            document_id=document_id,
                        )
                        if version_id is not None:
                            cur.execute(
                                "SELECT change_summary FROM public.specification_versions WHERE id = %s",
                                (version_id,),
                            )
                            change_row = cur.fetchone()
                            if change_row and change_row[0]:
                                document.metadata["specification_change_summary"] = str(change_row[0])

                            # Deterministic engineering relation extraction runs
                            # after PostgreSQL structure/chunks/version persistence
                            # but before any embedding/Qdrant worker can index the
                            # new content.  Failure is isolated with a savepoint so
                            # the immutable business version remains ingestible.
                            relation_result = self._extract_engineering_relations_safe(
                                cur=cur,
                                document=document,
                                specification_id=version_plan.specification_id if version_plan is not None else 0,
                                version_id=version_id,
                                document_id=document_id,
                            )
                            if relation_result is not None:
                                document.metadata["engineering_relation_summary"] = relation_result.summary_text
                                document.metadata["engineering_relation_extraction_status"] = "READY"
                                if self.project is not None:
                                    resolved = self.engineering_relation_manager.reconcile_unresolved(
                                        cur=cur, project_code=self.project.code
                                    )
                                    if resolved:
                                        document.metadata["engineering_relation_reconciled_count"] = resolved

                    conn.commit()

                except Exception:
                    conn.rollback()
                    raise

        except Exception as exc:
            context = self._document_length_context(
                document=document,
                document_id=document_id,
            )
            raise PostgresStorageError(
                f"Failed to save document '{document.file_name}' "
                f"to PostgreSQL: {exc}. {context}"
            ) from exc

        return True

    def _extract_engineering_relations_safe(
        self,
        *,
        cur: Any,
        document: Document,
        specification_id: int,
        version_id: int,
        document_id: str,
    ):
        """Run derived relation extraction without risking the base ingest tx."""
        if specification_id <= 0 or version_id <= 0:
            return None
        if self.project is None or not self.project.uses_specification_taxonomy:
            return None

        savepoint = "engineering_relation_extraction"
        cur.execute(f"SAVEPOINT {savepoint}")
        try:
            result = self.engineering_relation_manager.ensure_for_version(
                cur=cur,
                specification_id=specification_id,
                version_id=version_id,
                document_id=document_id,
            )
            cur.execute(f"RELEASE SAVEPOINT {savepoint}")
            return result
        except Exception as exc:
            cur.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            cur.execute(f"RELEASE SAVEPOINT {savepoint}")
            document.metadata["engineering_relation_extraction_status"] = "FAILED"
            document.metadata["engineering_relation_extraction_error"] = str(exc)[:1000]
            try:
                self.engineering_relation_manager.record_failure(
                    cur=cur,
                    specification_id=specification_id,
                    version_id=version_id,
                    document_id=document_id,
                    error_message=str(exc),
                )
            except Exception:
                # Relation extraction is derived/audit data.  Never let a
                # secondary failure roll back the immutable document version.
                pass
            return None

    # ==================================================
    # Project Guard
    # ==================================================

    def _validate_project_assignment(self, document: Document) -> None:
        if self.project is None:
            return

        metadata = dict(getattr(document, "metadata", None) or {})
        assigned = metadata.get("project_code")
        if assigned is None:
            raise PostgresStorageError(
                "Document project_code is missing. Select 21MM, 24MM, or Common "
                "in the GUI before processing."
            )

        actual = ProjectRegistry.resolve(str(assigned))
        if actual.code != self.project.code:
            raise PostgresStorageError(
                "Project/database routing mismatch: document is assigned to "
                f"{actual.display_name}, but storage targets "
                f"{self.project.display_name}."
            )

    def _verify_project_database(self, cur: Any) -> None:
        if self.project is None:
            return

        cur.execute("SELECT to_regclass('public.project_info')")
        row = cur.fetchone()
        if not row or row[0] is None:
            raise PostgresStorageError(
                "project_info table is missing. Initialize/upgrade the selected "
                "project database before importing documents."
            )

        cur.execute(
            """
            SELECT project_code, project_name
            FROM public.project_info
            ORDER BY created_at ASC
            LIMIT 1
            """
        )
        identity = cur.fetchone()
        if not identity:
            raise PostgresStorageError(
                "project_info is empty. Initialize/upgrade the selected project "
                "database before importing documents."
            )

        database_project = ProjectRegistry.resolve(str(identity[0]))
        if database_project.code != self.project.code:
            raise PostgresStorageError(
                "Project database identity mismatch: selected project is "
                f"{self.project.display_name}, but the target database is bound "
                f"to {database_project.display_name}."
            )

    # ==================================================
    # Document
    # ==================================================

    @staticmethod
    def _save_document(
        *,
        cur: Any,
        document: Document,
        document_id: str,
    ) -> None:
        metadata, _ = PostgresSafeSanitizer.sanitize_value(dict(getattr(document, "metadata", None) or {}))
        file_name = PostgresStorage._safe_text(document.file_name).strip()
        file_type = PostgresStorage._safe_text(document.file_type).strip()

        language = metadata.get("language")
        language_json = (
            PostgresStorage._json_text(language)
            if language is not None
            else None
        )
        metadata_json = PostgresStorage._json_text(metadata)

        source_hash = (
            metadata.get("source_hash")
            or metadata.get("file_hash")
            or metadata.get("sha256")
        )

        cur.execute(
            """
            INSERT INTO documents
            (
                document_id,
                file_name,
                file_type,
                title,
                module,
                document_type,
                version,
                company,
                category,
                project_code,
                project_name,
                project_assignment_source,
                series,
                region_scope,
                spec_type,
                spec_subtype,
                source_file,
                language,
                source_hash,
                metadata,
                updated_at
            )
            VALUES
            (
                %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                CAST(%s AS jsonb), %s,
                CAST(%s AS jsonb), NOW()
            )
            ON CONFLICT (document_id)
            DO UPDATE SET
                file_name = EXCLUDED.file_name,
                file_type = EXCLUDED.file_type,
                title = EXCLUDED.title,
                module = EXCLUDED.module,
                document_type = EXCLUDED.document_type,
                version = EXCLUDED.version,
                company = EXCLUDED.company,
                category = EXCLUDED.category,
                project_code = EXCLUDED.project_code,
                project_name = EXCLUDED.project_name,
                project_assignment_source = EXCLUDED.project_assignment_source,
                series = EXCLUDED.series,
                region_scope = EXCLUDED.region_scope,
                spec_type = EXCLUDED.spec_type,
                spec_subtype = EXCLUDED.spec_subtype,
                source_file = EXCLUDED.source_file,
                language = EXCLUDED.language,
                source_hash = EXCLUDED.source_hash,
                metadata = EXCLUDED.metadata,
                updated_at = NOW()
            """,
            (
                document_id,
                file_name,
                file_type,
                file_name,  # compatibility with the legacy schema/UI
                file_type,  # compatibility with the legacy schema/UI
                metadata.get("document_type") or file_type,
                metadata.get("version"),
                metadata.get("company"),
                metadata.get("category"),
                metadata.get("project_code"),
                metadata.get("project_name"),
                metadata.get("project_assignment_source"),
                metadata.get("series"),
                metadata.get("region_scope"),
                metadata.get("spec_type"),
                metadata.get("spec_subtype"),
                metadata.get("source_file") or file_name,
                language_json,
                str(source_hash) if source_hash is not None else None,
                metadata_json,
            ),
        )

    # ==================================================
    # Chapters
    # ==================================================

    @staticmethod
    def _save_chapters(
        *,
        cur: Any,
        document: Document,
        document_id: str,
    ) -> set[str]:
        chapter_ids: set[str] = set()

        for fallback_order, chapter in enumerate(document.chapters, start=1):
            chapter_id = str(chapter.id).strip()
            if not chapter_id or chapter_id in chapter_ids:
                continue
            chapter_ids.add(chapter_id)

            sort_order = getattr(chapter, "sort_order", None)
            if sort_order is None:
                sort_order = fallback_order

            metadata_json = PostgresStorage._json_text(
                getattr(chapter, "metadata", None) or {}
            )

            cur.execute(
                """
                INSERT INTO chapters
                (
                    document_id,
                    chapter_id,
                    title_jp,
                    title_en,
                    sort_order,
                    metadata,
                    updated_at
                )
                VALUES (%s, %s, %s, %s, %s, CAST(%s AS jsonb), NOW())
                ON CONFLICT (document_id, chapter_id)
                DO UPDATE SET
                    title_jp = EXCLUDED.title_jp,
                    title_en = EXCLUDED.title_en,
                    sort_order = EXCLUDED.sort_order,
                    metadata = EXCLUDED.metadata,
                    updated_at = NOW()
                """,
                (
                    document_id,
                    chapter_id,
                    getattr(chapter, "title_jp", None),
                    getattr(chapter, "title_en", None),
                    int(sort_order),
                    metadata_json,
                ),
            )

        return chapter_ids

    # ==================================================
    # Sections
    # ==================================================

    @staticmethod
    def _save_sections(
        *,
        cur: Any,
        document: Document,
        document_id: str,
        chapter_ids: set[str],
    ) -> set[str]:
        """Two-pass section upsert so parent links are order independent."""

        records: list[dict[str, Any]] = []
        section_ids: set[str] = set()

        for fallback_order, section in enumerate(document.sections, start=1):
            section_id = str(section.id).strip()
            if not section_id or section_id in section_ids:
                continue

            chapter_id = getattr(section, "chapter_id", None)
            if not chapter_id:
                chapter_id = section_id.split(".", maxsplit=1)[0]
            chapter_id = str(chapter_id).strip()
            if chapter_id not in chapter_ids:
                continue

            parent_section_id = getattr(section, "parent_section_id", None)
            if parent_section_id:
                parent_section_id = str(parent_section_id).strip()
            if not parent_section_id or parent_section_id == section_id:
                parent_section_id = None

            sort_order = getattr(section, "sort_order", None)
            if sort_order is None:
                sort_order = fallback_order

            records.append(
                {
                    "section_id": section_id,
                    "chapter_id": chapter_id,
                    "parent_section_id": parent_section_id,
                    "title_jp": getattr(section, "title_jp", None),
                    "title_en": getattr(section, "title_en", None),
                    "level": int(getattr(section, "level", 2) or 2),
                    "sort_order": int(sort_order),
                    "page_number": PostgresStorage._optional_int(
                        getattr(section, "page_number", None)
                    ),
                    "metadata": PostgresStorage._json_text(
                        getattr(section, "metadata", None) or {}
                    ),
                }
            )
            section_ids.add(section_id)

        chapter_by_section = {
            record["section_id"]: record["chapter_id"] for record in records
        }

        # Pass 1: all nodes, no parent link yet.
        for record in records:
            cur.execute(
                """
                INSERT INTO sections
                (
                    document_id,
                    chapter_id,
                    section_id,
                    parent_section_id,
                    title_jp,
                    title_en,
                    level,
                    sort_order,
                    page_number,
                    metadata,
                    updated_at
                )
                VALUES
                (
                    %s, %s, %s, NULL, %s, %s, %s, %s, %s,
                    CAST(%s AS jsonb), NOW()
                )
                ON CONFLICT (document_id, section_id)
                DO UPDATE SET
                    chapter_id = EXCLUDED.chapter_id,
                    parent_section_id = NULL,
                    title_jp = EXCLUDED.title_jp,
                    title_en = EXCLUDED.title_en,
                    level = EXCLUDED.level,
                    sort_order = EXCLUDED.sort_order,
                    page_number = EXCLUDED.page_number,
                    metadata = EXCLUDED.metadata,
                    updated_at = NOW()
                """,
                (
                    document_id,
                    record["chapter_id"],
                    record["section_id"],
                    record["title_jp"],
                    record["title_en"],
                    record["level"],
                    record["sort_order"],
                    record["page_number"],
                    record["metadata"],
                ),
            )

        # Pass 2: restore valid same-chapter parent links.
        for record in records:
            parent_section_id = record["parent_section_id"]
            if not parent_section_id:
                continue
            if (
                parent_section_id not in section_ids
                or chapter_by_section.get(parent_section_id)
                != record["chapter_id"]
            ):
                continue

            cur.execute(
                """
                UPDATE sections
                SET parent_section_id = %s,
                    updated_at = NOW()
                WHERE document_id = %s
                  AND section_id = %s
                """,
                (
                    parent_section_id,
                    document_id,
                    record["section_id"],
                ),
            )

        return section_ids

    @staticmethod
    def _save_root_sections(
        *,
        cur: Any,
        document: Document,
        document_id: str,
        chapter_ids: set[str],
        existing_section_ids: set[str],
    ) -> set[str]:
        required_root_ids: dict[str, str] = {}

        for content in document.contents:
            raw_section_id = getattr(content, "section_id", None)
            if raw_section_id:
                continue

            chapter_id = getattr(content, "chapter_id", None)
            if not chapter_id:
                continue
            chapter_id = str(chapter_id).strip()
            if chapter_id not in chapter_ids:
                continue

            root_section_id = f"{chapter_id}.ROOT"
            if root_section_id in existing_section_ids:
                continue
            required_root_ids[root_section_id] = chapter_id

        for root_section_id, chapter_id in required_root_ids.items():
            cur.execute(
                """
                INSERT INTO sections
                (
                    document_id,
                    chapter_id,
                    section_id,
                    parent_section_id,
                    title_jp,
                    title_en,
                    level,
                    sort_order,
                    page_number,
                    metadata,
                    updated_at
                )
                VALUES
                (
                    %s, %s, %s, NULL, NULL, NULL, 1, 0, NULL,
                    '{}'::jsonb, NOW()
                )
                ON CONFLICT (document_id, section_id)
                DO UPDATE SET
                    chapter_id = EXCLUDED.chapter_id,
                    parent_section_id = NULL,
                    level = 1,
                    sort_order = 0,
                    updated_at = NOW()
                """,
                (document_id, chapter_id, root_section_id),
            )

        return set(required_root_ids)

    # ==================================================
    # Contents / stable Chunk IDs
    # ==================================================

    @staticmethod
    def _delete_existing_contents(
        *,
        cur: Any,
        document_id: str,
    ) -> None:
        """
        Compatibility helper.

        Full delete is intentionally no longer used by save(); deleting every
        row would change contents.id on each import and break stable Qdrant
        point IDs.  If called directly, rows are queued for Qdrant deletion.
        """
        cur.execute(
            """
            SELECT id
            FROM contents
            WHERE document_id = %s
            ORDER BY id
            """,
            (document_id,),
        )
        ids = [int(row[0]) for row in cur.fetchall()]
        PostgresStorage._queue_vector_deletions(
            cur=cur,
            document_id=document_id,
            content_ids=ids,
            reason="DOCUMENT_CONTENT_REPLACE",
        )
        if ids:
            cur.execute("DELETE FROM contents WHERE id = ANY(%s)", (ids,))

    @staticmethod
    def _replace_contents(
        *,
        cur: Any,
        document: Document,
        document_id: str,
        chapter_ids: set[str],
        section_ids: set[str],
        delete_existing: bool = True,
    ) -> None:
        """
        Upsert the latest logical chunks while preserving contents.id.

        `delete_existing` is retained for private-interface compatibility but
        no longer causes a destructive delete-before-insert.  Stale rows are
        deleted only after the current logical chunk set has been upserted.
        """
        del delete_existing

        records = PostgresStorage._build_content_records(
            document=document,
            chapter_ids=chapter_ids,
            section_ids=section_ids,
        )
        current_keys = {
            (record["section_id"], record["chunk_index"])
            for record in records
        }

        cur.execute(
            """
            SELECT id, section_id, chunk_index
            FROM contents
            WHERE document_id = %s
            ORDER BY id
            """,
            (document_id,),
        )
        existing_rows = [
            (int(row[0]), str(row[1]), int(row[2])) for row in cur.fetchall()
        ]

        for record in records:
            cur.execute(
                """
                INSERT INTO contents
                (
                    document_id,
                    chapter_id,
                    section_id,
                    content,
                    page_number,
                    chunk_index,
                    token_count,
                    sort_order,
                    metadata,
                    content_hash,
                    embedding_status,
                    embedding_retry_count,
                    updated_at
                )
                VALUES
                (
                    %s, %s, %s, %s, %s, %s, %s, %s,
                    CAST(%s AS jsonb), %s, 'PENDING', 0, NOW()
                )
                ON CONFLICT (document_id, section_id, chunk_index)
                DO UPDATE SET
                    chapter_id = EXCLUDED.chapter_id,
                    content = EXCLUDED.content,
                    page_number = EXCLUDED.page_number,
                    token_count = EXCLUDED.token_count,
                    sort_order = EXCLUDED.sort_order,
                    metadata = EXCLUDED.metadata,
                    content_hash = EXCLUDED.content_hash,
                    embedding_status = CASE
                        WHEN contents.content_hash IS DISTINCT FROM EXCLUDED.content_hash
                          OR contents.chapter_id IS DISTINCT FROM EXCLUDED.chapter_id
                          OR contents.page_number IS DISTINCT FROM EXCLUDED.page_number
                          OR contents.token_count IS DISTINCT FROM EXCLUDED.token_count
                          OR contents.sort_order IS DISTINCT FROM EXCLUDED.sort_order
                        THEN 'PENDING'
                        ELSE contents.embedding_status
                    END,
                    embedding_started_at = CASE
                        WHEN contents.content_hash IS DISTINCT FROM EXCLUDED.content_hash
                          OR contents.chapter_id IS DISTINCT FROM EXCLUDED.chapter_id
                          OR contents.page_number IS DISTINCT FROM EXCLUDED.page_number
                          OR contents.token_count IS DISTINCT FROM EXCLUDED.token_count
                          OR contents.sort_order IS DISTINCT FROM EXCLUDED.sort_order
                        THEN NULL
                        ELSE contents.embedding_started_at
                    END,
                    embedded_at = CASE
                        WHEN contents.content_hash IS DISTINCT FROM EXCLUDED.content_hash
                          OR contents.chapter_id IS DISTINCT FROM EXCLUDED.chapter_id
                          OR contents.page_number IS DISTINCT FROM EXCLUDED.page_number
                          OR contents.token_count IS DISTINCT FROM EXCLUDED.token_count
                          OR contents.sort_order IS DISTINCT FROM EXCLUDED.sort_order
                        THEN NULL
                        ELSE contents.embedded_at
                    END,
                    embedded_content_hash = CASE
                        WHEN contents.content_hash IS DISTINCT FROM EXCLUDED.content_hash
                        THEN NULL
                        ELSE contents.embedded_content_hash
                    END,
                    embedding_error = CASE
                        WHEN contents.content_hash IS DISTINCT FROM EXCLUDED.content_hash
                          OR contents.chapter_id IS DISTINCT FROM EXCLUDED.chapter_id
                          OR contents.page_number IS DISTINCT FROM EXCLUDED.page_number
                          OR contents.token_count IS DISTINCT FROM EXCLUDED.token_count
                          OR contents.sort_order IS DISTINCT FROM EXCLUDED.sort_order
                        THEN NULL
                        ELSE contents.embedding_error
                    END,
                    embedding_retry_count = CASE
                        WHEN contents.content_hash IS DISTINCT FROM EXCLUDED.content_hash
                          OR contents.chapter_id IS DISTINCT FROM EXCLUDED.chapter_id
                          OR contents.page_number IS DISTINCT FROM EXCLUDED.page_number
                          OR contents.token_count IS DISTINCT FROM EXCLUDED.token_count
                          OR contents.sort_order IS DISTINCT FROM EXCLUDED.sort_order
                        THEN 0
                        ELSE contents.embedding_retry_count
                    END,
                    updated_at = NOW()
                """,
                (
                    document_id,
                    record["chapter_id"],
                    record["section_id"],
                    record["content"],
                    record["page_number"],
                    record["chunk_index"],
                    record["token_count"],
                    record["sort_order"],
                    record["metadata"],
                    record["content_hash"],
                ),
            )

        stale_ids = [
            row_id
            for row_id, section_id, chunk_index in existing_rows
            if (section_id, chunk_index) not in current_keys
        ]

        PostgresStorage._queue_vector_deletions(
            cur=cur,
            document_id=document_id,
            content_ids=stale_ids,
            reason="STALE_CONTENT",
        )
        if stale_ids:
            cur.execute("DELETE FROM contents WHERE id = ANY(%s)", (stale_ids,))

    @staticmethod
    def _build_content_records(
        *,
        document: Document,
        chapter_ids: set[str],
        section_ids: set[str],
    ) -> list[dict[str, Any]]:
        used_content_keys: set[tuple[str, int]] = set()
        section_chunk_counters: dict[str, int] = {}
        records: list[dict[str, Any]] = []

        for fallback_order, content in enumerate(document.contents, start=1):
            text = PostgresStorage._safe_text(getattr(content, "text", "") or "").strip()
            if not text:
                continue

            chapter_id = getattr(content, "chapter_id", None)
            chapter_id = str(chapter_id).strip() if chapter_id else None

            raw_section_id = getattr(content, "section_id", None)
            if raw_section_id:
                section_id = str(raw_section_id).strip()
                if section_id not in section_ids:
                    continue
            elif chapter_id and chapter_id in chapter_ids:
                section_id = f"{chapter_id}.ROOT"
            else:
                continue

            if chapter_id not in chapter_ids:
                # Derive from section identifier only as a conservative fallback.
                candidate = section_id.split(".", maxsplit=1)[0]
                chapter_id = candidate if candidate in chapter_ids else None
            if chapter_id is None:
                continue

            chunk_index = getattr(content, "chunk_index", None)
            if chunk_index is None:
                chunk_index = section_chunk_counters.get(section_id, 0)
            chunk_index = int(chunk_index)

            key = (section_id, chunk_index)
            if key in used_content_keys:
                chunk_index = section_chunk_counters.get(section_id, 0)
                while (section_id, chunk_index) in used_content_keys:
                    chunk_index += 1
                key = (section_id, chunk_index)

            used_content_keys.add(key)
            section_chunk_counters[section_id] = chunk_index + 1

            token_count = getattr(content, "token_count", 0)
            token_count = int(token_count or 0)

            sort_order = getattr(content, "sort_order", None)
            if sort_order is None:
                sort_order = fallback_order

            records.append(
                {
                    "chapter_id": chapter_id,
                    "section_id": section_id,
                    "content": text,
                    "page_number": PostgresStorage._optional_int(
                        getattr(content, "page_number", None)
                    ),
                    "chunk_index": chunk_index,
                    "token_count": token_count,
                    "sort_order": int(sort_order),
                    "metadata": PostgresStorage._json_text(
                        getattr(content, "metadata", None) or {}
                    ),
                    "content_hash": PostgresStorage._content_hash(text),
                }
            )

        return records

    @staticmethod
    def _queue_vector_deletions(
        *,
        cur: Any,
        document_id: str,
        content_ids: list[int],
        reason: str,
    ) -> None:
        if not content_ids:
            return

        # Keep compatibility with a database that has not yet been upgraded.
        cur.execute("SELECT to_regclass('public.vector_delete_queue')")
        row = cur.fetchone()
        if not row or row[0] is None:
            return

        for content_id in content_ids:
            cur.execute(
                """
                INSERT INTO vector_delete_queue
                (
                    content_id,
                    document_id,
                    reason,
                    queued_at,
                    processed_at,
                    last_error
                )
                VALUES (%s, %s, %s, NOW(), NULL, NULL)
                ON CONFLICT (content_id)
                DO UPDATE SET
                    document_id = EXCLUDED.document_id,
                    reason = EXCLUDED.reason,
                    queued_at = NOW(),
                    processed_at = NULL,
                    last_error = NULL
                """,
                (content_id, document_id, reason),
            )

    # ==================================================
    # Stale structure cleanup
    # ==================================================

    @staticmethod
    def _delete_stale_sections(
        *,
        cur: Any,
        document_id: str,
        section_ids: set[str],
    ) -> None:
        if not section_ids:
            cur.execute("DELETE FROM sections WHERE document_id = %s", (document_id,))
            return

        cur.execute(
            """
            DELETE FROM sections
            WHERE document_id = %s
              AND NOT (section_id = ANY(%s))
            """,
            (document_id, list(section_ids)),
        )

    @staticmethod
    def _delete_stale_chapters(
        *,
        cur: Any,
        document_id: str,
        chapter_ids: set[str],
    ) -> None:
        if not chapter_ids:
            cur.execute("DELETE FROM chapters WHERE document_id = %s", (document_id,))
            return

        cur.execute(
            """
            DELETE FROM chapters
            WHERE document_id = %s
              AND NOT (chapter_id = ANY(%s))
            """,
            (document_id, list(chapter_ids)),
        )

    # ==================================================
    # Helpers
    # ==================================================

    @staticmethod
    def _content_hash(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    @staticmethod
    def _json_text(value: Any) -> str:
        cleaned, _ = PostgresSafeSanitizer.sanitize_value(value)
        return json.dumps(
            cleaned,
            ensure_ascii=False,
            default=str,
            separators=(",", ":"),
        )

    @staticmethod
    def _safe_text(value: Any) -> str:
        cleaned, _, _ = PostgresSafeSanitizer.sanitize_text(str(value or ""))
        return cleaned

    @staticmethod
    def _optional_int(value: Any) -> int | None:
        if value is None or value == "":
            return None
        return int(value)

    @staticmethod
    def _document_length_context(
        *,
        document: Document,
        document_id: str,
    ) -> str:
        return (
            "field_lengths="
            f"document_id:{len(document_id)}, "
            f"title:{len(document.file_name)}, "
            f"module:{len(document.file_type)}"
        )

    @staticmethod
    def _resolve_document_id(document: Document) -> str:
        metadata_document_id = (
            document.metadata.get("document_id") if document.metadata else None
        )
        document_id = metadata_document_id or document.file_name
        document_id = PostgresStorage._safe_text(document_id).strip()
        if not document_id:
            raise ValueError("document_id cannot be empty.")
        return document_id

    @staticmethod
    def _validate_document(document: Document) -> None:
        if document is None:
            raise ValueError("Document cannot be None.")
        if not isinstance(document, Document):
            raise TypeError(
                "PostgresStorage expects an app.model.document.Document instance."
            )
        if not document.file_name.strip():
            raise ValueError("Document file_name cannot be empty.")
        if not document.file_type.strip():
            raise ValueError("Document file_type cannot be empty.")
