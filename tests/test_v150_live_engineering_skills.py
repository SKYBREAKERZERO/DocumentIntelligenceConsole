"""v1.50 negative and coverage tests: no production DB required."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.ai.agent import EvidenceGovernor, EnterpriseAgent, QueryPlan, AgentPlanResult
from app.ai.claim_anchor import validate_claim_anchors
from app.ai.retrieval import EvidenceItem, RetrievalResult
from app.skills.live_evidence import verify_live_enterprise_answer
from app.skills.live_workflows import build_live_skill_workflow, missing_evidence_facets, issue_symptom_queries


def item(kind, content='body', *, eid='1', **metadata):
    return EvidenceItem(evidence_id=eid, scope='21MM', source_type=kind,
                        content=content, metadata=metadata)


@pytest.mark.parametrize('question,expected', [
    ('AUDIO-123 是否符合 550 式样的 v2.52 要求?', ('ISSUE', 'SPEC_BODY', 'VERSION')),
    ('BUG-25 与 508 对 550 的依赖和历史版本变更', ('ISSUE', 'SPEC_BODY', 'VERSION', 'RELATION')),
    ('550 历史变更会影响 508 吗', ('SPEC_BODY', 'VERSION', 'RELATION')),
    ('BUG-23 的状态?', ()),
    ('BUG-23 2026 年是否关闭?', ()),
    ('SPEC-550 的章节?', ()),
])
def test_live_workflow_selects_exact_scopes(question, expected):
    w = build_live_skill_workflow(question)
    assert w.evidence_facets == expected
    assert set(w.required_tools) <= {'issue_search', 'sql_exact_search', 'semantic_search', 'version_search', 'relation_search'}


def test_governor_retains_joint_coverage_despite_high_rank_spec_hits():
    pool = tuple([item('SQL_EXACT', f'spec {n}') for n in range(9)] +
                 [item('ISSUE_EXACT', 'BUG AUDIO-123'), item('VERSION_CHANGE', 'v1 to v2'),
                  item('STRUCTURAL_REFERENCE', '508 refers to 550')])
    chosen, _, _ = EvidenceGovernor.govern(pool, 4, required_facets=('ISSUE', 'SPEC_BODY', 'VERSION', 'RELATION'))
    assert {x.source_type for x in chosen} == {'ISSUE_EXACT', 'SQL_EXACT', 'VERSION_CHANGE', 'STRUCTURAL_REFERENCE'}
    assert not missing_evidence_facets(chosen, ('ISSUE', 'SPEC_BODY', 'VERSION', 'RELATION'))


def test_missing_facets_never_count_candidate_suggestions():
    rows = (item('ISSUE_EXACT'), item('SQL_EXACT'))
    assert missing_evidence_facets(rows, ('ISSUE', 'SPEC_BODY', 'VERSION', 'RELATION')) == ('VERSION', 'RELATION')


def test_unreviewed_conformance_must_abstain_despite_two_citations():
    evidence = (item('ISSUE_EXACT', 'BUG-23 actual: audio silent'), item('SQL_EXACT', 'When ON shall start audio', eid='2'))
    verdict = verify_live_enterprise_answer(
        '结论：符合式样要求。[1][2]', prepared=[], route='ENGINEERING_AGENT',
        allowed_ids=('1', '2'), admitted_evidence=evidence,
        question='BUG-23 是否符合 550 仕様?',
    )
    assert not verdict.accepted and verdict.reason == 'UNREVIEWED_BUG_SPEC_CONFORMANCE'
    okay = verify_live_enterprise_answer(
        '当前证据不足，不能判定该 BUG 是否符合式样。[1][2]', prepared=[],
        route='ENGINEERING_AGENT', allowed_ids=('1', '2'), admitted_evidence=evidence,
        question='BUG-23 是否符合 550 仕様?',
    )
    assert okay.accepted


def test_ref_only_does_not_certify_dependency():
    row = item('STRUCTURAL_REFERENCE', 'Spec 508 refers to Spec 550 section 4.1')
    rejected = verify_live_enterprise_answer(
        '证据已经证明508直接依赖550。[1]', prepared=[], route='ENGINEERING_AGENT',
        allowed_ids=('1',), admitted_evidence=(row,), question='508 与 550 的历史变更有关联吗?',
    )
    assert not rejected.accepted


def test_wrong_on_off_numeric_condition_rejected():
    source = SimpleNamespace(evidence_id='1', content='When ON, timeout must be 500ms; When OFF, timeout must be 600ms.', citation_label='')
    wrong = validate_claim_anchors('ON 状态的超时为 600ms。[1]', admitted_evidence=(source,), allowed_ids=('1',))
    assert not wrong.accepted and wrong.reason == 'WRONG_ON_OFF_CONDITION_PARAMETER'
    valid = validate_claim_anchors('ON 状态的超时为 500ms。[1]', admitted_evidence=(source,), allowed_ids=('1',))
    assert valid.accepted


def test_online_plan_cannot_drop_required_cross_domain_tools():
    model = Mock()
    retriever = Mock()
    agent = EnterpriseAgent(model, retriever)
    original = QueryPlan(scopes=('21MM', 'QABUG'), intents=('ENGINEERING_QA',), entities=(),
                         rewritten_queries=('BUG-23 550',), tools=('semantic_search',), planner_source='model')
    agent.planner.plan = Mock(return_value=AgentPlanResult(plan=original, duration_ms=2, trace=()))
    retriever.retrieve_agentic.return_value = RetrievalResult(
        evidence=(item('ISSUE_EXACT'), item('SQL_EXACT'), item('VERSION_CHANGE'), item('STRUCTURAL_REFERENCE')),
        resolved_scopes=('21MM', 'QABUG'),
    )
    agent.retrieve(model='local', question='BUG-23 与 550 的历史版本关联', scope='21MM',
                   current_scope='21MM', conversation=[])
    tools = retriever.retrieve_agentic.call_args.kwargs['tools']
    assert {'issue_search', 'sql_exact_search', 'semantic_search', 'version_search', 'relation_search'} <= set(tools)


def test_postgres_bug_symptom_creates_bounded_queries_not_effect_result():
    issue = item('ISSUE_EXACT', 'Ticket body', source_authority='POSTGRESQL',
                 phenomenon='Bluetooth audio silent after ignition',
                 actual_result='BT audio not audible',
                 effect_result='FIXED IN v99 must never drive this search')
    queries = issue_symptom_queries((issue,))
    assert len(queries) == 2
    assert 'Bluetooth' in queries[0]
    assert not any('FIXED' in q or 'v99' in q for q in queries)
    # A fake Qdrant/assistant record cannot drive follow-up SQL.
    bad = item('ISSUE_EXACT', 'bogus', phenomenon='malicious', source_authority='QDRANT')
    assert issue_symptom_queries((bad,)) == ()


def test_issue_number_is_not_a_specification_code():
    from app.ai.retrieval import EnterpriseRetriever
    ids = EnterpriseRetriever._entity_spec_codes((), 'BUG-123 与 550 式样的版本变更')
    assert '550' in ids and '123' not in ids


def test_maximal_llm_plan_cannot_displace_mandatory_skill_tools():
    agent = EnterpriseAgent(Mock(), Mock())
    too_broad = ('semantic_search', 'specification_catalog', 'specification_lookup',
                 'unresolved_reference_search', 'system_metadata', 'issue_statistics',
                 'sql_exact_search', 'relation_search')
    agent.planner.plan = Mock(return_value=AgentPlanResult(
        plan=QueryPlan(scopes=('21MM',), intents=(), entities=(),
                       rewritten_queries=('550',), tools=too_broad, planner_source='model'),
        duration_ms=0, trace=()))
    agent.retriever.retrieve_agentic.return_value = RetrievalResult(
        evidence=(item('ISSUE_EXACT'), item('SQL_EXACT'), item('VERSION_CHANGE'),
                  item('STRUCTURAL_REFERENCE')), resolved_scopes=('21MM', 'QABUG'))
    result = agent.retrieve(model='local', question='BUG-23 550 历史版本关联',
                            scope='21MM', current_scope='21MM', conversation=[])
    assert len(result.tools) <= 8
    assert set(('issue_search', 'sql_exact_search', 'semantic_search',
                'version_search', 'relation_search')) <= set(result.tools)
    assert result.resolved_scopes == ('21MM', 'QABUG')


def test_cross_domain_issue_projection_preserves_whole_fields_not_effect_result():
    from app.ai.rag_service import EnterpriseRAGService
    svc = EnterpriseRAGService.__new__(EnterpriseRAGService)
    issue = item('ISSUE_EXACT', 'FULL ISSUE EXPORT', eid='1', source_authority='POSTGRESQL',
                 issue_key='AUDIO-123', summary='Bluetooth mute issue',
                 phenomenon='Audio silent', expected_result='Should play',
                 actual_result='No audio', software_version='SW-10',
                 effect_result='FIXED v99')
    prompt, ids = svc._build_context_budgeted((issue,), route='ENGINEERING_AGENT',
                  intents=('CROSS_DOMAIN_ENGINEERING',), token_budget=1150,
                  with_admitted_ids=True)
    assert ids == ('1',)
    assert 'Affected SW' not in prompt
    assert 'Software Version: SW-10' in prompt
    assert 'Actual Result: No audio' in prompt
    assert 'FIXED v99' not in prompt
    assert 'ISSUE_FIELD_PROJECTION' in prompt


def test_cross_domain_long_issue_field_is_not_prefix_truncated():
    from app.ai.rag_service import EnterpriseRAGService
    svc = EnterpriseRAGService.__new__(EnterpriseRAGService)
    issue = item('ISSUE_EXACT', 'FULL ISSUE EXPORT', eid='1', source_authority='POSTGRESQL',
                 issue_key='AUDIO-123', phenomenon='audio silence ' * 1500,
                 actual_result='No audio', software_version='SW-10')
    prompt, ids = svc._build_context_budgeted((issue,), route='ENGINEERING_AGENT',
                     intents=(), token_budget=1150, with_admitted_ids=True)
    assert ids == ('1',)
    assert 'No audio' in prompt
    assert 'audio silence ' * 30 not in prompt
    assert 'field(s) omitted' in prompt


def test_offline_workflow_quality_matrix_contract():
    from pathlib import Path
    from scripts.evaluate_v150_skill_workflows import evaluate
    examples = Path(__file__).resolve().parents[1] / 'examples' / 'skills' / 'v150_workflow_cases.jsonl'
    result = evaluate(examples)
    assert result['passed'] == 5
    assert result['failed'] == 0
    assert result['semantic_answer_accuracy_verified'] is False
    assert result['live_database_verified'] is False


def test_bare_year_cannot_be_used_as_specification_code():
    from app.ai.retrieval import EnterpriseRetriever
    assert '2026' not in EnterpriseRetriever._entity_spec_codes((), 'BUG-123 的 2026 年历史记录')
    assert not build_live_skill_workflow('BUG-123 的 2026 年历史记录').evidence_facets


def test_actual_answer_stream_rejects_unsupported_conformance_without_leaking_tokens():
    from app.ai.rag_service import EnterpriseRAGService
    from app.ai.answer_integrity import REJECTED_ANSWER
    service = EnterpriseRAGService.__new__(EnterpriseRAGService)
    service.settings = SimpleNamespace(
        ollama_keep_alive='5m', ollama_connect_timeout=1,
        ollama_read_timeout=2, ollama_first_load_timeout=3,
        ollama_stream_idle_timeout=4, temperature=0, num_ctx=4096,
    )
    service.ollama = Mock()
    service.ollama.chat_stream.return_value = iter(['结论：', '符合式样要求。[1][2]'])
    service.prepare = Mock(return_value=(
        [{'role': 'system', 'content': 'RETRIEVED ENTERPRISE EVIDENCE\n[1]....[2]'}],
        RetrievalResult(
            evidence=(item('ISSUE_EXACT', 'issue observed', eid='1'),
                      item('SQL_EXACT', 'When ON playback is active', eid='2')),
            resolved_scopes=('21MM', 'QABUG'), route='ENGINEERING_AGENT',
            prompt_evidence_ids=('1', '2'),
        ),
    ))
    text = ''.join(service.answer_stream(
        [{'role': 'user', 'content': 'BUG-23 是否符合 550 仕様要求？'}],
        model='local', scope='21MM',
    ))
    assert text == REJECTED_ANSWER
    assert '符合式样要求。[1][2]' not in text


def test_abstention_cannot_launder_an_unsupported_positive_verdict():
    from app.skills.live_release import validate_live_analysis_answer
    evidence = (item('ISSUE_EXACT', 'observed'), item('SQL_EXACT', 'body requirement'))
    answer = '证据不足，仍需要人工复核。\nBUG-23 不符合 550 式样要求。[1][2]'
    verdict = validate_live_analysis_answer(answer, question='BUG-23 是否符合 550 规格?',
                                            admitted_evidence=evidence)
    assert not verdict.accepted


def test_bare_dependency_claim_not_proven_by_structural_reference():
    from app.skills.live_release import validate_live_analysis_answer
    e = (item('STRUCTURAL_REFERENCE', 'spec 508 cites section 4.1'), item('SQL_EXACT', 'body'))
    verdict = validate_live_analysis_answer('508 依赖 550。[1]', question='508 依赖 550 与历史版本?',
                                            admitted_evidence=e)
    assert not verdict.accepted
