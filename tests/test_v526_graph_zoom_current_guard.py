from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_graph_zoom_controls_and_bindings_are_shipped():
    source = (ROOT / "app" / "gui" / "application.py").read_text(encoding="utf-8")
    assert 'APP_VERSION = "5.2.8"' in source
    assert 'text="100%"' in source
    assert 'text="Fit"' in source
    assert '<Control-MouseWheel>' in source
    assert '<ButtonPress-2>' in source
    assert '_fit_relation_graph' in source
    assert 'max_rows_side = 4' in source
    assert 'max_cols_vertical = 4' in source


def test_current_run_guard_is_shipped_in_schema_and_sql():
    schema = (ROOT / "app" / "storage" / "schema_manager.py").read_text(encoding="utf-8")
    manager = (ROOT / "app" / "relations" / "manager.py").read_text(encoding="utf-8")
    upgrade = (ROOT / "database" / "upgrade_relation_current_graph_v6_3.sql").read_text(encoding="utf-8")
    assert 'uq_relation_run_current_specification' in schema
    assert 'PARTITION BY run.specification_id' in schema
    assert 'WHERE specification_id = %s AND id <> %s AND is_current IS TRUE' in manager
    assert 'uq_relation_run_current_specification' in upgrade
    assert 'document_ingestion_relation_current_guard' in upgrade


def test_backfill_defaults_to_current_versions():
    source = (ROOT / "scripts" / "backfill_engineering_relations.py").read_text(encoding="utf-8")
    assert '--all-versions' in source
    assert 'sv.id = sp.current_version_id' in source


def test_graph_service_has_duplicate_edge_defense():
    source = (ROOT / "app" / "relations" / "graph_service.py").read_text(encoding="utf-8")
    assert 'Defensive graph-level de-duplication' in source
    assert 'deduped:' in source
