"""Offline BUG -> primary specification requirement *discovery*, not compliance.

The deterministic fallback deliberately avoids an LLM, network traffic, and any
"PASS/FAIL" classification. It produces only lexical candidates for review.
The caller must verify BOTH source rows and the approved exact version mapping.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Mapping

from app.engineering_intelligence.requirements import normalize_text
from app.storage.primary_document_policy import PrimarySpecificationDocumentPolicy

SCORER_VERSION = "bug-spec-lexical-v1"
VERDICTS = ("符合", "不符合", "式样未规定", "式样含糊或互相冲突", "证据不足")
_WORD = re.compile(r"[a-z][a-z0-9_./+-]{2,}|[\u3400-\u9fff\u3040-\u30ff]{2,}", re.I)
_IGNORE = {"shall", "must", "will", "should", "where", "when", "with", "this", "that", "仕様", "式样", "式樣", "功能", "要求", "问题", "条件"}


def sha256_normalized(text: str) -> str:
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


def source_digest(fields: Mapping[str, object]) -> str:
    """Version and all BUG observable/expected source fields bind a snapshot."""
    keys = ("issue_key", "record_type", "summary", "description", "phenomenon",
            "repro_steps", "expected_result", "actual_result", "affected_version",
            "software_version", "model", "destination")
    payload = {key: normalize_text(str(fields.get(key) or "")) for key in keys}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def extract_bug_observations(row: Mapping[str, object]) -> dict[str, str]:
    """Extract labels only from the correct fields; effect_result is NOT actual."""
    if str(row.get("record_type") or "").upper() != "BUG":
        raise ValueError("BUG record required")
    issue_key = str(row.get("issue_key") or "").strip()
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{1,19}-\d{1,10}", issue_key):
        raise ValueError("Explicit valid issue key required")
    return {
        "phenomenon": normalize_text(str(row.get("phenomenon") or row.get("actual_result") or row.get("description") or row.get("summary") or ""))[:4000],
        "trigger": normalize_text(str(row.get("repro_steps") or ""))[:4000],
        "expected": normalize_text(str(row.get("expected_result") or ""))[:4000],
        "actual": normalize_text(str(row.get("actual_result") or ""))[:4000],
        "summary": normalize_text(str(row.get("summary") or ""))[:1200],
    }


def lexical_tokens(text: str) -> frozenset[str]:
    """Cheap multilingual words/CJK bigrams. Zero overlap is not a match."""
    text = unicodedata.normalize("NFKC", str(text or "")).lower()
    result: set[str] = set()
    for word in _WORD.findall(text):
        if word in _IGNORE:
            continue
        if re.search(r"[\u3400-\u9fff\u3040-\u30ff]", word):
            result.update(word[i:i+2] for i in range(len(word) - 1))
        else:
            result.add(word)
    return frozenset(result)


@dataclass(frozen=True)
class SpecUnitSnapshot:
    scope: str
    spec_code: str
    version_id: int
    version_label: str
    document_id: str
    file_name: str
    document_metadata: dict
    unit_id: int
    content_id: int
    section_id: str
    page_number: int | None
    start_offset: int
    end_offset: int
    raw_text: str
    text_hash: str
    chunk_text: str

    def verify(self) -> bool:
        if self.scope not in {"21MM", "24MM", "COMMON"}:
            return False
        if not PrimarySpecificationDocumentPolicy.is_primary(
            file_name=self.file_name, metadata=self.document_metadata
        ):
            return False
        return (
            bool(self.spec_code and self.version_id > 0 and self.document_id)
            and 0 <= self.start_offset < self.end_offset <= len(self.chunk_text)
            and self.chunk_text[self.start_offset:self.end_offset] == self.raw_text
            and sha256_normalized(self.raw_text) == self.text_hash.strip()
            and bool(self.section_id.strip())
        )


def lexical_score(observations: Mapping[str, str], requirement: str) -> float:
    actual = lexical_tokens(" ".join((observations.get("phenomenon", ""),
                                      observations.get("actual", ""),
                                      observations.get("trigger", ""),
                                      observations.get("summary", ""))))
    expected = lexical_tokens(observations.get("expected", ""))
    target = lexical_tokens(requirement)
    if not actual or not target:
        return 0.0
    # Critical: expected behavior must not fully replace observed behavior.
    intersect = actual & target
    if not intersect:
        return 0.0
    overlap = len(intersect) / max(1, len(actual | target))
    bonus = min(0.04, len(expected & target) / max(1, len(expected | target)) * 0.04) if expected else 0.0
    return round(min(0.999, overlap + bonus), 5)


def rank_candidates(
    observations: Mapping[str, str], units: list[SpecUnitSnapshot],
    *, top_n: int = 8, minimum_score: float = 0.035,
) -> tuple[tuple[SpecUnitSnapshot, float], ...]:
    if not 1 <= top_n <= 30:
        raise ValueError("top_n must be between 1 and 30")
    ranked = [(unit, lexical_score(observations, unit.raw_text)) for unit in units if unit.verify()]
    return tuple(sorted(((u, s) for u, s in ranked if s >= minimum_score),
                        key=lambda p: (-p[1], p[0].unit_id))[:top_n])


def conservative_verdict(*, status: str, mapping_verified: bool, source_intact: bool,
                         target_intact: bool) -> str:
    """Candidate ranking is not adjudication, even if someone marked a link."""
    # A human-review outcome must come from a separate auditable decision flow.
    # No heuristic/4B-model path is permitted to return a normative verdict.
    return "证据不足"
