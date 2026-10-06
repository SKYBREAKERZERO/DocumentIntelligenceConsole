from __future__ import annotations

from app.ai.agent import EvidenceGovernor, QueryPlanner
from app.ai.config import AISettings
from app.ai.retrieval import EvidenceItem


class BrokenPlannerClient:
    def chat_json(self, **kwargs):
        raise RuntimeError("planner unavailable")


def test_ai_settings_defaults_to_single_auto_mode():
    settings = AISettings()
    assert settings.retrieval_mode == "AI AUTO"
    assert settings.evidence_limit == 4
    assert settings.audit_enabled is True


def test_query_planner_falls_back_to_safe_allow_list():
    planner = QueryPlanner(BrokenPlannerClient())
    result = planner.plan(
        model="qwen-test",
        question="24MM SPEC-1001 引用了哪些式样书？",
        requested_scope="AUTO",
        current_scope="",
        conversation=[],
    )
    assert result.plan.planner_source == "deterministic-fallback"
    assert "relation_search" in result.plan.tools
    assert "sql_exact_search" in result.plan.tools
    assert result.plan.scopes[0] == "24MM"
    assert all(tool in {
        "semantic_search", "sql_exact_search", "relation_search", "version_search", "unresolved_reference_search"
    } for tool in result.plan.tools)


def test_evidence_governor_prioritizes_confirmed_relation():
    evidence = (
        EvidenceItem(
            evidence_id="", scope="24MM", source_type="SEMANTIC", content="similar text",
            content_id=10, score=0.95, specification_code="1001",
        ),
        EvidenceItem(
            evidence_id="", scope="24MM", source_type="RELATION", content="1001 -> 2007",
            relation_id=11, score=1.0, specification_code="1001", metadata={"status": "CONFIRMED"},
        ),
    )
    governed, strength, conflicts = EvidenceGovernor.govern(evidence, 8)
    assert governed[0].source_type == "RELATION"
    assert governed[0].metadata["evidence_grade"] == "HIGH"
    assert strength == "STRONG"
    assert conflicts == ()


def test_llm_plan_rejects_unknown_tools():
    class Client:
        def chat_json(self, **kwargs):
            return {
                "scopes": ["24MM"],
                "intents": ["QA"],
                "entities": ["1001"],
                "rewritten_queries": ["1001 interface"],
                "tools": ["execute_sql", "semantic_search"],
                "summary": "test",
            }
    planner = QueryPlanner(Client())
    plan = planner.plan(
        model="qwen", question="1001 interface", requested_scope="AUTO", current_scope="24MM", conversation=[]
    ).plan
    assert plan.tools == ("semantic_search",)
    assert "execute_sql" not in plan.tools
