"""Bounded DCM event/phase source diagnostics for Stage 1 exported JSON.

DCM labels are lexical *review cues* only. They are NOT a reconstructed
state machine, transition, temporal proof, or safety requirement. Source spans
remain immutable and are never serialized in review reports.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any

from .stage1_review_evidence import make_pointer

# Do not match acronym fragments such as ABCDCM20. Japanese adjacency is valid
# in phrases like DCM通信. Full-width uppercase characters are common in PDFs.
_DCM = re.compile(r"(?<![A-Za-z0-9_])(?:DCM|ＤＣＭ)(?![A-Za-z0-9_])", re.I)
_LINE = re.compile(r"[^\r\n]+")
# Only consider UI button context from the *same extracted section* as a DCM
# source; this is not an assertion that the two lines describe one feature.
_RELATED_UI_BUTTON = re.compile(
    r"(?:Telematics|テレマティクス|APP)(?:[ \t]|の|用){0,6}(?:釦|ボタン|button)", re.I,
)
_PHASES = {
    "INITIAL": re.compile(r"初期(?:値|状態)?|起動直後|\binitial(?:ly)?\b|\bdefault\s+state\b|\bat\s+startup\b", re.I),
    "BEFORE": re.compile(
        r"(?:DCM|ＤＣＭ)(?:\s|と(?:の)?|の)*?(?:通信|接続)(?:が)?(?:完了する)?前|"
        r"\bbefore\s+(?:(?:the|a)\s+)?DCM\s+(?:communication|connection)\b", re.I,
    ),
    "AFTER": re.compile(
        r"(?:DCM|ＤＣＭ)(?:\s|と(?:の)?|の)*?(?:通信|接続)(?:が)?(?:完了|成功|終了)?(?:した)?後|"
        r"\bafter\s+(?:(?:the|a)\s+)?DCM\s+(?:communication|connection)\b", re.I,
    ),
    "SUCCESS": re.compile(
        r"(?:DCM|ＤＣＭ)(?:\s|と(?:の)?|の)*?(?:通信|接続)(?:が)?成功(?:した)?(?:場合|時|後)?|"
        r"\bDCM\s+(?:communication|connection)\s+(?:succeeds|succeeded|successful)\b", re.I,
    ),
    "FAILURE": re.compile(
        r"(?:DCM|ＤＣＭ)(?:\s|と(?:の)?|の)*?(?:通信|接続)(?:が)?(?:失敗|異常|タイムアウト)|"
        r"\bDCM\s+(?:communication|connection)\s+(?:fails|failed|times?\s*out)\b", re.I,
    ),
}
_NEGATION = re.compile(r"非表示|表示しない|なし|無し|未表示|未接続|未インストール|\b(?:not|never|without)\b", re.I)
_EXCEPTION = re.compile(r"ただし|但し|除外|除く|例外|除非|\b(?:except|unless)\b", re.I)
_ACTION = re.compile(
    r"表示|保持|維持|通知|インストール|更新|切断|送信|格納|保存|"
    r"\b(?:display|show|install|retain|keep|notify|update|disconnect|send|store|save)\b", re.I,
)


def inspect_dcm_sources(
    document_hash: str, contents: list[dict[str, Any]], *,
    max_items: int = 1_000, max_chars: int = 1_000_000,
    max_lines: int = 8_000, max_evidence: int = 100,
) -> dict[str, Any]:
    """Find bounded DCM-related source lines, without linking distinct states.

    Each evidence pointer refers to an exact line in the exported source text;
    phase and exception markers are review metadata, not a scope assertion.
    Probe completeness is always explicit, including line-budget exhaustion.
    """
    if not all(type(b) is int and b > 0 for b in (max_items, max_chars, max_lines, max_evidence)):
        raise ValueError("Invalid DCM probe budget")
    phases: Counter[str] = Counter()
    by_section: dict[str, set[str]] = defaultdict(set)
    evidence: list[dict[str, Any]] = []
    evidence_total = dcm_lines = multi_phase_lines = exception_lines = negative_lines = 0
    covered = analysed_chars = scanned_lines = 0
    related_lines = 0
    related_candidates: list[tuple[int, int, int, str]] = []
    dcm_sections: set[str] = set()
    complete = True
    for index, content in enumerate(contents, 1):
        body = content["text"]
        if covered >= max_items or analysed_chars + len(body) > max_chars:
            complete = False
            break
        covered += 1
        analysed_chars += len(body)
        for match in _LINE.finditer(body):
            if scanned_lines >= max_lines:
                complete = False
                break
            scanned_lines += 1
            literal = match.group(0)
            section = str(content.get("section_id") or "unknown")
            if not _DCM.search(literal):
                # Telematics / APP button's initial state may precede an
                # explicit DCM line in the next content record. Retain only
                # a bounded *possible* same-section association for review.
                if (_RELATED_UI_BUTTON.search(literal)
                        and _PHASES["INITIAL"].search(literal)):
                    related_candidates.append((index, match.start(), match.end(), section))
                continue
            dcm_sections.add(section)
            dcm_lines += 1
            matched = tuple(name for name, pattern in _PHASES.items() if pattern.search(literal))
            status = "CONTEXT_UNVERIFIED"
            if len(matched) == 1:
                status = matched[0] + "_UNVERIFIED"
            elif len(matched) > 1:
                status = "MULTIPLE_PHASES_UNRESOLVED"
                multi_phase_lines += 1
            for name in matched:
                phases[name] += 1
            if matched:
                by_section[section].update(matched)
            if _EXCEPTION.search(literal):
                exception_lines += 1
            if _NEGATION.search(literal):
                negative_lines += 1
            # Presence of an action and a phase does not establish which
            # condition controls it. Never export a synthesized transition.
            if len(matched) == 0 and _ACTION.search(literal):
                status = "ACTION_SCOPE_UNRESOLVED"
            evidence_total += 1
            if len(evidence) < max_evidence:
                evidence.append(make_pointer(
                    document_hash, content, index, match.start(), match.end(),
                    category="DCM_SOURCE", status=status,
                ))
        if not complete:
            break
    for index, start, end, section in related_candidates:
        if section not in dcm_sections:
            continue
        related_lines += 1
        by_section[section].add("INITIAL")
        phases["INITIAL"] += 1
        evidence_total += 1
        if len(evidence) < max_evidence:
            evidence.append(make_pointer(
                document_hash, contents[index-1], index, start, end,
                category="DCM_RELATED_UI_SOURCE", status="RELATED_INITIAL_UNVERIFIED",
            ))
    multi_phase_sections = sum(len(states) > 1 for states in by_section.values())
    return {
        "dcm_lines_sampled": dcm_lines,
        "related_ui_lines_sampled": related_lines,
        "phase_counts_sampled": dict(sorted(phases.items())),
        "multi_phase_lines_sampled": multi_phase_lines,
        "multi_phase_sections_sampled": multi_phase_sections,
        "exception_lines_sampled": exception_lines,
        "negation_lines_sampled": negative_lines,
        "probe_complete": complete and covered == len(contents),
        "state_transitions_verified": False,
        "event_action_scope_verified": False,
        "evidence_queue": evidence,
        "evidence_total": evidence_total,
        "evidence_truncated": max(0, evidence_total - len(evidence)),
    }
