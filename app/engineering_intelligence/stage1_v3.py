"""Additive, bounded, operator-triggered Stage 1 v3 backfill.

Derived candidate records never modify verified v2 units, reviews or relations.
Every call must run inside a version-scoped PostgreSQL transaction: caller owns
commit/rollback; a failed source/provenance check must roll back this version.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

from .atomic import ATOMIC_VERSION, AtomicCandidate, extract_atomic_candidates
from .persistence import IntelligenceSchemaMissing, load_version_units, require_complete_backfill
from .requirements import RequirementUnit
from .tables import TABLE_VERSION, TableBlockEvidence, extract_table_blocks, verify_table_blocks

STAGE1_V3_VERSION = "stage1-structure-v1"


@dataclass(frozen=True, slots=True)
class Stage1V3Summary:
    version_id: int
    source_units: int
    atomic_candidates: int
    table_blocks: int
    source_digest: str
    review_required: int


def require_stage1_v3_schema(cur: Any) -> None:
    cur.execute("""SELECT to_regclass('public.ei_atomic_candidates'),
                          to_regclass('public.ei_table_evidence'),
                          to_regclass('public.ei_stage1_v3_receipts')""")
    result = cur.fetchone()
    if not result or any(v is None for v in result):
        raise IntelligenceSchemaMissing("Apply database/upgrade_engineering_stage1_v3.sql after v7 and v2")


def _source_fingerprint(units: tuple[RequirementUnit, ...]) -> str:
    d = hashlib.sha256()
    for unit in units:
        for part in (unit.unit_key, unit.document_id, str(unit.content_id),
                     str(unit.start_offset), str(unit.end_offset), unit.text_hash):
            raw = part.encode('utf-8', 'surrogatepass')
            d.update(len(raw).to_bytes(8, 'big'))
            d.update(raw)
    return d.hexdigest()


def prepare_stage1_v3(*, version_id: int, units: tuple[RequirementUnit, ...],
                      max_atomic_candidates: int = 100_000,
                      max_table_blocks: int = 10_000,
                      max_candidates_per_unit: int = 64) -> tuple[Stage1V3Summary,
                                                                  tuple[AtomicCandidate, ...],
                                                                  tuple[TableBlockEvidence, ...]]:
    if version_id <= 0 or max_atomic_candidates < 1 or max_table_blocks < 1:
        raise ValueError("Invalid Stage1 v3 resource limits")
    candidates: list[AtomicCandidate] = []
    for unit in units:
        candidates.extend(extract_atomic_candidates(unit, max_candidates=max_candidates_per_unit))
        if len(candidates) > max_atomic_candidates:
            raise ValueError("Stage1 v3 atomic budget exceeded; no partial backfill")
    tables = extract_table_blocks(units)
    if len(tables) > max_table_blocks:
        raise ValueError("Stage1 v3 table budget exceeded; no partial backfill")
    verify_table_blocks(units, tables)
    summary = Stage1V3Summary(
        version_id=version_id, source_units=len(units), atomic_candidates=len(candidates),
        table_blocks=len(tables), source_digest=_source_fingerprint(units),
        review_required=sum(c.review_status == 'REVIEW_REQUIRED' for c in candidates),
    )
    return summary, tuple(candidates), tables


def verify_live_source_snapshot(cur: Any, *, version_id: int, max_rows: int = 100_000) -> None:
    """Recompute the original length-prefixed source digest, including blank rows.

    A receipt row count alone cannot detect a same-length source mutation.
    This deterministic check is IO-heavy but runs only in explicit offline
    backfill, never when a user asks a chat question.
    """
    if max_rows < 1:
        raise ValueError("Invalid live snapshot row budget")
    cur.execute("""SELECT r.document_id,r.source_digest
                   FROM public.ei_version_backfill_receipts r
                   JOIN public.specification_versions v ON v.id=r.version_id
                   WHERE r.version_id=%s AND r.document_id=v.document_id""", (version_id,))
    expected = cur.fetchone()
    if not expected:
        raise ValueError("Source backfill receipt is missing or has wrong document identity")
    document_id, original_digest = expected
    fingerprint = hashlib.sha256()
    last_id = 0
    count = 0
    while count < max_rows:
        cur.execute("""SELECT id,section_id,page_number,content
                       FROM public.contents
                       WHERE document_id=%s AND id>%s ORDER BY id LIMIT %s""",
                    (document_id,last_id,min(200,max_rows-count)))
        rows = cur.fetchall()
        if not rows:
            break
        for content_id, section_id, page_number, body in rows:
            last_id = int(content_id)
            for field in (str(content_id),str(section_id),str(page_number),str(body or '')):
                raw = field.encode('utf-8','surrogatepass')
                fingerprint.update(len(raw).to_bytes(8,'big'))
                fingerprint.update(raw)
        count += len(rows)
    if count == max_rows:
        cur.execute('SELECT 1 FROM public.contents WHERE document_id=%s AND id>%s LIMIT 1',
                    (document_id,last_id))
        if cur.fetchone():
            raise ValueError("Source row budget exhausted; refusing partial source verification")
    if fingerprint.hexdigest() != str(original_digest).strip():
        raise ValueError("Current SQL source differs from immutable backfill receipt")


def _verify_existing_derived(cur: Any, version_id: int,
                             candidates: tuple[AtomicCandidate,...],
                             tables: tuple[TableBlockEvidence,...]) -> None:
    """Verify persisted derived rows before idempotent insert; never rewrite reviews."""
    expected = {c.candidate_key: c for c in candidates}
    cur.execute("""SELECT candidate_key,source_text_hash,source_text,
                          start_in_unit,end_in_unit,start_in_content,end_in_content,
                          candidate_role
                   FROM public.ei_atomic_candidates WHERE version_id=%s""",(version_id,))
    for row in cur.fetchall():
        key, digest, text, left, right, absleft, absright, role = row
        item = expected.get(str(key).strip())
        if (item is None or str(digest).strip() != item.text_hash or text != item.text
            or int(left) != item.start_in_unit or int(right) != item.end_in_unit
            or int(absleft) != item.start_in_content or int(absright) != item.end_in_content
            or role != item.role):
            raise ValueError("Existing Stage1 v3 atomic evidence drift; refusing overwrite")
    existing_tables = {t.table_key: t for t in tables}
    cur.execute("""SELECT table_key,header_status,rows,review_reasons
                   FROM public.ei_table_evidence WHERE version_id=%s""",(version_id,))
    for key, status, rows, reasons in cur.fetchall():
        expected_table = existing_tables.get(str(key).strip())
        if isinstance(rows,str): rows=json.loads(rows)
        if isinstance(reasons,str): reasons=json.loads(reasons)
        if (expected_table is None or status != expected_table.header_status
            or rows != json.loads(json.dumps([asdict(row) for row in expected_table.rows], ensure_ascii=False))
            or reasons != list(expected_table.review_reasons)):
            raise ValueError("Existing Stage1 v3 table evidence drift; refusing overwrite")


def backfill_stage1_v3(cur: Any, *, version_id: int, dry_run: bool = True,
                       max_units: int = 40_000, max_atomic_candidates: int = 100_000,
                       max_table_blocks: int = 10_000) -> Stage1V3Summary:
    """Source-read only in preview mode; explicit additive writes with receipts.

    Existing reviewed candidate rows are kept unchanged on replay. The receipt
    fingerprints input units and detects drift instead of replacing history.
    """
    if max_units < 1:
        raise ValueError("max_units must be positive")
    require_complete_backfill(cur, version_id)
    if not dry_run:
        require_stage1_v3_schema(cur)
        # PostgreSQL transaction advisory lock protects independent backfills
        # for one version from interleaving; it is released by commit/rollback.
        cur.execute("SELECT pg_advisory_xact_lock(51230, %s)", (version_id,))
    verify_live_source_snapshot(cur, version_id=version_id)
    units = load_version_units(cur, version_id, max_units=max_units + 1)
    if len(units) > max_units:
        raise ValueError("Version source units exceed budget; refusing partial enrichment")
    summary, candidates, tables = prepare_stage1_v3(
        version_id=version_id, units=units,
        max_atomic_candidates=max_atomic_candidates, max_table_blocks=max_table_blocks,
    )
    if dry_run:
        return summary
    _verify_existing_derived(cur, version_id, candidates, tables)
    unit_keys = {u.unit_key for u in units}
    if len(unit_keys) != len(units):
        raise ValueError("Duplicate source unit keys")
    statement = """INSERT INTO public.ei_atomic_candidates (
        candidate_key, source_unit_id, version_id, extraction_version,
        start_in_unit, end_in_unit, start_in_content, end_in_content,
        source_text_hash, source_text, candidate_role, review_status,
        review_reasons, source_cues)
        VALUES (%s,(SELECT id FROM public.ei_requirement_units WHERE unit_key=%s),
                %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb)
        ON CONFLICT (candidate_key) DO NOTHING"""
    # Bounded batches protect parameter buffers on large PDF-derived documents.
    for start in range(0, len(candidates), 250):
        cur.executemany(statement, [
            (c.candidate_key, c.parent_unit_key, version_id, ATOMIC_VERSION,
             c.start_in_unit, c.end_in_unit, c.start_in_content, c.end_in_content,
             c.text_hash, c.text, c.role, c.review_status,
             json.dumps(c.review_reasons, ensure_ascii=False),
             json.dumps(c.cue_spans, ensure_ascii=False))
            for c in candidates[start:start + 250]
        ])
    table_sql = """INSERT INTO public.ei_table_evidence (
        table_key,version_id,document_id,content_id,extraction_version,
        header_status,rows,review_reasons)
        VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb)
        ON CONFLICT(table_key) DO NOTHING"""
    for start in range(0, len(tables), 250):
        cur.executemany(table_sql, [
            (t.table_key, version_id, t.document_id, t.content_id,TABLE_VERSION,
             t.header_status,json.dumps([asdict(r) for r in t.rows], ensure_ascii=False),
             json.dumps(t.review_reasons, ensure_ascii=False))
            for t in tables[start:start + 250]
        ])
    # Count and immutable receipt are a completeness gate; no partial success.
    cur.execute("SELECT COUNT(*) FROM public.ei_atomic_candidates WHERE version_id=%s", (version_id,))
    count_atomic = int(cur.fetchone()[0])
    cur.execute("SELECT COUNT(*) FROM public.ei_table_evidence WHERE version_id=%s", (version_id,))
    count_tables = int(cur.fetchone()[0])
    if count_atomic != summary.atomic_candidates or count_tables != summary.table_blocks:
        raise ValueError("Derived row count differs from source; roll back transaction")
    cur.execute("""INSERT INTO public.ei_stage1_v3_receipts(
        version_id,source_units_digest,source_unit_count,atomic_candidate_count,
        table_block_count,extraction_version)
        VALUES (%s,%s,%s,%s,%s,%s)
        ON CONFLICT(version_id) DO UPDATE SET completed_at=NOW()
        WHERE public.ei_stage1_v3_receipts.source_units_digest=EXCLUDED.source_units_digest
          AND public.ei_stage1_v3_receipts.source_unit_count=EXCLUDED.source_unit_count
          AND public.ei_stage1_v3_receipts.atomic_candidate_count=EXCLUDED.atomic_candidate_count
          AND public.ei_stage1_v3_receipts.table_block_count=EXCLUDED.table_block_count
          AND public.ei_stage1_v3_receipts.extraction_version=EXCLUDED.extraction_version
        RETURNING version_id""", (version_id, summary.source_digest,
            summary.source_units, summary.atomic_candidates, summary.table_blocks, STAGE1_V3_VERSION))
    if cur.fetchone() is None:
        raise ValueError("Stage1 v3 source receipt drift; roll back transaction")
    return summary
