from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_v528_gui_contract() -> None:
    source = (ROOT / "app/gui/application.py").read_text(encoding="utf-8")
    assert 'APP_VERSION = "5.2.8"' in source
    assert 'text="Manual Relation Editor"' in source
    assert 'text="Add Relation"' in source
    assert 'text="Reset Layout"' in source
    assert '_on_relation_graph_node_drag' in source
    assert '_relation_graph_manual_positions' in source
    assert 'Double-click: make this node the graph root + fit' in source


def test_v528_manual_relation_service_contract() -> None:
    source = (ROOT / "app/relations/audit_service.py").read_text(encoding="utf-8")
    assert 'MANUAL_EXTRACTION_METHOD = "MANUAL_EDITOR"' in source
    assert 'def create_manual_relation' in source
    assert 'def update_manual_relation' in source
    assert 'def delete_manual_relation' in source
    assert 'manual_spec_relation_audit' in source
    assert 'Evidence / engineering decision text is required' in source


def test_v528_sql_contract() -> None:
    sql = (ROOT / "database/upgrade_manual_spec_relations_v6_5.sql").read_text(encoding="utf-8")
    assert 'CREATE TABLE IF NOT EXISTS public.manual_spec_relations' in sql
    assert 'CREATE TABLE IF NOT EXISTS public.manual_spec_relation_audit' in sql
    assert 'uq_manual_spec_relation_active_identity' in sql
    assert 'document_ingestion_manual_spec_relations' in sql
