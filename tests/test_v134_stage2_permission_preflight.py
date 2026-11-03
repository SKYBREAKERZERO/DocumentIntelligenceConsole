"""Stage 2 v1.34 authorization preflight, independent of live DB availability."""
from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from app.engineering_intelligence.stage2_db_preflight import (
    Stage2PreflightError, inspect_stage2_db, SEMANTIC_GUARD_SHA256, REFERENCE_HELPER_SHA256,
)

ROOT = Path(__file__).resolve().parents[1]


class SnapshotCursor:
    """Returns catalog-shaped values but enforces read-only query texts."""
    def __init__(self, *, db="rag_21mm", project="21MM", owner_login=False,
                 owner_super=False, public_execute=False, guards=2, reviewer_enrolled=1,
                 reviewer_flags=None, reviewer_grants=None, app_grants=None, trust=True,
                 guard_functions=2, owner_flags=None, other_logins=None, semantic_guard_hash=None, reference_hash=None, semantic_flags=None):
        self.db, self.project = db, project
        self.owner_login, self.owner_super = owner_login, owner_super
        self.public_execute, self.guards = public_execute, guards
        self.reviewer_enrolled = reviewer_enrolled
        self.reviewer_flags = reviewer_flags if reviewer_flags is not None else (True, False, False, False, False, False)
        self.reviewer_grants = reviewer_grants if reviewer_grants is not None else (True, True, False, False, False, False, False, False, False)
        self.app_grants = app_grants if app_grants is not None else (False, False, False, False, False, False, False, False, False)
        self.trust = trust
        self.guard_functions = guard_functions
        self.owner_flags = owner_flags if owner_flags is not None else (False,) * 4
        self.other_logins = other_logins if other_logins is not None else []
        self.semantic_guard_hash = semantic_guard_hash if semantic_guard_hash is not None else SEMANTIC_GUARD_SHA256
        self.reference_hash = reference_hash if reference_hash is not None else REFERENCE_HELPER_SHA256
        self.semantic_flags = semantic_flags if semantic_flags is not None else (True, True, True)
        self.queries = []

    def execute(self, statement, params=()):
        self.queries.append((statement, params))
        if statement.strip().startswith(("UPDATE ", "DELETE ", "INSERT ", "ALTER ", "GRANT ", "REVOKE ", "CREATE ")):
            raise AssertionError("Preflight attempted to mutate DB")

    def fetchall(self):
        sql, _ = self.queries[-1]
        if "FROM pg_catalog.pg_roles r" in sql:
            safe_reviewer = ("reviewer_21mm", True, True, False) + (False,) * 13
            safe_app = ("app_user", False, False, False) + (False,) * 13
            return [safe_reviewer, safe_app, *self.other_logins]
        return [(self.project,)]

    def fetchone(self):
        sql, params = self.queries[-1]
        if "current_database()" in sql:
            return (self.db, "audit_login", "audit_login")
        if "encode(sha256(convert_to(p.prosrc,'UTF8')),'hex')" in sql:
            return (self.semantic_guard_hash, self.reference_hash, *self.semantic_flags)
        if "COUNT(*) FROM pg_catalog.pg_proc p" in sql:
            return (self.guard_functions,)
        if "r.rolcreatedb,r.rolcreaterole" in sql:
            return self.owner_flags
        if "prosecdef" in sql:
            return (True, True, True, True, self.trust, self.trust,
                    self.owner_login, self.owner_super, not self.public_execute)
        if "COUNT(*) FROM pg_catalog.pg_trigger" in sql:
            return (self.guards,)
        if "COUNT(*) FROM pg_catalog.pg_proc p" in sql:
            return (self.guard_functions,)
        if "rolcanlogin,rolsuper" in sql:
            return self.reviewer_flags
        if "COUNT(*) FROM dic_review.authorized_reviewers" in sql:
            return (self.reviewer_enrolled,)
        if "has_schema_privilege" in sql:
            return self.reviewer_grants if params[0] == "reviewer_21mm" else self.app_grants
        raise AssertionError(f"Unexpected SQL: {sql}")


def invoke(cursor=None, *, apps=("app_user",), **kw):
    return inspect_stage2_db(cursor or SnapshotCursor(), database="rag_21mm", project="21MM",
                             reviewer="reviewer_21mm", application_roles=apps, **kw)


def test_synthetic_compliant_snapshot_is_only_preflight_not_production_certificate():
    cur = SnapshotCursor()
    result = invoke(cur)
    assert result.passed and result.as_dict()["result"] == "PASS"
    assert result.as_dict()["production_acceptance"] == "NOT_VERIFIED"
    assert all(statement.strip().upper().startswith("SELECT") for statement, _ in cur.queries)
    assert "reviewer_21mm" not in " ".join(sql for sql, _ in cur.queries)
    assert any("tgfoid=to_regprocedure" in sql and "t.tgtype=31" in sql for sql, _ in cur.queries)
    assert all("password" not in json.dumps(check.__dict__ if hasattr(check, '__dict__') else check.detail)
               for check in result.checks)


@pytest.mark.parametrize("changed,expected", [
    ({"db": "rag_24mm"}, "database_identity"),
    ({"project": "QABUG"}, "exclusive_project_scope"),
    ({"owner_login": True}, "nonlogin_function_owner"),
    ({"owner_super": True}, "nonsuper_function_owner"),
    ({"public_execute": True}, "no_public_function_execute"),
    ({"trust": False}, "security_definer"),
    ({"guards": 1}, "bound_active_write_guards"),
    ({"reviewer_enrolled": 0}, "reviewer_explicit_enrollment"),
    ({"reviewer_flags": (True, True, False, False, False, False)}, "reviewer_login_least_privilege"),
    ({"reviewer_grants": (True, False, False, False, False, False, False, False, False)}, "reviewer_21mm:function_access"),
    ({"reviewer_grants": (True, True, False, False, False, True, False, False, False)}, "reviewer_21mm:no_sensitive_write_grants"),
    ({"app_grants": (False, True, False, False, False, False, False, False, False)}, "app_user:function_access"),
    ({"app_grants": (False, False, False, True, False, False, False, False, False)}, "app_user:no_sensitive_write_grants"),
    ({"app_grants": (False, False, False, False, False, False, False, False, True)}, "app_user:no_sensitive_write_grants"),
])
def test_fail_closed_under_privilege_or_identity_drift(changed, expected):
    result = invoke(SnapshotCursor(**changed))
    assert not result.passed and result.as_dict()["result"] == "BLOCKED"
    assert any(c.name == expected and not c.passed for c in result.checks)


def test_query_scoped_role_names_never_become_sql_tokens():
    cur = SnapshotCursor()
    injection = "x'); DROP TABLE public.ei_content_relations;--"
    result = inspect_stage2_db(cur, database="rag_21mm", project="21MM",
                               reviewer="reviewer_21mm", application_roles=(injection,))
    assert result.passed
    assert any(injection in params for _, params in cur.queries)
    assert all(injection not in sql for sql, _ in cur.queries)


@pytest.mark.parametrize("reviewer,apps", [
    ("", ()), ("postgres", ()), (" reviewer_21mm", ()),
    ("reviewer_21mm", ("reviewer_21mm",)),
    ("reviewer_21mm", ("app_user", "app_user")),
    ("reviewer_21mm", ("",)),
])
def test_invalid_identity_fails_before_any_query(reviewer, apps):
    cur = SnapshotCursor()
    with pytest.raises(Stage2PreflightError):
        inspect_stage2_db(cur, database="rag_21mm", project="21MM",
                           reviewer=reviewer, application_roles=apps)
    assert not cur.queries


class FakeConn:
    def __init__(self, cur):
        self.cur = cur
        self.rollbacks = 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def cursor(self):
        return self

    def execute(self, sql, params=()):
        self.cur.execute(sql, params)

    def fetchone(self):
        return self.cur.fetchone()

    def fetchall(self):
        return self.cur.fetchall()

    def rollback(self):
        self.rollbacks += 1


def cli_module():
    spec = importlib.util.spec_from_file_location("dic134_preflight_cli", ROOT / "scripts/preflight_stage2_db.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cli_transactions_fail_closed_and_never_overwrite(monkeypatch, tmp_path):
    c = SnapshotCursor(public_execute=True)
    conn = FakeConn(c)
    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=lambda **_: conn))
    monkeypatch.setenv("POSTGRES_PASSWORD", "nonlogged-test-secret")
    args = ["--database", "rag_21mm", "--project", "21MM",
            "--reviewer", "reviewer_21mm", "--app-role", "app_user",
            "--audit-user", "audit_login", "--output", str(tmp_path / "report.json")]
    mod = cli_module()
    assert mod.main(args) == 2
    data = json.loads((tmp_path / "report.json").read_text())
    assert data["result"] == "BLOCKED"
    assert "nonlogged-test-secret" not in json.dumps(data)
    assert c.queries[0][0] == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
    assert conn.rollbacks == 1
    # Existence checked before making a second DB connection.
    with pytest.raises(SystemExit):
        mod.main(args)


def test_cli_success_only_reports_preflight(monkeypatch, capsys):
    conn = FakeConn(SnapshotCursor())
    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=lambda **_: conn))
    monkeypatch.setenv("POSTGRES_PASSWORD", "test")
    status = cli_module().main(["--database", "rag_21mm", "--project", "21MM",
                                "--reviewer", "reviewer_21mm", "--audit-user", "audit_login"])
    assert status == 0
    output = json.loads(capsys.readouterr().out)
    assert output["production_acceptance"] == "NOT_VERIFIED"
    assert output["result"] == "PASS"
    assert conn.rollbacks == 1


def test_main_gui_has_stage2_launcher_with_no_shared_password_and_frozen_guard():
    source = (ROOT / "app/gui/application.py").read_text(encoding="utf-8")
    mod = ast.parse(source)
    methods = [node for node in ast.walk(mod)
               if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and
               node.name == "_open_stage2_review_window"]
    assert len(methods) == 1
    chunk = ast.get_source_segment(source, methods[0])
    assert "getattr(sys, \"frozen\", False)" in chunk
    assert '"-m", "app.gui.stage2_review_window"' in chunk
    assert "self.password" not in chunk and "postgres_password" not in chunk
    assert "command=self._open_stage2_review_window" in source
    assert 'APP_VERSION = "1.52"' in source


def test_rag_precondition_tests_function_owner_and_trigger_binding():
    source = (ROOT / "app/ai/retrieval.py").read_text(encoding="utf-8")
    assert "AS trusted_review_authority" in source
    assert "t.tgtype=31" in source
    assert "tgfoid=to_regprocedure('dic_review.guard_relation_write()')" in source
    assert "exists.get(\"trusted_review_authority\") is True" in source
