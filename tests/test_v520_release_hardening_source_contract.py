from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_extractor_version_change_creates_new_generation_automatically():
    source = (ROOT / "app" / "relations" / "manager.py").read_text(encoding="utf-8")
    assert "existing.extractor_version == EXTRACTOR_VERSION" in source
    assert "creates a new immutable generation" in source


def test_dashboard_does_not_require_relation_view_before_schema_upgrade():
    source = (ROOT / "app" / "enterprise" / "data_module.py").read_text(encoding="utf-8")
    assert "base_counts_sql" in source
    assert "engineering_relation_catalog" in source
    assert "Schema v5 database before the v5.2/v6 migration" in source
