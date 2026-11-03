"""Read-only, fail-closed Stage 2 authorization/configuration preflight.

This is a diagnostic, not a substitute for executing negative grant/transaction
checks using the real DB roles. Run it as a vetted DBA auditor with SELECT on
``dic_review.authorized_reviewers`` and catalog visibility. Never logs secrets,
source text, document names, or relation contents.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


REVIEW_FUNCTION = "dic_review.apply_content_relation_review(bigint,text,text,text,text,text,text)"
# Pin both function bodies rather than accepting only familiar object names.
# SHA-256 is calculated from pg_proc.prosrc (UTF-8) of the v1.37 migration.
SEMANTIC_GUARD_SHA256 = "2f717ff9202ad4b2517af7b67fdb9fa48df362b4c6dacdeb99906e9b23b428b1"
REFERENCE_HELPER_SHA256 = "01850260d8fc6afe73e6297da39535749bb531ad363dbdc261f78cfcfa8cdc0c"


class Stage2PreflightError(ValueError):
    """Misconfigured preflight request or unreadable database shape."""


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class PreflightResult:
    database: str
    project: str
    reviewer: str
    checks: tuple[Check, ...]
    app_roles: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "dic-stage2-readonly-preflight-v3",
            "database": self.database,
            "project": self.project,
            "reviewer": self.reviewer,
            "application_roles": list(self.app_roles),
            "result": "PASS" if self.passed else "BLOCKED",
            "production_acceptance": "NOT_VERIFIED",
            "checks": [{"name": c.name, "result": "PASS" if c.passed else "FAIL",
                        "detail": c.detail} for c in self.checks],
        }


def _one(cur: Any, query: str, params: tuple[Any, ...] = ()) -> tuple[Any, ...]:
    cur.execute(query, params)
    row = cur.fetchone()
    if row is None:
        return ()
    if isinstance(row, dict):
        return tuple(row.values())
    return tuple(row)


def inspect_stage2_db(cur: Any, *, database: str, project: str,
                      reviewer: str, application_roles: tuple[str, ...] = ()) -> PreflightResult:
    """Audit actual effective GRANTs for a reviewer and specified app roles.

    The caller MUST already have opened a REPEATABLE READ READ ONLY transaction.
    No SQL identifiers are interpolated from operator arguments. A caller must
    verify that ``database`` is the connection's real current_database().
    """
    if (not database or not project or project.upper() in {"QABUG", "RAG_QABUG"}
            or not reviewer or reviewer.lower() in {"postgres", "root"}
            or not reviewer.strip() == reviewer
            or any(not role or role.strip() != role or role == reviewer for role in application_roles)
            or len(application_roles) > 10 or len(set(application_roles)) != len(application_roles)):
        raise Stage2PreflightError("Explicit engineering project, reviewer and distinct app roles required")

    checks: list[Check] = []

    def check(name: str, ok: bool, failure: str) -> None:
        checks.append(Check(name, bool(ok), "verified" if ok else failure))

    actual_db, session_user, current_user = _one(
        cur, "SELECT current_database(), session_user::text, current_user::text")
    check("database_identity", actual_db == database, "connected database name differs from expected")
    check("operator_session", session_user == current_user,
          "auditor used SET ROLE; assess with the direct authenticated login")

    cur.execute("SELECT project_code FROM public.project_info ORDER BY project_code LIMIT 2")
    rows = cur.fetchall()
    codes = [str(row["project_code"] if isinstance(row, dict) else row[0]) for row in rows]
    check("exclusive_project_scope", codes == [project],
          "database must contain exactly the declared engineering project")

    structure = _one(cur, """
        SELECT
          to_regclass('public.ei_content_relations') IS NOT NULL,
          to_regclass('public.ei_relation_review_audit') IS NOT NULL,
          to_regclass('dic_review.authorized_reviewers') IS NOT NULL,
          EXISTS (SELECT 1 FROM pg_catalog.pg_attribute a
                  WHERE a.attrelid=to_regclass('public.ei_relation_review_audit')
                    AND a.attname='identity_provenance' AND NOT a.attisdropped),
          COALESCE((SELECT p.prosecdef FROM pg_catalog.pg_proc p
                    WHERE p.oid=to_regprocedure('dic_review.apply_content_relation_review(bigint,text,text,text,text,text,text)')),false),
          COALESCE((SELECT 'search_path=pg_catalog'=ANY(p.proconfig)
                    FROM pg_catalog.pg_proc p
                    WHERE p.oid=to_regprocedure('dic_review.apply_content_relation_review(bigint,text,text,text,text,text,text)')),false),
          (SELECT r.rolcanlogin FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_roles r ON r.oid=p.proowner
           WHERE p.oid=to_regprocedure('dic_review.apply_content_relation_review(bigint,text,text,text,text,text,text)')),
          (SELECT r.rolsuper FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_roles r ON r.oid=p.proowner
           WHERE p.oid=to_regprocedure('dic_review.apply_content_relation_review(bigint,text,text,text,text,text,text)')),
          COALESCE((SELECT NOT EXISTS (
              SELECT 1 FROM pg_catalog.aclexplode(COALESCE(p.proacl,
                                    pg_catalog.acldefault('f',p.proowner))) a
              WHERE a.grantee=0 AND a.privilege_type='EXECUTE')
            FROM pg_catalog.pg_proc p
            WHERE p.oid=to_regprocedure('dic_review.apply_content_relation_review(bigint,text,text,text,text,text,text)')),false)
    """)
    labels = ("relation_table", "audit_table", "reviewer_registry", "provenance_column",
              "security_definer", "locked_search_path")
    for index, label in enumerate(labels):
        check(label, bool(structure and structure[index]), "required DB object or property absent")
    check("nonlogin_function_owner", bool(structure and structure[6] is False),
          "controlled function owner must be NOLOGIN")
    check("nonsuper_function_owner", bool(structure and structure[7] is False),
          "controlled function owner must not be SUPERUSER")
    check("no_public_function_execute", bool(structure and structure[8]),
          "PUBLIC can execute the controlled review function")

    owner_flags = _one(cur, """
        SELECT r.rolcreatedb,r.rolcreaterole,r.rolreplication,r.rolbypassrls
        FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_roles r ON r.oid=p.proowner
        WHERE p.oid=to_regprocedure('dic_review.apply_content_relation_review(bigint,text,text,text,text,text,text)')
    """)
    check("least_privilege_function_owner", len(owner_flags) == 4 and
          all(flag is False for flag in owner_flags),
          "function owner must not have CREATEDB/CREATEROLE/REPLICATION/BYPASSRLS")

    # Both trigger functions must remain SECURITY INVOKER with an immutable
    # search path. A changed trigger body/owner still requires independent DBA
    # review; this check detects privilege/search-path drift without DDL.
    guard_functions = _one(cur, """
        SELECT COUNT(*) FROM pg_catalog.pg_proc p
        WHERE p.oid IN (
            to_regprocedure('dic_review.guard_relation_write()'),
            to_regprocedure('dic_review.guard_audit_write()'))
          AND NOT p.prosecdef AND 'search_path=pg_catalog'=ANY(p.proconfig)
    """)
    check("invoker_locked_guard_functions", bool(guard_functions and guard_functions[0] == 2),
          "both review guards must be SECURITY INVOKER with locked search_path")

    # v1.37: fail closed for old v1.33/v1.36 SQL installations. This is a
    # read-only attestation of exact function source, schema ownership and
    # EXECUTE ACL, not evidence of tests against a live PostgreSQL instance.
    semantic = _one(cur, """
        SELECT
          (SELECT encode(sha256(convert_to(p.prosrc,'UTF8')),'hex')
           FROM pg_catalog.pg_proc p
           WHERE p.oid=to_regprocedure('dic_review.guard_relation_write()')),
          (SELECT encode(sha256(convert_to(p.prosrc,'UTF8')),'hex')
           FROM pg_catalog.pg_proc p
           WHERE p.oid=to_regprocedure('dic_review.has_unambiguous_section_reference(text,text,text)')),
          COALESCE((SELECT NOT p.prosecdef AND p.provolatile='i'
                   AND 'search_path=pg_catalog'=ANY(p.proconfig)
                   FROM pg_catalog.pg_proc p
                   WHERE p.oid=to_regprocedure('dic_review.has_unambiguous_section_reference(text,text,text)')), false),
          COALESCE((SELECT p.proowner = (
                            SELECT f.proowner FROM pg_catalog.pg_proc f
                            WHERE f.oid=to_regprocedure('dic_review.apply_content_relation_review(bigint,text,text,text,text,text,text)'))
                   FROM pg_catalog.pg_proc p
                   WHERE p.oid=to_regprocedure('dic_review.has_unambiguous_section_reference(text,text,text)')), false),
          COALESCE((SELECT NOT EXISTS (
                       SELECT 1 FROM pg_catalog.aclexplode(COALESCE(p.proacl,
                                          pg_catalog.acldefault('f',p.proowner))) a
                       WHERE a.grantee=0 AND a.privilege_type='EXECUTE')
                   FROM pg_catalog.pg_proc p
                   WHERE p.oid=to_regprocedure('dic_review.has_unambiguous_section_reference(text,text,text)')), false)
    """)
    check("v137_pinned_relation_guard", len(semantic) == 5 and semantic[0] == SEMANTIC_GUARD_SHA256,
          "v1.37 semantic trigger body absent or changed; run reviewed migration")
    check("v137_pinned_reference_helper", len(semantic) == 5 and semantic[1] == REFERENCE_HELPER_SHA256,
          "v1.37 server citation parser absent or changed")
    check("v137_helper_authority", len(semantic) == 5 and all(v is True for v in semantic[2:]),
          "citation parser must be immutable, invoker, same trusted owner and no PUBLIC EXECUTE")

    guards = _one(cur, """
        SELECT COUNT(*) FROM pg_catalog.pg_trigger t
        JOIN pg_catalog.pg_class c ON c.oid=t.tgrelid
        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND t.tgenabled='O' AND NOT t.tgisinternal
          AND t.tgtype=31
          AND ((c.relname='ei_content_relations' AND t.tgname='dic_stage2_relation_guard'
                AND t.tgfoid=to_regprocedure('dic_review.guard_relation_write()'))
            OR (c.relname='ei_relation_review_audit' AND t.tgname='dic_stage2_audit_guard'
                AND t.tgfoid=to_regprocedure('dic_review.guard_audit_write()')))
    """)
    check("bound_active_write_guards", bool(guards and guards[0] == 2),
          "both BEFORE ROW INSERT/UPDATE/DELETE guards must bind expected functions")

    reviewer_role = _one(cur, """
        SELECT rolcanlogin,rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls
          FROM pg_catalog.pg_roles WHERE rolname=%s
    """, (reviewer,))
    check("reviewer_login_least_privilege",
          len(reviewer_role) == 6 and reviewer_role[0] is True and
          all(value is False for value in reviewer_role[1:]),
          "reviewer must exist as a non-privileged LOGIN")
    registered = _one(cur, """
        SELECT COUNT(*) FROM dic_review.authorized_reviewers
        WHERE login_name=%s AND disabled_at IS NULL
    """, (reviewer,))
    check("reviewer_explicit_enrollment", bool(registered and registered[0] == 1),
          "reviewer missing, disabled or not explicitly enrolled by DBA")

    # Global audit: naming an app role is insufficient. Other LOGIN roles in
    # this same database may inherit EXECUTE, column UPDATE, TRIGGER or CREATE.
    # pg_has_role(...,'MEMBER') conservatively rejects even NOINHERIT membership
    # in the trusted function owner, including SET ROLE paths. No SET ROLE is
    # performed by this diagnostic; cluster superusers remain trusted DBA root.
    cur.execute("""
        SELECT r.rolname::text AS login_name,
               ar.login_name IS NOT NULL AS registered,
               pg_catalog.has_function_privilege(r.oid,p.oid,'EXECUTE') AS can_review,
               pg_catalog.pg_has_role(r.oid,p.proowner,'MEMBER') AS owner_membership,
               pg_catalog.has_table_privilege(r.oid,'public.ei_content_relations','UPDATE') AS relation_update,
               pg_catalog.has_any_column_privilege(r.oid,'public.ei_content_relations','UPDATE') AS relation_column_update,
               pg_catalog.has_table_privilege(r.oid,'public.ei_content_relations','DELETE') AS relation_delete,
               pg_catalog.has_table_privilege(r.oid,'public.ei_content_relations','TRUNCATE') AS relation_truncate,
               pg_catalog.has_table_privilege(r.oid,'public.ei_content_relations','TRIGGER') AS relation_trigger,
               pg_catalog.has_table_privilege(r.oid,'public.ei_relation_review_audit','INSERT') AS audit_insert,
               pg_catalog.has_table_privilege(r.oid,'public.ei_relation_review_audit','UPDATE') AS audit_update,
               pg_catalog.has_any_column_privilege(r.oid,'public.ei_relation_review_audit','UPDATE') AS audit_column_update,
               pg_catalog.has_table_privilege(r.oid,'public.ei_relation_review_audit','DELETE') AS audit_delete,
               pg_catalog.has_table_privilege(r.oid,'public.ei_relation_review_audit','TRUNCATE') AS audit_truncate,
               pg_catalog.has_table_privilege(r.oid,'public.ei_relation_review_audit','TRIGGER') AS audit_trigger,
               pg_catalog.has_schema_privilege(r.oid,'public','CREATE') AS public_create,
               pg_catalog.has_schema_privilege(r.oid,'dic_review','CREATE') AS review_create
        FROM pg_catalog.pg_roles r
        CROSS JOIN pg_catalog.pg_proc p
        LEFT JOIN dic_review.authorized_reviewers ar
               ON ar.login_name=r.rolname AND ar.disabled_at IS NULL
        WHERE p.oid=to_regprocedure('dic_review.apply_content_relation_review(bigint,text,text,text,text,text,text)')
          AND r.rolcanlogin AND NOT r.rolsuper
          AND pg_catalog.has_database_privilege(r.oid,current_database(),'CONNECT')
        ORDER BY r.rolname LIMIT 501
    """)
    role_rows = cur.fetchall()
    check("complete_login_role_scan", len(role_rows) <= 500,
          "more than 500 connecting LOGIN roles; full scan is required")
    check("reviewer_visible_in_global_scan",
          any((row.get("login_name") if isinstance(row, dict) else row[0]) == reviewer
              for row in role_rows),
          "declared reviewer cannot CONNECT or is missing from full LOGIN audit")
    # Explicitly fail if the query cannot return meaningful per-role records;
    # a missing function or role metadata must not be interpreted as safety.
    for row in role_rows:
        if isinstance(row, dict):
            vals = tuple(row.values())
        else:
            vals = tuple(row)
        if len(vals) != 17 or not isinstance(vals[0], str):
            raise Stage2PreflightError("Incomplete PostgreSQL role-audit row")
        name, registered, can_review, owner_member, *sensitive = vals
        # Never whitelist a role based on its name alone. Every additional
        # reviewer must have a currently active DBA enrollment.
        check(f"login:{name}:controlled_execute", can_review is False or registered is True,
              "unregistered login has review function EXECUTE")
        check(f"login:{name}:no_owner_membership", owner_member is False,
              "login is a member of the trusted function owner, including SET ROLE paths")
        check(f"login:{name}:no_sensitive_write_or_ddl", all(v is False for v in sensitive),
              "effective table, column, trigger, TRUNCATE or schema CREATE privilege")

    roles = (reviewer,) + application_roles
    for name in roles:
        p = _one(cur, """
            SELECT
              pg_catalog.has_schema_privilege(%s,'dic_review','USAGE'),
              pg_catalog.has_function_privilege(%s,'dic_review.apply_content_relation_review(bigint,text,text,text,text,text,text)','EXECUTE'),
              pg_catalog.has_table_privilege(%s,'public.ei_content_relations','UPDATE'),
              pg_catalog.has_table_privilege(%s,'public.ei_content_relations','DELETE'),
              pg_catalog.has_table_privilege(%s,'public.ei_relation_review_audit','INSERT'),
              pg_catalog.has_table_privilege(%s,'public.ei_relation_review_audit','UPDATE'),
              pg_catalog.has_table_privilege(%s,'public.ei_relation_review_audit','DELETE'),
              pg_catalog.has_schema_privilege(%s,'dic_review','CREATE'),
              pg_catalog.has_schema_privilege(%s,'public','CREATE')
        """, (name,) * 9)
        is_reviewer = name == reviewer
        check(f"{name}:function_access", len(p) == 9 and
              (p[0] is True and p[1] is True if is_reviewer else p[1] is False),
              "reviewer must have USAGE+EXECUTE; app roles must not execute reviews")
        check(f"{name}:no_sensitive_write_grants", len(p) == 9 and
              all(v is False for v in p[2:]),
              "effective DML or schema CREATE privilege remains (including inheritance)")
    return PreflightResult(database=actual_db, project=project, reviewer=reviewer,
                           app_roles=application_roles, checks=tuple(checks))
