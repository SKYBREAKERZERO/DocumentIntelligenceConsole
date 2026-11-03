"""v1.43: Agent live PG snapshot audit routing and fail-closed boundaries."""
from __future__ import annotations

import pytest

from app.ai.agent import EvidenceGovernor, QueryPlanner
from app.ai.retrieval import EnterpriseRetriever
from app.engineering_intelligence.temporal_query import (
    choose_audit_scope, parse_temporal_audit_ids,
)
from app.enterprise.profiles import PostgresProfile


@pytest.mark.parametrize('query', [
    '21MM 关系ID 31 新目标版本ID 303 是否仍成立',
    '21MM relation_id=31 new_target_version_id=303 validity?',
    '关系 id:31 目标版本id:303',
    'relation ID 31 new target version ID 303',
    '关系ID：31 新版本ID：303',
])
def test_parse_explicit_labeled_coordinates(query):
    assert parse_temporal_audit_ids(query) == (31, 303)


@pytest.mark.parametrize('question', [
    '关系 31 新版本 303', '550 v2.52 是否还有效？',
    'relation_id=31 target=303', 'relation_id=0 new_target_version_id=303',
    'relation_id=31 new_target_version_id=0',
    'relation_id=31 relation_id=32 new_target_version_id=303',
    'relation_id=31 new_target_version_id=303 new_target_version_id=304',
    'relation_id=31 new_target_version_id=303 OR 404',
    'relation_id=999999999999999999999 new_target_version_id=303',
    'relation_id=31 new_target_version_id=303; relation_id=31',
])
def test_never_guess_or_accept_ambiguous_relation_version(question):
    assert parse_temporal_audit_ids(question) is None


def test_single_project_scope_is_mandatory():
    assert choose_audit_scope('21MM relation_id=31 new_target_version_id=303', ('21MM','COMMON')) == '21MM'
    assert choose_audit_scope('relation_id=31 new_target_version_id=303', ('21MM','COMMON')) is None
    assert choose_audit_scope('21MM 24MM relation_id=31 new_target_version_id=303', ('21MM','24MM')) is None
    assert choose_audit_scope('24MM relation_id=31 new_target_version_id=303', ('21MM',)) is None
    assert choose_audit_scope('relation_id=31 new_target_version_id=303', ('QABUG',)) is None
    assert choose_audit_scope('relation_id=31 new_target_version_id=303', ('COMMON',)) == 'COMMON'


class Connection:
    def __init__(self, project='21MM', database='rag_21mm'):
        self.project, self.database = project, database
        self.queries = []
        self.rollback_count = 0
        self.closed = False

    def __enter__(self):
        return self
    def __exit__(self, *_):
        self.closed = True
    def cursor(self):
        return self
    def execute(self, sql, params=()):
        self.queries.append((sql, params))
    def fetchall(self):
        return [{'db': self.database, 'project_code': self.project}]
    def rollback(self):
        self.rollback_count += 1


def new_retriever():
    return EnterpriseRetriever(
        postgres_profiles={'21MM': PostgresProfile(database='rag_21mm', user='dic_reader')},
        postgres_password='fake', qdrant=None, qdrant_api_key='', collections={},
    )


def test_live_audit_does_not_grant_continuity_or_write(monkeypatch):
    retriever = new_retriever()
    db = Connection()
    retriever._connect = lambda _: db
    calls = []
    def assessor(cur, *, project, relation_id, new_target_version_id):
        calls.append((project, relation_id, new_target_version_id))
        assert db.queries[0][0] == 'SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'
        return {'status':'TARGET_TEXT_CONTINUITY_VERIFIED', 'issues':[],
                'target_spec_code':'550', 'old_target_version':'2.50',
                'old_target_section':'4.1', 'old_target_hash':'a'*64,
                'new_target_version':'2.52', 'new_target_hash':'a'*64,
                'source_version_alignment':'NOT_ESTABLISHED',
                'full_source_digest_check':'NOT_PERFORMED'}
    import app.engineering_intelligence.relation_temporal_audit as module
    monkeypatch.setattr(module, 'assess_temporal_relation', assessor)
    result = retriever.retrieve_agentic(
        question='21MM relation_id=31 new_target_version_id=303', scopes=('21MM',),
        rewritten_queries=('ignore arbitrary rewritten relation_id=999',), tools=(),
    )
    assert calls == [('21MM', 31, 303)]
    assert db.rollback_count == 1 and db.closed
    assert len(result.evidence) == 1
    row = result.evidence[0]
    assert row.source_type == 'TEMPORAL_RELATION_AUDIT'
    assert row.metadata['relation_validity'] == 'UNDETERMINED'
    assert row.metadata['human_revalidation_required'] is True
    assert 'NOT engineering approval' in row.content or 'NOT engineering approval'.lower() in row.content.lower()
    assert all(sql.lstrip().upper().startswith(('SELECT', 'SET')) for sql, _ in db.queries)
    chosen, strength, _ = EvidenceGovernor.govern(result.evidence, 8)
    assert strength in {'LIMITED', 'MODERATE'}  # never STRONG on one temporal report
    assert chosen[0].metadata['evidence_grade'] != 'HIGH'


def test_incomplete_audit_is_diagnostic_only(monkeypatch):
    retriever = new_retriever(); db = Connection(); retriever._connect = lambda _: db
    import app.engineering_intelligence.relation_temporal_audit as module
    monkeypatch.setattr(module,'assess_temporal_relation',lambda *_a, **_k: {
        'status':'INSUFFICIENT_EVIDENCE', 'issues':['VERSION_BACKFILL_RECEIPT_MISSING_OR_INCOMPLETE']})
    row = retriever._temporal_relation_audit_search('21MM', relation_id=31, new_target_version_id=303)
    assert row.source_type == 'TEMPORAL_RELATION_UNVERIFIED'
    assert row.metadata['relation_validity'] == 'UNDETERMINED'
    evidence, strength, _ = EvidenceGovernor.govern((row,), 8)
    assert strength == 'LIMITED'
    assert evidence[0].metadata['evidence_grade'] == 'LOW'


def test_wrong_project_identity_is_rejected_before_audit(monkeypatch):
    retriever = new_retriever(); db = Connection(project='24MM')
    retriever._connect = lambda _: db
    import app.engineering_intelligence.relation_temporal_audit as module
    monkeypatch.setattr(module,'assess_temporal_relation',lambda *_a, **_k: pytest.fail('should not assess'))
    with pytest.raises(RuntimeError, match='identity mismatch'):
        retriever._temporal_relation_audit_search('21MM', relation_id=31, new_target_version_id=303)
    assert db.rollback_count == 1


@pytest.mark.parametrize('user,database', [('postgres','rag_21mm'), ('dic_reader','rag'),('root','rag_21mm')])
def test_unrestricted_or_shared_profile_is_blocked(user,database):
    retriever=new_retriever()
    retriever.postgres_profiles['21MM']=PostgresProfile(database=database,user=user)
    retriever._connect=lambda _: pytest.fail('must never connect')
    with pytest.raises(ValueError):
        retriever._temporal_relation_audit_search('21MM', relation_id=31, new_target_version_id=303)


def test_no_project_broadcast_and_no_unlabeled_ids():
    retriever = new_retriever()
    retriever._connect=lambda _: pytest.fail('must never connect')
    result=retriever.retrieve_agentic(question='relation_id=31 new_target_version_id=303',
        scopes=('21MM','COMMON'), rewritten_queries=(), tools=())
    assert not result.evidence
    assert any('one engineering project' in w for w in result.warnings)
    result=retriever.retrieve_agentic(question='550 v2.52 有效?',scopes=('21MM',),rewritten_queries=(),tools=())
    assert not result.evidence


def test_fallback_plan_forces_both_audit_tools():
    plan=QueryPlanner._fallback_plan('21MM relation_id=31 new_target_version_id=303',
                                              '21MM','21MM')
    assert 'relation_search' in plan.tools and 'version_search' in plan.tools


def test_router_dispatches_exact_temporal_audit_before_generic_metadata():
    from app.ai.agent import FastIntentRouter
    decision=FastIntentRouter.classify('21MM 当前关系 relation_id=31 new_target_version_id=303 数据库状态?')
    assert decision.route == 'ENGINEERING_AGENT'
    assert decision.intent == 'TEMPORAL_RELATION_AUDIT'


def test_only_temporal_evidence_even_if_other_tools_requested(monkeypatch):
    retriever = new_retriever(); db = Connection(); retriever._connect=lambda _: db
    import app.engineering_intelligence.relation_temporal_audit as module
    monkeypatch.setattr(module,'assess_temporal_relation',lambda *_a, **_k: {
        'status':'TARGET_CHANGED_REVIEW_REQUIRED','target_spec_code':'550',
        'new_target_version':'2.52','issues':[]})
    retriever._semantic_search=lambda *_a, **_k: pytest.fail('semantic search must not run')
    retriever._relation_search=lambda *_a, **_k: pytest.fail('general relation search must not run')
    retriever._version_search=lambda *_a, **_k: pytest.fail('general version search must not run')
    result=retriever.retrieve_agentic(question='21MM relation_id=31 new_target_version_id=303',
        scopes=('21MM',), rewritten_queries=('other spec version',),
        tools=('semantic_search','relation_search','version_search'))
    assert len(result.evidence)==1
    assert result.evidence[0].metadata['audit_status']=='TARGET_CHANGED_REVIEW_REQUIRED'
    assert result.evidence[0].metadata['relation_validity']=='UNDETERMINED'
