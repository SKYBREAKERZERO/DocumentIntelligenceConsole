"""Permission-scoped, optimistic human-review events for Stage1 v4.

Review is a human decision about specific anchored evidence; source changes,
ambiguous scopes and concurrency conflicts cannot be silently accepted.
Caller must use a write transaction and a privileged, audited reviewer DB role.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from .stage1_review import REVIEW_VERSION, ReviewProposal, sha, validate_review_decision


def record_review_event(cur: Any, *, version_id: int, project: str,
                        proposal_key: str, expected_evidence_digest: str,
                        expected_last_event_id: int, request_id: str,
                        reviewer: str, reason: str, status: str,
                        assertion: str | None = None, target_unit_key: str | None = None) -> int:
    """CAS append with exact project/version identity and immutable evidence digest.

    `expected_last_event_id=0` means there have been no previous decisions.
    The proposal SELECT FOR UPDATE serializes competing reviewers.
    """
    if version_id < 1 or expected_last_event_id < 0 or project not in ("21MM","24MM"):
        raise ValueError("Invalid review version/project/event expectation")
    for field in (proposal_key,expected_evidence_digest):
        if len(field) != 64 or any(ch not in '0123456789abcdef' for ch in field):
            raise ValueError("Expected exact lowercase SHA256 evidence identity")
    event_uuid = str(uuid.UUID(request_id))
    # Authority scope is DB-validated, not LLM-provided.
    cur.execute("""SELECT p.kind,p.source_key,p.extraction_version,p.evidence_digest,
                          p.source_text_digest,p.payload
                   FROM public.ei_stage1_review_proposals p
                   JOIN public.specification_versions v ON v.id=p.version_id
                   JOIN public.specifications s ON s.id=v.specification_id
                   WHERE p.proposal_key=%s AND p.version_id=%s AND s.project_code=%s
                   FOR UPDATE OF p""",(proposal_key,version_id,project))
    row=cur.fetchone()
    if not row:
        raise ValueError("Review proposal unavailable in authorized project/version")
    kind,source_key,extractor,evidence_digest,source_digest,payload=row
    if isinstance(payload,str):
        payload=json.loads(payload)
    proposal=ReviewProposal(proposal_key,kind,str(source_key).strip(),version_id,
                            str(evidence_digest).strip(),str(source_digest).strip(),payload)
    if (extractor != REVIEW_VERSION or proposal.evidence_digest != expected_evidence_digest
        or sha(payload) != proposal.evidence_digest):
        raise ValueError("Review proposal source/provenance drift")
    cur.execute("""SELECT source_units_digest,proposal_set_digest,source_unit_count,proposal_count,
                          extraction_version
                   FROM public.ei_stage1_review_receipts WHERE version_id=%s""",(version_id,))
    receipt=cur.fetchone()
    if not receipt or receipt[4] != REVIEW_VERSION:
        raise ValueError("Review backfill not completed")
    cur.execute("""SELECT source_units_digest FROM public.ei_stage1_v3_receipts
                   WHERE version_id=%s""",(version_id,))
    v3_receipt = cur.fetchone()
    if not v3_receipt or str(v3_receipt[0]).strip() != str(receipt[0]).strip():
        raise ValueError("Stage1 review/v3 source receipt mismatch")
    cur.execute("SELECT COUNT(*) FROM public.ei_stage1_review_proposals WHERE version_id=%s",
                (version_id,))
    row_count=cur.fetchone()
    if not row_count or int(row_count[0]) != int(receipt[3]):
        raise ValueError("Stage1 review proposal set is incomplete or has unexpected rows")
    # Raw source must still match original import receipt on review day.
    from .stage1_v3 import verify_live_source_snapshot
    verify_live_source_snapshot(cur,version_id=version_id)
    validate_review_decision(proposal,status=status,assertion=assertion,
                             target_unit_key=target_unit_key,reason=reason,reviewer=reviewer)
    cur.execute("""SELECT id FROM public.ei_stage1_review_events
                   WHERE proposal_key=%s ORDER BY id DESC LIMIT 1""",(proposal_key,))
    latest=cur.fetchone()
    actual=int(latest[0]) if latest else 0
    if actual != expected_last_event_id:
        raise ValueError("Review conflict: latest event changed; refresh before deciding")
    cur.execute("""INSERT INTO public.ei_stage1_review_events (
         request_id,proposal_key,evidence_digest,previous_event_id,reviewer,
         status,assertion,target_unit_key,reason)
         VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (event_uuid,proposal.key,proposal.evidence_digest,actual or None,reviewer.strip(),
         status,assertion,target_unit_key,reason.strip()))
    result=cur.fetchone()
    if not result:
        raise ValueError("Review event not written")
    return int(result[0])
