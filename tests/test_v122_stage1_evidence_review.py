"""Stage 1 v1.22 synthetic and private-text-free regression suite."""
from __future__ import annotations

import copy
import json

import pytest

from app.engineering_intelligence.pipeline_json_review import review_pipeline_documents
from app.engineering_intelligence.stage1_review_evidence import (
    make_pointer, safe_page, safe_section_id, verify_review_pointers,
)
from scripts.review_stage1_pipeline_json import main as generate_review
from scripts.verify_stage1_pipeline_evidence import main as verify_review


def _doc(text: str = "When BT connects, HU shall load.") -> dict:
    return {
        "document": {"document_id": "PRIVATE-CUSTOMER-ID", "project_code": "21MM",
                     "file_name": "PRIVATE-vehicle-document.pdf"},
        "metadata": {"source_format": "pdf", "specification_code": "550",
                     "specification_version": "v2.52"},
        "sections": [{"id": "2.9", "title_en": "Now Playing", "page_number": 4}],
        "contents": [{"section_id": "2.9", "page_number": 4, "text": text}],
    }


def test_normative_evidence_has_reproducible_unicode_source_offsets():
    doc = _doc("BT接続時、HUは通知すること。\nWhen BT connects, HU shall load.")
    unchanged = copy.deepcopy(doc)
    review = review_pipeline_documents([doc])
    assert doc == unchanged
    points = [p for p in review["evidence_queue"] if p["category"] == "NORMATIVE_SOURCE"]
    assert len(points) == 2
    assert [p["start_offset"] for p in points] == [0, len("BT接続時、HUは通知すること。\n")]
    assert [p["status"] for p in points] == ["LANGUAGE_JA_UNVERIFIED", "LANGUAGE_EN_UNVERIFIED"]
    assert all(p["native_source_verified"] is False for p in points)
    assert all(p["requirement_relationship_approved"] is False for p in points)
    assert verify_review_pointers(review, [doc])["verified_pointer_count"] >= 2
    output = json.dumps(review, ensure_ascii=False)
    for forbidden in ("PRIVATE-CUSTOMER-ID", "PRIVATE-vehicle-document", "shall load", "BT接続時"):
        assert forbidden not in output


def test_conditional_ambiguity_produces_separate_exact_source_pointer():
    doc = _doc("HU shall load unless NA.")
    review = review_pipeline_documents([doc])
    pairs = [(p["category"], p["status"]) for p in review["evidence_queue"]]
    assert ("NORMATIVE_SOURCE", "LANGUAGE_EN_UNVERIFIED") in pairs
    assert ("SCOPE_SOURCE", "UNRESOLVED") in pairs
    assert review["documents"][0]["scope_unresolved_sampled"] == 1
    assert verify_review_pointers(review, [doc])["source_native_file_verified"] is False


def test_export_reference_cell_has_exact_pointer_without_title_content():
    doc = _doc("2.9 Now Playing 参照\nnon-normative source")
    report = review_pipeline_documents([doc])
    point, = [p for p in report["evidence_queue"] if p["category"] == "SECTION_REFERENCE"]
    assert point["start_offset"] == 0
    assert point["end_offset"] == len("2.9 Now Playing 参照")
    assert point["section_id"] == "2.9"
    assert point["decision"] == "UNREVIEWED"
    assert verify_review_pointers(report, [doc])["verified_pointer_count"] >= 1


def test_leaked_section_and_source_format_are_never_echoed():
    doc = _doc()
    secret = "HR/employee@example.net/Confidential project title"
    doc["sections"] = [{"id": secret, "title_en": "Now Playing", "page_number": 4},
                       {"id": secret, "title_en": "Now Playing", "page_number": 4}]
    doc["contents"][0]["section_id"] = secret
    doc["metadata"]["source_format"] = "private-company-confidential-format"
    doc["contents"][0]["page_number"] = 10**200
    report = review_pipeline_documents([doc])
    encoded = json.dumps(report)
    assert secret not in encoded
    assert "private-company-confidential-format" not in encoded
    assert report["documents"][0]["source_format"] == "unknown"
    assert any(x["code"] == "DUPLICATE_SECTION_IDENTIFIER_REQUIRES_REVIEW" for x in report["findings"])
    assert all(p["page"] is None for p in report["evidence_queue"])
    assert all(p["section_id"].startswith("opaque:") for p in report["evidence_queue"])
    assert all(x["section_id"] is None or x["section_id"].startswith("opaque:") for x in report["findings"])


def test_pointer_fails_closed_after_source_or_offsets_change():
    doc = _doc("When BT connects, HU shall load.")
    report = review_pipeline_documents([doc])
    changed = copy.deepcopy(doc)
    changed["contents"][0]["text"] = "When BT connects, HU shall stop."
    with pytest.raises(ValueError, match="drift"):
        verify_review_pointers(report, [changed])
    changed_report = copy.deepcopy(report)
    changed_report["evidence_queue"][0]["start_offset"] = 1
    with pytest.raises(ValueError, match="drift"):
        verify_review_pointers(changed_report, [doc])
    changed_report = copy.deepcopy(report)
    changed_report["evidence_queue"][0]["decision"] = "APPROVED"
    with pytest.raises(ValueError, match="drift"):
        verify_review_pointers(changed_report, [doc])


def test_bounded_evidence_queue_does_not_silently_claim_full_coverage():
    doc = _doc("HU shall load.\nHU must notify.\nHU should save.\nHU shall disconnect.")
    report = review_pipeline_documents([doc], max_evidence=1)
    assert report["evidence_queue_count"] == 1
    assert report["evidence_queue_truncated"] >= 3
    assert verify_review_pointers(report, [doc])["verified_pointer_count"] == 1
    assert not report["quality_metrics_available"]


def test_section_budget_and_title_budget_are_enforced():
    doc = _doc()
    doc["sections"] *= 2
    with pytest.raises(ValueError, match="Section budget"):
        review_pipeline_documents([doc], max_sections=1)
    doc["sections"][0]["title_en"] = "T" * 1000
    with pytest.raises(ValueError, match="Invalid section record"):
        review_pipeline_documents([doc])
    with pytest.raises(ValueError, match="Positive integer"):
        review_pipeline_documents([doc], max_evidence=0)


def test_numeric_ids_and_non_numeric_identifiers_are_redacted():
    assert safe_section_id("2.9.3") == "2.9.3"
    assert safe_section_id("1234567890") != "1234567890"
    assert safe_section_id("private") != "private"
    assert safe_page(True) is None
    assert safe_page(-1) is None
    assert safe_page(4) == 4
    with pytest.raises(ValueError, match="Invalid source pointer"):
        make_pointer("a" * 20, {"text": "abc"}, 1, True, 2, category="SCOPE_SOURCE", status="UNRESOLVED")


def test_offline_cli_review_then_verify_and_detect_stale_report(tmp_path, capsys):
    source = tmp_path / "private-source.json"
    report_path = tmp_path / "review.json"
    doc = _doc("HU shall load unless NA.")
    source.write_text(json.dumps(doc), encoding="utf-8")
    assert generate_review(["--json", str(source), "--report-out", str(report_path)]) == 0
    capsys.readouterr()
    assert verify_review(["--json", str(source), "--report", str(report_path)]) == 0
    stdout = capsys.readouterr().out
    assert "PIPELINE_JSON_EVIDENCE_RECHECKED" in stdout
    assert "shall load" not in stdout
    doc["contents"][0]["text"] = "HU shall stop unless NA."
    source.write_text(json.dumps(doc), encoding="utf-8")
    assert verify_review(["--json", str(source), "--report", str(report_path)]) == 2
    captured = capsys.readouterr()
    assert "NOT_VERIFIED" in captured.err
    assert "shall stop" not in captured.err
    assert "private-source.json" not in captured.err


def test_file_name_budget_prevents_oversized_private_metadata():
    doc = _doc()
    doc["document"]["file_name"] = "P" * 10000
    with pytest.raises(ValueError, match="document name budget"):
        review_pipeline_documents([doc])


def test_crosspage_table_review_has_two_distinct_source_pointers():
    doc = _doc("Section | State | Action\nNA | ON | Save")
    doc["contents"] = [
        {"section_id": "2.9", "page_number": 7,
         "text": "Section | State | Action\nNA | ON | Save"},
        {"section_id": "2.9", "page_number": 8,
         "text": "Section | State | Action\nEU | OFF | Skip"},
    ]
    report = review_pipeline_documents([doc])
    points = [p for p in report["evidence_queue"] if p["category"] == "PDF_TABLE_FRAGMENT"]
    assert len(points) == 2
    assert [p["page"] for p in points] == [7, 8]
    assert [p["content_index"] for p in points] == [1, 2]
    assert all(p["status"] == "CROSSPAGE_UNVERIFIED" for p in points)
    assert report["original_native_layout_verified"] is False
    assert verify_review_pointers(report, [doc])["verified_pointer_count"] >= 2
    redacted = json.dumps(report)
    assert "Section | State | Action" not in redacted


def test_crosspage_pair_budget_never_exports_an_orphan():
    doc = _doc()
    doc["contents"] = [
        {"section_id": "2.9", "page_number": page, "text": "A | B | C\nD | E | F"}
        for page in (3, 4)
    ]
    report = review_pipeline_documents([doc], max_evidence=1)
    assert not any(p["category"] == "PDF_TABLE_FRAGMENT" for p in report["evidence_queue"])
    assert report["evidence_queue_truncated"] >= 2


def test_reference_evidence_handles_japanese_suffix_without_whitespace():
    doc = _doc("2.9 Now Playingを参照\nUnrelated content.")
    report = review_pipeline_documents([doc])
    assert "SECTION_NODE_MAY_BE_CROSS_REFERENCE_CELL" in report["documents"][0]["issue_counts"]
    assert any(p["category"] == "SECTION_REFERENCE" for p in report["evidence_queue"])


def test_large_number_of_sections_uses_bounded_linear_reference_scan():
    doc = _doc("3.2.9 Last title 参照")
    doc["sections"] = [
        {"id": f"3.2.{n}", "title_en": f"Title {n}", "page_number": 5}
        for n in range(300)
    ]
    doc["sections"].append({"id": "3.2.9", "title_en": "Last title", "page_number": 5})
    report = review_pipeline_documents([doc])
    assert "SECTION_NODE_MAY_BE_CROSS_REFERENCE_CELL" in report["documents"][0]["issue_counts"]
    assert verify_review_pointers(report, [doc])["verified_pointer_count"] >= 1


def test_content_source_identifier_is_bounded():
    doc = _doc()
    doc["contents"][0]["section_id"] = "secret" * 1000
    with pytest.raises(ValueError, match="Invalid content record"):
        review_pipeline_documents([doc])
    doc["contents"][0]["section_id"] = 13
    with pytest.raises(ValueError, match="Invalid content record"):
        review_pipeline_documents([doc])
