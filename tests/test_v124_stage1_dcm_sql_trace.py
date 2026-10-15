"""v1.24 DCM SQL-boundary synthetic tests; no real database claimed."""
from __future__ import annotations

import copy
import json
from dataclasses import replace

import pytest

from app.engineering_intelligence.pipeline_json_review import review_pipeline_documents
from app.engineering_intelligence.requirements import extract_requirement_units
from app.engineering_intelligence.stage1_dcm_sql_trace import (
    SqlTraceContent, trace_dcm_sql_sources,
)
from scripts.inspect_stage1_dcm_sql_trace import native_hash

DOC_ID = "nonpublic-dcm-source"
AFTER = "DCM通信後、APP釦を表示すること。"
INITIAL = "Telematics釦の初期値はなし。"


def document(*lines, section_ids=None, pages=None, role="PRIMARY_CANDIDATE"):
    ids = section_ids or ("4.3",) * len(lines)
    pages = pages or tuple(range(7, 7 + len(lines)))
    return {
        "document": {"document_id": DOC_ID, "project_code": "21MM",
                     "file_name": "private-name-dcm.pdf"},
        "metadata": {"source_format": "pdf", "document_role": role,
                     "specification_code": "888", "specification_version": "v1.02"},
        "sections": [{"id": "4.3", "title_jp": "DCM状態"}],
        "contents": [{"text": line, "section_id": section, "page_number": page,
                      "metadata": {"native_offsets": True}}
                     for line, section, page in zip(lines, ids, pages)],
    }


def sql(text=AFTER, *, cid=41, section="4.3", page=7):
    return SqlTraceContent(cid, DOC_ID, section, page, text)


def units(text=AFTER, *, cid=41, section="4.3", page=7):
    return extract_requirement_units(document_id=DOC_ID, content_id=cid,
                                     section_id=section, page_number=page, text=text)


def trace(doc=None, *, rows=None, sql_units=None, report=None, **kwargs):
    doc = doc or document(AFTER)
    report = report or review_pipeline_documents([doc])
    return trace_dcm_sql_sources(report, [doc], project="21MM",
                                 document_id=DOC_ID, version_id=12,
                                 sql_contents=[sql()] if rows is None else rows,
                                 sql_units=units() if sql_units is None else sql_units,
                                 **kwargs)


def test_exact_source_unit_and_atomic_scope_are_review_candidates_only():
    result = trace()
    assert result["dcm_evidence_complete"]
    assert result["sql_content_status_counts"] == {"EXACT_SQL_CONTENT_ONLY": 1}
    point, = result["entries"]
    assert point["unit_status"] == "EXACT_SQL_UNIT_REVIEW_ONLY"
    assert point["sql_content_id"] == 41
    assert point["requirement_unit_key"] == units()[0].unit_key
    assert point["atomic_candidate_count"] == 1
    assert point["scope_status_counts"] == {"ACTION_ONLY": 1}
    assert not point["condition_action_scope_verified"]
    assert not point["state_transition_verified"]
    assert not result["sql_rows_authenticated_by_caller"]
    assert not result["semantic_relations_approved"]
    text = json.dumps(result, ensure_ascii=False)
    for private in (AFTER, DOC_ID, "private-name", "APP釦"):
        assert private not in text


def test_same_section_telematics_initial_is_separate_sql_source_not_a_transition():
    doc = document(INITIAL, AFTER)
    result = trace(doc, rows=[sql(INITIAL, cid=101, page=7), sql(AFTER, cid=102, page=8)],
                   sql_units=[*units(INITIAL, cid=101, page=7), *units(AFTER, cid=102, page=8)])
    assert result["dcm_evidence_count"] == 2
    assert {r["sql_content_id"] for r in result["entries"]} == {101, 102}
    assert not any(r["state_transition_verified"] for r in result["entries"])


def test_page_mismatch_never_binds_sql_id():
    p, = trace(rows=[sql(page=8)], sql_units=[])["entries"]
    assert p["source_status"] == "PAGE_MISMATCH"
    assert p["sql_content_id"] is None


def test_section_mismatch_never_binds_sql_id():
    p, = trace(rows=[sql(section="4.4")], sql_units=[])["entries"]
    assert p["source_status"] == "SECTION_MISMATCH"


def test_missing_sql_source_no_fuzzy_match():
    p, = trace(rows=[sql(AFTER.replace("表示", "更新"))], sql_units=[])["entries"]
    assert p["source_status"] == "SQL_CONTENT_NOT_FOUND"


def test_two_equivalent_sql_rows_same_page_ambiguous():
    p, = trace(rows=[sql(), sql(cid=42)], sql_units=[])["entries"]
    assert p["source_status"] == "AMBIGUOUS_SQL_CONTENT"


def test_repeated_export_content_same_page_is_ambiguous():
    doc = document(AFTER, AFTER, pages=(7, 7))
    report = trace(doc, rows=[sql()], sql_units=[]) 
    assert report["dcm_evidence_count"] == 2
    assert all(p["source_status"] == "AMBIGUOUS_PIPELINE_SOURCE" for p in report["entries"])


def test_bounded_report_refuses_to_claim_total_dcm_coverage():
    doc = document(AFTER, "DCM通信前に表示しないこと。")
    report = review_pipeline_documents([doc], max_evidence=1)
    # The trace is replay-gated; truncated custom reports cannot bypass this.
    with pytest.raises(ValueError, match="drift"):
        trace(doc, report=report)


def test_native_source_drift_report_refused_even_if_pointer_hash_was_preserved():
    doc = document(AFTER)
    report = review_pipeline_documents([doc])
    doc["contents"][0]["text"] = AFTER.replace("表示", "更新")
    with pytest.raises(ValueError, match="drift"):
        trace(doc, report=report)


def test_foreign_content_row_fails_closed():
    with pytest.raises(ValueError, match="foreign"):
        trace(rows=[replace(sql(), document_id="unrelated")])


def test_duplicate_sql_ids_fail_closed():
    with pytest.raises(ValueError, match="duplicate"):
        trace(rows=[sql(), sql(AFTER, cid=41)])


def test_foreign_requirement_unit_fails_closed():
    with pytest.raises(ValueError, match="span"):
        trace(sql_units=[replace(units()[0], document_id="other")])


def test_requirement_text_hash_drift_fails_closed():
    with pytest.raises(ValueError, match="span"):
        trace(sql_units=[replace(units()[0], text_hash="0" * 64)])


def test_unit_offset_disagrees_with_pointer_without_spurious_unit_binding():
    text = "  " + AFTER
    doc = document(text)
    own_units = units(text)
    report = trace(doc, rows=[sql(text)], sql_units=own_units)
    point = report["entries"][0]
    assert point["source_status"] == "EXACT_SQL_CONTENT_ONLY"
    assert point["unit_status"] == "SQL_UNIT_OFFSETS_NOT_EXACT"
    assert point["requirement_unit_key"] is None


def test_sql_version_scoping_errors_are_rejected():
    doc = document(AFTER)
    report = review_pipeline_documents([doc])
    with pytest.raises(ValueError, match="scope"):
        trace_dcm_sql_sources(report, [doc], project="COMMON", document_id=DOC_ID,
                              version_id=1, sql_contents=[])
    doc["metadata"]["document_role"] = "SUPPORTING"
    with pytest.raises(ValueError, match="primary"):
        trace(doc, sql_units=[], rows=[])


def test_native_file_hash_rejects_extension_and_symlinks(tmp_path):
    file = tmp_path / "sample.pdf"
    file.write_bytes(b"tiny synthetic PDF placeholder")
    import hashlib
    assert native_hash(file) == hashlib.sha256(file.read_bytes()).hexdigest()
    forbidden = tmp_path / "sample.txt"
    forbidden.write_text("wrong file", encoding="utf-8")
    with pytest.raises(ValueError, match="native"):
        native_hash(forbidden)
    symlink = tmp_path / "link.pdf"
    try:
        symlink.symlink_to(file)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink not supported")
    with pytest.raises(ValueError, match="native"):
        native_hash(symlink)


def test_source_units_more_than_budget_refused():
    with pytest.raises(ValueError, match="budget"):
        trace(max_units=0)


def test_input_objects_remain_immutable():
    doc = document(AFTER)
    snapshot = copy.deepcopy(doc)
    rows = [sql()]
    result = trace(doc, rows=rows)
    assert doc == snapshot
    assert rows == [sql()]
    assert result["dcm_evidence_count"] == 1


def test_read_only_sql_gateway_rejects_native_binary_hash_before_fetch(monkeypatch):
    from scripts.inspect_stage1_dcm_sql_trace import read_only_dcm_trace

    class GuardCursor:
        statements = []

        def execute(self, statement, *args):
            self.statements.append(statement)
            if "public.contents" in statement or "ei_" in statement:
                pytest.fail("Source content accessed without verifying DB native binary SHA")

        def fetchone(self):
            if len(self.statements) == 1:
                return ("21MM",)
            return (DOC_ID, "c" * 64)

    cursor = GuardCursor()
    doc = document(AFTER)
    with pytest.raises(ValueError, match="hash mismatch"):
        read_only_dcm_trace(cursor, document_id=DOC_ID, project="21MM", version_id=11,
                            file_sha256="a" * 64,
                            report=review_pipeline_documents([doc]), documents=[doc])
    assert len(cursor.statements) == 2
    assert all(stmt.lstrip().upper().startswith("SELECT") for stmt in cursor.statements)


def test_read_only_gateway_calls_backfill_and_digest_gates(monkeypatch):
    from scripts import inspect_stage1_dcm_sql_trace as gateway

    checked = []
    monkeypatch.setattr(gateway, "require_complete_backfill",
                        lambda cur, vid: checked.append(("backfill", vid)))
    monkeypatch.setattr(gateway, "verify_live_source_snapshot",
                        lambda cur, **kw: checked.append(("digest", kw["version_id"])))
    monkeypatch.setattr(gateway, "load_version_units",
                        lambda cur, vid, **kw: units())

    class ReadCursor:
        def __init__(self):
            self.index = 0
            self.queries = []
        def execute(self, statement, params=None):
            assert statement.strip().upper().startswith("SELECT")
            self.queries.append((statement, params))
            self.index += 1
        def fetchone(self):
            return {1: ("21MM",), 2: (DOC_ID, "a" * 64), 3: (1, len(AFTER))}[self.index]
        def fetchall(self):
            assert self.index == 4
            return [(41, "4.3", 7, AFTER)]

    cursor = ReadCursor()
    doc = document(AFTER)
    output = gateway.read_only_dcm_trace(
        cursor, document_id=DOC_ID, project="21MM", version_id=11,
        file_sha256="a" * 64, report=review_pipeline_documents([doc]), documents=[doc])
    assert checked == [("backfill", 11), ("digest", 11)]
    assert output["sql_rows_authenticated_by_caller"]
    assert output["native_binary_matches_version_hash"]
    assert output["source_backfill_receipt_rechecked"]
    assert not output["native_pdf_layout_verified"]
    assert not output["semantic_relations_approved"]
    assert len(cursor.queries) == 4
    assert all(q.lstrip().upper().startswith("SELECT") for q, _ in cursor.queries)


def test_sql_unit_uses_normalized_source_sha_not_raw_sha():
    text = "DCM通信後、APP釦を表示すること。  "
    doc = document(text)
    # Stored unit spans strip trailing whitespace and hash normalized text.
    result = trace(doc, rows=[sql(text)], sql_units=units(text))
    assert result["entries"][0]["unit_status"] == "SQL_UNIT_OFFSETS_NOT_EXACT"
