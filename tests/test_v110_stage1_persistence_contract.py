"""Fail-closed Stage1 v3 schema/backfill contracts, no live DB required."""
from __future__ import annotations
import hashlib
import json
from dataclasses import asdict
from unittest.mock import patch

import pytest

from app.engineering_intelligence.requirements import extract_requirement_units
from app.engineering_intelligence.stage1_v3 import (
    backfill_stage1_v3, prepare_stage1_v3, verify_live_source_snapshot,
    _verify_existing_derived,
)


class Cursor:
    def __init__(self, select_results):
        self.responses=list(select_results)
        self.current=None
        self.sql=[]
        self.executed=[]

    def execute(self, query, params=None):
        self.sql.append((' '.join(query.split()), params))
        self.current=[] if 'pg_advisory_xact_lock' in query else (self.responses.pop(0) if self.responses else [])

    def fetchall(self):
        return self.current

    def fetchone(self):
        if not self.current: return None
        return self.current.pop(0)

    def executemany(self, query, rows):
        self.executed.append((query, rows))


def prepared():
    units=extract_requirement_units(document_id='doc', content_id=3, section_id='1',
          text='ECU shall load; ECU must save\n|H|Q|\n|---|---|\n|A|B|')
    summary,atoms,tables=prepare_stage1_v3(version_id=4, units=units)
    return units, summary, atoms, tables


def source_hash(content_id,section,page,body):
    h=hashlib.sha256()
    for part in (str(content_id),str(section),str(page),body):
        raw=part.encode('utf-8','surrogatepass')
        h.update(len(raw).to_bytes(8,'big'));h.update(raw)
    return h.hexdigest()


def test_live_snapshot_detects_content_mutation_at_same_row_count():
    original='ECU shall save'
    expected=source_hash(3,'4',None,original)
    cur=Cursor([[('doc',expected)], [(3,'4',None,original)], []])
    verify_live_source_snapshot(cur,version_id=4)
    wrong=Cursor([[('doc',expected)], [(3,'4',None,'ECU shall load')], []])
    with pytest.raises(ValueError,match='source differs'):
        verify_live_source_snapshot(wrong,version_id=4)


def test_live_snapshot_fails_on_row_cap_and_missing_receipt():
    cur=Cursor([[('doc','0'*64)],[(1,'a',None,'text')],[(2,)]])
    with pytest.raises(ValueError, match='row budget'):
        verify_live_source_snapshot(cur,version_id=4,max_rows=1)
    with pytest.raises(ValueError,match='receipt'):
        verify_live_source_snapshot(Cursor([[]]),version_id=4)


def test_idempotent_recheck_accepts_exact_rows_and_approved_status_untouched():
    _, summary, atoms, tables=prepared()
    rows=[(a.candidate_key,a.text_hash,a.text,a.start_in_unit,a.end_in_unit,
           a.start_in_content,a.end_in_content,a.role) for a in atoms]
    table_rows=[(t.table_key,t.header_status,
                 json.loads(json.dumps([asdict(r) for r in t.rows])),
                 list(t.review_reasons)) for t in tables]
    cur=Cursor([rows,table_rows])
    _verify_existing_derived(cur,summary.version_id,atoms,tables)
    assert not cur.executed


def test_idempotent_recheck_rejects_tampered_evidence():
    _,summary,atoms,tables=prepared()
    bad=[(atoms[0].candidate_key,'0'*64,atoms[0].text,atoms[0].start_in_unit,
         atoms[0].end_in_unit,atoms[0].start_in_content,atoms[0].end_in_content,atoms[0].role)]
    with pytest.raises(ValueError, match='drift'):
        _verify_existing_derived(Cursor([bad,[]]),summary.version_id,atoms,tables)


def test_dry_run_does_not_persist():
    source,*_=prepared()
    cur=Cursor([])
    with (patch('app.engineering_intelligence.stage1_v3.require_complete_backfill'),
          patch('app.engineering_intelligence.stage1_v3.verify_live_source_snapshot'),
          patch('app.engineering_intelligence.stage1_v3.load_version_units',return_value=source)):
        result=backfill_stage1_v3(cur,version_id=4,dry_run=True)
    assert result.source_units==4
    assert not cur.executed
    assert not any('INSERT' in sql for sql,_ in cur.sql)


def test_apply_only_inserts_derived_and_keeps_source_tables_unchanged():
    source,*_=prepared()
    summary,atoms,tables=prepare_stage1_v3(version_id=4,units=source)
    cur=Cursor([[], [], [(len(atoms),)], [(len(tables),)], [(4,)]])
    with (patch('app.engineering_intelligence.stage1_v3.require_complete_backfill'),
          patch('app.engineering_intelligence.stage1_v3.require_stage1_v3_schema'),
          patch('app.engineering_intelligence.stage1_v3.verify_live_source_snapshot'),
          patch('app.engineering_intelligence.stage1_v3.load_version_units',return_value=source)):
        result=backfill_stage1_v3(cur,version_id=4,dry_run=False)
    assert result==summary
    assert len(cur.executed)==2
    writes='\n'.join(x[0] for x in cur.executed)+'\n'+ '\n'.join(s for s,_ in cur.sql)
    assert 'INSERT INTO public.ei_atomic_candidates' in writes
    assert 'INSERT INTO public.ei_table_evidence' in writes
    assert 'INSERT INTO public.ei_stage1_v3_receipts' in writes
    assert 'UPDATE public.ei_requirement_units' not in writes
    assert 'DELETE FROM' not in writes


def test_apply_partial_count_mismatch_fails_before_receipt():
    source,*_=prepared()
    cur=Cursor([[], [], [(0,)], [(0,)]])
    with (patch('app.engineering_intelligence.stage1_v3.require_complete_backfill'),
          patch('app.engineering_intelligence.stage1_v3.require_stage1_v3_schema'),
          patch('app.engineering_intelligence.stage1_v3.verify_live_source_snapshot'),
          patch('app.engineering_intelligence.stage1_v3.load_version_units',return_value=source)):
        with pytest.raises(ValueError,match='count differs'):
            backfill_stage1_v3(cur,version_id=4,dry_run=False)
    assert not any('ei_stage1_v3_receipts' in s for s,_ in cur.sql)
