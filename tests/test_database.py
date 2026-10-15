"""DatabaseConnection must never connect during test collection or module import."""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from app.config.config_loader import DatabaseConfig
from app.database.connection import DatabaseConnection, DatabaseConnectionError


def _config() -> DatabaseConfig:
    return DatabaseConfig(
        enabled=True, host="127.0.0.1", port=5432, database="rag_21mm",
        project_databases={"21MM": "rag_21mm"}, user="read_only",
        password="test-only-fake", password_env="DIC_FAKE_PASSWORD", connect_timeout=3,
    )


def test_import_and_construct_does_not_connect(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Unexpected live DB connection")

    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=forbidden, Error=Exception))
    db = DatabaseConnection(_config())
    assert db.get_connection_info()["database"] == "rag_21mm"
    assert "password" not in db.get_connection_info()


def test_connect_uses_only_configured_driver(monkeypatch):
    called = []
    token = object()

    def fake_connect(**kwargs):
        called.append(kwargs)
        return token

    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=fake_connect, Error=Exception))
    db = DatabaseConnection(_config())
    assert db.connect() is token
    assert called == [{"host": "127.0.0.1", "port": 5432, "dbname": "rag_21mm",
                       "user": "read_only", "password": "test-only-fake", "connect_timeout": 3}]


def test_driver_error_redacts_secret(monkeypatch):
    def fake_connect(**kwargs):
        raise RuntimeError("host rejected password=test-only-fake")

    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=fake_connect, Error=RuntimeError))
    with pytest.raises(DatabaseConnectionError) as exc:
        DatabaseConnection(_config()).connect()
    assert "test-only-fake" not in str(exc.value)
