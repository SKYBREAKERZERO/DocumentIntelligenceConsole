"""v1.23 DCM lexical lifecycle evidence; all fixtures are synthetic."""
from __future__ import annotations

import copy
import json

import pytest

from app.engineering_intelligence.pipeline_json_review import review_pipeline_documents
from app.engineering_intelligence.stage1_dcm import inspect_dcm_sources
from app.engineering_intelligence.stage1_review_evidence import verify_review_pointers


def _doc(*lines: str, role: str = "PRIMARY_CANDIDATE", sections: tuple[str, ...] | None = None) -> dict:
    assert lines
    content = [
        {"section_id": (sections or ("4.3",) * len(lines))[i],
         "page_number": 7 + i, "text": line,
         "metadata": {"native_offsets": True}}
        for i, line in enumerate(lines)
    ]
    return {
        "document": {"document_id": "customer-secret-dcm", "project_code": "21MM",
                     "file_name": "private-Dcm-spec.pdf"},
        "metadata": {"source_format": "pdf", "document_role": role,
                     "specification_code": "888", "specification_version": "v1.02"},
        "sections": [{"id": "4.3", "title_jp": "DCM状態"},
                     {"id": "4.4", "title_jp": "その他"}],
        "contents": content,
    }


def _report(*lines: str, **kwargs) -> dict:
    return review_pipeline_documents([_doc(*lines, **kwargs)])


def _dcm_points(report: dict) -> list[dict]:
    return [p for p in report["evidence_queue"] if p["category"] == "DCM_SOURCE"]


def test_dcm_initial_after_across_lines_creates_manual_multiphase_warning():
    doc = _doc("Telematics釦の初期値はなし。DCM状態を確認。",
               "DCM通信後、APP釦をインストールして表示すること。")
    unchanged = copy.deepcopy(doc)
    report = review_pipeline_documents([doc])
    state = report["documents"][0]
    assert state["dcm_phase_counts_sampled"] == {"AFTER": 1, "INITIAL": 1}
    assert state["dcm_multi_phase_sections_sampled"] == 1
    assert "DCM_MULTIPHASE_SECTION_REQUIRES_REVIEW" in state["issue_counts"]
    assert [p["status"] for p in _dcm_points(report)] == ["INITIAL_UNVERIFIED", "AFTER_UNVERIFIED"]
    assert not state["dcm_state_transitions_verified"]
    assert not state["dcm_event_action_scope_verified"]
    assert verify_review_pointers(report, [doc])["verified_pointer_count"] >= 2
    assert doc == unchanged
    serialized = json.dumps(report, ensure_ascii=False)
    for private in ("Telematics釦", "インストール", "customer-secret", "private-Dcm"):
        assert private not in serialized


def test_dcm_multiple_phases_in_one_line_is_not_a_verified_state_change():
    report = _report("初期状態はなし。ただしDCM通信後はAPP釦を表示すること。")
    data = report["documents"][0]
    assert data["dcm_multi_phase_lines_sampled"] == 1
    assert data["dcm_multi_phase_sections_sampled"] == 1
    assert "DCM_MULTIPLE_PHASES_IN_SOURCE_LINE_REQUIRES_REVIEW" in data["issue_counts"]
    assert "DCM_EXCEPTION_OR_FALLBACK_SCOPE_UNVERIFIED" in data["issue_counts"]
    assert "DCM_NEGATED_STATE_SCOPE_UNVERIFIED" in data["issue_counts"]
    assert _dcm_points(report)[0]["status"] == "MULTIPLE_PHASES_UNRESOLVED"


def test_dcm_before_and_after_explicit_japanese_anchors():
    result = _report("DCM通信前にボタンを表示しないこと。", "DCMとの通信完了後にAPPを表示すること。")
    assert result["documents"][0]["dcm_phase_counts_sampled"] == {"AFTER": 1, "BEFORE": 1}
    assert [p["status"] for p in _dcm_points(result)] == ["BEFORE_UNVERIFIED", "AFTER_UNVERIFIED"]


def test_dcm_failure_and_exception_do_not_assert_fallback_actions():
    report = _report("DCM通信が失敗した場合、ただしNAでは表示しないこと。")
    d = report["documents"][0]
    assert d["dcm_phase_counts_sampled"] == {"FAILURE": 1}
    assert d["dcm_exception_lines_sampled"] == 1
    assert d["dcm_negation_lines_sampled"] == 1
    assert _dcm_points(report)[0]["status"] == "FAILURE_UNVERIFIED"
    assert not d["dcm_event_action_scope_verified"]


def test_dcm_success_en_and_after_en_are_preserved_as_ambiguous_when_both_present():
    report = _report("After DCM communication, if DCM communication succeeds, HU shall display the APP.")
    assert report["documents"][0]["dcm_phase_counts_sampled"] == {"AFTER": 1, "SUCCESS": 1}
    assert _dcm_points(report)[0]["status"] == "MULTIPLE_PHASES_UNRESOLVED"


def test_dcm_action_without_phase_is_scope_unresolved():
    report = _report("DCM shall display a menu button.")
    assert _dcm_points(report)[0]["status"] == "ACTION_SCOPE_UNRESOLVED"
    assert report["documents"][0]["dcm_phase_counts_sampled"] == {}


def test_dcm_acronym_only_is_context_not_action():
    report = _report("DCM is described here.")
    assert _dcm_points(report)[0]["status"] == "CONTEXT_UNVERIFIED"
    assert report["documents"][0]["requires_manual_review"]


def test_dcm_rejects_subword_and_other_controller_event_false_positive():
    report = _report("ABCDCM20 is not a communication module.",
                     "After BT communication, DCM shall notify HU.")
    assert len(_dcm_points(report)) == 1
    assert _dcm_points(report)[0]["status"] == "ACTION_SCOPE_UNRESOLVED"
    assert report["documents"][0]["dcm_phase_counts_sampled"] == {}


def test_dcm_separate_section_phases_never_create_cross_section_transition():
    report = _report("DCM 初期値なし。", "DCM通信後に更新すること。", sections=("4.3", "4.4"))
    assert report["documents"][0]["dcm_multi_phase_sections_sampled"] == 0
    assert "DCM_MULTIPHASE_SECTION_REQUIRES_REVIEW" not in report["documents"][0]["issue_counts"]


def test_fullwidth_dcm_is_visible_without_changing_verbatim_source():
    doc = _doc("ＤＣＭ通信後は通知すること。")
    review = review_pipeline_documents([doc])
    point, = _dcm_points(review)
    assert point["status"] == "AFTER_UNVERIFIED"
    assert point["end_offset"] == len(doc["contents"][0]["text"])
    assert verify_review_pointers(review, [doc])["verified_pointer_count"] >= 1


def test_supported_role_is_not_promoted_into_dcm_requirements():
    doc = _doc("DCM通信後にボタンを表示。", role="SUPPORTING")
    doc["metadata"].update({"qa_index_policy": "EXCLUDE", "specification_code": ""})
    review = review_pipeline_documents([doc])
    assert review["documents"][0]["dcm_lines_sampled"] == 0
    assert not _dcm_points(review)


def test_dcm_offset_for_multiline_unicode_source_is_exact():
    doc = _doc("some context\nDCM通信後に表示すること。\nmore context")
    review = review_pipeline_documents([doc])
    point, = _dcm_points(review)
    assert point["start_offset"] == len("some context\n")
    assert point["end_offset"] == len("some context\nDCM通信後に表示すること。")
    assert verify_review_pointers(review, [doc])["verified_pointer_count"] >= 1


def test_dcm_provenance_detects_mutated_source_data():
    doc = _doc("DCM通信後に表示すること。")
    report = review_pipeline_documents([doc])
    doc["contents"][0]["text"] = "DCM通信後に表示しないこと。"
    with pytest.raises(ValueError, match="drift"):
        verify_review_pointers(report, [doc])


def test_dcm_evidence_cap_cannot_falsely_claim_complete_coverage():
    doc = _doc("DCM初期状態。\nDCM通信後。\nDCM通信が失敗。")
    # Semantic evidence and DCM evidence share the export-level budget.
    report = review_pipeline_documents([doc], max_evidence=1)
    assert report["documents"][0]["dcm_lines_sampled"] == 3
    assert report["evidence_queue_truncated"] >= 2
    assert len(_dcm_points(report)) <= 1


def test_dcm_probe_rejects_false_budgets_and_reports_line_truncation():
    content = [{"text": "DCM通信後\nDCM通信前", "section_id": "4.3", "page_number": 1}]
    with pytest.raises(ValueError, match="budget"):
        inspect_dcm_sources("a" * 20, content, max_lines=0)
    result = inspect_dcm_sources("a" * 20, content, max_lines=1)
    assert not result["probe_complete"]
    assert result["dcm_lines_sampled"] == 1
    assert result["evidence_total"] == 1


def test_no_dcm_lines_report_no_dcm_findings():
    report = _report("Bluetooth Audio specification with no controller states.")
    assert not _dcm_points(report)
    assert report["documents"][0]["dcm_lines_sampled"] == 0
    assert not any(k.startswith("DCM_") for k in report["documents"][0]["issue_counts"])


def test_telematics_initial_value_without_dcm_token_is_reviewable_same_section():
    source = _doc("Telematics釦の初期値はなし。",
                  "DCM通信後、APP釦を表示すること。")
    review = review_pipeline_documents([source])
    points = [p for p in review["evidence_queue"] if p["category"] == "DCM_RELATED_UI_SOURCE"]
    assert len(points) == 1
    assert points[0]["content_index"] == 1
    assert points[0]["status"] == "RELATED_INITIAL_UNVERIFIED"
    d = review["documents"][0]
    assert d["dcm_related_ui_lines_sampled"] == 1
    assert d["dcm_multi_phase_sections_sampled"] == 1
    assert "DCM_RELATED_UI_CONTEXT_REQUIRES_REVIEW" in d["issue_counts"]
    assert verify_review_pointers(review, [source])["verified_pointer_count"] >= 2
    assert "Telematics釦" not in json.dumps(review, ensure_ascii=False)


def test_telematics_initial_value_in_other_section_is_not_associated():
    report = _report("Telematics釦の初期値はなし。",
                     "DCM通信後、APP釦を表示すること。", sections=("4.4", "4.3"))
    assert report["documents"][0]["dcm_related_ui_lines_sampled"] == 0
    assert not any(p["category"] == "DCM_RELATED_UI_SOURCE" for p in report["evidence_queue"])


def test_app_button_without_initial_marker_is_not_associated():
    report = _report("APPボタンの色は青。", "DCM通信後に表示すること。")
    assert report["documents"][0]["dcm_related_ui_lines_sampled"] == 0
