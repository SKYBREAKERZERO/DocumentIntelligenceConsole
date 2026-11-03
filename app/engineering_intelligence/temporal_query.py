"""Strict, explicit Stage 3 audit coordinates; never infer IDs from SPEC numbers.

Both IDs must be independently and uniquely labeled in the original user
question. Rewrites and language-model-provided entities are never parsed.
"""
from __future__ import annotations

import re

_RELATION = re.compile(r"(?<![\w])(?:relation[ _-]*id|关系[ _-]*id|關係[ _-]*id)\s*[:：=#]?\s*(\d{1,18})(?!\d)", re.I)
_VERSION = re.compile(r"(?<![\w])(?:new[ _-]*target[ _-]*version[ _-]*id|(?:新目标|新目標|目标|目標|新)[ _-]*版本[ _-]*id|目标版本[ _-]*id|目標版本[ _-]*id)\s*[:：=#]?\s*(\d{1,18})(?!\d)", re.I)


def parse_temporal_audit_ids(question: str) -> tuple[int, int] | None:
    """Only a single explicit relation ID + single new version ID is accepted."""
    q = str(question or '')[:2400]
    # A labeled ID followed by an alternative bare number is still ambiguous.
    if re.search(r'\b(?:or|或者)\s*\d+\b|或\s*\d+\b', q, re.I):
        return None
    relations = _RELATION.findall(q)
    versions = _VERSION.findall(q)
    if len(relations) != 1 or len(versions) != 1:
        return None
    relation_id, version_id = int(relations[0]), int(versions[0])
    if not 1 <= relation_id <= 9223372036854775807 or not 1 <= version_id <= 9223372036854775807:
        return None
    return relation_id, version_id


def choose_audit_scope(question: str, scopes: tuple[str, ...]) -> str | None:
    """Never fan out a temporal identity audit to other project databases."""
    allowed = tuple(s for s in scopes if s in {'21MM', '24MM', 'COMMON'})
    # If more than one scope is available, original query must disambiguate.
    labels = set(re.findall(r'(?<![A-Z0-9])(?:21MM|24MM|COMMON)(?![A-Z0-9])',
                            str(question or '').upper()))
    requested = labels.intersection(allowed)
    if len(requested) == 1 and len(labels) == 1:
        return next(iter(requested))
    if labels or len(allowed) != 1:
        return None
    return allowed[0]
