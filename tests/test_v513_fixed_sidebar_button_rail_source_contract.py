from pathlib import Path


def test_sidebar_is_compact_scrollable_tree() -> None:
    root = Path(__file__).resolve().parents[1]
    gui = (root / "app" / "gui" / "application.py").read_text(encoding="utf-8")

    assert 'APP_VERSION = "5.2.8"' in gui
    assert 'width=238' in gui
    assert 'text="NAVIGATION"' in gui
    assert 'style="Sidebar.Treeview"' in gui
    assert 'scrollbar = ttk.Scrollbar(nav_host' in gui
    assert 'def _on_sidebar_tree_selected' in gui
    assert 'def _on_sidebar_group_opened' in gui
    assert 'def _collapse_other_sidebar_groups' in gui
    assert 'def _sync_sidebar_selection_for_page' in gui
    assert '("spec_catalog", "Specification Catalog")' in gui
    assert '("spec_versions", "Version History")' in gui
    assert '("spec_compare", "Version Compare")' in gui
    assert '("spec_changes", "Change Center")' in gui

    nav_start = gui.index('def _build_sidebar')
    nav_end = gui.index('def navigate', nav_start)
    nav = gui[nav_start:nav_end]

    # Navigation is one stable widget. Groups may collapse internally, but the
    # sidebar itself is never rebuilt or geometry-forgotten.
    assert 'ttk.Treeview(' in nav
    assert '.pack_forget()' not in nav
    assert '.grid_remove()' not in nav
    assert '.grid_forget()' not in nav
    assert 'ttk.Button(' not in nav
