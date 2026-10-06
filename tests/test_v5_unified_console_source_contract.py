from pathlib import Path


def test_unified_console_source_contract() -> None:
    root = Path(__file__).resolve().parents[1]
    gui = (root / "app" / "gui" / "application.py").read_text(encoding="utf-8")
    module = (root / "app" / "enterprise" / "data_module.py").read_text(encoding="utf-8")

    assert 'APP_NAME = "Document Intelligence Console Enterprise"' in gui
    assert 'APP_VERSION = "5.2.8"' in gui
    assert 'DataConsoleModule' in gui
    assert 'def navigate(' in gui
    for page in (
        '"document_import"',
        '"spec_catalog"',
        '"spec_versions"',
        '"spec_compare"',
        '"spec_changes"',
        '"database"',
        '"sql"',
        '"erd"',
        '"documents"',
        '"embedding"',
        '"qdrant"',
        '"settings"',
    ):
        assert page in gui

    for builder in (
        "_build_database_tab",
        "_build_sql_tab",
        "_build_visual_query_tab",
        "_build_erd_tab",
        "_build_document_tab",
        "_build_qdrant_tab",
        "_build_embedding_tab",
    ):
        assert builder in module

    assert "Specification → Version → Document → Chapter → Section → Chunk" in module
