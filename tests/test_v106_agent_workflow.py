from __future__ import annotations

import pytest

from app.ai.agent import FastIntentRouter, QueryPlanner
from app.engineering_intelligence.workflow import WorkflowGraph, WorkflowNode, build_workflow
from app.engineering_intelligence.ontology import expand_engineering_query
from app.ai.retrieval import EnterpriseRetriever


def test_workflow_is_bounded_and_tool_gated():
    graph=build_workflow(('sql_exact_search','relation_search','version_search'),('21MM',))
    assert len(graph.nodes)==3
    assert graph.max_recovery_passes==1
    with pytest.raises(ValueError):
        build_workflow(('execute_sql',),('21MM',))
    with pytest.raises(ValueError):
        WorkflowGraph(nodes=(WorkflowNode('execute_sql'),),scopes=('21MM',)).validate()
    with pytest.raises(ValueError):
        WorkflowGraph(nodes=(WorkflowNode('relation_search',depends_on=('semantic_search',)), WorkflowNode('semantic_search')),
                      scopes=('21MM',)).validate()


def test_ontology_adds_canonical_term_without_identifying_different_entities():
    q=expand_engineering_query('21MM 508 的蓝牙识别判断是什么？')
    assert q[0]=='21MM 508 的蓝牙识别判断是什么？'
    assert any('Bluetooth identification' in value for value in q[1:])
    assert all('550' not in value for value in q)


def test_issue_plus_spec_relation_uses_governed_engineering_workflow():
    result=FastIntentRouter.classify('21MM 508 版本变更影响哪些 QA/BUG？')
    assert result.route=='ENGINEERING_AGENT'
    result2=FastIntentRouter.classify('MM21SPMAN-180 的状态')
    assert result2.route=='ISSUE_QUERY'


def test_cross_domain_fallback_uses_isolated_qabug_sql():
    plan=QueryPlanner._fallback_plan('21MM 508 和 550 版本变更影响哪些 BUG？', '21MM','')
    assert 'QABUG' in plan.scopes
    assert '21MM' in plan.scopes
    assert 'issue_search' in plan.tools


def test_explicit_scope_cannot_be_widened_by_untrusted_llm_proposal():
    class Offline:
        def chat_json(self,**kwargs):
            return {'scopes':['24MM','21MM','QABUG'],'tools':['semantic_search','execute_sql'],
                    'rewritten_queries':['Bluetooth']}
    planner=QueryPlanner(Offline())
    plan=planner.plan(model='local',question='21MM 508 设备判断要求',requested_scope='21MM',
                      current_scope='',conversation=[]).plan
    assert plan.scopes==('21MM',)
    assert plan.tools==('semantic_search',)


def test_qa_issue_scopes_in_mixed_agentic_mode_use_dedicated_repository():
    import inspect
    src=inspect.getsource(EnterpriseRetriever._issue_sql_search)
    assert 'if "QABUG" in scope_tuple' in src


def test_auto_scope_cannot_follow_llm_into_wrong_project():
    class Client:
        def chat_json(self, **kwargs):
            return {"scopes": ["24MM"], "tools": ["sql_exact_search"], "rewritten_queries": ["508"]}
    result=QueryPlanner(Client()).plan(model="local",question="21MM 508 式样书要求",requested_scope="AUTO",current_scope="",conversation=[])
    assert result.plan.scopes==("21MM",)
