from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "app/gui/application.py").read_text(encoding="utf-8")
AI = (ROOT / "app/gui/ai_workspace.py").read_text(encoding="utf-8")
CONFIG = (ROOT / "app/config/config_loader.py").read_text(encoding="utf-8")


def test_dashboard_removed_from_shell_and_ai_is_start_page():
    page_defs = APP[APP.index("page_defs = {"):APP.index("for key in page_defs")]
    groups = APP[APP.index("groups = ["):APP.index("start_group =", APP.index("groups = ["))]
    build_ui = APP[APP.index("def _build_ui"):APP.index("def _build_unified_header")]
    assert '"dashboard": "Dashboard"' not in page_defs
    assert '("WORKSPACE", [("dashboard", "Dashboard")])' not in groups
    assert 'self._build_dashboard_page(' not in build_ui
    assert 'self.navigate("ai_chat")' in build_ui


def test_ai_header_is_clean_and_enter_sends():
    chat = AI[AI.index("def _build_chat_page"):AI.index("def _build_settings_page")]
    assert "Ollama · PostgreSQL · Qdrant · Engineering Evidence" not in chat
    assert 'text="Agent Engine"' not in chat
    assert 'textvariable=self.agent_engine_var' not in chat
    assert 'self.composer.bind("<Return>", self._send_shortcut)' in chat
    assert 'Control-Return' not in chat
    assert 'text="发送"' in chat
    assert "Ctrl+Enter" not in chat


def test_qabug_is_unified_import_scope_and_sql_database():
    assert 'display_name="QA/BUG"' in (ROOT / "app/project/project_registry.py").read_text(encoding="utf-8")
    assert '"QABUG": str(' in CONFIG
    assert 'raw_project_databases.get("QABUG", "rag_qabug")' in CONFIG
    assert 'worker = self._process_qabug_files if options.project_code == "QABUG"' in APP
    assert 'def _process_qabug_files(' in APP
    assert 'Save to PostgreSQL + Qdrant' not in APP  # CN runtime label is deliberate
    assert '保存到 PostgreSQL + Qdrant' in APP
    assert '支持 QA/BUG List：XLSX / XLSM / CSV / TSV / TXT' in APP


def test_qabug_selection_available_in_ai():
    scope_block = AI[AI.index("SCOPE_VALUES = ("):AI.index(")", AI.index("SCOPE_VALUES = (")) + 1]
    assert '"QA/BUG"' in scope_block
