"""v1.26 synthetic 4-layer source-trace integration and fail-closed tests."""
from __future__ import annotations

import copy
import json
from dataclasses import replace

import fitz
import pytest

from app.engineering_intelligence.native_layout import inspect_pdf_native
from app.engineering_intelligence.pipeline_json_review import review_pipeline_documents
from app.engineering_intelligence.requirements import extract_requirement_units
from app.engineering_intelligence.stage1_dcm_sql_trace import SqlTraceContent, trace_dcm_sql_sources
from app.engineering_intelligence.stage1_native_fidelity import reconcile_pdf_pipeline
from app.engineering_intelligence.stage1_native_sql_chain import crosscheck_native_sql
from scripts import inspect_stage1_native_sql_chain as gateway

PHRASE = "When DCM communication completes, the APP button shall appear."
OTHER = "After updating the APP, the button shall remain visible."
ID = "confidential-document-123"


def doc(*values, pages=None, sections=None):
    pages = pages or tuple(range(1, len(values) + 1))
    sections = sections or ("4.3",) * len(values)
    return {
        "document": {"document_id": ID, "project_code": "21MM", "file_name": "sensitive.pdf"},
        "metadata": {"source_format": "pdf", "specification_code": "888", "specification_version": "v1.02"},
        "sections": [{"id": "4.3", "title_jp": "secret heading"}],
        "contents": [{"text": v, "section_id": s, "page_number": page}
                     for v, page, s in zip(values, pages, sections)],
    }


def pdf(tmp_path, pages=((PHRASE,), (OTHER,))):
    path = tmp_path / "sensitive_original.pdf"
    with fitz.open() as d:
        for values in pages:
            page = d.new_page(width=600, height=800)
            for pos, value in enumerate(values):
                page.insert_text((50, 100 + 30 * pos), value, fontsize=10)
        d.save(path)
    return path


def reports(tmp_path, *, d=None, path=None, rows=None, units=None, max_contents=2000):
    d = d or doc(PHRASE)
    path = path or pdf(tmp_path)
    rev = review_pipeline_documents([d])
    native = reconcile_pdf_pipeline([d], rev, inspect_pdf_native(path),
                                    max_contents=max_contents, max_entries=max_contents)
    rows = rows if rows is not None else [SqlTraceContent(41, ID, "4.3", 1, PHRASE)]
    units = units if units is not None else extract_requirement_units(
        document_id=ID, content_id=41, section_id="4.3", page_number=1, text=PHRASE)
    sql = trace_dcm_sql_sources(rev, [d], project="21MM", document_id=ID,
                                version_id=12, sql_contents=rows, sql_units=units)
    return native, sql


def test_one_source_four_layers_only_literal_candidate(tmp_path):
    n, s = reports(tmp_path)
    c = crosscheck_native_sql(n, s)
    assert c["source_review_queue_complete"]
    assert c["literal_candidates"] == 1
    assert c["blocked_count"] == 0
    assert c["entries"][0]["sql_content_id"] == 41
    assert c["entries"][0]["requirement_unit_key"] is not None
    assert c["entries"][0]["native_locator"]["page"] == 1
    assert not c["human_gold_acceptance_complete"]
    assert not c["live_sql_transaction_verified_by_cli"]
    assert not c["semantic_relationships_approved"]
    serialized = json.dumps(c, ensure_ascii=False)
    for private in (PHRASE, OTHER, ID, "sensitive_original", "secret heading"):
        assert private not in serialized


def test_native_page_drift_blocks_sql_ids(tmp_path):
    n, s = reports(tmp_path, d=doc(PHRASE, pages=(2,)))
    c = crosscheck_native_sql(n, s)
    assert c["literal_candidates"] == 0
    assert c["entries"][0]["native_alignment_status"] == "EXACT_NATIVE_TEXT_ON_DIFFERENT_PAGE"
    assert c["entries"][0]["sql_content_id"] is None
    assert c["entries"][0]["requirement_unit_key"] is None


def test_duplicate_native_occurrences_block_sql_binding(tmp_path):
    n, s = reports(tmp_path, path=pdf(tmp_path, pages=((PHRASE,), (PHRASE,))))
    c = crosscheck_native_sql(n, s)
    assert c["entries"][0]["native_alignment_status"] == "AMBIGUOUS_REPEATED_NATIVE_TEXT_REVIEW"
    assert c["blocked_count"] == 1


def test_missing_exported_section_blocks_chain_even_when_pdf_and_sql_match(tmp_path):
    n, s = reports(tmp_path, d=doc(PHRASE, sections=("4.99",)),
                   rows=[SqlTraceContent(41, ID, "4.99", 1, PHRASE)],
                   units=extract_requirement_units(document_id=ID, content_id=41,
                           section_id="4.99", page_number=1, text=PHRASE))
    c = crosscheck_native_sql(n, s)
    assert c["entries"][0]["chain_status"] == "EXPORTED_SECTION_NOT_RESOLVED"


def test_duplicate_sql_rows_or_wrong_unit_span_stays_blocked(tmp_path):
    n, s = reports(tmp_path, rows=[SqlTraceContent(41, ID, "4.3", 1, PHRASE),
                                    SqlTraceContent(42, ID, "4.3", 1, PHRASE)],
                   units=[])
    assert crosscheck_native_sql(n, s)["entries"][0]["chain_status"] == "SQL_CONTENT_NOT_EXACT_UNIQUE"
    n, s = reports(tmp_path, units=[])
    assert crosscheck_native_sql(n, s)["entries"][0]["chain_status"] == "SQL_REQUIREMENT_SPAN_NOT_EXACT"


def test_outside_pdf_budget_marks_chain_incomplete(tmp_path):
    d = doc(PHRASE, OTHER, pages=(1, 2))
    n, s = reports(tmp_path, d=d, max_contents=1)
    c = crosscheck_native_sql(n, s)
    assert c["literal_candidates"] == 1
    assert not c["source_review_queue_complete"]
    assert not c["all_four_layer_literal_candidates"]


def test_scanned_pdf_does_not_verify_text_layer(tmp_path):
    n, s = reports(tmp_path, path=pdf(tmp_path, pages=((), ())))
    c = crosscheck_native_sql(n, s)
    assert c["native_pdf_has_no_text_layer"]
    assert c["blocked_count"] == 1


def test_crosswalk_detects_mixed_documents_and_duplicate_keys(tmp_path):
    n, s = reports(tmp_path)
    forged = copy.deepcopy(n)
    forged["document_hash"] = "a" * 20
    with pytest.raises(ValueError, match="identities"):
        crosscheck_native_sql(forged, s)
    forged = copy.deepcopy(s)
    forged["entries"].append(copy.deepcopy(forged["entries"][0]))
    with pytest.raises(ValueError, match="duplicate"):
        crosscheck_native_sql(n, forged)


def test_report_truncation_and_invalid_budget_fail_closed(tmp_path):
    n, s = reports(tmp_path)
    with pytest.raises(ValueError, match="budget"):
        crosscheck_native_sql(n, s, max_evidence=0)
    with pytest.raises(ValueError, match="budget"):
        crosscheck_native_sql(n, s, max_evidence=0)
    tampered = copy.deepcopy(n)
    tampered["entries"] = []
    with pytest.raises(ValueError, match="count mismatch"):
        crosscheck_native_sql(tampered, s)


def test_gateway_refuses_unmatched_document_before_db_access(tmp_path):
    d = doc(PHRASE)
    class FailCursor:
        def execute(self, *args):
            pytest.fail("No SQL query should be made on wrong document")
    with pytest.raises(ValueError, match="document"):
        gateway.audit_live_source_chain(FailCursor(), documents=[d],
            review=review_pipeline_documents([d]), pdf=pdf(tmp_path),
            project="21MM", document_id="wrong", version_id=11)


def test_gateway_native_document_hash_passed_to_existing_sql_version_gate(tmp_path, monkeypatch):
    d = doc(PHRASE)
    path = pdf(tmp_path)
    rev = review_pipeline_documents([d])
    expected_sha = inspect_pdf_native(path).file_sha256
    called = []
    def fake_sql(cur, **kwargs):
        assert kwargs["file_sha256"] == expected_sha
        called.append(kwargs["version_id"])
        return trace_dcm_sql_sources(rev, [d], project="21MM", document_id=ID,
          version_id=12, sql_contents=[SqlTraceContent(41, ID, "4.3", 1, PHRASE)],
          sql_units=extract_requirement_units(document_id=ID, content_id=41,
             section_id="4.3", page_number=1, text=PHRASE))
    monkeypatch.setattr(gateway, "read_only_dcm_trace", fake_sql)
    c = gateway.audit_live_source_chain(object(), documents=[d], review=rev,
          pdf=path, project="21MM", document_id=ID, version_id=12)
    assert called == [12]
    assert c["literal_candidates"] == 1
    assert not c["live_sql_transaction_verified_by_cli"]


def test_gateway_refuses_modified_source_and_cli_input_overwrite(tmp_path, capsys):
    d = doc(PHRASE)
    path = pdf(tmp_path)
    input_json = tmp_path / "doc.json"
    review_file = tmp_path / "review.json"
    input_json.write_text(json.dumps(d), encoding="utf-8")
    review_file.write_text(json.dumps(review_pipeline_documents([d])), encoding="utf-8")
    argv = ["--pdf", str(path), "--json", str(input_json), "--review", str(review_file),
            "--project", "21MM", "--database", "rag_21mm", "--document-id", ID,
            "--version-id", "12", "--user", "ro", "--report-out", str(input_json)]
    assert gateway.main(argv) == 2
    assert "NOT_VERIFIED" in capsys.readouterr().err


def test_gateway_rejects_symlinks(tmp_path):
    file = pdf(tmp_path)
    symlink = tmp_path / "native_link.pdf"
    try:
        symlink.symlink_to(file)
    except (OSError, NotImplementedError):
        pytest.skip("symlink not supported")
    with pytest.raises(ValueError, match="scope"):
        gateway.audit_live_source_chain(object(), documents=[doc(PHRASE)],
            review=review_pipeline_documents([doc(PHRASE)]), pdf=symlink,
            project="21MM", document_id=ID, version_id=12)


def test_result_does_not_mutate_pipeline_or_prebuilt_reports(tmp_path):
    n, s = reports(tmp_path)
    before_n, before_s = copy.deepcopy(n), copy.deepcopy(s)
    crosscheck_native_sql(n, s)
    assert n == before_n and s == before_s


def test_full_cli_fake_db_readonly_transaction_and_report(tmp_path, monkeypatch, capsys):
    """Exercises CLI wiring with an instrumented fake; NOT a live PG test."""
    import sys
    from types import SimpleNamespace
    from scripts import inspect_stage1_dcm_sql_trace as sql_gateway

    d = doc(PHRASE)
    path = pdf(tmp_path)
    raw_json = tmp_path / "source.json"
    raw_review = tmp_path / "review.json"
    out = tmp_path / "result.json"
    raw_json.write_text(json.dumps(d), encoding="utf-8")
    raw_review.write_text(json.dumps(review_pipeline_documents([d])), encoding="utf-8")
    sha = inspect_pdf_native(path).file_sha256
    checked = []
    monkeypatch.setattr(sql_gateway, "require_complete_backfill", lambda cur, version: checked.append("receipt"))
    monkeypatch.setattr(sql_gateway, "verify_live_source_snapshot", lambda cur, **kw: checked.append("digest"))
    monkeypatch.setattr(sql_gateway, "load_version_units", lambda cur, vid, **kw:
                        extract_requirement_units(document_id=ID, content_id=41, section_id="4.3",
                                                  page_number=1, text=PHRASE))

    class Cursor:
        index = 0
        statements = []
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def execute(self, stmt, params=None):
            assert stmt.strip().upper().startswith(("SELECT", "SET TRANSACTION"))
            if stmt.strip().upper().startswith("SELECT"):
                self.index += 1
            self.statements.append(stmt)
        def fetchone(self):
            return {1: ("21MM",), 2: (ID, sha), 3: (1, len(PHRASE))}[self.index]
        def fetchall(self):
            assert self.index == 4
            return [(41, "4.3", 1, PHRASE)]

    class Connection:
        def __init__(self): self.cur = Cursor()
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def cursor(self): return self.cur
    conn = Connection()
    def fake_connect(**kw):
        assert kw["dbname"] == "rag_21mm" and kw["user"] == "ro"
        assert kw["application_name"] == "dic-stage1-native-sql-readonly"
        return conn
    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=fake_connect))
    monkeypatch.setenv("TEST_DIC_SQL_PASSWORD", "dummy_password")
    args = ["--pdf", str(path), "--json", str(raw_json), "--review", str(raw_review),
            "--project", "21MM", "--database", "rag_21mm", "--document-id", ID,
            "--version-id", "12", "--user", "ro", "--password-env", "TEST_DIC_SQL_PASSWORD",
            "--report-out", str(out)]
    assert gateway.main(args) == 0
    assert checked == ["receipt", "digest"]
    assert conn.cur.statements[0] == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
    assert all(stmt.lstrip().upper().startswith(("SELECT", "SET TRANSACTION")) for stmt in conn.cur.statements)
    body = json.loads(out.read_text(encoding="utf-8"))
    assert body["literal_candidates"] == 1
    assert body["live_sql_transaction_verified_by_cli"]
    assert body["native_pdf_ownership_hash_checked_by_cli"]
    assert body["human_gold_acceptance_complete"] is False
    assert "dummy_password" not in out.read_text(encoding="utf-8")
    assert PHRASE not in out.read_text(encoding="utf-8")
    assert gateway.main(args) == 2  # exclusive report creation
    assert "NOT_VERIFIED" in capsys.readouterr().err


def test_live_cli_rejects_sql_version_hash_drift_without_writing(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace
    from scripts import inspect_stage1_dcm_sql_trace as sql_gateway

    d = doc(PHRASE)
    path = pdf(tmp_path)
    source = tmp_path / "source.json"
    review = tmp_path / "review.json"
    output = tmp_path / "evidence.json"
    source.write_text(json.dumps(d), encoding="utf-8")
    review.write_text(json.dumps(review_pipeline_documents([d])), encoding="utf-8")

    class Cursor:
        count = 0
        def __enter__(self): return self
        def __exit__(self, *exc): return False
        def execute(self, statement, params=None):
            if self.count >= 2:
                pytest.fail("SQL content read before authoritative PDF hash gate")
            self.count += 1
        def fetchone(self):
            return ("21MM",) if self.count == 1 else (ID, "0" * 64)
    class Connection:
        cur = Cursor()
        def __enter__(self): return self
        def __exit__(self, *exc): return False
        def cursor(self): return self.cur
    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=lambda **kw: Connection()))
    monkeypatch.setenv("TEST_DIC_SQL_PASSWORD", "dummy")
    argv = ["--pdf", str(path), "--json", str(source), "--review", str(review),
            "--project", "21MM", "--database", "rag_21mm", "--document-id", ID,
            "--version-id", "12", "--user", "ro", "--password-env", "TEST_DIC_SQL_PASSWORD",
            "--report-out", str(output)]
    assert gateway.main(argv) == 2
    assert not output.exists()
