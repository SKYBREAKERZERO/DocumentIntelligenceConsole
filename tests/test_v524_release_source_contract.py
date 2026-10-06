from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_v524_precision_release_contract():
    gui = (ROOT / "app" / "gui" / "application.py").read_text(encoding="utf-8")
    extractor = (ROOT / "app" / "relations" / "extractor.py").read_text(encoding="utf-8")
    manager = (ROOT / "app" / "relations" / "manager.py").read_text(encoding="utf-8")
    graph = (ROOT / "app" / "relations" / "graph_service.py").read_text(encoding="utf-8")

    assert 'APP_VERSION = "5.2.8"' in gui
    assert 'value="SPEC REFERENCES"' in gui
    assert 'value="Auto (recommended)"' in gui
    assert 'text="Audit candidates"' in gui
    assert '"engineering-relations-v5-toyota-dependency-graph"' in extractor
    assert "TOYOTA_BRACKET_LIST_REFERENCE" in extractor
    assert "_is_plausible_spec_code" in extractor
    assert "_coalesce_title_candidates" in extractor
    assert "self.reconcile_unresolved(cur=cur, project_code=source.project_code)" in manager
    assert "hidden_title_candidates" in graph
    assert "semantic_type" in graph
