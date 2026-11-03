"""Production-regression cases: factual repository questions never ask a 4B model to hallucinate."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.ai.agent import FastIntentRouter
from app.ai.retrieval import EnterpriseRetriever, EvidenceItem, RetrievalResult
from app.ai.rag_service import EnterpriseRAGService
from app.ai.spec_facts import extra_spec_codes, lookup_keywords, requested_version, render_sql_facts


@pytest.mark.parametrize("question,route", [
    ("查询21MM Bluetooth Audio规格书已登记的相邻两个本体版本之间的变更。显示变更前后原文、章节、版本ID及关联影响；如果没有两个可信本体版本，请明确说明，不要使用Cover Sheet代替。", "SPEC_VERSION_FACTS"),
    ("查一下21MM 的 550 BT式样书的内容 按目录总结", "SPEC_OUTLINE"),
    ("你现在能查到哪些21MM式样书", "SPEC_CATALOG"),
    ("Bluetooth Audio function spec v2.52有吗", "SPEC_LOOKUP"),
    ("550 BT式样书有吗", "SPEC_LOOKUP"),
    ("21MM的550蓝牙是什么式样书", "SPEC_LOOKUP"),
    ("BUG-123 是否符合 550 规格", "CONFORMANCE_CHECK"),
    ("550 历史变更会影响 508 吗", "ENGINEERING_AGENT"),
])
def test_actual_user_queries_have_correct_route(question, route):
    assert FastIntentRouter.classify(question).route == route


def test_bt_expansion_is_a_search_term_not_a_fake_550_identity():
    assert 'Bluetooth' in lookup_keywords('查一下BT式样书')
    assert extra_spec_codes('查一下BT式样书') == ()
    assert extra_spec_codes('查一下550 BT式样书') == ('550',)
    assert requested_version('Bluetooth Audio function spec v2.52有吗') == '2.52'
    assert requested_version('2026年的21MM 550 BT式样') is None


@pytest.mark.parametrize('route', ['SPEC_LOOKUP', 'SPEC_CATALOG', 'SPEC_OUTLINE', 'SPEC_VERSION_FACTS'])
def test_factual_routes_never_call_ollama_or_enter_model_gate(route):
    service = EnterpriseRAGService.__new__(EnterpriseRAGService)
    service.prepare = Mock(return_value=([], RetrievalResult(
        evidence=(EvidenceItem(evidence_id='1', scope='21MM', source_type=route,
             content='已登记 550', metadata={'version_count': 1}),),
        resolved_scopes=('21MM',), route=route)))
    service.ollama = Mock()
    service._emit_stage = Mock()
    text = ''.join(service.answer_stream([{'role': 'user', 'content':'550 BT式样书有吗'}], model='qwen3:4b-instruct'))
    assert text
    assert '企业引用完整性校验' not in text
    service.ollama.chat_stream.assert_not_called()


def test_missing_primary_version_never_becomes_file_absence_or_cover_fallback():
    result = RetrievalResult(evidence=(), resolved_scopes=('21MM',), route='SPEC_LOOKUP')
    text = render_sql_facts(result, 'Bluetooth Audio function spec v2.52有吗')
    assert '未找到' in text and 'SPEC_BODY' in text and 'Cover Sheet' in text
    assert 'Windows 文件不存在' in text


def test_pg_error_not_rendered_as_empty_corpus():
    result = RetrievalResult(evidence=(), resolved_scopes=('21MM',), route='SPEC_LOOKUP', warnings=('OperationalError',))
    assert '查询未完成' in render_sql_facts(result,'550 BT式样书有吗')


def test_two_primary_versions_required_to_claim_a_comparison():
    result = RetrievalResult(evidence=(EvidenceItem(evidence_id='', scope='21MM',
        source_type='SPEC_VERSION_FACTS', content='', specification_code='550',
        metadata={'versions': ({'version_label':'v2.52','id':53,'document_id':'body52','file_name':'Bluetooth Audio function spec v2.52.pdf'},),
                  'adjacent':False, 'change_set_ready':False}),), resolved_scopes=('21MM',), route='SPEC_VERSION_FACTS')
    text = render_sql_facts(result, '查询相邻两个本体版本之间的变更')
    assert '没有两个可信' in text and 'Cover Sheet' in text
    assert 'READY 变更集' not in text


def test_unavailable_sections_cannot_be_hallucinated():
    item = EvidenceItem(evidence_id='', scope='21MM',source_type='SPEC_OUTLINE',content='',
                        file_name='Bluetooth Audio function spec v2.52.pdf',specification_code='550',
                        metadata={'version_id':53,'outline':()})
    text=render_sql_facts(RetrievalResult(evidence=(item,), resolved_scopes=('21MM',),route='SPEC_OUTLINE'),'550目录')
    assert '没有可验证的章节目录' in text


def test_spec_lookup_sql_checks_real_filename_and_binds_exact_version(monkeypatch):
    retriever = EnterpriseRetriever.__new__(EnterpriseRetriever)
    executed = []
    class Cursor:
        def execute(self, sql, params):
            executed.append((sql,params))
        def fetchall(self): return []
        def __enter__(self): return self
        def __exit__(self, *_): return False
    class Connection:
        def cursor(self): return Cursor()
        def __enter__(self): return self
        def __exit__(self,*_):return False
    retriever._connect = lambda scope: Connection()
    rows,warnings = retriever._specification_lookup('Bluetooth Audio function spec v2.52有吗',('21MM',))
    assert rows == [] and warnings == []
    query, params = executed[0]
    assert 'pd.file_name ILIKE' in query
    assert 'pd0' in query and 'pv.id IS NOT NULL' in query
    assert params[:3] == ('2.52','2.52','21MM')
    assert '%Bluetooth Audio%' in params
    assert query.count('%s') == len(params)


def test_spec_catalog_is_not_trusting_cover_sheet_current_pointer():
    retriever = EnterpriseRetriever.__new__(EnterpriseRetriever)
    captured=[]
    class Cursor:
        def execute(self,sql,params): captured.append(sql)
        def fetchall(self):return [{'spec_code':'550','canonical_name':'Bluetooth Audio',
                                     'current_version':None,'current_version_status':None}]
        def __enter__(self):return self
        def __exit__(self,*_):return False
    class DB:
        def cursor(self):return Cursor()
        def __enter__(self):return self
        def __exit__(self,*_):return False
    retriever._connect=lambda scope: DB()
    result, warnings = retriever._specification_catalog('有哪些式样书',('21MM',))
    assert not warnings and 'NO_REGISTERED_SPEC_BODY' in result[0].content
    assert 'public.specification_versions' in captured[0]
    assert 'public.documents' in captured[0]
    assert 'current_version_id' in captured[0]
