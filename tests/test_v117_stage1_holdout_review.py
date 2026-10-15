"""Synthetic only: v3 holdout, source identity, independent review declarations."""
from __future__ import annotations

import json
from pathlib import Path
import hashlib

import pytest

from app.engineering_intelligence.stage1_evaluation_v3 import (
    GOLD_SCHEMA_V3, case_label_digest, case_source_fingerprint,
    evaluate_gold_v3_cases,
)
from scripts.evaluate_stage1_gold import main as eval_main
from scripts.prepare_stage1_gold_v3 import build_unlabelled_v3, main as prep_main
from scripts.inspect_stage1_gold_v3_digests import inspect_label_digests


def _source(text: str, *, cid: int, project="21MM", source_kind="DOCX_TEXT", split="holdout", tags=None):
    return {"project": project, "document_id": "synthetic-document", "content_id": cid,
            "section_id": "4.1", "text": text, "source_kind": source_kind,
            "split": split, "complexity_tags": tags or ["NORMAL"]}


def _verified(row: dict, *, has_requirement=True, reviewer="quality-lead"):
    text = row["text"]
    row.update({"annotation_state": "VERIFIED",
        "expected_requirement_spans": [[0, len(text)]] if has_requirement else [],
        "scope_annotations": ([{"requirement_span": [0, len(text)],
                                "status": "ACTION_ONLY", "action_span": [0, len(text)]}]
                               if has_requirement else []),
        "independent_review": {"annotator_id": "engineer-a",
                               "reviewer_id": reviewer, "review_note": "Inspected original text against source",},
    })
    row["independent_review"]["reviewed_label_sha256"] = case_label_digest(row)
    return row


def _data(*rows):
    src = {"schema": "stage1-source-items-v1", "cases": list(rows)}
    result = build_unlabelled_v3(src, dataset_id="local-test")
    return result


def _set_seal(data):
    data["dataset"]["source_fingerprint"] = case_source_fingerprint(data["cases"])


def test_v3_scoring_uses_only_verified_holdout_and_reports_slices():
    data = _data(_source("ECU shall notify.", cid=1),
                 _source("No normative requirement here.", cid=2, source_kind="PDF_TABLE", tags=["NO_REQUIREMENT"]),
                 _source("ECU shall save.", cid=3, split="development"))
    _verified(data["cases"][0]); _verified(data["cases"][1], has_requirement=False)
    _verified(data["cases"][2])
    result = evaluate_gold_v3_cases(data)
    assert result.holdout_cases == 2 and result.total_cases == 3
    assert result.negative_holdout_cases == 1
    assert result.overall.requirement_metrics.f1 == 1.0
    assert result.by_source_kind["DOCX_TEXT"].requirement_f1 == 1.0
    assert result.by_source_kind["PDF_TABLE"].requirement_f1 is None
    assert result.by_source_kind["PDF_TABLE"].status == "NO_POSITIVE_GOLD"
    assert result.declared_independent_review and not result.review_identity_verified
    assert 'ECU shall' not in json.dumps(result, default=str)


def test_v3_scoring_project_identity_no_collision():
    data = _data(_source("ECU shall notify.", cid=1, project="21MM"),
                 _source("ECU shall notify.", cid=1, project="24MM"))
    for row in data["cases"]: _verified(row)
    result = evaluate_gold_v3_cases(data)
    assert result.by_project["21MM"].requirement_f1 == 1.0
    assert result.by_project["24MM"].requirement_f1 == 1.0


@pytest.mark.parametrize("modify,pattern", [
    (lambda d: d["cases"][0].update(annotation_state="PENDING"), "incomplete"),
    (lambda d: d["cases"][0]["independent_review"].update(reviewer_id="engineer-a"), "different"),
    (lambda d: d["cases"][0]["independent_review"].update(reviewer_id=""), "reviewer_id"),
    (lambda d: d["cases"][0]["independent_review"].update(reviewed_label_sha256="0"*64), "digest drift"),
    (lambda d: d["cases"][0].update(text="Modified source"), "SHA-256 drift"),
    (lambda d: d["cases"][0].update(source_kind="PDF_TABLE"), "fingerprint drift"),
    (lambda d: d["cases"][0].update(split="development"), "fingerprint drift"),
    (lambda d: d["cases"][0].update(expected_requirement_spans=[]), "digest drift"),
    (lambda d: d["cases"][0].update(project="COMMON"), "fingerprint drift"),
])
def test_v3_fails_closed_when_annotation_or_source_changes(modify,pattern):
    data = _data(_source("ECU shall notify.", cid=1))
    _verified(data["cases"][0]);modify(data)
    with pytest.raises(ValueError, match=pattern):evaluate_gold_v3_cases(data)


def test_v3_rejects_unlabelled_positive_slices_and_incomplete_labels():
    data = _data(_source("ECU shall notify.", cid=1))
    with pytest.raises(ValueError, match="incomplete"):
        evaluate_gold_v3_cases(data)
    _verified(data["cases"][0]);data["cases"][0]["scope_annotations"] = []
    data["cases"][0]["independent_review"]["reviewed_label_sha256"] = case_label_digest(data["cases"][0])
    with pytest.raises(ValueError, match="Every positive"):
        evaluate_gold_v3_cases(data)


def test_v3_no_positive_holdout_cannot_claim_good_accuracy():
    data = _data(_source("Informational text only.", cid=1))
    _verified(data["cases"][0], has_requirement=False)
    with pytest.raises(ValueError, match="no positive"):
        evaluate_gold_v3_cases(data)


def test_v3_template_and_digest_inspector_never_approve(tmp_path:Path):
    original = _source("ECU shall notify.", cid=1)
    src = tmp_path / "source.json"
    src.write_text(json.dumps({"schema":"stage1-source-items-v1","cases":[original]}), encoding="utf-8")
    out=tmp_path/"private_gold.json"
    with pytest.raises(SystemExit):
        prep_main(["--source-json",str(src),"--out",str(out),"--dataset-id","unit"])
    assert prep_main(["--source-json",str(src),"--out",str(out),"--dataset-id","unit",
                      "--confirm-local-sensitive-copy"]) == 0
    data=json.loads(out.read_text(encoding="utf-8"))
    assert data["cases"][0]["annotation_state"] == "PENDING"
    assert inspect_label_digests(data) == []
    data["cases"][0]["annotation_state"]="READY_FOR_REVIEW"
    data["cases"][0]["expected_requirement_spans"]=[[0,len(original["text"])]]
    data["cases"][0]["scope_annotations"]=[{"requirement_span":[0,len(original["text"])],
                                             "status":"ACTION_ONLY","action_span":[0,len(original["text"]) ]}]
    assert len(inspect_label_digests(data)) == 1
    assert data["cases"][0]["annotation_state"] == "READY_FOR_REVIEW"
    with pytest.raises(ValueError, match="incomplete"):
        evaluate_gold_v3_cases(data)
    with pytest.raises(FileExistsError):
        prep_main(["--source-json",str(src),"--out",str(out),"--dataset-id","unit",
                   "--confirm-local-sensitive-copy"])


def test_v3_cli_gates_on_slice_coverage_and_case_minimums(tmp_path:Path,capsys):
    data=_data(_source("ECU shall notify.",cid=1),
               _source("No normative instruction.",cid=2,source_kind="PDF_TABLE",tags=["NO_REQUIREMENT"]))
    _verified(data["cases"][0]);_verified(data["cases"][1],has_requirement=False)
    out=tmp_path/"gold.json";out.write_text(json.dumps(data),encoding="utf-8")
    assert eval_main(["--gold",str(out),"--min-holdout-cases","2", "--min-negative-cases","1",
                      "--require-source-kind","DOCX_TEXT","--min-source-kind-f1","0.90"]) == 0
    info=json.loads(capsys.readouterr().out)
    assert info["evaluation_split"] == "holdout" and info["acceptance_passed"]
    assert not info["metrics"]["review_identity_verified"]
    assert eval_main(["--gold",str(out),"--min-holdout-cases","3"]) == 2
    assert not json.loads(capsys.readouterr().out)["acceptance_passed"]
    assert eval_main(["--gold",str(out),"--require-source-kind","PDF_TABLE",
                      "--min-source-kind-f1","0.80"]) == 2
    assert not json.loads(capsys.readouterr().out)["acceptance_passed"]


def test_v3_per_role_metrics_and_redacted_failure_coordinates():
    text = 'Unless region is NA, ECU shall save the profile.'
    data = _data(_source(text, cid=1, tags=['EXCEPTION']))
    _verified(data['cases'][0])
    # Manual evaluator marks action-only with source range different from the
    # deterministic exception candidate. This is a synthetic disagreement.
    result = evaluate_gold_v3_cases(data)
    assert result.by_scope_role['exception'].labelled == 0
    assert result.by_scope_role['exception'].f1 is None
    assert result.by_scope_role['action'].labelled == 1
    assert len(result.redacted_error_cases) >= 1
    first = result.redacted_error_cases[0]
    assert len(first.source_identity_sha256_prefix) == 20
    assert first.project == '21MM'
    assert 'Unless' not in json.dumps(result, default=str)
    assert 'synthetic-document' not in json.dumps(result, default=str)


def test_v3_error_cases_are_bounded():
    cases = [_source('ECU shall notify.', cid=1)]
    cases.extend(_source('ECU shall notify.', cid=i, tags=['NO_REQUIREMENT']) for i in range(2, 70))
    data = _data(*cases)
    _verified(data['cases'][0])
    for row in data['cases'][1:]:
        _verified(row, has_requirement=False)
    result = evaluate_gold_v3_cases(data)
    assert len(result.redacted_error_cases) == 40
    assert result.truncated_error_cases == 28


def test_v3_legacy_v2_still_supported(tmp_path: Path, capsys):
    from app.engineering_intelligence.stage1_evaluation import GOLD_SCHEMA_V2
    case = {"document_id": "legacy", "content_id": 1, "section_id": "4",
            "text": "ECU shall notify.",
            "source_sha256": hashlib.sha256(b"ECU shall notify.").hexdigest(),
            "expected_requirement_spans": [[0, 17]],
            "scope_annotations": [{"requirement_span": [0, 17],
                                   "status": "ACTION_ONLY", "action_span": [0, 17]}]}
    out = tmp_path / 'v2.json'
    out.write_text(json.dumps({'schema': GOLD_SCHEMA_V2, 'cases': [case]}), encoding='utf-8')
    assert eval_main(['--gold', str(out)]) == 0
    stats = json.loads(capsys.readouterr().out)
    assert stats['evaluation_split'] == 'unspecified_legacy'


def test_label_digest_covers_slice_provenance_too():
    data = _data(_source('ECU shall notify.', cid=1))
    _verified(data['cases'][0])
    data['cases'][0]['complexity_tags'] = ['EXCEPTION']
    _set_seal(data)  # Even a recalculated source manifest cannot forge the review digest.
    with pytest.raises(ValueError, match='digest drift'):
        evaluate_gold_v3_cases(data)


def test_v3_cli_refuses_ignored_quality_gates(tmp_path: Path):
    row = _source('ECU shall notify.', cid=1)
    data = _data(row)
    _verified(data['cases'][0])
    out = tmp_path / 'v3.json'
    out.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(SystemExit, match='2'):
        eval_main(['--gold',str(out),'--min-source-kind-f1','0.95'])
    from app.engineering_intelligence.stage1_evaluation import GOLD_SCHEMA_V2
    legacy = tmp_path / 'legacy.json'
    legacy.write_text(json.dumps({'schema':GOLD_SCHEMA_V2,'cases':[
        {k:data['cases'][0][k] for k in ('document_id','content_id','section_id',
             'text','source_sha256','expected_requirement_spans','scope_annotations')}
    ]}), encoding='utf-8')
    with pytest.raises(SystemExit, match='2'):
        eval_main(['--gold',str(legacy),'--require-source-kind','DOCX_TABLE'])


def test_v3_cli_enforces_complexity_and_exception_role(tmp_path: Path, capsys):
    data = _data(_source('ECU shall notify.', cid=1, tags=['NORMAL']))
    _verified(data['cases'][0])
    gold = tmp_path / 'v3.json'
    gold.write_text(json.dumps(data), encoding='utf-8')
    assert eval_main(['--gold',str(gold), '--require-complexity-tag','CROSS_PAGE']) == 2
    assert not json.loads(capsys.readouterr().out)['coverage_gate_passed']
    assert eval_main(['--gold',str(gold), '--min-exception-f1','0.80']) == 2
    obj = json.loads(capsys.readouterr().out)
    assert obj['metrics']['by_scope_role']['exception']['f1'] is None
    assert not obj['role_gate_passed']
    assert eval_main(['--gold',str(gold), '--require-complexity-tag','NORMAL',
                      '--min-tag-cases','1','--min-tag-f1','0.9']) == 0
    assert json.loads(capsys.readouterr().out)['acceptance_passed']
