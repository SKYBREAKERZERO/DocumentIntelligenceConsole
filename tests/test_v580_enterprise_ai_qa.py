from __future__ import annotations

from pathlib import Path

from app.ai.agent import FastIntentRouter
from app.ai.config import AISettings
from app.ai.ollama_client import OllamaClient
from app.analyzer.issue_register_policy import IssueRegisterPolicy
from app.rag.qdrant_governance import PAYLOAD_INDEX_CONTRACT


ROOT = Path(__file__).resolve().parents[1]


def test_router_catalog_queries_use_structured_sql_path():
    cases = (
        "现在库里有哪些式样书？",
        "式样书有几本？",
        "列出当前规格清单",
        "目前有多少 specification？",
    )
    for question in cases:
        assert FastIntentRouter.classify(question).route == "SPEC_CATALOG"


def test_router_issue_queries_separate_statistics_from_lookup():
    assert FastIntentRouter.classify("现在有多少 open BUG？").route == "ISSUE_STATS"
    assert FastIntentRouter.classify("BUG-123 现在是什么状态？").route == "ISSUE_QUERY"
    assert FastIntentRouter.classify("有哪些 high priority Jira issue？").route == "ISSUE_QUERY"


def test_router_general_chat_never_touches_repository():
    assert FastIntentRouter.classify("帮我写一个Python循环").route == "DIRECT_CHAT"
    assert FastIntentRouter.classify("今天晚饭吃什么好？").route == "DIRECT_CHAT"


def test_router_exact_spec_identity_and_complex_engineering_are_separate():
    assert FastIntentRouter.classify("2007是什么规格？").route == "SPEC_LOOKUP"
    assert FastIntentRouter.classify("2007的通信接口要求是什么？").route == "ENGINEERING_AGENT"


def test_issue_register_policy_detects_jira_style_sheet():
    match = IssueRegisterPolicy.detect_headers(
        ["Issue Key", "Issue Type", "Status", "Priority", "Summary", "Root Cause", "Resolution"],
        file_name="jira_bug_export.xlsx",
        sheet_name="QA BUG",
    )
    assert match is not None
    assert "issue_key" in match.recognized_fields
    assert "status" in match.recognized_fields
    assert "root_cause" in match.recognized_fields


def test_issue_register_row_serialization_preserves_enterprise_fields():
    match = IssueRegisterPolicy.detect_headers(
        ["Issue Key", "Status", "Summary", "Root Cause", "Expected Result", "Actual Result"],
        file_name="QA_BUG.xlsx",
    )
    assert match is not None
    metadata = IssueRegisterPolicy.row_metadata(match, ["BUG-42", "Open", "Audio drops", "Race condition", "Stable", "Disconnected"])
    text = IssueRegisterPolicy.serialize_row(metadata, row_number=12)
    assert metadata["issue_key"] == "BUG-42"
    assert metadata["root_cause"] == "Race condition"
    assert "Expected Result: Stable" in text
    assert "Actual Result: Disconnected" in text


def test_ai_defaults_are_cpu_friendly_and_timeout_governed():
    settings = AISettings()
    assert settings.num_ctx == 4096
    assert settings.top_k == 12
    assert settings.evidence_limit == 4
    assert settings.max_context_chars == 12000
    assert settings.ollama_connect_timeout == 10.0
    assert settings.ollama_read_timeout == 600.0
    assert settings.ollama_first_load_timeout == 900.0
    assert settings.ollama_stream_idle_timeout == 180.0


def test_qdrant_contract_supports_issue_filters_without_removing_spec_filters():
    assert PAYLOAD_INDEX_CONTRACT["spec_code"] == "keyword"
    assert PAYLOAD_INDEX_CONTRACT["artifact_type"] == "keyword"
    assert PAYLOAD_INDEX_CONTRACT["issue_key"] == "keyword"
    assert PAYLOAD_INDEX_CONTRACT["issue_status"] == "keyword"
    assert PAYLOAD_INDEX_CONTRACT["priority"] == "keyword"


def test_local_model_reasoning_prelude_filter_is_conservative():
    assert OllamaClient._looks_like_reasoning_prelude("首先，用户要求用三句话说明什么是RAG，我需要先整理") is True
    assert OllamaClient._looks_like_reasoning_prelude("首先介绍系统架构，然后说明检索流程。") is False


def test_enterprise_runtime_uses_structured_and_agentic_paths():
    source = (ROOT / "app/ai/rag_service.py").read_text(encoding="utf-8")
    assert 'decision.route == "SPEC_CATALOG"' in source
    assert 'decision.route == "ISSUE_STATS"' in source
    assert 'decision.route == "ISSUE_QUERY"' in source
    assert "self.agent.retrieve(" in source
    assert "thinking=OFF" in source
    assert "ollama_read_timeout" in source


def test_issue_statistics_count_logical_issues_not_chunks():
    source = (ROOT / "app/ai/retrieval.py").read_text(encoding="utf-8")
    assert "COUNT(DISTINCT" in source
    assert "DISTINCT_LOGICAL_ISSUE" in source
    assert "logical_issue_id" in source


def test_xlsx_parser_projects_issue_rows_individually():
    source = (ROOT / "app/parser/xlsx_parser.py").read_text(encoding="utf-8")
    assert "_build_issue_register_contents" in source
    assert "IssueRegisterPolicy.detect_headers" in source
    assert '"artifact_type": IssueRegisterPolicy.ARTIFACT_TYPE' in source


def test_gui_exposes_all_four_ollama_timeout_controls():
    source = (ROOT / "app/gui/ai_workspace.py").read_text(encoding="utf-8")
    assert "Connect Timeout (s)" in source
    assert "Read Timeout (s)" in source
    assert "First Load Timeout (s)" in source
    assert "Stream Idle Timeout (s)" in source
    assert '"qdrant_only_knowledge": False' in source


def test_xlsx_parser_creates_one_rag_unit_per_issue_row():
    from app.model.block import BlockType, DocumentBlock
    from app.model.document import Document
    from app.parser.xlsx_parser import XLSXParser

    rows = [
        ["Issue Key", "Status", "Priority", "Summary", "Root Cause", "Resolution"],
        ["BUG-1", "Open", "P1", "Bluetooth disconnects", "Race condition", ""],
        ["BUG-2", "Closed", "P2", "Audio mute", "Wrong state", "Fixed"],
    ]
    blocks = [
        DocumentBlock(
            order=i,
            block_type=BlockType.TABLE,
            table_index=0,
            row_index=i,
            cells=cells,
            text=" | ".join(cells),
            metadata={"sheet_index": 0, "sheet_name": "QA BUG"},
        )
        for i, cells in enumerate(rows)
    ]
    document = Document(file_name="jira_bug.xlsx", file_type="xlsx", blocks=blocks, metadata={"project_code": "21MM"})
    parsed = XLSXParser().parse(document)

    assert parsed.metadata["artifact_type"] == "ISSUE_REGISTER"
    assert parsed.metadata["qa_index_policy"] == "INCLUDE"
    assert len(parsed.contents) == 2
    assert parsed.contents[0].metadata["issue_key"] == "BUG-1"
    assert parsed.contents[1].metadata["root_cause"] == "Wrong state"
    assert "Issue Key: BUG-2" in parsed.contents[1].text


def test_follow_up_resolution_carries_only_explicit_recent_entities():
    conversation = [
        {"role": "user", "content": "BUG-123现在是什么状态？"},
        {"role": "assistant", "content": "记录显示为 Open。"},
        {"role": "user", "content": "这个是谁负责的？"},
    ]
    contextual = FastIntentRouter.contextualize("这个是谁负责的？", conversation)
    assert "BUG-123" in contextual
    assert FastIntentRouter.classify(contextual).route == "ISSUE_QUERY"

    spec_conversation = [
        {"role": "user", "content": "2007是什么规格？"},
        {"role": "assistant", "content": "..."},
        {"role": "user", "content": "那它的接口要求呢？"},
    ]
    contextual = FastIntentRouter.contextualize("那它的接口要求呢？", spec_conversation)
    assert "SPEC-2007" in contextual
    assert FastIntentRouter.classify(contextual).route == "ENGINEERING_AGENT"


def test_legacy_ai_settings_are_migrated_to_cpu_safe_defaults_without_overwriting_custom_values(tmp_path):
    import json
    from app.ai.config import AISettingsStore

    path = tmp_path / "ai_config.json"
    path.write_text(json.dumps({
        "num_ctx": 32768,
        "top_k": 8,
        "evidence_limit": 8,
        "max_context_chars": 24000,
        "temperature": 0.37,
        "chat_model": "qwen3:4b-instruct",
    }), encoding="utf-8")
    settings = AISettingsStore(path).load()
    assert settings.settings_schema_version == 2
    assert settings.num_ctx == 4096
    assert settings.top_k == 12
    assert settings.evidence_limit == 4
    assert settings.max_context_chars == 12000
    assert settings.temperature == 0.37
    assert settings.chat_model == "qwen3:4b-instruct"
