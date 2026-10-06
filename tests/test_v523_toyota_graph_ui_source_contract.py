from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_toyota_graph_defaults_to_include_unresolved_targets():
    source = (ROOT / "app" / "gui" / "application.py").read_text(encoding="utf-8")
    assert 'APP_VERSION = "5.2.8"' in source
    assert 'relation_graph_include_unresolved_var = tk.BooleanVar(value=True)' in source
