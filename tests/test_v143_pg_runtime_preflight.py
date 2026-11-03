"""Offline simulation of Stage 3 PostgreSQL read-only preflight gates."""
from __future__ import annotations

import pytest

from app.engineering_intelligence.stage3_pg_runtime_check import (
    REQUIRED_TABLES, WRITE_GUARDED_TABLES, inspect_stage3_runtime,
)

class PG:
    def __init__(self):
        self.queries=[]; self.rows=[]; self.version=170005; self.project='21MM'
        self.login='dic_reader'; self.role='dic_reader'; self.admin=False
        self.admin_members=0; self.can_create=False; self.missing=set(); self.writes=set()
        self.owners=0
    def execute(self, sql, params=()):
        self.queries.append((sql,params))
        if 'server_version_num' in sql:
            self.rows=[dict(db='rag_21mm',role=self.role,login=self.login,version=self.version)]
        elif 'FROM public.project_info' in sql:
            self.rows=[dict(project_code=self.project)]
        elif 'FROM pg_roles WHERE rolname' in sql:
            self.rows=[dict(rolsuper=self.admin,rolbypassrls=False,rolcreaterole=False,
                            rolcreatedb=False,rolreplication=False)]
        elif 'FROM pg_roles' in sql:
            self.rows=[dict(member_count=self.admin_members)]
        elif 'has_schema_privilege' in sql:
            self.rows=[dict(public_create=self.can_create,database_create=False)]
        elif 'to_regclass' in sql:
            self.rows=[dict(present=params[0] not in self.missing)]
        elif 'has_any_column_privilege' in sql:
            self.rows=[dict(can_select=True,can_insert=False,can_update=False,
                can_update_column=params[0] in self.writes,can_delete=False,
                can_truncate=False,can_trigger=False)]
        elif 'FROM pg_class c' in sql:
            self.rows=[dict(member_count=self.owners)]
        else:
            raise AssertionError(f'Unexpected SQL {sql}')
    def fetchall(self):
        return self.rows

def audit(db=None):
    return inspect_stage3_runtime(db or PG(),database='rag_21mm',project='21MM')

def test_good_restricted_pg17_readonly_schema_checks():
    db=PG(); report=audit(db)
    assert report['status']=='PASS'
    assert report['production_acceptance']=='NOT_VERIFIED'
    assert len([c for c in report['checks'] if c['name'].startswith('REQUIRED_TABLE_')]) == len(REQUIRED_TABLES)+1
    assert len([c for c in report['checks'] if c['name'].startswith('READ_WITHOUT_WRITE_')]) == len(WRITE_GUARDED_TABLES)
    assert all(sql.lstrip().upper().startswith('SELECT') for sql,_ in db.queries)
    assert all('%s' in sql for sql,params in db.queries if params)

@pytest.mark.parametrize('field,value', [
    ('version',160000),('project','24MM'),('login','postgres'),('admin',True),
    ('admin_members',1),('can_create',True),('owners',1),
])
def test_rejects_incompatible_pg_or_privilege(field,value):
    db=PG();setattr(db,field,value)
    assert audit(db)['status']=='BLOCKED'


def test_column_write_is_detected():
    db=PG();db.writes.add('public.ei_content_relations')
    report=audit(db)
    assert report['status']=='BLOCKED'
    assert any(x['name']=='READ_WITHOUT_WRITE_ei_content_relations' and not x['passed'] for x in report['checks'])


def test_missing_table_fails_closed_without_grant_calls():
    db=PG();db.missing.add('public.ei_version_mappings')
    report=audit(db)
    assert report['status']=='BLOCKED'
    assert any(x['name']=='EFFECTIVE_PRIVILEGE_CHECK_BLOCKED_BY_MISSING_TABLE' for x in report['checks'])
    assert not any('has_any_column_privilege' in sql for sql,_ in db.queries)

@pytest.mark.parametrize('database,project', [('rag','21MM'),('rag_21mm','QABUG'),('rag_21mm','bad sql;')])
def test_invalid_scope_input(database,project):
    with pytest.raises(ValueError):
        inspect_stage3_runtime(PG(),database=database,project=project)
