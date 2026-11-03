"""Fail-closed Stage 4 opt-in CrossEncoder contract (no live model needed)."""
from __future__ import annotations

import json
import math
import os
from dataclasses import replace
from pathlib import Path

import pytest

from app.ai.retrieval import EnterpriseRetriever, EvidenceItem
from app.ai.stage4_reranker import RerankConfig, rerank_spec_chunks
from scripts.benchmark_stage4_reranker import benchmark, percentile


def chunk(n, *, scope='21MM', kind='HYBRID_CHUNK', file='550 function spec.pdf'):
    return EvidenceItem(evidence_id=str(n), scope=scope, source_type=kind,
        content=f'actual body requirement {n}', content_id=n, document_id='doc550',
        file_name=file, specification_version='v2.52',
        metadata={'ranking_only': True, 'evidence_role': 'PRIMARY_CONTENT',
                  'rrf_score': 0.04, 'fusion_method':'RRF_V2',
                  'retrieval_sources':['POSTGRESQL','QDRANT_DENSE']})


def cfg(**kw):
    return replace(RerankConfig(enabled=True), **kw)


def test_disabled_is_noop():
    source = [chunk(1), chunk(2)]
    result, state = rerank_spec_chunks('query', source, config=RerankConfig())
    assert result == source and state.state == 'DISABLED'


def test_rank_changes_but_source_and_rrf_are_unchanged():
    source = [chunk(1), chunk(2), chunk(3)]
    result, status = rerank_spec_chunks('audio', source, config=cfg(),
        score_fn=lambda pairs, conf: [1.0, 8.0, -4.0])
    assert status.state == 'APPLIED'
    assert [x.content_id for x in result] == [2, 1, 3]
    assert result[0].metadata['reranking_only'] is True
    assert result[0].metadata['rerank_original_rank'] == 2
    assert result[0].metadata['rrf_score'] == source[1].metadata['rrf_score']
    assert all(x.score is None for x in result)
    assert source[1].metadata.get('rerank_rank') is None


@pytest.mark.parametrize('scores', [[1.0], [float('nan'),0], [float('inf'),0], [True, 1], ['bad', 1]])
def test_invalid_scores_restore_rrf(scores):
    original = [chunk(1), chunk(2)]
    result, status = rerank_spec_chunks('q', original, config=cfg(),
        score_fn=lambda *_: scores)
    assert status.state == 'FAILED' and result == original


@pytest.mark.parametrize('item', [chunk(1,scope='QABUG'), chunk(1,kind='RELATION'),
                                   chunk(1,file='550 Coversheet.pdf'),
                                   replace(chunk(1),metadata={'ranking_only':False})])
def test_unsafe_input_never_reaches_model(item):
    result, status = rerank_spec_chunks('q', [chunk(2),item], config=cfg(),
        score_fn=lambda *_: pytest.fail('model must not run'))
    assert status.state == 'NOT_ELIGIBLE'


def test_dense_only_without_postgres_provenance_is_rejected():
    dense = replace(chunk(1), metadata=dict(chunk(1).metadata, retrieval_sources=['QDRANT_DENSE']))
    originals=[chunk(2),dense]
    result, state=rerank_spec_chunks('audio',originals,config=cfg(),
        score_fn=lambda *_:pytest.fail('not authoritative'))
    assert state.state == 'NOT_ELIGIBLE' and result == originals


def test_enforced_bounds_before_model_invocation():
    source = [chunk(i) for i in range(1, 17)]
    lengths = []
    def fake(pairs, conf):
        lengths.extend((len(pairs), len(pairs[0][0]), len(pairs[0][1])))
        return list(range(len(pairs)))
    result, status = rerank_spec_chunks('q'*1000, source, config=cfg(max_candidates=3,
        query_chars=128, text_chars=128), score_fn=fake)
    assert lengths == [3,128, len(source[0].content)]
    assert [x.content_id for x in result[3:]] == list(range(4,17))
    assert status.candidates == 3


def test_invalid_config_is_fail_closed():
    original = [chunk(1),chunk(2)]
    result, status = rerank_spec_chunks('hello',original,config=cfg(timeout_s=-1),
        score_fn=lambda *_:pytest.fail('no model'))
    assert status.state == 'FAILED' and result == original


def test_model_error_does_not_break_normal_retrieval(monkeypatch):
    monkeypatch.setenv('DIC_STAGE4_RERANK_ENABLED','1')
    import app.ai.stage4_reranker as mod
    def broken(*_a, **_kw):
        return [chunk(1),chunk(2)], mod.RerankStatus('TIMEOUT', 10000)
    monkeypatch.setattr(mod, 'rerank_spec_chunks', broken)
    retriever=object.__new__(EnterpriseRetriever)
    retriever.top_k=8
    retriever._semantic_search=lambda *a, **kw: [chunk(2)]
    retriever._sql_exact_search=lambda *a, **kw: [chunk(2),chunk(1)]
    result=retriever.retrieve_agentic(question='550 audio', scopes=('21MM',),
        rewritten_queries=('550 audio',),tools=('semantic_search','sql_exact_search'))
    assert any('TIMEOUT' in w for w in result.warnings)
    assert any('stage4_reranker:' in line for line in result.trace)


def test_percentile_linear_interpolation():
    assert percentile([1,2,3,4,5],95)==4.8
    with pytest.raises(ValueError): percentile([float('nan')],95)


def test_golden_benchmark_reports_measured_latency_and_rank_gain(tmp_path):
    def d(n):
        return {'scope':'21MM','document_id':'doc550','specification_version':'v2.52',
                'content_id':n,'file_name':'550 function spec.pdf','content':f'body {n}'}
    path=tmp_path/'golden.jsonl'
    row={'query_id':'q1','query':'550 audio', 'relevant':[d(2)],
         'sql':[d(1),d(2)], 'dense':[d(1),d(2)], 'sparse':[d(1),d(2)]}
    path.write_text(json.dumps(row)+'\n',encoding='utf8')
    report=benchmark(path, config=cfg(),k=1,score_fn=lambda p,c:[0,4])
    assert report['rrf']['recall_at_k']==0
    assert report['reranked']['recall_at_k']==1
    assert report['recall_delta']==1
    assert report['reranker_state']=='APPLIED_FOR_ALL'
    assert report['reranked_latency_ms']['p95'] >= 0


def test_golden_benchmark_refuses_missing_sparse(tmp_path):
    path=tmp_path/'bad.jsonl'
    path.write_text(json.dumps({'query_id':'x','query':'hello', 'relevant':[],
        'sql':[], 'dense':[], 'sparse':[]}),encoding='utf8')
    with pytest.raises(ValueError,match='truth'):
        benchmark(path,config=cfg(),score_fn=lambda *_:[])


def test_reranker_does_not_run_without_explicit_opt_in(monkeypatch):
    monkeypatch.delenv('DIC_STAGE4_RERANK_ENABLED',raising=False)
    assert RerankConfig.from_env().enabled is False


def test_local_model_directory_is_mandatory():
    from app.ai.stage4_reranker import score_in_isolated_process
    with pytest.raises(ValueError,match='local directory'):
        score_in_isolated_process([('a','b')],cfg(model_dir='/__missing_private_model__'))
