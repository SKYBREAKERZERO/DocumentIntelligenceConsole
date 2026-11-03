"""Every bounded skill has positive and fail-closed contract tests."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.ai.retrieval import EvidenceItem
from app.skills.catalog import SKILLS, list_skills, suggest_skills
from app.skills.executor import SkillInputError, execute_skill
from app.skills.live_evidence import verify_live_enterprise_answer
from app.engineering_intelligence.requirements import normalize_text

ROOT = Path(__file__).resolve().parents[1]


def body(text='When Ignition ON, timeout must be 500 ms.'):
    return dict(project='21MM', spec_code='550', version_id=25, document_id='doc550',
                file_name='Bluetooth Audio function spec v2.52.pdf', document_role='SPEC_BODY',
                content_id=17, section_id='4.1', page_number=31, text=text)


def digest(text):
    return hashlib.sha256(normalize_text(text).encode('utf-8')).hexdigest()


def relation():
    left = 'Refer to spec 550 section 4.1 for Bluetooth requirements.'
    right = 'The Bluetooth audio output shall mute within 500ms.'
    return dict(source=dict(project='21MM', file_name='Some function spec.pdf',
                            document_role='SPEC_BODY', document_id='doc508',
                            source_document='doc508', source_text=left, source_chunk=left,
                            source_start=0, source_end=len(left), source_hash=digest(left)),
                target=dict(project='21MM', file_name='Bluetooth Audio function spec v2.52.pdf',
                            document_role='SPEC_BODY', document_id='doc550',
                            target_document='doc550', target_code='550', target_section='4.1',
                            target_text=right, target_chunk=right,
                            target_start=0, target_end=len(right), target_hash=digest(right)))


def bug_unit():
    text = 'Bluetooth audio is silent after ignition'
    return dict(scope='21MM', spec_code='550', version_id=25, version_label='v2.52',
                document_id='doc550', file_name='Bluetooth Audio function spec.pdf',
                document_metadata={'document_role':'SPEC_BODY'}, unit_id=1,
                content_id=17, section_id='4.1', page_number=31,
                start_offset=0, end_offset=len(text), raw_text=text,
                text_hash=digest(text), chunk_text=text)


def bug():
    return dict(record_type='BUG', issue_key='AUDIO-123', summary='Bluetooth audio silent',
                phenomenon='Bluetooth audio silent', actual_result='Bluetooth audio silent',
                expected_result='audio active', repro_steps='ignition ON',
                effect_result='FIXED but must not become actual_result',
                affected_version='SW10', software_version='SW10')


def rr_item(content='Timeout 500ms, version v2.52'):
    return dict(scope='21MM', document_id='doc550', version='v2.52',
                content_id=17, content=content,
                content_hash=hashlib.sha256(content.encode()).hexdigest(),
                file_name='Bluetooth Audio function spec.pdf', document_role='SPEC_BODY')


def evidence():
    return {'answer':'超时为500ms，版本为v2.52。[1]',
            'evidence':[{'id':'1','text':'Timeout 500ms version v2.52','label':'550 v2.52'}]}


def test_registry_is_complete_and_static():
    assert len(SKILLS) == 7
    assert {x['name'] for x in list_skills()} == set(SKILLS)
    for slug in SKILLS:
        skill = ROOT / 'config' / 'skills' / slug
        assert (skill / 'SKILL.md').is_file() and (skill / 'manifest.json').is_file()
        assert (skill / 'SKILL.md').read_text(encoding='utf-8').startswith('---\nname:')


def test_no_dynamic_skill_dispatch():
    with pytest.raises(SkillInputError, match='UNKNOWN_SKILL'):
        execute_skill('__import__("os").system("whoami")', {})
    with pytest.raises(SkillInputError):
        execute_skill('../content-relation-audit', {})


def test_requirement_extraction_preserves_verbatim_span_and_hash():
    result = execute_skill('spec-requirement-extraction', body())
    unit = result['findings'][0]
    assert unit['source_text'] == body()['text']
    assert unit['evidence']['source_hash'] == digest(body()['text'])
    assert unit['signals']['conditional']
    assert result['database_access'] == 'NONE' and result['database_mutations'] == 0


@pytest.mark.parametrize('overrides', [
    {'document_role':'COVER_SHEET'}, {'file_name':'Bluetooth Audio coversheet.pdf'},
    {'document_role':''}, {'text':'x' * 150001}, {'content_id':0},
])
def test_requirement_extraction_rejects_unsafe_or_invalid(overrides):
    with pytest.raises((SkillInputError, ValueError)):
        execute_skill('spec-requirement-extraction', body() | overrides)


def test_relation_verifies_both_spans_and_never_confirms_dependency():
    result=execute_skill('content-relation-audit',relation())
    assert result['findings'][0]['type']=='STRUCTURAL_REFERENCE'
    assert result['needs_human_review'] is True
    assert result['production_acceptance']=='NOT_ASSESSED'


@pytest.mark.parametrize('change', [
    lambda p: p['target'].update(target_section='5.1'),
    lambda p: p['source'].update(source_hash='0' * 64),
    lambda p: p['source'].update(project='24MM'),
    lambda p: p['target'].update(file_name='Bluetooth Audio cover sheet.pdf'),
    lambda p: p['source'].update(source_text='Refer to spec 550. Another sentence mentions section 4.1'),
])
def test_relation_fails_closed(change):
    payload=relation();change(payload)
    with pytest.raises(SkillInputError):
        execute_skill('content-relation-audit',payload)


def test_version_only_returns_text_alignment():
    old=body('When Ignition ON, timeout shall be 500ms.')
    new=body('When Ignition ON, timeout shall be 600ms.') | {'version_id':26}
    result=execute_skill('spec-version-impact',{'old':old,'new':new})
    assert result['needs_human_review'] and 'TEXT' in result['evidence_level']
    assert result['findings']
    assert '版本' in str(result['limitations'])


@pytest.mark.parametrize('changes', [
    {'spec_code':'508'},{'project':'24MM'},{'version_id':25},
    {'document_role':'COVER_SHEET'},
])
def test_version_fails_identity_and_role_drift(changes):
    with pytest.raises(SkillInputError):
        execute_skill('spec-version-impact',{'old':body(),'new':body() | {'version_id':26} | changes})


def test_bug_uses_actual_not_effect_result_and_never_labels_conformance():
    result=execute_skill('engineering-qa-bug-analysis',{'bug':bug(),'units':[bug_unit()]})
    assert 'FIXED' not in str(result['observations'])
    assert result['conformance_verdict']=='证据不足'
    assert result['findings'][0]['status']=='UNREVIEWED_CANDIDATE'


@pytest.mark.parametrize('overrides', [
    {'file_name':'550 coversheet.pdf'}, {'text_hash':'abc'},
    {'version_id':0}, {'start_offset':2},
])
def test_bug_rejects_invalid_spec_snapshots(overrides):
    with pytest.raises(SkillInputError):
        execute_skill('engineering-qa-bug-analysis',{'bug':bug(),'units':[bug_unit() | overrides]})


def test_bug_record_type_required():
    with pytest.raises(ValueError):
        execute_skill('engineering-qa-bug-analysis',{'bug':bug() | {'record_type':'QA'},'units':[]})


def test_rrf_three_lanes_rank_only_with_sql_identity():
    hit=rr_item()
    result=execute_skill('engineering-hybrid-retrieval',{'sql':[hit], 'dense':[hit], 'sparse':[hit]})
    assert result['findings'][0]['retrieval_sources']==['POSTGRESQL','QDRANT_DENSE','QDRANT_SPARSE']
    assert result['evidence_level']=='OFFLINE_RANKING_ONLY'


def test_rrf_disallows_vector_only_and_cross_version_votes():
    hit=rr_item()
    result=execute_skill('engineering-hybrid-retrieval',{'sql':[hit], 'dense':[hit | {'version':'v2.53'}], 'sparse':[]})
    assert result['findings'][0]['retrieval_sources']==['POSTGRESQL']
    assert not execute_skill('engineering-hybrid-retrieval',{'sql':[], 'dense':[hit]})['findings']


@pytest.mark.parametrize('overrides', [
    {'content_hash':'fake'}, {'document_role':'COVER_SHEET'},
    {'file_name':'550 Cover Sheet.pdf'},
])
def test_rrf_rejects_untrusted_snapshot(overrides):
    with pytest.raises(SkillInputError):
        execute_skill('engineering-hybrid-retrieval',{'sql':[rr_item() | overrides]})


@pytest.mark.parametrize('answer,accepted', [
    ('超时为500ms，版本为v2.52。[1]',True),
    ('超时为600ms，版本为v2.52。[1]',False),
    ('超时为500ms，版本为v2.53。[1]',False),
    ('超时为500ms。[999]',False),
])
def test_offline_evidence_skill_is_bounded(answer,accepted):
    result=execute_skill('rag-evidence-verification', evidence() | {'answer':answer})
    assert result['findings'][0]['accepted'] is accepted
    assert result['needs_human_review'] is True


def test_offline_evidence_rejects_duplicate_ids():
    x=evidence();x['evidence'].append(x['evidence'][0])
    with pytest.raises(SkillInputError):
        execute_skill('rag-evidence-verification',x)


def test_live_evidence_skill_uses_only_admitted_citations():
    e=EvidenceItem(evidence_id='1', scope='21MM', source_type='SQL_EXACT',
                   content='timeout 500ms version v2.52',document_id='doc550')
    valid=verify_live_enterprise_answer('超时500ms。[1]',prepared=[],route='ENGINEERING_AGENT',
        allowed_ids=('1',),admitted_evidence=(e,))
    assert valid.accepted
    invalid=verify_live_enterprise_answer('超时500ms。[999]', prepared=[],route='ENGINEERING_AGENT',
        allowed_ids=('1',),admitted_evidence=(e,))
    assert not invalid.accepted


def test_quality_gate_does_not_certify_synthetic():
    case=dict(case_id='synthetic-001',dataset_type='SYNTHETIC_CONTRACT',
              answer='超时500ms。[1]',evidence=[dict(id='1',text='timeout 500ms')], expected_accept=True)
    result=execute_skill('rag-quality-gate',{'cases':[case]})
    assert result['matched']==1
    assert result['human_reviewed_real_cases']==0
    assert result['production_acceptance']=='NOT_ASSESSED'


def test_quality_gate_rejects_fake_real_and_duplicates():
    case=dict(case_id='synthetic-001',dataset_type='HUMAN_REVIEWED_REAL',
              answer='超时500ms。[1]',evidence=[dict(id='1',text='timeout 500ms')], expected_accept=True)
    with pytest.raises(ValueError):
        execute_skill('rag-quality-gate',{'cases':[case]})
    case['dataset_type']='SYNTHETIC_CONTRACT'
    with pytest.raises(SkillInputError):
        execute_skill('rag-quality-gate',{'cases':[case,case]})


def test_skills_cli_is_executable_and_has_no_db_side_effects(tmp_path: Path):
    p=tmp_path/'case.json'
    p.write_text(json.dumps(evidence(),ensure_ascii=False),encoding='utf-8')
    proc=subprocess.run([sys.executable, str(ROOT/'scripts/run_skill.py'),
                         'rag-evidence-verification','--input',str(p)],
                        capture_output=True,text=True,check=False,cwd=tmp_path)
    assert proc.returncode==0,proc.stderr
    data=json.loads(proc.stdout)
    assert data['database_access']=='NONE' and data['findings'][0]['accepted']


def test_skills_cli_rejects_oversized_input(tmp_path: Path):
    p=tmp_path/'large.json';p.write_bytes(b' '*2_000_001)
    proc=subprocess.run([sys.executable,str(ROOT/'scripts/run_skill.py'),
                         'rag-evidence-verification','--input',str(p)],
                        capture_output=True,text=True,check=False)
    assert proc.returncode==2 and 'INPUT_TOO_LARGE' in proc.stderr


def test_skill_suggestions_are_deterministic_and_never_execute():
    suggested = suggest_skills('21MM BUG-123 在历史版本的 550 关联是否符合式样')
    assert 'engineering-qa-bug-analysis' in suggested
    assert 'spec-version-impact' in suggested
    assert 'content-relation-audit' in suggested
    assert suggested[-1] == 'rag-evidence-verification'
    assert suggest_skills('') == ()


def test_cli_skill_suggestion_is_nonexecuting():
    proc = subprocess.run([sys.executable, str(ROOT/'scripts/run_skill.py'),
                          '--suggest', '分析 550 的版本变更'],
                          capture_output=True,text=True,check=False)
    assert proc.returncode == 0
    data = json.loads(proc.stdout)
    assert data['executed'] is False
    assert 'spec-version-impact' in data['suggested_skills']


def test_release_runtime_uses_evidence_skill_for_technical_answers():
    source = (ROOT/'app/ai/rag_service.py').read_text(encoding='utf-8')
    assert 'verdict = verify_live_enterprise_answer(' in source
    assert 'allowed_ids=retrieval.prompt_evidence_ids' in source
    assert 'admitted_evidence=retrieval.evidence' in source


def test_version_partial_snapshot_never_claims_confirmed_removal():
    a = body('Source-only unrelated clause A')
    b = body('Entirely separate clause B') | {'version_id': 26}
    outcome = execute_skill('spec-version-impact', {'old':a,'new':b})
    for finding in outcome['findings']:
        assert finding['change_type'] != 'REMOVED'
        assert finding['change_type'] != 'ADDED'
        if finding['change_type'] == 'UNMATCHED_IN_SUPPLIED_SNAPSHOT':
            assert finding['review_status'] == 'REVIEW_REQUIRED'


def test_relation_rejects_forged_endpoint_document_identity():
    request = relation()
    request['target']['target_document'] = 'forged-version-document'
    with pytest.raises(SkillInputError, match='DOCUMENT_IDENTITY_MISMATCH'):
        execute_skill('content-relation-audit', request)


def test_definitions_export_contains_only_allowlisted_skill_text(tmp_path: Path):
    from zipfile import ZipFile
    from scripts.export_engineering_skills import export
    target = tmp_path/'skills.zip'
    export(target)
    with ZipFile(target) as z:
        names=set(z.namelist())
        assert len(names)==15
        assert 'README.md' in names
        assert not any('password' in n or 'postgres' in n for n in names)
        for slug in SKILLS:
            assert f'{slug}/SKILL.md' in names
            assert f'{slug}/manifest.json' in names
