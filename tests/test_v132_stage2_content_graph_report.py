"""Offline enterprise contract tests for the Stage 2 read-only graph workspace."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.engineering_intelligence import content_relation_graph_report as graph
from app.engineering_intelligence.content_relation_review import (
    ContentRelationReviewCase,
    ContentRelationReviewError,
    EvidenceEndpoint,
)
from app.engineering_intelligence.requirements import normalize_text


def endpoint(code: str, text: str, *, key: str | None = None) -> EvidenceEndpoint:
    return EvidenceEndpoint(
        specification_code=code, version="v2.52" if code == "550" else "v1.0",
        document_id=f"doc-{code}", file_name=f"{code} Primary.pdf",
        unit_key=key or f"unit-{code}", content_id=int(code), section_id="4.1",
        page_number=13, start_offset=3, end_offset=3 + len(text), raw_text=text,
        text_hash=hashlib.sha256(normalize_text(text).encode()).hexdigest(),
    )


def case(candidate_id: int = 12, status: str = "REVIEW_REQUIRED", *, injected: bool = False):
    return ContentRelationReviewCase(
        candidate_id=candidate_id, status=status, relation_type="REFERENCES_SECTION",
        score=0.95, rationale="Explicit 508 to 550 references",
        source=endpoint("508", "<script>alert('x')</script>" if injected else "508 references 550 §4.1"),
        target=endpoint("550", "Bluetooth Audio v2.52 shall link."),
        review_identity_provenance="DB_SESSION_USER" if status == "CONFIRMED" else None,
    )


def build(monkeypatch, *, status="REVIEW_REQUIRED", invalid_ids=()):
    c = case(status=status, injected=True)
    def queue(cur, *, project, status, limit):
        assert project == "21MM" and 1 <= limit <= 200
        return ({"candidate_id": 12, "status": status},
                *({"candidate_id": i, "status": status} for i in invalid_ids))[:limit]

    def load(cur, *, candidate_id, project):
        assert project == "21MM"
        if candidate_id in invalid_ids:
            raise ContentRelationReviewError("target: cover/supporting document is not engineering evidence")
        return c
    monkeypatch.setattr(graph, "list_relation_queue", queue)
    monkeypatch.setattr(graph, "load_review_case", load)
    return graph.build_graph_snapshot(object(), project="21MM", statuses=[status], exported_at="2026-10-21T01:00:00+00:00")


def test_snapshot_stays_non_authoritative_and_excludes_bad_endpoint(monkeypatch):
    snapshot = build(monkeypatch, invalid_ids=(13,))
    assert snapshot["counts"] == {"examined": 2, "validated": 1, "quarantined": 1}
    assert snapshot["relations"][0]["rag_eligible_at_snapshot"] is False
    assert "source" not in snapshot["quarantined"][0]
    assert "target" not in snapshot["quarantined"][0]
    assert snapshot["quarantined"][0]["evidence_excluded"] is True
    assert snapshot["relations"][0]["source"]["raw_text"] == "<script>alert('x')</script>"


def test_confirmed_can_be_displayed_but_snapshot_never_approves(monkeypatch):
    snap = build(monkeypatch, status="CONFIRMED")
    assert snap["relations"][0]["rag_eligible_at_snapshot"] is True
    assert snap["note"].startswith("Read-only historical")


@pytest.mark.parametrize("project,status,limit", [
    ("QABUG", "REVIEW_REQUIRED", 5), ("21MM", "UNVALIDATED", 5),
    ("21MM", "CONFIRMED", 0), ("21MM", "CONFIRMED", 201),
])
def test_invalid_snapshot_scope_is_rejected(project, status, limit):
    with pytest.raises(ValueError):
        graph.build_graph_snapshot(object(), project=project, statuses=(status,), limit=limit)


def test_html_escapes_original_text_without_network_scripts(monkeypatch):
    doc = graph.render_graph_html(build(monkeypatch)).decode()
    assert "&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt;" in doc
    assert "<script>" not in doc
    assert "javascript:" not in doc
    assert "default-src 'none'" in doc
    assert 'href="#relation-12"' in doc
    assert "SHA-256" in doc
    assert "双端原文" in doc
    assert "Bluetooth Audio" in doc


def test_bundle_export_verify_and_detect_tampering(tmp_path, monkeypatch):
    snap = build(monkeypatch, invalid_ids=(13,))
    folder = tmp_path / "review-case"
    graph.write_graph_bundle(snap, folder)
    assert (folder / "index.html").is_file()
    assert (folder / "graph.json").is_file()
    assert graph.verify_graph_bundle(folder)["counts"]["quarantined"] == 1
    with pytest.raises(FileExistsError):
        graph.write_graph_bundle(snap, folder)
    (folder / "index.html").write_text("altered by attacker", encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        graph.verify_graph_bundle(folder)


def test_verifier_enforces_source_span_hash_even_when_bundle_rehashed(tmp_path, monkeypatch):
    snap = build(monkeypatch)
    snap["relations"][0]["source"]["text_hash"] = "0" * 64
    folder = tmp_path / "malformed"
    graph.write_graph_bundle(snap, folder)
    with pytest.raises(ValueError, match="source evidence hash"):
        graph.verify_graph_bundle(folder)


def test_verifier_refuses_count_and_status_inconsistency(tmp_path, monkeypatch):
    snap = build(monkeypatch)
    snap["relations"][0]["rag_eligible_at_snapshot"] = True
    folder = tmp_path / "bad-status"
    graph.write_graph_bundle(snap, folder)
    with pytest.raises(ValueError, match="status"):
        graph.verify_graph_bundle(folder)


def test_queue_limits_are_global_across_states(monkeypatch):
    observed = []
    def queue(_, *, project, status, limit):
        observed.append((status, limit))
        return [{"candidate_id": 1, "status": status}] if status == "REVIEW_REQUIRED" else []
    monkeypatch.setattr(graph, "list_relation_queue", queue)
    monkeypatch.setattr(graph, "load_review_case", lambda *a, **kw: case(1))
    doc = graph.build_graph_snapshot(object(), project="21MM",
                                     statuses=("REVIEW_REQUIRED", "REVIEW_REQUIRED", "CONFIRMED"), limit=1)
    assert doc["counts"]["examined"] == 1
    assert observed == [("REVIEW_REQUIRED", 1)]


def test_static_graph_does_not_trigger_database_writes(monkeypatch):
    class ReadOnlyCursor:
        def execute(self, *a, **kw):
            raise AssertionError("Graph exporter must not directly run SQL; calls queue/review adapters")
    monkeypatch.setattr(graph, "list_relation_queue", lambda *a, **k: ())
    snap = graph.build_graph_snapshot(ReadOnlyCursor(), project="21MM")
    assert snap["counts"]["examined"] == 0
    assert "无已验证的内容关系" in graph.render_graph_html(snap).decode()


def test_content_topology_uses_real_unit_keys_and_connects_to_full_source(monkeypatch):
    snap = build(monkeypatch)
    snap["relations"].append({**deepcopy(snap["relations"][0]), "candidate_id": 14})
    snap["counts"]["validated"] = 2
    snap["counts"]["examined"] = 2
    output = graph.render_graph_html(snap).decode()
    assert 'class="topology"' in output
    assert output.count('<path d="M390') == 2
    assert 'href="#relation-12"' in output
    assert 'href="#relation-14"' in output
    assert output.count('unit-508') >= 1
    assert output.count('unit-550') >= 1
