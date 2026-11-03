"""v1.40 offline, read-only layout and change/revalidation contracts."""
from __future__ import annotations

import hashlib
from dataclasses import replace

from app.ai.agent import EvidenceGovernor
from app.ai.retrieval import EnterpriseRetriever, EvidenceItem
from app.engineering_intelligence.deterministic_read_models import (
    structural_unit_pair, join_reference_and_diff,
)
from app.engineering_intelligence.requirements import normalize_text


def unit(i: int, section: str, text: str, content: int, start: int,
         *, doc='550-spec-body', version_id=9, spec_code='550', file='Bluetooth Audio function spec v2.52.pdf'):
    chunk = ' ' * start + text + '\n'
    return dict(unit_id=i, section_id=section, raw_text=text,
                chunk=chunk, start_offset=start, end_offset=start+len(text),
                document_id=doc, version_id=version_id, content_id=content,
                text_hash=hashlib.sha256(normalize_text(text).encode()).hexdigest(),
                spec_code=spec_code, version_label='2.52', page_number=i, file_name=file)


def sides(a, b):
    def side(u, prefix):
        return {prefix + '_' + k: v for k, v in {
            'text': u['raw_text'], 'chunk': u['chunk'], 'start': u['start_offset'],
            'end': u['end_offset'], 'hash': u['text_hash'], 'document': u['document_id'],
            'version_id': u['version_id'], 'code': u['spec_code'],
            'unit_id': u['unit_id'], 'content_id': u['content_id'], 'section': u['section_id'],
        }.items()}
    return side(a, 'source'), side(b, 'target')


class Fake:
    def __init__(self, rows):
        self.rows = rows
        self.sql = []
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def cursor(self): return self
    def execute(self, sql, params=()): self.sql.append((sql, params))
    def fetchall(self): return self.rows


def retriever(db):
    obj = object.__new__(EnterpriseRetriever)
    obj.top_k = 8
    obj.postgres_profiles = {'21MM': object()}
    obj._connect = lambda scope: db
    obj._specification_lookup = lambda question, scopes: ([EvidenceItem(
        evidence_id='', scope='21MM', source_type='SPEC_LOOKUP', content='',
        specification_code='550')], [])
    return obj


def test_layout_classification_no_speculative_semantics():
    a = unit(1,'4','Parent',1,0)
    b = unit(2,'4.1','Child',2,0)
    x,y=sides(a,b)
    assert structural_unit_pair(x,y) == 'PARENT_CHILD_SECTION'
    assert structural_unit_pair(*sides(a, unit(3,'4.1.2','Grandchild',3,0))) == ''
    assert structural_unit_pair(*sides(b, unit(4,'4.10','Sibling',4,0))) == ''
    assert structural_unit_pair(*sides(a, unit(5,'4','Same section',5,0))) == 'SAME_SECTION'
    assert structural_unit_pair(*sides(a, unit(6,'4','Same chunk',1,7)), consecutive_in_chunk=True) == 'ADJACENT_INDEXED_UNITS'


def test_layout_strict_identity_and_hash_drift():
    a = unit(1,'4.1','源正文 仕様',1,0)
    b = unit(2,'4.1','Two',1,10)
    x,y=sides(a,b)
    assert structural_unit_pair(x,y,consecutive_in_chunk=True)
    for bad in (
        {'target_document': 'coversheet'}, {'target_code':'508'}, {'target_version_id': 777},
        {'target_hash': '0'*64}, {'target_start': 1},
        {'target_section': 'v2.52'}, {'target_unit_id': 1},
    ):
        assert structural_unit_pair(x, {**y, **bad}, consecutive_in_chunk=True) == ''
    assert structural_unit_pair(x, {**y, 'target_content_id': 999}, consecutive_in_chunk=True) == 'SAME_SECTION'


def test_readonly_layout_search_and_bounded_source(monkeypatch):
    a=unit(1,'4','General rule',1,0)
    b=unit(2,'4.1','Mute shall start',2,0)
    c=unit(3,'4.1','Mute shall finish',2,20)
    # Shared chunk for the two §4.1 spans; exact offsets must still verify.
    shared = b['raw_text'] + ' '*(20-len(b['raw_text'])) + c['raw_text']
    b['chunk']=shared; c['chunk']=shared
    b['next_unit_id']=c['unit_id']  # DB-derived over full indexed chunk
    db=Fake([a,b,c]); obj=retriever(db)
    rows=obj._deterministic_layout_relation_search('蓝牙静音', ('21MM',))
    kinds={r.metadata['relation_kind'] for r in rows}
    assert kinds == {'ADJACENT_INDEXED_UNITS','PARENT_CHILD_SECTION'}
    assert all(r.metadata['authority']=='LAYOUT_ONLY' for r in rows)
    assert all('NOT DEPENDENCY' in r.content for r in rows)
    sql='\n'.join(x for x,_ in db.sql)
    assert 'SET TRANSACTION READ ONLY' in sql
    assert 'body_ver.id=ss.current_version_id' in sql
    assert "metadata ->> 'document_role'" in sql
    assert 'LIMIT 121' in sql
    assert all('UPDATE ' not in x.upper() and 'DELETE ' not in x.upper() for x,_ in db.sql)
    assert obj._deterministic_layout_relation_search('Section 4.1 关系', ('21MM',), spec_codes=('550',))


def test_layout_overflow_does_not_assume_adjacency():
    db=Fake([unit(i,'4.1',f'rule {i}',i,0) for i in range(1,122)])
    assert retriever(db)._deterministic_layout_relation_search('section 4.1',('21MM',),spec_codes=('550',)) == []


def reference(*, version='2.52', section='4.1', code='550', doc='new_body', scope='21MM'):
    return EvidenceItem(
        evidence_id='', scope=scope, source_type='STRUCTURAL_REFERENCE',
        content='Refer to Spec 550 section 4.1; original requirement',
        document_id='body508', specification_code='508', specification_version='1.1',
        section_id='2.1', score=.82,
        metadata={'source':{'text_hash':'abc'},
                  'target':{'spec_code':code,'version':version,'section':section,
                            'units':[{'document_id':doc,'text_hash':'tt'}]}})


def diff(*, version='2.52', section='4.1', doc='new_body', code='550', scope='21MM'):
    return EvidenceItem(evidence_id='', scope=scope, source_type='CHAPTER_VERSION_DIFF',
                        content='OLD: 100ms NEW: 30ms', specification_code=code,
                        section_id=section, metadata={'from_version':'2.50','to_version':version,
                            'to_document':doc,'change_id':12,'change_set_id':8,
                            'old_hash':'old','new_hash':'new'})


def test_joint_alert_requires_exact_cross_source_identity():
    r,d=reference(),diff()
    md=join_reference_and_diff(r,d)
    assert md['status']=='REVALIDATION_REQUIRED'
    assert 'NOT_RELATION_INVALIDATION' in md['interpretation_limit']
    for candidate in (
        diff(version='2.51'),diff(doc='coversheet'),diff(code='551'),diff(section='4.2'),
        diff(scope='24MM'),replace(d, source_type='VERSION_HISTORY'),
    ):
        assert join_reference_and_diff(r,candidate) is None
    assert join_reference_and_diff(reference(section='4.1'), diff(section='4.1.2'))
    assert not join_reference_and_diff(reference(section='4.1'), diff(section='4.10'))


def test_join_output_governor_preserves_independent_edges():
    obj=retriever(Fake([]))
    first=reference()
    second=reference(section='5.1')
    second_change=replace(diff(section='5.1'), metadata={**diff(section='5.1').metadata, 'change_id': 13})
    alerts=obj._joint_reference_change_alerts([first, second], [diff(), second_change])
    assert len(alerts)==2
    assert all(a.source_type=='REFERENCE_CHANGE_REVALIDATION' for a in alerts)
    assert all('NOT INVALIDATED' in a.content for a in alerts)
    governed, strength, _ = EvidenceGovernor.govern(tuple(alerts), 8)
    assert len(governed)==2 and strength!='STRONG'


def test_governor_keeps_multiple_pairs_in_same_section():
    def layout(src, tgt):
        return EvidenceItem(evidence_id='', scope='21MM', source_type='STRUCTURAL_LAYOUT',
            content='indexed structure only', specification_code='550', section_id='4.1',
            content_id=1, metadata={'status':'RULE_VERIFIED','relation_kind':'SAME_SECTION',
                                   'source': {'unit_id':src}, 'target': {'unit_id':tgt}})
    items, strength, _ = EvidenceGovernor.govern((layout(1,2), layout(2,3)), 8)
    assert len(items)==2 and strength!='STRONG'


def test_intervening_different_section_blocks_adjacency():
    a=unit(1,'4.1','First',1,0)
    between=unit(2,'4.2','Other',1,10)
    b=unit(3,'4.1','Last',1,20)
    chunk='First'+' '*5+'Other'+' '*5+'Last'
    for row in (a,between,b): row['chunk']=chunk
    results=retriever(Fake([a,between,b]))._deterministic_layout_relation_search(
        '550 的结构关系', ('21MM',), spec_codes=('550',)
    )
    assert len(results)==1
    assert results[0].metadata['relation_kind']=='SAME_SECTION'


def test_rejected_unit_breaks_adjacency_even_with_same_section():
    a=unit(1,'4.1','First',1,0)
    between=unit(2,'4.1','Other',1,10)
    b=unit(3,'4.1','Last',1,20)
    chunk='First'+' '*5+'Other'+' '*5+'Last'
    for row in (a,between,b): row['chunk']=chunk
    between['review_status']='REJECTED'
    results=retriever(Fake([a,between,b]))._deterministic_layout_relation_search(
        '550 的结构关系', ('21MM',), spec_codes=('550',)
    )
    assert not results
