"""Production-path implementation of the evidence verification Skill.

Caller alone must supply real, admitted EvidenceItem objects and an immutable
allowed-ID list assembled by EnterpriseRAGService. Never build allow-list from
LLM text or from a client-provided JSON field.
"""
from __future__ import annotations

from collections.abc import Iterable
from app.ai.answer_integrity import AnswerValidation, validate_enterprise_answer
from app.ai.claim_anchor import validate_claim_anchors
from app.skills.live_release import validate_live_analysis_answer


def verify_live_enterprise_answer(answer: str, *, prepared: list[dict[str, str]],
                                  route: str, allowed_ids: tuple[str, ...],
                                  admitted_evidence: Iterable[object], question: str = "") -> AnswerValidation:
    structural = validate_enterprise_answer(answer, prepared, route=route, allowed_ids=allowed_ids)
    if not structural.accepted or route == "DIRECT_CHAT":
        return structural
    # Only the assembler-admitted records can support a final answer.
    evidence = tuple(item for item in admitted_evidence if str(getattr(item, "evidence_id", "")) in allowed_ids)
    anchors = validate_claim_anchors(answer, admitted_evidence=evidence, allowed_ids=allowed_ids)
    if not anchors.accepted:
        return anchors
    return validate_live_analysis_answer(answer, question=question, admitted_evidence=evidence)
