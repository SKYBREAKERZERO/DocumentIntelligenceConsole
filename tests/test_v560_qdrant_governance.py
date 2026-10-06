from __future__ import annotations

from app.ai.retrieval import EnterpriseRetriever, EvidenceItem
from app.enterprise.qdrant_service import QdrantCollectionSummary
from app.rag.qdrant_governance import (
    PAYLOAD_INDEX_CONTRACT,
    QdrantDataQualityValidator,
)


class _FakePostgres:
    database = "rag_21mm"


class _FakeQdrant:
    def __init__(self, *, points, schema=None):
        self._points = list(points)
        self._schema = dict(PAYLOAD_INDEX_CONTRACT if schema is None else schema)

    def collection_exists(self, name):
        return True

    def list_collections(self):
        return [QdrantCollectionSummary("rag_21mm_current_chunks", len(self._points), None, "green", "1024", "Cosine")]

    def count_collection(self, name):
        return len(self._points)

    def payload_schema(self, name):
        return dict(self._schema)

    def scroll_all_points(self, name):
        return list(self._points)


class _Validator(QdrantDataQualityValidator):
    def __init__(self, source, state, qdrant):
        self.postgres = _FakePostgres()
        self.postgres_password = ""
        self.qdrant = qdrant
        self.project_code = "21MM"
        self.collection_name = "rag_21mm_current_chunks"
        self.embedding_model = "BAAI/bge-m3"
        self.embedding_version = "current-index-v1"
        self.vector_dimension = 1024
        self._source = source
        self._state = state

    def _source_rows(self):
        return self._source

    def _embedding_state(self):
        return self._state


def _source(content="Specification body", version="v2.50"):
    return {
        78: {
            "content_id": 78,
            "document_id": "21MM:EXCEPT_NA:550@v2.50",
            "project_code": "21MM",
            "spec_type": "BT",
            "spec_subtype": "BLUETOOTH_AUDIO",
            "region_scope": "EXCEPT_NA",
            "specification_id": 2,
            "specification_key": "21MM:EXCEPT_NA:550",
            "specification_code": "550",
            "specification_name": "Bluetooth Audio Function Spec",
            "specification_version_id": 2,
            "specification_version": version,
            "chapter_id": "1",
            "section_id": "1.2",
            "section_title": "Purpose",
            "content_hash": "abc",
            "embedding_status": "COMPLETED",
            "embedding_model": "BAAI/bge-m3",
            "embedding_version": "current-index-v1",
            "embedded_content_hash": "abc",
            "content": content,
        }
    }


def _state():
    return {
        78: {
            "qdrant_point_id": "78",
            "content_hash": "abc",
            "index_hash": "idx",
            "vector_dimension": 1024,
            "is_current_index": True,
        }
    }


def _point():
    return {
        "id": 78,
        "payload": {
            "content_id": 78,
            "chunk_key": "21MM:EXCEPT_NA:550@v2.50:1.2:0",
            "content_hash": "abc",
            "index_hash": "idx",
            "project_code": "21MM",
            "document_id": "21MM:EXCEPT_NA:550@v2.50",
            "file_name": "550.xlsx",
            "file_type": "xlsx",
            "spec_type": "BT",
            "spec_subtype": "BLUETOOTH_AUDIO",
            "region_scope": "EXCEPT_NA",
            "specification_id": 2,
            "specification_key": "21MM:EXCEPT_NA:550",
            "spec_code": "550",
            "specification_name": "Bluetooth Audio Function Spec",
            "specification_version_id": 2,
            "version": "v2.50",
            "is_current": True,
            "chapter_id": "1",
            "section_id": "1.2",
            "chunk_index": 0,
            "embedding_model": "BAAI/bge-m3",
            "embedding_version": "current-index-v1",
            "index_scope": "CURRENT",
            "source_view": "public.rag_current_chunks",
        },
    }


def test_payload_index_contract_is_selective_and_typed():
    assert PAYLOAD_INDEX_CONTRACT["spec_code"] == "keyword"
    assert PAYLOAD_INDEX_CONTRACT["specification_id"] == "integer"
    assert PAYLOAD_INDEX_CONTRACT["document_id"] == "keyword"
    assert "project_code" not in PAYLOAD_INDEX_CONTRACT
    assert "is_current" not in PAYLOAD_INDEX_CONTRACT


def test_quality_validator_passes_consistent_current_index():
    report = _Validator(_source(), _state(), _FakeQdrant(points=[_point()])).validate()
    assert report.status == "PASS"
    assert report.sql_count == 1
    assert report.qdrant_count == 1
    assert report.payload_index_present == len(PAYLOAD_INDEX_CONTRACT)
    assert report.stale_hash_count == 0
    assert report.stale_embedding_count == 0


def test_quality_validator_detects_version_marker_conflict_as_warning():
    source = _source(content="[Header] SpecID | 550 | Ver. | 2.52", version="v2.50")
    report = _Validator(source, _state(), _FakeQdrant(points=[_point()])).validate()
    assert report.version_conflict_count == 1
    assert report.status == "WARN"
    assert any(x.code == "VERSION_MARKER_CONFLICT_SUSPECTED" for x in report.issues)


def test_quality_validator_detects_missing_indexes_and_hash_drift():
    point = _point()
    point["payload"]["content_hash"] = "stale"
    report = _Validator(_source(), _state(), _FakeQdrant(points=[point], schema={})).validate()
    assert report.stale_hash_count == 1
    assert report.missing_payload_indexes
    assert report.status == "FAIL"


def test_entity_spec_code_extraction_and_semantic_consensus_fusion():
    assert EnterpriseRetriever._entity_spec_codes(("SPEC-550",), "tell me about 550") == ("550",)
    items = [
        EvidenceItem("", "21MM", "SEMANTIC", "x", content_id=78, score=0.70),
        EvidenceItem("", "21MM", "SEMANTIC", "x", content_id=78, score=0.72),
        EvidenceItem("", "21MM", "SEMANTIC", "y", content_id=79, score=0.75),
    ]
    fused = EnterpriseRetriever._fuse_semantic_evidence(items)
    by_id = {x.content_id: x for x in fused}
    assert by_id[78].score > 0.72
    assert by_id[78].metadata["semantic_query_hits"] == 2
    assert by_id[79].metadata["semantic_query_hits"] == 1
