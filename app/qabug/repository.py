from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Mapping


class QABugRepositoryError(RuntimeError):
    pass


def _dt(value: Any):
    text = str(value or "").strip()
    if not text:
        return None
    # PostgreSQL can parse most ISO/Jira date forms through text cast, but using
    # Python datetimes for common cases improves portability.
    for fmt in (
        "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%d", "%Y/%m/%d",
    ):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _split_history(value: str) -> list[dict[str, str]]:
    """Parse Jira export history lines conservatively; preserve unmatched text."""
    text = str(value or "").strip()
    if not text:
        return []
    # A Jira comment/worklog entry often begins: YYYY/MM/DD HH:MM;author;body
    markers = list(re.finditer(r"(?m)(?=\d{4}[/-]\d{1,2}[/-]\d{1,2}\s+\d{1,2}:\d{2};[^;\r\n]*;)", text))
    if not markers:
        return [{"body": text}]
    result: list[dict[str, str]] = []
    for i, marker in enumerate(markers):
        end = markers[i + 1].start() if i + 1 < len(markers) else len(text)
        block = text[marker.start():end].strip()
        parts = block.split(";", 2)
        if len(parts) == 3:
            result.append({"at": parts[0].strip(), "author": parts[1].strip(), "body": parts[2].strip()})
        elif block:
            result.append({"body": block})
    return result


def _split_attachments(value: str) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for item in _split_history(value):
        body = item.get("body", "")
        url = ""
        file_name = body
        if ";" in body:
            file_name, url = body.split(";", 1)
        elif "http" in body:
            match = re.search(r"https?://\S+", body)
            if match:
                url = match.group(0)
                file_name = body[:match.start()].strip(" ;")
        result.append({**item, "file_name": file_name.strip(), "uri": url.strip()})
    return result


class QABugRepository:
    def __init__(self, profile, password: str) -> None:
        self.profile = profile
        self.password = password

    def _connect(self):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise QABugRepositoryError("psycopg is required for QA/BUG PostgreSQL import.") from exc
        kwargs = self.profile.dsn_kwargs()
        kwargs["password"] = self.password
        return psycopg.connect(**kwargs, row_factory=dict_row)

    def begin_import(self, *, source_file: str, record_type: str, source_hash: str, file_size_bytes: int = 0, options: Mapping[str, Any] | None = None) -> tuple[int, int]:
        from pathlib import Path
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.qabug_source_files(source_file_name,source_file_path,source_format,record_type,source_hash,file_size_bytes,last_imported_at)
                    VALUES(%s,%s,%s,%s,%s,%s,CURRENT_TIMESTAMP)
                    ON CONFLICT(record_type,source_hash) DO UPDATE SET
                        source_file_name=EXCLUDED.source_file_name, source_file_path=EXCLUDED.source_file_path,
                        file_size_bytes=EXCLUDED.file_size_bytes, last_imported_at=CURRENT_TIMESTAMP
                    RETURNING id
                    """,
                    (Path(source_file).name,str(Path(source_file).resolve()),Path(source_file).suffix.lstrip('.').upper(),record_type,source_hash,int(file_size_bytes or 0)),
                )
                source_file_id=int(cur.fetchone()["id"])
                cur.execute(
                    "INSERT INTO public.qabug_import_runs(source_file_id,record_type,mode,status,options) VALUES(%s,%s,'UPSERT','RUNNING',%s::jsonb) RETURNING id",
                    (source_file_id,record_type,json.dumps(dict(options or {}),ensure_ascii=False)),
                )
                run_id=int(cur.fetchone()["id"])
                conn.commit()
                return source_file_id,run_id
        except Exception as exc:
            raise QABugRepositoryError(f"Cannot start QA/BUG import run: {exc}") from exc

    def finish_import(self, run_id: int, *, status: str, rows_seen: int, rows_valid: int, rows_upserted: int, rows_skipped: int, rows_failed: int, chunks_upserted: int, error_summary: str = "") -> None:
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE public.qabug_import_runs SET status=%s,finished_at=CURRENT_TIMESTAMP,
                        rows_seen=%s,rows_valid=%s,rows_upserted=%s,rows_skipped=%s,rows_failed=%s,
                        chunks_upserted=%s,error_summary=%s WHERE id=%s
                    """,
                    (status,rows_seen,rows_valid,rows_upserted,rows_skipped,rows_failed,chunks_upserted,error_summary or None,run_id),
                )
                cur.execute(
                    """
                    UPDATE public.qabug_source_files sf
                    SET row_count=%s, last_imported_at=CURRENT_TIMESTAMP
                    FROM public.qabug_import_runs ir
                    WHERE ir.id=%s AND ir.source_file_id=sf.id
                    """,
                    (rows_seen, run_id),
                )
                conn.commit()
        except Exception as exc:
            raise QABugRepositoryError(f"Cannot finish QA/BUG import run: {exc}") from exc

    def record_import_error(
        self,
        import_run_id: int,
        *,
        source_row_number: int | None = None,
        issue_key: str = "",
        error_code: str = "IMPORT_RECORD_FAILED",
        error_message: str,
        raw_json: Mapping[str, Any] | None = None,
    ) -> None:
        """Persist a per-record import failure without mutating business data.

        Import-error persistence is best-effort at the pipeline boundary: callers
        may continue the import even when the original row cannot be upserted.
        The raw payload is deliberately bounded by the caller and stored as JSONB
        for review/retry provenance.
        """
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.qabug_import_errors(
                        import_run_id, source_row_number, issue_key, error_code, error_message, raw_json
                    ) VALUES(%s,%s,%s,%s,%s,%s::jsonb)
                    """,
                    (
                        int(import_run_id),
                        int(source_row_number) if source_row_number is not None else None,
                        str(issue_key or "") or None,
                        str(error_code or "IMPORT_RECORD_FAILED"),
                        str(error_message or "")[:8000],
                        json.dumps(dict(raw_json or {}), ensure_ascii=False),
                    ),
                )
                conn.commit()
        except Exception as exc:
            raise QABugRepositoryError(f"Cannot persist QA/BUG import error: {exc}") from exc

    def upsert_document(self, document: Mapping[str, Any], *, source_file_id: int | None = None) -> tuple[int, int]:
        identity = document["identity"]
        meta = document["structured_metadata"]
        n = document["normalized_fields"]
        hashes = document["hashes"]
        source = document["source"]
        record_type = str(document["document_type"])
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.qabug_records(
                        record_key, record_type, issue_key, issue_id, parent_issue_id, issue_type,
                        project_key, project_name, summary, description,
                        status_raw, status_norm, priority_raw, priority_norm, resolution_raw, resolution_norm,
                        assignee, reporter, creator, component, release_plan_version, affected_version,
                        software_version, grade, destination, model, environment, labels,
                        created_at_source, updated_at_source, resolved_at_source, due_at_source,
                        source_file_id, source_sheet, source_row_number, source_hash, content_hash, index_hash,
                        raw_json, custom_fields, normalized_fields, quality_flags, acl, is_queryable, is_current, updated_at
                    ) VALUES (
                        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s,TRUE,CURRENT_TIMESTAMP
                    )
                    ON CONFLICT (record_key) DO UPDATE SET
                        issue_key=EXCLUDED.issue_key, issue_id=EXCLUDED.issue_id, parent_issue_id=EXCLUDED.parent_issue_id,
                        issue_type=EXCLUDED.issue_type, project_key=EXCLUDED.project_key, project_name=EXCLUDED.project_name,
                        summary=EXCLUDED.summary, description=EXCLUDED.description,
                        status_raw=EXCLUDED.status_raw, status_norm=EXCLUDED.status_norm,
                        priority_raw=EXCLUDED.priority_raw, priority_norm=EXCLUDED.priority_norm,
                        resolution_raw=EXCLUDED.resolution_raw, resolution_norm=EXCLUDED.resolution_norm,
                        assignee=EXCLUDED.assignee, reporter=EXCLUDED.reporter, creator=EXCLUDED.creator,
                        component=EXCLUDED.component, release_plan_version=EXCLUDED.release_plan_version,
                        affected_version=EXCLUDED.affected_version, software_version=EXCLUDED.software_version,
                        grade=EXCLUDED.grade, destination=EXCLUDED.destination, model=EXCLUDED.model,
                        environment=EXCLUDED.environment, labels=EXCLUDED.labels,
                        created_at_source=EXCLUDED.created_at_source, updated_at_source=EXCLUDED.updated_at_source,
                        resolved_at_source=EXCLUDED.resolved_at_source, due_at_source=EXCLUDED.due_at_source,
                        source_file_id=EXCLUDED.source_file_id, source_sheet=EXCLUDED.source_sheet,
                        source_row_number=EXCLUDED.source_row_number, source_hash=EXCLUDED.source_hash,
                        content_hash=EXCLUDED.content_hash, index_hash=EXCLUDED.index_hash,
                        raw_json=EXCLUDED.raw_json, custom_fields=EXCLUDED.custom_fields,
                        normalized_fields=EXCLUDED.normalized_fields, quality_flags=EXCLUDED.quality_flags,
                        acl=EXCLUDED.acl, is_queryable=EXCLUDED.is_queryable, is_current=TRUE, updated_at=CURRENT_TIMESTAMP
                    RETURNING id
                    """,
                    (
                        identity.get("record_key"), record_type, identity.get("issue_key"), identity.get("issue_id"), identity.get("parent_issue_id"), n.get("issue_type"),
                        identity.get("project_key"), meta.get("project_name"), n.get("summary"), n.get("description"),
                        meta.get("status"), meta.get("status_norm"), meta.get("priority"), meta.get("priority_norm"), meta.get("resolution"), meta.get("resolution_norm"),
                        meta.get("assignee"), meta.get("reporter"), n.get("creator"), meta.get("component"), meta.get("release_plan_version"), meta.get("affected_version"),
                        meta.get("software_version"), meta.get("grade"), meta.get("destination"), meta.get("model"), n.get("environment"), list(meta.get("labels") or []),
                        _dt(meta.get("created_at")), _dt(meta.get("updated_at")), _dt(meta.get("resolved_at")), _dt(meta.get("due_at")),
                        source_file_id, source.get("sheet_name"), source.get("row_number"), hashes.get("source_hash"), hashes.get("content_hash"), hashes.get("index_hash"),
                        json.dumps(document.get("raw_record") or {}, ensure_ascii=False), json.dumps(document.get("custom_fields") or {}, ensure_ascii=False),
                        json.dumps(n, ensure_ascii=False), json.dumps(document.get("quality_flags") or [], ensure_ascii=False), json.dumps(document.get("acl") or {}, ensure_ascii=False),
                        bool(document.get("queryable", True)),
                    ),
                )
                record_id = int(cur.fetchone()["id"])
                self._upsert_detail(cur, record_id, record_type, n)
                self._replace_children(cur, record_id, document)
                # Mark previous chunks inactive before upserting the current canonical set.
                cur.execute("UPDATE public.qabug_chunks SET is_current=FALSE, updated_at=CURRENT_TIMESTAMP WHERE record_id=%s", (record_id,))
                chunk_count = 0
                for chunk in document.get("chunks") or []:
                    payload = chunk.get("payload") or {}
                    cur.execute(
                        """
                        INSERT INTO public.qabug_chunks(
                            record_id, chunk_key, section_type, section_order, content, content_hash, index_hash,
                            metadata, qdrant_point_id, qdrant_collection, embedding_model, embedding_version, is_current, updated_at
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,TRUE,CURRENT_TIMESTAMP)
                        ON CONFLICT (chunk_key) DO UPDATE SET
                            record_id=EXCLUDED.record_id, section_type=EXCLUDED.section_type,
                            section_order=EXCLUDED.section_order, content=EXCLUDED.content,
                            content_hash=EXCLUDED.content_hash, index_hash=EXCLUDED.index_hash,
                            metadata=EXCLUDED.metadata, qdrant_point_id=EXCLUDED.qdrant_point_id,
                            qdrant_collection=EXCLUDED.qdrant_collection,
                            embedding_model=EXCLUDED.embedding_model, embedding_version=EXCLUDED.embedding_version,
                            is_current=TRUE, updated_at=CURRENT_TIMESTAMP,
                            embedding_status=CASE WHEN public.qabug_chunks.content_hash=EXCLUDED.content_hash AND public.qabug_chunks.index_hash=EXCLUDED.index_hash THEN public.qabug_chunks.embedding_status ELSE 'PENDING' END,
                            embedded_content_hash=CASE WHEN public.qabug_chunks.content_hash=EXCLUDED.content_hash AND public.qabug_chunks.index_hash=EXCLUDED.index_hash THEN public.qabug_chunks.embedded_content_hash ELSE NULL END,
                            embedded_at=CASE WHEN public.qabug_chunks.content_hash=EXCLUDED.content_hash AND public.qabug_chunks.index_hash=EXCLUDED.index_hash THEN public.qabug_chunks.embedded_at ELSE NULL END
                        """,
                        (
                            record_id, chunk.get("chunk_key"), chunk.get("section_type"), chunk.get("section_order"), chunk.get("content"),
                            chunk.get("content_hash"), chunk.get("index_hash"), json.dumps(payload, ensure_ascii=False), chunk.get("point_id"),
                            payload.get("collection") or document.get("embedding_metadata", {}).get("collection") or "rag_qabug_current_chunks",
                            document.get("embedding_metadata", {}).get("model") or "BAAI/bge-m3",
                            document.get("embedding_metadata", {}).get("version") or "current-index-v1",
                        ),
                    )
                    chunk_count += 1
                # Persist quality findings for this current record; keep history but resolve old open findings first.
                cur.execute("UPDATE public.qabug_quality_findings SET resolved_at=CURRENT_TIMESTAMP WHERE record_id=%s AND resolved_at IS NULL", (record_id,))
                for finding in document.get("quality_flags") or []:
                    cur.execute(
                        "INSERT INTO public.qabug_quality_findings(record_id,severity,code,message,details) VALUES(%s,%s,%s,%s,%s::jsonb)",
                        (record_id, finding.get("severity", "INFO"), finding.get("code", "UNKNOWN"), finding.get("message", ""), json.dumps(finding, ensure_ascii=False)),
                    )
                conn.commit()
                return record_id, chunk_count
        except Exception as exc:
            raise QABugRepositoryError(f"QA/BUG upsert failed for {identity.get('record_key')}: {exc}") from exc

    @staticmethod
    def _upsert_detail(cur, record_id: int, record_type: str, n: Mapping[str, Any]) -> None:
        if record_type == "QA":
            cur.execute(
                """
                INSERT INTO public.qa_details(record_id,precondition,procedure_expected,actual_behavior,answer,answer_type,
                    answer_requested_at,final_answer_group,final_answer,final_answerer,rationale,reference_spec,sheet_name,
                    test_spec_name,test_spec_defect_detail,item_cell,responsible_group,change_target,customer_report)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(record_id) DO UPDATE SET
                    precondition=EXCLUDED.precondition, procedure_expected=EXCLUDED.procedure_expected,
                    actual_behavior=EXCLUDED.actual_behavior, answer=EXCLUDED.answer, answer_type=EXCLUDED.answer_type,
                    answer_requested_at=EXCLUDED.answer_requested_at, final_answer_group=EXCLUDED.final_answer_group,
                    final_answer=EXCLUDED.final_answer, final_answerer=EXCLUDED.final_answerer, rationale=EXCLUDED.rationale,
                    reference_spec=EXCLUDED.reference_spec, sheet_name=EXCLUDED.sheet_name, test_spec_name=EXCLUDED.test_spec_name,
                    test_spec_defect_detail=EXCLUDED.test_spec_defect_detail, item_cell=EXCLUDED.item_cell,
                    responsible_group=EXCLUDED.responsible_group, change_target=EXCLUDED.change_target,
                    customer_report=EXCLUDED.customer_report
                """,
                (record_id,n.get("precondition"),n.get("procedure_expected"),n.get("actual_behavior"),n.get("answer"),n.get("answer_type"),_dt(n.get("answer_requested_at")),n.get("final_answer_group"),n.get("final_answer"),n.get("final_answerer"),n.get("rationale"),n.get("reference_spec"),n.get("sheet_name"),n.get("test_spec_name"),n.get("test_spec_defect_detail"),n.get("item_cell"),n.get("responsible_group"),n.get("change_target"),n.get("customer_report")),
            )
        else:
            cur.execute(
                """
                INSERT INTO public.bug_details(record_id,phenomenon,repro_steps,expected_result,actual_result,analysis_result,analysis_comment,
                    analysis_owner,root_cause,cause_module,cause_company,countermeasure,countermeasure_comment,countermeasure_owner,
                    countermeasure_scope,effect_result,effect_version,effect_date,recurrence_prevention_escape,recurrence_prevention_injection,
                    impact_analysis,impact,occurrence_probability,occurrence_frequency,occurrence_date,occurrence_environment_detail,
                    recovery_procedure,problem_type,function_class)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(record_id) DO UPDATE SET
                    phenomenon=EXCLUDED.phenomenon,repro_steps=EXCLUDED.repro_steps,expected_result=EXCLUDED.expected_result,
                    actual_result=EXCLUDED.actual_result,analysis_result=EXCLUDED.analysis_result,analysis_comment=EXCLUDED.analysis_comment,
                    analysis_owner=EXCLUDED.analysis_owner,root_cause=EXCLUDED.root_cause,cause_module=EXCLUDED.cause_module,
                    cause_company=EXCLUDED.cause_company,countermeasure=EXCLUDED.countermeasure,countermeasure_comment=EXCLUDED.countermeasure_comment,
                    countermeasure_owner=EXCLUDED.countermeasure_owner,countermeasure_scope=EXCLUDED.countermeasure_scope,
                    effect_result=EXCLUDED.effect_result,effect_version=EXCLUDED.effect_version,effect_date=EXCLUDED.effect_date,
                    recurrence_prevention_escape=EXCLUDED.recurrence_prevention_escape,
                    recurrence_prevention_injection=EXCLUDED.recurrence_prevention_injection,impact_analysis=EXCLUDED.impact_analysis,
                    impact=EXCLUDED.impact,occurrence_probability=EXCLUDED.occurrence_probability,
                    occurrence_frequency=EXCLUDED.occurrence_frequency,occurrence_date=EXCLUDED.occurrence_date,
                    occurrence_environment_detail=EXCLUDED.occurrence_environment_detail,recovery_procedure=EXCLUDED.recovery_procedure,
                    problem_type=EXCLUDED.problem_type,function_class=EXCLUDED.function_class
                """,
                (record_id,n.get("phenomenon"),n.get("repro_steps"),n.get("expected_result"),n.get("actual_result"),n.get("analysis_result"),n.get("analysis_comment"),n.get("analysis_owner"),n.get("root_cause"),n.get("cause_module"),n.get("cause_company"),n.get("countermeasure"),n.get("countermeasure_comment"),n.get("countermeasure_owner"),n.get("countermeasure_scope"),n.get("effect_result"),n.get("effect_version"),_dt(n.get("effect_date")),n.get("recurrence_prevention_escape"),n.get("recurrence_prevention_injection"),n.get("impact_analysis"),n.get("impact"),n.get("occurrence_probability"),n.get("occurrence_frequency"),_dt(n.get("occurrence_date")),n.get("occurrence_environment_detail"),n.get("recovery_procedure"),n.get("problem_type"),n.get("function_class")),
            )

    @staticmethod
    def _replace_children(cur, record_id: int, document: Mapping[str, Any]) -> None:
        import hashlib
        cur.execute("DELETE FROM public.qabug_relations WHERE source_record_id=%s", (record_id,))
        for rel in document.get("relations") or []:
            raw = json.dumps(rel, ensure_ascii=False, sort_keys=True)
            rh = hashlib.sha256(raw.encode("utf-8")).hexdigest()
            cur.execute(
                "INSERT INTO public.qabug_relations(source_record_id,relation_type,target_issue_key,source_field,relation_hash,metadata) VALUES(%s,%s,%s,%s,%s,%s::jsonb) ON CONFLICT DO NOTHING",
                (record_id, rel.get("relation_type") or "RELATES", rel.get("target_issue_key") or "", rel.get("source_field"), rh, raw),
            )
        cur.execute("DELETE FROM public.qabug_comments WHERE record_id=%s", (record_id,))
        for seq, item in enumerate(_split_history(str(document.get("normalized_fields", {}).get("comments") or "")), 1):
            raw = json.dumps(item, ensure_ascii=False, sort_keys=True)
            ch = hashlib.sha256(raw.encode("utf-8")).hexdigest()
            cur.execute(
                "INSERT INTO public.qabug_comments(record_id,sequence_no,commented_at,author,body,comment_hash,metadata) VALUES(%s,%s,%s,%s,%s,%s,%s::jsonb) ON CONFLICT DO NOTHING",
                (record_id, seq, _dt(item.get("at")), item.get("author"), item.get("body") or "", ch, raw),
            )
        cur.execute("DELETE FROM public.qabug_attachments WHERE record_id=%s", (record_id,))
        for item in _split_attachments(str(document.get("normalized_fields", {}).get("attachments") or "")):
            raw = json.dumps(item, ensure_ascii=False, sort_keys=True)
            ah = hashlib.sha256(raw.encode("utf-8")).hexdigest()
            cur.execute(
                "INSERT INTO public.qabug_attachments(record_id,file_name,uri,added_at,added_by,attachment_hash,metadata) VALUES(%s,%s,%s,%s,%s,%s,%s::jsonb) ON CONFLICT DO NOTHING",
                (record_id, item.get("file_name"), item.get("uri"), _dt(item.get("at")), item.get("author"), ah, raw),
            )
