"""Stage 1 review proposals and *human* decisions over immutable source evidence.

A source cue, table header, or plausible conditional span is never proof of an
engineering rule. No proposal in this module is automatically approved.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

from .atomic import AtomicCandidate
from .condition_scope import SCOPE_VERSION, extract_requirement_scope
from .requirements import RequirementUnit
from .tables import TableBlockEvidence

REVIEW_VERSION = "stage1-review-proposals-v1"
SCOPE_ASSERTIONS = frozenset({"ACTION_ONLY", "CONDITION_GOVERNS_ACTION", "EXCEPTION_OVERRIDES_ACTION"})
TABLE_ASSERTIONS = frozenset({"HEADER_ROW_APPLIES"})
FINAL_STATES = frozenset({"CONFIRMED", "REJECTED", "NEEDS_CLARIFICATION"})


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8", "surrogatepass")).hexdigest()


@dataclass(frozen=True, slots=True)
class ReviewProposal:
    key: str
    kind: str  # SCOPE | TABLE
    source_key: str
    version_id: int
    evidence_digest: str
    source_text_digest: str
    payload: dict[str, Any]


def prepare_review_proposals(
    *, version_id: int,
    units: tuple[RequirementUnit, ...],
    atoms: tuple[AtomicCandidate, ...],
    tables: tuple[TableBlockEvidence, ...],
    max_proposals: int = 120_000,
) -> tuple[ReviewProposal, ...]:
    """Create bounded, stable, *unapproved* evidence; never infer precedence."""
    if version_id < 1 or max_proposals < 1:
        raise ValueError("Invalid review proposal resource limits")
    units_by_key = {u.unit_key: u for u in units}
    if len(units_by_key) != len(units):
        raise ValueError("Duplicate source unit keys")
    proposals: list[ReviewProposal] = []
    for atom in atoms:
        unit = units_by_key.get(atom.parent_unit_key)
        if unit is None:
            raise ValueError("Missing source unit for scope")
        if atom.role != "REQUIREMENT_CANDIDATE":
            continue
        scope = extract_requirement_scope(unit, atom)
        payload = {
            "status": scope.status,
            "modality": scope.modality,
            "condition": asdict(scope.condition) if scope.condition else None,
            "exception": asdict(scope.exception) if scope.exception else None,
            "action": asdict(scope.action) if scope.action else None,
            "review_reasons": list(scope.review_reasons),
            "parent_unit_key": unit.unit_key,
            "source_content_id": unit.content_id,
            "section_id": unit.section_id,
            "page_number": unit.page_number,
            "candidate_source_start": atom.start_in_content,
            "candidate_source_end": atom.end_in_content,
            "candidate_text_hash": atom.text_hash,
            "precedence": "UNDETERMINED",
        }
        digest = sha(payload)
        key = sha([REVIEW_VERSION, version_id, "SCOPE", atom.candidate_key, digest])
        proposals.append(ReviewProposal(key, "SCOPE", atom.candidate_key, version_id,
                                        digest, atom.text_hash, payload))
        if len(proposals) > max_proposals:
            raise ValueError("Review proposal budget exceeded; no partial output")
    for table in tables:
        payload = {
            "header_status": table.header_status,
            "review_reasons": list(table.review_reasons),
            "document_id": table.document_id,
            "content_id": table.content_id,
            "rows": [asdict(r) for r in table.rows],
            "header_assertion": "UNDETERMINED",
        }
        digest = sha(payload)
        key = sha([REVIEW_VERSION, version_id, "TABLE", table.table_key, digest])
        source_text_digest = sha([
            [row.unit_key, [[c.column_index, c.start_in_content,
                             c.end_in_content, c.source_text] for c in row.cells]]
            for row in table.rows
        ])
        proposals.append(ReviewProposal(key, "TABLE", table.table_key, version_id,
                                        digest, source_text_digest, payload))
        if len(proposals) > max_proposals:
            raise ValueError("Review proposal budget exceeded; no partial output")
    if len({x.key for x in proposals}) != len(proposals):
        raise ValueError("Duplicate review proposal key")
    return tuple(proposals)


def validate_review_decision(
    proposal: ReviewProposal, *, status: str, assertion: str | None,
    target_unit_key: str | None, reason: str, reviewer: str,
) -> None:
    """Make claims possible only with supporting *verbatim* span anchors.

    A confirmed decision expresses a HUMAN judgement, not automated verification.
    Inherited/cross-page or unanchored scope claims have no assertion type here.
    """
    if status not in FINAL_STATES:
        raise ValueError("Invalid review status")
    if not (1 <= len(reason.strip()) <= 2000 and 1 <= len(reviewer.strip()) <= 128):
        raise ValueError("Reviewer and explicit reason required")
    if any(ord(ch) < 32 for ch in reviewer):
        raise ValueError("Invalid reviewer identifier")
    if status != "CONFIRMED":
        if assertion is not None or target_unit_key is not None:
            raise ValueError("Non-confirming decision cannot assert an engineering rule")
        return
    if proposal.kind == "SCOPE":
        if target_unit_key is not None or assertion not in SCOPE_ASSERTIONS:
            raise ValueError("Invalid scope assertion or target")
        p = proposal.payload
        if p.get("status") == "UNRESOLVED":
            raise ValueError("Unresolved scope cannot be confirmed without new reviewed evidence")
        if assertion == "ACTION_ONLY" and (p.get("status") != "ACTION_ONLY" or not p.get("action")):
            raise ValueError("Action-only assertion contradicts source")
        if assertion == "CONDITION_GOVERNS_ACTION" and not (p.get("condition") and p.get("action")):
            raise ValueError("Condition/action source anchors required")
        if assertion == "EXCEPTION_OVERRIDES_ACTION" and not (p.get("exception") and p.get("action")):
            raise ValueError("Exception/action source anchors required")
    elif proposal.kind == "TABLE":
        if assertion not in TABLE_ASSERTIONS or not target_unit_key:
            raise ValueError("Table confirmation requires explicit header row unit key")
        p = proposal.payload
        structural_issues = {"COLUMN_COUNT_MISMATCH", "ESCAPED_PIPE_OR_LAYOUT_UNSUPPORTED",
                             "AMBIGUOUS_TABLE_SEPARATOR", "HEADER_LABELS_NOT_UNIQUE"}
        if structural_issues.intersection(p.get("review_reasons") or []):
            raise ValueError("Unsafe table structure requires new corrected source evidence")
        rows = p.get("rows") or []
        found = [r for r in rows if r.get("unit_key") == target_unit_key]
        if len(found) != 1 or found[0].get("row_kind") not in ("HEADER", "UNKNOWN"):
            raise ValueError("Header selection must point to unique source row")
        if not found[0].get("cells") or any(not c.get("source_text", "").strip() for c in found[0]["cells"]):
            raise ValueError("Header cells require original non-empty evidence")
        if p.get("header_status") == "UNRESOLVED" and any(r.get("row_kind") == "SEPARATOR" for r in rows):
            # Ambiguous separators require correction in a new evidence snapshot.
            raise ValueError("Ambiguous table separator: cannot confirm header from incomplete structure")
    else:
        raise ValueError("Unknown review proposal kind")
