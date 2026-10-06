from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_single_user_facing_agent_mode_and_audit_ui_present():
    text = (ROOT / "app/gui/ai_workspace.py").read_text(encoding="utf-8")
    assert "AI AUTO · Agentic RAG" in text
    assert "Agent Trace / Audit" in text
    assert "AI Query Audit" in text
    assert "RETRIEVAL_VALUES" not in text
    assert "retrieval_var" not in text


def test_agentic_backend_has_safe_tool_allow_list():
    text = (ROOT / "app/ai/agent.py").read_text(encoding="utf-8")
    assert "ALLOWED_TOOLS" in text
    assert '"semantic_search"' in text
    assert '"sql_exact_search"' in text
    assert '"relation_search"' in text
    assert '"version_search"' in text
    assert '"unresolved_reference_search"' in text
    assert "execute_sql" not in text


def test_version_bumped():
    text = (ROOT / "app/gui/application.py").read_text(encoding="utf-8")
    assert 'APP_VERSION = "5.5.0"' in text
