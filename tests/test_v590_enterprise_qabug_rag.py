from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.ai.agent import FastIntentRouter
from app.ai.ollama_client import OllamaClient, OllamaError
from app.ai.retrieval import EnterpriseRetriever
from app.qabug.canonical import QABugCanonicalizer, RAG_JSON_SCHEMA, stable_point_id
from app.qabug.importer import QABugFileReader, QABugImportPipeline
from app.qabug.qdrant_sync import QABUG_COLLECTION, QABUG_PAYLOAD_INDEX_CONTRACT
from app.qabug.schema_manager import QABugSchemaManager


ROOT = Path(__file__).resolve().parents[1]


def _doc(record_type: str = "BUG", *, values=None):
    headers = ["要約", "課題キー", "課題タイプ", "ステータス", "優先度", "説明", "カスタム フィールド (任意項目)"]
    row = values or ["Audio issue", "BUG-123", "不具合管理", "クローズ", "高", "【現象】音が出ない\n【期待結果】音が出る", "custom-value"]
    c = QABugCanonicalizer()
    return c.canonicalize(
        record_type=record_type,
        mapped_headers=c.map_headers(headers),
        values=row,
        source_file="sample.tsv",
        source_row=2,
        source_format="TSV",
    )


def test_qabug_isolated_runtime_contract():
    assert QABUG_COLLECTION == "rag_qabug_current_chunks"
    assert QABUG_PAYLOAD_INDEX_CONTRACT["issue_key"] == "keyword"
    assert QABUG_PAYLOAD_INDEX_CONTRACT["record_type"] == "keyword"
    assert len(QABUG_PAYLOAD_INDEX_CONTRACT) >= 18


def test_stable_point_id_is_deterministic_positive_int64():
    a = stable_point_id("BUG:BUG-123:SUMMARY:0")
    b = stable_point_id("BUG:BUG-123:SUMMARY:0")
    assert a == b
    assert 0 <= a <= 0x7FFF_FFFF_FFFF_FFFF


def test_canonical_document_preserves_unknown_custom_fields_and_is_queryable():
    doc = _doc()
    assert doc is not None
    assert doc["identity"]["issue_key"] == "BUG-123"
    assert doc["queryable"] is True
    assert doc["custom_fields"]["カスタム フィールド (任意項目)"] == "custom-value"
    section_types = {x["section_type"] for x in doc["content_sections"]}
    assert "BUG_PHENOMENON" in section_types
    assert "BUG_EXPECTED_RESULT" in section_types


def test_column_count_mismatch_is_preserved_but_quarantined():
    c = QABugCanonicalizer()
    mapped = c.map_headers(["要約", "課題キー", "ステータス", "優先度"])
    doc = c.canonicalize(
        record_type="BUG",
        mapped_headers=mapped,
        values=["broken", "BUG-9"],
        source_file="broken.tsv",
        source_row=9,
    )
    assert doc is not None
    codes = {x["code"] for x in doc["quality_flags"]}
    assert "SOURCE_COLUMN_COUNT_MISMATCH" in codes
    assert doc["queryable"] is False
    assert doc["source"]["source_column_count"] == 2
    assert doc["source"]["expected_column_count"] == 4


def test_deterministic_fragment_repair_marks_provenance(tmp_path):
    path = tmp_path / "broken.tsv"
    # 5-column header. Broken record is 2-column prefix + one continuation
    # field + 2-column suffix, so exact-width repair is deterministic.
    path.write_text(
        "要約\t課題キー\t説明\tステータス\t優先度\n"
        "Broken export\tBUG-77\n"
        "multiline description continuation\n"
        "クローズ\t高\n",
        encoding="utf-8",
    )
    rows = list(QABugFileReader.iter_delimited(path, delimiter="\t"))
    assert len(rows) == 1
    _, source_row, headers, values = rows[0]
    assert len(values) == len(headers) == 5
    doc = QABugCanonicalizer().canonicalize(
        record_type="BUG",
        mapped_headers=QABugCanonicalizer.map_headers(headers),
        values=values,
        source_file=str(path),
        source_row=source_row,
    )
    assert doc is not None
    assert doc["queryable"] is True
    assert doc["source"]["repair_info"]["strategy"] == "PREFIX_MULTILINE_SUFFIX_EXACT_WIDTH"
    assert "SOURCE_FRAGMENT_REPAIRED" in {x["code"] for x in doc["quality_flags"]}


def test_rag_json_schema_requires_queryable_governance_field():
    assert "queryable" in RAG_JSON_SCHEMA["required"]
    assert RAG_JSON_SCHEMA["properties"]["queryable"]["type"] == "boolean"


def test_schema_is_non_destructive_and_contains_quarantine_contract():
    ddl = "\n".join(QABugSchemaManager._ddl_statements()).upper()
    assert "DROP TABLE" not in ddl
    assert "TRUNCATE" not in ddl
    assert "DROP DATABASE" not in ddl
    assert "IS_QUERYABLE BOOLEAN" in ddl
    assert "QABUG_QUARANTINE_RECORDS" in ddl
    assert "R.IS_CURRENT AND R.IS_QUERYABLE" in ddl
    assert "QABUG_CURRENT_CHUNKS" in ddl


def test_chunk_index_does_not_reference_nonexistent_queryable_column():
    ddl = "\n".join(QABugSchemaManager._ddl_statements())
    assert "ix_qabug_chunks_record_current ON public.qabug_chunks(record_id, section_order) WHERE is_current AND is_queryable" not in ddl


def test_qabug_sql_filter_excludes_quarantined_records():
    where, params = EnterpriseRetriever._qabug_where({"record_type": "BUG"})
    assert "r.is_current" in where
    assert "r.is_queryable" in where
    assert params == ["BUG"]


def test_custom_field_filter_is_parameterized_and_never_interpolated():
    key = "カスタム フィールド (秘密っぽい名前)"
    filters = {"custom_field_key": key, "custom_field_value": "Audio", "custom_field_match": "CONTAINS"}
    where, params = EnterpriseRetriever._qabug_where(filters)
    assert "custom_fields ->> %s" in where
    assert key not in where
    assert key in params
    assert "%Audio%" in params

    exact_where, exact_params = EnterpriseRetriever._qabug_where({
        "custom_field_key": key, "custom_field_value": "Audio", "custom_field_match": "EXACT"
    })
    assert "custom_fields @> %s::jsonb" in exact_where
    assert key not in exact_where
    assert json.loads(exact_params[-1]) == {key: "Audio"}


def test_issue_filter_parser_supports_deep_enterprise_fields_and_generic_custom_field():
    q = "BUG root cause: race; effect version: 01.09.00.A; custom[任意項目]=ABC"
    f = EnterpriseRetriever._parse_issue_filters(q)
    assert f["record_type"] == "BUG"
    assert f["root_cause"] == "race"
    assert f["effect_version"] == "01.09.00.A"
    assert f["custom_field_key"] == "任意項目"
    assert f["custom_field_value"] == "ABC"
    assert f["custom_field_match"] == "EXACT"


def test_system_runtime_questions_use_runtime_meta_route():
    assert FastIntentRouter.classify("现在Qdrant有哪些collection和payload索引？").route == "SYSTEM_META"
    assert FastIntentRouter.classify("这个工具SQL一键建表会建立哪些表和索引？").route == "SYSTEM_META"
    # Generic conceptual questions are not falsely presented as live runtime facts.
    assert FastIntentRouter.classify("RAG是什么？").route != "SYSTEM_META"


def test_spec_catalog_and_issue_counts_remain_structured():
    assert FastIntentRouter.classify("现在库里有哪些式样书？").route == "SPEC_CATALOG"
    assert FastIntentRouter.classify("现在有多少Open BUG？").route == "ISSUE_STATS"


def test_qdrant_sync_excludes_quarantined_rows_and_prunes_derived_orphans():
    source = (ROOT / "app/qabug/qdrant_sync.py").read_text(encoding="utf-8")
    assert "r.is_current AND r.is_queryable" in source
    assert "existing - eligible" in source
    assert "delete_points" in source


def test_cli_uses_dataclass_asdict_not_slots_dict():
    source = (ROOT / "scripts/qabug_enterprise.py").read_text(encoding="utf-8")
    assert "asdict(report)" in source
    assert "report.__dict__" not in source


def test_ollama_normalizes_structured_content_to_string():
    messages = OllamaClient._normalize_messages([
        {"role": "system", "content": {"scope": "QABUG", "count": 3}},
        {"role": "user", "content": ["a", "b"]},
    ])
    assert all(isinstance(x["content"], str) for x in messages)
    assert json.loads(messages[0]["content"])["count"] == 3


def test_ollama_http_400_keeps_server_error_body_without_prompt_text():
    class Req:
        url = "http://127.0.0.1:11434/api/chat"

    class Resp:
        status_code = 400
        reason = "Bad Request"
        text = '{"error":"messages[2].content must be string"}'
        request = Req()

        @staticmethod
        def json():
            return {"error": "messages[2].content must be string"}

    with pytest.raises(OllamaError) as exc:
        OllamaClient._raise_for_status(Resp(), operation="chat request")
    message = str(exc.value)
    assert "HTTP 400" in message
    assert "messages[2].content must be string" in message
    assert "/api/chat" in message


def test_realistic_mapping_report_shape_is_lossless(tmp_path):
    path = tmp_path / "qa.tsv"
    path.write_text(
        "要約\t課題キー\t課題タイプ\tステータス\t優先度\tカスタム フィールド (未知)\n"
        "Question\tQA-1\t仕様問題管理\tクローズ\t中\tkeep-me\n",
        encoding="utf-8",
    )
    report = QABugImportPipeline().field_mapping_report(path)
    assert report["column_count"] == 6
    assert report["unknown_policy"] == "PRESERVE_IN_RAW_JSON_AND_CUSTOM_FIELDS"
    assert any("未知" in x for x in report["unknown"])


def test_sql_only_qabug_filters_disable_semantic_weakening_contract():
    assert EnterpriseRetriever._qabug_structured_only_filter_keys({"record_type": "BUG", "status_norm": "OPEN"}) == ()
    keys = EnterpriseRetriever._qabug_structured_only_filter_keys({
        "record_type": "BUG",
        "root_cause": "race",
        "custom_field_key": "任意項目",
        "custom_field_value": "ABC",
        "custom_field_match": "EXACT",
        "date_basis": "updated",
        "date_from": "2026-01-01",
    })
    assert "root_cause" in keys
    assert "custom_field_key" in keys
    assert "custom_field_value" in keys
    assert "custom_field_match" in keys
    assert "date_from" in keys
    assert "date_basis" not in keys


def test_explicit_source_language_priority_and_status_are_normalized():
    f = EnterpriseRetriever._parse_issue_filters("BUG 优先度:高; ステータス:クローズ")
    assert f["record_type"] == "BUG"
    assert f["priority_norm"] == "HIGH"
    assert f["status_norm"] == "CLOSED"


def test_enterprise_date_filters_cover_occurrence_answer_requested_and_due():
    occurrence = EnterpriseRetriever._parse_issue_filters("発生日 2020-01-01 到 2020-12-31")
    assert occurrence["date_basis"] == "occurrence"
    where, params = EnterpriseRetriever._qabug_where(occurrence)
    assert "bd.occurrence_date >= %s::date" in where
    assert "bd.occurrence_date < (%s::date + INTERVAL '1 day')" in where
    assert params[-2:] == ["2020-01-01", "2020-12-31"]

    requested = EnterpriseRetriever._parse_issue_filters("回答希望日 2020-01-01 之后")
    assert requested["date_basis"] == "answer_requested"
    where, _ = EnterpriseRetriever._qabug_where(requested)
    assert "qd.answer_requested_at >= %s::date" in where

    due = EnterpriseRetriever._parse_issue_filters("期限 2020-01-01 之前")
    assert due["date_basis"] == "due"
    where, _ = EnterpriseRetriever._qabug_where(due)
    assert "r.due_at_source < (%s::date + INTERVAL '1 day')" in where


def test_release_schema_and_import_error_audit_contracts_are_present():
    ddl = "\n".join(QABugSchemaManager._ddl_statements())
    assert "ix_bug_details_effect_date" in ddl
    assert "ix_bug_details_occurrence_date" in ddl
    assert "ix_qa_details_answer_requested_at" in ddl
    assert "ix_qabug_import_errors_run" in ddl
    repo_source = (ROOT / "app/qabug/repository.py").read_text(encoding="utf-8")
    importer_source = (ROOT / "app/qabug/importer.py").read_text(encoding="utf-8")
    assert "def record_import_error(" in repo_source
    assert 'error_code="UPSERT_FAILED"' in importer_source
    assert RAG_JSON_SCHEMA["$defs"]["chunk"]["properties"]["point_id"]["maximum"] == 0x7FFF_FFFF_FFFF_FFFF
