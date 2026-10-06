from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence


MAPPING_STATUSES = (
    "EXACT",
    "CONTENT_CHANGED",
    "IDENTITY_CHANGED",
    "CHUNK_MISSING",
    "UNMAPPED",
)


@dataclass(frozen=True)
class ChunkEvidenceMapping:
    evidence_id: int
    relation_id: int | None
    content_id: int | None
    mapping_status: str
    chunk_document_id: str | None = None
    chunk_chapter_id: str | None = None
    chunk_section_id: str | None = None
    chunk_page_number: int | None = None
    chunk_index: int | None = None
    chunk_sort_order: int | None = None
    chunk_token_count: int | None = None
    live_content_hash: str | None = None
    snapshot_content_hash: str | None = None
    chunk_content: str | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "ChunkEvidenceMapping":
        return cls(
            evidence_id=int(row["mapped_evidence_id"]),
            relation_id=_optional_int(row.get("relation_id")),
            content_id=_optional_int(row.get("content_id")),
            mapping_status=str(row.get("mapping_status") or "UNMAPPED"),
            chunk_document_id=_optional_str(row.get("mapped_chunk_document_id")),
            chunk_chapter_id=_optional_str(row.get("mapped_chunk_chapter_id")),
            chunk_section_id=_optional_str(row.get("mapped_chunk_section_id")),
            chunk_page_number=_optional_int(row.get("mapped_chunk_page_number")),
            chunk_index=_optional_int(row.get("mapped_chunk_index")),
            chunk_sort_order=_optional_int(row.get("mapped_chunk_sort_order")),
            chunk_token_count=_optional_int(row.get("mapped_chunk_token_count")),
            live_content_hash=_optional_str(row.get("mapped_chunk_live_content_hash")),
            snapshot_content_hash=_optional_str(row.get("chunk_content_hash_snapshot")),
            chunk_content=_optional_str(row.get("mapped_chunk_content")),
        )


class ChunkEvidenceMappingService:
    """
    Read-only application service for exact Evidence -> Chunk tracing.

    The database contract is deliberately simple:

        relation_evidence.content_id -> contents.id

    document/section/page/chunk_index/content_hash snapshots are audit
    invariants only. They never replace content_id as the mapping key.

    ``connect`` must return a DB-API/psycopg connection. The service does
    not own credentials and never mutates RAG business data.
    """

    def __init__(self, connect: Callable[[], Any]) -> None:
        if not callable(connect):
            raise TypeError("connect must be callable")
        self._connect = connect

    def get(self, evidence_id: int) -> dict[str, Any] | None:
        evidence_id = _positive_int(evidence_id, "evidence_id")
        rows = self._query(
            """
            SELECT *
            FROM public.relation_evidence_chunk_map
            WHERE mapped_evidence_id = %s
            """,
            (evidence_id,),
        )
        return rows[0] if rows else None

    def get_mapping(self, evidence_id: int) -> ChunkEvidenceMapping | None:
        row = self.get(evidence_id)
        return ChunkEvidenceMapping.from_row(row) if row else None

    def list_for_relation(
        self,
        relation_id: int,
        *,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        relation_id = _positive_int(relation_id, "relation_id")
        limit = _bounded_limit(limit)
        return self._query(
            """
            SELECT *
            FROM public.relation_evidence_chunk_map
            WHERE relation_id = %s
            ORDER BY mapped_evidence_id
            LIMIT %s
            """,
            (relation_id, limit),
        )

    def list_by_status(
        self,
        statuses: Sequence[str] | None = None,
        *,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        limit = _bounded_limit(limit, maximum=10000)
        normalized = _normalize_statuses(statuses)
        if not normalized:
            return self._query(
                """
                SELECT *
                FROM public.relation_evidence_chunk_map
                ORDER BY mapped_evidence_id
                LIMIT %s
                """,
                (limit,),
            )

        return self._query(
            """
            SELECT *
            FROM public.relation_evidence_chunk_map
            WHERE mapping_status = ANY(%s)
            ORDER BY mapped_evidence_id
            LIMIT %s
            """,
            (list(normalized), limit),
        )

    def audit_summary(self) -> dict[str, int]:
        rows = self._query(
            """
            SELECT mapping_status, COUNT(*)::BIGINT AS count
            FROM public.relation_evidence_chunk_map
            GROUP BY mapping_status
            ORDER BY mapping_status
            """
        )
        result = {status: 0 for status in MAPPING_STATUSES}
        for row in rows:
            result[str(row["mapping_status"])] = int(row["count"])
        result["TOTAL"] = sum(
            count for status, count in result.items() if status != "TOTAL"
        )
        return result

    def exact_ratio(self) -> float:
        summary = self.audit_summary()
        total = summary.get("TOTAL", 0)
        return 1.0 if total == 0 else summary.get("EXACT", 0) / total

    def open_chunk(self, content_id: int) -> dict[str, Any] | None:
        """Return the canonical PostgreSQL chunk by stable contents.id."""
        content_id = _positive_int(content_id, "content_id")
        rows = self._query(
            """
            SELECT
                c.id AS content_id,
                c.document_id,
                c.chapter_id,
                c.section_id,
                c.page_number,
                c.chunk_index,
                c.sort_order,
                c.token_count,
                c.content_hash,
                c.metadata,
                c.content
            FROM public.contents AS c
            WHERE c.id = %s
            """,
            (content_id,),
        )
        return rows[0] if rows else None

    def assert_no_broken_mappings(self) -> None:
        summary = self.audit_summary()
        broken = {
            status: summary.get(status, 0)
            for status in (
                "CONTENT_CHANGED",
                "IDENTITY_CHANGED",
                "CHUNK_MISSING",
                "UNMAPPED",
            )
            if summary.get(status, 0)
        }
        if broken:
            details = ", ".join(f"{k}={v}" for k, v in sorted(broken.items()))
            raise RuntimeError(f"Chunk/Evidence mapping audit failed: {details}")

    def _query(
        self,
        statement: str,
        params: Iterable[Any] | None = None,
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(statement, tuple(params or ()))
                rows = cur.fetchall()
                return _rows_to_dicts(cur, rows)


def _rows_to_dicts(cur: Any, rows: Iterable[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    description = getattr(cur, "description", None) or ()
    columns = [
        str(getattr(item, "name", item[0]))
        for item in description
    ]
    for row in rows:
        if isinstance(row, Mapping):
            result.append(dict(row))
        else:
            result.append(dict(zip(columns, row, strict=False)))
    return result


def _normalize_statuses(statuses: Sequence[str] | None) -> tuple[str, ...]:
    if not statuses:
        return ()
    normalized: list[str] = []
    for value in statuses:
        status = str(value).strip().upper()
        if status not in MAPPING_STATUSES:
            raise ValueError(
                f"Unknown mapping status '{value}'. Expected one of: "
                + ", ".join(MAPPING_STATUSES)
            )
        if status not in normalized:
            normalized.append(status)
    return tuple(normalized)


def _positive_int(value: Any, name: str) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if normalized <= 0:
        raise ValueError(f"{name} must be greater than 0")
    return normalized


def _bounded_limit(value: Any, *, maximum: int = 5000) -> int:
    normalized = _positive_int(value, "limit")
    if normalized > maximum:
        raise ValueError(f"limit must not exceed {maximum}")
    return normalized


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)
