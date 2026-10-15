"""v1.29: package manifest and opt-in smoke probes, no network or credentials."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.engineering_intelligence.stage1_environment_smoke import (
    inspect_dependency_manifest, probe_gui_import, probe_postgresql_readonly,
)
from scripts import stage1_environment_smoke as cli

ROOT = Path(__file__).resolve().parents[1]


def test_shipped_requirements_are_pip_readable_utf8():
    result = inspect_dependency_manifest(ROOT / "requirements.txt")
    assert result["status"] == "PASS"
    assert result["pywin32_windows_only"]
    assert result["is_exact_pinned"]


@pytest.mark.parametrize("payload,reason", [
    ("psycopg==3.0".encode("utf-16"), "NOT_UTF8"),
    (b"something==1.0\nsomething==1.1\n", "EMPTY_OR_DUPLICATE_PACKAGE"),
    (b"!invalid requirement", "INVALID_MANIFEST"),
    (b"psycopg==3.3.4\n", "PYWIN32_PLATFORM_MARKER_INVALID"),
])
def test_bad_dependency_manifest_blocked(tmp_path, payload, reason):
    path = tmp_path / "requirements.txt"
    path.write_bytes(payload)
    assert inspect_dependency_manifest(path)["reason"] == reason


def test_manifest_missing_blocked(tmp_path):
    assert inspect_dependency_manifest(tmp_path / "missing")["status"] == "BLOCKED"


class FakeCursor:
    def __init__(self, row):
        self.statements = []
        self.row = row

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql):
        self.statements.append(sql)

    def fetchone(self):
        return self.row


class FakeConnection:
    def __init__(self, row):
        self.cur = FakeCursor(row)
        self.closed = False

    def cursor(self):
        return self.cur

    def close(self):
        self.closed = True


def test_sql_probe_uses_read_only_transaction_and_rolls_back():
    calls = []
    conn = FakeConnection(("rag_21mm", "on"))

    def connector(**kwargs):
        calls.append(kwargs)
        return conn

    result = probe_postgresql_readonly(
        host="127.0.0.1", port=5432, database="rag_21mm", user="review_ro",
        password="synthetic-test-only", connector=connector)
    assert result["status"] == "PASS"
    assert result["verification"] == "LIVE_READ_ONLY_TRANSACTION"
    assert conn.cur.statements == [
        "BEGIN TRANSACTION READ ONLY",
        "SELECT current_database(), current_setting('transaction_read_only')",
        "ROLLBACK",
    ]
    assert calls[0]["autocommit"] is True
    assert "default_transaction_read_only=on" in calls[0]["options"]
    assert calls[0]["password"] == "synthetic-test-only"
    assert conn.closed


@pytest.mark.parametrize("row", [("wrong_db", "on"), ("rag_21mm", "off"), None])
def test_sql_probe_fails_closed_on_wrong_identity(row):
    result = probe_postgresql_readonly(
        host="localhost", port=5432, database="rag_21mm", user="review_ro",
        password="synthetic-test-only", connector=lambda **kw: FakeConnection(row))
    assert result["status"] == "BLOCKED"


@pytest.mark.parametrize("kwargs", [
    {"host": "postgres:///bad"}, {"password": ""}, {"database": "other db"},
    {"port": 0}, {"timeout": 600}, {"user": "postgres;DROP"},
])
def test_sql_probe_invalid_inputs_never_connect(kwargs):
    opts = dict(host="127.0.0.1", port=5432, database="rag_21mm", user="review_ro",
                password="fake", timeout=5)
    opts.update(kwargs)
    def connector(**params):
        raise AssertionError("No connection allowed for invalid config")
    assert probe_postgresql_readonly(**opts, connector=connector)["verification"] == "INVALID_PARAMETERS"


def test_sql_error_is_redacted_and_connection_closed():
    class Broken(FakeConnection):
        def cursor(self):
            raise RuntimeError("password fake-leak")
    conn = Broken(("rag_21mm", "on"))
    result = probe_postgresql_readonly(
        host="127.0.0.1", port=5432, database="rag_21mm", user="review_ro",
        password="fake-leak", connector=lambda **kw: conn)
    assert result == {"status": "BLOCKED", "verification": "CONNECTION_OR_QUERY_FAILED_REDACTED"}
    assert conn.closed


def test_gui_probe_non_windows_never_launches_process():
    def forbidden(*a, **kw):
        raise AssertionError("No subprocess outside Windows")
    result = probe_gui_import(ROOT, platform_name="linux", runner=forbidden)
    assert result["status"] == "NOT_WINDOWS"


def test_gui_import_windows_simulated_result():
    calls = []
    def runner(*args, **kwargs):
        calls.append((args,kwargs))
        return SimpleNamespace(returncode=0, stdout="GUI_IMPORT_OK\n")
    result = probe_gui_import(ROOT, platform_name="win32", runner=runner)
    assert result["status"] == "PASS"
    assert result["verification"] == "IMPORT_ONLY_NOT_INTERACTIVE_GUI"
    assert "app.gui.application" in calls[0][0][0][-1]
    assert calls[0][1]["timeout"] == 25


def test_gui_probe_bad_exit_is_blocked():
    def runner(*args, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="secret")
    assert probe_gui_import(ROOT, platform_name="win32", runner=runner)["status"] == "BLOCKED"


def test_cli_default_report_no_live_actions(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "ROOT", ROOT)
    monkeypatch.setattr(cli, "probe_postgresql_readonly", lambda **kw: pytest.fail("not authorized"))
    monkeypatch.setattr(cli, "probe_gui_import", lambda *a: pytest.fail("not authorized"))
    target = tmp_path / "report.json"
    assert cli.main(["--out", str(target)]) == 0
    data = json.loads(target.read_text("utf-8"))
    assert data["stage1_production_acceptance"] == "NOT_VERIFIED"
    assert data["postgresql_readonly_probe"]["status"] == "NOT_REQUESTED"
    assert data["windows_gui_import"]["status"] == "NOT_REQUESTED"
    assert not data["windows_gui_interactive_accepted"]
    assert cli.main(["--out", str(target)]) == 2
    assert "NOT_VERIFIED" in capsys.readouterr().err


def test_cli_database_only_by_explicit_flag_and_env(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "ROOT", ROOT)
    monkeypatch.setenv("DIC_STAGE1_RO_PASSWORD", "synthetic-secret")
    received = []
    def fake_probe(**kwargs):
        received.append(kwargs)
        return {"status": "PASS", "verification": "LIVE_READ_ONLY_TRANSACTION"}
    monkeypatch.setattr(cli, "probe_postgresql_readonly", fake_probe)
    out = tmp_path / "report.json"
    assert cli.main(["--out", str(out), "--check-db", "--db-host", "localhost",
                     "--db-name", "rag_21mm", "--db-user", "read_only"]) == 0
    assert received[0]["password"] == "synthetic-secret"
    data = out.read_text("utf-8")
    assert "synthetic-secret" not in data
    assert "read_only" not in data
    assert "NOT_VERIFIED" in data


def test_cli_rejects_database_params_without_opt_in(tmp_path):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--out", str(tmp_path / "x"), "--db-host", "localhost"])
    assert exc.value.code == 2
    assert not (tmp_path / "x").exists()
