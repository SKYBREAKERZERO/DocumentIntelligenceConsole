"""Stage 3 v1.42 read-only audit, source-integrity and negative contracts."""
from __future__ import annotations

import hashlib
from copy import deepcopy

import pytest

from app.engineering_intelligence.relation_temporal_audit import (
    assess_temporal_relation, TemporalAuditError,
)
from app.engineering_intelligence.requirements import normalize_text


def proof(text: str, prefix: str):
    chunk = '§' + text + 'END'
    return {f'{prefix}_text': text, f'{prefix}_chunk': chunk,
            f'{prefix}_start': 1, f'{prefix}_end': 1 + len(text),
            f'{prefix}_hash': hashlib.sha256(normalize_text(text).encode()).hexdigest()}


class DB:
    def __init__(self):
        self.queries = []
        self.old = dict(
            id=31, relation_status='CONFIRMED', relation_type='REFERENCES_SECTION',
            source_unit_id=14, source_spec_id=5, source_code='508', source_project='21MM',
            source_version_id=101, source_version='1.0', source_branch='MAIN',
            source_document='s508', source_version_document='s508', source_file='508 body.pdf',
            source_metadata={'document_role':'SPEC_BODY'}, source_review_status='APPROVED',
            source_section='3.1',
            old_unit_id=21, target_spec_id=9, target_code='550', target_project='21MM',
            old_version_id=202, old_version='2.50', old_version_seq=3, old_branch='MAIN',
            old_document='s550-old', old_version_document='s550-old', old_file='550 body v2.50.pdf',
            old_metadata={'document_role':'SPEC_BODY'}, old_review_status='APPROVED',
            old_section='4.1',
            **proof('Refer to Spec 550 section 4.1', 'source'),
            **proof('Mute <= 30ms', 'old'))
        self.new = dict(id=303, specification_id=9, document_id='s550-new',
                        branch_name='MAIN', version_label='2.52', version_seq=4,
                        file_name='550 body v2.52.pdf', metadata={'document_role':'SPEC_BODY'})
        self.receipts = [dict(id=id, document_id=doc, receipt_document=doc,
                    source_chunk_count=1, source_unit_count=1, max_content_id=100,
                    source_digest='a'*64, actual_chunks=1, actual_units=1,
                    actual_max_content=100)
                    for id,doc in ((101,'s508'),(202,'s550-old'),(303,'s550-new'))]
        self.changes = [dict(id=7, is_adjacent=True, algorithm_version='section-structural-v1')]
        self.maps = [dict(id=8, change_type='UNCHANGED', review_status='DETERMINISTIC',
                          old_unit_id=21, new_unit_id=22, new_spec_id=9, new_version_id=303,
                          new_document='s550-new', new_review_status='APPROVED', new_section='4.1',
                          **proof('Mute <= 30ms','new'))]
    def execute(self, statement, params=()):
        self.queries.append((statement, params))
        if 'FROM public.ei_content_relations r' in statement:
            self.rows = [self.old]
        elif 'FROM public.specification_versions nv' in statement:
            self.rows = [self.new]
        elif 'FROM public.specification_versions v' in statement:
            self.rows = self.receipts
        elif 'FROM public.specification_change_sets' in statement:
            self.rows = self.changes
        elif 'FROM public.ei_version_mappings' in statement:
            self.rows = self.maps
        else:
            raise AssertionError('Unexpected SQL')
    def fetchall(self):
        return deepcopy(self.rows)


def audit(db=None, **kwargs):
    return assess_temporal_relation(db or DB(), project='21MM', relation_id=31,
                                    new_target_version_id=303, **kwargs)


def test_verified_text_continuity_is_not_relation_validity():
    db=DB(); result=audit(db)
    assert result['status'] == 'TARGET_TEXT_CONTINUITY_VERIFIED'
    assert result['relation_validity'] == 'UNDETERMINED'
    assert result['human_revalidation_required'] is True
    assert result['source_version_alignment'] == 'NOT_ESTABLISHED'
    assert result['comparison_is_adjacent'] is True
    assert result['old_target_hash'] == result['new_target_hash']
    assert len(db.queries) == 5
    assert result['receipt_identity_and_count_check'] == 'PASS'
    assert result['full_source_digest_check'] == 'NOT_PERFORMED'
    text='\n'.join(x for x,_ in db.queries)
    assert "status='READY'" in text and "branch_name='MAIN'" in text
    assert "JOIN public.contents" in text and "project_code=%s" in text
    assert 'LIMIT 3' in text and 'LIMIT 2' in text
    assert all(not q.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE', 'ALTER', 'CREATE')) for q,_ in db.queries)
    assert db.queries[0][1] == (31, '21MM', '21MM')


@pytest.mark.parametrize('variant, expected', [
    ('MODIFIED', 'TARGET_CHANGED_REVIEW_REQUIRED'),
    ('MOVED', 'TARGET_MOVED_REVIEW_REQUIRED'),
    ('REMOVED', 'TARGET_REMOVED_REVIEW_REQUIRED'),
])
def test_supported_change_types_still_require_revalidation(variant, expected):
    db=DB(); m=db.maps[0]; m['change_type']=variant
    if variant == 'MODIFIED':
        m.update(proof('Mute <= 20ms','new'))
    if variant == 'REMOVED':
        m['new_unit_id']=None
    result=audit(db)
    assert result['status']==expected
    assert result['relation_validity']=='UNDETERMINED'


@pytest.mark.parametrize('mutate,reason', [
    (lambda db: db.old.update(relation_status='REVIEW_REQUIRED'), 'RELATION_NOT_CONFIRMED'),
    (lambda db: db.old.update(source_branch='QA'), 'OLD_ENDPOINT_IDENTITY_OR_SOURCE_HASH_INVALID'),
    (lambda db: db.old.update(old_document='Cover sheet'), 'OLD_ENDPOINT_IDENTITY_OR_SOURCE_HASH_INVALID'),
    (lambda db: db.old.update(old_file='Bluetooth Audio Coversheet.pdf'), 'OLD_ENDPOINT_IDENTITY_OR_SOURCE_HASH_INVALID'),
    (lambda db: db.old.update(source_hash='0'*64), 'OLD_ENDPOINT_IDENTITY_OR_SOURCE_HASH_INVALID'),
    (lambda db: db.old.update(old_start=0), 'OLD_ENDPOINT_IDENTITY_OR_SOURCE_HASH_INVALID'),
    (lambda db: db.old.update(old_review_status='REJECTED'), 'OLD_ENDPOINT_IDENTITY_OR_SOURCE_HASH_INVALID'),
    (lambda db: db.new.update(specification_id=88), 'NEW_SPEC_BODY_VERSION_NOT_FOUND'),
    (lambda db: db.new.update(file_name='550 Cover Sheet.pdf'), 'NEW_VERSION_NOT_LATER_MAIN_SPEC_BODY'),
    (lambda db: db.new.update(version_seq=3), 'NEW_VERSION_NOT_LATER_MAIN_SPEC_BODY'),
    (lambda db: db.new.update(branch_name='BRANCH_A'), 'NEW_VERSION_NOT_LATER_MAIN_SPEC_BODY'),
    (lambda db: setattr(db,'receipts',[]), 'VERSION_BACKFILL_RECEIPT_MISSING_OR_INCOMPLETE'),
    (lambda db: db.receipts[0].update(actual_units=0), 'VERSION_BACKFILL_RECEIPT_MISSING_OR_INCOMPLETE'),
    (lambda db: db.receipts[1].update(source_digest=''), 'VERSION_BACKFILL_RECEIPT_MISSING_OR_INCOMPLETE'),
    (lambda db: setattr(db,'changes',[]), 'READY_VERSION_PAIR_NOT_UNIQUE_OR_MISSING'),
    (lambda db: setattr(db,'changes',db.changes * 2), 'READY_VERSION_PAIR_NOT_UNIQUE_OR_MISSING'),
    (lambda db: setattr(db,'maps',[]), 'UNMAPPED_OR_AMBIGUOUS_OLD_REQUIREMENT'),
    (lambda db: setattr(db,'maps',db.maps * 2), 'UNMAPPED_OR_AMBIGUOUS_OLD_REQUIREMENT'),
    (lambda db: db.maps[0].update(review_status='REVIEW_REQUIRED'), 'REQUIREMENT_MAPPING_REVIEW_REQUIRED'),
    (lambda db: db.maps[0].update(change_type='ADDED'), 'INCONSISTENT_MAPPING_KIND'),
    (lambda db: db.maps[0].update(new_spec_id=10), 'NEW_REQUIREMENT_ENDPOINT_OR_HASH_INVALID'),
    (lambda db: db.maps[0].update(new_document='other-body'), 'NEW_REQUIREMENT_ENDPOINT_OR_HASH_INVALID'),
    (lambda db: db.maps[0].update(new_review_status='REJECTED'), 'NEW_REQUIREMENT_ENDPOINT_OR_HASH_INVALID'),
    (lambda db: db.maps[0].update(new_hash='f'*64), 'NEW_REQUIREMENT_ENDPOINT_OR_HASH_INVALID'),
    (lambda db: db.maps[0].update(new_start=2), 'NEW_REQUIREMENT_ENDPOINT_OR_HASH_INVALID'),
    (lambda db: db.maps[0].update(change_type='MODIFIED'), 'MODIFIED_MAPPING_HAS_IDENTICAL_TEXT'),
    (lambda db: db.maps[0].update(change_type='MOVED', **proof('Changed','new')), 'MOVED_MAPPING_HAS_CHANGED_TEXT'),
    (lambda db: db.maps[0].update(new_unit_id=None), 'NEW_REQUIREMENT_ENDPOINT_OR_HASH_INVALID'),
    (lambda db: db.maps[0].update(change_type='REMOVED'), 'INCONSISTENT_REMOVAL_ENDPOINT'),
    (lambda db: db.maps[0].update(**proof('Changed','new')), 'UNCHANGED_MAPPING_HAS_CHANGED_TEXT'),
])
def test_fail_closed_boundaries(mutate, reason):
    db=DB();mutate(db)
    # A fake DB does not apply WHERE predicates; the audit must reject bad
    # identity even when the mocked query returns a row unexpectedly.
    if db.new['specification_id'] != 9:
        db.new['specification_id']=9
        db.newrows_missing=True
        class MissingVersionDB(DB):
            pass
        original=db.fetchall
        def missing():
            if 'FROM public.specification_versions nv' in db.queries[-1][0]:
                return []
            return original()
        db.fetchall=missing
    result=audit(db)
    assert result['status']=='INSUFFICIENT_EVIDENCE'
    assert reason in result['issues']


def test_missing_confirmed_body_edge():
    db=DB(); old=db.fetchall
    def missing():
        if 'FROM public.ei_content_relations r' in db.queries[-1][0]:
            return []
        return old()
    db.fetchall=missing
    result=audit(db)
    assert 'CONFIRMED_BODY_RELATION_NOT_UNIQUELY_FOUND' in result['issues']


@pytest.mark.parametrize('project, rid, vid', [('QABUG',31,303), ('21MM',0,303),
                ('21MM',31,0), ('21MM;DROP',31,303), ('21MM',True,303)])
def test_invalid_identity_rejected_before_query(project,rid,vid):
    with pytest.raises(TemporalAuditError):
        assess_temporal_relation(DB(),project=project,relation_id=rid,new_target_version_id=vid)
