"""CLI regression: no write transaction, strong project DB scope, offline verify."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "export_content_relation_graph.py"


def load_script():
    spec = importlib.util.spec_from_file_location("dic_export_content_graph_cli_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeDb:
    def __init__(self, project="21MM"):
        self.project = project
        self.calls = []
        self.rolled_back = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self):
        return self

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        assert "UPDATE " not in sql and "INSERT " not in sql and "DELETE " not in sql

    def fetchall(self):
        return [(item,) for item in self.project] if isinstance(self.project, (list, tuple)) else [(self.project,)]

    def rollback(self):
        self.rolled_back = True


def test_cli_readonly_repeatable_read_before_any_data_access(tmp_path, monkeypatch):
    script = load_script()
    conn = FakeDb()
    params = []
    def connect(**kw):
        params.append(kw)
        return conn
    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=connect))
    monkeypatch.setenv("POSTGRES_PASSWORD", "top-secret-operator-password")
    monkeypatch.setattr(script, "build_graph_snapshot", lambda *a, **k: {
        "format": "dic-content-relation-graph-v1", "project": "21MM",
        "snapshot_utc": "2026-10-21T00:00:00+00:00", "requested_statuses": ["REVIEW_REQUIRED"],
        "limit": 10, "counts": {"validated": 0, "quarantined": 0, "examined": 0},
        "relations": [], "quarantined": [], "note": "read only",
    })
    path = tmp_path / "export"
    assert script.main(["--output", str(path), "--database", "rag_21mm", "--project", "21MM",
                        "--user", "graph_viewer", "--limit", "10"]) == 0
    assert conn.calls[0][0] == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
    assert conn.calls[1][0] == "SELECT project_code FROM public.project_info ORDER BY project_code LIMIT 2"
    assert conn.rolled_back
    assert params[0]["user"] == "graph_viewer"
    assert script.main(["--verify", str(path)]) == 0


def test_cli_rejects_wrong_project_without_publishing(tmp_path, monkeypatch):
    script = load_script()
    conn = FakeDb("24MM")
    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=lambda **k: conn))
    monkeypatch.setenv("POSTGRES_PASSWORD", "safe-test-only")
    target = tmp_path / "forbidden"
    with pytest.raises(RuntimeError, match="identity mismatch"):
        script.main(["--output", str(target), "--database", "rag_24mm",
                     "--project", "21MM", "--user", "viewer"])
    assert not target.exists()
    assert conn.calls[0][0].endswith("REPEATABLE READ READ ONLY")


def test_cli_refuses_qa_and_must_not_overwrite(tmp_path):
    script = load_script()
    for opts in (["--database", "rag_qabug", "--project", "21MM"],
                 ["--database", "rag_21mm", "--project", "QABUG"]):
        with pytest.raises(SystemExit) as exc:
            script.main(["--output", str(tmp_path / "export"), "--user", "viewer"] + opts)
        assert exc.value.code == 2
    (tmp_path / "export").mkdir()
    with pytest.raises(SystemExit) as exc:
        script.main(["--output", str(tmp_path / "export"), "--user", "viewer",
                     "--database", "rag_21mm", "--project", "21MM"])
    assert exc.value.code == 2


def test_cli_rejects_ambiguous_project_info_even_if_first_row_matches(tmp_path, monkeypatch):
    script = load_script()
    conn = FakeDb(["21MM", "24MM"])
    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=lambda **kw: conn))
    monkeypatch.setenv("POSTGRES_PASSWORD", "test-only")
    target = tmp_path / "ambiguous"
    with pytest.raises(RuntimeError, match="identity mismatch"):
        script.main(["--output", str(target), "--database", "rag_21mm",
                     "--project", "21MM", "--user", "viewer"])
    assert not target.exists()
