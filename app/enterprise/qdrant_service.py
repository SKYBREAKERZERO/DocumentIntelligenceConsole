from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.enterprise.profiles import QdrantProfile


class QdrantUnavailableError(RuntimeError):
    pass




@dataclass(frozen=True, slots=True)
class PayloadIndexChange:
    field_name: str
    expected_type: str
    actual_type: str = ""
    action: str = "NONE"
    message: str = ""


@dataclass(frozen=True, slots=True)
class PayloadIndexEnsureResult:
    collection_name: str
    created: tuple[PayloadIndexChange, ...] = ()
    repaired: tuple[PayloadIndexChange, ...] = ()
    unchanged: tuple[PayloadIndexChange, ...] = ()
    mismatched: tuple[PayloadIndexChange, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.mismatched


@dataclass
class QdrantCollectionSummary:
    name: str
    points_count: int | None
    vectors_count: int | None
    status: str
    vector_size: str
    distance: str


class QdrantService:
    def __init__(self, profile: QdrantProfile, api_key: str = "") -> None:
        self.profile = profile
        self.api_key = api_key
        self._cached_client = None

    def _client(self):
        try:
            from qdrant_client import QdrantClient
        except ImportError as exc:
            raise QdrantUnavailableError("qdrant-client is not installed.") from exc

        if self._cached_client is None:
            self._cached_client = QdrantClient(
                url=self.profile.url,
                api_key=self.api_key or None,
                timeout=self.profile.timeout,
            )
        return self._cached_client

    def test_connection(self) -> list[str]:
        client = self._client()
        response = client.get_collections()
        return [item.name for item in response.collections]

    @staticmethod
    def _vector_config_summary(config: Any) -> tuple[str, str]:
        vectors = getattr(config, "vectors", None)
        if vectors is None:
            return "-", "-"

        if isinstance(vectors, dict):
            parts = []
            distances = []
            for name, params in vectors.items():
                size = getattr(params, "size", "?")
                distance = getattr(params, "distance", "?")
                parts.append(f"{name}:{size}")
                distances.append(f"{name}:{distance}")
            return ", ".join(parts), ", ".join(distances)

        return (
            str(getattr(vectors, "size", "-")),
            str(getattr(vectors, "distance", "-")),
        )

    def list_collections(self) -> list[QdrantCollectionSummary]:
        client = self._client()
        result = []
        for item in client.get_collections().collections:
            info = client.get_collection(item.name)
            vector_size, distance = self._vector_config_summary(info.config.params)
            result.append(
                QdrantCollectionSummary(
                    name=item.name,
                    points_count=getattr(info, "points_count", None),
                    vectors_count=getattr(info, "vectors_count", None),
                    status=str(getattr(info, "status", "")),
                    vector_size=vector_size,
                    distance=distance,
                )
            )
        return result


    def collection_exists(self, collection_name: str) -> bool:
        client = self._client()
        return bool(client.collection_exists(collection_name=collection_name))

    def ensure_collection(self, collection_name: str, dimension: int) -> None:
        try:
            from qdrant_client.models import Distance, VectorParams
        except ImportError as exc:
            raise QdrantUnavailableError("qdrant-client is not installed.") from exc

        client = self._client()
        if client.collection_exists(collection_name=collection_name):
            info = client.get_collection(collection_name)
            vectors = getattr(info.config.params, "vectors", None)
            if isinstance(vectors, dict):
                raise QdrantUnavailableError(
                    f"Collection {collection_name!r} uses named vectors; current sync expects one unnamed dense vector."
                )
            actual_size = int(getattr(vectors, "size", 0) or 0)
            if actual_size != int(dimension):
                raise QdrantUnavailableError(
                    f"Collection {collection_name!r} dimension mismatch: expected={dimension}, actual={actual_size}. Use Full Rebuild."
                )
            actual_distance = str(getattr(vectors, "distance", "") or "").upper()
            if "COSINE" not in actual_distance:
                raise QdrantUnavailableError(
                    f"Collection {collection_name!r} distance mismatch: expected=COSINE, actual={actual_distance or '?'}; use Full Rebuild."
                )
            return
        client.create_collection(
            collection_name=collection_name,
            vectors_config=VectorParams(size=int(dimension), distance=Distance.COSINE),
        )

    @staticmethod
    def _normalize_payload_schema_type(value: Any) -> str:
        raw = str(value or "").strip().lower()
        for token in ("keyword", "integer", "bool", "float", "text", "datetime", "uuid", "geo"):
            if token in raw:
                return token
        return raw

    def payload_schema(self, collection_name: str) -> dict[str, str]:
        client = self._client()
        if not client.collection_exists(collection_name=collection_name):
            return {}
        info = client.get_collection(collection_name)
        raw_schema = getattr(info, "payload_schema", None) or {}
        result: dict[str, str] = {}
        for field_name, schema in dict(raw_schema).items():
            data_type = getattr(schema, "data_type", schema)
            result[str(field_name)] = self._normalize_payload_schema_type(data_type)
        return result

    def ensure_payload_indexes(
        self,
        collection_name: str,
        contract: dict[str, str],
        *,
        repair_mismatched: bool = False,
    ) -> PayloadIndexEnsureResult:
        """Ensure the approved payload-index contract exists.

        Missing indexes are created idempotently. Type mismatches are never
        changed implicitly unless ``repair_mismatched`` is explicitly enabled.
        """
        try:
            from qdrant_client.models import PayloadSchemaType
        except ImportError as exc:
            raise QdrantUnavailableError("qdrant-client is not installed.") from exc

        client = self._client()
        if not client.collection_exists(collection_name=collection_name):
            raise QdrantUnavailableError(f"Collection {collection_name!r} does not exist.")

        enum_map = {
            "keyword": PayloadSchemaType.KEYWORD,
            "integer": PayloadSchemaType.INTEGER,
            "bool": PayloadSchemaType.BOOL,
            "float": PayloadSchemaType.FLOAT,
            "text": PayloadSchemaType.TEXT,
        }
        existing = self.payload_schema(collection_name)
        created: list[PayloadIndexChange] = []
        repaired: list[PayloadIndexChange] = []
        unchanged: list[PayloadIndexChange] = []
        mismatched: list[PayloadIndexChange] = []

        for field_name, expected_raw in contract.items():
            expected = self._normalize_payload_schema_type(expected_raw)
            if expected not in enum_map:
                raise QdrantUnavailableError(
                    f"Unsupported payload index type {expected_raw!r} for {field_name!r}."
                )
            actual = existing.get(field_name, "")
            if not actual:
                client.create_payload_index(
                    collection_name=collection_name,
                    field_name=field_name,
                    field_schema=enum_map[expected],
                    wait=True,
                )
                created.append(PayloadIndexChange(field_name, expected, "", "CREATE", "Missing index created."))
                continue
            if actual == expected:
                unchanged.append(PayloadIndexChange(field_name, expected, actual, "NONE", "Index matches contract."))
                continue
            if not repair_mismatched:
                mismatched.append(PayloadIndexChange(
                    field_name, expected, actual, "MISMATCH",
                    "Existing index type differs from the approved contract.",
                ))
                continue
            client.delete_payload_index(
                collection_name=collection_name,
                field_name=field_name,
                wait=True,
            )
            client.create_payload_index(
                collection_name=collection_name,
                field_name=field_name,
                field_schema=enum_map[expected],
                wait=True,
            )
            repaired.append(PayloadIndexChange(field_name, expected, actual, "REPAIR", "Mismatched index recreated."))

        return PayloadIndexEnsureResult(
            collection_name=collection_name,
            created=tuple(created),
            repaired=tuple(repaired),
            unchanged=tuple(unchanged),
            mismatched=tuple(mismatched),
        )

    def scroll_page(
        self,
        collection_name: str,
        *,
        limit: int = 256,
        offset: Any = None,
    ) -> tuple[list[dict[str, Any]], Any]:
        client = self._client()
        points, next_offset = client.scroll(
            collection_name=collection_name,
            limit=max(1, min(int(limit), 2048)),
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        return (
            [{"id": point.id, "payload": dict(point.payload or {})} for point in points],
            next_offset,
        )

    def scroll_all_points(
        self,
        collection_name: str,
        *,
        page_size: int = 512,
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        offset: Any = None
        seen_offsets: set[str] = set()
        while True:
            page, next_offset = self.scroll_page(
                collection_name, limit=page_size, offset=offset
            )
            result.extend(page)
            if next_offset is None:
                break
            marker = repr(next_offset)
            if marker in seen_offsets:
                raise QdrantUnavailableError(
                    f"Qdrant scroll returned a repeated offset for {collection_name!r}."
                )
            seen_offsets.add(marker)
            offset = next_offset
        return result

    def delete_collection(self, collection_name: str) -> None:
        client = self._client()
        if client.collection_exists(collection_name=collection_name):
            client.delete_collection(collection_name=collection_name)

    def count_collection(self, collection_name: str) -> int:
        client = self._client()
        result = client.count(collection_name=collection_name, exact=True)
        return int(getattr(result, "count", 0) or 0)

    def upsert_points(self, collection_name: str, points: list[dict[str, Any]]) -> int:
        if not points:
            return 0
        try:
            from qdrant_client.models import PointStruct
        except ImportError as exc:
            raise QdrantUnavailableError("qdrant-client is not installed.") from exc
        client = self._client()
        normalized = [
            PointStruct(
                id=point["id"],
                vector=point["vector"],
                payload=dict(point.get("payload") or {}),
            )
            for point in points
        ]
        client.upsert(
            collection_name=collection_name,
            points=normalized,
            wait=True,
        )
        return len(normalized)

    def delete_points(self, collection_name: str, point_ids: list[int | str]) -> int:
        if not point_ids:
            return 0
        try:
            from qdrant_client.models import PointIdsList
        except ImportError as exc:
            raise QdrantUnavailableError("qdrant-client is not installed.") from exc
        client = self._client()
        if not client.collection_exists(collection_name=collection_name):
            return 0
        client.delete(
            collection_name=collection_name,
            points_selector=PointIdsList(points=list(point_ids)),
            wait=True,
        )
        return len(point_ids)

    def scroll_points(
        self,
        collection_name: str,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        client = self._client()
        points, _ = client.scroll(
            collection_name=collection_name,
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )
        return [
            {"id": point.id, "payload": point.payload or {}}
            for point in points
        ]
