"""v1.39 focused offline contracts. Real PostgreSQL/GUI not asserted here."""
from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from app.ai.agent import EvidenceGovernor
from app.ai.retrieval import EnterpriseRetriever, EvidenceItem
from app.engineering_intelligence.deterministic_read_models import (
    keyword_selector, matches_change_selector, section_selector,
    verify_persisted_diff, verify_requirement_span, verify_section_reference,
)
from app.engineering_intelligence.requirements import normalize_text

ROOT = Path(__file__).resolve().parents[1]


def mk_span(prefix: str, literal: str, doc: str, *, start: str) -> dict:
    chunk = '前' + literal + '後'
    return {
        f'{prefix}_document': doc, f'{prefix}_chunk': chunk,
        f'{prefix}_start': 1, f'{prefix}_end': len(chunk) - 1,
        f'{prefix}_text': literal,
        f'{prefix}_hash': hashlib.sha256(normalize_text(literal).encode()).hexdigest(),
        f'{prefix}_content_id': 20 if prefix == 'source' else 30,
        f'{prefix}_section': start,
        f'{prefix}_page': 2,
    }


def source():
    return {
        **mk_span('source', 'Refer to Spec 550 section 4.1 for mute timing.', 'body508', start='2.1'),
        'source_unit_id': 3, 'source_code': '508', 'source_version': '1.1',
        'source_file': '508 body.pdf',
    }


def target(text='Mute signal shall transition within 30ms.'):
    return {
        **mk_span('target', text, 'body550', start='4.1'),
        'target_code': '550', 'target_version': '2.52',
        'target_file': 'Bluetooth Audio function spec v2.52.pdf',
    }


class FakeDB:
    def __init__(self, sources=(), targets=(), changesets=(), changes=()):
        self.sources = sources
        self.targets = targets
        self.changesets = changesets
        self.changes = changes
        self.rows = []
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self):
        return self

    def execute(self, sql, params=()):
        self.statements.append((sql, params))
        if 'FROM public.ei_requirement_units su' in sql:
            self.rows = self.sources
        elif 'FROM public.ei_requirement_units tu' in sql:
            self.rows = self.targets
        elif 'FROM public.specification_change_sets cs' in sql:
            self.rows = self.changesets
        elif 'FROM public.specification_changes' in sql:
            self.rows = self.changes
        else:
            self.rows = []

    def fetchall(self):
        return self.rows


def retriever(monkeypatch, db, code='550'):
    obj = object.__new__(EnterpriseRetriever)
    obj.top_k = 8
    obj.postgres_profiles = {'21MM': object()}
    monkeypatch.setattr(obj, '_connect', lambda scope: db)
    monkeypatch.setattr(obj, '_specification_lookup', lambda q, scopes: (
        [EvidenceItem(evidence_id='', scope='21MM', source_type='SPEC_LOOKUP', content='',
                      specification_code=code, metadata={'specification_id': 90})], []
    ))
    return obj


def change(**kwargs):
    data = {
        'id': 10, 'change_type': 'MODIFIED',
        'chapter_id': '4', 'chapter_title': 'Bluetooth', 'section_id': '4.1',
        'section_title': 'Mute timing',
        'old_text': 'Mute within 100ms', 'new_text': 'Mute within 30ms',
        'old_page_number': 2, 'new_page_number': 3,
    }
    data.update(kwargs)
    data['old_hash'] = hashlib.sha256(str(data['old_text']).encode()).hexdigest() if data['old_text'] is not None else None
    data['new_hash'] = hashlib.sha256(str(data['new_text']).encode()).hexdigest() if data['new_text'] is not None else None
    return data


def changeset(**kwargs):
    data = dict(change_set_id=7, spec_code='550', from_version='2.50', to_version='2.52',
                from_document='old_body', to_document='new_body')
    data.update(kwargs)
    return data


def test_span_offsets_and_normalized_hash_no_fuzzy_substring():
    s = source()
    assert verify_requirement_span(s, 'source')
    assert not verify_requirement_span({**s, 'source_start': 0}, 'source')
    assert not verify_requirement_span({**s, 'source_hash': '0' * 64}, 'source')
    assert not verify_requirement_span({**s, 'source_text': s['source_text'] + '!'}, 'source')
    assert verify_section_reference(s, target())
    assert not verify_section_reference(s, {**target(), 'target_section': '4.2'})
    assert not verify_section_reference({**s, 'source_text': 'Refer to Spec 550. Section 4.1.'}, target())


def test_section_reference_online_with_multiple_target_requirements(monkeypatch):
    t2 = target('When mute is requested, persist the state.')
    db = FakeDB(sources=[source()], targets=[target(), t2])
    result = retriever(monkeypatch, db)._deterministic_section_reference_search(
        '508 和 550 的章节引用', ('21MM',), spec_codes=('508', '550'),
    )
    assert len(result) == 1
    item = result[0]
    assert item.source_type == 'STRUCTURAL_REFERENCE'
    assert item.metadata['status'] == 'RULE_VERIFIED'
    assert item.metadata['target']['unit_count'] == 2
    assert 'NOT HUMAN-REVIEWED DEPENDENCY' in item.content
    assert 'dependency' in item.content and 'mute' in item.content.lower()
    assert any('SET TRANSACTION READ ONLY' in sql for sql, _ in db.statements)
    sql = '\n'.join(stmt for stmt, _ in db.statements)
    assert 'sv.id=(' in sql and 'body_ver.branch_name' in sql
    assert 'tv.id=(' in sql and 'body_ver.specification_id=ts.id' in sql
    assert "metadata ->> 'document_role'" in sql
    assert 'LIMIT 241' in sql and 'LIMIT 9' in sql


@pytest.mark.parametrize('bad_target', [
    [],
    [{**target(), 'target_hash': '0' * 64}],
    [{**target(), 'target_code': '551'}],
    [target()] * 9,
])
def test_deterministic_citation_fail_closed_on_missing_drift_or_oversize(monkeypatch, bad_target):
    db = FakeDB(sources=[source()], targets=bad_target)
    obj = retriever(monkeypatch, db)
    assert not obj._deterministic_section_reference_search(
        '508 550 关联', ('21MM',), spec_codes=('508', '550'))


def test_no_spec_code_resolved_by_catalog_but_no_freeform_global_scan(monkeypatch):
    db = FakeDB(sources=[source()], targets=[target()])
    obj = retriever(monkeypatch, db)
    rows = obj._deterministic_section_reference_search('蓝牙静音章节引用', ('21MM',))
    assert len(rows) == 1
    assert rows[0].metadata['target']['spec_code'] == '550'


def test_explicit_section_and_keyword_not_confused_with_revision():
    assert section_selector('550 从 v2.50 到 v2.52 的 section 4.1 变更') == '4.1'
    assert section_selector('第4章有什么变化') == '4'
    assert section_selector('550 v2.52 有何变化') == ''
    assert keyword_selector('关键词:Bluetooth 变化') == 'bluetooth'
    assert keyword_selector('蓝牙有哪些变化') == ''
    assert matches_change_selector(change(), '4', 'mute')
    assert matches_change_selector(change(), '4.1', '')
    assert not matches_change_selector(change(), '4.2', '')
    assert not matches_change_selector(change(), '', 'speaker')


def test_persisted_diff_uses_raw_hash_not_normalized_hash():
    row = change()
    assert verify_persisted_diff(row)
    assert not verify_persisted_diff({**row, 'old_text': row['old_text'] + '!'})
    assert not verify_persisted_diff({**row, 'new_hash': None})
    assert verify_persisted_diff(change(change_type='ADDED', old_text=None))
    assert verify_persisted_diff(change(change_type='REMOVED', new_text=None))
    assert not verify_persisted_diff(change(change_type='UNCHANGED'))


def test_live_readonly_chapter_diff_version_chapter_and_hash(monkeypatch):
    db = FakeDB(changesets=[changeset(), changeset(change_set_id=8, from_version='2.40')],
                changes=[change(), change(id=11, section_id='7.1')])
    obj = retriever(monkeypatch, db)
    result = obj._chapter_version_diff_search(
        '550 从 v2.50 到 v2.52 的 section 4.1 变化', ('21MM',),
    )
    assert len(result) == 1
    row = result[0]
    assert row.source_type == 'CHAPTER_VERSION_DIFF'
    assert row.metadata['from_version'] == '2.50'
    assert row.metadata['to_version'] == '2.52'
    assert row.metadata['change_id'] == 10
    assert 'Mute within 100ms' in row.content and 'Mute within 30ms' in row.content
    assert any('SET TRANSACTION READ ONLY' in sql for sql, _ in db.statements)
    assert any('cs.status=\'READY\'' in sql and "metadata ->> 'document_role'" in sql
               for sql, _ in db.statements)


def test_diff_does_not_misread_section_as_version_or_synthesize_unready(monkeypatch):
    db = FakeDB(changesets=[changeset()], changes=[change()])
    obj = retriever(monkeypatch, db)
    assert len(obj._chapter_version_diff_search('550 section 4.1 修改', ('21MM',))) == 1
    assert not obj._chapter_version_diff_search('550 v9.90 section 4.1 修改', ('21MM',))
    assert not obj._chapter_version_diff_search('550 发生了变化', ('21MM',))


def test_rule_citation_cannot_turn_into_confirmed_dependency_via_governance():
    a = EvidenceItem(evidence_id='', scope='21MM', source_type='STRUCTURAL_REFERENCE',
                     content='Refer to Spec 550 section 4.1', score=0.82,
                     content_id=1, metadata={'status': 'RULE_VERIFIED',
                                            'anchor': {'start_offset': 10},
                                            'source': {'text_hash': 'a'},
                                            'target': {'spec_code': '550', 'section': '4.1'}})
    items, strength, _ = EvidenceGovernor.govern((a,), 8)
    assert len(items) == 1
    assert strength != 'STRONG'
    assert items[0].metadata['status'] == 'RULE_VERIFIED'
    b = replace(a, content='Refer to Spec 551 section 5.1',
                metadata={**a.metadata, 'target': {'spec_code': '551', 'section': '5.1'}})
    items2, _, _ = EvidenceGovernor.govern((a, b), 8)
    assert len(items2) == 2


def test_stage139_does_not_add_auto_approval_or_schema_mutation():
    code = (ROOT / 'app/ai/retrieval.py').read_text()
    assert 'STRUCTURAL_REFERENCE' in code
    assert 'CHAPTER_VERSION_DIFF' in code
    assert 'if "relation_search" in tool_set:' in code
    assert 'if "version_search" in tool_set:' in code
    assert 'SET TRANSACTION READ ONLY' in code


def test_relation_retrieval_does_not_wait_for_review_db_authority(monkeypatch):
    db = FakeDB(sources=[source()], targets=[target()])
    obj = retriever(monkeypatch, db)
    monkeypatch.setattr(obj, '_relation_search', lambda *args, **kwargs: [])
    monkeypatch.setattr(obj, '_content_relation_search', lambda *args, **kwargs: [])
    result = obj.retrieve_agentic(
        question='508 引用 550 哪一章', scopes=('21MM',),
        rewritten_queries=('508 引用 550',), tools=('relation_search',), entities=('508', '550'),
    )
    assert any(e.source_type == 'STRUCTURAL_REFERENCE' for e in result.evidence)
    assert any('structural_reference_search: 1 verified' in t for t in result.trace)


def test_version_retrieval_keeps_deterministic_diff_when_v7_mapping_absent(monkeypatch):
    db = FakeDB(changesets=[changeset()], changes=[change()])
    obj = retriever(monkeypatch, db)
    monkeypatch.setattr(obj, '_version_search', lambda *args, **kwargs: [])
    monkeypatch.setattr(obj, '_requirement_version_search', lambda *args, **kwargs: [])
    result = obj.retrieve_agentic(
        question='550 v2.50 到 v2.52 的 section 4.1 变更', scopes=('21MM',),
        rewritten_queries=('550 v2.50 v2.52 section 4.1',),
        tools=('version_search',), entities=('550',),
    )
    assert len(result.evidence) == 1
    assert result.evidence[0].source_type == 'CHAPTER_VERSION_DIFF'
