from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from app.relations.extractor import (
    EXTRACTOR_VERSION,
    EngineeringRelationExtractor,
    ExtractionUnit,
    KnownSpecification,
    RelationExtractionResult,
)
from app.storage.primary_document_policy import PrimarySpecificationDocumentPolicy


class EngineeringRelationError(RuntimeError):
    """Engineering relation extraction/persistence error."""


@dataclass(frozen=True, slots=True)
class EngineeringRelationRunResult:
    run_id: int
    entity_count: int
    relation_count: int
    unresolved_count: int
    extractor_version: str = EXTRACTOR_VERSION

    @property
    def summary_text(self) -> str:
        return (
            f"{self.relation_count} relations / {self.entity_count} entities / "
            f"{self.unresolved_count} unresolved"
        )


class EngineeringRelationManager:
    """Persist version-aware engineering graph facts with immutable run history."""

    def __init__(self, extractor: EngineeringRelationExtractor | None = None) -> None:
        self.extractor = extractor or EngineeringRelationExtractor()

    def ensure_for_version(
        self,
        *,
        cur: Any,
        specification_id: int,
        version_id: int,
        document_id: str,
        force: bool = False,
    ) -> EngineeringRelationRunResult | None:
        if not self._relation_schema_ready(cur):
            return None

        if not force:
            existing = self._current_run(cur, version_id)
            # A READY generation is reusable only when it was produced by
            # the currently shipped extractor contract.  When extraction
            # logic is version-bumped, ingestion/backfill automatically
            # creates a new immutable generation and moves the current-run
            # pointer without overwriting historical audit output.
            if existing is not None and existing.extractor_version == EXTRACTOR_VERSION:
                return existing

        source = self._load_source_specification(cur, specification_id)
        if source is None:
            raise EngineeringRelationError(
                f"Specification {specification_id} does not exist."
            )

        units = self._load_units(cur, document_id)
        known = self._load_known_specifications(cur, source.project_code)
        result = self.extractor.extract(
            source_specification_id=specification_id,
            source_specification_key=source.specification_key,
            units=units,
            known_specifications=known,
        )
        persisted = self._persist_result(
            cur=cur,
            specification_id=specification_id,
            version_id=version_id,
            document_id=document_id,
            result=result,
        )
        # A newly imported Toyota specification may satisfy unresolved references
        # already emitted by older source specifications (e.g. 508 -> SPEC:408).
        # Reconcile immediately against the SQL registry so users do not need a
        # manual backfill/reconcile step after every target import.
        self.reconcile_unresolved(cur=cur, project_code=source.project_code)
        return self._current_run(cur, version_id) or persisted

    def record_failure(
        self,
        *,
        cur: Any,
        specification_id: int,
        version_id: int,
        document_id: str,
        error_message: str,
    ) -> None:
        if not self._relation_schema_ready(cur):
            return
        cur.execute(
            """
            INSERT INTO public.relation_extraction_runs (
                specification_id, version_id, document_id, extractor_version,
                status, is_current, entity_count, relation_count,
                unresolved_count, stats, error_message,
                started_at, finished_at, created_at
            )
            VALUES (%s, %s, %s, %s, 'FAILED', FALSE, 0, 0, 0,
                    '{}'::jsonb, %s, NOW(), NOW(), NOW())
            """,
            (
                specification_id,
                version_id,
                document_id,
                EXTRACTOR_VERSION,
                str(error_message)[:4000],
            ),
        )

    def reconcile_unresolved(self, *, cur: Any, project_code: str | None = None) -> int:
        """Resolve deterministic external spec references when a Master appears.

        This reconciliation is SQL/registry-first and never calls Qdrant.  It
        supports both SPEC:<code> and SPEC_TITLE:<normalized-title> candidates.
        Ambiguous candidates stay non-authoritative instead of being guessed.
        """
        params: list[Any] = []
        where = (
            "r.status IN ('UNRESOLVED', 'AMBIGUOUS') "
            "AND (r.target_external_key LIKE 'SPEC:%%' "
            "OR r.target_external_key LIKE 'SPEC_TITLE:%%')"
        )
        if project_code:
            where += " AND src.project_code = %s"
            params.append(project_code)
        cur.execute(
            f"""
            SELECT r.id, r.source_specification_id, r.target_external_key
            FROM public.spec_relations r
            JOIN public.specifications src ON src.id = r.source_specification_id
            JOIN public.relation_extraction_runs run
              ON run.id = r.extraction_run_id AND run.is_current IS TRUE
            WHERE {where}
            ORDER BY r.id
            """,
            tuple(params),
        )
        rows = list(cur.fetchall())
        known = self._load_known_specifications(cur, project_code)

        code_index: dict[str, list[KnownSpecification]] = {}
        title_index: dict[str, list[KnownSpecification]] = {}
        for item in known:
            if item.spec_code:
                code_index.setdefault(
                    EngineeringRelationExtractor._normalize_code(item.spec_code), []
                ).append(item)
            code_index.setdefault(
                EngineeringRelationExtractor._normalize_code(item.specification_key), []
            ).append(item)
            for alias in (item.canonical_name, *item.aliases):
                normalized = EngineeringRelationExtractor._normalize_title(alias)
                if normalized:
                    title_index.setdefault(normalized, []).append(item)

        resolved = 0
        for relation_id, source_specification_id, external_key in rows:
            external = str(external_key or "").strip()
            if not external:
                continue
            if external.startswith("SPEC_TITLE:"):
                token = external[len("SPEC_TITLE:"):].strip()
                candidates = list(title_index.get(token, []))
                resolution_method = "RECONCILE_EXACT_TITLE"
            elif external.startswith("SPEC:"):
                token = EngineeringRelationExtractor._normalize_code(
                    external[len("SPEC:"):].strip()
                )
                candidates = list(code_index.get(token, []))
                resolution_method = "RECONCILE_EXACT_CODE"
            else:
                continue

            candidates = [
                item for item in candidates
                if int(item.specification_id) != int(source_specification_id)
            ]
            unique: dict[int, KnownSpecification] = {
                int(item.specification_id): item for item in candidates
            }
            candidates = list(unique.values())

            if len(candidates) == 1:
                target = candidates[0]
                patch = json.dumps(
                    {
                        "reconciled": True,
                        "reconciliation_method": resolution_method,
                        "reconciled_target_specification_key": target.specification_key,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                cur.execute(
                    """
                    UPDATE public.spec_relations
                    SET target_specification_id = %s,
                        target_external_key = NULL,
                        status = 'CONFIRMED',
                        confidence = GREATEST(confidence, 0.99),
                        metadata = COALESCE(metadata, '{}'::jsonb) || CAST(%s AS jsonb)
                    WHERE id = %s
                    """,
                    (int(target.specification_id), patch, int(relation_id)),
                )
                resolved += 1
                continue

            if len(candidates) > 1:
                patch = json.dumps(
                    {
                        "reconciled": False,
                        "reconciliation_method": "AMBIGUOUS_EXACT_REGISTRY",
                        "candidate_specification_ids": sorted(unique),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                cur.execute(
                    """
                    UPDATE public.spec_relations
                    SET status = 'AMBIGUOUS',
                        metadata = COALESCE(metadata, '{}'::jsonb) || CAST(%s AS jsonb)
                    WHERE id = %s
                    """,
                    (patch, int(relation_id)),
                )

        resolved += self._reconcile_manual_relations(
            cur=cur,
            project_code=project_code,
            code_index=code_index,
        )
        self._refresh_relation_run_counters(cur=cur, project_code=project_code)
        return resolved

    @staticmethod
    def _reconcile_manual_relations(
        *,
        cur: Any,
        project_code: str | None,
        code_index: dict[str, list[KnownSpecification]],
    ) -> int:
        """Reconcile manually curated SPEC:<code> edges after target imports.

        Manual relations live outside immutable extractor generations, but they
        must participate in the same deterministic SQL-first resolution path.
        """
        cur.execute("SELECT to_regclass('public.manual_spec_relations') IS NOT NULL")
        schema_rows = list(cur.fetchall())
        if not bool(schema_rows and schema_rows[0] and schema_rows[0][0]):
            return 0
        params: list[Any] = []
        project_clause = ""
        if project_code:
            project_clause = "AND mr.project_code = %s"
            params.append(project_code)
        cur.execute(
            f"""
            SELECT mr.id, mr.source_specification_id, mr.target_external_key
            FROM public.manual_spec_relations mr
            WHERE mr.is_active IS TRUE
              AND mr.status IN ('UNRESOLVED', 'AMBIGUOUS')
              AND mr.target_external_key LIKE 'SPEC:%%'
              {project_clause}
            ORDER BY mr.id
            """,
            tuple(params),
        )
        rows = list(cur.fetchall())
        resolved = 0
        for manual_id, source_specification_id, external_key in rows:
            external = str(external_key or "").strip()
            token = EngineeringRelationExtractor._normalize_code(
                external[len("SPEC:"):].strip()
            )
            candidates = [
                item for item in code_index.get(token, [])
                if int(item.specification_id) != int(source_specification_id)
            ]
            unique = {int(item.specification_id): item for item in candidates}
            candidates = list(unique.values())
            if len(candidates) == 1:
                target = candidates[0]
                patch = json.dumps(
                    {
                        "manual_reconciled": True,
                        "manual_reconciliation_method": "EXACT_SPEC_CODE",
                        "reconciled_target_specification_key": target.specification_key,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                cur.execute(
                    """
                    UPDATE public.manual_spec_relations
                    SET target_specification_id = %s,
                        target_external_key = NULL,
                        status = 'CONFIRMED',
                        metadata = COALESCE(metadata, '{}'::jsonb) || CAST(%s AS jsonb),
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (int(target.specification_id), patch, int(manual_id)),
                )
                cur.execute(
                    """
                    INSERT INTO public.manual_spec_relation_audit(
                        manual_relation_id, action, actor, snapshot, created_at
                    )
                    SELECT id, 'RECONCILE', 'SYSTEM', to_jsonb(mr), NOW()
                    FROM public.manual_spec_relations mr
                    WHERE mr.id = %s
                    """,
                    (int(manual_id),),
                )
                resolved += 1
            elif len(candidates) > 1:
                patch = json.dumps(
                    {
                        "manual_reconciled": False,
                        "manual_reconciliation_method": "AMBIGUOUS_EXACT_REGISTRY",
                        "candidate_specification_ids": sorted(unique),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                cur.execute(
                    """
                    UPDATE public.manual_spec_relations
                    SET status = 'AMBIGUOUS',
                        metadata = COALESCE(metadata, '{}'::jsonb) || CAST(%s AS jsonb),
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (patch, int(manual_id)),
                )
        return resolved

    @staticmethod
    def _refresh_relation_run_counters(*, cur: Any, project_code: str | None = None) -> None:
        params: list[Any] = []
        project_clause = ""
        if project_code:
            project_clause = "WHERE src.project_code = %s"
            params.append(project_code)
        cur.execute(
            f"""
            WITH counts AS (
                SELECT
                    run.id AS run_id,
                    COUNT(r.id)::INTEGER AS relation_count,
                    COUNT(r.id) FILTER (WHERE r.status IN ('UNRESOLVED', 'AMBIGUOUS'))::INTEGER AS unresolved_count
                FROM public.relation_extraction_runs run
                JOIN public.specifications src ON src.id = run.specification_id
                LEFT JOIN public.spec_relations r ON r.extraction_run_id = run.id
                {project_clause}
                GROUP BY run.id
            )
            UPDATE public.relation_extraction_runs run
            SET relation_count = counts.relation_count,
                unresolved_count = counts.unresolved_count
            FROM counts
            WHERE run.id = counts.run_id
            """,
            tuple(params),
        )

    def _persist_result(
        self,
        *,
        cur: Any,
        specification_id: int,
        version_id: int,
        document_id: str,
        result: RelationExtractionResult,
    ) -> EngineeringRelationRunResult:
        cur.execute(
            """
            INSERT INTO public.relation_extraction_runs (
                specification_id, version_id, document_id, extractor_version,
                status, is_current, entity_count, relation_count,
                unresolved_count, stats, error_message,
                started_at, finished_at, created_at
            )
            VALUES (%s, %s, %s, %s, 'READY', FALSE, %s, %s, %s,
                    CAST(%s AS jsonb), NULL, NOW(), NOW(), NOW())
            RETURNING id
            """,
            (
                specification_id,
                version_id,
                document_id,
                EXTRACTOR_VERSION,
                len(result.entities),
                len(result.relations),
                result.unresolved_count,
                json.dumps(result.stats, ensure_ascii=False, sort_keys=True),
            ),
        )
        run_id = int(cur.fetchone()[0])

        entity_ids: dict[str, int] = {}
        for entity in result.entities:
            cur.execute(
                """
                INSERT INTO public.engineering_entities (
                    extraction_run_id, specification_id, version_id,
                    entity_type, entity_key, canonical_name, normalized_value,
                    extraction_method, confidence, status, metadata, created_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        CAST(%s AS jsonb), NOW())
                RETURNING id
                """,
                (
                    run_id,
                    specification_id,
                    version_id,
                    entity.entity_type,
                    entity.entity_key,
                    entity.canonical_name,
                    entity.normalized_value,
                    entity.extraction_method,
                    entity.confidence,
                    entity.status,
                    json.dumps(entity.metadata, ensure_ascii=False, sort_keys=True),
                ),
            )
            entity_ids[entity.entity_key] = int(cur.fetchone()[0])

        for relation in result.relations:
            cur.execute(
                """
                INSERT INTO public.spec_relations (
                    extraction_run_id, relation_key,
                    source_specification_id, source_version_id, source_entity_id,
                    target_specification_id, target_entity_id, target_external_key,
                    relation_type, extraction_method, confidence, status,
                    is_explicit, metadata, created_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, CAST(%s AS jsonb), NOW())
                RETURNING id
                """,
                (
                    run_id,
                    relation.relation_key,
                    specification_id,
                    version_id,
                    entity_ids.get(relation.source_entity_key or ""),
                    relation.target_specification_id,
                    entity_ids.get(relation.target_entity_key or ""),
                    relation.target_external_key,
                    relation.relation_type,
                    relation.extraction_method,
                    relation.confidence,
                    relation.status,
                    relation.is_explicit,
                    json.dumps(relation.metadata, ensure_ascii=False, sort_keys=True),
                ),
            )
            relation_id = int(cur.fetchone()[0])
            for evidence in relation.evidence:
                cur.execute(
                    """
                    INSERT INTO public.relation_evidence (
                        relation_id, document_id, content_id, page_number,
                        chapter_id, chapter_title, section_id, section_title,
                        sheet_name, cell_range, evidence_text,
                        start_offset, end_offset, evidence_hash, metadata, created_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, CAST(%s AS jsonb), NOW())
                    ON CONFLICT (relation_id, evidence_hash) DO NOTHING
                    """,
                    (
                        relation_id,
                        document_id,
                        evidence.content_id,
                        evidence.page_number,
                        evidence.chapter_id,
                        evidence.chapter_title,
                        evidence.section_id,
                        evidence.section_title,
                        evidence.sheet_name,
                        evidence.cell_range,
                        evidence.evidence_text,
                        evidence.start_offset,
                        evidence.end_offset,
                        evidence.evidence_hash,
                        json.dumps(evidence.metadata, ensure_ascii=False, sort_keys=True),
                    ),
                )

        # is_current is a Specification-level pointer to the extraction for the
        # Specification's current business version. Historical version runs stay
        # immutable and READY, but never compete with the active version in the
        # authoritative graph.
        primary_d = PrimarySpecificationDocumentPolicy.sql_primary_predicate("d")
        cur.execute(
            f"""
            SELECT sv.id
            FROM public.specifications sp
            JOIN public.specification_versions sv ON sv.specification_id = sp.id
            LEFT JOIN public.documents d ON d.document_id = sv.document_id
            WHERE sp.id = %s
              AND sv.branch_name = 'MAIN'
              AND {primary_d}
            ORDER BY
                CASE WHEN sv.id = sp.current_version_id THEN 0 ELSE 1 END,
                sv.version_sort_key DESC NULLS LAST, sv.version_seq DESC, sv.id DESC
            LIMIT 1
            """,
            (specification_id,),
        )
        current_version_row = cur.fetchone()
        is_active_version = bool(
            current_version_row and int(current_version_row[0] or -1) == int(version_id)
        )
        if is_active_version:
            cur.execute(
                "UPDATE public.relation_extraction_runs SET is_current = FALSE "
                "WHERE specification_id = %s AND id <> %s AND is_current IS TRUE",
                (specification_id, run_id),
            )
            cur.execute(
                "UPDATE public.relation_extraction_runs SET is_current = TRUE WHERE id = %s",
                (run_id,),
            )

        return EngineeringRelationRunResult(
            run_id=run_id,
            entity_count=len(result.entities),
            relation_count=len(result.relations),
            unresolved_count=result.unresolved_count,
        )

    @staticmethod
    def _current_run(cur: Any, version_id: int) -> EngineeringRelationRunResult | None:
        cur.execute(
            """
            SELECT id, entity_count, relation_count, unresolved_count, extractor_version
            FROM public.relation_extraction_runs
            WHERE version_id = %s AND status = 'READY'
            ORDER BY id DESC
            LIMIT 1
            """,
            (version_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return EngineeringRelationRunResult(
            run_id=int(row[0]),
            entity_count=int(row[1] or 0),
            relation_count=int(row[2] or 0),
            unresolved_count=int(row[3] or 0),
            extractor_version=str(row[4] or EXTRACTOR_VERSION),
        )

    @staticmethod
    def _metadata_aliases(metadata: Any) -> tuple[str, ...]:
        raw = dict(metadata or {}) if isinstance(metadata, dict) else {}
        values: list[str] = []
        for key in (
            "reference_aliases",
            "aliases",
            "document_aliases",
            "legacy_names",
            "legacy_titles",
        ):
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                values.append(value.strip())
            elif isinstance(value, (list, tuple, set)):
                values.extend(str(item).strip() for item in value if str(item).strip())
        seen: set[str] = set()
        result: list[str] = []
        for value in values:
            key = EngineeringRelationExtractor._normalize_title(value)
            if key and key not in seen:
                seen.add(key)
                result.append(value)
        return tuple(result)

    @staticmethod
    def _load_source_specification(cur: Any, specification_id: int) -> KnownSpecification | None:
        cur.execute(
            """
            SELECT id, specification_key, canonical_name, spec_code, project_code,
                   COALESCE(metadata, '{}'::jsonb)
            FROM public.specifications WHERE id = %s
            """,
            (specification_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return KnownSpecification(
            specification_id=int(row[0]),
            specification_key=str(row[1]),
            canonical_name=str(row[2]),
            spec_code=str(row[3]) if row[3] is not None else None,
            project_code=str(row[4]) if row[4] is not None else None,
            aliases=EngineeringRelationManager._metadata_aliases(row[5]),
        )

    @staticmethod
    def _load_known_specifications(cur: Any, project_code: str | None) -> list[KnownSpecification]:
        # Do not use ``(%s IS NULL OR project_code = %s)`` here. PostgreSQL
        # cannot infer the type of the first placeholder when it is used only
        # by ``IS NULL`` (SQLSTATE 42P18 / IndeterminateDatatype). Build the
        # deterministic predicate instead so every bound parameter has a
        # typed comparison context.
        if project_code is None:
            cur.execute(
                """
                SELECT id, specification_key, canonical_name, spec_code, project_code,
                       COALESCE(metadata, '{}'::jsonb)
                FROM public.specifications
                WHERE status = 'ACTIVE'
                ORDER BY id
                """
            )
        else:
            cur.execute(
                """
                SELECT id, specification_key, canonical_name, spec_code, project_code,
                       COALESCE(metadata, '{}'::jsonb)
                FROM public.specifications
                WHERE project_code = %s
                  AND status = 'ACTIVE'
                ORDER BY id
                """,
                (project_code,),
            )
        return [
            KnownSpecification(
                specification_id=int(row[0]),
                specification_key=str(row[1]),
                canonical_name=str(row[2]),
                spec_code=str(row[3]) if row[3] is not None else None,
                project_code=str(row[4]) if row[4] is not None else None,
                aliases=EngineeringRelationManager._metadata_aliases(row[5]),
            )
            for row in cur.fetchall()
        ]

    @staticmethod
    def _load_units(cur: Any, document_id: str) -> list[ExtractionUnit]:
        cur.execute(
            """
            SELECT
                c.id,
                c.content,
                c.page_number,
                c.chapter_id,
                COALESCE(NULLIF(ch.title_jp, ''), ch.title_en),
                c.section_id,
                COALESCE(NULLIF(s.title_jp, ''), s.title_en),
                COALESCE(c.metadata, '{}'::jsonb),
                COALESCE(s.metadata, '{}'::jsonb)
            FROM public.contents c
            LEFT JOIN public.chapters ch
              ON ch.document_id = c.document_id AND ch.chapter_id = c.chapter_id
            LEFT JOIN public.sections s
              ON s.document_id = c.document_id AND s.section_id = c.section_id
            WHERE c.document_id = %s
            ORDER BY COALESCE(c.sort_order, c.id), c.id
            """,
            (document_id,),
        )
        units: list[ExtractionUnit] = []
        for row in cur.fetchall():
            units.append(
                ExtractionUnit(
                    content_id=int(row[0]),
                    text=str(row[1] or ""),
                    page_number=int(row[2]) if row[2] is not None else None,
                    chapter_id=str(row[3]) if row[3] is not None else None,
                    chapter_title=str(row[4]) if row[4] is not None else None,
                    section_id=str(row[5]) if row[5] is not None else None,
                    section_title=str(row[6]) if row[6] is not None else None,
                    metadata=dict(row[7] or {}),
                    section_metadata=dict(row[8] or {}),
                )
            )
        return units

    @staticmethod
    def _relation_schema_ready(cur: Any) -> bool:
        cur.execute("SELECT to_regclass('public.relation_extraction_runs')")
        row = cur.fetchone()
        return bool(row and row[0] is not None)
