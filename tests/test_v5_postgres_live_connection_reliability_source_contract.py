from pathlib import Path


def test_v5_postgres_live_connection_reliability_source_contract() -> None:
    root = Path(__file__).resolve().parents[1]
    gui = (root / "app" / "gui" / "application.py").read_text(encoding="utf-8")
    module = (root / "app" / "enterprise" / "data_module.py").read_text(encoding="utf-8")
    pg = (root / "app" / "enterprise" / "postgres_service.py").read_text(encoding="utf-8")

    assert 'APP_VERSION = "5.2.8"' in gui
    assert "self.enterprise_module._rebuild_services()" in gui
    assert "self.enterprise_module.on_connection_verified()" in gui

    dashboard_start = module.index("def dashboard_snapshot")
    dashboard_end = module.index("def shutdown", dashboard_start)
    dashboard_body = module[dashboard_start:dashboard_end]
    assert "self._rebuild_services()" not in dashboard_body

    assert "def on_connection_verified" in module
    assert 'snapshot = service.metadata_snapshot()' in module
    assert 'self._loaded_context.pop("visual", None)' in module
    assert "finally:" in module[module.index("def _poll_events"):module.index("def _bind_shortcuts")]
    assert "Enterprise UI callback failed" in module

    assert "def metadata_snapshot" in pg
    assert "one PostgreSQL connection" in pg
    assert "def schema_erd_model" in pg
    assert "PRIMARY KEY" in pg
    assert "FOREIGN KEY" in pg
