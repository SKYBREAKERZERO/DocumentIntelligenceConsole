"""Stage 1 gate: text provenance, engineering cues, conservative quality.

Contains generated *test strings* only. No fabricated 508/550 specification
files or private knowledge-base records are packaged or imported.
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import subprocess
import sys

import pytest

from app.engineering_intelligence.quality import (
    identify_repeated_requirement_ids,
    review_requirement_units,
    summarize_requirement_units,
)
from app.engineering_intelligence.requirements import (
    EXTRACTOR_VERSION,
    extract_requirement_units,
    normalize_text,
    validate_extracted_units,
)
from app.engineering_intelligence.persistence import _signals


def extract(text, **kwargs):
    return extract_requirement_units(
        document_id="unit-test-doc", content_id=42, section_id="4.1", text=text,
        **kwargs,
    )


def test_provenance_non_lossy_crlf_unicode_and_full_width():
    text = " \r\n  当识别完成时，必须加载 Profile。\r\n\r\n  必須、BLE を確認すること。  \n"
    items = extract(text)
    assert len(items) == 2
    validate_extracted_units(text, items)
    assert items[0].text.startswith("当")
    assert text[items[0].start_offset:items[0].end_offset] == items[0].text
    assert items[0].signals.modality == "MANDATORY"
    assert all(text[x.start_offset:x.end_offset] == x.text for x in items)


def test_stable_identity_repeat_and_no_collisions_on_source_position_shift():
    text = "shall use User ID.\nshall use User ID."
    items = extract(text)
    assert len(items) == 2
    assert items[0].unit_key != items[1].unit_key
    assert items == extract(text)
    changed = extract("Prefix.\n" + text)
    assert items[0].text_hash == changed[1].text_hash
    assert items[0].unit_key != changed[1].unit_key  # source span, NOT cross-version identity


def test_table_header_is_not_automatically_normative():
    a, b = extract("|条件|动作|例外|\n| NA | 必须保持配置 | ただし他条件 |")
    assert a.unit_type == "TABLE_ROW"
    assert a.signals.normative is False
    assert a.signals.structural_kind == "TABLE_ROW"
    assert b.unit_type == "TABLE_ROW"
    assert b.signals.normative
    assert b.signals.exception
    findings = review_requirement_units("|条件|动作|例外|\n| NA | 必须保持配置 | ただし他条件 |", (a,b))
    assert any(x.code == "TABLE_RULE_REVIEW" for x in findings)


@pytest.mark.parametrize("value,kind", [
    ("1) The device shall not load the profile.", "LIST_ITEM"),
    ("- Must use the selected User ID.", "LIST_ITEM"),
    ("（1） BLE は必須とする。", "LIST_ITEM"),
    ("4.2.1 Bluetooth Device Identification", "HEADING"),
    ("| UC-MMPID-005 | 5s |", "TABLE_ROW"),
    ("T1\tT2", "TABLE_ROW"),
])
def test_structure_is_just_a_hint_not_invented_units(value, kind):
    a = extract(value)[0]
    assert a.signals.structural_kind == kind
    assert a.text == value


def test_identifiers_do_not_absorb_terminal_punctuation():
    a = extract("REQ-0001: Refer to UC-MMPID-005.")[0]
    assert a.signals.identifiers == ("REQ-0001", "UC-MMPID-005")


def test_engineering_constraint_cues_exact_source_spans():
    text = "Decision Time=max(5s, completion); profile <=5kB; server <=100kB; ≥19ePF, <=15ePF."
    a = extract(text)[0]
    assert a.signals.timing == ("5s",)
    assert set(a.signals.quantity_cues) >= {"<=5kB", "<=100kB", "≥19ePF", "<=15ePF"}
    for cue, start, end, fragment in a.signals.cue_spans:
        assert a.text[start:end] == fragment
    json_flags = json.loads(_signals(a))
    assert json_flags["quantity_cues"]
    assert json_flags["cue_spans"]


@pytest.mark.parametrize("text,modality", [
    ("The ECU must not send a payload.", "PROHIBITED"),
    ("送信してはならない", "PROHIBITED"),
    ("装置を起動しなければならない", "MANDATORY"),
    ("The ECU should save a snapshot.", "RECOMMENDED"),
    ("Bluetooth functions list", "NONE"),
])
def test_modality_is_explicit_cue_not_approved_rule(text, modality):
    a = extract(text)[0]
    assert a.signals.modality == modality
    assert a.text == text


def test_invalid_source_span_and_evidence_cue_rejected():
    source = "Memory ID shall be selected within 5s."
    a = extract(source)[0]
    with pytest.raises(ValueError, match="source span"):
        validate_extracted_units("X"+source, (a,))
    bad_cues = replace(a.signals, cue_spans=(("TIMING", 0, 2, "5s"),))
    with pytest.raises(ValueError, match="Source cue"):
        validate_extracted_units(source, (replace(a, signals=bad_cues),))
    with pytest.raises(ValueError, match="source text hash"):
        validate_extracted_units(source, (replace(a, text_hash="0"*64),))


def test_no_source_drop_even_if_well_formed_units_modified():
    source = "A\nB\nC"
    a, b, c = extract(source)
    with pytest.raises(ValueError, match="omitted"):
        validate_extracted_units(source, (a, c))
    with pytest.raises(ValueError, match="overlapping"):
        validate_extracted_units(source, (a, a, c))


def test_excessive_source_is_not_silently_truncated():
    with pytest.raises(ValueError, match="characters"):
        extract("the unit shall be used", max_source_chars=4)
    with pytest.raises(ValueError, match="requirement units"):
        extract("shall do A\nshall do B", max_units=1)
    with pytest.raises(ValueError, match="resource limits"):
        extract("hi", max_units=0)


def test_large_source_budget_linear_and_non_lossy():
    source = "\n".join(f"{i}) BT profile shall remain <=5kB." for i in range(4000))
    rows = extract(source,max_units=4100)
    assert len(rows) == 4000
    validate_extracted_units(source, rows)
    assert rows[-1].end_offset == len(source)


def test_review_long_and_multiple_normatives_are_not_auto_rejected():
    source = "When HFP connects, the ECU must load and shall store the profile."
    rows = extract(source)
    findings = review_requirement_units(source, rows, long_unit_chars=12)
    assert {f.code for f in findings} == {"LONG_SOURCE_UNIT", "MULTIPLE_NORMATIVE_CUES"}
    assert summarize_requirement_units(rows, findings)["findings"] == 2


def test_repeated_identifier_is_review_candidate_not_identity_merge():
    rows = extract("REQ-0010: must load.\nREQ-0010 is referred to by test plan.")
    duplicates = identify_repeated_requirement_ids(rows)
    assert "REQ-0010" in duplicates
    assert len(duplicates["REQ-0010"]) == 2


def test_empty_line_source_cannot_create_a_requirement():
    assert extract("\n \t\r\n\r\n") == ()


def test_legacy_signal_json_fields_remain_compatible():
    a = extract("Spec shall require 5s response.")[0]
    flags = json.loads(_signals(a))
    for field in ("normative", "conditional", "exception", "timing", "reference_cue", "identifiers"):
        assert field in flags
    assert EXTRACTOR_VERSION == "requirement-units-v2"


def test_inspection_script_prints_only_metrics_by_default(tmp_path):
    source = "SECRET_SAMPLE: ECU shall respond within 5s."
    p = tmp_path / "source.txt"
    p.write_text(source, encoding="utf-8")
    from scripts.inspect_stage1_source import main
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert main(["--file",str(p),"--document-id","D","--content-id","1","--section","3.1"]) == 0
    report=json.loads(buf.getvalue())
    assert report["units"] == 1
    assert "SECRET_SAMPLE" not in buf.getvalue()
