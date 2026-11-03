"""Golden dataset contract prevents synthetic fixtures being called real acceptance."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.evaluate_answer_gate import DatasetError, evaluate_case, evaluate_jsonl

ROOT = Path(__file__).resolve().parents[1]


def test_synthetic_contract_fixture_is_explicitly_not_semantic_proof():
    result = evaluate_jsonl(ROOT / 'examples' / 'answer_gate_synthetic_v148.jsonl')
    assert result['total'] == 6 and result['matched'] == 6
    assert result['human_reviewed_real_cases'] == 0
    assert result['production_acceptance'] == 'NOT_ASSESSED'
    assert result['proves_semantic_entailment'] is False


def test_fake_real_golden_without_independent_review_is_rejected():
    row = dict(case_id='fake-real', dataset_type='HUMAN_REVIEWED_REAL',
               answer='500ms。[1]', evidence=[dict(id='1', text='500ms')],
               expected_accept=True)
    with pytest.raises(DatasetError, match='reviewer and source SHA256'):
        evaluate_case(row)


def test_duplicate_citation_identifier_is_rejected():
    row = dict(case_id='dup', dataset_type='SYNTHETIC_CONTRACT',
               answer='500ms。[1]', evidence=[dict(id='1', text='500ms'), dict(id='1', text='500ms')],
               expected_accept=True)
    with pytest.raises(DatasetError, match='duplicate'):
        evaluate_case(row)


def test_dataset_duplicate_case_id_is_rejected(tmp_path: Path):
    case = dict(case_id='dup', dataset_type='SYNTHETIC_CONTRACT',
                answer='500ms。[1]', evidence=[dict(id='1', text='500ms')], expected_accept=True)
    path = tmp_path / 'sample.jsonl'
    path.write_text(json.dumps(case)+'\n'+json.dumps(case)+'\n', encoding='utf-8')
    with pytest.raises(DatasetError, match='duplicate case_id'):
        evaluate_jsonl(path)
