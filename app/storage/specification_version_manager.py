from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from app.model.document import Document
from app.project.project_registry import ProjectRegistry
from app.storage.specification_change_manager import SpecificationChangeManager
from app.storage.primary_document_policy import PrimarySpecificationDocumentPolicy
from app.analyzer.supporting_document_policy import SupportingDocumentPolicy
from app.analyzer.import_resilience_policy import ImportResiliencePolicy
from app.analyzer.document_variant_policy import DocumentVariantPolicy
from app.normalizer.postgres_safe_sanitizer import PostgresSafeSanitizer


class SpecificationVersionError(RuntimeError):
    """Base error for specification master/version persistence."""


class SpecificationVersionConflictError(SpecificationVersionError):
    """Raised when the same version label is re-used with different content."""


@dataclass(frozen=True, slots=True)
class SpecificationVersionPlan:
    specification_id: int
    specification_key: str
    version_label: str
    version_seq: int
    version_sort_key: str | None
    previous_version_id: int | None
    next_version_id: int | None
    content_hash: str
    status: str
    version_source: str | None
    promote_to_current: bool
    duplicate: bool = False
    existing_version_id: int | None = None
    existing_document_id: str | None = None


class SpecificationVersionManager:
    """Transactional manager for specification master/current/history.

    A Specification is stable identity.  A SpecificationVersion is immutable.
    Current is represented only by specifications.current_version_id; version
    rows do not carry a mutable is_current flag.
    """

    _NUMERIC_VERSION = re.compile(
        r"^[vV]?(?P<body>\d+(?:[.-]\d+)+)(?P<suffix>[A-Za-z]?)$"
    )
    _REV_VERSION = re.compile(
        r"^(?:rev|revision)[.\-_ ]*(?P<body>[A-Za-z0-9]+)$",
        re.IGNORECASE,
    )
    _R_VERSION = re.compile(r"^[rR](?P<body>\d{2,4})$")

    def __init__(self, project_code: str | None) -> None:
        self.project = ProjectRegistry.resolve(project_code) if project_code else None
        self.change_manager = SpecificationChangeManager()

    def prepare(
        self,
        *,
        cur: Any,
        document: Document,
        document_id: str,
    ) -> SpecificationVersionPlan | None:
        metadata = dict(document.metadata or {})

        # Supporting/metadata-only/translation artefacts are stored as auditable
        # documents but never create or replace immutable business versions.
        if ImportResiliencePolicy.is_non_versioned(document):
            return None

        project = self.project
        if project is None and metadata.get("project_code"):
            project = ProjectRegistry.resolve(str(metadata["project_code"]))

        if project is None or not project.uses_specification_taxonomy:
            return None

        specification_key = str(metadata.get("specification_key") or "").strip()
        canonical_name = str(
            metadata.get("specification_name") or document.file_name
        ).strip()
        if not specification_key:
            raise SpecificationVersionError(
                "specification_key is missing. Reprocess the file with the current "
                "SpecificationClassifier before saving to PostgreSQL."
            )

        content_hash = self.document_content_hash(document)
        version_label = str(metadata.get("specification_version") or "").strip()
        version_source = metadata.get("specification_version_source")
        status = "APPROVED"
        if not version_label:
            version_label = f"UNVERSIONED-{content_hash[:12]}"
            version_source = "content_hash_fallback"
            status = "UNRESOLVED"

        # document_id is also an immutable version identity in the database
        # (uq_spec_versions_document).  Check it before creating/updating the
        # specification master so a retry or classifier drift cannot leak a
        # raw PostgreSQL duplicate-key error or leave an orphan master row.
        cur.execute(
            """
            SELECT
                sv.id,
                sv.specification_id,
                s.specification_key,
                sv.version_label,
                sv.content_hash,
                sv.document_id
            FROM public.specification_versions sv
            JOIN public.specifications s
              ON s.id = sv.specification_id
            WHERE sv.document_id = %s
            LIMIT 1
            """,
            (document_id,),
        )
        existing_document_version = cur.fetchone()
        if existing_document_version:
            (
                existing_version_id,
                existing_specification_id,
                existing_specification_key,
                existing_version_label,
                existing_content_hash,
                existing_document_id,
            ) = existing_document_version

            if str(existing_content_hash or "") == content_hash:
                incoming_key = specification_key
                incoming_label = version_label
                persisted_key = str(existing_specification_key or "")
                persisted_label = str(existing_version_label or "")

                # Preserve the authoritative persisted identity on idempotent
                # re-import.  If newer classification logic would assign a
                # different identity, surface it as an audit warning instead
                # of attempting a second immutable version row.
                document.metadata["specification_key"] = persisted_key
                document.metadata["specification_version"] = persisted_label
                document.metadata["specification_version_status"] = "IDEMPOTENT_DOCUMENT_REUSE"
                if incoming_key != persisted_key or incoming_label != persisted_label:
                    warnings = list(document.metadata.get("import_warning_codes") or [])
                    code = "DOCUMENT_VERSION_IDENTITY_REUSED"
                    if code not in warnings:
                        warnings.append(code)
                    document.metadata["import_warning_codes"] = warnings
                    document.metadata["import_status"] = "IMPORTED_WITH_WARNING"
                    document.metadata["version_identity_reuse"] = {
                        "incoming_specification_key": incoming_key,
                        "incoming_version_label": incoming_label,
                        "persisted_specification_key": persisted_key,
                        "persisted_version_label": persisted_label,
                    }

                return SpecificationVersionPlan(
                    specification_id=int(existing_specification_id),
                    specification_key=persisted_key,
                    version_label=persisted_label,
                    version_seq=0,
                    version_sort_key=self.version_sort_key(persisted_label),
                    previous_version_id=None,
                    next_version_id=None,
                    content_hash=content_hash,
                    status=status,
                    version_source=self._optional_text(version_source),
                    promote_to_current=False,
                    duplicate=True,
                    existing_version_id=int(existing_version_id),
                    existing_document_id=str(existing_document_id),
                )

            raise SpecificationVersionConflictError(
                "Specification version conflict: document_id "
                f"'{document_id}' is already bound to immutable "
                f"specification_key={existing_specification_key}, "
                f"version={existing_version_label}, but the incoming content "
                "hash is different. Re-import the unchanged source for an "
                "idempotent retry, or assign a new version label/document "
                "identity for genuinely changed content."
            )

        specification_id, current_version_id = self._upsert_master(
            cur=cur,
            project_code=project.code,
            specification_key=specification_key,
            spec_code=self._optional_text(metadata.get("specification_code")),
            region_scope=self._optional_text(metadata.get("region_scope")),
            spec_type=self._optional_text(metadata.get("spec_type")),
            spec_subtype=self._optional_text(metadata.get("spec_subtype")),
            canonical_name=canonical_name,
            metadata=metadata,
        )
        cur.execute(
            f"""
            SELECT
                sv.id,
                sv.document_id,
                sv.content_hash,
                COALESCE(d.file_name, sv.document_id),
                COALESCE(d.metadata, '{{}}'::jsonb)
            FROM public.specification_versions sv
            LEFT JOIN public.documents d
              ON d.document_id = sv.document_id
            WHERE sv.specification_id = %s
              AND sv.branch_name = 'MAIN'
              AND sv.version_label = %s
              AND {PrimarySpecificationDocumentPolicy.sql_primary_predicate("d")}
            LIMIT 1
            """,
            (specification_id, version_label),
        )
        existing = cur.fetchone()
        if existing:
            existing_id, existing_document_id, existing_hash, existing_file_name, existing_metadata = existing
            if str(existing_hash or "") == content_hash:
                return SpecificationVersionPlan(
                    specification_id=int(specification_id),
                    specification_key=specification_key,
                    version_label=version_label,
                    version_seq=0,
                    version_sort_key=self.version_sort_key(version_label),
                    previous_version_id=current_version_id,
                    next_version_id=None,
                    content_hash=content_hash,
                    status=status,
                    version_source=self._optional_text(version_source),
                    promote_to_current=False,
                    duplicate=True,
                    existing_version_id=int(existing_id),
                    existing_document_id=str(existing_document_id),
                )

            # Do not weaken the immutable-version guard. Only a clearly signalled
            # language/localization pair is accepted as a non-primary variant.
            if DocumentVariantPolicy.is_translation_pair(
                existing_file_name=str(existing_file_name or existing_document_id),
                incoming_file_name=document.file_name,
                existing_metadata=dict(existing_metadata or {}),
                incoming_metadata=metadata,
            ):
                DocumentVariantPolicy.apply_translation_variant(
                    document,
                    specification_key=specification_key,
                    specification_code=self._optional_text(metadata.get("specification_code")),
                    specification_name=self._optional_text(metadata.get("specification_name")),
                    version_label=version_label,
                    primary_document_id=str(existing_document_id),
                    primary_file_name=str(existing_file_name or existing_document_id),
                    primary_metadata=dict(existing_metadata or {}),
                    content_hash=content_hash,
                )
                return None

            raise SpecificationVersionConflictError(
                "Specification version conflict: the same specification and "
                f"version label already exist with different content. "
                f"specification_key={specification_key}, version={version_label}. "
                "Historical versions are immutable; use a new version label. "
                "Only explicitly detected language variants are stored separately."
            )

        current_version_id = self._preferred_primary_current_version_id(
            cur=cur,
            specification_id=int(specification_id),
            raw_current_version_id=current_version_id,
        )

        cur.execute(
            """
            SELECT COALESCE(MAX(version_seq), 0) + 1
            FROM public.specification_versions
            WHERE specification_id = %s
              AND branch_name = 'MAIN'
            """,
            (specification_id,),
        )
        version_seq = int(cur.fetchone()[0])

        promote = self._should_promote(
            cur=cur,
            current_version_id=current_version_id,
            incoming_label=version_label,
            incoming_status=status,
        )
        previous_version_id, next_version_id = self._resolve_neighbor_version_ids(
            cur=cur,
            specification_id=int(specification_id),
            incoming_label=version_label,
            current_version_id=current_version_id,
            promote_to_current=promote,
        )

        return SpecificationVersionPlan(
            specification_id=int(specification_id),
            specification_key=specification_key,
            version_label=version_label,
            version_seq=version_seq,
            version_sort_key=self.version_sort_key(version_label),
            previous_version_id=previous_version_id,
            next_version_id=next_version_id,
            content_hash=content_hash,
            status=status,
            version_source=self._optional_text(version_source),
            promote_to_current=promote,
        )

    def finalize(
        self,
        *,
        cur: Any,
        plan: SpecificationVersionPlan | None,
        document_id: str,
    ) -> int | None:
        if plan is None or plan.duplicate:
            return None

        cur.execute(
            """
            INSERT INTO public.specification_versions (
                specification_id,
                version_seq,
                version_label,
                version_sort_key,
                branch_name,
                previous_version_id,
                document_id,
                content_hash,
                status,
                version_source,
                created_at,
                updated_at
            )
            VALUES (%s, %s, %s, %s, 'MAIN', %s, %s, %s, %s, %s, NOW(), NOW())
            RETURNING id
            """,
            (
                plan.specification_id,
                plan.version_seq,
                plan.version_label,
                plan.version_sort_key,
                plan.previous_version_id,
                document_id,
                plan.content_hash,
                plan.status,
                plan.version_source,
            ),
        )
        version_id = int(cur.fetchone()[0])

        if plan.next_version_id is not None:
            # Late history import: splice the new version into the semantic
            # version chain (e.g. importing v1.02 after v1.03).
            cur.execute(
                """
                UPDATE public.specification_versions
                SET previous_version_id = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (version_id, plan.next_version_id),
            )
            self.change_manager.mark_not_adjacent(
                cur=cur,
                specification_id=plan.specification_id,
                from_version_id=plan.previous_version_id,
                to_version_id=plan.next_version_id,
            )

        if plan.promote_to_current:
            cur.execute(
                """
                UPDATE public.specifications
                SET current_version_id = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (version_id, plan.specification_id),
            )

        # Deterministic change detection is part of the same transaction.
        # Newer imports get predecessor -> new. A late historical import also
        # refreshes new -> successor so the semantic history remains complete.
        if plan.previous_version_id is not None:
            self.change_manager.ensure_comparison(
                cur=cur,
                specification_id=plan.specification_id,
                from_version_id=plan.previous_version_id,
                to_version_id=version_id,
                is_adjacent=True,
                force=True,
            )
        else:
            cur.execute(
                """
                UPDATE public.specification_versions
                SET change_summary = 'Initial version',
                    updated_at = NOW()
                WHERE id = %s
                """,
                (version_id,),
            )

        if plan.next_version_id is not None:
            self.change_manager.ensure_comparison(
                cur=cur,
                specification_id=plan.specification_id,
                from_version_id=version_id,
                to_version_id=plan.next_version_id,
                is_adjacent=True,
                force=True,
            )

        return version_id

    @staticmethod
    def document_content_hash(document: Document) -> str:
        """Hash stable parsed business content, excluding volatile timestamps."""
        payload = {
            "file_type": document.file_type,
            "chapters": [chapter.model_dump(mode="json") for chapter in document.chapters],
            "sections": [section.model_dump(mode="json") for section in document.sections],
            # Keep the historical v4/v5 content-hash contract stable.
            # v5.2 adds Content.metadata for relation evidence, but source
            # location metadata must not make an unchanged business version
            # look like different content after an application upgrade.
            "contents": [
                {
                    "chapter_id": content.chapter_id,
                    "section_id": content.section_id,
                    "text": content.text,
                    "page_number": content.page_number,
                    "chunk_index": content.chunk_index,
                    "token_count": content.token_count,
                }
                for content in document.contents
            ],
        }
        raw = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _upsert_master(
        *,
        cur: Any,
        project_code: str,
        specification_key: str,
        spec_code: str | None,
        region_scope: str | None,
        spec_type: str | None,
        spec_subtype: str | None,
        canonical_name: str,
        metadata: dict[str, Any],
    ) -> tuple[int, int | None]:
        safe_metadata, _ = PostgresSafeSanitizer.sanitize_value(metadata)
        cur.execute(
            """
            INSERT INTO public.specifications (
                specification_key,
                project_code,
                region_scope,
                spec_code,
                spec_type,
                spec_subtype,
                canonical_name,
                status,
                metadata,
                created_at,
                updated_at
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s,
                'ACTIVE', CAST(%s AS jsonb), NOW(), NOW()
            )
            ON CONFLICT (specification_key)
            DO UPDATE SET
                region_scope = COALESCE(NULLIF(public.specifications.region_scope, ''), EXCLUDED.region_scope),
                spec_code = COALESCE(NULLIF(public.specifications.spec_code, ''), EXCLUDED.spec_code),
                spec_type = COALESCE(NULLIF(public.specifications.spec_type, ''), EXCLUDED.spec_type),
                spec_subtype = COALESCE(NULLIF(public.specifications.spec_subtype, ''), EXCLUDED.spec_subtype),
                canonical_name = COALESCE(NULLIF(public.specifications.canonical_name, ''), EXCLUDED.canonical_name),
                metadata = EXCLUDED.metadata || public.specifications.metadata,
                updated_at = NOW()
            RETURNING id, current_version_id
            """,
            (
                specification_key,
                project_code,
                region_scope,
                spec_code,
                spec_type,
                spec_subtype,
                canonical_name,
                json.dumps(safe_metadata, ensure_ascii=False, default=str),
            ),
        )
        row = cur.fetchone()
        return int(row[0]), int(row[1]) if row[1] is not None else None

    @staticmethod
    def _preferred_primary_current_version_id(
        *,
        cur: Any,
        specification_id: int,
        raw_current_version_id: int | None,
    ) -> int | None:
        """Return Current if it is a body, otherwise the newest body version.

        This guards imports performed against a legacy database before the v1.01
        one-click schema repair has corrected ``specifications.current_version_id``.
        """
        primary_doc = PrimarySpecificationDocumentPolicy.sql_primary_predicate("primary_doc")
        cur.execute(
            f"""
            SELECT sv.id
            FROM public.specification_versions sv
            LEFT JOIN public.documents primary_doc
              ON primary_doc.document_id = sv.document_id
            WHERE sv.specification_id = %s
              AND sv.branch_name = 'MAIN'
              AND {primary_doc}
            ORDER BY
                CASE WHEN sv.id = %s THEN 0 ELSE 1 END,
                sv.version_sort_key DESC NULLS LAST,
                sv.version_seq DESC,
                sv.id DESC
            LIMIT 1
            """,
            (specification_id, raw_current_version_id),
        )
        row = cur.fetchone()
        return int(row[0]) if row and row[0] is not None else None

    def _resolve_neighbor_version_ids(
        self,
        *,
        cur: Any,
        specification_id: int,
        incoming_label: str,
        current_version_id: int | None,
        promote_to_current: bool,
    ) -> tuple[int | None, int | None]:
        """Return semantic predecessor/successor, independent of import order."""
        primary_neighbor = PrimarySpecificationDocumentPolicy.sql_primary_predicate("neighbor_doc")
        cur.execute(
            f"""
            SELECT sv.id, sv.version_label
            FROM public.specification_versions sv
            LEFT JOIN public.documents neighbor_doc
              ON neighbor_doc.document_id = sv.document_id
            WHERE sv.specification_id = %s
              AND sv.branch_name = 'MAIN'
              AND {primary_neighbor}
            """,
            (specification_id,),
        )
        rows = [(int(version_id), str(label)) for version_id, label in cur.fetchall()]

        lower: list[tuple[int, str]] = []
        higher: list[tuple[int, str]] = []
        comparable_found = False
        for version_id, label in rows:
            comparison = self.compare_version_labels(incoming_label, label)
            if comparison is None:
                continue
            comparable_found = True
            if comparison > 0:
                lower.append((version_id, label))
            elif comparison < 0:
                higher.append((version_id, label))

        if not comparable_found:
            # Preserve the prior policy for non-comparable labels: explicit MAIN
            # imports follow the current version in import order.
            return (current_version_id if promote_to_current else None, None)

        predecessor: tuple[int, str] | None = None
        for candidate in lower:
            if predecessor is None:
                predecessor = candidate
                continue
            comparison = self.compare_version_labels(candidate[1], predecessor[1])
            if comparison is not None and comparison > 0:
                predecessor = candidate

        successor: tuple[int, str] | None = None
        for candidate in higher:
            if successor is None:
                successor = candidate
                continue
            comparison = self.compare_version_labels(candidate[1], successor[1])
            if comparison is not None and comparison < 0:
                successor = candidate

        return (
            predecessor[0] if predecessor else None,
            successor[0] if successor else None,
        )

    def _should_promote(
        self,
        *,
        cur: Any,
        current_version_id: int | None,
        incoming_label: str,
        incoming_status: str,
    ) -> bool:
        if current_version_id is None:
            return True
        if incoming_status == "UNRESOLVED":
            return False

        cur.execute(
            """
            SELECT version_label, status
            FROM public.specification_versions
            WHERE id = %s
            """,
            (current_version_id,),
        )
        row = cur.fetchone()
        if not row:
            return True
        current_label, current_status = str(row[0]), str(row[1])
        if current_status == "UNRESOLVED":
            return True

        comparison = self.compare_version_labels(incoming_label, current_label)
        if comparison is None:
            # When labels are not comparable, import order is the safest default
            # for an explicitly imported MAIN revision.
            return True
        return comparison > 0

    @classmethod
    def compare_version_labels(cls, left: str, right: str) -> int | None:
        left_key = cls._version_key(left)
        right_key = cls._version_key(right)
        if left_key is None or right_key is None or left_key[0] != right_key[0]:
            return None
        return (left_key > right_key) - (left_key < right_key)

    @classmethod
    def _version_key(cls, label: str) -> tuple[Any, ...] | None:
        value = str(label).strip()
        match = cls._NUMERIC_VERSION.match(value)
        if match:
            raw_nums = tuple(int(part) for part in re.split(r"[.-]", match.group("body")))
            nums = raw_nums + (0,) * max(0, 6 - len(raw_nums))
            suffix = match.group("suffix").upper()
            return ("NUM", nums, suffix)

        match = cls._REV_VERSION.match(value)
        if match:
            body = match.group("body").upper()
            if body.isdigit():
                return ("REV", 0, int(body))
            return ("REV", 1, body)

        match = cls._R_VERSION.match(value)
        if match:
            return ("R", int(match.group("body")))

        return None

    @classmethod
    def version_sort_key(cls, label: str) -> str | None:
        """Stable lexicographic key for business-version ordering in SQL/UI."""
        key = cls._version_key(label)
        if key is None:
            return None
        family = str(key[0])
        if family == "NUM":
            nums = key[1]
            suffix = str(key[2] or "")
            padded = ".".join(f"{int(value):010d}" for value in nums)
            return f"NUM:{padded}:{suffix}"
        if family == "REV":
            kind = int(key[1])
            body = key[2]
            if kind == 0:
                return f"REV:N:{int(body):010d}"
            return f"REV:A:{str(body)}"
        if family == "R":
            return f"R:{int(key[1]):010d}"
        return None

    @staticmethod
    def _optional_text(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None
