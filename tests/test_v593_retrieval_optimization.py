from __future__ import annotations

from app.ai.agent import FastIntentRouter
from app.ai.config import AISettings
from app.ai.rag_service import EnterpriseRAGService
from app.ai.retrieval import EnterpriseRetriever, EvidenceItem, RetrievalResult


def _service() -> EnterpriseRAGService:
    service = object.__new__(EnterpriseRAGService)
    service.settings = AISettings()
    return service


def test_wifi_find_one_routes_to_semantic_issue_search_not_exact_lookup():
    question = "我想找个关于WIFI的BUG 给我找一个JIRA号"
    decision = FastIntentRouter.classify(question)
    assert decision.route == "ISSUE_QUERY"
    assert decision.intent == "ISSUE_SEARCH_ONE"
    filters = EnterpriseRetriever._parse_issue_filters(question)
    assert filters["record_type"] == "BUG"
    assert "issue_key" not in filters
    assert EnterpriseRetriever._issue_query_mode(question, filters) == "FIND_ONE"
    assert EnterpriseRetriever._issue_free_text_query(question, filters) == "WIFI"


def test_issue_free_text_query_removes_request_scaffolding_but_keeps_symptoms():
    q = "有哪些蓝牙断线相关BUG"
    filters = EnterpriseRetriever._parse_issue_filters(q)
    assert EnterpriseRetriever._issue_free_text_query(q, filters) == "蓝牙断线"

    q2 = "给我找一个CarPlay黑屏的BUG"
    filters2 = EnterpriseRetriever._parse_issue_filters(q2)
    assert EnterpriseRetriever._issue_free_text_query(q2, filters2) == "CarPlay 黑屏"


def test_explicit_issue_key_remains_exact_and_skips_semantic_query():
    q = "MM21PROBL-3147现在是什么状态"
    decision = FastIntentRouter.classify(q)
    filters = EnterpriseRetriever._parse_issue_filters(q)
    assert decision.intent == "ISSUE_EXACT_LOOKUP"
    assert EnterpriseRetriever._issue_query_mode(q, filters) == "EXACT"
    assert EnterpriseRetriever._issue_free_text_query(q, filters) == ""


def test_sql_and_qdrant_candidates_fuse_by_issue_key_with_sql_authority():
    sql = EvidenceItem(
        evidence_id="",
        scope="QABUG",
        source_type="ISSUE_SQL_MATCH",
        content="Issue Key: BUG-77\nSummary: WiFi disconnect",
        score=0.56,
        metadata={"issue_key": "BUG-77", "summary": "WiFi disconnect", "source_authority": "POSTGRESQL"},
    )
    sem = EvidenceItem(
        evidence_id="",
        scope="QABUG",
        source_type="ISSUE_SEMANTIC",
        content="WiFi reconnect fails",
        score=0.91,
        metadata={"issue_key": "BUG-77", "section_type": "BUG_PHENOMENON"},
    )
    fused = EnterpriseRetriever._fuse_issue_candidates([sql], [sem], mode="SEARCH")
    assert len(fused) == 1
    assert fused[0].source_type == "ISSUE_HYBRID"
    assert fused[0].score == 0.91
    assert fused[0].metadata["source_authority"] == "POSTGRESQL"
    assert fused[0].metadata["retrieval_sources"] == ["POSTGRESQL", "QDRANT"]


def test_prompt_budget_defaults_are_safe_for_4096_context():
    s = AISettings()
    assert s.num_ctx == 4096
    assert s.prompt_budget_tokens == 3200
    assert s.generation_reserve_tokens == 800
    assert s.history_budget_tokens == 650
    assert s.evidence_budget_tokens == 1500
    assert s.answer_history_turns == 3
    assert s.simple_issue_evidence_limit == 2
    assert s.prompt_budget_tokens + s.generation_reserve_tokens <= s.num_ctx


def test_simple_issue_context_is_compact_and_token_bounded():
    service = _service()
    item = EvidenceItem(
        evidence_id="1",
        scope="QABUG",
        source_type="ISSUE_HYBRID",
        content=("very long raw jira text " * 2000),
        score=0.93,
        metadata={
            "issue_key": "MM21PROBL-3147",
            "record_type": "BUG",
            "summary": "WiFi connection intermittently fails",
            "status_raw": "クローズ",
            "priority_raw": "高",
            "component": "WiFi",
            "assignee": "owner",
            "release_plan_version": "01.00.00.A",
        },
    )
    context = service._build_context_budgeted(
        (item,),
        route="ISSUE_QUERY",
        intents=("ISSUE_SEARCH_ONE",),
        token_budget=500,
    )
    assert "MM21PROBL-3147" in context
    assert "WiFi connection intermittently fails" in context
    assert "very long raw jira text" not in context
    assert service._estimate_tokens(context) <= 500


def test_simple_issue_evidence_limit_is_route_specific():
    service = _service()
    find_one = RetrievalResult(
        evidence=(), resolved_scopes=("QABUG",), route="ISSUE_QUERY", intents=("ISSUE_SEARCH_ONE",)
    )
    exact = RetrievalResult(
        evidence=(), resolved_scopes=("QABUG",), route="ISSUE_QUERY", intents=("ISSUE_EXACT_LOOKUP",)
    )
    generic = RetrievalResult(
        evidence=(), resolved_scopes=("QABUG",), route="ISSUE_QUERY", intents=("ISSUE_SEARCH",)
    )
    assert service._effective_evidence_limit("find one", find_one) == 2
    assert service._effective_evidence_limit("BUG-1", exact) == 1
    assert service._effective_evidence_limit("list", generic) == 3


def test_history_budget_keeps_latest_user_and_discards_old_noise():
    service = _service()
    messages = []
    for i in range(10):
        messages.append({"role": "user", "content": f"old question {i} " + "x" * 200})
        messages.append({"role": "assistant", "content": f"old answer {i} " + "y" * 200})
    messages.append({"role": "user", "content": "给我找一个WiFi BUG"})
    bounded = service._bounded_history(messages, token_budget=220)
    assert bounded[-1]["role"] == "user"
    assert bounded[-1]["content"] == "给我找一个WiFi BUG"
    assert len(bounded) <= service.settings.answer_history_turns * 2


def test_evidence_governor_preserves_distinct_issue_records_without_content_ids():
    from app.ai.agent import EvidenceGovernor
    evidence = tuple(
        EvidenceItem(
            evidence_id="",
            scope="QABUG",
            source_type="ISSUE_HYBRID",
            content=f"Issue {i}",
            score=0.90 - i * 0.01,
            metadata={"issue_key": f"BUG-{i}"},
        )
        for i in range(3)
    )
    governed, _, _ = EvidenceGovernor.govern(evidence, 3)
    assert [x.metadata["issue_key"] for x in governed] == ["BUG-0", "BUG-1", "BUG-2"]
