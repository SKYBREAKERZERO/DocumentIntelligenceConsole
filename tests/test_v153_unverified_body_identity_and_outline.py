"""Production-grounded regression: v2.52 is MAIN body version_id=3, not a verified 550 version."""
from app.ai.retrieval import EnterpriseRetriever, RetrievalResult
from app.ai.spec_facts import render_sql_facts, lookup_outline, lookup_adjacent_versions

BODY = '21MM:UNSPECIFIED:NAME:a349727f3f5156c17204@v2.52'
COVER = '21MM:EXCEPT_NA:550@v2.50'

class Cursor:
    def __init__(self, calls):
        self.sql = ''
        self.calls = calls
    def execute(self, sql, params):
        self.sql = sql
        self.calls.append((sql, params))
    def fetchall(self):
        if 'LEFT JOIN public.specification_versions sv' in self.sql and 'd.updated_at' in self.sql:
            # Only PRIMARY body survives the real SQL document-role predicate.
            return [dict(document_id=BODY, file_name='Bluetooth Audio function spec v2.52.pdf',
                        version='v2.52', metadata={}, linked_version_id=3,
                        linked_version='v2.52', linked_branch='MAIN',
                        linked_specification_id=3, linked_spec_code=None,
                        linked_region=None)]
        if 'FROM public.chapters' in self.sql:
            return [dict(chapter_id='2', title_jp='機能', title_en='Functions', sort_order=2)]
        if 'FROM public.sections' in self.sql:
            return [dict(chapter_id='2', section_id='2.1', title_jp='Bluetooth Audio',
                         title_en='Bluetooth Audio', page_number=8, sort_order=1)]
        return []
    def __enter__(self): return self
    def __exit__(self, *_): return False

class Connection:
    def __init__(self, calls): self.calls = calls
    def cursor(self): return Cursor(self.calls)
    def __enter__(self): return self
    def __exit__(self, *_): return False


def _retriever():
    calls=[]
    retriever=EnterpriseRetriever.__new__(EnterpriseRetriever)
    retriever._connect=lambda scope: Connection(calls)
    return retriever, calls


def test_main_body_not_upgraded_to_verified_spec_550():
    retriever,calls=_retriever()
    result,warnings=retriever._specification_lookup('550 BT式样书有吗', ('21MM',))
    assert not warnings
    assert len(result)==1
    doc=result[0]
    assert doc.document_id == BODY and doc.source_type=='SPEC_IDENTITY_CANDIDATE'
    assert doc.metadata['linked_version_id']==3
    assert doc.metadata['linked_specification_id']==3
    assert doc.metadata['linked_spec_code'] is None
    assert doc.metadata['linkage_verified'] is False
    assert doc.specification_code == ''
    answer=render_sql_facts(RetrievalResult(
        evidence=tuple(result), resolved_scopes=('21MM',), route='SPEC_LOOKUP'),
        '550 BT式样书有吗')
    assert 'v2.52' in answer and '550' in answer
    assert '尚未核准' in answer and COVER not in answer
    fallback_queries=[(q,p) for q,p in calls if 'd.updated_at' in q]
    assert len(fallback_queries)==1
    query,params=fallback_queries[0]
    assert 'public.specification_versions sv' in query
    assert 'public.specifications sp' in query
    assert '%Bluetooth Audio%' in params
    assert '%Bluetooth%' not in params
    assert query.count('%s')==len(params)


def test_unverified_numeric_identity_does_not_block_physical_body_outline():
    retriever,_=_retriever()
    result,warnings=lookup_outline(retriever,'查一下21MM的550 BT式样书按目录总结',('21MM',))
    assert not warnings and len(result)==1
    assert result[0].source_type=='SPEC_OUTLINE_CANDIDATE'
    assert result[0].metadata['version_id']==3
    text=render_sql_facts(RetrievalResult(evidence=tuple(result),
        resolved_scopes=('21MM',), route='SPEC_OUTLINE'), '550 BT按目录总结')
    assert 'Bluetooth Audio function spec v2.52.pdf' in text
    assert '编号归属未核准' in text and '2.1' in text and 'PDF页=8' in text
    assert COVER not in text


def test_not_two_trusted_body_versions_and_no_cover_comparison():
    retriever,_=_retriever()
    result,warnings=lookup_adjacent_versions(retriever,'550 BT相邻两个本体版本变更',('21MM',))
    assert not warnings and len(result)==1
    assert result[0].metadata['versions'][0]['id']==3
    assert result[0].metadata['identity_verified'] is False
    text=render_sql_facts(RetrievalResult(evidence=tuple(result),
        resolved_scopes=('21MM',), route='SPEC_VERSION_FACTS'), '550版本比较')
    assert 'v2.52' in text and '没有两个可信' in text and 'Cover Sheet' in text
    assert COVER not in text
