from types import SimpleNamespace
from unittest.mock import Mock

from app.ai.rag_service import EnterpriseRAGService
from app.ai.retrieval import EvidenceItem, RetrievalResult


def test_single_ticket_steps_use_model_with_complete_record():
    ticket = EvidenceItem(evidence_id="", scope="QABUG", source_type="ISSUE_EXACT",
        content="Original record", metadata={"issue_key":"MM21PROBL-56138",
        "repro_steps":"1) Play USB 2) Pause", "effect_result":"AllWords 21UP_HENREN; RC2407D.U"})
    retrieval = RetrievalResult(evidence=(ticket,), resolved_scopes=("QABUG",), route="ISSUE_QUERY")
    service = EnterpriseRAGService.__new__(EnterpriseRAGService)
    service.prepare = Mock(return_value=([], retrieval))
    service.settings = SimpleNamespace(ollama_keep_alive="5m")
    service._runtime_context_for = Mock(return_value=8192)
    service.ollama = Mock()
    service.ollama.chat_stream.return_value = iter(["前提条件\n1. 播放USB音乐。\n2. 暂停。"])
    result = "".join(service.answer_stream([
        {"role":"user", "content":"BUG MM21PROBL-56138的发生手顺是什么"}], model="test"))
    assert "1. 播放USB音乐" in result
    prompt = service.ollama.chat_stream.call_args.kwargs["messages"][0]["content"]
    assert "1) Play USB 2) Pause" in prompt
    assert "AllWords 21UP_HENREN" not in prompt
    assert "编号操作步骤" in prompt
    assert "票据记载" in prompt


def test_issue_list_keeps_pagination_without_model():
    service = EnterpriseRAGService.__new__(EnterpriseRAGService)
    retrieval = RetrievalResult(evidence=(), resolved_scopes=("QABUG",), route="ISSUE_QUERY")
    service.prepare = Mock(return_value=([], retrieval))
    service.ollama = Mock()
    list(service.answer_stream([{"role":"user", "content":"列出QA"}], model="test"))
    service.ollama.chat_stream.assert_not_called()
