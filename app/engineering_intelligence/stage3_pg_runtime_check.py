"""Read-only PostgreSQL 17 runtime checks for Stage 3 temporal evidence audit.

A PASS proves only the checked database shape and current LOGIN restrictions
at the time of the transaction. It is NOT a functional/production acceptance.
"""
from __future__ import annotations

import re
from typing import Any

REQUIRED_TABLES = (
    'public.project_info', 'public.specifications',
    'public.specification_versions', 'public.documents', 'public.contents',
    'public.ei_requirement_units', 'public.ei_content_relations',
    'public.ei_version_mappings', 'public.ei_version_backfill_receipts',
    'public.specification_change_sets',
)
WRITE_GUARDED_TABLES = (
    'public.ei_requirement_units', 'public.ei_content_relations',
    'public.ei_version_mappings', 'public.ei_version_backfill_receipts',
    'public.ei_relation_review_audit',
)


def inspect_stage3_runtime(cur: Any, *, database: str, project: str) -> dict[str, Any]:
    """Caller MUST open REPEATABLE READ READ ONLY and use a restricted login."""
    if (not re.fullmatch(r'[A-Za-z0-9_-]{2,80}', database)
            or not re.fullmatch(r'[A-Za-z0-9_-]{2,40}', project)
            or project.upper() in {'QABUG', 'RAG_QABUG'}
            or database.lower() in {'rag', 'rag_qabug'}):
        raise ValueError('Individual project database and project required')
    checks: list[dict[str, Any]] = []
    def add(name: str, passed: bool) -> None:
        checks.append({'name': name, 'passed': bool(passed)})

    cur.execute('''SELECT current_database() AS db, current_user AS role,
                          session_user AS login,
                          current_setting('server_version_num')::integer AS version''')
    identity = cur.fetchall()
    if len(identity) != 1:
        add('UNIQUE_CONNECTION_IDENTITY', False)
        return {'schema':'dic-stage3-runtime-preflight-v1','status':'BLOCKED',
                'checks':checks,'production_acceptance':'NOT_VERIFIED'}
    row = dict(identity[0]); role = str(row['role']); login = str(row['login'])
    add('POSTGRESQL_17', 170000 <= int(row['version']) < 180000)
    add('DATABASE_IDENTITY', row['db'] == database)
    add('RESTRICTED_LOGIN', role not in {'postgres','root'} and login not in {'postgres','root'})
    cur.execute('SELECT project_code FROM public.project_info LIMIT 2')
    projects = cur.fetchall()
    add('PROJECT_IDENTITY', len(projects) == 1 and projects[0]['project_code'] == project)
    cur.execute('''SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, rolreplication
                   FROM pg_roles WHERE rolname IN (current_user, session_user)''')
    roles = [dict(v) for v in cur.fetchall()]
    add('NO_ROLE_ADMINISTRATIVE_PRIVILEGES', len(roles) in {1,2} and all(
        not any(v[k] for k in ('rolsuper','rolbypassrls','rolcreaterole','rolcreatedb','rolreplication'))
        for v in roles))
    cur.execute('''SELECT count(*) AS member_count FROM pg_roles
                   WHERE (rolsuper OR rolbypassrls OR rolcreaterole OR rolcreatedb OR rolreplication)
                     AND pg_has_role(current_user, oid, 'MEMBER')''')
    add('NO_ADMIN_ROLE_MEMBERSHIP', int(cur.fetchall()[0]['member_count']) == 0)
    cur.execute('''SELECT has_schema_privilege(current_user,'public','CREATE') AS public_create,
                          has_database_privilege(current_user,current_database(),'CREATE') AS database_create''')
    privilege = dict(cur.fetchall()[0])
    add('NO_SCHEMA_OR_DATABASE_CREATE', not privilege['public_create'] and not privilege['database_create'])
    for relation in REQUIRED_TABLES + ('public.ei_relation_review_audit',):
        cur.execute('SELECT to_regclass(%s) IS NOT NULL AS present', (relation,))
        add('REQUIRED_TABLE_' + relation.rsplit('.',1)[-1], bool(cur.fetchall()[0]['present']))
    # Calling privilege functions for absent tables is an error. Fail closed
    # before proceeding to the effective privilege check.
    if all(item['passed'] for item in checks if item['name'].startswith('REQUIRED_TABLE_')):
        for relation in WRITE_GUARDED_TABLES:
            cur.execute('''SELECT has_table_privilege(current_user,%s,'INSERT') AS can_insert,
                                  has_table_privilege(current_user,%s,'UPDATE') AS can_update,
                                  has_any_column_privilege(current_user,%s,'UPDATE') AS can_update_column,
                                  has_table_privilege(current_user,%s,'DELETE') AS can_delete,
                                  has_table_privilege(current_user,%s,'TRUNCATE') AS can_truncate,
                                  has_table_privilege(current_user,%s,'TRIGGER') AS can_trigger,
                                  has_table_privilege(current_user,%s,'SELECT') AS can_select''',
                        (relation,) * 7)
            grants = dict(cur.fetchall()[0])
            add('READ_WITHOUT_WRITE_' + relation.rsplit('.',1)[-1],
                grants['can_select'] and not any(
                    grants[k] for k in ('can_insert','can_update','can_update_column',
                                        'can_delete','can_truncate','can_trigger')))
        cur.execute('''SELECT count(*) AS member_count FROM pg_class c
                       WHERE c.oid = ANY(%s::regclass[])
                         AND pg_has_role(current_user, c.relowner, 'MEMBER')''',
                    (list(WRITE_GUARDED_TABLES),))
        add('NO_SENSITIVE_TABLE_OWNER_MEMBERSHIP',
            int(cur.fetchall()[0]['member_count']) == 0)
    else:
        add('EFFECTIVE_PRIVILEGE_CHECK_BLOCKED_BY_MISSING_TABLE', False)
    return {'schema':'dic-stage3-runtime-preflight-v1',
            'status':'PASS' if all(c['passed'] for c in checks) else 'BLOCKED',
            'checks':checks,'production_acceptance':'NOT_VERIFIED',
            'read_only':True, 'database':database,'project':project}
