"""Stage 2 v1.35: global LOGIN-role privilege paths must fail closed.

Synthetic catalog snapshots are *not* proof that real grants and SET ROLE work.
"""
from __future__ import annotations

import pytest

from app.engineering_intelligence.stage2_db_preflight import inspect_stage2_db
from tests.test_v134_stage2_permission_preflight import SnapshotCursor


def role(name: str, *, registered: bool = False, execute: bool = False,
         owner_member: bool = False, sensitive_field: int | None = None):
    """A 17-column row matching the SELECT catalog projection."""
    flags = [False] * 13
    if sensitive_field is not None:
        flags[sensitive_field] = True
    return (name, registered, execute, owner_member, *flags)


def audit(cur: SnapshotCursor):
    return inspect_stage2_db(cur, database="rag_21mm", project="21MM",
                             reviewer="reviewer_21mm", application_roles=("app_user",))


def failed(result, name: str):
    assert not result.passed
    assert result.as_dict()["result"] == "BLOCKED"
    assert any(c.name == name and not c.passed for c in result.checks)


def test_global_role_scan_will_not_treat_pass_as_a_production_certificate():
    result = audit(SnapshotCursor(other_logins=[role("unlisted_reader")]))
    assert result.passed
    assert result.as_dict()["schema"] == "dic-stage2-readonly-preflight-v3"
    assert result.as_dict()["production_acceptance"] == "NOT_VERIFIED"


@pytest.mark.parametrize("role_row,violation", [
    (role("unknown_user", execute=True), "controlled_execute"),
    (role("disabled_reviewer", execute=True), "controlled_execute"),
    (role("unlisted_worker", owner_member=True), "no_owner_membership"),
    (role("registered_reviewer", registered=True, owner_member=True), "no_owner_membership"),
    (role("another_reviewer", registered=True, execute=True, sensitive_field=1), "no_sensitive_write_or_ddl"),
    (role("app2", sensitive_field=0), "no_sensitive_write_or_ddl"),
    (role("app3", sensitive_field=2), "no_sensitive_write_or_ddl"),
    (role("app4", sensitive_field=3), "no_sensitive_write_or_ddl"),
    (role("app5", sensitive_field=4), "no_sensitive_write_or_ddl"),
    (role("app6", sensitive_field=5), "no_sensitive_write_or_ddl"),
    (role("app7", sensitive_field=6), "no_sensitive_write_or_ddl"),
    (role("app8", sensitive_field=7), "no_sensitive_write_or_ddl"),
    (role("app9", sensitive_field=8), "no_sensitive_write_or_ddl"),
    (role("app10", sensitive_field=9), "no_sensitive_write_or_ddl"),
    (role("app11", sensitive_field=10), "no_sensitive_write_or_ddl"),
    (role("app12", sensitive_field=11), "no_sensitive_write_or_ddl"),
    (role("app13", sensitive_field=12), "no_sensitive_write_or_ddl"),
])
def test_unlisted_roles_and_indirect_privileges_are_not_trusted(role_row, violation):
    result = audit(SnapshotCursor(other_logins=[role_row]))
    failed(result, f"login:{role_row[0]}:{violation}")


def test_guard_invoker_or_locked_search_path_drift_blocks():
    failed(audit(SnapshotCursor(guard_functions=1)), "invoker_locked_guard_functions")


@pytest.mark.parametrize("index", range(4))
def test_privileged_function_owner_blocks(index):
    flags = [False] * 4
    flags[index] = True
    failed(audit(SnapshotCursor(owner_flags=tuple(flags))), "least_privilege_function_owner")


def test_scan_is_complete_and_bounded():
    extras = [role(f"other{i}") for i in range(499)]
    result = audit(SnapshotCursor(other_logins=extras))
    failed(result, "complete_login_role_scan")


def test_no_named_reviewer_in_audited_connecting_logins_is_blocked():
    class MissingReviewer(SnapshotCursor):
        def fetchall(self):
            sql, _ = self.queries[-1]
            if "FROM pg_catalog.pg_roles r" in sql:
                return [role("app_user")]
            return super().fetchall()

    failed(audit(MissingReviewer()), "reviewer_visible_in_global_scan")


def test_global_scan_checks_effective_column_trigger_and_membership_permissions():
    cursor = SnapshotCursor(other_logins=[role("other_login")])
    assert audit(cursor).passed
    sql = next(sql for sql, _ in cursor.queries if "FROM pg_catalog.pg_roles r" in sql)
    assert "pg_has_role(r.oid,p.proowner,'MEMBER')" in sql
    assert "has_any_column_privilege" in sql
    assert "'TRIGGER'" in sql and "'TRUNCATE'" in sql
    assert "r.rolcanlogin AND NOT r.rolsuper" in sql
    assert "LIMIT 501" in sql
    assert all(statement.strip().startswith("SELECT") for statement, _ in cursor.queries)
