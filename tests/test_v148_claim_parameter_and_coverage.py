"""v1.48 deterministic claims: cited literal facts, no invented completeness."""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.ai.answer_integrity import REJECTED_ANSWER
from app.ai.claim_anchor import (
    EXHAUSTIVE_UNVERIFIED_ANSWER,
    requires_exhaustive_coverage,
    validate_claim_anchors,
)
from app.ai.rag_service import EnterpriseRAGService
from app.ai.retrieval import EvidenceItem, RetrievalResult


def ev(identifier: str, body: str, version: str = "v2.52") -> EvidenceItem:
    return EvidenceItem(
        evidence_id=identifier, scope="21MM", source_type="SQL_EXACT", content=body,
        content_id=int(identifier), document_id="spec", specification_code="550",
        specification_version=version, file_name="Bluetooth Audio function spec.pdf",
        page_number=31, metadata={"content_hash": "abcdef"},
    )


def check(answer: str, *items: EvidenceItem):
    return validate_claim_anchors(answer, admitted_evidence=items, allowed_ids=tuple(x.evidence_id for x in items))


@pytest.mark.parametrize('claim', [
    '点火启动超时500ms。[1]',
    '启动超时 0.5 s。[1]',
    '版本为v2.52。[1]',
    '最大电压12V。[1]',
    '相邻章节关系尚待人工确认。[1]',
])
def test_grounded_claims_preserve_unit_conversion(claim):
    assert check(claim, ev('1', 'Ignition ON, timeout 500 ms, voltage 12000mV; version v2.52')).accepted


@pytest.mark.parametrize('claim,reason', [
    ('超时600ms。[1]', 'UNSUPPORTED_PARAMETER_OR_UNIT'),
    ('最大电压24V。[1]', 'UNSUPPORTED_PARAMETER_OR_UNIT'),
    ('版本为v2.53。[1]', 'UNSUPPORTED_SPEC_VERSION'),
    ('超时500秒。[1]', 'UNSUPPORTED_PARAMETER_OR_UNIT'),
    ('温度80℃。[1]', 'UNSUPPORTED_PARAMETER_OR_UNIT'),
])
def test_rejects_unbacked_parameters_and_version(claim, reason):
    result = check(claim, ev('1', 'Timeout 500ms, voltage 12V, version v2.52'))
    assert result.reason == reason and not result.accepted


def test_evidence_bound_to_correct_citation_not_neighbouring_section():
    answer = '超时500ms。[2]'
    result = check(answer, ev('1', 'Timeout 500ms'), ev('2', 'Timeout 200ms'))
    assert not result.accepted and result.reason == 'UNSUPPORTED_PARAMETER_OR_UNIT'


def test_source_omitted_from_admitted_ids_cannot_anchor_parameter():
    result = validate_claim_anchors(
        '超时500ms。[1]',
        admitted_evidence=(ev('1', 'Timeout 200ms'), ev('2', 'Timeout 500ms')),
        allowed_ids=('1',),
    )
    assert not result.accepted


def test_missing_real_evidence_object_does_not_accept_forged_id():
    result = validate_claim_anchors('超时500ms。[1]', admitted_evidence=[], allowed_ids=('1',))
    assert not result.accepted and result.reason == 'MISSING_ADMITTED_EVIDENCE_OBJECT'


def test_document_or_chapter_numbers_do_not_trigger_parameter_guard():
    assert check('550 的 4.1 节提到蓝牙音频。[1]', ev('1', 'Section 4.1 of spec 550 mentions audio')).accepted


def test_exhaustive_intent_is_distinguished_from_specific_request():
    for question in ('列出全部例外条件', '给出所有要求', '完整列出550的要求', 'List all requirements'):
        assert requires_exhaustive_coverage(question)
    for question in ('有哪些典型例外？', '550的4.1节中500ms什么意思', '列出三条要求'):
        assert not requires_exhaustive_coverage(question)


def fake_service(answer: str, *, evidence: tuple[EvidenceItem, ...] | None = None):
    svc = object.__new__(EnterpriseRAGService)
    items = evidence or (ev('1', 'IF ignition ON, timeout 500ms, version v2.52'),)
    r = RetrievalResult(
        evidence=items, resolved_scopes=('21MM',), route='ENGINEERING_AGENT',
        prompt_evidence_ids=('1',),
    )
    svc.prepare = lambda *_a, **_kw: (
        [{'role':'system', 'content':'RETRIEVED ENTERPRISE EVIDENCE\n[1] original requirement'},
         {'role':'user', 'content':'question'}], r,
    )
    svc.settings = SimpleNamespace(
        num_ctx=4096, ollama_keep_alive='5m', ollama_connect_timeout=5.,
        ollama_read_timeout=30., ollama_first_load_timeout=60.,
        ollama_stream_idle_timeout=30., temperature=0.,
    )
    svc.ollama = SimpleNamespace(chat_stream=lambda **_kw: iter([answer]))
    return svc


def test_answer_stream_fails_closed_without_semantic_generation_when_exhaustive():
    svc = fake_service('允许。[1]')
    svc.ollama.chat_stream = lambda **_kw: pytest.fail('No model for unverifiable exhaustive answer')
    text = ''.join(svc.answer_stream([{'role':'user','content':'列出全部工程要求'}], model='local'))
    assert text == EXHAUSTIVE_UNVERIFIED_ANSWER


def test_stream_blocks_false_numeric_claim_but_allows_anchored_claim():
    svc = fake_service('启动超时600ms。[1]')
    assert list(svc.answer_stream([{'role':'user','content':'超时是多少'}], model='local')) == [REJECTED_ANSWER]
    svc.ollama.chat_stream = lambda **_kw: iter(['启动超时500ms。[1]'])
    assert list(svc.answer_stream([{'role':'user','content':'超时是多少'}], model='local')) == ['启动超时500ms。[1]']


def test_legacy_context_builder_does_not_cut_critical_exceptions():
    svc = object.__new__(EnterpriseRAGService)
    svc.settings = SimpleNamespace(max_context_chars=250)
    big = ev('1', 'IF ignition ON, threshold 500ms; ' + 'A' * 1000 + '; EXCEPT sleep mode OFF')
    small = ev('2', 'Sleep mode OFF')
    context = svc._build_context((big, small))
    assert 'IF ignition' not in context
    assert 'EXCEPT' not in context
    assert 'Sleep mode OFF' in context
