"""Deterministic enterprise answer release gate (not semantic entailment).

A citation is usable only if its ID was actually included in the bounded system
prompt. The gate deliberately does not equate syntactic citation validity with
proof of a technical claim; independent semantic Golden tests remain necessary.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_CITATION = re.compile(r"(?<!\w)\[([1-9][0-9]{0,5})\]")
_MAX_ANSWER_CHARS = 40_000


@dataclass(frozen=True)
class AnswerValidation:
    accepted: bool
    reason: str
    cited_ids: tuple[str, ...] = ()


def _is_nonclaim(line: str) -> bool:
    """Only structural headings are safely uncited; short text can be a claim."""
    value = line.strip()
    if not value:
        return True
    core = re.sub(r"^\s*(?:>\s*|[-*]\s*|\d+[.)]\s*)", "", value).strip()
    core = re.sub(r"[\s*_#：:]+$", "", core).strip()
    return core in {"结论", "引用依据", "工程依据", "适用条件", "例外", "证据", "风险",
                    "限制", "版本信息", "需要复核", "说明", "Summary", "Evidence"}


def validate_enterprise_answer(
    answer: str,
    prepared: list[dict[str, str]],
    *,
    route: str,
    allowed_ids: tuple[str, ...] | frozenset[str] = (),
) -> AnswerValidation:
    """Block out-of-context IDs, uncited factual paragraphs and blank answers.

    Operates on FULL buffered output; never attempts unverifiable semantic
    entailment of the language model's free-form engineering reasoning.
    """
    if route == "DIRECT_CHAT":
        return AnswerValidation(True, "DIRECT_CHAT")
    if not answer or not answer.strip() or len(answer) > _MAX_ANSWER_CHARS:
        return AnswerValidation(False, "EMPTY_OR_OVERSIZED")
    # This immutable allow-list comes from the trusted prompt assembler, not
    # from parsing a model/system text that may contain spoofed evidence blocks.
    ids = frozenset(allowed_ids)
    if not ids:
        return AnswerValidation(False, "NO_TRUSTED_PROMPT_EVIDENCE")
    cited = tuple(dict.fromkeys(_CITATION.findall(answer)))
    if set(cited) - ids:
        return AnswerValidation(False, "INVALID_CITATION_ID", cited)
    # An uncited model abstention is replaced by a deterministic fixed refusal;
    # a malicious model must not hide an affirmative claim after "证据不足".
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", answer) if p.strip()]
    if not cited:
        return AnswerValidation(False, "MISSING_CITATION")
    for paragraph in paragraphs:
        # A numbered/markdown heading and a short advisory note are not claims;
        # other blocks need at least one actual prompt-bound evidence ID.
        if not _CITATION.search(paragraph):
            if all(_is_nonclaim(x) for x in paragraph.splitlines()):
                continue
            # No free-form uncited paragraphs. The caller can use the fixed
            # rejection text instead of trusting a paraphrased abstention.
            return AnswerValidation(False, "UNCITED_PARAGRAPH", cited)
        # Long multiline bullet lists each contain independent factual claims.
        for line in paragraph.splitlines():
            text = line.strip()
            if (text.startswith(("- ", "* ", "• ")) or re.match(r"^\d+[.)] ", text)) \
                    and not _CITATION.search(text) and not _is_nonclaim(text):
                return AnswerValidation(False, "UNCITED_LIST_CLAIM", cited)
            if not _CITATION.search(text) and not _is_nonclaim(text):
                return AnswerValidation(False, "UNCITED_LINE", cited)
            # Each Chinese/English factual sentence is required to cite an
            # admitted evidence ID. Avoid splitting decimals/section numbers.
            citation_attached = re.sub(
                r"([。！？!?])(\s*(?:\[[1-9][0-9]*\]\s*)+)", r" \2\1", text,
            )
            for sentence in re.split(r"(?<=[。！？!?])\s*", citation_attached):
                sentence = sentence.strip()
                if sentence and not _is_nonclaim(sentence) and not _CITATION.search(sentence):
                    return AnswerValidation(False, "UNCITED_SENTENCE", cited)
    return AnswerValidation(True, "CITATION_IDS_VALIDATED_NOT_SEMANTIC_PROOF", cited)


REJECTED_ANSWER = (
    "证据不足：本次模型回答未通过企业引用完整性校验，已阻止输出。"
    "这不表示式样条款不存在或工程结论为否定；请检查检索证据、版本与页码后重新查询。"
)
