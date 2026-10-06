from pathlib import Path


def test_live_data_pages_auto_load_source_contract() -> None:
    root = Path(__file__).resolve().parents[1]
    gui = (root / "app" / "gui" / "application.py").read_text(encoding="utf-8")
    module = (root / "app" / "enterprise" / "data_module.py").read_text(encoding="utf-8")

    assert "self.enterprise_module.activate_page(target)" in gui
    assert 'if page_key == "database":' in module
    assert 'self.refresh_database_tree()' in module
    assert 'elif page_key == "erd":' in module
    assert 'self.load_erd()' in module
    assert 'Connected: {database_name}' in module
    assert 'service.test_connection()' in module
    assert 'self._loaded_context.clear()' in module
