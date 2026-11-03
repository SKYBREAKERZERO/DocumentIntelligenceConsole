"""Fail-closed conclusions for multi-domain engineering requests.

A real BUG/SPEC compliance verdict requires a trusted, explicitly approved
software-to-specification version map and reviewed bilateral evidence. Neither
unreviewed candidate suggestions nor two independent source hits establish it.
This policy is deliberately narrower than general semantic entailment.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

from app.ai.answer_integrity import AnswerValidation
from app.skills.live_workflows import build_live_skill_workflow, missing_evidence_facets

# Only exact dedicated review records can prove a compliance verdict. Existing
# index candidate rows NEVER carry this source_type and never enter this gate.
_VERIFIED_CONFORMANCE_SOURCE = "BUG_SPEC_REVIEWED_CONFORMANCE"
_AFFIRMATIVE = re.compile(r"(?:结论|判定|判断|确认|证实|可以确定|结果)\s*[:：]?\s*(?:为|是)?\s*(?:符合|不符合|满足|不满足|违反|违背)|(?:confirmed|proven)\s+(?:compliant|noncompliant)", re.I)
_DIRECT_CONFORMANCE = re.compile(
    r"(?:BUG(?:-\d+)?|QA(?:-\d+)?|问题|缺陷|现象).{0,30}(?:不符合|符合|违反|违背|满足)"
    r"|(?:不符合|符合|违反|违背|满足).{0,20}(?:式样|仕様|规格|spec)", re.I,
)
_VERDICT = re.compile(r"(?:证据不足|无法判定|不能判定|不可判定|尚不能确定|无法确认|needs?\s+review|insufficient\s+evidence)", re.I)
_DEPENDENCY = re.compile(
    r"(?:已经|确实|明确|证明|必然|确定|直接|已经确认)(?:存在|造成|导致|影响|依赖)"
    r"|(?:必然导致|可确认.*?依赖|confirmed\s+dependency)"
    r"|(?<![A-Za-z0-9])\d{2,6}\s*(?:依赖|影响|导致)\s*\d{2,6}", re.I,
)


def validate_live_analysis_answer(answer: str, *, question: str,
                                  admitted_evidence: Iterable[object]) -> AnswerValidation:
    """Check unsupported *claims*, not legitimate quoted source references.

    Conservative: where a robust semantic proof is absent, the model may
    describe exact evidence but must explicitly abstain from conformance.
    """
    flow = build_live_skill_workflow(question)
    if not flow.skills:
        return AnswerValidation(True, "NOT_A_MULTI_DOMAIN_SKILL_QUESTION")
    records = tuple(admitted_evidence)
    missing = missing_evidence_facets(records, flow.evidence_facets)
    if flow.conformance_request:
        reviewed = any(
            getattr(item, "source_type", "") == _VERIFIED_CONFORMANCE_SOURCE
            and str((getattr(item, "metadata", {}) or {}).get("review_status") or "").upper() == "CONFIRMED"
            and (getattr(item, "metadata", {}) or {}).get("approved_version_mapping") is True
            for item in records
        )
        if not reviewed:
            # An abstention in one sentence cannot launder a positive verdict
            # hidden in a different sentence of the same response.
            affirmative_lines = [line for line in re.split(r"[。！？!?\n]+", answer)
                                 if (_AFFIRMATIVE.search(line) or _DIRECT_CONFORMANCE.search(line))
                                 and not _VERDICT.search(line)]
            if affirmative_lines or not _VERDICT.search(answer):
                return AnswerValidation(False, "UNREVIEWED_BUG_SPEC_CONFORMANCE")
    if missing and _AFFIRMATIVE.search(answer):
        return AnswerValidation(False, "MISSING_CROSS_DOMAIN_EVIDENCE")
    # Unreviewed reference/layout or a history diff do not prove impact.
    has_confirmed_relation = any(
        getattr(item, "source_type", "") in {"CONTENT_RELATION", "RELATION"}
        and str((getattr(item, "metadata", {}) or {}).get("status") or "").upper() == "CONFIRMED"
        for item in records
    )
    if "RELATION" in flow.evidence_facets and not has_confirmed_relation and _DEPENDENCY.search(answer):
        return AnswerValidation(False, "UNREVIEWED_ENGINEERING_DEPENDENCY")
    return AnswerValidation(True, "CROSS_DOMAIN_EVIDENCE_LEVEL_LIMITED_NOT_SEMANTIC_CERTIFICATION")
