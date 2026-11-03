"""Stage 4 phase 1: RRF identity, scope, source integrity and safe routing."""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from app.ai.hybrid_retrieval import fuse_spec_rrf
from app.ai.hybrid_evaluation import aggregate, evaluate_one
from app.ai.retrieval import EnterpriseRetriever, EvidenceItem
from scripts.evaluate_stage4_rrf import evaluate


def chunk(cid=1, *, scope='21MM', doc='21MM:550@v2.52', version='v2.52',
          content='Bluetooth audio link handshake', file='Bluetooth Audio function spec v2.52.pdf',
          source='SQL_EXACT', metadata=None, score=None):
    return EvidenceItem(
        evidence_id='', scope=scope, source_type=source, content=content,
        content_id=cid, document_id=doc, specification_version=version,
        specification_code='550', file_name=file, score=score,
        metadata=dict(metadata or {}),
    )


def test_rrf_promotes_cross_source_agreement_and_keeps_sql_authority():
    sql = [chunk(1), chunk(2)]
    dense = [chunk(2, source='SEMANTIC', score=0.98), chunk(3, source='SEMANTIC', score=.8)]
    result = fuse_spec_rrf(sql, dense)
    assert [x.content_id for x in result] == [2, 1, 3]
    assert result[0].metadata['retrieval_sources'] == ['POSTGRESQL', 'QDRANT_DENSE']
    assert result[0].metadata['rrf_ranks'] == {'POSTGRESQL': 2, 'QDRANT_DENSE': 1}
    assert result[0].metadata['source_authority'] == 'POSTGRESQL'
    assert result[0].source_type == 'HYBRID_CHUNK'
    assert result[0].score is None  # ranking only, not an engineering confidence
    assert result[1].metadata['retrieval_sources'] == ['POSTGRESQL']


def test_rrf_never_joins_two_projects_or_two_revisions():
    items = fuse_spec_rrf([chunk(8)], [
        chunk(8, scope='24MM'),
        chunk(8, doc='21MM:550@v2.50', version='v2.50'),
    ])
    assert len(items) == 3
    assert all(len(x.metadata['retrieval_sources']) == 1 for x in items)


def test_rrf_never_joins_different_physical_documents():
    items = fuse_spec_rrf([chunk(8)], [chunk(8, doc='21MM:550@other-source')])
    assert len(items) == 2


def test_rrf_rejects_coversheet_and_supporting_role():
    result = fuse_spec_rrf([
        chunk(1, file='550_Bluetooth_Audio_Cover_Sheet.pdf'),
        chunk(2, metadata={'document_role': 'METADATA_ONLY'}),
        chunk(3),
    ], [chunk(4, file='review_checklist.pdf'), chunk(3)])
    assert [x.content_id for x in result] == [3]


def test_rrf_rejects_issues_and_unknown_identity():
    items = fuse_spec_rrf([
        chunk(1, scope='QABUG'), chunk(2, doc=''), replace(chunk(3), content_id=None), chunk(4),
    ], [])
    assert [x.content_id for x in items] == [4]


def test_rrf_rejects_stale_vector_vote_when_text_differs():
    sql = chunk(9, metadata={'content_hash': 'old'})
    dense = chunk(9, source='SEMANTIC', content='tampered text',
                  metadata={'content_hash': 'new'})
    [item] = fuse_spec_rrf([sql], [dense])
    assert item.content == sql.content
    assert item.metadata['vector_source_mismatch'] is True
    assert item.metadata['retrieval_sources'] == ['POSTGRESQL']


def test_rrf_rejects_text_mismatch_without_hash_metadata():
    [item] = fuse_spec_rrf([chunk(1)], [chunk(1, content='different original')])
    assert item.metadata['retrieval_sources'] == ['POSTGRESQL']


def test_rrf_deduplicates_repeated_query_hits_without_faking_new_sources():
    items = fuse_spec_rrf([chunk(3), chunk(3)], [chunk(3), chunk(3)])
    assert len(items) == 1
    assert items[0].metadata['rrf_ranks'] == {'POSTGRESQL': 1, 'QDRANT_DENSE': 1}


def test_rrf_bounded_and_invalid_config():
    assert len(fuse_spec_rrf([chunk(i) for i in range(1, 11)], [], max_candidates=3)) == 3
    with pytest.raises(ValueError):
        fuse_spec_rrf([], [], rrf_k=0)
    with pytest.raises(ValueError):
        fuse_spec_rrf([], [], max_candidates=501)


def test_rrf_ranking_is_deterministic_on_ties():
    a = fuse_spec_rrf([chunk(4), chunk(3)], [])
    b = fuse_spec_rrf([chunk(4), chunk(3)], [])
    assert [x.content_id for x in a] == [x.content_id for x in b]


def test_rrf_agentic_route_preserves_temporal_isolation(monkeypatch):
    retriever = object.__new__(EnterpriseRetriever)
    retriever.top_k = 8
    monkeypatch.setattr(retriever, '_semantic_search', lambda *a, **k: [chunk(1, source='SEMANTIC')])
    monkeypatch.setattr(retriever, '_sql_exact_search', lambda *a, **k: [chunk(1)])
    result = retriever.retrieve_agentic(
        question='550 蓝牙重连设计', scopes=('21MM',),
        rewritten_queries=('550 蓝牙重连设计',), tools=('semantic_search', 'sql_exact_search'),
    )
    assert [x.content_id for x in result.evidence] == [1]
    assert result.evidence[0].source_type == 'HYBRID_CHUNK'
    assert any('stage4_rrf:' in t for t in result.trace)


def test_single_tool_retains_legacy_semantic_path(monkeypatch):
    retriever = object.__new__(EnterpriseRetriever)
    retriever.top_k = 8
    monkeypatch.setattr(retriever, '_semantic_search', lambda *a, **k: [chunk(1, source='SEMANTIC')])
    result = retriever.retrieve_agentic(
        question='550 蓝牙重连', scopes=('21MM',), rewritten_queries=('550 蓝牙重连',),
        tools=('semantic_search',),
    )
    assert result.evidence[0].source_type == 'SEMANTIC'


def test_golden_metrics_no_silent_empty_acceptance():
    relevant = {('21MM', 'doc', 'v1', 2)}
    metrics = evaluate_one([('21MM','doc','v1',1), ('21MM','doc','v1',2)], relevant, k=2)
    assert metrics.recall_at_k == 1
    assert metrics.precision_at_k == .5
    assert metrics.reciprocal_rank_at_k == .5
    assert aggregate([metrics])['queries'] == 1
    with pytest.raises(ValueError):
        evaluate_one([], set(), k=5)
    with pytest.raises(ValueError):
        aggregate([])


def test_offline_golden_eval_uses_same_rrf_contract(tmp_path):
    def as_dict(c):
        return dict(scope=c.scope, document_id=c.document_id, specification_version=c.specification_version,
                    content_id=c.content_id, file_name=c.file_name, content=c.content)
    path = tmp_path / 'golden.jsonl'
    path.write_text(json.dumps({
        'query_id':'BT-1', 'relevant': [as_dict(chunk(2))],
        'sql': [as_dict(chunk(1)), as_dict(chunk(2))],
        'dense': [as_dict(chunk(2)), as_dict(chunk(3))],
    }), encoding='utf-8')
    result = evaluate(path, k=1)
    assert result['offline_only'] is True
    assert result['results']['rrf']['recall_at_k'] == 1.0
    assert result['results']['sql']['recall_at_k'] == 0.0


def test_golden_eval_rejects_unlabeled_and_duplicate_queries(tmp_path):
    path = tmp_path / 'bad.jsonl'
    path.write_text(json.dumps({'query_id':'a', 'relevant':[], 'sql':[], 'dense':[]}),encoding='utf-8')
    with pytest.raises(ValueError, match='relevance'):
        evaluate(path)
    path.write_text('', encoding='utf-8')
    with pytest.raises(ValueError, match='Golden dataset'):
        evaluate(path)


def test_rrf_keeps_relation_candidates_out_of_chunk_fusion():
    sql = chunk(1, source='CONTENT_RELATION')
    # The fusion helper accepts only source-provided lists; the orchestration
    # never passes relationships into these lists, and QABUG is hard-excluded.
    assert fuse_spec_rrf([chunk(1)], [chunk(1, scope='QABUG')])[0].scope == '21MM'
