"""Bounded, deterministic multi-domain Skill coordination for the live Agent.

Inspired by graph-based retrieval and temporal provenance design patterns; no
third-party framework code is copied, and no model-authored SQL is executed.
Only original user intent is eligible to add mandatory read-only retrievals.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

_ISSUE = re.compile(r"(?<![A-Z0-9])(?:(?!(?:SPEC|REV|SW)-)[A-Z][A-Z0-9]{1,19}-\d{1,10}|QA|BUG|JIRA|不具合|缺陷|故障|问题单)(?![A-Z0-9])", re.I)
_SPEC = re.compile(r"式样|式樣|仕様|规格|規格|spec(?:ification)?|requirements?|要件|要求|蓝牙|bluetooth", re.I)
_BARE_SPEC_CODE = re.compile(r"(?<![A-Za-z0-9-])\d{3,6}(?!\d)")
_VERSION = re.compile(r"历史|历代|版本|改版|修订|变更|変更|変更点|revision|version|diff|旧版|新版|影响|(?<![A-Za-z0-9])v\d+(?:\.\d+)+", re.I)
_RELATION = re.compile(r"关联|關聯|相关|有關|引用|参照|依赖|关系|冲突|影响|relation|reference|dependency|conflict", re.I)
_CONFORMANCE = re.compile(r"符合|满足|違反|违反|合规|不符合|一致|conform|compliance|meet(?:s)?\s+(?:the\s+)?spec", re.I)

_ISSUE_KINDS = frozenset({"ISSUE_EXACT", "ISSUE_HYBRID", "ISSUE_SQL_MATCH", "ISSUE_SEMANTIC"})
_SPEC_KINDS = frozenset({"SQL_EXACT", "HYBRID_CHUNK", "SEMANTIC", "QDRANT", "QDRANT_SPARSE"})
_RELATION_KINDS = frozenset({"RELATION", "CONTENT_RELATION", "STRUCTURAL_REFERENCE", "STRUCTURAL_LAYOUT", "RELATION_GRAPH", "REFERENCE_CHANGE_REVALIDATION", "TEMPORAL_RELATION_AUDIT"})
_VERSION_KINDS = frozenset({"VERSION_HISTORY", "VERSION_CHANGE_DETAIL", "VERSION_CHANGE", "CHAPTER_VERSION_DIFF", "REQUIREMENT_VERSION_CHANGE", "TEMPORAL_RELATION_AUDIT"})


@dataclass(frozen=True)
class SkillWorkflow:
    skills: tuple[str, ...]
    required_tools: tuple[str, ...]
    evidence_facets: tuple[str, ...]
    conformance_request: bool = False


def build_live_skill_workflow(question: str) -> SkillWorkflow:
    """Compute a fixed allow-listed workflow from the user's question only.

    Neither retrieved document text nor model rewrites may grant more tools.
    A single BUG key by itself is intentionally not a cross-domain request.
    """
    q = str(question or "")[:4000]
    issue = bool(_ISSUE.search(q))
    version, relation = bool(_VERSION.search(q)), bool(_RELATION.search(q))
    bare_codes = [m.group(0) for m in _BARE_SPEC_CODE.finditer(q)
                  if not re.fullmatch(r"(?:19|20)\d{2}", m.group(0))]
    spec = bool(_SPEC.search(q)) or (bool(bare_codes) and (version or relation or bool(_CONFORMANCE.search(q))))
    if not issue and not spec:
        return SkillWorkflow((), (), ())
    if issue and spec:
        tools = ["issue_search", "sql_exact_search", "semantic_search"]
        facets = ["ISSUE", "SPEC_BODY"]
        skills = ["engineering-qa-bug-analysis", "engineering-hybrid-retrieval"]
        if version:
            tools.append("version_search")
            facets.append("VERSION")
            skills.append("spec-version-impact")
        if relation:
            tools.append("relation_search")
            facets.append("RELATION")
            skills.append("content-relation-audit")
        skills.append("rag-evidence-verification")
        return SkillWorkflow(tuple(skills), tuple(tools), tuple(facets), bool(_CONFORMANCE.search(q)))
    if spec and version and relation:
        return SkillWorkflow(
            ("spec-version-impact", "content-relation-audit", "rag-evidence-verification"),
            ("sql_exact_search", "version_search", "relation_search"),
            ("SPEC_BODY", "VERSION", "RELATION"),
        )
    return SkillWorkflow((), (), ())


def evidence_facet(source_type: str) -> str:
    kind = str(source_type or "").upper()
    if kind in _ISSUE_KINDS:
        return "ISSUE"
    if kind in _SPEC_KINDS:
        return "SPEC_BODY"
    if kind in _VERSION_KINDS:
        return "VERSION"
    if kind in _RELATION_KINDS:
        return "RELATION"
    return "OTHER"


def missing_evidence_facets(items: Iterable[object], required_facets: tuple[str, ...]) -> tuple[str, ...]:
    """Classify only admitted evidence, never unreviewed display candidates."""
    present = {evidence_facet(getattr(x, "source_type", "")) for x in items}
    return tuple(facet for facet in required_facets if facet not in present)


def is_unreviewed_only_relation(items: Iterable[object]) -> bool:
    """Structural reference/change alerts cannot prove engineering dependency."""
    return not any(
        getattr(x, "source_type", "") in {"RELATION", "CONTENT_RELATION"}
        and str((getattr(x, "metadata", {}) or {}).get("status") or "").upper() == "CONFIRMED"
        for x in items
    )


def issue_symptom_queries(issue_evidence: Iterable[object]) -> tuple[str, ...]:
    """Derive at most two lexical SQL lookups from real QA/BUG SQL observations.

    Do not use LLM-provided rewrites, issue numbers, ``effect_result`` or
    other post-fix feedback as observations. This creates SEARCH TERMS only.
    """
    out: list[str] = []
    for item in issue_evidence:
        md = getattr(item, "metadata", {}) or {}
        if (getattr(item, "source_type", "") not in {"ISSUE_EXACT", "ISSUE_SQL_MATCH"}
                or md.get("source_authority") != "POSTGRESQL"):
            continue
        for field in ("phenomenon", "actual_result", "actual_behavior", "summary"):
            value = str(md.get(field) or "")[:240]
            # Control characters, quoted prompts and ticket IDs are never
            # carried into a new retrieval query. Keep natural language and
            # technical identifiers only; escape is handled by SQL driver.
            terms = re.findall(r"[\u3400-\u9fff]{2,}|[\u3040-\u30ff]{2,}|[A-Za-z][A-Za-z0-9_/-]{2,}", value)
            terms = [v for v in terms if v.lower() not in {"bug", "qa", "ignore", "prompt", "system", "instructions"}]
            query = " ".join(terms[:10])[:120]
            if query and len(query) >= 3 and query not in out:
                out.append(query)
            if len(out) >= 2:
                return tuple(out)
    return tuple(out)
