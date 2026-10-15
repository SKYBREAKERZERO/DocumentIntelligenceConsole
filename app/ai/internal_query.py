"""Conservative multilingual *query candidate* expansion for private engineering RAG.

Terms here aid discovery, not document identity or factual equivalence. The
planner cannot authorize new knowledge stores, projects, or external searches.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class InternalQuery:
    original: str
    normalized: str
    queries: tuple[str, ...]
    specification_intent: bool
    issue_intent: bool
    cross_domain: bool


# The dictionary intentionally covers *search expressions*, not confirmed aliases
# for a specification, Jira ticket, or a document in the authoritative catalog.
_SEARCH_TERMS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?<![A-Za-z0-9])BT(?![A-Za-z0-9])|蓝牙|藍牙|ブルートゥース|Bluetooth", re.I), "Bluetooth"),
    (re.compile(r"(?<![A-Za-z0-9])DCM(?![A-Za-z0-9])|データ通信モジュール", re.I), "DCM Telematics"),
    (re.compile(r"配对|配對|ペアリング|pairing", re.I), "pairing ペアリング"),
    (re.compile(r"连接|連接|接続|connection", re.I), "connection 接続"),
    (re.compile(r"式样书|式樣書|试样书|試樣書|仕様書|specification|\bspec\b", re.I), "仕様書 specification"),
    (re.compile(r"(?<![A-Za-z0-9])HFP(?![A-Za-z0-9])", re.I), "Hands Free Profile"),
    (re.compile(r"(?<![A-Za-z0-9])BLE(?![A-Za-z0-9])", re.I), "Bluetooth Low Energy"),
)
_SPEC = re.compile(
    r"式样|式樣|试样|試樣|仕様|spec(?:ification)?|文档|文檔|文件|PDF|章节|章節|" \
    r"目录|目錄|目次|要求|要件|需求|設計|设计|功能|機能|连接|連接|接続|配对|配對|" \
    r"(?<![A-Za-z0-9])(?:BT|BLE|HFP|DCM|ECU|HMI|HU)(?![A-Za-z0-9])|蓝牙|藍牙|Bluetooth",
    re.I,
)
_ISSUE = re.compile(r"(?<![A-Za-z0-9])(?:QA|BUG|JIRA)(?![A-Za-z0-9])|不具合|障害|缺陷|缺陥|问题单|問題票|ticket|defect", re.I)


def interpret_internal_query(question: str, *, max_queries: int = 4) -> InternalQuery:
    original = re.sub(r"\s+", " ", str(question or "").strip())[:10000]
    # Orthographic correction only: never replace the user's identifiers.
    normalized = re.sub(r"试样书|試樣書|式樣書", "式样书", original)
    matched: list[str] = []
    for pattern, extension in _SEARCH_TERMS:
        if pattern.search(original):
            matched.append(extension)
    # Whole questions remain intact in one candidate. Short focused additions
    # make title/filename discovery possible without fake identity mappings.
    candidates = [normalized]
    for extension in matched:
        if extension.casefold() not in normalized.casefold():
            candidates.append(f"{normalized} {extension}")
    if matched:
        candidates.append(" ".join(matched))
    # Avoid empty or massive LLM-induced query explosions.
    deduped = tuple(dict.fromkeys(x.strip()[:1200] for x in candidates if x.strip()))[:max(1, min(max_queries, 6))]
    spec_intent = bool(_SPEC.search(original))
    issue_intent = bool(_ISSUE.search(original))
    return InternalQuery(
        original=original, normalized=normalized, queries=deduped or (original,),
        specification_intent=spec_intent,
        issue_intent=issue_intent,
        # A Bluetooth issue does not implicitly authorize searching the spec
        # corpus. Require an explicit specification/document/requirement noun.
        cross_domain=issue_intent and bool(re.search(
            r"式样|式樣|试样|試樣|仕様|spec(?:ification)?|文档|文檔|文件|PDF|章节|章節|要求|要件|需求|设计|設計",
            original, re.I,
        )),
    )
