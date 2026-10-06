from pathlib import Path


def test_v522_relationship_graph_and_scroll_contract():
    root = Path(__file__).resolve().parents[1]
    gui = (root / "app" / "gui" / "application.py").read_text(encoding="utf-8")
    audit = (root / "app" / "relations" / "audit_service.py").read_text(encoding="utf-8")
    graph = (root / "app" / "relations" / "graph_service.py").read_text(encoding="utf-8")

    assert 'APP_VERSION = "5.2.8"' in gui
    assert "_create_scrollable_page" in gui
    assert "_page_scroll_canvases" in gui
    assert 'text="Relationship Graph"' in gui
    assert "EngineeringRelationGraphService" in gui
    assert "Include unresolved" in gui
    assert "Canonical Chunk" in gui
    assert "Chunk / Evidence" in gui
    assert "mapped_chunk_content" in audit
    assert "mapping_status" in audit
    assert "class EngineeringRelationGraphService" in graph
    assert "max_nodes" in graph
    assert "max_edges" in graph
