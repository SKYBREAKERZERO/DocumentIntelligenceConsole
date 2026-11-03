from app.ai.agent import FastIntentRouter
from app.ai.retrieval import EvidenceItem, RetrievalResult
from app.ai.spec_facts import render_sql_facts


def test_outline_is_ordered_and_hides_internal_markers():
    item = EvidenceItem(evidence_id='', scope='21MM', source_type='SPEC_OUTLINE', content='',
        specification_code='508', metadata={'outline':(
            {'chapter_id':'2', 'title':'Two'}, {'chapter_id':'1','title':'One'},
            {'section_id':'2.ROOT'}, {'section_id':'1.2','title':'Subsection'})})
    result = RetrievalResult(evidence=(item,), resolved_scopes=('21MM',), route='SPEC_OUTLINE')
    text = render_sql_facts(result, '按目录展开')
    assert text.index('1 One') < text.index('1.2 Subsection') < text.index('2 Two')
    assert '.ROOT' not in text
    assert 'PDF页=未知' not in text


def test_chapter_expansion_requests_model_summary_unless_verbatim():
    for question in ('508按章节展开', '508按目录总结内容'):
        assert FastIntentRouter.classify(question).route == 'SPEC_SUMMARY'


def test_process_followup_retains_active_document_and_uses_model():
    for question in ('登录车机用户的流程是什么', '按 章 节 展开说说'):
        contextual = FastIntentRouter.contextualize(question, [
            {'role':'user', 'content':'查一下21MM的508式样书'},
            {'role':'user', 'content':question}])
        assert 'SPEC-508' in contextual
        assert FastIntentRouter.classify(contextual).route == 'SPEC_SUMMARY'
