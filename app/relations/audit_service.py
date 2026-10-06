from __future__ import annotations

import getpass
import json
from typing import Any

from app.database.connection import DatabaseConnection
from app.relations.graph_service import EngineeringRelationGraphService
from app.relations.manager import EngineeringRelationManager


class EngineeringRelationAuditService:
    """Read/admin service for the deterministic + manually curated relation registry."""

    MANUAL_RELATION_ID_OFFSET = -1
    MANUAL_EXTRACTOR_VERSION = "manual-relation-editor-v1"
    MANUAL_EXTRACTION_METHOD = "MANUAL_EDITOR"

    _MANUAL_MEANINGS = {
        "REFERENCES_SPEC",
        "COMPLIES_WITH",
        "RELEVANT_SPEC",
        "REFERENCES_SECTION",
        "DETAILS_DEFINED_IN",
        "CORRESPONDS_TO_SECTION",
    }
    _MANUAL_STATUSES = {"AUTO", "CONFIRMED", "UNRESOLVED", "AMBIGUOUS"}
    _EVIDENCE_KINDS = {"SOURCE_QUOTE", "REFERENCE_TABLE", "ENGINEERING_DECISION", "OTHER"}
    _LOCATOR_TYPES = {"", "NONE", "SECTION", "USE_CASE", "REQUIREMENT", "CHAPTER", "PAGE", "CUSTOM"}

    def __init__(self, database_connection: DatabaseConnection) -> None:
        self.db = database_connection

    @staticmethod
    def _manual_schema_ready(cur: Any) -> bool:
        cur.execute("SELECT to_regclass('public.manual_spec_relations') IS NOT NULL")
        return bool((cur.fetchone() or (False,))[0])

    @classmethod
    def _manual_relation_id(cls, manual_id: int) -> int:
        # Negative namespace keeps GUI/graph relation ids unique without changing
        # the immutable automatic spec_relations primary key contract.
        return -abs(int(manual_id))

    @classmethod
    def _manual_id_from_relation_id(cls, relation_id: int) -> int:
        if int(relation_id) >= 0:
            raise ValueError("Relation id is not a manual relation id.")
        return abs(int(relation_id))

    def manual_schema_installed(self) -> bool:
        with self.db.connect() as conn:
            with conn.cursor() as cur:
                return self._manual_schema_ready(cur)

    def list_relations(self, *, current_source_only: bool = True) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            with conn.cursor() as cur:
                where = "WHERE is_current_extraction IS TRUE"
                if current_source_only:
                    where += " AND is_current_source_version IS TRUE"
                cur.execute(
                    f"""
                    SELECT
                        cat.relation_id,
                        cat.source_specification_id,
                        cat.source_specification_key,
                        cat.source_specification_name,
                        src.spec_code AS source_spec_code,
                        cat.source_version_id,
                        cat.source_version,
                        cat.source_entity_id,
                        cat.source_entity_type,
                        cat.source_entity_key,
                        cat.source_entity_name,
                        cat.target_specification_id,
                        cat.target_specification_key,
                        cat.target_specification_name,
                        tgt.spec_code AS target_spec_code,
                        cat.target_entity_id,
                        cat.target_entity_type,
                        cat.target_entity_key,
                        cat.target_entity_name,
                        cat.target_external_key,
                        cat.relation_type,
                        cat.extraction_method,
                        cat.confidence,
                        cat.status,
                        cat.is_explicit,
                        cat.evidence_count,
                        COALESCE((
                            SELECT COUNT(DISTINCT ev.content_id)::INTEGER
                            FROM public.relation_evidence ev
                            WHERE ev.relation_id = cat.relation_id
                        ), 0) AS evidence_chunk_count,
                        cat.extractor_version,
                        cat.is_current_source_version,
                        cat.metadata,
                        cat.created_at
                    FROM public.engineering_relation_catalog cat
                    JOIN public.specifications src ON src.id = cat.source_specification_id
                    LEFT JOIN public.specifications tgt ON tgt.id = cat.target_specification_id
                    {where.replace('is_current_extraction', 'cat.is_current_extraction').replace('is_current_source_version', 'cat.is_current_source_version')}
                    ORDER BY source_specification_name, source_version, relation_type, relation_id
                    """
                )
                columns = [item.name for item in cur.description]
                rows = [dict(zip(columns, row)) for row in cur.fetchall()]

                if not self._manual_schema_ready(cur):
                    return rows

                manual_where = "WHERE mr.is_active IS TRUE"
                if current_source_only:
                    manual_where += " AND src.current_version_id = mr.source_version_id"
                cur.execute(
                    f"""
                    SELECT
                        mr.id AS manual_id,
                        mr.source_specification_id,
                        src.specification_key AS source_specification_key,
                        src.canonical_name AS source_specification_name,
                        src.spec_code AS source_spec_code,
                        mr.source_version_id,
                        sv.version_label AS source_version,
                        mr.target_specification_id,
                        tgt.specification_key AS target_specification_key,
                        tgt.canonical_name AS target_specification_name,
                        tgt.spec_code AS target_spec_code,
                        mr.target_external_key,
                        mr.semantic_type,
                        mr.status,
                        mr.confidence,
                        mr.evidence_kind,
                        mr.evidence_text,
                        mr.target_locator_type,
                        mr.target_locator_value,
                        mr.note,
                        mr.created_by,
                        mr.metadata,
                        mr.created_at,
                        mr.updated_at,
                        (src.current_version_id = mr.source_version_id) AS is_current_source_version
                    FROM public.manual_spec_relations mr
                    JOIN public.specifications src ON src.id = mr.source_specification_id
                    JOIN public.specification_versions sv ON sv.id = mr.source_version_id
                    LEFT JOIN public.specifications tgt ON tgt.id = mr.target_specification_id
                    {manual_where}
                    ORDER BY src.canonical_name, sv.version_label, mr.semantic_type, mr.id
                    """
                )
                manual_columns = [item.name for item in cur.description]
                for raw in cur.fetchall():
                    item = dict(zip(manual_columns, raw))
                    manual_id = int(item["manual_id"])
                    metadata = dict(item.get("metadata") or {})
                    metadata.update(
                        {
                            "toyota_reference_kind": item.get("semantic_type") or "REFERENCES_SPEC",
                            "manual_relation": True,
                            "manual_relation_id": manual_id,
                            "evidence_kind": item.get("evidence_kind"),
                            "target_locator_type": item.get("target_locator_type"),
                            "target_locator_value": item.get("target_locator_value"),
                            "manual_note": item.get("note"),
                            "created_by": item.get("created_by"),
                        }
                    )
                    rows.append(
                        {
                            "relation_id": self._manual_relation_id(manual_id),
                            "source_specification_id": item.get("source_specification_id"),
                            "source_specification_key": item.get("source_specification_key"),
                            "source_specification_name": item.get("source_specification_name"),
                            "source_spec_code": item.get("source_spec_code"),
                            "source_version_id": item.get("source_version_id"),
                            "source_version": item.get("source_version"),
                            "source_entity_id": None,
                            "source_entity_type": None,
                            "source_entity_key": None,
                            "source_entity_name": None,
                            "target_specification_id": item.get("target_specification_id"),
                            "target_specification_key": item.get("target_specification_key"),
                            "target_specification_name": item.get("target_specification_name"),
                            "target_spec_code": item.get("target_spec_code"),
                            "target_entity_id": None,
                            "target_entity_type": None,
                            "target_entity_key": None,
                            "target_entity_name": None,
                            "target_external_key": item.get("target_external_key"),
                            "relation_type": "REFERENCES",
                            "extraction_method": self.MANUAL_EXTRACTION_METHOD,
                            "confidence": item.get("confidence"),
                            "status": item.get("status"),
                            "is_explicit": True,
                            "evidence_count": 1 if str(item.get("evidence_text") or "").strip() else 0,
                            "evidence_chunk_count": 0,
                            "extractor_version": self.MANUAL_EXTRACTOR_VERSION,
                            "is_current_source_version": item.get("is_current_source_version"),
                            "metadata": metadata,
                            "created_at": item.get("created_at"),
                            "updated_at": item.get("updated_at"),
                        }
                    )
                return rows

    def list_evidence(self, relation_id: int) -> list[dict[str, Any]]:
        """Return automatic exact Chunk evidence or one manual audit-evidence row."""
        if int(relation_id) < 0:
            manual_id = self._manual_id_from_relation_id(relation_id)
            with self.db.connect() as conn:
                with conn.cursor() as cur:
                    if not self._manual_schema_ready(cur):
                        return []
                    cur.execute(
                        """
                        SELECT
                            mr.id,
                            src.canonical_name AS source_specification_name,
                            sv.version_label AS source_version,
                            tgt.canonical_name AS target_specification_name,
                            mr.target_external_key,
                            mr.semantic_type,
                            mr.status,
                            mr.confidence,
                            mr.evidence_kind,
                            mr.evidence_text,
                            mr.target_locator_type,
                            mr.target_locator_value,
                            mr.note,
                            mr.created_by,
                            mr.updated_at
                        FROM public.manual_spec_relations mr
                        JOIN public.specifications src ON src.id = mr.source_specification_id
                        JOIN public.specification_versions sv ON sv.id = mr.source_version_id
                        LEFT JOIN public.specifications tgt ON tgt.id = mr.target_specification_id
                        WHERE mr.id = %s
                        """,
                        (manual_id,),
                    )
                    row = cur.fetchone()
                    if row is None:
                        return []
                    cols = [item.name for item in cur.description]
                    item = dict(zip(cols, row))
                    locator_parts = [
                        str(item.get("target_locator_type") or "").strip(),
                        str(item.get("target_locator_value") or "").strip(),
                    ]
                    locator = " ".join(part for part in locator_parts if part)
                    note = str(item.get("note") or "").strip()
                    evidence = str(item.get("evidence_text") or "").strip()
                    if note:
                        evidence = f"{evidence}\n\nManual note:\n{note}" if evidence else f"Manual note:\n{note}"
                    return [
                        {
                            "evidence_id": self._manual_relation_id(manual_id),
                            "relation_id": int(relation_id),
                            "source_specification_name": item.get("source_specification_name"),
                            "source_version": item.get("source_version"),
                            "relation_type": "REFERENCES",
                            "target_specification_name": item.get("target_specification_name"),
                            "target_entity_type": None,
                            "target_entity_name": None,
                            "target_external_key": item.get("target_external_key"),
                            "extraction_method": self.MANUAL_EXTRACTION_METHOD,
                            "confidence": item.get("confidence"),
                            "status": item.get("status"),
                            "document_id": f"MANUAL:{manual_id}",
                            "content_id": None,
                            "page_number": None,
                            "chapter_id": None,
                            "chapter_title": None,
                            "section_id": None,
                            "section_title": locator or None,
                            "sheet_name": item.get("evidence_kind") or "MANUAL",
                            "cell_range": None,
                            "evidence_text": evidence,
                            "start_offset": None,
                            "end_offset": None,
                            "created_at": item.get("updated_at"),
                            "mapping_status": "MANUAL",
                            "mapped_chunk_index": None,
                            "mapped_chunk_page_number": None,
                            "mapped_chunk_sort_order": None,
                            "mapped_chunk_content": "",
                            "mapped_chunk_live_content_hash": None,
                        }
                    ]

        # Automatic extraction evidence path.
        with self.db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT to_regclass('public.relation_evidence_chunk_map') IS NOT NULL"
                )
                mapping_available = bool((cur.fetchone() or (False,))[0])
                mapping_columns = ""
                mapping_join = ""
                if mapping_available:
                    mapping_columns = """
                        , map.mapping_status
                        , map.mapped_chunk_index
                        , map.mapped_chunk_page_number
                        , map.mapped_chunk_sort_order
                        , map.mapped_chunk_content
                        , map.mapped_chunk_live_content_hash
                    """
                    mapping_join = """
                    LEFT JOIN public.relation_evidence_chunk_map map
                      ON map.mapped_evidence_id = detail.evidence_id
                    """
                cur.execute(
                    f"""
                    SELECT
                        detail.evidence_id,
                        detail.relation_id,
                        detail.source_specification_name,
                        detail.source_version,
                        detail.relation_type,
                        detail.target_specification_name,
                        detail.target_entity_type,
                        detail.target_entity_name,
                        detail.target_external_key,
                        detail.extraction_method,
                        detail.confidence,
                        detail.status,
                        detail.document_id,
                        detail.content_id,
                        detail.page_number,
                        detail.chapter_id,
                        detail.chapter_title,
                        detail.section_id,
                        detail.section_title,
                        detail.sheet_name,
                        detail.cell_range,
                        detail.evidence_text,
                        detail.start_offset,
                        detail.end_offset,
                        detail.created_at
                        {mapping_columns}
                    FROM public.relation_evidence_detail detail
                    {mapping_join}
                    WHERE detail.relation_id = %s
                    ORDER BY detail.content_id NULLS LAST, detail.evidence_id
                    """,
                    (relation_id,),
                )
                columns = [item.name for item in cur.description]
                return [dict(zip(columns, row)) for row in cur.fetchall()]

    def summary(self, *, current_source_only: bool = True) -> dict[str, int]:
        # Use the unified list so manual relations are counted exactly as users
        # see them in the registry and graph.
        rows = self.list_relations(current_source_only=current_source_only)
        return {
            "relation_count": len(rows),
            "confirmed_count": sum(1 for row in rows if str(row.get("status") or "").upper() == "CONFIRMED"),
            "unresolved_count": sum(1 for row in rows if str(row.get("status") or "").upper() in {"UNRESOLVED", "AMBIGUOUS"}),
            "evidence_count": sum(int(row.get("evidence_count") or 0) for row in rows),
        }

    def list_specifications_for_manual_editor(self) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        sp.id AS specification_id,
                        sp.specification_key,
                        sp.project_code,
                        sp.region_scope,
                        sp.spec_code,
                        sp.canonical_name,
                        sp.current_version_id,
                        sv.version_label AS current_version
                    FROM public.specifications sp
                    LEFT JOIN public.specification_versions sv ON sv.id = sp.current_version_id
                    WHERE sp.status = 'ACTIVE'
                    ORDER BY COALESCE(sp.spec_code, ''), sp.canonical_name, sp.id
                    """
                )
                columns = [item.name for item in cur.description]
                return [dict(zip(columns, row)) for row in cur.fetchall()]

    def list_manual_relations(self, *, include_inactive: bool = False) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            with conn.cursor() as cur:
                if not self._manual_schema_ready(cur):
                    return []
                where = "" if include_inactive else "WHERE mr.is_active IS TRUE"
                cur.execute(
                    f"""
                    SELECT
                        mr.id AS manual_id,
                        mr.project_code,
                        mr.source_specification_id,
                        src.specification_key AS source_specification_key,
                        src.spec_code AS source_spec_code,
                        src.canonical_name AS source_specification_name,
                        mr.source_version_id,
                        sv.version_label AS source_version,
                        mr.target_specification_id,
                        tgt.specification_key AS target_specification_key,
                        tgt.spec_code AS target_spec_code,
                        tgt.canonical_name AS target_specification_name,
                        mr.target_external_key,
                        mr.semantic_type,
                        mr.status,
                        mr.confidence,
                        mr.evidence_kind,
                        mr.evidence_text,
                        mr.target_locator_type,
                        mr.target_locator_value,
                        mr.note,
                        mr.created_by,
                        mr.is_active,
                        mr.metadata,
                        mr.created_at,
                        mr.updated_at
                    FROM public.manual_spec_relations mr
                    JOIN public.specifications src ON src.id = mr.source_specification_id
                    JOIN public.specification_versions sv ON sv.id = mr.source_version_id
                    LEFT JOIN public.specifications tgt ON tgt.id = mr.target_specification_id
                    {where}
                    ORDER BY mr.updated_at DESC, mr.id DESC
                    """
                )
                columns = [item.name for item in cur.description]
                return [dict(zip(columns, row)) for row in cur.fetchall()]

    def create_manual_relation(
        self,
        *,
        source_specification_id: int,
        target_specification_id: int | None,
        target_external_code: str | None,
        target_title_hint: str | None,
        semantic_type: str,
        status: str,
        evidence_kind: str,
        evidence_text: str,
        target_locator_type: str | None = None,
        target_locator_value: str | None = None,
        note: str | None = None,
        created_by: str | None = None,
    ) -> int:
        with self.db.connect() as conn:
            try:
                with conn.cursor() as cur:
                    manual_id = self._save_manual_relation(
                        cur=cur,
                        manual_id=None,
                        source_specification_id=source_specification_id,
                        target_specification_id=target_specification_id,
                        target_external_code=target_external_code,
                        target_title_hint=target_title_hint,
                        semantic_type=semantic_type,
                        status=status,
                        evidence_kind=evidence_kind,
                        evidence_text=evidence_text,
                        target_locator_type=target_locator_type,
                        target_locator_value=target_locator_value,
                        note=note,
                        actor=created_by,
                    )
                conn.commit()
                return manual_id
            except Exception:
                conn.rollback()
                raise

    def update_manual_relation(self, *, manual_id: int, **values: Any) -> int:
        with self.db.connect() as conn:
            try:
                with conn.cursor() as cur:
                    updated = self._save_manual_relation(cur=cur, manual_id=int(manual_id), **values)
                conn.commit()
                return updated
            except Exception:
                conn.rollback()
                raise

    def delete_manual_relation(self, manual_id: int, *, actor: str | None = None) -> None:
        with self.db.connect() as conn:
            try:
                with conn.cursor() as cur:
                    if not self._manual_schema_ready(cur):
                        raise RuntimeError("Manual relation schema is not installed. Run upgrade_manual_spec_relations_v6_5.sql first.")
                    cur.execute("SELECT to_jsonb(mr) FROM public.manual_spec_relations mr WHERE id = %s", (int(manual_id),))
                    row = cur.fetchone()
                    if row is None:
                        raise ValueError(f"Manual relation {manual_id} does not exist.")
                    snapshot = row[0]
                    cur.execute(
                        "UPDATE public.manual_spec_relations SET is_active = FALSE, updated_at = NOW() WHERE id = %s",
                        (int(manual_id),),
                    )
                    cur.execute(
                        """
                        INSERT INTO public.manual_spec_relation_audit(manual_relation_id, action, actor, snapshot, created_at)
                        VALUES (%s, 'DELETE', %s, CAST(%s AS jsonb), NOW())
                        """,
                        (int(manual_id), str(actor or getpass.getuser() or "unknown"), json.dumps(snapshot, default=str, ensure_ascii=False)),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def _save_manual_relation(
        self,
        *,
        cur: Any,
        manual_id: int | None,
        source_specification_id: int,
        target_specification_id: int | None,
        target_external_code: str | None,
        target_title_hint: str | None,
        semantic_type: str,
        status: str,
        evidence_kind: str,
        evidence_text: str,
        target_locator_type: str | None = None,
        target_locator_value: str | None = None,
        note: str | None = None,
        actor: str | None = None,
        created_by: str | None = None,
    ) -> int:
        if not self._manual_schema_ready(cur):
            raise RuntimeError("Manual relation schema is not installed. Run database/upgrade_manual_spec_relations_v6_5.sql first.")

        semantic = str(semantic_type or "").strip().upper()
        if semantic not in self._MANUAL_MEANINGS:
            raise ValueError(f"Unsupported manual relation meaning: {semantic or '-'}")
        requested_status = str(status or "AUTO").strip().upper()
        if requested_status not in self._MANUAL_STATUSES:
            raise ValueError(f"Unsupported status: {requested_status or '-'}")
        evidence_kind_value = str(evidence_kind or "").strip().upper()
        if evidence_kind_value not in self._EVIDENCE_KINDS:
            raise ValueError(f"Unsupported evidence kind: {evidence_kind_value or '-'}")
        evidence = str(evidence_text or "").strip()
        if not evidence:
            raise ValueError("Evidence / engineering decision text is required for a manual relation.")

        locator_type = str(target_locator_type or "").strip().upper()
        if locator_type not in self._LOCATOR_TYPES:
            raise ValueError(f"Unsupported target locator type: {locator_type}")
        if locator_type == "NONE":
            locator_type = ""
        locator_value = str(target_locator_value or "").strip()
        if semantic in {"REFERENCES_SECTION", "CORRESPONDS_TO_SECTION"} and not locator_value:
            raise ValueError(f"{semantic} requires a target section/locator value.")

        cur.execute(
            """
            SELECT id, specification_key, project_code, spec_code, canonical_name, current_version_id
            FROM public.specifications
            WHERE id = %s AND status = 'ACTIVE'
            """,
            (int(source_specification_id),),
        )
        source = cur.fetchone()
        if source is None:
            raise ValueError("Source specification does not exist or is inactive.")
        source_id, _, project_code, _, _, source_version_id = source
        if source_version_id is None:
            raise ValueError("Source specification has no current version.")

        target_id: int | None = int(target_specification_id) if target_specification_id is not None else None
        external_key: str | None = None
        target_code = str(target_external_code or "").strip()
        title_hint = str(target_title_hint or "").strip()
        if target_id is not None:
            if target_id == int(source_id):
                raise ValueError("Source and target specification cannot be the same.")
            cur.execute(
                "SELECT id, project_code, spec_code, canonical_name FROM public.specifications WHERE id = %s AND status = 'ACTIVE'",
                (target_id,),
            )
            target = cur.fetchone()
            if target is None:
                raise ValueError("Target specification does not exist or is inactive.")
            if requested_status == "UNRESOLVED":
                raise ValueError("An existing target specification cannot be manually marked UNRESOLVED.")
            resolved_status = "AMBIGUOUS" if requested_status == "AMBIGUOUS" else "CONFIRMED"
        else:
            if not target_code:
                raise ValueError("Choose an existing target or enter an external Toyota Specification No.")
            if not EngineeringRelationGraphService.is_primary_spec_code(target_code):
                raise ValueError(f"'{target_code}' is not accepted as a primary Toyota Specification No.")
            normalized = EngineeringRelationGraphService._normalize_code(target_code)
            external_key = f"SPEC:{normalized}"
            if requested_status == "CONFIRMED":
                raise ValueError("CONFIRMED requires an existing target specification in the current database.")
            resolved_status = "AMBIGUOUS" if requested_status == "AMBIGUOUS" else "UNRESOLVED"

        actor_value = str(actor or created_by or getpass.getuser() or "unknown").strip()[:200]
        metadata = {
            "toyota_reference_kind": semantic,
            "manual_relation": True,
            "target_title_hint": title_hint or None,
            "target_locator_type": locator_type or None,
            "target_locator_value": locator_value or None,
            "evidence_kind": evidence_kind_value,
        }
        metadata_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True)

        if manual_id is None:
            cur.execute(
                """
                INSERT INTO public.manual_spec_relations(
                    project_code, source_specification_id, source_version_id,
                    target_specification_id, target_external_key,
                    semantic_type, status, confidence,
                    evidence_kind, evidence_text,
                    target_locator_type, target_locator_value,
                    note, created_by, metadata, is_active,
                    created_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, 1.0, %s, %s, %s, %s, %s, %s,
                        CAST(%s AS jsonb), TRUE, NOW(), NOW())
                RETURNING id
                """,
                (
                    str(project_code or ""), int(source_id), int(source_version_id),
                    target_id, external_key, semantic, resolved_status,
                    evidence_kind_value, evidence, locator_type or None, locator_value or None,
                    str(note or "").strip() or None, actor_value, metadata_json,
                ),
            )
            saved_id = int(cur.fetchone()[0])
            action = "CREATE"
        else:
            cur.execute("SELECT id FROM public.manual_spec_relations WHERE id = %s AND is_active IS TRUE", (int(manual_id),))
            if cur.fetchone() is None:
                raise ValueError(f"Manual relation {manual_id} does not exist or is inactive.")
            cur.execute(
                """
                UPDATE public.manual_spec_relations
                SET project_code = %s,
                    source_specification_id = %s,
                    source_version_id = %s,
                    target_specification_id = %s,
                    target_external_key = %s,
                    semantic_type = %s,
                    status = %s,
                    confidence = 1.0,
                    evidence_kind = %s,
                    evidence_text = %s,
                    target_locator_type = %s,
                    target_locator_value = %s,
                    note = %s,
                    metadata = CAST(%s AS jsonb),
                    updated_at = NOW()
                WHERE id = %s
                """,
                (
                    str(project_code or ""), int(source_id), int(source_version_id),
                    target_id, external_key, semantic, resolved_status,
                    evidence_kind_value, evidence, locator_type or None, locator_value or None,
                    str(note or "").strip() or None, metadata_json, int(manual_id),
                ),
            )
            saved_id = int(manual_id)
            action = "UPDATE"

        cur.execute("SELECT to_jsonb(mr) FROM public.manual_spec_relations mr WHERE id = %s", (saved_id,))
        snapshot = (cur.fetchone() or ({},))[0]
        cur.execute(
            """
            INSERT INTO public.manual_spec_relation_audit(manual_relation_id, action, actor, snapshot, created_at)
            VALUES (%s, %s, %s, CAST(%s AS jsonb), NOW())
            """,
            (saved_id, action, actor_value, json.dumps(snapshot, default=str, ensure_ascii=False)),
        )
        return saved_id

    def reconcile_unresolved(self, *, project_code: str | None = None) -> int:
        with self.db.connect() as conn:
            try:
                with conn.cursor() as cur:
                    count = EngineeringRelationManager().reconcile_unresolved(
                        cur=cur,
                        project_code=project_code,
                    )
                conn.commit()
                return count
            except Exception:
                conn.rollback()
                raise

    def _reconcile_manual_unresolved(self, *, cur: Any, project_code: str | None = None) -> int:
        if not self._manual_schema_ready(cur):
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
        for manual_id, source_id, external_key in rows:
            code = str(external_key or "")[len("SPEC:"):].strip()
            normalized = EngineeringRelationGraphService._normalize_code(code)
            cur.execute(
                """
                SELECT id
                FROM public.specifications
                WHERE id <> %s
                  AND status = 'ACTIVE'
                  AND UPPER(REPLACE(REPLACE(COALESCE(spec_code, ''), ' ', ''), '-', '_')) = %s
                ORDER BY id
                """,
                (int(source_id), normalized.replace(" ", "").replace("-", "_")),
            )
            candidates = [int(row[0]) for row in cur.fetchall()]
            if len(candidates) == 1:
                patch = json.dumps(
                    {"manual_reconciled": True, "manual_reconciliation_method": "EXACT_SPEC_CODE"},
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
                    (candidates[0], patch, int(manual_id)),
                )
                resolved += 1
            elif len(candidates) > 1:
                cur.execute(
                    "UPDATE public.manual_spec_relations SET status = 'AMBIGUOUS', updated_at = NOW() WHERE id = %s",
                    (int(manual_id),),
                )
        return resolved
