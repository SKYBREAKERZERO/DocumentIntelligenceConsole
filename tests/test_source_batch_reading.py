from types import SimpleNamespace
from app.ai.rag_service import EnterpriseRAGService


def test_large_source_is_read_in_full_with_original_marker():
    service = EnterpriseRAGService.__new__(EnterpriseRAGService)
    calls = []
    def stream(**kwargs):
        calls.append(kwargs)
        return iter(['分析完成'])
    service.ollama = SimpleNamespace(chat_stream=stream)
    source = '[E1] ' + '用户识别与设置加载。' * 700 + 'FINAL_SOURCE_SENTINEL'
    list(service._read_source_batches(model='test', instruction='根据正文回答',
        blocks=[source], num_ctx=2048, max_tokens=700))
    assert len(calls) > 1
    assert 'FINAL_SOURCE_SENTINEL' in calls[-1]['messages'][0]['content']
    for call in calls:
        prompt = call['messages'][0]['content']
        assert '[E1]' in prompt
        assert service._estimate_tokens(prompt) + call['max_tokens'] < 2048
        assert '[truncated]' not in prompt
