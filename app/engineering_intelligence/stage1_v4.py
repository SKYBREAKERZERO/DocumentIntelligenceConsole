"""Stage1 v4 explicit review candidate backfill; source-verified and additive.

Operators must run this inside a REPEATABLE READ PostgreSQL transaction.
No mutation of old requirements, relations or Stage1 v3 evidence is permitted.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .persistence import IntelligenceSchemaMissing, load_version_units, require_complete_backfill
from .stage1_review import (REVIEW_VERSION, ReviewProposal, canonical_json,
                            prepare_review_proposals, sha)
from .stage1_v3 import (STAGE1_V3_VERSION, _verify_existing_derived,
                        prepare_stage1_v3, require_stage1_v3_schema,
                        verify_live_source_snapshot)


@dataclass(frozen=True, slots=True)
class Stage1V4Summary:
    version_id: int
    source_units: int
    scope_proposals: int
    table_proposals: int
    source_digest: str
    proposal_digest: str


def require_stage1_v4_schema(cur: Any) -> None:
    cur.execute("""SELECT to_regclass('public.ei_stage1_review_proposals'),
                          to_regclass('public.ei_stage1_review_receipts'),
                          to_regclass('public.ei_stage1_review_events')""")
    row = cur.fetchone()
    if not row or any(x is None for x in row):
        raise IntelligenceSchemaMissing("Apply database/upgrade_engineering_stage1_v4.sql explicitly")


def _check_stage1_v3_complete(cur: Any, *, version_id: int, source_digest: str,
                              unit_count: int, atoms_count: int, tables_count: int) -> None:
    cur.execute("""SELECT source_units_digest,source_unit_count,atomic_candidate_count,
                          table_block_count,extraction_version
                   FROM public.ei_stage1_v3_receipts WHERE version_id=%s""", (version_id,))
    row = cur.fetchone()
    if not row or (str(row[0]).strip(), int(row[1]), int(row[2]), int(row[3]), row[4]) != (
        source_digest, unit_count, atoms_count, tables_count, STAGE1_V3_VERSION
    ):
        raise ValueError("Stage1 v3 receipt missing/drifted; refuse v4 review proposals")
    cur.execute("SELECT COUNT(*) FROM public.ei_atomic_candidates WHERE version_id=%s",(version_id,))
    if int(cur.fetchone()[0]) != atoms_count:
        raise ValueError("Stage1 v3 atomic candidates incomplete")
    cur.execute("SELECT COUNT(*) FROM public.ei_table_evidence WHERE version_id=%s",(version_id,))
    if int(cur.fetchone()[0]) != tables_count:
        raise ValueError("Stage1 v3 table evidence incomplete")


def _check_existing(cur: Any, *, version_id: int, proposals: tuple[ReviewProposal,...]) -> None:
    lookup = {p.key:p for p in proposals}
    cur.execute("""SELECT proposal_key,kind,source_key,extraction_version,
                          evidence_digest,source_text_digest,payload
                   FROM public.ei_stage1_review_proposals WHERE version_id=%s""", (version_id,))
    for key,kind,source_key,extractor,digest,source_digest,payload in cur.fetchall():
        actual = lookup.get(str(key).strip())
        if isinstance(payload,str):
            import json
            payload=json.loads(payload)
        if (actual is None or kind != actual.kind or str(source_key).strip() != actual.source_key
            or extractor != REVIEW_VERSION or str(digest).strip() != actual.evidence_digest
            or str(source_digest).strip() != actual.source_text_digest
            or canonical_json(payload) != canonical_json(actual.payload)):
            raise ValueError("Existing Stage1 v4 review evidence drift; do not overwrite")


def backfill_stage1_v4(cur: Any, *, version_id: int, dry_run: bool = True,
                       max_units: int = 40_000, max_atomic_candidates: int = 100_000,
                       max_tables: int = 10_000, max_proposals: int = 120_000) -> Stage1V4Summary:
    if version_id < 1 or min(max_units,max_atomic_candidates,max_tables,max_proposals) < 1:
        raise ValueError("Invalid Stage1 v4 resource limits")
    require_complete_backfill(cur,version_id)
    require_stage1_v3_schema(cur)
    if not dry_run:
        require_stage1_v4_schema(cur)
        cur.execute("SELECT pg_advisory_xact_lock(51231,%s)", (version_id,))
    verify_live_source_snapshot(cur,version_id=version_id)
    units = load_version_units(cur,version_id,max_units=max_units+1)
    if len(units) > max_units:
        raise ValueError("Stage1 v4 unit budget exceeded")
    summary,atoms,tables = prepare_stage1_v3(
        version_id=version_id,units=units,max_atomic_candidates=max_atomic_candidates,
        max_table_blocks=max_tables,
    )
    _check_stage1_v3_complete(cur,version_id=version_id,source_digest=summary.source_digest,
                             unit_count=len(units),atoms_count=len(atoms),tables_count=len(tables))
    _verify_existing_derived(cur,version_id,atoms,tables)
    proposals = prepare_review_proposals(version_id=version_id,units=units,atoms=atoms,
                                         tables=tables,max_proposals=max_proposals)
    p_digest=sha([[x.key,x.evidence_digest,x.source_text_digest] for x in proposals])
    result=Stage1V4Summary(version_id,len(units),sum(x.kind=='SCOPE' for x in proposals),
                           sum(x.kind=='TABLE' for x in proposals),summary.source_digest,p_digest)
    if dry_run:
        return result
    _check_existing(cur,version_id=version_id,proposals=proposals)
    sql="""INSERT INTO public.ei_stage1_review_proposals (
          proposal_key,kind,source_key,version_id,extraction_version,
          evidence_digest,source_text_digest,payload)
          VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
          ON CONFLICT(proposal_key) DO NOTHING"""
    for index in range(0,len(proposals),200):
        cur.executemany(sql,[(p.key,p.kind,p.source_key,p.version_id,REVIEW_VERSION,
                              p.evidence_digest,p.source_text_digest,canonical_json(p.payload))
                             for p in proposals[index:index+200]])
    cur.execute("SELECT COUNT(*) FROM public.ei_stage1_review_proposals WHERE version_id=%s",(version_id,))
    if int(cur.fetchone()[0]) != len(proposals):
        raise ValueError("Stage1 v4 proposal count mismatch; roll back")
    cur.execute("""INSERT INTO public.ei_stage1_review_receipts (
          version_id,source_units_digest,proposal_set_digest,source_unit_count,
          proposal_count,extraction_version) VALUES (%s,%s,%s,%s,%s,%s)
          ON CONFLICT(version_id) DO NOTHING""",
        (version_id,summary.source_digest,p_digest,len(units),len(proposals),REVIEW_VERSION))
    cur.execute("""SELECT source_units_digest,proposal_set_digest,source_unit_count,
                          proposal_count,extraction_version
                   FROM public.ei_stage1_review_receipts WHERE version_id=%s""",(version_id,))
    row=cur.fetchone()
    if not row or (str(row[0]).strip(),str(row[1]).strip(),int(row[2]),int(row[3]),row[4]) != (
        summary.source_digest,p_digest,len(units),len(proposals),REVIEW_VERSION
    ):
        raise ValueError("Stage1 v4 receipt drift; roll back")
    return result
