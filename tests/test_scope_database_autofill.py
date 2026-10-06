from __future__ import annotations

from app.gui.application import DocumentIngestionGUI


class DummyVar:
    def __init__(self, value: str = "") -> None:
        self.value = value

    def get(self) -> str:
        return self.value

    def set(self, value: str) -> None:
        self.value = value


class DummyButton:
    def configure(self, **kwargs: object) -> None:
        pass


def _gui_for(scope: str, database: str = "stale_database") -> DocumentIngestionGUI:
    gui = object.__new__(DocumentIngestionGUI)
    gui.project_var = DummyVar(scope)
    gui.project_target_database_var = DummyVar()
    gui.postgres_database_var = DummyVar(database)
    gui._project_database_names = {
        "21MM": "legacy_21",
        "24MM": "legacy_24",
        "COMMON": "legacy_common",
    }
    gui._last_selected_project_code = None
    gui.postgres_connection_verified = True
    gui.postgres_create_tables_button = DummyButton()
    gui.selected_files = []
    return gui


def test_21mm_auto_fills_rag_21mm() -> None:
    gui = _gui_for("21MM")
    gui._on_project_selected()
    assert gui.postgres_database_var.get() == "rag_21mm"
    assert gui._project_database_names["21MM"] == "rag_21mm"
    assert gui.project_target_database_var.get() == "Target PostgreSQL: rag_21mm"


def test_24mm_auto_fills_rag_24mm() -> None:
    gui = _gui_for("24MM")
    gui._on_project_selected()
    assert gui.postgres_database_var.get() == "rag_24mm"
    assert gui._project_database_names["24MM"] == "rag_24mm"


def test_common_auto_fills_rag() -> None:
    gui = _gui_for("Common")
    gui._on_project_selected()
    assert gui.postgres_database_var.get() == "rag"
    assert gui._project_database_names["COMMON"] == "rag"
