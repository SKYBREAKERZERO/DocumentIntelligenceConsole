"""Conversation understanding and evidence usability for enterprise answers.

This module deliberately does not answer questions.  It produces a compact,
auditable frame that helps the planner and answer model understand the active
engineering objects, the user's practical goal, and which retrieved chunks are
actually readable enough to support a claim.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable


ISSUE_KEY = re.compile(r"(?<![A-Z0-9])(?!SPEC-)([A-Z][A-Z0-9]{1,19}-\d{1,10})(?![A-Z0-9])", re.I)
SPEC_CODE = re.compile(r"(?:SPEC[-_: ]*)?(?<!\d)(\d{3,6})(?!\d)", re.I)
PROJECT = re.compile(r"(?<![A-Z0-9])(21MM|24MM|COMMON)(?![A-Z0-9])", re.I)
VERSION = re.compile(r"(?<![A-Z0-9])(?:v|ver\.?)[ ]?(\d+(?:\.\d+){1,4})(?!\d)", re.I)


@dataclass(frozen=True, slots=True)
class ConversationState:
    active_issue: str = ""
    active_spec: str = ""
    active_project: str = ""
    active_version: str = ""
    relation_source: str = ""
    relation_target: str = ""

    @classmethod
    def from_messages(cls, messages: Iterable[dict[str, str]]) -> "ConversationState":
        """Extract state only from user-authored turns, newest fact wins."""
        users = [re.sub(r"\s+", " ", str(m.get("content") or "").strip())
                 for m in messages if str(m.get("role") or "").lower() == "user"][-20:]
        issue = spec = project = version = source = target = ""
        for text in reversed(users):
            text_without_issues = ISSUE_KEY.sub(" ", text)
            if not issue:
                hit = ISSUE_KEY.search(text)
                issue = hit.group(1).upper() if hit else ""
            if not project:
                hit = PROJECT.search(text)
                project = hit.group(1).upper() if hit else ""
            if not version:
                hit = VERSION.search(text)
                version = "v" + hit.group(1) if hit else ""
            codes = tuple(dict.fromkeys(SPEC_CODE.findall(text_without_issues)))
            relation = re.search(r"引用|参照|參照|依赖|依賴|关联|關聯|关系|關係|有关|有關|对应|對應|related|relation|reference", text, re.I)
            if relation and len(codes) >= 2 and not source:
                source, target = codes[0], codes[1]
            if not spec and len(codes) == 1:
                spec = codes[0]
            if issue and spec and project and (source or len(users) < 2):
                break
        return cls(issue, spec, project, version, source, target)

    def retrieval_hints(self) -> tuple[str, ...]:
        hints = []
        if self.active_issue:
            hints.append(f"active_issue={self.active_issue}")
        if self.active_spec:
            hints.append(f"active_spec=SPEC-{self.active_spec}")
        if self.active_project:
            hints.append(f"active_project={self.active_project}")
        if self.active_version:
            hints.append(f"active_version={self.active_version}")
        if self.relation_source and self.relation_target:
            hints.append(f"active_relation=SPEC-{self.relation_source}->SPEC-{self.relation_target}")
        return tuple(hints)


@dataclass(frozen=True, slots=True)
class QuestionFrame:
    goal: str
    asks_current_judgment: bool
    asks_recorded_explanation: bool
    asks_test_design: bool
    asks_relationship: bool
    desired_shape: str

    @classmethod
    def parse(cls, question: str, state: ConversationState) -> "QuestionFrame":
        q = str(question or "")
        asks_test = bool(re.search(r"回归测试|回歸測試|测试观点|測試觀點|测试点|测试手顺|測試手順|test\s*case|regression", q, re.I))
        asks_relation = bool(re.search(r"引用|参照|參照|关联|關聯|对应|對應|哪本式样|哪些式样|relation|reference", q, re.I))
        asks_why = bool(re.search(r"为什么|為什麼|为何|為何|凭什么|理由|依据|why|explain", q, re.I))
        recorded = bool(state.active_issue and asks_why and re.search(r"判断|判定|结案|結案|结论|結論|仕様通り|式样通|根因|原因", q, re.I))
        current = bool(re.search(r"现在|現在|当前|當前|还是|還是|仍然|重新判断|是否符合|这是.{0,3}(?:bug|缺陷)|是不是.{0,3}(?:bug|缺陷)", q, re.I))
        if asks_test:
            goal, shape = "DESIGN_TESTS_FROM_EVIDENCE", "risk_then_test_cases"
        elif recorded:
            goal, shape = "EXPLAIN_RECORDED_TICKET_CONCLUSION", "facts_reasoning_gaps"
        elif current:
            goal, shape = "ASSESS_CURRENT_CONFORMANCE", "conditions_evidence_verdict"
        elif asks_relation:
            goal, shape = "TRACE_RELATIONSHIPS", "direct_links_then_candidates"
        elif re.search(r"总结|總結|概括|展开|展開|说说|說說|summary", q, re.I):
            goal, shape = "ANALYZE_AND_SUMMARIZE", "answer_then_key_points"
        else:
            goal, shape = "ANSWER_ENGINEERING_QUESTION", "answer_evidence_limits"
        return cls(goal, current, recorded, asks_test, asks_relation, shape)

    def prompt_contract(self) -> str:
        return (
            f"User goal: {self.goal}; preferred answer shape: {self.desired_shape}. "
            "Answer the practical question, not the retrieval procedure. "
            "Separate recorded ticket conclusions, independently verified specification facts, and semantic candidates."
        )


def evidence_usability(text: str) -> tuple[str, str]:
    """Return (quality, reason) for prompt admission, not truth confidence."""
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if not value:
        return "REJECT", "empty"
    if re.fullmatch(r"(?:\[?Header[^\]]*\]?|Screen Image|[-|:;,.\s]){1,120}", value, re.I):
        return "REJECT", "header_only"
    uuid_chars = sum(len(x) for x in re.findall(r"[0-9A-F]{8}(?:-[0-9A-F]{4}){3}-[0-9A-F]{12}", value, re.I))
    words = re.findall(r"[A-Za-z㐀-鿿぀-ヿ]{2,}", value)
    column_cells = len(re.findall(r"\bColumn\s+[A-Z]{1,3}\s*:", value, re.I))
    meaningful = [w for w in words if w.lower() not in {"column", "row", "header", "screen", "image"}]
    if uuid_chars > len(value) * 0.30 and len(meaningful) < 12:
        return "REJECT", "uuid_dominated"
    if column_cells >= 8 and len(meaningful) < 12:
        return "REJECT", "table_coordinates_without_requirement"
    if len(meaningful) < 3:
        return "LOW", "low_information"
    return "USABLE", "readable_content"
