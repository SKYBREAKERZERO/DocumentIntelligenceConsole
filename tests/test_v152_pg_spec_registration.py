"""Regressions grounded in the 21MM Bluetooth PDF/Cover Sheet diagnostic."""
from types import SimpleNamespace

import pytest

from app.ai.agent import FastIntentRouter
from app.ai.retrieval import EnterpriseRetriever, EvidenceItem, RetrievalResult
from app.ai.spec_facts import lookup_adjacent_versions, lookup_outline, render_sql_facts


REAL_BODY_ID = '21MM:UNSPECIFIED:NAME:a349727f3f5156c17204@v2.52'
REAL_COVER_ID = '21MM:EXCEPT_NA:550@v2.50'


@pytest.mark.parametrize('question', [
    'Bluetooth Audio function spec v2.52',
    'Bluetooth Audio function spec v2.52.pdf',
    'Bluetooth Audio function spec v2.52有吗',
    '550 BT式样书有吗',
])
def test_versioned_spec_identity_routes_to_sql(question):
    assert FastIntentRouter.classify(question).route == 'SPEC_LOOKUP'


def test_typed_pg_parameters_and_imported_body_is_not_mislabelled_as_verified():
    queries = []

    class Cursor:
        def __init__(self): self.query = ''
        def execute(self, sql, params):
            self.query = sql
            queries.append((sql, params))
        def fetchall(self):
            if 'SELECT d.document_id, d.file_name, d.version' in self.query:
                # Actual PostgreSQL search result; cover sheet is excluded by
                # the SQL PRIMARY predicate, version matching is exact.
                return [dict(document_id=REAL_BODY_ID,
                             file_name='Bluetooth Audio function spec v2.52.pdf',
                             version='v2.52', metadata={})]
            return []
        def __enter__(self): return self
        def __exit__(self, *args): return False

    class Connection:
        def cursor(self): return Cursor()
        def __enter__(self): return self
        def __exit__(self, *args): return False

    retriever = EnterpriseRetriever.__new__(EnterpriseRetriever)
    retriever._connect = lambda scope: Connection()
    evidence, warnings = retriever._specification_lookup(
        'Bluetooth Audio function spec v2.52有吗', ('21MM',))

    assert not warnings
    assert len(evidence) == 1
    assert evidence[0].source_type == 'SPEC_UNBOUND_FILE'
    assert evidence[0].document_id == REAL_BODY_ID
    assert evidence[0].metadata['linkage_verified'] is False
    sql, params = queries[0]
    assert '%s::text IS NULL' in sql
    assert '= %s::text)' in sql
    assert sql.count('%s') == len(params)
    assert params[:3] == ('2.52', '2.52', '21MM')
    rendered = render_sql_facts(RetrievalResult(
        evidence=tuple(evidence), resolved_scopes=('21MM',), route='SPEC_LOOKUP'),
        'Bluetooth Audio function spec v2.52有吗')
    assert '已找到导入文件' in rendered
    assert '未核实' in rendered
    assert '可信式样书正文' not in rendered
    assert REAL_BODY_ID in rendered


def test_sql_failure_keeps_warning_but_does_not_erase_independent_import_record():
    class Cursor:
        def __init__(self): self.sql = ''
        def execute(self, sql, params):
            self.sql = sql
            if 'SELECT d.document_id, d.file_name, d.version' not in sql:
                raise RuntimeError('Simulated broken spec version lookup')
        def fetchall(self):
            return [dict(document_id=REAL_BODY_ID,
                         file_name='Bluetooth Audio function spec v2.52.pdf',
                         version='v2.52', metadata={})]
        def __enter__(self): return self
        def __exit__(self, *args): return False
    class Connection:
        def cursor(self): return Cursor()
        def __enter__(self): return self
        def __exit__(self, *args): return False
    retriever = EnterpriseRetriever.__new__(EnterpriseRetriever)
    retriever._connect = lambda scope: Connection()
    evidence, warnings = retriever._specification_lookup('550 BT式样书有吗', ('21MM',))
    assert len(evidence) == 1 and evidence[0].source_type == 'SPEC_UNBOUND_FILE'
    assert any('RuntimeError' in warning for warning in warnings)


def test_unbound_file_not_promoted_to_trusted_outline_or_two_versions():
    row = EvidenceItem(evidence_id='', scope='21MM', source_type='SPEC_UNBOUND_FILE',
                       content='Imported only', file_name='Bluetooth Audio function spec v2.52.pdf',
                       document_id=REAL_BODY_ID, metadata={'linkage_verified': False})
    retriever = EnterpriseRetriever.__new__(EnterpriseRetriever)
    retriever._specification_lookup = lambda question, scopes: ([row], [])
    outline, warnings = lookup_outline(retriever, '550 BT 按目录总结', ('21MM',))
    assert not warnings and outline[0].source_type == 'SPEC_UNBOUND_FILE'
    text = render_sql_facts(RetrievalResult(evidence=tuple(outline), resolved_scopes=('21MM',),
                                            route='SPEC_OUTLINE'), '550 BT 按目录总结')
    assert '未核实' in text and '不能' in text
    versions, warnings = lookup_adjacent_versions(retriever, '550 本体版本比较', ('21MM',))
    assert not warnings and len(versions) == 1
    assert '没有两个可信' in render_sql_facts(RetrievalResult(
        evidence=tuple(versions), resolved_scopes=('21MM',), route='SPEC_VERSION_FACTS'), '550')


def test_cover_sheet_cannot_become_primary_registration():
    from app.storage.primary_document_policy import PrimarySpecificationDocumentPolicy
    assert not PrimarySpecificationDocumentPolicy.is_primary(
        file_name='550_Bluetooth Audio Function Spec_Cover_Sheet_Ver.2.50改(電子承認).xlsx', metadata={})
    assert PrimarySpecificationDocumentPolicy.is_primary(
        file_name='Bluetooth Audio function spec v2.52.pdf', metadata={})
    assert REAL_COVER_ID != REAL_BODY_ID
