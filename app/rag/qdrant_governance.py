from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.enterprise.profiles import PostgresProfile
from app.enterprise.qdrant_service import QdrantService


# The collection is already scope-specific, so the contract deliberately
# focuses on fields that materially constrain enterprise retrieval. Low-value
# display-only fields stay unindexed to avoid unnecessary memory overhead.
PAYLOAD_INDEX_CONTRACT: dict[str, str] = {
    # High-selectivity identity/filter fields used by Agentic RAG. The
    # collection itself already guarantees scope + CURRENT semantics, so
    # project_code/is_current/index_scope are validated as invariants rather
    # than indexed with low-cardinality structures.
    "document_id": "keyword",
    "specification_id": "integer",
    "specification_key": "keyword",
    "spec_code": "keyword",
    "specification_version_id": "integer",
    "version": "keyword",
    "section_id": "keyword",
    "spec_type": "keyword",
    "spec_subtype": "keyword",
    "region_scope": "keyword",
    # QA/BUG/Jira issue-register filters. These remain empty for specification
    # points and become selective when a scope mixes specifications and issue rows.
    "artifact_type": "keyword",
    "issue_key": "keyword",
    "issue_type": "keyword",
    "issue_status": "keyword",
    "priority": "keyword",
    "component": "keyword",
    "assignee": "keyword",
}

REQUIRED_PAYLOAD_FIELDS: tuple[str, ...] = (
    "content_id",
    "chunk_key",
    "content_hash",
    "index_hash",
    "project_code",
    "document_id",
    "file_name",
    "file_type",
    "specification_key",
    "spec_code",
    "specification_name",
    "version",
    "is_current",
    "section_id",
    "chunk_index",
    "embedding_model",
    "embedding_version",
    "index_scope",
    "source_view",
)


@dataclass(frozen=True, slots=True)
class QualityIssue:
    severity: str
    code: str
    message: str
    content_id: int | None = None
    field_name: str = ""


@dataclass(frozen=True, slots=True)
class QdrantQualityReport:
    project_code: str
    database_name: str
    collection_name: str
    collection_status: str
    vector_size: int
    distance: str
    sql_count: int
    qdrant_count: int
    payload_index_expected: int
    payload_index_present: int
    missing_payload_indexes: tuple[str, ...] = ()
    mismatched_payload_indexes: tuple[str, ...] = ()
    missing_point_ids: tuple[int, ...] = ()
    orphan_point_ids: tuple[int, ...] = ()
    stale_hash_count: int = 0
    stale_index_hash_count: int = 0
    stale_embedding_count: int = 0
    embedding_state_error_count: int = 0
    payload_error_count: int = 0
    payload_type_error_count: int = 0
    duplicate_chunk_key_count: int = 0
    wrong_scope_count: int = 0
    not_current_count: int = 0
    point_identity_error_count: int = 0
    version_conflict_count: int = 0
    empty_classification_count: int = 0
    issues: tuple[QualityIssue, ...] = ()

    @property
    def error_count(self) -> int:
        return sum(1 for issue in self.issues if issue.severity == "ERROR")

    @property
    def warning_count(self) -> int:
        return sum(1 for issue in self.issues if issue.severity == "WARN")

    @property
    def status(self) -> str:
        if self.error_count:
            return "FAIL"
        if self.warning_count:
            return "WARN"
        return "PASS"

    def summary_lines(self) -> list[str]:
        lines = [
            f"Data Quality {self.project_code}: {self.status}",
            f"SQL current={self.sql_count} · Qdrant={self.qdrant_count} · collection={self.collection_status}",
            f"Vector={self.vector_size} · distance={self.distance}",
            f"Payload indexes={self.payload_index_present}/{self.payload_index_expected}",
            f"Missing points={len(self.missing_point_ids)} · Orphans={len(self.orphan_point_ids)}",
            f"Stale content hashes={self.stale_hash_count} · Stale index hashes={self.stale_index_hash_count} · Stale embeddings={self.stale_embedding_count}",
            f"Embedding-state errors={self.embedding_state_error_count}",
            f"Payload errors={self.payload_error_count} · Type errors={self.payload_type_error_count} · Duplicate chunk keys={self.duplicate_chunk_key_count}",
            f"Identity errors={self.point_identity_error_count}",
            f"Wrong scope={self.wrong_scope_count} · Not current={self.not_current_count}",
            f"Version conflicts={self.version_conflict_count} · Empty classifications={self.empty_classification_count}",
        ]
        if self.missing_payload_indexes:
            lines.append("Missing indexes: " + ", ".join(self.missing_payload_indexes))
        if self.mismatched_payload_indexes:
            lines.append("Mismatched indexes: " + ", ".join(self.mismatched_payload_indexes))
        return lines


class QdrantDataQualityValidator:
    """Validate SQL/Qdrant current-index integrity and retrieval governance.

    PostgreSQL remains authoritative. Qdrant is treated as a derived index and
    is therefore validated against SQL identity, hash, current-version and
    embedding state instead of being trusted independently.
    """

    _VERSION_MARKER = re.compile(
        r"(?i)\b(?:ver(?:sion)?|rev(?:ision)?)\.?\s*(?:[:|=\-]\s*)*"
        r"(?:v|rev)?\s*([0-9]+(?:\.[0-9A-Za-z]+)+)"
    )

    def __init__(
        self,
        *,
        postgres: PostgresProfile,
        postgres_password: str,
        qdrant: QdrantService,
        project_code: str,
        collection_name: str,
        embedding_model: str,
        embedding_version: str,
        vector_dimension: int,
    ) -> None:
        self.postgres = postgres
        self.postgres_password = str(postgres_password or "")
        self.qdrant = qdrant
        self.project_code = str(project_code or "").strip().upper()
        self.collection_name = str(collection_name or "").strip()
        self.embedding_model = str(embedding_model or "").strip()
        self.embedding_version = str(embedding_version or "").strip()
        self.vector_dimension = int(vector_dimension)

    def _connect(self):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover - runtime dependency path
            raise RuntimeError("psycopg is not installed") from exc
        kwargs = self.postgres.dsn_kwargs()
        kwargs["password"] = self.postgres_password
        return psycopg.connect(**kwargs, row_factory=dict_row)

    @staticmethod
    def _normalize_version(value: Any) -> str:
        raw = str(value or "").strip().lower()
        raw = re.sub(r"^(?:version|ver|rev(?:ision)?)\.?\s*", "", raw)
        raw = re.sub(r"^[vr]\s*", "", raw)
        return raw.strip(" ._-:")

    def _source_rows(self) -> dict[int, dict[str, Any]]:
        sql = """
            SELECT
                content_id, document_id, project_code, spec_type, spec_subtype,
                region_scope, specification_id, specification_key,
                specification_code, specification_name, specification_version_id,
                specification_version, chapter_id, section_id, section_title,
                content_hash, embedding_status, embedding_model, embedding_version,
                embedded_content_hash, content
            FROM public.rag_current_chunks
            WHERE project_code = %s
              AND LENGTH(BTRIM(content)) > 0
            ORDER BY content_id
        """
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (self.project_code,))
            return {int(row["content_id"]): dict(row) for row in cur.fetchall()}

    def _embedding_state(self) -> dict[int, dict[str, Any]]:
        sql = """
            SELECT content_id, qdrant_point_id, content_hash, index_hash,
                   vector_dimension, is_current_index, model_name, model_version,
                   qdrant_collection
            FROM public.embeddings
            WHERE qdrant_collection = %s
              AND model_name = %s
              AND model_version = %s
        """
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(sql, (self.collection_name, self.embedding_model, self.embedding_version))
                return {int(row["content_id"]): dict(row) for row in cur.fetchall()}
        except Exception:
            # Older schemas can still be inspected. The caller will surface
            # embedding freshness from rag_current_chunks instead.
            return {}

    def validate(self) -> QdrantQualityReport:
        issues: list[QualityIssue] = []
        source = self._source_rows()
        state = self._embedding_state()

        if not self.qdrant.collection_exists(self.collection_name):
            issues.append(QualityIssue("ERROR", "COLLECTION_MISSING", f"Collection {self.collection_name} does not exist."))
            return QdrantQualityReport(
                project_code=self.project_code,
                database_name=self.postgres.database,
                collection_name=self.collection_name,
                collection_status="missing",
                vector_size=0,
                distance="-",
                sql_count=len(source),
                qdrant_count=0,
                payload_index_expected=len(PAYLOAD_INDEX_CONTRACT),
                payload_index_present=0,
                missing_payload_indexes=tuple(PAYLOAD_INDEX_CONTRACT),
                missing_point_ids=tuple(sorted(source)),
                issues=tuple(issues),
            )

        summaries = {item.name: item for item in self.qdrant.list_collections()}
        summary = summaries.get(self.collection_name)
        vector_size = 0
        distance = "-"
        collection_status = "unknown"
        qdrant_count = self.qdrant.count_collection(self.collection_name)
        if summary is not None:
            collection_status = summary.status
            distance = summary.distance
            try:
                vector_size = int(str(summary.vector_size).split(":")[-1])
            except (TypeError, ValueError):
                vector_size = 0

        if vector_size != self.vector_dimension:
            issues.append(QualityIssue("ERROR", "VECTOR_DIMENSION_MISMATCH", f"Expected {self.vector_dimension}, got {vector_size}."))
        if "COSINE" not in str(distance).upper():
            issues.append(QualityIssue("ERROR", "DISTANCE_MISMATCH", f"Expected Cosine, got {distance}."))
        if "GREEN" not in str(collection_status).upper():
            issues.append(QualityIssue("WARN", "COLLECTION_NOT_GREEN", f"Collection status is {collection_status}."))

        schema = self.qdrant.payload_schema(self.collection_name)
        missing_indexes: list[str] = []
        mismatched_indexes: list[str] = []
        for field_name, expected in PAYLOAD_INDEX_CONTRACT.items():
            actual = schema.get(field_name, "")
            if not actual:
                missing_indexes.append(field_name)
            elif actual != expected:
                mismatched_indexes.append(f"{field_name}:{actual}->{expected}")
        if missing_indexes:
            issues.append(QualityIssue("WARN", "PAYLOAD_INDEX_MISSING", f"Missing {len(missing_indexes)} approved payload indexes."))
        if mismatched_indexes:
            issues.append(QualityIssue("ERROR", "PAYLOAD_INDEX_TYPE_MISMATCH", "One or more payload index types do not match the approved contract."))

        points = self.qdrant.scroll_all_points(self.collection_name)
        qdrant_ids: set[int] = set()
        payload_error_count = 0
        wrong_scope_count = 0
        not_current_count = 0
        identity_error_count = 0
        stale_hash_count = 0
        stale_index_hash_count = 0
        payload_type_error_count = 0
        duplicate_chunk_key_count = 0
        empty_classification_count = 0
        seen_chunk_keys: dict[str, int] = {}
        payload_by_content_id: dict[int, dict[str, Any]] = {}

        for point in points:
            payload = dict(point.get("payload") or {})
            point_id_raw = point.get("id")
            content_id_raw = payload.get("content_id")
            try:
                point_id = int(point_id_raw)
                content_id = int(content_id_raw)
            except (TypeError, ValueError):
                payload_error_count += 1
                issues.append(QualityIssue("ERROR", "POINT_ID_INVALID", f"Invalid point/content identity: point={point_id_raw!r}, content_id={content_id_raw!r}."))
                continue
            qdrant_ids.add(content_id)
            payload_by_content_id[content_id] = payload
            if point_id != content_id:
                identity_error_count += 1
                issues.append(QualityIssue("ERROR", "POINT_ID_MISMATCH", f"Qdrant point id {point_id} != payload content_id {content_id}.", content_id))

            missing_fields = [name for name in REQUIRED_PAYLOAD_FIELDS if name not in payload]
            if missing_fields:
                payload_error_count += 1
                issues.append(QualityIssue("ERROR", "PAYLOAD_REQUIRED_FIELD_MISSING", "Missing fields: " + ", ".join(missing_fields), content_id))

            for field_name, expected_type in PAYLOAD_INDEX_CONTRACT.items():
                value = payload.get(field_name)
                if value is None:
                    continue
                valid = (
                    isinstance(value, str) if expected_type == "keyword"
                    else isinstance(value, int) and not isinstance(value, bool) if expected_type == "integer"
                    else isinstance(value, bool) if expected_type == "bool"
                    else True
                )
                if not valid:
                    payload_type_error_count += 1
                    issues.append(QualityIssue(
                        "ERROR", "PAYLOAD_TYPE_MISMATCH",
                        f"{field_name} expects {expected_type}, got {type(value).__name__}.",
                        content_id, field_name,
                    ))

            chunk_key = str(payload.get("chunk_key") or "")
            if chunk_key:
                previous = seen_chunk_keys.get(chunk_key)
                if previous is not None and previous != content_id:
                    duplicate_chunk_key_count += 1
                    issues.append(QualityIssue(
                        "ERROR", "DUPLICATE_CHUNK_KEY",
                        f"chunk_key duplicates content_id={previous}.", content_id, "chunk_key"
                    ))
                else:
                    seen_chunk_keys[chunk_key] = content_id

            if str(payload.get("project_code") or "").upper() != self.project_code:
                wrong_scope_count += 1
                issues.append(QualityIssue("ERROR", "WRONG_SCOPE", f"Expected project_code={self.project_code}, got {payload.get('project_code')!r}.", content_id, "project_code"))
            if payload.get("is_current") is not True or str(payload.get("index_scope") or "").upper() != "CURRENT":
                not_current_count += 1
                issues.append(QualityIssue("ERROR", "NON_CURRENT_POINT", "Current-only collection contains a point not marked CURRENT.", content_id))
            if str(payload.get("embedding_model") or "") != self.embedding_model or str(payload.get("embedding_version") or "") != self.embedding_version:
                payload_error_count += 1
                issues.append(QualityIssue("ERROR", "PAYLOAD_EMBEDDING_CONTRACT_MISMATCH", "Qdrant payload embedding model/version differs from the configured contract.", content_id))
            if str(payload.get("source_view") or "") != "public.rag_current_chunks":
                payload_error_count += 1
                issues.append(QualityIssue("WARN", "PAYLOAD_SOURCE_VIEW_DRIFT", f"Unexpected source_view={payload.get('source_view')!r}.", content_id, "source_view"))

            sql_row = source.get(content_id)
            if sql_row is not None:
                if str(payload.get("content_hash") or "") != str(sql_row.get("content_hash") or ""):
                    stale_hash_count += 1
                    issues.append(QualityIssue("ERROR", "CONTENT_HASH_DRIFT", "Qdrant content_hash differs from PostgreSQL.", content_id, "content_hash"))
                expected_pairs = (
                    ("document_id", sql_row.get("document_id")),
                    ("specification_key", sql_row.get("specification_key")),
                    ("spec_code", sql_row.get("specification_code")),
                    ("version", sql_row.get("specification_version")),
                    ("section_id", sql_row.get("section_id")),
                )
                drifted = [name for name, expected in expected_pairs if str(payload.get(name) or "") != str(expected or "")]
                if drifted:
                    payload_error_count += 1
                    issues.append(QualityIssue("WARN", "PAYLOAD_METADATA_DRIFT", "Metadata differs from PostgreSQL: " + ", ".join(drifted), content_id))

            artifact_type = str(payload.get("artifact_type") or "").strip().upper()
            if artifact_type == "ISSUE_REGISTER":
                if not str(payload.get("issue_key") or "").strip():
                    issues.append(QualityIssue(
                        "WARN", "ISSUE_KEY_MISSING",
                        "QA/BUG issue row has no issue_key; row-level fallback identity will be used.",
                        content_id, "issue_key",
                    ))
                if not str(payload.get("issue_status") or "").strip():
                    issues.append(QualityIssue(
                        "WARN", "ISSUE_STATUS_MISSING",
                        "QA/BUG issue row has no status; status filters/statistics may be incomplete.",
                        content_id, "issue_status",
                    ))
            elif not str(payload.get("spec_subtype") or "").strip() or not str(payload.get("region_scope") or "").strip():
                empty_classification_count += 1

        source_ids = set(source)
        missing_ids = tuple(sorted(source_ids - qdrant_ids))
        orphan_ids = tuple(sorted(qdrant_ids - source_ids))
        if missing_ids:
            issues.append(QualityIssue("ERROR", "QDRANT_POINTS_MISSING", f"{len(missing_ids)} SQL current chunks are missing from Qdrant."))
        if orphan_ids:
            issues.append(QualityIssue("ERROR", "QDRANT_ORPHAN_POINTS", f"{len(orphan_ids)} Qdrant points have no SQL current chunk."))
        if qdrant_count != len(points):
            issues.append(QualityIssue("WARN", "QDRANT_SCROLL_COUNT_MISMATCH", f"Count API returned {qdrant_count}, payload scan returned {len(points)}."))

        stale_embedding_count = 0
        embedding_state_error_count = 0
        version_conflict_count = 0
        for content_id, row in source.items():
            embedded_hash = str(row.get("embedded_content_hash") or "")
            content_hash = str(row.get("content_hash") or "")
            if (
                str(row.get("embedding_status") or "").upper() not in {"COMPLETED", "DONE"}
                or embedded_hash != content_hash
                or str(row.get("embedding_model") or "") != self.embedding_model
                or str(row.get("embedding_version") or "") != self.embedding_version
            ):
                stale_embedding_count += 1
                issues.append(QualityIssue("ERROR", "EMBEDDING_STALE", "SQL embedding state is not current for this chunk.", content_id))

            state_row = state.get(content_id)
            if state_row is None:
                embedding_state_error_count += 1
                issues.append(QualityIssue(
                    "ERROR", "EMBEDDING_STATE_MISSING",
                    "No current PostgreSQL embedding-state row exists for this Qdrant collection/model/version.",
                    content_id,
                ))
            else:
                point_payload = payload_by_content_id.get(content_id, {})
                if str(state_row.get("content_hash") or "") != content_hash:
                    embedding_state_error_count += 1
                    issues.append(QualityIssue("ERROR", "EMBEDDING_STATE_CONTENT_HASH_DRIFT", "Embedding state content_hash differs from current SQL content_hash.", content_id, "content_hash"))
                if str(point_payload.get("index_hash") or "") and str(state_row.get("index_hash") or "") and str(point_payload.get("index_hash") or "") != str(state_row.get("index_hash") or ""):
                    stale_index_hash_count += 1
                    issues.append(QualityIssue("ERROR", "INDEX_HASH_DRIFT", "Qdrant index_hash differs from PostgreSQL embedding state.", content_id, "index_hash"))
                if str(state_row.get("qdrant_point_id") or "") != str(content_id):
                    embedding_state_error_count += 1
                    issues.append(QualityIssue("ERROR", "EMBEDDING_STATE_POINT_MISMATCH", "Embedding state qdrant_point_id differs from content_id.", content_id))
                if int(state_row.get("vector_dimension") or 0) != self.vector_dimension:
                    embedding_state_error_count += 1
                    issues.append(QualityIssue("ERROR", "EMBEDDING_STATE_DIMENSION_MISMATCH", "Embedding state vector dimension differs from collection contract.", content_id))
                if not bool(state_row.get("is_current_index")):
                    embedding_state_error_count += 1
                    issues.append(QualityIssue("ERROR", "EMBEDDING_STATE_NOT_CURRENT", "Embedding state is not marked current.", content_id))

            metadata_version = self._normalize_version(row.get("specification_version"))
            if metadata_version:
                text = f"{row.get('section_title') or ''}\n{row.get('content') or ''}"[:3000]
                markers = {self._normalize_version(match) for match in self._VERSION_MARKER.findall(text)}
                markers.discard("")
                conflicting = sorted(marker for marker in markers if marker != metadata_version)
                if conflicting:
                    version_conflict_count += 1
                    issues.append(QualityIssue(
                        "WARN", "VERSION_MARKER_CONFLICT_SUSPECTED",
                        f"Metadata version={row.get('specification_version')}; content marker(s)={', '.join(conflicting[:4])}.",
                        content_id, "specification_version",
                    ))

        if empty_classification_count:
            issues.append(QualityIssue(
                "WARN", "CLASSIFICATION_FIELDS_EMPTY",
                f"{empty_classification_count} point(s) have empty spec_subtype or region_scope; preserve raw values but review taxonomy completeness.",
            ))

        return QdrantQualityReport(
            project_code=self.project_code,
            database_name=self.postgres.database,
            collection_name=self.collection_name,
            collection_status=collection_status,
            vector_size=vector_size,
            distance=distance,
            sql_count=len(source),
            qdrant_count=qdrant_count,
            payload_index_expected=len(PAYLOAD_INDEX_CONTRACT),
            payload_index_present=len([name for name in PAYLOAD_INDEX_CONTRACT if schema.get(name) == PAYLOAD_INDEX_CONTRACT[name]]),
            missing_payload_indexes=tuple(missing_indexes),
            mismatched_payload_indexes=tuple(mismatched_indexes),
            missing_point_ids=missing_ids,
            orphan_point_ids=orphan_ids,
            stale_hash_count=stale_hash_count,
            stale_index_hash_count=stale_index_hash_count,
            stale_embedding_count=stale_embedding_count,
            embedding_state_error_count=embedding_state_error_count,
            payload_error_count=payload_error_count,
            payload_type_error_count=payload_type_error_count,
            duplicate_chunk_key_count=duplicate_chunk_key_count,
            wrong_scope_count=wrong_scope_count,
            not_current_count=not_current_count,
            point_identity_error_count=identity_error_count,
            version_conflict_count=version_conflict_count,
            empty_classification_count=empty_classification_count,
            issues=tuple(issues),
        )
