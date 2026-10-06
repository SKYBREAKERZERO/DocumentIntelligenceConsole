from __future__ import annotations

from pathlib import Path

from app.ai.agent import QueryPlanner
from app.ai.retrieval import EnterpriseRetriever


ROOT = Path(__file__).resolve().parents[1]


class MinimalPlannerClient:
    def __init__(self, tools: list[str]):
        self.tools = tools

    def chat_json(self, **kwargs):
        return {
            "scopes": ["21MM"],
            "intents": ["ENGINEERING_QA"],
            "entities": [],
            "rewritten_queries": ["550"],
            "tools": self.tools,
            "summary": "minimal test plan",
        }


def test_bare_spec_code_is_extracted_for_explicit_relation_and_version_questions():
    assert EnterpriseRetriever._entity_spec_codes((), "550和哪些式样存在工程关系") == ("550",)
    assert EnterpriseRetriever._entity_spec_codes((), "550从2.10到2.11有哪些版本改动") == ("550",)


def test_version_label_parser_does_not_treat_spec_code_as_version():
    assert EnterpriseRetriever._version_labels_in_question("550从2.10到2.11有哪些改动") == ("2.10", "2.11")


def test_llm_planner_cannot_omit_relation_tool_for_explicit_relation_intent():
    planner = QueryPlanner(MinimalPlannerClient(["semantic_search"]))
    plan = planner.plan(
        model="qwen",
        question="550和哪些式样存在工程关系",
        requested_scope="21MM",
        current_scope="21MM",
        conversation=[],
    ).plan
    assert "relation_search" in plan.tools


def test_llm_planner_cannot_omit_version_tool_for_explicit_version_intent():
    planner = QueryPlanner(MinimalPlannerClient(["semantic_search"]))
    plan = planner.plan(
        model="qwen",
        question="550从2.10到2.11有哪些改动",
        requested_scope="21MM",
        current_scope="21MM",
        conversation=[],
    ).plan
    assert "version_search" in plan.tools


def test_ai_relation_reader_uses_same_graph_policy_and_manual_registry_as_ui():
    source = (ROOT / "app/ai/retrieval.py").read_text(encoding="utf-8")
    assert "EngineeringRelationGraphService.is_primary_reference_row" in source
    assert "public.engineering_relation_catalog" in source
    assert "public.manual_spec_relations" in source
    assert 'source_type="RELATION_GRAPH"' in source
    assert "direction=BOTH" in source


def test_ai_version_reader_hydrates_persisted_history_and_change_details():
    source = (ROOT / "app/ai/retrieval.py").read_text(encoding="utf-8")
    assert "public.specification_version_history" in source
    assert "public.specification_change_summary" in source
    assert "public.specification_change_detail" in source
    assert 'source_type="VERSION_HISTORY"' in source
    assert 'source_type="VERSION_CHANGE_DETAIL"' in source


def test_answer_policy_names_graph_and_version_evidence_as_authoritative():
    source = (ROOT / "app/ai/rag_service.py").read_text(encoding="utf-8")
    assert "RELATION_GRAPH evidence is the AI-readable form of the Engineering Relations graph" in source
    assert "VERSION_HISTORY and VERSION_CHANGE_DETAIL" in source


def test_relation_search_returns_graph_neighborhood_instead_of_generic_token_hits():
    retriever = object.__new__(EnterpriseRetriever)
    retriever.top_k = 8

    rows = [
        {
            "relation_id": 1,
            "source_specification_id": 10,
            "source_specification_key": "21MM:NA:555",
            "source_specification_name": "VUI Function Spec",
            "source_spec_code": "555",
            "source_version": "2.0",
            "target_specification_id": 20,
            "target_specification_key": "21MM:NA:550",
            "target_specification_name": "Bluetooth Audio Function Spec",
            "target_spec_code": "550",
            "target_external_key": None,
            "relation_type": "REFERENCES",
            "extraction_method": "RULE",
            "confidence": 1.0,
            "status": "CONFIRMED",
            "evidence_count": 2,
            "metadata": {},
        },
        {
            "relation_id": 2,
            "source_specification_id": 20,
            "source_specification_key": "21MM:NA:550",
            "source_specification_name": "Bluetooth Audio Function Spec",
            "source_spec_code": "550",
            "source_version": "2.1",
            "target_specification_id": None,
            "target_specification_key": None,
            "target_specification_name": None,
            "target_spec_code": None,
            "target_external_key": "SPEC:132",
            "relation_type": "REFERENCES",
            "extraction_method": "RULE",
            "confidence": 0.9,
            "status": "UNRESOLVED",
            "evidence_count": 1,
            "metadata": {},
        },
        {
            "relation_id": 3,
            "source_specification_id": 20,
            "source_specification_key": "21MM:NA:550",
            "source_specification_name": "Bluetooth Audio Function Spec",
            "source_spec_code": "550",
            "source_version": "2.1",
            "target_specification_id": None,
            "target_specification_key": None,
            "target_specification_name": None,
            "target_spec_code": None,
            "target_external_key": "SPEC:512",
            "relation_type": "REFERENCES",
            "extraction_method": "RULE",
            "confidence": 0.88,
            "status": "UNRESOLVED",
            "evidence_count": 1,
            "metadata": {},
        },
        # This supporting/title-only relation is intentionally excluded by the same
        # graph policy used in the Engineering Relations page.
        {
            "relation_id": 99,
            "source_specification_id": 99,
            "source_specification_key": "21MM:UNSPECIFIED:NAME:random",
            "source_specification_name": "Cover Sheet",
            "source_spec_code": "",
            "source_version": "1",
            "target_specification_id": 20,
            "target_specification_key": "21MM:NA:550",
            "target_specification_name": "Bluetooth Audio Function Spec",
            "target_spec_code": "550",
            "target_external_key": None,
            "relation_type": "REFERENCES",
            "extraction_method": "RULE",
            "confidence": 1.0,
            "status": "CONFIRMED",
            "evidence_count": 5,
            "metadata": {},
        },
    ]

    retriever._relation_registry_rows = lambda scope, spec_codes=(): rows  # type: ignore[method-assign]
    evidence = retriever._relation_search("550和哪些式样存在工程关系", ("21MM",))

    assert len(evidence) == 1
    item = evidence[0]
    assert item.source_type == "RELATION_GRAPH"
    assert item.specification_code == "550"
    assert "INCOMING: 555 VUI Function Spec" in item.content
    assert "OUTGOING: 550 Bluetooth Audio Function Spec" in item.content
    assert "SPEC:132" not in item.content  # rendered as the primary Spec No.
    assert "132" in item.content and "512" in item.content
    assert "Cover Sheet" not in item.content
    assert item.metadata["edge_count"] == 3
