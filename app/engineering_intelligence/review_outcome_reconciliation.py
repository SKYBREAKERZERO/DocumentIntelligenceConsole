"""Read-only investigation of a lost Stage 2 review COMMIT response.

This never retries a write and cannot prove absence of an in-flight commit.
One authenticated audit event after the saved pre-submit watermark is
required before reporting a matching *observed* commit.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from .content_relation_review import ContentRelationReviewError, _read_dict, load_review_case


@dataclass(frozen=True, slots=True)
class ReviewOutcome:
    candidate_id: int
    status: str
    observed_state: str | None
    latest_audit_id: int | None
    post_watermark_events: int
    production_acceptance: str = "NOT_VERIFIED"

    def as_dict(self) -> dict[str, Any]:
        return {"candidate_id": self.candidate_id, "result": self.status,
                "observed_state": self.observed_state,
                "latest_audit_id": self.latest_audit_id,
                "post_watermark_events": self.post_watermark_events,
                "production_acceptance": self.production_acceptance,
                "automatic_retry_allowed": False}


def reconcile_review_commit(
    cur: Any, *, candidate_id: int, project: str, before_audit_id: int,
    expected_status: str, intended_status: str, source_hash: str,
    target_hash: str, reason_sha256: str,
) -> ReviewOutcome:
    """Compare a fresh read-only snapshot with the recorded pre-submit proof.

    A state match without a new audit is never proof of a committed write.
    If audit rows have advanced more than once, we cannot attribute the head to
    this submission. Reports contain no raw engineering content or reasons.
    """
    permitted = {"REVIEW_REQUIRED": {"CONFIRMED", "REJECTED"},
                 "CONFIRMED": {"REVIEW_REQUIRED"},
                 "REJECTED": {"REVIEW_REQUIRED"}}
    if (candidate_id < 1 or before_audit_id < 0 or
            intended_status not in permitted.get(expected_status, set()) or
            any(len(v) != 64 or any(ch not in '0123456789abcdef' for ch in v)
                for v in (source_hash, target_hash, reason_sha256))):
        raise ContentRelationReviewError("Invalid read-only commit reconciliation proof")
    case = load_review_case(cur, candidate_id=candidate_id, project=project)
    cur.execute("SELECT session_user::text")
    actor = cur.fetchone()
    if actor is None:
        raise ContentRelationReviewError("Authenticated reconciliation user not visible")
    actual_actor = str(actor[0] if not isinstance(actor, dict) else actor["session_user"])
    cur.execute("""
        SELECT id, actor, previous_status, new_status, reason, identity_provenance
        FROM public.ei_relation_review_audit
        WHERE candidate_id=%s AND id>%s
        ORDER BY id LIMIT 2
    """, (candidate_id, before_audit_id))
    columns = tuple(col.name if hasattr(col, "name") else col[0] for col in cur.description)
    events = [event if isinstance(event, dict) else dict(zip(columns, event))
              for event in cur.fetchall()]
    latest = case.last_audit_id
    # Fail closed on inconsistent catalog reads or evidence drift.
    if (case.source.text_hash != source_hash or case.target.text_hash != target_hash
            or (latest or 0) < before_audit_id):
        status = "INDETERMINATE_EVIDENCE_OR_AUDIT_DRIFT"
    elif len(events) == 1 and case.status == intended_status and latest == int(events[0]["id"]):
        event = events[0]
        if (event["actor"] == actual_actor
                and event["identity_provenance"] == "DB_SESSION_USER"
                and event["previous_status"] == expected_status
                and event["new_status"] == intended_status
                and hashlib.sha256(str(event["reason"]).encode("utf-8")).hexdigest() == reason_sha256):
            status = "MATCHING_COMMIT_OBSERVED"
        else:
            status = "INDETERMINATE_AUDIT_IDENTITY_OR_REASON"
    elif not events and case.status == expected_status and (latest or 0) == before_audit_id:
        status = "NO_COMMIT_VISIBLE_AT_SNAPSHOT"
    else:
        status = "INDETERMINATE_CONCURRENT_TRANSITION"
    return ReviewOutcome(candidate_id=candidate_id, status=status,
                         observed_state=case.status, latest_audit_id=latest,
                         post_watermark_events=len(events))
