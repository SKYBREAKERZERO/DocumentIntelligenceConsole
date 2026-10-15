"""v1.27 synthetic Gold and fail-closed acceptance regression (no private spec)."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from app.engineering_intelligence.stage1_native_fidelity import reconcile_pdf_pipeline
from app.engineering_intelligence.stage1_structural_gold import (
    evaluate_structural_gold, prepare_structural_gold, reviewed_label_digest,
)
from app.engineering_intelligence.native_layout import NativeLayoutReport, PdfTableCandidate
from app.engineering_intelligence.pipeline_json_review import review_pipeline_documents
from scripts import evaluate_stage1_structural_gold as cli


def sha(s):
    return hashlib.sha256(s.encode()).hexdigest()


def reports():
    h, f = sha("document"), sha("native")
    nrows = [{"content_index": i, "content_sha256": sha(f"text{i}"),
              "text_alignment_status": "UNIQUE_NATIVE_LINE_CANDIDATE" if i == 1 else "EXACT_NATIVE_TEXT_ON_DIFFERENT_PAGE",
              "section_tree_status": "SECTION_ID_IN_EXPORTED_TREE" if i == 1 else "SECTION_NOT_IN_EXPORTED_TREE"}
             for i in (1, 2)]
    native = {"schema": "stage1-native-pipeline-fidelity-v1", "document_hash": h,
              "native_file_sha256": f, "contents_not_sampled": 0,
              "entries_omitted": 0, "pipeline_evidence_queue_complete": True,
              "crosspage_candidate_queue_complete": True,
              "crosspage_pair_universe_complete": True,
              "pipeline_json_review_replayed": True,
              "pipeline_content_count": 2, "entries": nrows,
              "crosspage_pair_universe_count": 3,
              "crosspage_pair_universe": [
                  {"previous_page": 1, "previous_table_index": 0,
                   "next_page": 2, "next_table_index": x} for x in (0, 1, 2)],
              "crosspage_review_queue": [
                  {"previous_page": 1, "previous_table_index": 0,
                   "next_page": 2, "next_table_index": x, "status": "POSSIBLE_CONTINUATION_REVIEW_ONLY"}
                  for x in (0, 1)],
              }
    chain = {"schema": "stage1-native-sql-chain-v1", "project": "21MM", "document_hash": h,
             "native_file_sha256": f, "source_review_queue_complete": True,
             "evidence_count": 2,
             "entries": [{"content_index": i, "review_key": sha(f"key{i}"),
                          "native_alignment_status": nrows[i-1]["text_alignment_status"],
                          "exported_section_status": nrows[i-1]["section_tree_status"],
                          "chain_status": "FOUR_LAYER_LITERAL_CANDIDATE_REVIEW_ONLY" if i == 1 else "NATIVE_ANCHOR_NOT_UNIQUE",
                          "sql_content_status": "EXACT_SQL_CONTENT_ONLY" if i == 1 else "SQL_CONTENT_NOT_EXACT_UNIQUE",
                          "sql_unit_status": "EXACT_SQL_UNIT_REVIEW_ONLY" if i == 1 else "SQL_REQUIREMENT_SPAN_NOT_EXACT"}
                         for i in (1, 2)]}
    return native, chain


def reviewed(n=None, c=None, *, source_labels=((True, True), (False, False)),
             table_labels=(True, False, True)):
    n, c = (n, c) if n is not None else reports()
    gold = prepare_structural_gold(n, c, dataset_id="DCM-structural-benchmark")
    for row, (literal, section) in zip(gold["sources"], source_labels):
        row["literal_source_correct"] = literal
        row["section_assignment_correct"] = section
    for row, correct in zip(gold["adjacent_table_pairs"], table_labels):
        row["true_continuation"] = correct
    gold["review"] = {"annotation_state": "VERIFIED", "annotator_id": "analyst-a",
                      "reviewer_id": "reviewer-b", "review_note": "Native paper checked",
                      "reviewed_label_sha256": reviewed_label_digest(gold)}
    return gold


def test_preparation_never_fills_human_labels():
    n, c = reports()
    gold = prepare_structural_gold(n, c, dataset_id="test")
    assert all(x["literal_source_correct"] is None for x in gold["sources"])
    assert all(x["true_continuation"] is None for x in gold["adjacent_table_pairs"])
    assert gold["review"]["annotation_state"] == "PENDING"
    with pytest.raises(ValueError, match="unlabelled"):
        evaluate_structural_gold(n, c, gold)


def test_scores_crosspage_false_negative_without_pretending_suggestion_recall():
    n, c = reports()
    score = evaluate_structural_gold(n, c, reviewed())
    cross = score["crosspage_geometry_candidate"]
    assert (cross["true_positive"], cross["false_positive"], cross["false_negative"]) == (1, 1, 1)
    assert cross["precision"] == pytest.approx(.5)
    assert cross["recall"] == pytest.approx(.5)
    assert cross["f1"] == pytest.approx(.5)
    assert score["literal_source_classification"]["f1"] == pytest.approx(1)
    assert score["section_membership_proxy"]["f1"] == pytest.approx(1)
    assert not score["reviewer_identity_authenticated"]
    assert not score["physical_table_headers_semantically_verified"]


def test_all_negative_no_perfect_recall():
    n, c = reports()
    score = evaluate_structural_gold(n, c, reviewed(source_labels=((False, False), (False, False)),
                                                  table_labels=(False, False, False)))
    assert score["crosspage_geometry_candidate"]["recall"] is None
    assert score["crosspage_geometry_candidate"]["f1"] is None
    assert score["literal_source_classification"]["f1"] is None


@pytest.mark.parametrize("edit", [
    lambda n,c,g: n.update(native_file_sha256=sha("changed")),
    lambda n,c,g: n.update(entries_omitted=1),
    lambda n,c,g: n.update(contents_not_sampled=1),
    lambda n,c,g: n.update(crosspage_pair_universe_complete=False),
    lambda n,c,g: n.update(crosspage_candidate_queue_complete=False),
    lambda n,c,g: n.update(pipeline_evidence_queue_complete=False),
    lambda n,c,g: c.update(source_review_queue_complete=False),
    lambda n,c,g: c["entries"][1].update(review_key=c["entries"][0]["review_key"]),
    lambda n,c,g: n["crosspage_pair_universe"].append(n["crosspage_pair_universe"][0]),
    lambda n,c,g: n["crosspage_review_queue"][0].update(next_table_index=99),
    lambda n,c,g: g["sources"][0].update(content_sha256=sha("tampered")),
    lambda n,c,g: g["sources"][0].update(literal_source_correct=None),
    lambda n,c,g: g["adjacent_table_pairs"][0].update(true_continuation="yes"),
    lambda n,c,g: g["review"].update(reviewer_id="analyst-a"),
    lambda n,c,g: g["review"].update(reviewed_label_sha256=sha("old")),
    lambda n,c,g: g["review"].update(annotation_state="PENDING"),
    lambda n,c,g: g["sources"].reverse(),
    lambda n,c,g: c["entries"][0].update(sql_unit_status="SQL_REQUIREMENT_SPAN_NOT_EXACT"),
])
def test_reject_incomplete_changed_or_unreviewed_sources(edit):
    n, c = reports()
    g = reviewed(n, c)
    edit(n, c, g)
    with pytest.raises(ValueError):
        evaluate_structural_gold(n, c, g)


def test_exhaustive_pair_universe_from_native_geometry():
    import fitz
    from app.engineering_intelligence.native_layout import PdfGeometryCell
    from app.engineering_intelligence.crosspage import suggest_crosspage_tables

    def table(page, ix, x):
        return PdfTableCandidate(page, ix, (x, 740 if page == 1 else 20, x+100, 780 if page == 1 else 80),
                                 1, 1, (PdfGeometryCell(0, 0, (x, 20, x+100, 50)),),
                                 page_height=800)
    layout = NativeLayoutReport("test", "PDF", sha("dummy pdf bytes"), 20,
                                pdf_table_candidates=(table(1,0,10),table(1,1,300),
                                                      table(2,0,10),table(2,1,500)),
                                pdf_page_count=2)
    assert len(suggest_crosspage_tables(layout)) == 1
    d = {"document": {"document_id": "doc", "project_code": "21MM", "file_name": "anon.pdf"},
         "metadata": {"source_format": "pdf", "specification_code": "888", "specification_version": "v1.00"},
         "sections": [{"id": "1", "title_jp": "test"}],
         "contents": [{"text": "When DCM communication completes, display the APP button.",
                       "section_id": "1", "page_number": 1}]}
    rev = review_pipeline_documents([d])
    result = reconcile_pdf_pipeline([d], rev, layout)
    assert result["crosspage_pair_universe_count"] == 4
    assert result["crosspage_pair_universe_complete"]
    assert len(result["crosspage_review_queue"]) == 1
    assert len(result["crosspage_pair_universe"]) == 4


def test_pair_universe_budget_truncation_is_explicit():
    # Once the source's comparison universe is incomplete, scoring must fail.
    n,c=reports()
    n["crosspage_pair_universe_count"] = 11
    n["crosspage_pair_universe_complete"] = False
    n["crosspage_pair_universe"] = []
    with pytest.raises(ValueError, match="Incomplete"):
        prepare_structural_gold(n,c,dataset_id="test")


def test_cli_prepare_and_no_overwrite(tmp_path, capsys):
    n,c=reports()
    np,cp,out=(tmp_path / p for p in ("native.json", "chain.json", "gold.json"))
    np.write_text(json.dumps(n)); cp.write_text(json.dumps(c))
    command = ["--prepare", "--native-report", str(np), "--chain-report", str(cp),
               "--dataset-id", "test", "--confirm-local-review", "--out", str(out)]
    assert cli.main(command) == 0
    assert out.exists()
    assert all("text" not in r for r in json.loads(out.read_text())["sources"])
    with pytest.raises(SystemExit) as e:
        cli.main(command)
    assert e.value.code == 2


def test_cli_evaluate_without_v3_does_not_pass_approval(tmp_path):
    n,c=reports()
    np,cp,gp,out=(tmp_path / x for x in ("n.json", "c.json", "gold.json", "scored.json"))
    np.write_text(json.dumps(n)); cp.write_text(json.dumps(c)); gp.write_text(json.dumps(reviewed()))
    result=cli.main(["--evaluate", "--native-report", str(np), "--chain-report", str(cp),
                     "--gold", str(gp), "--out", str(out), "--confirm-local-review",
                     "--min-source-cases", "1", "--min-crosspage-pairs", "1",
                     "--min-literal-f1", "0", "--min-section-proxy-f1", "0",
                     "--min-crosspage-f1", "0"])
    assert result == 2
    score=json.loads(out.read_text())
    assert score["measured_human_gold_gates_passed"] is False
    assert score["stage1_production_acceptance"] == "NOT_ESTABLISHED_BY_THIS_TOOL"
    assert "REQUIREMENT_SCOPE_HOLDOUT_MISSING_OR_BELOW_THRESHOLD" in score["reasons"]
