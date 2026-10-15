"""Synthetic data only: offline filtered JSON safety, extraction and CLI."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.engineering_intelligence.filtered_json_diagnostics import (
    SCHEMA_FILTERED, SCHEMA_SOURCE, diagnostic_dict, diagnose_filtered_cases,
    read_limited_json,
)
from scripts.inspect_stage1_filtered_json import main


def _row(text="If connected, ECU shall notify.", *, cid=1, project="21MM", kind="DOCX_TEXT"):
    return {"project":project,"document_id":"source-example","content_id":cid,
            "section_id":"4.2","source_kind":kind,"text":text,
            "source_sha256":hashlib.sha256(text.encode("utf-8","surrogatepass")).hexdigest()}


def _data(*rows, schema=SCHEMA_FILTERED, transform="REPLACED", offset="CHANGED"):
    data = {"schema":schema, "cases":list(rows)}
    if schema == SCHEMA_FILTERED:
        data["manifest"]={"transformation":transform,"offset_policy":offset}
    return data


def test_filtered_returns_only_offsets_and_no_quality_claim():
    text = "如果设备连接，ECU应当通知。"
    source = _data(_row(text))
    report=diagnostic_dict(source)
    asjson=json.dumps(report,ensure_ascii=False)
    assert report["result"] == "UNLABELLED_DIAGNOSTIC_ONLY"
    assert not report["quality_metrics_available"]
    assert not report["source_verified"]
    assert not report["original_offsets_verified"]
    assert report["transformation_present"]
    assert report["case_diagnostics"][0]["finding_offsets"]
    assert text not in asjson
    assert "source-example" not in asjson
    assert report["requirement_candidates"] == 1
    assert "FILTERED_TEXT_OFFSETS_APPLY_ONLY_TO_SUPPLIED_TEXT" in report["warnings"]


def test_source_schema_still_not_claimed_authoritative_original():
    report=diagnose_filtered_cases(_data(_row(),schema=SCHEMA_SOURCE))
    assert not report.transformation_present
    assert not report.source_verified and not report.quality_metrics_available
    assert report.case_diagnostics[0].offset_policy=="UNKNOWN"


def test_same_identity_across_projects_allowed_and_reports_separated():
    report=diagnose_filtered_cases(_data(_row(project="21MM"),_row(project="24MM")))
    assert report.case_count_by_project == {"21MM":1,"24MM":1}
    assert report.case_diagnostics[0].identity_hash_prefix != report.case_diagnostics[1].identity_hash_prefix


def test_multilingual_exact_offsets_are_relative_to_filtered_text():
    source="BT接続の場合、ECUは通知すること。\nUnless disabled, system shall save."
    report=diagnose_filtered_cases(_data(_row(source)))
    assert report.case_diagnostics[0].source_units==2
    for item in report.case_diagnostics[0].finding_offsets:
        assert 0 <= item["start"] < item["end"] <=len(source)


def test_table_units_are_not_approved_relationships():
    report=diagnose_filtered_cases(_data(_row("| 条件 | 动作 |\n| --- | --- |\n| ON | ECU应当通知 |",kind="DOCX_TABLE")))
    assert report.table_units >=2
    assert report.review_required and not report.source_verified


@pytest.mark.parametrize("alter,match", [
    (lambda d: d.pop("schema"), "explicit"),
    (lambda d: d["manifest"].pop("offset_policy"), "offset_policy"),
    (lambda d: d["manifest"].update(transformation="NONE"), "transformation"),
    (lambda d: d["cases"][0].update(project="COMMON"), "project"),
    (lambda d: d["cases"][0].update(content_id=True), "content_id"),
    (lambda d: d["cases"][0].update(source_kind="GUESS"), "source_kind"),
    (lambda d: d["cases"][0].update(text="tampered"), "SHA-256"),
    (lambda d: d["cases"].append(dict(d["cases"][0])), "Duplicate"),
    (lambda d: d["cases"][0].update(complexity_tags=["NORMAL","CROSS_PAGE"]), "complexity_tags"),
])
def test_fails_closed_for_ambiguous_or_tampered_data(alter,match):
    data=_data(_row())
    alter(data)
    with pytest.raises(ValueError,match=match):
        diagnose_filtered_cases(data)


def test_bounded_cases_and_char_budget():
    data=_data(_row(),_row(cid=2))
    with pytest.raises(ValueError,match="cases"):
        diagnose_filtered_cases(data,max_cases=1)
    with pytest.raises(ValueError,match="character budget"):
        diagnose_filtered_cases(data,max_chars=3)


def test_bounded_finding_output_and_resource_limits():
    data=_data(_row("ECU shall notify.\nECU shall save.\nECU shall load."))
    r=diagnose_filtered_cases(data,max_findings_per_case=1)
    assert len(r.case_diagnostics[0].finding_offsets)==1
    assert r.case_diagnostics[0].findings_truncated>=2
    with pytest.raises(ValueError,match="bounded"):
        diagnose_filtered_cases(data,max_findings_per_case=1000)


def test_privacy_heuristics_are_counts_only():
    text="admin@example.com 192.168.1.1 Password: invalid C:\\private\\sample ECU shall notify."
    result=diagnostic_dict(_data(_row(text)))
    assert result["privacy_pattern_case_counts"]["EMAIL_PATTERN"]==1
    assert result["privacy_pattern_case_counts"]["IPV4_PATTERN"]==1
    assert result["privacy_pattern_case_counts"]["CREDENTIAL_LABEL"]==1
    encoded=json.dumps(result)
    for raw in ("admin@example.com","192.168.1.1","C:\\\\private", "Password:"):
        assert raw not in encoded


def test_cli_report_does_not_overwrite_and_keeps_text_private(tmp_path:Path,capsys):
    text="ECU shall notify. private@example.com"
    src=tmp_path/"private.json";out=tmp_path/"redacted_report.json"
    src.write_text(json.dumps(_data(_row(text))),encoding="utf-8")
    assert main(["--json",str(src),"--report-out",str(out)])==0
    stdout=capsys.readouterr().out
    assert text not in out.read_text(encoding="utf-8")
    assert text not in stdout and str(src) not in stdout
    assert json.loads(out.read_text(encoding="utf-8"))["quality_metrics_available"] is False
    assert main(["--json",str(src),"--report-out",str(out)])==2
    assert out.is_file()


def test_cli_bad_file_does_not_echo_private_paths(tmp_path:Path,capsys):
    src=tmp_path/"secret-original-name.json";out=tmp_path/"report.json"
    src.write_text('INVALID SECRET SOURCE',encoding="utf-8")
    assert main(["--json",str(src),"--report-out",str(out)])==2
    text=capsys.readouterr().err
    assert "secret-original-name" not in text and "SECRET SOURCE" not in text
    assert not out.exists()


def test_json_reader_refuses_broken_encoding_and_oversize(tmp_path:Path):
    src=tmp_path/"test.json";src.write_bytes(b'{"schema":"test"}')
    assert read_limited_json(src)["schema"]=="test"
    with pytest.raises(ValueError,match="oversized"):
        read_limited_json(src,max_input_bytes=5)


def test_standalone_chinese_mandatory_is_not_a_conditional_signal():
    from app.engineering_intelligence.requirements import extract_requirement_units
    from app.engineering_intelligence.atomic import extract_atomic_candidates
    from app.engineering_intelligence.condition_scope import extract_requirement_scope
    text="ECU应当通知。"
    units=extract_requirement_units(document_id="d",content_id=1,section_id="s",text=text)
    assert units[0].signals.normative
    assert not units[0].signals.conditional
    evidence=extract_requirement_scope(units[0],extract_atomic_candidates(units[0])[0])
    assert evidence.status=="ACTION_ONLY"
    assert evidence.condition is None
    conditional="当蓝牙连接成功时，ECU应当通知。"
    conditional_units=extract_requirement_units(document_id="d",content_id=1,section_id="s",text=conditional)
    assert conditional_units[0].signals.conditional
    assert any(cue[0]=="CONDITION" and cue[3]=="当" for cue in conditional_units[0].signals.cue_spans)


def test_json_reader_rejects_duplicate_keys_in_nested_objects(tmp_path:Path):
    f=tmp_path/"ambiguous.json"
    f.write_text('{"schema":"stage1-filtered-samples-v1","manifest":{"offset_policy":"CHANGED","offset_policy":"PRESERVED"}}',encoding="utf-8")
    with pytest.raises(ValueError,match="Duplicate JSON object key"):
        read_limited_json(f)
