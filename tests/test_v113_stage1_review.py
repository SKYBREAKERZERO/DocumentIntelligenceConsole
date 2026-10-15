"""Stage1 v4 manual provenance review: deterministic synthetic tests, no real DB."""
from __future__ import annotations

from dataclasses import replace
from unittest.mock import patch

import pytest

from app.engineering_intelligence.requirements import extract_requirement_units
from app.engineering_intelligence.stage1_v3 import prepare_stage1_v3
from app.engineering_intelligence.stage1_review import (
    REVIEW_VERSION, prepare_review_proposals, sha, validate_review_decision,
)
from app.engineering_intelligence.review_events import record_review_event
from app.engineering_intelligence.stage1_v4 import (
    _check_existing, _check_stage1_v3_complete, backfill_stage1_v4,
)


SOURCE = ('If BT connects, ECU shall notify Body ECU.\n'
          'Unless region is NA, ECU shall save the profile.\n'
          '| Region | Action |\n| --- | --- |\n| NA | load local data |')


def fixtures():
    units = extract_requirement_units(document_id='doc', content_id=13, section_id='4.2', text=SOURCE)
    summary, atoms, tables = prepare_stage1_v3(version_id=5, units=units)
    p = prepare_review_proposals(version_id=5, units=units, atoms=atoms, tables=tables)
    return units, summary, atoms, tables, p


def decision(p, status='CONFIRMED', assertion=None, target=None):
    validate_review_decision(p, status=status, assertion=assertion,
                             target_unit_key=target, reviewer='reviewer-1', reason='Reviewed source text')


def test_review_proposals_are_source_reproducible_and_unapproved():
    units, summary, atoms, tables, proposals = fixtures()
    assert len(proposals) == 3
    assert proposals == prepare_review_proposals(version_id=5,units=units,atoms=atoms,tables=tables)
    assert len({x.key for x in proposals}) == 3
    assert all(x.evidence_digest == sha(x.payload) for x in proposals)
    assert all(p.payload.get('precedence',p.payload.get('header_assertion')) == 'UNDETERMINED' for p in proposals)
    assert sum(p.kind=='SCOPE' for p in proposals)==2
    with pytest.raises(ValueError,match='proposal budget'):
        prepare_review_proposals(version_id=5,units=units,atoms=atoms,tables=tables,max_proposals=1)


def test_scope_confirmation_requires_correct_exact_roles():
    p = fixtures()[-1]
    conditional = next(x for x in p if x.kind=='SCOPE' and x.payload['condition'])
    exception = next(x for x in p if x.kind=='SCOPE' and x.payload['exception'])
    decision(conditional, assertion='CONDITION_GOVERNS_ACTION')
    decision(exception, assertion='EXCEPTION_OVERRIDES_ACTION')
    with pytest.raises(ValueError,match='Exception/action'):
        decision(conditional, assertion='EXCEPTION_OVERRIDES_ACTION')
    with pytest.raises(ValueError,match='Condition/action'):
        decision(exception, assertion='CONDITION_GOVERNS_ACTION')
    with pytest.raises(ValueError,match='Non-confirming'):
        decision(exception, status='REJECTED', assertion='EXCEPTION_OVERRIDES_ACTION')
    decision(exception,status='REJECTED')
    decision(exception,status='NEEDS_CLARIFICATION')


def test_unresolved_scope_not_confirmable_even_human_review_without_new_evidence():
    src='When A and if B, ECU shall notify.'
    units=extract_requirement_units(document_id='d',content_id=1,section_id='1',text=src)
    _,atoms,tables=prepare_stage1_v3(version_id=1,units=units)
    p,=prepare_review_proposals(version_id=1,units=units,atoms=atoms,tables=tables)
    assert p.payload['status']=='UNRESOLVED'
    with pytest.raises(ValueError,match='Unresolved'):
        decision(p,assertion='CONDITION_GOVERNS_ACTION')


def test_table_header_must_point_to_included_nonempty_source_row():
    table=next(x for x in fixtures()[-1] if x.kind=='TABLE')
    header=table.payload['rows'][0]['unit_key']
    decision(table,assertion='HEADER_ROW_APPLIES',target=header)
    with pytest.raises(ValueError,match='unique source row'):
        decision(table,assertion='HEADER_ROW_APPLIES',target='not-from-this-document')
    with pytest.raises(ValueError,match='explicit header'):
        decision(table,assertion='HEADER_ROW_APPLIES')


def test_table_with_column_mismatch_cannot_be_approved():
    table=next(x for x in fixtures()[-1] if x.kind=='TABLE')
    payload={**table.payload, 'review_reasons':['COLUMN_COUNT_MISMATCH']}
    modified=replace(table,payload=payload,evidence_digest=sha(payload))
    with pytest.raises(ValueError,match='Unsafe table structure'):
        decision(modified,assertion='HEADER_ROW_APPLIES',target=modified.payload['rows'][0]['unit_key'])


def test_mismatched_source_identity_rejected():
    units, _, atoms, tables, _ = fixtures()
    with pytest.raises(ValueError,match='Missing source unit'):
        prepare_review_proposals(version_id=5,units=units[1:],atoms=atoms,tables=tables)


def test_empty_reason_rejected_and_no_automatic_precedence():
    p=fixtures()[-1][0]
    with pytest.raises(ValueError,match='explicit reason'):
        validate_review_decision(p,status='REJECTED',assertion=None,target_unit_key=None,reviewer='x',reason='  ')
    with pytest.raises(ValueError,match='Invalid review status'):
        validate_review_decision(p,status='PENDING',assertion=None,target_unit_key=None,reviewer='x',reason='reason')


class FakeCursor:
    def __init__(self, answers):
        self.answers=list(answers)
        self.last=[]
        self.sql=[]
        self.inserts=[]

    def execute(self,query,params=None):
        self.sql.append((' '.join(query.split()),params))
        self.last=self.answers.pop(0) if self.answers else []

    def fetchone(self):
        if isinstance(self.last,list):
            return self.last.pop(0) if self.last else None
        return self.last

    def fetchall(self):
        if isinstance(self.last,list):
            rows=self.last
            self.last=[]
            return rows
        return []

    def executemany(self,sql,values):
        self.inserts.append((sql,values))


def test_v3_receipt_blocks_missing_or_mismatched_v4_backfill():
    _,summary,atoms,tables,_=fixtures()
    with pytest.raises(ValueError,match='v3 receipt'):
        _check_stage1_v3_complete(FakeCursor([[]]),version_id=5,source_digest=summary.source_digest,
                                 unit_count=summary.source_units,atoms_count=len(atoms),tables_count=len(tables))
    with pytest.raises(ValueError,match='v3 receipt'):
        _check_stage1_v3_complete(FakeCursor([[('0'*64,summary.source_units,len(atoms),len(tables),'stage1-structure-v1')]]),
            version_id=5,source_digest=summary.source_digest,unit_count=summary.source_units,
            atoms_count=len(atoms),tables_count=len(tables))


def test_existing_review_proposal_drift_is_not_overwritten():
    p=fixtures()[-1]
    row=(p[0].key,p[0].kind,p[0].source_key,REVIEW_VERSION,'0'*64,p[0].source_text_digest,p[0].payload)
    with pytest.raises(ValueError,match='drift'):
        _check_existing(FakeCursor([[row]]),version_id=5,proposals=p)


def test_v4_preview_never_inserts_and_apply_is_additive():
    units,_,atoms,tables,p=fixtures()
    base_patches=(patch('app.engineering_intelligence.stage1_v4.require_complete_backfill'),
                  patch('app.engineering_intelligence.stage1_v4.require_stage1_v3_schema'),
                  patch('app.engineering_intelligence.stage1_v4.verify_live_source_snapshot'),
                  patch('app.engineering_intelligence.stage1_v4._check_stage1_v3_complete'),
                  patch('app.engineering_intelligence.stage1_v4._verify_existing_derived'),
                  patch('app.engineering_intelligence.stage1_v4.load_version_units',return_value=units))
    from contextlib import ExitStack
    with ExitStack() as stack:
        for ctx in base_patches: stack.enter_context(ctx)
        preview=FakeCursor([])
        result=backfill_stage1_v4(preview,version_id=5,dry_run=True)
        assert result.scope_proposals==2 and result.table_proposals==1
        assert not preview.inserts and not any('INSERT' in s for s,_ in preview.sql)
        with patch('app.engineering_intelligence.stage1_v4.require_stage1_v4_schema'):
            # lock, old rows, count, receipt check
            applied=FakeCursor([[],[],[(len(p),)],[],[(result.source_digest,result.proposal_digest,len(units),len(p),REVIEW_VERSION)]])
            out=backfill_stage1_v4(applied,version_id=5,dry_run=False)
            assert out == result and len(applied.inserts)==1
            writes='\n'.join(s for s,_ in applied.sql)+'\n'+applied.inserts[0][0]
            assert 'ei_stage1_review_proposals' in writes and 'ei_stage1_review_receipts' in writes
            assert 'UPDATE public.ei_requirement_units' not in writes
            assert 'DELETE FROM' not in writes


def event_cursor(proposal, *, latest=None, drift=False):
    row=(proposal.kind,proposal.source_key,REVIEW_VERSION,
         '0'*64 if drift else proposal.evidence_digest,
         proposal.source_text_digest,proposal.payload)
    return FakeCursor([[row],[('x'*64,'y'*64,2,3,REVIEW_VERSION)],[('x'*64,) ],[(3,) ],[(latest,)] if latest else [],[(44,)]])


def call_event(cur,proposal,**overrides):
    opts=dict(version_id=5,project='21MM',proposal_key=proposal.key,
              expected_evidence_digest=proposal.evidence_digest,expected_last_event_id=0,
              request_id='123e4567-e89b-12d3-a456-426614174000',reviewer='r1',
              reason='Confirmed with source',status='CONFIRMED',
              assertion='CONDITION_GOVERNS_ACTION')
    opts.update(overrides)
    with patch('app.engineering_intelligence.stage1_v3.verify_live_source_snapshot'):
        return record_review_event(cur,**opts)


def test_review_event_atomic_append_and_expected_identity():
    p=next(x for x in fixtures()[-1] if x.kind=='SCOPE' and x.payload['condition'])
    c=event_cursor(p)
    assert call_event(c,p)==44
    inserted=[(q,params) for q,params in c.sql if q.startswith('INSERT INTO')]
    assert len(inserted)==1 and inserted[0][1][1]==p.key
    assert 'FOR UPDATE OF p' in c.sql[0][0]


def test_review_event_concurrency_and_digest_mismatch_fail_closed():
    p=next(x for x in fixtures()[-1] if x.kind=='SCOPE' and x.payload['condition'])
    with pytest.raises(ValueError,match='conflict'):
        call_event(event_cursor(p,latest=13),p)
    with pytest.raises(ValueError,match='drift'):
        call_event(event_cursor(p,drift=True),p)
    with pytest.raises(ValueError,match='SHA256'):
        call_event(event_cursor(p),p,expected_evidence_digest='not-a-digest')


def test_review_event_denies_cross_project_or_bad_source():
    p=next(x for x in fixtures()[-1] if x.kind=='SCOPE' and x.payload['condition'])
    with pytest.raises(ValueError,match='authorized'):
        call_event(FakeCursor([[]]),p)
    with pytest.raises(ValueError,match='Invalid review version/project'):
        call_event(FakeCursor([]),p,project='COMMON')
    with pytest.raises(ValueError,match='source'):
        with patch('app.engineering_intelligence.stage1_v3.verify_live_source_snapshot',side_effect=ValueError('source drift')):
            record_review_event(event_cursor(p),version_id=5,project='21MM',proposal_key=p.key,
               expected_evidence_digest=p.evidence_digest,expected_last_event_id=0,
               request_id='123e4567-e89b-12d3-a456-426614174000',reviewer='r1',
               reason='source verified',status='CONFIRMED',assertion='CONDITION_GOVERNS_ACTION')


def test_new_migration_has_append_only_guards_and_no_old_table_mutations():
    from pathlib import Path
    sql=(Path(__file__).resolve().parents[1] / 'database/upgrade_engineering_stage1_v4.sql').read_text()
    assert 'ei_stage1_review_events' in sql and 'ei_stage1_review_receipts' in sql
    assert 'BEFORE UPDATE OR DELETE' in sql
    assert 'UPDATE public.ei_requirement_units' not in sql
    assert 'DROP TABLE' not in sql
