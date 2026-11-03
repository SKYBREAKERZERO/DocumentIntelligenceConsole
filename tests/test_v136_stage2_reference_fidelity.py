"""v1.36: source-exact reference anchor fidelity, conservative interpretation.

All DB cursors in this module are synthetic; no live PostgreSQL is touched.
"""
from __future__ import annotations

from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import Mock
import hashlib
import json

import pytest

from app.engineering_intelligence.relations import (
    extract_reference_anchors, find_reference_anchor, link_explicit_references,
)
from app.engineering_intelligence.requirements import extract_requirement_units, normalize_text
from app.engineering_intelligence import content_relation_review as review
from app.engineering_intelligence import content_relation_graph_report as graph
from app.gui import stage2_review_window as gui
from tests.test_v131_stage2_content_relation_review import Cursor, example_row, _retriever, retrieval_row
from tests.test_v133_stage2_controlled_review import ControlledCursor, apply


def units(text, *, document='source', section='4.1'):
    return extract_requirement_units(document_id=document, content_id=1,
                                     section_id=section, text=text)


@pytest.mark.parametrize('text', [
    'Refer to Spec 550 for details. See section 4.1 of another spec.',
    'Refer to Spec 550 for details; section 4.1 of 542 shall be followed.',
    'Refer to Spec 550 for general details. see section 4.1 of another spec.',
    '参照 Spec 550 的概要。另见 section 4.1',
    'Refer to Spec 550 section 4.1 and section 5.2.',
    'Refer to Spec 550 section 4.1; see Spec 542 section 5.2',
])
def test_same_clause_and_unambiguous_anchor_gate(text):
    source=units(text)
    if 'section 4.1;' in text:
        assert find_reference_anchor(text, '550', '4.1') is not None
    else:
        assert find_reference_anchor(text, '550', '4.1') is None
    if find_reference_anchor(text, '550', '4.1') is None:
        assert not link_explicit_references(source_spec_code='508', source_units=source,
                                            target_spec_code='550',
                                            target_units=units('Target shall respond', document='target'))


def test_explicit_source_span_with_unicode_and_multiple_specs():
    raw='AA 参照 Spec 550 section 4.1。 See Spec 542 section 5.2.'
    anchors=extract_reference_anchors(raw)
    assert [(a.specification_code, a.section_id) for a in anchors] == [('550','4.1'),('542','5.2')]
    for anchor in anchors:
        assert raw[anchor.start_offset:anchor.end_offset] == anchor.raw_text
    assert find_reference_anchor(raw, '550', '§ 4.1') == anchors[0]
    assert find_reference_anchor(raw, '542', '4.1') is None


def test_decimal_section_and_sec_abbreviation_not_false_sentence_boundaries():
    assert find_reference_anchor('Refer to Spec 550, Sec. 4.1.2', '550', '4.1.2')
    assert not find_reference_anchor('Refer to Spec 550 in general. Read section 4.1', '550', '4.1')


def test_no_arbitrary_requirement_chosen_from_ambiguous_target_section():
    source=units('Refer to Spec 550 section 4.1 for Memory ID')
    first=units('Bluetooth shall wake.', document='target',section='4.1')[0]
    second=units('Bluetooth must sleep.', document='target-2',section='4.1')[0]
    proposals=link_explicit_references(source_spec_code='508', source_units=source,
        target_spec_code='550',target_units=(first,second))
    assert proposals == ()
    assert len(link_explicit_references(source_spec_code='508',source_units=source,
        target_spec_code='550',target_units=(first,))) == 1


@pytest.fixture(autouse=True)
def no_live_schema(monkeypatch):
    monkeypatch.setattr(review,'ensure_schema_ready',Mock())
    monkeypatch.setattr(review,'require_complete_backfill',Mock())


def test_review_case_contains_verbatim_anchor_and_explicit_interpretation_boundary():
    case=review.load_review_case(Cursor(), candidate_id=12, project='21MM')
    anchor=case.reference_anchor
    assert anchor is not None
    assert case.source.raw_text[anchor.start_offset:anchor.end_offset] == anchor.raw_text
    assert anchor.specification_code == '550' and anchor.section_id == '4.1'
    screen=gui.Stage2ReviewWindow._describe(case)
    assert anchor.raw_text in screen
    assert '功能依赖' in screen


@pytest.mark.parametrize('field,value', [('target_section','5.2'),('target_code','542')])
def test_review_rejects_semantically_wrong_target_even_when_hashes_valid(field,value):
    row=example_row()
    row[field]=value
    with pytest.raises(review.ContentRelationReviewError, match='anchor'):
        review.load_review_case(Cursor(row), candidate_id=12,project='21MM')
    c=ControlledCursor(row)
    with pytest.raises(review.ContentRelationReviewError, match='anchor'):
        apply(c)
    assert not any('apply_content_relation_review' in statement for statement,_ in c.queries)


def test_review_rejects_stale_or_unrelated_source_with_consistent_hash():
    row=example_row()
    text='Refer to Spec 542 section 4.1 instead.'
    row.update(source_text=text,source_chunk='abc'+text+'\n',source_end=3+len(text),
               source_hash=hashlib.sha256(normalize_text(text).encode()).hexdigest())
    with pytest.raises(review.ContentRelationReviewError, match='anchor'):
        review.load_review_case(Cursor(row),candidate_id=12,project='21MM')


@pytest.mark.parametrize('field,value', [('target_section','5.2'),('target_code','542')])
def test_rag_fails_closed_for_reviewed_but_false_reference(field,value):
    row=retrieval_row()
    row[field]=value
    retriever,_=_retriever(row)
    assert retriever._content_relation_search(['COMMON'],spec_codes=('508',)) == []


def test_rag_distinguishes_reviewed_reference_from_proven_dependency():
    retriever,_=_retriever(retrieval_row())
    results=retriever._content_relation_search(['COMMON'],spec_codes=('508',))
    assert len(results)==1
    evidence=results[0]
    assert 'does NOT prove' in evidence.content
    assert evidence.metadata['reference_anchor']['raw_text'] == '参照 Spec 550 section 4.1'
    assert 'not proven' in evidence.metadata['interpretation_limit']


def test_export_verifier_recomputes_original_anchor_not_only_manifest_hash(tmp_path):
    source='Refer to Spec 550 section 4.1 shall link.'
    from tests.test_v132_stage2_content_graph_report import endpoint
    case=review.ContentRelationReviewCase(
        candidate_id=24,relation_type='REFERENCES_SECTION',status='CONFIRMED',
        score=.95,rationale='EXPLICIT_SPEC_AND_SECTION_ANCHOR',
        source=endpoint('508', source),target=endpoint('550','Bluetooth Audio shall link.'),
        review_identity_provenance='DB_SESSION_USER',
        reference_anchor=find_reference_anchor(source,'550','4.1'))
    r=asdict(case)
    r['rag_eligible_at_snapshot']=True
    snapshot={'format':graph.GRAPH_FORMAT, 'project':'21MM',
              'snapshot_utc':'2026-10-22T10:00:00+00:00',
              'requested_statuses':['CONFIRMED'],'limit':1,
              'counts':{'examined':1,'validated':1,'quarantined':0},
              'relations':[r],'quarantined':[]}
    out=tmp_path/'export'
    graph.write_graph_bundle(snapshot,out)
    assert graph.verify_graph_bundle(out)['verified'] is True
    broken=json.loads((out/'graph.json').read_text(encoding='utf-8'))
    broken['relations'][0]['reference_anchor']['section_id']='5.2'
    # Pretend an attacker also recomputed the UNTRUSTED, unsigned manifest.
    raw=graph._canonical(broken)
    (out/'graph.json').write_bytes(raw)
    (out/'index.html').write_bytes(graph.render_graph_html(broken))
    manifest=json.loads((out/'manifest.json').read_text(encoding='utf-8'))
    manifest['sha256']={n:graph._hash((out/n).read_bytes()) for n in ('graph.json','index.html')}
    (out/'manifest.json').write_bytes(graph._canonical(manifest))
    with pytest.raises(ValueError,match='reference anchor'):
        graph.verify_graph_bundle(out)


def test_gui_does_not_claim_rollback_after_ambiguous_commit(monkeypatch):
    # Tk loop mocked: network timeout can occur *after* the server COMMIT.
    from queue import Queue
    w=gui.Stage2ReviewWindow.__new__(gui.Stage2ReviewWindow)
    w._queue=Queue()
    w._queue.put(('error','OperationalError'))
    w._busy=True
    w.root=Mock()
    w.status=Mock()
    w._clear_preview=Mock()
    show=Mock()
    monkeypatch.setattr(gui.messagebox,'showerror',show)
    w._drain()
    message=show.call_args.args[1]
    assert '不能据此断言已回滚' in message and '禁止直接重试' in message
    assert '结果未知' in w.status.set.call_args.args[0]


def test_bounded_multisection_source_fails_closed_without_partial_edges():
    source=units('Refer to Spec 550 section 4.1; Refer to Spec 550 section 5.2')
    targets=units('Bluetooth shall connect',document='target',section='4.1') + units(
        'Bluetooth shall disconnect',document='other',section='5.2')
    with pytest.raises(ValueError,match='review budget'):
        link_explicit_references(source_spec_code='508',source_units=source,
            target_spec_code='550',target_units=targets,max_candidates_per_source=1)
