"""Read-only, project-scoped Stage 1 reviewer queue and verbatim evidence detail.

No approvals, revisions or database writes. The GUI may only submit decisions
through the existing provenance/CAS-checked `record_review_event` function.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .stage1_review import REVIEW_VERSION, sha


@dataclass(frozen=True, slots=True)
class ReviewQueueItem:
    key: str
    kind: str
    digest: str
    status: str
    last_event_id: int
    section_id: str | None
    review_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReviewDetail:
    item: ReviewQueueItem
    payload: dict[str, Any]
    history: tuple[dict[str, Any], ...]


def validate_review_scope(project: str, version_id: int, limit: int = 50) -> None:
    if project not in ("21MM", "24MM") or not isinstance(version_id, int) or version_id < 1:
        raise ValueError("Only an explicit 21MM/24MM specification version is permitted")
    if not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("Review page size must be 1..100")


def check_database_project(cur: Any, project: str) -> None:
    """Refuse an accidentally connected or spoofed project database."""
    if project not in ("21MM", "24MM"):
        raise ValueError("Unsupported review project")
    cur.execute("SELECT project_code FROM public.project_info LIMIT 1")
    identity = cur.fetchone()
    if not identity or str(identity[0]).upper() != project:
        raise ValueError("PostgreSQL database project identity mismatch")


def _receipt(cur: Any, *, project: str, version_id: int) -> None:
    """Require complete source-scoped proposal dataset before displaying evidence."""
    check_database_project(cur, project)
    cur.execute("""SELECT r.proposal_count, r.extraction_version
        FROM public.ei_stage1_review_receipts r
        JOIN public.specification_versions v ON v.id=r.version_id
        JOIN public.specifications s ON s.id=v.specification_id
        WHERE r.version_id=%s AND s.project_code=%s""", (version_id, project))
    row = cur.fetchone()
    if not row or row[1] != REVIEW_VERSION:
        raise ValueError("Stage1 review dataset not migrated/backfilled for this project/version")
    cur.execute("SELECT COUNT(*) FROM public.ei_stage1_review_proposals WHERE version_id=%s", (version_id,))
    count = cur.fetchone()
    if not count or int(count[0]) != int(row[0]):
        raise ValueError("Stage1 review dataset incomplete; refusing inspection")


def _item(row: tuple[Any, ...]) -> ReviewQueueItem:
    key, kind, digest, payload, status, event_id = row
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict) or sha(payload) != str(digest).strip():
        raise ValueError("Review proposal payload digest mismatch")
    if kind not in ("SCOPE", "TABLE") or len(str(key).strip()) != 64:
        raise ValueError("Invalid review proposal identity")
    return ReviewQueueItem(
        str(key).strip(), kind, str(digest).strip(), str(status or "PENDING"),
        int(event_id or 0), payload.get("section_id"),
        tuple(payload.get("review_reasons") or ()),
    )


def _summary_item(row: tuple[Any, ...]) -> ReviewQueueItem:
    key,kind,digest,section_id,reasons,status,event_id = row
    key=str(key).strip();digest=str(digest).strip()
    if (len(key)!=64 or len(digest)!=64 or
        any(ch not in '0123456789abcdef' for ch in key+digest) or kind not in ('SCOPE','TABLE')):
        raise ValueError('Review metadata identity invalid')
    if isinstance(reasons,str):
        reasons=json.loads(reasons)
    if not isinstance(reasons,list) or any(not isinstance(x,str) for x in reasons):
        raise ValueError('Review metadata reason format invalid')
    return ReviewQueueItem(key,kind,digest,str(status or 'PENDING'),int(event_id or 0),
                           section_id,tuple(reasons))


def list_review_queue(cur: Any, *, project: str, version_id: int,
                      limit: int = 50, after_key: str | None = None,
                      kind: str | None = None,
                      status: str | None = None) -> tuple[ReviewQueueItem, ...]:
    validate_review_scope(project, version_id, limit)
    if kind not in (None, "SCOPE", "TABLE"):
        raise ValueError("Invalid review kind")
    if status not in (None, "PENDING", "CONFIRMED", "REJECTED", "NEEDS_CLARIFICATION"):
        raise ValueError("Invalid review status filter")
    if after_key is not None and (len(after_key) != 64 or any(c not in '0123456789abcdef' for c in after_key)):
        raise ValueError("Invalid review cursor")
    _receipt(cur, project=project, version_id=version_id)
    cur.execute("""SELECT p.proposal_key,p.kind,p.evidence_digest,
        p.payload->>'section_id',p.payload->'review_reasons',e.status,e.id
        FROM public.ei_stage1_review_proposals p
        JOIN public.specification_versions v ON v.id=p.version_id
        JOIN public.specifications s ON s.id=v.specification_id
        LEFT JOIN LATERAL (
            SELECT id,status FROM public.ei_stage1_review_events
            WHERE proposal_key=p.proposal_key ORDER BY id DESC LIMIT 1
        ) e ON true
        WHERE p.version_id=%s AND s.project_code=%s
          AND (%s::text IS NULL OR p.kind=%s)
          AND (%s::text IS NULL OR COALESCE(e.status,'PENDING')=%s)
          AND (%s::char(64) IS NULL OR p.proposal_key > %s)
        ORDER BY p.proposal_key LIMIT %s""",
        (version_id, project, kind, kind, status, status, after_key, after_key, limit))
    return tuple(_summary_item(row) for row in cur.fetchall())


def load_review_detail(cur: Any, *, project: str, version_id: int,
                       proposal_key: str, max_history: int = 100) -> ReviewDetail:
    validate_review_scope(project, version_id)
    if len(proposal_key) != 64 or any(c not in '0123456789abcdef' for c in proposal_key):
        raise ValueError("Invalid review proposal key")
    if not 1 <= max_history <= 200:
        raise ValueError("Invalid history budget")
    _receipt(cur, project=project, version_id=version_id)
    cur.execute("""SELECT p.proposal_key,p.kind,p.evidence_digest,p.payload,e.status,e.id
        FROM public.ei_stage1_review_proposals p
        JOIN public.specification_versions v ON v.id=p.version_id
        JOIN public.specifications s ON s.id=v.specification_id
        LEFT JOIN LATERAL (
            SELECT id,status FROM public.ei_stage1_review_events
            WHERE proposal_key=p.proposal_key ORDER BY id DESC LIMIT 1
        ) e ON true
        WHERE p.proposal_key=%s AND p.version_id=%s AND s.project_code=%s""",
        (proposal_key, version_id, project))
    rows = cur.fetchall()
    if len(rows) != 1:
        raise ValueError("Review evidence not found in authorized project/version")
    item = _item(rows[0])
    payload = rows[0][3]
    if isinstance(payload, str):
        payload = json.loads(payload)
    cur.execute("""SELECT id,reviewer,status,assertion,target_unit_key,reason,recorded_at
        FROM public.ei_stage1_review_events WHERE proposal_key=%s
        ORDER BY id DESC LIMIT %s""", (proposal_key, max_history + 1))
    history_rows = cur.fetchall()
    if len(history_rows) > max_history:
        raise ValueError("Review history exceeds display budget; use CLI audit query")
    history = tuple({
        "id": int(r[0]), "reviewer": r[1], "status": r[2],
        "assertion": r[3], "target_unit_key": r[4], "reason": r[5],
        "recorded_at": str(r[6]),
    } for r in history_rows)
    if (history and history[0]["id"] != item.last_event_id) or (not history and item.last_event_id):
        raise ValueError("Review event snapshot inconsistency")
    return ReviewDetail(item, payload, history)
