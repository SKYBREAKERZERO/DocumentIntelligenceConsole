from pathlib import Path


def test_v51_specification_management_center_contract() -> None:
    root = Path(__file__).resolve().parents[1]
    gui = (root / "app" / "gui" / "application.py").read_text(encoding="utf-8")
    history = (root / "app" / "storage" / "specification_history_service.py").read_text(encoding="utf-8")

    for builder in (
        "_build_specification_catalog_page",
        "_build_specification_versions_page",
        "_build_specification_compare_page",
        "_build_specification_changes_page",
    ):
        assert f"def {builder}" in gui

    assert "def _sync_sidebar_selection_for_page" in gui
    assert "self._nav_buttons" in gui
    assert '("spec_catalog", "Specification Catalog")' in gui
    assert '("spec_versions", "Version History")' in gui
    assert '("spec_compare", "Version Compare")' in gui
    assert '("spec_changes", "Change Center")' in gui
    assert "def _apply_spec_catalog_filters" in gui
    assert "def refresh_change_timeline" in gui
    assert "def _apply_change_detail_filters" in gui
    assert 'VERSION_CONFLICT' in gui
    assert "def list_change_timeline" in history
    assert "def list_change_details" in history
    assert "cs.is_adjacent IS TRUE" in history
