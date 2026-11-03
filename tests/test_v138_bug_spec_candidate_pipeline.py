"""Stage 2-6 v1.38 synthetic negative acceptance, not real PostgreSQL tests."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from app.ai.agent import EvidenceGovernor
from app.ai.retrieval import EvidenceItem, EnterpriseRetriever
from app.engineering_intelligence.bug_spec_candidates import (
    SCORER_VERSION, SpecUnitSnapshot, conservative_verdict, extract_bug_observations,
    rank_candidates, sha256_normalized, source_digest,
)

ROOT = Path(__file__).resolve().parents[1]


def bug(**extra):
    row = dict(record_type='BUG', issue_key='AUDIO-123',summary='蓝牙静音',description='蓝牙静音偶发',
               phenomenon='Bluetooth audio mute after resume',
               repro_steps='When Bluetooth resumes audio output',
               expected_result='Bluetooth audio shall continue playback',
               actual_result='Bluetooth output remains muted',
               effect_result='Fixed in 9.99 (not actual)',
               software_version='SW3',affected_version='SW2',model='CAR1',destination='JP')
    row.update(extra)
    return row


def unit(**extra):
    txt = 'When Bluetooth resumes, audio output shall continue playback.'
    chunk = 'pre ' + txt + ' after'
    row = dict(scope='21MM',spec_code='550',version_id=52,version_label='2.52',
               document_id='document-body',file_name='Bluetooth Audio function spec v2.52.pdf',
               document_metadata={'document_role':'SPEC_BODY'}, unit_id=9001, content_id=51,
               section_id='4.1',page_number=14,start_offset=4,end_offset=4+len(txt),
               raw_text=txt,text_hash=sha256_normalized(txt),chunk_text=chunk)
    row.update(extra)
    return SpecUnitSnapshot(**row)


def test_offline_ranking_requires_primary_body_and_verbatim_unicode_offsets():
    good = unit()
    assert good.verify()
    cover = replace(good,file_name='Bluetooth Audio Function Spec Cover Sheet v2.50.pdf')
    assert not cover.verify()
    wrong = replace(good,raw_text=good.raw_text.replace('continue','stop'))
    assert not wrong.verify()
    wrong_hash = replace(good,text_hash='0'*64)
    assert not wrong_hash.verify()
    wrong_scope = replace(good,scope='QABUG')
    assert not wrong_scope.verify()
    matches = rank_candidates(extract_bug_observations(bug()),[cover,wrong,good])
    assert len(matches) == 1 and matches[0][0].unit_id == good.unit_id
    assert 0 < matches[0][1] <= 1


def test_real_bug_observations_not_from_fix_effect_and_do_not_infer_normative_verdict():
    observations=extract_bug_observations(bug(phenomenon='', actual_result='mute observed'))
    assert observations['phenomenon']=='mute observed'
    assert 'Fixed in 9.99' not in str(observations)
    with pytest.raises(ValueError,match='BUG record'):
        extract_bug_observations(bug(record_type='QA'))
    assert source_digest(bug(affected_version='SW2')) != source_digest(bug(affected_version='SW1'))
    assert source_digest(bug(actual_result='mute')) != source_digest(bug(actual_result='play'))
    assert all(conservative_verdict(status=s,mapping_verified=True,source_intact=True,target_intact=True)=='证据不足'
               for s in ('UNREVIEWED','REJECTED','CONFIRMED','STALE'))


def test_unknown_or_weak_query_does_not_create_semantic_fact():
    assert not rank_candidates({'phenomenon':'unrelatedxyz'},[unit()])
    assert SCORER_VERSION == 'bug-spec-lexical-v1'
    with pytest.raises(ValueError):
        rank_candidates({'phenomenon':'Bluetooth'},[unit()],top_n=0)


def test_db_migration_is_qabug_only_no_autogrant_or_automatic_verdict():
    sql=(ROOT/'database/upgrade_bug_spec_candidates_v1_38.sql').read_text()
    assert "to_regclass('public.qabug_records')" in sql
    assert "to_regclass('public.specifications') IS NOT NULL" in sql
    assert "('PENDING','APPROVED','REVOKED')" in sql
    assert "('UNREVIEWED','REJECTED','STALE')" in sql
    assert 'REVOKE ALL ON public.qabug_spec_candidates FROM PUBLIC' in sql
    assert 'GRANT ALL' not in sql
    assert 'ON CONFLICT' not in sql


def test_online_separates_non_authoritative_candidate_from_llm_governor():
    candidate=EvidenceItem(evidence_id='C1',scope='21MM',source_type='BUG_SPEC_CANDIDATE',
                           content='UNREVIEWED example',score=0.91,
                           metadata={'non_authoritative_candidate':True})
    main, strength, _=EvidenceGovernor.govern((),5)
    assert not main and strength == 'NONE'
    src=(ROOT/'app/ai/retrieval.py').read_text()
    assert 'candidate_suggestions=tuple(candidate_suggestions)' in src
    assert 'candidate_suggestions: tuple[EvidenceItem, ...] = ()' in src
    assert '(*catalog_items, *issue_items, *relation_items' not in src  # simple regression marker
    assert 'for item in [*catalog_items, *issue_items, *relation_items' in src
    assert 'bug_spec_candidate_display_only' in src
    assert candidate.metadata['non_authoritative_candidate']


def test_offline_script_version_locked_and_dry_run_by_default():
    src=(ROOT/'scripts/build_bug_spec_candidates.py').read_text()
    for required in ('sv.version_label=%s','sv.document_id=%s','sp.spec_code=%s',
                     'sp.project_code=%s',"m.status='APPROVED'","r.record_type='BUG'",
                     'rank_candidates(observation, units','if not apply:',
                     'source_digest(current) != digest','SET TRANSACTION READ ONLY'):
        assert required in src


class FakeCursor:
    def __init__(self, row, is_spec=False):
        self.row = row
        self.is_spec = is_spec
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def execute(self, sql, params):
        self.valid = True
        if self.is_spec:
            self.valid = (params[0] == self.row['unit_id']
                          and params[1] == self.row['version_id']
                          and params[2] == self.row['spec_code']
                          and params[4] == self.row['version_label']
                          and params[5] == self.row['document_id'])
        assert '%s' in sql and isinstance(params, tuple)
        assert ('ei_requirement_units' in sql) == self.is_spec
        if self.is_spec:
            assert 'sv.version_label=%s' in sql
            assert 'sp.project_code=%s' in sql
            assert 'sv.document_id=%s' in sql
            assert 'AND ' in sql and 'document_role' in sql
        else:
            assert "c.status='UNREVIEWED'" in sql
            assert "m.status='APPROVED'" in sql
    def fetchall(self): return [] if self.is_spec else [self.row]
    def fetchone(self): return self.row if self.is_spec and self.valid else None


class FakeConnection:
    def __init__(self,cursor): self._cursor=cursor
    def __enter__(self): return self
    def __exit__(self,*args): return False
    def cursor(self): return self._cursor


def test_online_query_revalidates_historical_source_and_exposes_only_display_candidates():
    u=unit()
    bug_fields=bug()
    candidate={**bug_fields,
        'id':901, 'issue_snapshot_hash':source_digest(bug_fields),
        'mapping_id':7, 'spec_scope':'21MM', 'spec_code':u.spec_code,
        'spec_version_id':u.version_id, 'spec_version_label':u.version_label,
        'spec_document_id':u.document_id, 'spec_unit_id':u.unit_id,
        'spec_content_id':u.content_id,'spec_file_name':u.file_name,
        'spec_text_hash':u.text_hash,'spec_raw_text':u.raw_text,
        'start_offset':u.start_offset,'end_offset':u.end_offset,
        'rank_score':0.25,'mapped_software_version':'SW2'}
    spec={ 'spec_code':u.spec_code,'version_id':u.version_id,
        'version_label':u.version_label,'document_id':u.document_id,
        'file_name':u.file_name,'metadata':u.document_metadata,
        'unit_id':u.unit_id,'content_id':u.content_id,'section_id':u.section_id,
        'page_number':u.page_number,'start_offset':u.start_offset,
        'end_offset':u.end_offset,'raw_text':u.raw_text,'text_hash':u.text_hash,
        'chunk_text':u.chunk_text}
    retriever=EnterpriseRetriever.__new__(EnterpriseRetriever)
    retriever.postgres_profiles={'QABUG':object(),'21MM':object()}
    retriever.top_k=8
    retriever._connect=lambda scope: FakeConnection(FakeCursor(candidate if scope=='QABUG' else spec,
                                                                 is_spec=(scope!='QABUG')))
    rows=retriever._bug_spec_candidate_search('BUG AUDIO-123 是否符合 550 式样')
    assert len(rows)==1
    assert rows[0].metadata['verdict']=='证据不足'
    assert rows[0].metadata['non_authoritative_candidate'] is True
    assert 'SPEC_BODY verbatim' in rows[0].content
    assert 'SW2' in rows[0].content and '2.52' in rows[0].content

    for drift in (
         dict(candidate,issue_snapshot_hash='0'*64),
         dict(candidate,spec_version_label='2.50'),
         dict(candidate,spec_text_hash='0'*64),
         dict(candidate,spec_file_name='Bluetooth Audio Cover Sheet.pdf')):
        retriever._connect=lambda scope, row=drift: FakeConnection(FakeCursor(
            row if scope=='QABUG' else spec,is_spec=(scope!='QABUG')))
        assert retriever._bug_spec_candidate_search('BUG AUDIO-123 是否符合 550 式样')==[]
    bad_spec=dict(spec,file_name='Bluetooth Audio Cover Sheet.pdf')
    retriever._connect=lambda scope: FakeConnection(FakeCursor(
        candidate if scope=='QABUG' else bad_spec,is_spec=(scope!='QABUG')))
    assert retriever._bug_spec_candidate_search('BUG AUDIO-123 是否符合 550 式样')==[]


def test_candidate_context_is_physically_excluded_from_llm_and_version_filter():
    from app.ai.retrieval import RetrievalResult
    from app.ai.rag_service import EnterpriseRAGService
    from app.ai.agent import EvidenceGovernor
    candidate=EvidenceItem(evidence_id='C1',scope='21MM',source_type='BUG_SPEC_CANDIDATE',
                           content='SENSITIVE UNREVIEWED BODY',score=0.91,
                           metadata={'non_authoritative_candidate':True,'candidate_id':8})
    result=RetrievalResult(evidence=(),candidate_suggestions=(candidate,),resolved_scopes=('21MM',))
    result=EnterpriseRAGService._renumber_governed(result,5)
    assert result.candidate_suggestions == (candidate,)
    assert result.evidence_strength == 'NONE'
    svc=EnterpriseRAGService.__new__(EnterpriseRAGService)
    assert svc._build_context_budgeted(result.evidence,route='ENGINEERING_AGENT',intents=(),token_budget=9999)==''
    assert not EvidenceGovernor.govern(result.evidence,8)[0]
    # Filtering via the end-user's explicit spec code cannot be ignored.
    assert 'requested_codes = self._entity_spec_codes((), question)' in (ROOT/'app/ai/retrieval.py').read_text()
    assert "requested_codes and str(row['spec_code']).upper() not in requested_codes" in (ROOT/'app/ai/retrieval.py').read_text()


def test_fast_route_and_fallback_keep_explicit_bug_spec_compliance_cross_domain():
    from app.ai.agent import QueryPlanner, FastIntentRouter
    for question in ('BUG AUDIO-123 是否符合 550 式样',
                     'AUDIO-123 是否满足 SPEC 550 的技术要求'):
        route=FastIntentRouter.classify(question)
        assert route.route == 'ENGINEERING_AGENT'
        plan=QueryPlanner._fallback_plan(question,'AUTO','')
        assert 'issue_search' in plan.tools
        assert 'QABUG' in plan.scopes
        assert 'sql_exact_search' in plan.tools
    assert FastIntentRouter.classify('BUG-123 当前状态').route == 'ISSUE_QUERY'
