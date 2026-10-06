from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .qdrant_sync import QABUG_COLLECTION, QABUG_PAYLOAD_INDEX_CONTRACT


@dataclass(frozen=True, slots=True)
class QABugQualityReport:
    status: str
    sql_records: int
    sql_chunks: int
    qdrant_points: int
    missing_points: int
    orphan_points: int
    stale_points: int
    missing_payload_indexes: tuple[str, ...]
    mismatched_payload_indexes: tuple[str, ...]
    warnings: tuple[str, ...] = ()
    quarantined_records: int = 0


class QABugDataQualityValidator:
    """Cross-store validator for the dedicated QA/BUG SQL/Qdrant domain."""

    def __init__(self, *, postgres_profile, password: str, qdrant_service, collection_name: str = QABUG_COLLECTION) -> None:
        self.postgres = postgres_profile
        self.password = password
        self.qdrant = qdrant_service
        self.collection_name = collection_name

    def validate(self) -> QABugQualityReport:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("psycopg is required for QA/BUG quality validation") from exc
        kwargs = self.postgres.dsn_kwargs(); kwargs["password"] = self.password
        with psycopg.connect(**kwargs, row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM public.qabug_records WHERE is_current AND is_queryable")
            records = int(cur.fetchone()["n"])
            cur.execute("SELECT COUNT(*) AS n FROM public.qabug_records WHERE is_current AND NOT is_queryable")
            quarantined = int(cur.fetchone()["n"])
            cur.execute(
                """
                SELECT c.id,c.qdrant_point_id,c.content_hash,c.index_hash
                FROM public.qabug_chunks c
                JOIN public.qabug_records r ON r.id=c.record_id
                WHERE c.is_current AND r.is_current AND r.is_queryable
                """
            )
            rows = [dict(r) for r in cur.fetchall()]
        sql_by_point = {int(r["qdrant_point_id"]): r for r in rows}
        schema = self.qdrant.payload_schema(self.collection_name) if self.qdrant.collection_exists(self.collection_name) else {}
        missing_indexes = tuple(k for k,v in QABUG_PAYLOAD_INDEX_CONTRACT.items() if not schema.get(k))
        mismatched = tuple(k for k,v in QABUG_PAYLOAD_INDEX_CONTRACT.items() if schema.get(k) and schema.get(k) != v)
        qpoints = self.qdrant.scroll_all_points(self.collection_name) if self.qdrant.collection_exists(self.collection_name) else []
        q_by_point = {int(p["id"]): dict(p.get("payload") or {}) for p in qpoints}
        missing = set(sql_by_point) - set(q_by_point)
        orphan = set(q_by_point) - set(sql_by_point)
        stale = 0
        for pid in set(sql_by_point) & set(q_by_point):
            sql_row = sql_by_point[pid]; payload = q_by_point[pid]
            if str(sql_row.get("content_hash") or "") != str(payload.get("content_hash") or "") or str(sql_row.get("index_hash") or "") != str(payload.get("index_hash") or ""):
                stale += 1
        status = "PASS" if not missing and not orphan and not stale and not missing_indexes and not mismatched else "FAIL"
        warnings = (f"Quarantined non-queryable records: {quarantined}",) if quarantined else ()
        return QABugQualityReport(
            status, records, len(rows), len(qpoints), len(missing), len(orphan), stale,
            missing_indexes, mismatched, warnings, quarantined,
        )
