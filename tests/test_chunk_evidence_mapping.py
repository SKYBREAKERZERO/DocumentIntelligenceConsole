from __future__ import annotations

from app.services.chunk_evidence_mapping import (
    ChunkEvidenceMapping,
    ChunkEvidenceMappingService,
    MAPPING_STATUSES,
    _normalize_statuses,
)


class _Description:
    def __init__(self, name: str) -> None:
        self.name = name


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.description = []
        self.statement = ""
        self.params = ()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, statement, params=()):
        self.statement = statement
        self.params = params
        if self.rows and not isinstance(self.rows[0], dict):
            self.description = [_Description("mapping_status"), _Description("count")]

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def cursor(self):
        return self._cursor


def test_mapping_from_row():
    mapping = ChunkEvidenceMapping.from_row(
        {
            "mapped_evidence_id": 11,
            "relation_id": 22,
            "content_id": 33,
            "mapping_status": "EXACT",
            "mapped_chunk_document_id": "doc-1",
            "mapped_chunk_index": 4,
            "mapped_chunk_live_content_hash": "abc",
            "chunk_content_hash_snapshot": "abc",
        }
    )
    assert mapping.evidence_id == 11
    assert mapping.content_id == 33
    assert mapping.mapping_status == "EXACT"
    assert mapping.chunk_index == 4
    assert mapping.live_content_hash == mapping.snapshot_content_hash


def test_status_contract_is_complete():
    assert MAPPING_STATUSES == (
        "EXACT",
        "CONTENT_CHANGED",
        "IDENTITY_CHANGED",
        "CHUNK_MISSING",
        "UNMAPPED",
    )


def test_status_normalization_deduplicates():
    assert _normalize_statuses(["exact", " EXACT ", "unmapped"]) == (
        "EXACT",
        "UNMAPPED",
    )


def test_service_uses_evidence_id_not_fuzzy_fields():
    cursor = FakeCursor(
        [{"mapped_evidence_id": 7, "relation_id": 8, "content_id": 9, "mapping_status": "EXACT"}]
    )
    service = ChunkEvidenceMappingService(lambda: FakeConnection(cursor))
    row = service.get(7)
    assert row["content_id"] == 9
    assert "WHERE mapped_evidence_id = %s" in cursor.statement
    assert cursor.params == (7,)
    lowered = cursor.statement.lower()
    assert "similar" not in lowered
    assert "chunk_index =" not in lowered
    assert "content like" not in lowered


def test_audit_summary_fills_zero_statuses():
    cursor = FakeCursor([
        {"mapping_status": "EXACT", "count": 5},
        {"mapping_status": "UNMAPPED", "count": 2},
    ])
    service = ChunkEvidenceMappingService(lambda: FakeConnection(cursor))
    summary = service.audit_summary()
    assert summary["EXACT"] == 5
    assert summary["UNMAPPED"] == 2
    assert summary["CHUNK_MISSING"] == 0
    assert summary["TOTAL"] == 7
