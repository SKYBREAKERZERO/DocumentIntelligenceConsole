"""Stage1 v1.21 regression: synthetic Bluetooth Audio-like source evidence.

No proprietary specification text is bundled. These tests assert diagnostics,
unchanged provenance and manual-review-only relationships.
"""
from __future__ import annotations

import copy
import json

from app.analyzer.title_detector import TitleDetector
from app.engineering_intelligence.atomic import extract_atomic_candidates
from app.engineering_intelligence.condition_scope import extract_requirement_scope
from app.engineering_intelligence.crosspage import suggest_crosspage_tables
from app.engineering_intelligence.native_layout import (
    NativeLayoutReport, PdfGeometryCell, PdfTableCandidate,
)
from app.engineering_intelligence.pipeline_json_review import review_pipeline_documents
from app.engineering_intelligence.requirements import extract_requirement_units
from app.engineering_intelligence.stage1_semantics import (
    inspect_requirement_signals, possible_pdf_page_table_fragments, source_language,
)


def _primary(contents=None):
    if contents is None:
        contents = []
    return {
        "document": {"document_id": "secret-bt-550", "file_name": "Bluetooth Audio spec.pdf",
                     "project_code": "21MM"},
        "metadata": {"source_format": "pdf", "specification_version": "v2.52",
                     "specification_code": "550"},
        "chapters": [{"id": "2"}],
        "sections": [
            {"id": "2.2", "title_jp": "機能概要", "page_number": 3},
            {"id": "2.9", "title_en": "Now Playing", "page_number": 9},
        ],
        "contents": contents,
    }


def _scope(text):
    unit, = extract_requirement_units(
        document_id="bt", content_id=1, section_id="2.2", text=text)
    atom, = extract_atomic_candidates(unit)
    return unit, extract_requirement_scope(unit, atom)


def test_reference_titles_suppressed_but_real_japanese_heading_retained():
    assert TitleDetector.detect("2.9 Now Playing 参照") is None
    assert TitleDetector.detect("2.9 Now Playingを参照") is None
    assert TitleDetector.detect("2.9 Now Playing を参照先") is None
    assert TitleDetector.detect("2.9 参照方法") is not None
    assert TitleDetector.detect("2.2.3 Playback volume specification") is not None


def test_section_node_cross_reference_warning_is_review_only():
    p = _primary([{
        "section_id": "2.9", "page_number": 9,
        "text": "2.9 Now Playing 参照\nother section reference",
    }])
    result = review_pipeline_documents([p])
    codes = {x["code"] for x in result["findings"]}
    assert "SECTION_NODE_MAY_BE_CROSS_REFERENCE_CELL" in codes
    assert result["status"] == "DIAGNOSTIC_ONLY"
    assert not result["database_modified"]
    assert not result["qdrant_modified"]
    assert "secret-bt-550" not in json.dumps(result)


def test_reference_number_in_body_alone_does_not_prove_misclassified_section():
    p = _primary([{
        "section_id": "2.2", "page_number": 3,
        "text": "The state table refers to section 2.9 elsewhere.",
    }])
    codes = {x["code"] for x in review_pipeline_documents([p])["findings"]}
    assert "SECTION_NODE_MAY_BE_CROSS_REFERENCE_CELL" not in codes


def test_japanese_english_requirements_are_separate_without_fake_translation_pairs():
    p = _primary([{
        "section_id": "2.2", "page_number": 3,
        "text": "BT接続時、HUはBody ECUへ通知すること。\n"
                "When BT connects, HU shall notify Body ECU.",
    }])
    before = copy.deepcopy(p)
    report = review_pipeline_documents([p])
    d = report["documents"][0]
    assert d["normative_language_counts_sampled"]["JA"] == 1
    assert d["normative_language_counts_sampled"]["EN"] == 1
    assert d["bilingual_section_count_sampled"] == 1
    assert "JA_EN_REQUIREMENTS_EQUIVALENCE_NOT_VERIFIED" in d["issue_counts"]
    assert "translation_pairs" not in json.dumps(report)
    assert p == before
    assert report["source_verified"] is False


def test_japanese_implicit_timing_scope_requires_explicit_delimiter():
    _, explicit = _scope("BT接続時、HUはBody ECUへ通知すること。")
    assert explicit.status == "EXPLICIT_PREFIX_CANDIDATE"
    assert explicit.condition.exact_text == "BT接続時、"
    assert explicit.action.exact_text == "HUはBody ECUへ通知すること。"
    _, implicit = _scope("BT接続時はBody ECUへ通知する必要がある。")
    assert implicit.status == "UNRESOLVED"
    assert "SCOPE_BOUNDARY_NOT_EXPLICIT" in implicit.review_reasons


def test_postposed_exception_and_multiple_actions_do_not_assert_one_scope():
    _, postposed = _scope("When BT connects, HU shall load unless NA.")
    assert postposed.status == "UNRESOLVED"
    _, multiple = _scope("HU shall load and shall notify.")
    assert multiple.status == "UNRESOLVED"
    assert multiple.action is None
    assert "MULTIPLE_ACTION_CLAUSES_UNRESOLVED" in multiple.review_reasons


def test_parent_atomic_condition_is_not_inherited_across_actions():
    text = "When BT connects, HU shall load; HU must notify."
    unit, = extract_requirement_units(document_id="bt", content_id=1,
                                       section_id="2.2", text=text)
    atoms = extract_atomic_candidates(unit)
    assert len(atoms) == 2
    scopes = [extract_requirement_scope(unit, x) for x in atoms]
    assert all(s.status == "UNRESOLVED" for s in scopes)
    assert all(s.condition is None and s.action is None for s in scopes)


def test_pdf_adjacent_page_fragments_are_not_merged_or_marked_as_headers():
    pieces = [
        {"section_id": "2.2", "page_number": 7,
         "text": "Region | Action | Mode\nNA | HU shall load | A\nEU | HU shall skip | B"},
        {"section_id": "2.2", "page_number": 8,
         "text": "Region | Action | Mode\nNA | HU must notify | C\nEU | HU must save | D"},
    ]
    assert possible_pdf_page_table_fragments(pieces) == (("2.2", 7),)
    p = _primary(pieces)
    result = review_pipeline_documents([p])
    codes = {x["code"] for x in result["findings"]}
    assert "PDF_CROSSPAGE_TABLE_FRAGMENT_REQUIRES_NATIVE_VERIFICATION" in codes
    assert "table_merges" not in result
    assert not result["original_native_layout_verified"]


def test_pdf_nonadjacent_pages_and_sections_do_not_create_fragment_pairs():
    items = [
        {"section_id": "2.2", "page_number": 7, "text": "A | B | C\nD | E | F"},
        {"section_id": "2.2", "page_number": 9, "text": "A | B | C\nD | E | F"},
        {"section_id": "3.1", "page_number": 8, "text": "A | B | C\nD | E | F"},
    ]
    assert not possible_pdf_page_table_fragments(items)


def _native(page, idx, *, top, bottom):
    x = (70., 180., 350.)
    cells = tuple(PdfGeometryCell(0, col, (x[col], top, x[col+1], bottom))
                  for col in range(2))
    return PdfTableCandidate(page, idx, (70., top, 350., bottom), 1, 2, cells,
                             page_height=800., page_width=600.)


def test_ambiguous_native_crosspage_geometry_is_not_promoted():
    report = NativeLayoutReport(
        "sample", "PDF", "a" * 64, 123,
        pdf_table_candidates=(
            _native(1, 1, top=690, bottom=760),
            _native(2, 2, top=50, bottom=150),
            _native(2, 3, top=50, bottom=160),
        ),
    )
    options = suggest_crosspage_tables(report)
    assert len(options) == 2
    assert all(x.status == "POSSIBLE_CONTINUATION_REVIEW_ONLY" for x in options)
    assert all("MULTIPLE_COMPATIBLE_TABLES_REVIEW" in x.reasons for x in options)


def test_mixed_language_and_scope_diagnostics_redact_all_text():
    docs = _primary([{
        "section_id": "2.2", "page_number": 3,
        "text": "When BT connects, HU shall loadすること unless NA.",
    }])
    report = review_pipeline_documents([docs])
    assert report["documents"][0]["normative_language_counts_sampled"].get("MIXED") == 1
    assert "MIXED_LANGUAGE_NORMATIVE_SOURCE_REQUIRES_REVIEW" in {
        f["code"] for f in report["findings"]
    }
    assert "shall load" not in json.dumps(report)
