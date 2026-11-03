"""v1.41 negative contracts: body selection, filtered adjacency, full index receipt."""
from __future__ import annotations

import hashlib

import pytest

from app.ai.retrieval import EnterpriseRetriever
from app.engineering_intelligence import relation_index_integrity as idx
from app.engineering_intelligence.requirements import normalize_text
from app.storage.primary_document_policy import PrimarySpecificationDocumentPolicy as Policy
from test_v139_structural_reference_and_chapter_diff import (
    FakeDB, retriever, source, target,
)
from test_v140_layout_and_relation_change_join import (
    Fake, retriever as layout_retriever, unit,
)


class AuditCursor:
    def __init__(self):
        self.sql = []
        self.body = [(90, 105, 'spec-body-v2.52', '2.52',
                      'Bluetooth Audio function spec v2.52.pdf', {'document_role': 'SPEC_BODY'})]
        self.contents = [(7, '4.1', 3, 'Mute shall end within 30ms.\n'),
                         (8, '4.2', 4, 'Unindexed heading')]
        raw = 'Mute shall end within 30ms.'
        self.units = [(32, 'spec-body-v2.52', 105, 7, 0, len(raw), raw,
                       hashlib.sha256(normalize_text(raw).encode()).hexdigest(),
                       'AUTO_EXTRACTED', self.contents[0][3])]
        self.receipt = self._receipt()
        self.rows = []

    def _receipt(self):
        digest = hashlib.sha256()
        for content in self.contents:
            idx._fingerprint_add(digest, *content)
        return (len(self.contents), len(self.units), max(r[0] for r in self.contents), digest.hexdigest())

    def execute(self, sql, params=()):
        self.sql.append((sql, params))
        if 'FROM public.specifications sp' in sql:
            self.rows = self.body
        elif 'SELECT source_chunk_count,source_unit_count' in sql:
            self.rows = [self.receipt]
        elif 'FROM public.contents' in sql:
            self.rows = [r for r in self.contents if r[0] > params[1]][:256]
        elif 'FROM public.ei_requirement_units u' in sql:
            self.rows = [r for r in self.units if r[0] > params[1]][:256]
        else:
            raise AssertionError(sql)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


def audit(monkeypatch, db):
    monkeypatch.setattr(idx, 'require_complete_backfill', lambda cursor, vid: None)
    return idx.inspect_relation_index(db, project='21MM', spec_code='550')


def test_body_preferred_even_when_current_pointer_is_coversheet():
    sql = Policy.sql_preferred_body_version_id('sp')
    assert 'body_ver.id=sp.current_version_id' in sql
    assert 'JOIN public.documents body_doc' in sql
    assert "body_ver.branch_name='MAIN'" in sql
    assert 'document_role' in sql and 'cover' in sql.lower()
    assert 'ORDER BY CASE' in sql
    with pytest.raises(ValueError):
        Policy.sql_preferred_body_version_id('sp;DROP TABLE public.documents')


def test_citation_rejected_source_and_target_fail_closed(monkeypatch):
    db=FakeDB(sources=[{**source(), 'source_review_status':'REJECTED'}], targets=[target()])
    obj=retriever(monkeypatch, db)
    assert obj._deterministic_section_reference_search('508 550', ('21MM',), spec_codes=('508', '550')) == []
    db=FakeDB(sources=[source()],targets=[{**target(), 'target_review_status':'REJECTED'}])
    obj=retriever(monkeypatch, db)
    assert obj._deterministic_section_reference_search('508 550', ('21MM',), spec_codes=('508', '550')) == []


def test_source_and_target_queries_choose_body_without_current_pointer(monkeypatch):
    db=FakeDB(sources=[source()],targets=[target()]); obj=retriever(monkeypatch,db)
    assert obj._deterministic_section_reference_search('508 550', ('21MM',), spec_codes=('508','550'))
    statements='\n'.join(x for x,_ in db.statements)
    assert 'sv.id=(' in statements and 'tv.id=(' in statements
    assert 'sv.id=ss.current_version_id' not in statements
    assert 'tv.id=ts.current_version_id' not in statements
    assert 'su.review_status AS source_review_status' in statements
    assert 'tu.review_status AS target_review_status' in statements


def test_filtered_chapter_cannot_fake_adjacency():
    a=unit(1,'4.1','First',1,0)
    b=unit(3,'4.1','Last',1,20)
    chunk='First'+' '*15+'Last'
    for r in (a,b): r['chunk']=chunk
    # Actual unit 2 (§4.2), omitted by the SQL §4.1 filter, lies between.
    a['next_unit_id']=2
    result=layout_retriever(Fake([a,b]))._deterministic_layout_relation_search(
        '550 section 4.1', ('21MM',), spec_codes=('550',))
    assert len(result)==1
    assert result[0].metadata['relation_kind']=='SAME_SECTION'
    # Without database proof the caller must never synthesize adjacent links.
    a['next_unit_id']=None
    assert all(r.metadata['relation_kind']!='ADJACENT_INDEXED_UNITS' for r in
               layout_retriever(Fake([a,b]))._deterministic_layout_relation_search(
                   '550 section 4.1',('21MM',),spec_codes=('550',)))


def test_layout_sql_adjacency_uses_all_indexed_units():
    db=Fake([])
    obj=layout_retriever(db)
    assert not obj._deterministic_layout_relation_search('550 section 4.1',('21MM',),spec_codes=('550',))
    sql='\n'.join(x for x,_ in db.sql)
    assert 'LEFT JOIN LATERAL' in sql
    assert 'following.section_id=' not in sql
    assert 'following.review_status=' not in sql
    assert 'following.content_id=su.content_id' in sql
    assert 'next_indexed.next_unit_id' in sql
    assert 'sv.id=(' in sql


def test_index_audit_accepts_full_digest_and_all_spans(monkeypatch):
    db=AuditCursor(); result=audit(monkeypatch,db)
    assert result['status']=='PASS'
    assert result['source_chunks']==2 and result['indexed_units']==1
    assert result['production_acceptance']=='DATABASE_SNAPSHOT_ONLY'
    assert all(not sql.lstrip().upper().startswith(('UPDATE ','DELETE ','INSERT ')) for sql,_ in db.sql)
    assert any('ORDER BY id LIMIT 256' in sql for sql,_ in db.sql)


@pytest.mark.parametrize('mutate,reason', [
    (lambda d: setattr(d,'body',[]),'BODY_VERSION_MISSING_OR_AMBIGUOUS'),
    (lambda d: setattr(d,'body',d.body*2),'BODY_VERSION_MISSING_OR_AMBIGUOUS'),
    (lambda d: setattr(d,'body',[(90,105,'spec-body-v2.52','2.52',
                                 'Bluetooth Audio Cover Sheet.pdf',{'document_role':'SPEC_BODY'})]),
     'NON_PRIMARY_DOCUMENT'),
    (lambda d: setattr(d,'receipt',(2,1,8,'0'*64)),'SOURCE_DIGEST_OR_CHUNK_MISMATCH'),
    (lambda d: setattr(d,'units',[(r[0],r[1],r[2],r[3],r[4],r[5],r[6]+'!',r[7],r[8],r[9])
                               for r in d.units]),'UNIT_SOURCE_SPAN_OR_HASH_MISMATCH'),
    (lambda d: setattr(d,'units',[]),'UNIT_COUNT_MISMATCH'),
    (lambda d: setattr(d,'receipt',(2,50001,8,d.receipt[3])),'RESOURCE_LIMIT_EXCEEDED'),
])
def test_index_audit_negative_cases(monkeypatch, mutate, reason):
    db=AuditCursor();mutate(db);result=audit(monkeypatch,db)
    assert result['status']=='BLOCKED' and reason in result['issues']


def test_missing_backfill_receipt_blocks(monkeypatch):
    def fail(*_): raise ValueError('receipt missing')
    monkeypatch.setattr(idx,'require_complete_backfill',fail)
    result=idx.inspect_relation_index(AuditCursor(),project='21MM',spec_code='550')
    assert result['status']=='BLOCKED'
    assert 'INCOMPLETE_BACKFILL_RECEIPT:ValueError' in result['issues']


def test_index_audit_invalid_scope_rejected():
    with pytest.raises(ValueError):
        idx.inspect_relation_index(AuditCursor(),project='QABUG',spec_code='550;')
    with pytest.raises(ValueError):
        idx.inspect_relation_index(AuditCursor(),project='21MM',spec_code='550',max_units=0)
