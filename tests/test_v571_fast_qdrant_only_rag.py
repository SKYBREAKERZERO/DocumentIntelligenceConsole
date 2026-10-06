from __future__ import annotations

from pathlib import Path

from app.ai.agent import FastIntentRouter
from app.ai.config import AISettings
from app.ai.rag_service import EnterpriseRAGService
from app.enterprise.profiles import QdrantProfile


ROOT = Path(__file__).resolve().parents[1]


def test_fast_router_bypasses_greetings_and_general_chat():
    assert FastIntentRouter.classify("你好").route == "DIRECT_CHAT"
    assert FastIntentRouter.classify("谢谢").route == "DIRECT_CHAT"
    assert FastIntentRouter.classify("帮我写一个Python循环").route == "DIRECT_CHAT"


def test_fast_router_uses_enterprise_auto_routes():
    assert FastIntentRouter.classify("508的User Profile功能是什么？").route == "ENGINEERING_AGENT"
    assert FastIntentRouter.classify("这个式样书的Section 4.12.5写了什么？").route == "ENGINEERING_AGENT"
    assert FastIntentRouter.classify("ECU接口要求是什么？").route == "ENGINEERING_AGENT"
    assert FastIntentRouter.classify("现在库里有哪些式样书？").route == "SPEC_CATALOG"
    assert FastIntentRouter.classify("现在有多少open bug？").route == "ISSUE_STATS"


def test_direct_chat_prepare_performs_no_retrieval():
    service = EnterpriseRAGService(
        settings=AISettings(),
        postgres_profiles={},
        postgres_password="",
        qdrant_profile=QdrantProfile(),
    )
    prepared, result = service.prepare(
        [{"role": "user", "content": "你好"}],
        model="qwen3:4b-instruct",
    )
    assert prepared
    assert result.route == "DIRECT_CHAT"
    assert result.knowledge_source == "NONE"
    assert result.evidence == ()
    assert result.tools == ()
    assert result.semantic_ms == 0
    assert result.sql_ms == 0


def test_desktop_rag_path_is_enterprise_auto_by_source_contract():
    source = (ROOT / "app/ai/rag_service.py").read_text(encoding="utf-8")
    assert "retrieve_spec_catalog" in source
    assert "retrieve_issue_register" in source
    assert "self.agent.retrieve(" in source
    assert "PostgreSQL remains the source of truth" in source
    assert "No model-generated SQL is ever executed" in source


def test_workspace_reuses_service_and_shows_visible_trace():
    source = (ROOT / "app/gui/ai_workspace.py").read_text(encoding="utf-8")
    assert "_rag_service_key" in source
    assert "_warmup_rag_runtime" in source
    assert "处理轨迹 / Agent Trace / Audit" in source
    assert "DIRECT_CHAT" in source
    assert "工程 Agentic RAG" in source
    assert "TTFT" in source


def test_ollama_keep_alive_timeouts_and_thinking_off_supported():
    source = (ROOT / "app/ai/ollama_client.py").read_text(encoding="utf-8")
    assert 'body["keep_alive"]' in source
    assert 'body["think"]' in source
    assert "first_load_timeout" in source
    assert "stream_idle_timeout" in source
    settings = AISettings()
    assert settings.ollama_keep_alive == "30m"
    assert settings.ollama_connect_timeout == 10.0
    assert settings.ollama_read_timeout == 600.0
    assert settings.ollama_first_load_timeout == 900.0
    assert settings.qdrant_only_knowledge is False
