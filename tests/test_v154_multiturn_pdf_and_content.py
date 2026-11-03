"""Regression for the literal 21MM / Bluetooth v2.52 Open WebUI dialogue."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.ai.agent import FastIntentRouter
from app.ai.retrieval import EvidenceItem, RetrievalResult
from app.ai.rag_service import EnterpriseRAGService
from app.ai.spec_facts import lookup_chapter_content, render_sql_facts

DOC_ID = '21MM:UNSPECIFIED:NAME:a349727f3f5156c17204@v2.52'
TITLE = 'Bluetooth Audio function spec v2.52.pdf'


@pytest.mark.parametrize('question,expected', [
    ('查一下 21MM 的 Bluetooth Audio function spec v2.52.pdf', 'SPEC_LOOKUP'),
    ('Bluetooth Audio function spec v2.52', 'SPEC_LOOKUP'),
    ('Bluetooth Audio function spec v2.52的内容按目录总结一下', 'SPEC_OUTLINE'),
    ('21MM 的 Bluetooth Audio function spec v2.52.pdf 按章节总结一下内容', 'SPEC_CHAPTER_CONTENT'),
    ('Bluetooth Audio function spec v2.52.pdf 有哪些功能要求', 'ENGINEERING_AGENT'),
])
def test_literal_openwebui_questions_do_not_choose_wrong_path(question, expected):
    assert FastIntentRouter.classify(question).route == expected


@pytest.mark.parametrize('question', [
    '508 | User Profile function Spec | body_version=v26.01.15.0 | status=APPROVED',
    '508 | User Profile function Spec | body_version=v26.01.15.0 | status=APPROVED 你上面写了有这本',
    '211 | User Profile | body_version=Revhistory.2.546.A | status=APPROVED',
])
def test_catalog_row_followup_is_exact_sql_lookup(question):
    assert FastIntentRouter.classify(question).route == 'SPEC_LOOKUP'


def test_catalog_like_free_text_is_not_mistaken_for_catalog_row():
    assert FastIntentRouter.classify('508 的 User Profile 功能是什么？').route == 'ENGINEERING_AGENT'


@pytest.mark.parametrize('question', [
    '找一下21MM的508式样书',
    '查找 24MM 508 仕様書',
    '打开一下21MM的508规格书',
])
def test_numeric_spec_find_command_is_exact_sql_lookup(question):
    assert FastIntentRouter.classify(question).route == 'SPEC_LOOKUP'


@pytest.mark.parametrize('question', [
    '21MM 508', '508式样书', '21MM中编号508的资料', '麻烦查一下SPEC-508',
    '给我508', '看下508', '规格书编号508', '21MM工程的508',
])
def test_entity_first_spec_lookup_does_not_depend_on_wording(question):
    assert FastIntentRouter.classify(question).route == 'SPEC_LOOKUP'


@pytest.mark.parametrize('question', [
    '508的功能是什么', '分析508的要求', '对比508两个版本', '508关联哪些式样书',
    '508内部引用了哪些其他式样书', '哪些式样书被508参照', '508依赖哪些规格书',
])
def test_entity_first_lookup_keeps_engineering_questions_governed(question):
    assert FastIntentRouter.classify(question).route != 'SPEC_LOOKUP'


@pytest.mark.parametrize('question', [
    '508内部引用了哪些其他式样书？', '哪些式样书被508参照',
    '508依赖哪些规格书', '508的上游下游是什么',
])
def test_relation_questions_use_deterministic_graph_route(question):
    assert FastIntentRouter.classify(question).route == 'SPEC_RELATIONS'


@pytest.mark.parametrize('followup,route', [
    ('章节内容展开', 'SPEC_CHAPTER_CONTENT'),
    ('按目录展开', 'SPEC_OUTLINE'),
    ('详细说说', 'SPEC_CHAPTER_CONTENT'),
])
def test_numeric_spec_multiturn_navigation_inherits_last_user_identity(followup, route):
    conversation = [
        {'role':'user', 'content':'查询21MM的508式样书'},
        {'role':'assistant', 'content':'untrusted response mentioning SPEC-999'},
        {'role':'user', 'content':followup},
    ]
    contextual = FastIntentRouter.contextualize(followup, conversation)
    assert 'SPEC-508' in contextual
    assert '21MM' in contextual
    assert '999' not in contextual
    assert FastIntentRouter.classify(contextual).route == route


def test_explicit_numeric_expand_is_content_not_identity_lookup():
    assert FastIntentRouter.classify('508式样书展开说说').route == 'SPEC_CHAPTER_CONTENT'


@pytest.mark.parametrize('followup', ['按章节总结一下内容', '总结一下', '按章节概括内容'])
def test_followup_reuses_only_previous_user_pdf(followup):
    conversation = [
        {'role':'user', 'content':f'查一下 21MM 的 {TITLE}'},
        {'role':'assistant', 'content':'fake 24MM doc v9.99.pdf'},
        {'role':'user', 'content':f'{TITLE}的内容按目录总结一下'},
        {'role':'assistant', 'content':'catalog snippet'},
        {'role':'user', 'content':followup},
    ]
    contextual = FastIntentRouter.contextualize(followup, conversation)
    assert TITLE in contextual
    assert '21MM' in contextual
    assert '24MM' not in contextual
    assert FastIntentRouter.classify(contextual).route == 'SPEC_CHAPTER_CONTENT'


def test_followup_without_named_pdf_never_invents_identity():
    conversation = [
        {'role':'user', 'content':'今天怎么样'},
        {'role':'assistant', 'content':f'21MM {TITLE}'},
        {'role':'user', 'content':'总结一下'},
    ]
    assert FastIntentRouter.contextualize('总结一下', conversation) == '总结一下'


def test_annotated_original_excerpts_are_sql_bound_not_cover_content():
    metadata = {'version_id':3, 'outline':({'chapter_id':'1', 'title':'Introduction'},)}
    physical = EvidenceItem(evidence_id='', scope='21MM', source_type='SPEC_OUTLINE_CANDIDATE',
                            content='body', document_id=DOC_ID,file_name=TITLE,metadata=metadata)

    class FakeCursor:
        def __init__(self): self.calls=[]
        def execute(self,sql,args=None): self.calls.append((sql,args))
        def fetchall(self):
            return [dict(chapter_key='1', section_id='1.1',page_number=2,content_id=42,
                         excerpt='This specification describes Bluetooth Audio scope.',
                         excerpt_truncated=False, title='Scope')]
        def __enter__(self): return self
        def __exit__(self,*args): return False
    cursor = FakeCursor()
    class DB:
        def cursor(self): return cursor
        def __enter__(self): return self
        def __exit__(self,*args): return False
    retriever = SimpleNamespace(_connect=lambda scope: DB())
    import app.ai.spec_facts as sf
    original = sf.lookup_outline
    try:
        sf.lookup_outline = lambda *_: ([physical], [])
        rows,warnings=lookup_chapter_content(retriever, f'21MM {TITLE} 按章节总结一下内容', ('21MM',))
    finally:
        sf.lookup_outline=original
    assert not warnings
    assert len(rows)==1 and rows[0].metadata['identity_verified'] is False
    assert cursor.calls[1][1] == (DOC_ID,)
    assert 'public.contents c' in cursor.calls[1][0]
    text=render_sql_facts(RetrievalResult(evidence=tuple(rows),resolved_scopes=('21MM',),
                                         route='SPEC_CHAPTER_CONTENT'), '按章节总结一下内容')
    assert DOC_ID in text and 'content_id=42' in text and 'PDF页=2' in text
    assert '编号/地域' in text
    assert '并非完整技术摘要' in text
    assert 'Cover Sheet' not in text


def test_chapter_sql_fast_path_never_calls_ollama():
    service = EnterpriseRAGService.__new__(EnterpriseRAGService)
    retrieval = RetrievalResult(evidence=(), resolved_scopes=('21MM',), route='SPEC_CHAPTER_CONTENT')
    service.prepare = Mock(return_value=([],retrieval))
    service.ollama = Mock()
    service._emit_stage = Mock()
    text=''.join(service.answer_stream([{'role':'user','content':'按章节总结一下内容'}],
                                       model='qwen3:4b-instruct'))
    assert text and '企业引用完整性校验' not in text
    service.ollama.chat_stream.assert_not_called()


def test_real_service_prepare_fast_path_keeps_candidate_limited_and_no_prompt():
    service = EnterpriseRAGService.__new__(EnterpriseRAGService)
    registered = EvidenceItem(evidence_id='', scope='21MM', source_type='SPEC_CHAPTER_CONTENT',
       content='verbatim section', document_id=DOC_ID, file_name=TITLE,
       metadata={'version_id':3, 'identity_verified':False,
                 'excerpts':({'chapter_key':'1','section_id':'1.1','page_number':2,
                              'content_id':42,'excerpt':'source text','excerpt_truncated':False},)})
    result = RetrievalResult(evidence=(registered,), resolved_scopes=('21MM',),
                             route='SPEC_CHAPTER_CONTENT', evidence_strength='LIMITED')
    service.retriever=Mock()
    service.retriever.retrieve_spec_chapter_content.return_value=result
    service.agent=Mock()
    service.ollama=Mock()
    conversation=[
        {'role':'user','content':f'查一下 21MM 的 {TITLE}'},
        {'role':'assistant','content':'the pdf exists'},
        {'role':'user','content':'按章节总结一下内容'},
    ]
    output=''.join(service.answer_stream(conversation, model='qwen3:4b-instruct',scope='AUTO'))
    assert 'source text' in output and 'content_id=42' in output
    assert DOC_ID in output and '编号/地域' in output
    service.retriever.retrieve_spec_chapter_content.assert_called_once()
    service.agent.retrieve.assert_not_called()
    service.ollama.chat_stream.assert_not_called()
