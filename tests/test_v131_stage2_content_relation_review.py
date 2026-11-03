"""Stage 2 fail-closed review / retrieval with no live database dependencies."""
from __future__ import annotations

import hashlib
from unittest.mock import Mock

import pytest

from app.ai.retrieval import EnterpriseRetriever
from app.engineering_intelligence import content_relation_review as review
from app.engineering_intelligence.requirements import normalize_text


def _digest(text):
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


def example_row():
    s, t = "参照 Spec 550 section 4.1，蓝牙 SHALL link.", "Bluetooth Audio shall link Memory ID."
    return {
        "id": 12, "status": "REVIEW_REQUIRED", "relation_type": "REFERENCES_SECTION",
        "score": 0.95, "rationale": "EXPLICIT_SPEC_AND_SECTION_ANCHOR",
        "source_version_id": 10, "target_version_id": 20,
        "source_current": 10, "target_current": 20,
        "source_project": "21MM", "target_project": "21MM",
        "source_spec_id": 100, "target_spec_id": 200,
        "source_version_spec_id": 100, "target_version_spec_id": 200,
        "source_version_doc": "doc508", "target_version_doc": "doc550",
        "source_code": "508", "target_code": "550",
        "source_version": "v1", "target_version": "v2.52",
        "source_document": "doc508", "target_document": "doc550",
        "source_file_name": "508 Body.pdf", "target_file_name": "Bluetooth Audio function spec v2.52.pdf",
        "source_metadata": {}, "target_metadata": {},
        "source_unit_key": "source_key", "target_unit_key": "target_key",
        "source_content_id": 1, "target_content_id": 2,
        "source_section": "5.0", "target_section": "4.1",
        "source_page": 8, "target_page": 13,
        "source_start": 3, "target_start": 5,
        "source_end": 3 + len(s), "target_end": 5 + len(t),
        "source_text": s, "target_text": t,
        "source_hash": _digest(s), "target_hash": _digest(t),
        "source_chunk": "abc" + s + "\n", "target_chunk": "12345" + t,
        "source_chunk_doc": "doc508", "target_chunk_doc": "doc550",
        "last_audit_status": None,
    }


class Cursor:
    def __init__(self, row=None, update_ok=True):
        self.row = example_row() if row is None else row
        self.queries = []
        self.update_ok = update_ok

    def execute(self, sql, params=None):
        self.queries.append((sql, params))

    def fetchone(self):
        q = self.queries[-1][0]
        if "UPDATE public.ei_content_relations" in q:
            return (12,) if self.update_ok else None
        return self.row


@pytest.fixture(autouse=True)
def no_database(monkeypatch):
    monkeypatch.setattr(review, "ensure_schema_ready", Mock())
    mock = Mock()
    monkeypatch.setattr(review, "require_complete_backfill", mock)
    return mock


def test_full_two_end_source_and_exact_provenance(no_database):
    cur = Cursor()
    candidate = review.load_review_case(cur, candidate_id=12, project="21MM")
    assert candidate.target.file_name == "Bluetooth Audio function spec v2.52.pdf"
    assert candidate.source.raw_text == example_row()["source_text"]
    assert candidate.target.end_offset == example_row()["target_end"]
    assert no_database.call_count == 2
    assert not any("FOR UPDATE" in sql for sql, _ in cur.queries)


@pytest.mark.parametrize("change,pattern", [
    ({"target_file_name": "550_Bluetooth Audio Function Spec_Cover_Sheet_Ver.2.50改(電子承認).pdf"}, "cover/supporting"),
    ({"target_metadata": {"document_role": "SUPPORTING"}}, "cover/supporting"),
    ({"target_metadata": {"qa_index_policy": "EXCLUDE"}}, "cover/supporting"),
    ({"target_project": "24MM"}, "Cross-project"),
    ({"target_current": 21}, "Historical"),
    ({"source_version_doc": "other"}, "primary source"),
    ({"source_spec_id": 999}, "identity mismatch"),
    ({"target_chunk_doc": "doc-other"}, "ownership"),
    ({"target_hash": "0" * 64}, "hash mismatch"),
    ({"source_start": 2}, "offsets"),
    ({"status": "STALE"}, "STALE"),
    ({"status": "CONFIRMED", "last_audit_status": None}, "audit event"),
    ({"status": "REVIEW_REQUIRED", "last_audit_status": "CONFIRMED"}, "audit head"),
])
def test_invalid_evidence_is_rejected(change, pattern):
    row = example_row()
    row.update(change)
    with pytest.raises(review.ContentRelationReviewError, match=pattern):
        review.load_review_case(Cursor(row), candidate_id=12, project="21MM")


@pytest.mark.parametrize("previous,next_state", [
    ("REVIEW_REQUIRED", "CONFIRMED"), ("REVIEW_REQUIRED", "REJECTED"),
    ("CONFIRMED", "REVIEW_REQUIRED"), ("REJECTED", "REVIEW_REQUIRED"),
    ("CONFIRMED", "CONFIRMED"),
])
def test_historical_self_declared_review_api_is_inert(previous, next_state):
    """Legacy caller must not reach SQL UPDATE/INSERT, even with valid inputs."""
    cur = Cursor()
    with pytest.raises(review.ContentRelationReviewError, match="Legacy direct review writes are disabled"):
        review.review_content_relation(
            cur, candidate_id=12, project="21MM", expected_status=previous,
            new_status=next_state, actor="spoofable-actor",
            reason="Reviewed both exact source and target content",
        )
    assert not cur.queries


class FakeConn:
    def __init__(self, row):
        self.row = row
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def cursor(self):
        return self

    def execute(self, sql, params=None):
        self.queries.append((sql, params))

    def fetchone(self):
        return {"relation_table": "ei_content_relations", "authenticated_review_authority": "dic_review.apply_content_relation_review", "trusted_review_authority": True, "active_review_guards": 2}

    def fetchall(self):
        return [self.row]


def retrieval_row():
    r = example_row()
    return {
        **{k: r[k] for k in (
            "id", "relation_type", "score", "rationale", "source_code", "target_code",
            "source_document", "source_content_id", "source_section", "source_page",
            "source_start", "source_end", "source_text", "source_hash", "source_chunk",
            "source_version", "source_file_name", "target_document", "target_content_id",
            "target_section", "target_page", "target_start", "target_end", "target_text",
            "target_hash", "target_chunk", "target_version", "target_file_name",
        )},
        "status": "CONFIRMED", "reviewed_by": "reviewer-a",
        "review_reason": "Validated exact cited section", "review_identity_provenance": "DB_SESSION_USER",
    }


def _retriever(row):
    conn = FakeConn(row)
    retriever = object.__new__(EnterpriseRetriever)
    retriever.postgres_profiles = {"COMMON": object()}
    retriever.top_k = 8
    retriever._connect = Mock(return_value=conn)
    return retriever, conn


def test_qa_only_confirmed_audited_primary_evidence_and_all_scopes():
    retriever, conn = _retriever(retrieval_row())
    result = retriever._content_relation_search(["COMMON", "QABUG"], spec_codes=("508",))
    assert len(result) == 1
    assert result[0].metadata["target"]["text_hash"] == retrieval_row()["target_hash"]
    assert result[0].metadata["reviewed_by"] == "reviewer-a"
    assert "Memory ID" in result[0].content
    assert retriever._connect.call_count == 1
    query, params = conn.queries[-1]
    assert "rel.status='CONFIRMED'" in query
    assert "audit.new_status='CONFIRMED'" in query
    assert "audit.identity_provenance='DB_SESSION_USER'" in query
    assert "src.project_code=%s AND tgt.project_code=%s" in query
    assert "sv.document_id=su.document_id" in query
    assert "sd.file_name" in query and "td.file_name" in query
    assert params[:2] == ("COMMON", "COMMON")
    assert "cover" in query.lower()


@pytest.mark.parametrize("field,value", [
    ("status", "REVIEW_REQUIRED"), ("reviewed_by", ""),
    ("target_hash", "0" * 64), ("source_chunk", "changed"),
])
def test_rag_rejects_unconfirmed_or_drifted_relationship(field, value):
    row = retrieval_row()
    row[field] = value
    retriever, _ = _retriever(row)
    assert retriever._content_relation_search(["COMMON"], spec_codes=("508",)) == []


def test_empty_spec_code_prevents_broad_fanout():
    retriever, _ = _retriever(retrieval_row())
    assert retriever._content_relation_search(["COMMON"], spec_codes=()) == []
    retriever._connect.assert_not_called()


def test_relation_queue_bounded_current_primary_metadata():
    class QueueCursor:
        description = [type("Col", (), {"name": name}) for name in (
            "candidate_id", "status", "source_code", "target_code",
            "current_both", "primary_both", "last_audit_status",
        )]
        def __init__(self):
            self.query = None
            self.params = None
        def execute(self, query, params):
            self.query, self.params = query, params
        def fetchall(self):
            return [(12, "REVIEW_REQUIRED", "508", "550", True, True, None)]
    cursor = QueueCursor()
    queue = review.list_relation_queue(cursor, project="21MM", limit=20)
    assert queue[0]["candidate_id"] == 12
    assert queue[0]["target_code"] == "550"
    assert cursor.params == ("21MM", "21MM", "REVIEW_REQUIRED", 20)
    assert "sd.file_name" in cursor.query and "td.file_name" in cursor.query
    assert "r.status=%s" in cursor.query
    assert "ORDER BY r.id LIMIT %s" in cursor.query


@pytest.mark.parametrize("status,limit,project", [
    ("INJECTED", 20, "21MM"), ("REVIEW_REQUIRED", 500, "21MM"),
    ("REVIEW_REQUIRED", 2, "QABUG"),
])
def test_queue_fails_closed_for_invalid_parameters(status, limit, project):
    with pytest.raises(review.ContentRelationReviewError):
        review.list_relation_queue(Mock(), project=project, status=status, limit=limit)
