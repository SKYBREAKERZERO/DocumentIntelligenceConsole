"""No live model, network or database required for the v1.45 rejection gates."""
from __future__ import annotations

import hashlib
import sys
import types
from types import SimpleNamespace

import pytest

from app.ai.hybrid_retrieval import fuse_spec_rrf
from app.ai.retrieval import EnterpriseRetriever
from app.ai.stage4_sparse import BGEM3SparseEncoder, canonical_sparse, sparse_collection
from scripts.build_stage4_sparse_index import fetch_bounded_sources, make_sparse_points
from tests.test_v144_stage4_rrf import chunk


def test_native_sparse_token_ids_are_sorted_without_token_hashing():
    assert canonical_sparse({'99': .5, 12: .2}) == ([12, 99], [.2, .5])


@pytest.mark.parametrize('weights', [
    {}, {'abc': .1}, {-1: .1}, {2: -1}, {2: float('nan')},
    {2: float('inf')}, {2: 0}, {2**32: .1}, {1: True},
    {'01': .2, 1: .3}, {str(k): 1 for k in range(2050)},
])
def test_sparse_encoder_rejects_invalid_token_weights(weights):
    with pytest.raises(ValueError):
        canonical_sparse(weights)


def test_sparse_collection_is_isolated_and_name_bound():
    assert sparse_collection('spec_current') == 'spec_current__bge_m3_sparse_v1'
    with pytest.raises(ValueError):
        sparse_collection('../other')


def test_encoder_uses_native_flagembedding_without_loading_in_constructor():
    encoder = BGEM3SparseEncoder()
    assert encoder._model is None
    class FakeModel:
        def __init__(self, name, **kwargs):
            assert name == 'BAAI/bge-m3'
            assert kwargs['devices'] == 'cpu'
            assert kwargs['use_fp16'] is False
        def encode(self, texts, **kwargs):
            assert kwargs['return_sparse'] and not kwargs['return_dense']
            return {'lexical_weights': [{'32': .2, '5': .8} for _ in texts]}
    old = sys.modules.get('FlagEmbedding')
    sys.modules['FlagEmbedding'] = types.SimpleNamespace(BGEM3FlagModel=FakeModel)
    try:
        result = encoder.encode(['audio requirement'])
        assert result[0].indices == [5, 32]
        assert result[0].values == [.8, .2]
    finally:
        if old is None:
            sys.modules.pop('FlagEmbedding', None)
        else:
            sys.modules['FlagEmbedding'] = old


def test_rrf_three_independent_ranks_promote_verified_three_way_hit():
    sql = [chunk(1), chunk(2)]
    dense = [chunk(2, source='SEMANTIC'), chunk(3, source='SEMANTIC')]
    sparse = [chunk(2, source='QDRANT_SPARSE'), chunk(4, source='QDRANT_SPARSE')]
    ranked = fuse_spec_rrf(sql, dense, sparse_hits=sparse)
    assert ranked[0].content_id == 2
    assert ranked[0].metadata['rrf_ranks'] == {
        'POSTGRESQL': 2, 'QDRANT_DENSE': 1, 'QDRANT_SPARSE': 1,
    }
    assert ranked[0].metadata['fusion_method'] == 'RRF_V2'
    assert ranked[0].score is None


def test_rrf_drops_stale_sparse_hash_vote_but_preserves_other_sources():
    sql = chunk(1, metadata={'content_hash':'a'})
    dense = chunk(1, metadata={'content_hash':'a'})
    sparse = chunk(1, metadata={'content_hash':'b'})
    [item] = fuse_spec_rrf([sql], [dense], sparse_hits=[sparse])
    assert item.metadata['retrieval_sources'] == ['POSTGRESQL','QDRANT_DENSE']
    assert item.metadata['vector_source_mismatch'] is True


def test_rrf_no_sql_conflicting_dense_sparse_does_not_claim_agreement():
    [item] = fuse_spec_rrf([], [chunk(1)], sparse_hits=[chunk(1, content='altered')])
    assert item.metadata['retrieval_sources'] == ['QDRANT_DENSE']


def test_rrf_coversheet_never_gets_sparse_vote():
    assert not fuse_spec_rrf([], [], sparse_hits=[chunk(1, file='Cover Sheet.pdf')])


def test_sidecar_batch_points_exclude_original_content():
    row = {'content_id':9,'document_id':'d9','file_name':'550 actual spec.pdf',
           'specification_code':'550','specification_version':'v2.52','content_hash':'abc',
           'content':'actual technical body'}
    [pt] = make_sparse_points([row], [SimpleNamespace(indices=[9], values=[1.])],project='21MM')
    assert pt['id'] == 9 and 'content' not in pt['payload']
    assert pt['payload']['content_hash'] == 'abc'


class FakeCursor:
    def __init__(self, rows): self.rows = rows; self.args = None
    def __enter__(self): return self
    def __exit__(self,*_): return False
    def execute(self,sql,args): self.args = args; assert 'JOIN public.documents' in sql
    def fetchall(self): return self.rows
class FakeConn:
    def __init__(self,rows): self.cur=FakeCursor(rows)
    def cursor(self): return self.cur


def test_sidecar_scan_fails_closed_on_truncation_and_coversheet():
    body = {'content_id':1,'document_id':'d1','file_name':'550 function spec.pdf',
            'content':'hello','content_hash':'hash'}
    with pytest.raises(ValueError, match='exceeds'):
        fetch_bounded_sources(FakeConn([body,dict(body,content_id=2)]),project='21MM',max_chunks=1)
    with pytest.raises(ValueError, match='non-body'):
        fetch_bounded_sources(FakeConn([dict(body,file_name='550 Cover Sheet.pdf')]),project='21MM',max_chunks=5)


def test_sparse_online_rejects_hash_and_version_drift(monkeypatch):
    # Qdrant stub is injected without installing its optional client dependency.
    fake_models = types.ModuleType('qdrant_client.models')
    fake_models.SparseVector = lambda **kw: kw
    monkeypatch.setitem(sys.modules, 'qdrant_client.models', fake_models)
    fake = object.__new__(EnterpriseRetriever)
    fake.top_k = 4
    fake.collections = {'21MM': 'legacy_dense'}
    fake._sparse_encoder = SimpleNamespace(encode=lambda texts:[SimpleNamespace(indices=[1], values=[1.])])
    payload = {'content_id':7,'project_code':'21MM','index_scope':'CURRENT',
               'sparse_model':'BAAI/bge-m3','sparse_vector_name':'bge_m3_lexical_v1',
               'document_id':'d7','file_name':'550 function spec.pdf',
               'version':'v2.52','spec_code':'550','content_hash':hashlib.sha256(b'authored requirement').hexdigest()}
    point = SimpleNamespace(id=7, payload=payload, score=7.5)
    fake_client = SimpleNamespace(query_points=lambda **kw: SimpleNamespace(points=[point]))
    fake._get_qdrant_client = lambda: fake_client
    fake._collection_exists = lambda *args: True
    fake._fetch_chunks = lambda *_: [{
        'content_id':7,'document_id':'d7','file_name':'550 function spec.pdf',
        'specification_version':'v2.52','specification_code':'550','content_hash':'wrong',
        'content':'authored requirement',
    }]
    assert fake._sparse_search('audio',('21MM',)) == []
    fake._fetch_chunks = lambda *_: [{
        'content_id':7,'document_id':'d7','file_name':'550 function spec.pdf',
        'specification_version':'v2.52','specification_code':'550','project_code':'21MM','content_hash':payload['content_hash'],
        'content':'authored requirement',
    }]
    [hit] = fake._sparse_search('audio',('21MM',))
    assert hit.content == 'authored requirement'
    assert hit.metadata['hydration']=='POSTGRES_HASH_AND_IDENTITY_VERIFIED'
    point.payload = dict(payload, version='v9')
    assert fake._sparse_search('audio',('21MM',)) == []


def test_sparse_agent_path_is_opt_in_and_keeps_relation_separate(monkeypatch):
    retriever = object.__new__(EnterpriseRetriever)
    retriever.top_k = 8
    retriever.sparse_enabled = True
    retriever._sparse_search = lambda *a,**kw:[chunk(2,source='QDRANT_SPARSE')]
    retriever._semantic_search = lambda *a,**kw:[chunk(2,source='SEMANTIC')]
    retriever._sql_exact_search = lambda *a,**kw:[chunk(2)]
    result = retriever.retrieve_agentic(question='550 蓝牙握手',scopes=('21MM',),
        rewritten_queries=('550 蓝牙握手',),tools=('semantic_search','sql_exact_search'))
    assert result.evidence[0].metadata['retrieval_sources']==[
        'POSTGRESQL','QDRANT_DENSE','QDRANT_SPARSE']
    assert any('stage4_sparse:' in entry for entry in result.trace)


def test_sparse_error_degrades_to_dense_sql(monkeypatch):
    retriever = object.__new__(EnterpriseRetriever)
    retriever.top_k = 8
    retriever.sparse_enabled = True
    retriever._sparse_search = lambda *a,**kw: (_ for _ in ()).throw(RuntimeError('offline'))
    retriever._semantic_search = lambda *a,**kw:[chunk(2)]
    retriever._sql_exact_search = lambda *a,**kw:[chunk(2)]
    result = retriever.retrieve_agentic(question='550 audio',scopes=('21MM',),
        rewritten_queries=('550 audio',),tools=('semantic_search','sql_exact_search'))
    assert result.evidence[0].metadata['retrieval_sources']==['POSTGRESQL','QDRANT_DENSE']
    assert any('Sparse unavailable' in w for w in result.warnings)


def test_golden_three_way_metrics_are_separate_from_baseline(tmp_path):
    from scripts.evaluate_stage4_rrf import evaluate
    import json
    def d(cid):
        return {'scope':'21MM','document_id':'d','specification_version':'v1',
                'content_id':cid,'file_name':'550 spec.pdf','content':f'original {cid}'}
    path = tmp_path / 'golden.jsonl'
    path.write_text(json.dumps({'query_id':'q1', 'relevant':[d(3)],
        'sql':[d(1),d(3)],'dense':[d(1),d(3)],'sparse':[d(3),d(4)]}),encoding='utf-8')
    res = evaluate(path,k=1)
    assert res['results']['rrf_three_way']['recall_at_k'] == 1
    assert res['results']['rrf']['recall_at_k'] == 0
    assert res['results']['sparse']['recall_at_k'] == 1


def test_three_way_quality_gate_cannot_silently_use_missing_sparse_data(tmp_path, monkeypatch):
    from scripts.evaluate_stage4_rrf import main
    import json
    path = tmp_path / 'dataset.jsonl'
    d = {'scope':'21MM','document_id':'d','specification_version':'v1',
         'content_id':1,'file_name':'550 spec.pdf','content':'true body'}
    path.write_text(json.dumps({'query_id':'q','relevant':[d], 'sql':[d], 'dense':[d]}),encoding='utf-8')
    monkeypatch.setattr(sys,'argv',['evaluate_stage4_rrf.py',str(path),'--ranker','rrf_three_way'])
    assert main() == 2


def test_sparse_rejects_spoofed_qdrant_point_id(monkeypatch):
    fake_models = types.ModuleType('qdrant_client.models')
    fake_models.SparseVector = lambda **kw: kw
    monkeypatch.setitem(sys.modules, 'qdrant_client.models', fake_models)
    ret = object.__new__(EnterpriseRetriever)
    ret.top_k = 4
    ret.collections = {'21MM':'original'}
    ret._sparse_encoder = SimpleNamespace(encode=lambda _: [SimpleNamespace(indices=[1],values=[.3])])
    point = SimpleNamespace(id=44, payload={
        'content_id':7,'project_code':'21MM', 'index_scope':'CURRENT',
        'sparse_model':'BAAI/bge-m3','sparse_vector_name':'bge_m3_lexical_v1',
        'document_id':'d7','file_name':'550 spec.pdf','content_hash':'abc',
    },score=2.0)
    ret._get_qdrant_client = lambda: SimpleNamespace(
        query_points=lambda **kwargs: SimpleNamespace(points=[point]))
    ret._collection_exists = lambda *args: True
    ret._fetch_chunks = lambda *args: (_ for _ in ()).throw(AssertionError('Must not hydrate spoofed ID'))
    assert ret._sparse_search('hello',('21MM',)) == []
