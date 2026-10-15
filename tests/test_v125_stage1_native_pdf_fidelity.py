"""v1.25 native-source acceptance harness regressions (synthetic local PDFs)."""
from __future__ import annotations

import copy
import json

import fitz
import pytest

from app.engineering_intelligence.native_layout import inspect_pdf_native
from app.engineering_intelligence.pipeline_json_review import review_pipeline_documents
from app.engineering_intelligence.stage1_native_fidelity import reconcile_pdf_pipeline
from scripts.audit_stage1_pdf_fidelity import main as cli_main
from scripts.review_stage1_pipeline_json import main as create_review

A = "When DCM communication completes, the APP button shall appear."
B = "After software update, the APP button shall remain available."
C = "When the vehicle powers off, the session shall terminate."
PRIVATE_NAME = "confidential-Alice@example.org-DCM.pdf"


def source_pdf(tmp_path, *, first=(A,), second=(B,), geometry=False):
    path = tmp_path / PRIVATE_NAME
    pdf = fitz.open()
    for i, rows in enumerate((first, second)):
        page = pdf.new_page(width=500, height=700)
        for n, text in enumerate(rows):
            page.insert_text((60, 90 + 30*n), text, fontsize=10)
        if geometry:
            y0, y1 = (600, 690) if i == 0 else (10, 100)
            shape = page.new_shape()
            for x in (100, 200, 300):
                shape.draw_line((x, y0), (x, y1))
            for y in (y0, (y0+y1)/2, y1):
                shape.draw_line((100, y), (300, y))
            shape.finish(color=(0, 0, 0), width=1)
            shape.commit()
            for row_number in range(2):
                ty = y0 + 20 + row_number * (y1 - y0) / 2
                page.insert_text((110, ty), "Region" if row_number == 0 else "NA", fontsize=8)
                page.insert_text((210, ty), "State" if row_number == 0 else "On", fontsize=8)
    pdf.save(path)
    pdf.close()
    return path


def doc(*parts, pages=None, section="4.3"):
    pages = pages or tuple(range(1, len(parts)+1))
    return {
        "document": {"document_id": "sensitive-private-id", "project_code": "21MM",
                     "file_name": PRIVATE_NAME},
        "metadata": {"source_format": "pdf", "specification_code": "888",
                     "specification_version": "v1.02"},
        "sections": [{"id": "4.3", "title_jp": "DCM 状態"}],
        "contents": [{"section_id": section, "text": t, "page_number": page}
                     for t, page in zip(parts, pages)],
    }


def audit(tmp_path, d=None, *, pdf=None, **kwargs):
    d = d or doc(A, B)
    pdf = pdf or source_pdf(tmp_path)
    return reconcile_pdf_pipeline([d], review_pipeline_documents([d]),
                                  inspect_pdf_native(pdf), **kwargs)


def test_unique_native_line_and_metadata_are_review_only(tmp_path):
    result = audit(tmp_path)
    assert result["native_pdf_page_count"] == 2
    assert result["alignment_status_counts"] == {"UNIQUE_NATIVE_LINE_CANDIDATE": 2}
    assert result["section_tree_status_counts"] == {"SECTION_ID_IN_EXPORTED_TREE": 2}
    assert result["entries"][0]["native_locator"]["page"] == 1
    assert result["entries"][1]["native_locator"]["page"] == 2
    assert not result["native_extraction_provenance_authenticated"]
    assert not result["semantic_acceptance_complete"]
    assert not result["condition_action_scope_verified"]
    assert not result["database_modified"]
    assert not result["qdrant_modified"]
    encoded = json.dumps(result, ensure_ascii=False)
    for secret in (PRIVATE_NAME, A, B, "sensitive-private-id", "DCM 状態"):
        assert secret not in encoded


def test_wrong_page_found_elsewhere_never_assigns_native_locator(tmp_path):
    result = audit(tmp_path, doc(A, pages=(2,)))
    assert result["alignment_status_counts"] == {"EXACT_NATIVE_TEXT_ON_DIFFERENT_PAGE": 1}
    assert result["entries"][0]["native_locator"] is None


def test_out_of_range_page_when_last_pdf_page_has_no_text(tmp_path):
    pdf = source_pdf(tmp_path, first=(A,), second=())
    result = audit(tmp_path, doc(A, pages=(3,)), pdf=pdf)
    assert result["native_pdf_page_count"] == 2
    assert result["entries"][0]["text_alignment_status"] == "PIPELINE_PAGE_OUT_OF_RANGE"


def test_duplicate_same_text_on_two_pages_is_ambiguous(tmp_path):
    pdf = source_pdf(tmp_path, first=(A,), second=(A,))
    result = audit(tmp_path, doc(A, pages=(1,)), pdf=pdf)
    assert result["alignment_status_counts"] == {"AMBIGUOUS_REPEATED_NATIVE_TEXT_REVIEW": 1}
    assert result["entries"][0]["native_locator"] is None


def test_different_case_is_not_equated(tmp_path):
    result = audit(tmp_path, doc(A.lower(), pages=(1,)))
    assert result["entries"][0]["text_alignment_status"] == "NO_EXACT_NATIVE_TEXT_SEQUENCE_REVIEW"


def test_contiguous_native_lines_match_whitespace_but_not_other_words(tmp_path):
    pdf = source_pdf(tmp_path, first=(A, C), second=())
    result = audit(tmp_path, doc(A + "\n" + C, pages=(1,)), pdf=pdf)
    assert result["alignment_status_counts"] == {"UNIQUE_NATIVE_MULTILINE_CANDIDATE": 1}
    assert result["entries"][0]["native_locator"]["first_line"] == 0
    assert result["entries"][0]["native_locator"]["last_block"] >= 0
    swapped = audit(tmp_path, doc(C + "\n" + A, pages=(1,)), pdf=pdf)
    assert swapped["entries"][0]["native_locator"] is None


def test_no_text_layer_cannot_pass_even_with_pdf_pages(tmp_path):
    pdf = source_pdf(tmp_path, first=(), second=())
    result = audit(tmp_path, doc(A, pages=(1,)), pdf=pdf)
    assert result["native_pdf_has_no_text_layer"]
    assert result["alignment_status_counts"] == {"NO_EXACT_NATIVE_TEXT_SEQUENCE_REVIEW": 1}


def test_short_or_numeric_text_cannot_be_a_strong_anchor(tmp_path):
    result = audit(tmp_path, doc("DCM", "1234567890123456", pages=(1, 1)))
    assert result["alignment_status_counts"] == {"WEAK_NATIVE_TEXT_ANCHOR_REVIEW": 2}


def test_section_missing_from_exported_tree_is_separate_warning(tmp_path):
    result = audit(tmp_path, doc(A, pages=(1,), section="4.99"))
    assert result["alignment_status_counts"] == {"UNIQUE_NATIVE_LINE_CANDIDATE": 1}
    assert result["section_tree_status_counts"] == {"SECTION_NOT_IN_EXPORTED_TREE": 1}


def test_budgets_and_output_truncation_are_explicit(tmp_path):
    d = doc(A, B)
    result = audit(tmp_path, d, max_contents=1, max_entries=1)
    assert result["contents_not_sampled"] == 1
    assert result["contents_sampled"] == 1
    assert len(result["entries"]) == 1
    result = audit(tmp_path, d, max_entries=1)
    assert result["entries_omitted"] == 1
    assert sum(result["alignment_status_counts"].values()) == 2
    with pytest.raises(ValueError, match="budgets"):
        audit(tmp_path, d, max_group_lines=13)
    with pytest.raises(ValueError, match="bounded"):
        audit(tmp_path, d, max_lines=1)


def test_stale_review_fails_closed_before_audit(tmp_path):
    d = doc(A)
    review = review_pipeline_documents([d])
    stale = copy.deepcopy(d)
    stale["contents"][0]["text"] = B
    pdf = source_pdf(tmp_path)
    with pytest.raises(ValueError, match="drift"):
        reconcile_pdf_pipeline([stale], review, inspect_pdf_native(pdf))
    forged = copy.deepcopy(review)
    forged["evidence_queue"].clear()
    with pytest.raises(ValueError, match="drift"):
        reconcile_pdf_pipeline([d], forged, inspect_pdf_native(pdf))


def test_document_scope_and_pdf_format_enforced(tmp_path):
    d = doc(A)
    native = inspect_pdf_native(source_pdf(tmp_path))
    with pytest.raises(ValueError, match="index"):
        reconcile_pdf_pipeline([d], review_pipeline_documents([d]), native, document_index=2)
    d["metadata"]["source_format"] = "xlsx"
    with pytest.raises(ValueError, match="PDF"):
        reconcile_pdf_pipeline([d], review_pipeline_documents([d]), native)


def test_native_sha_changes_with_pdf_changes(tmp_path):
    f = source_pdf(tmp_path, first=(A,), second=(B,))
    first = audit(tmp_path, doc(A), pdf=f)
    f = source_pdf(tmp_path, first=(C,), second=(B,))
    other = audit(tmp_path, doc(A), pdf=f)
    assert first["native_file_sha256"] != other["native_file_sha256"]
    assert other["entries"][0]["native_locator"] is None


def test_crosspage_geometry_stays_candidate_only(tmp_path):
    pdf = source_pdf(tmp_path, geometry=True)
    native = inspect_pdf_native(pdf, include_table_geometry=True)
    assert len(native.pdf_table_candidates) == 2
    result = reconcile_pdf_pipeline([doc(A, B)], review_pipeline_documents([doc(A, B)]), native)
    assert result["pdf_table_geometry_candidates"] == 2
    assert result["crosspage_continuity_candidates_total"] >= 1
    assert not result["physical_table_header_verified"]
    assert not result["semantic_acceptance_complete"]
    assert all(not candidate["header_verified"] and not candidate["continuation_verified"]
               for candidate in result["crosspage_review_queue"])


def test_cli_end_to_end_does_not_overwrite_or_expose_private_text(tmp_path, capsys):
    file = source_pdf(tmp_path)
    d = doc(A, B)
    json_path = tmp_path / "export.json"
    review_path = tmp_path / "review.json"
    result_path = tmp_path / "native_audit.json"
    json_path.write_text(json.dumps(d), encoding="utf-8")
    assert create_review(["--json", str(json_path), "--report-out", str(review_path)]) == 0
    capsys.readouterr()
    argv = ["--pdf", str(file), "--json", str(json_path), "--review", str(review_path),
            "--report-out", str(result_path)]
    assert cli_main(argv) == 0
    out = capsys.readouterr()
    assert "NATIVE_PDF_REVIEW_REQUIRED" in out.out
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["alignment_status_counts"] == {"UNIQUE_NATIVE_LINE_CANDIDATE": 2}
    assert A not in result_path.read_text(encoding="utf-8")
    assert PRIVATE_NAME not in result_path.read_text(encoding="utf-8")
    assert cli_main(argv) == 2
    assert "NOT_AUDITED" in capsys.readouterr().err
    assert cli_main([*argv[:-1], str(json_path)]) == 2


def test_cli_rejects_symlink_and_stale_pipeline(tmp_path, capsys):
    pdf = source_pdf(tmp_path)
    d = doc(A)
    json_path = tmp_path / "export.json"
    review_path = tmp_path / "review.json"
    json_path.write_text(json.dumps(d), encoding="utf-8")
    assert create_review(["--json", str(json_path), "--report-out", str(review_path)]) == 0
    capsys.readouterr()
    shortcut = tmp_path / "alias.pdf"
    try:
        shortcut.symlink_to(pdf)
    except (OSError, NotImplementedError):
        pass
    else:
        assert cli_main(["--pdf", str(shortcut), "--json", str(json_path), "--review", str(review_path),
                         "--report-out", str(tmp_path / "r1.json")]) == 2
    d["contents"][0]["text"] = B
    json_path.write_text(json.dumps(d), encoding="utf-8")
    assert cli_main(["--pdf", str(pdf), "--json", str(json_path), "--review", str(review_path),
                     "--report-out", str(tmp_path / "r2.json")]) == 2
    assert not (tmp_path / "r2.json").exists()
