from __future__ import annotations

from pathlib import Path

from app.gui.application import DocumentIngestionGUI


class DummyListbox:
    def __init__(self) -> None:
        self.deleted: list[tuple[object, object]] = []

    def delete(self, start: object, end: object) -> None:
        self.deleted.append((start, end))


class DummyVar:
    def __init__(self, value: str = "") -> None:
        self.value = value

    def set(self, value: str) -> None:
        self.value = value


def test_project_switch_clears_pending_document_files() -> None:
    gui = object.__new__(DocumentIngestionGUI)
    gui.selected_files = [Path("a.pdf"), Path("b.docx")]
    gui.file_listbox = DummyListbox()
    gui.file_count_var = DummyVar()
    gui.current_file_var = DummyVar("a.pdf")
    gui.status_var = DummyVar("Ready")

    gui._clear_pending_files_for_project_switch(
        previous_project_code="21MM",
        new_project_code="24MM",
    )

    assert gui.selected_files == []
    assert gui.file_listbox.deleted
    assert gui.file_count_var.value == "0 files selected"
    assert gui.current_file_var.value == "No file selected."
    assert "21MM -> 24MM" in gui.status_var.value
    assert "cleared 2 pending input file(s)" in gui.status_var.value


def test_project_switch_with_no_pending_files_is_noop() -> None:
    gui = object.__new__(DocumentIngestionGUI)
    gui.selected_files = []
    gui.file_listbox = DummyListbox()
    gui.file_count_var = DummyVar("0 files selected")
    gui.current_file_var = DummyVar("No file selected.")
    gui.status_var = DummyVar("Ready")

    gui._clear_pending_files_for_project_switch(
        previous_project_code="24MM",
        new_project_code="21MM",
    )

    assert gui.selected_files == []
    assert gui.file_listbox.deleted == []
    assert gui.file_count_var.value == "0 files selected"
    assert gui.current_file_var.value == "No file selected."
    assert gui.status_var.value == "Ready"


def test_switch_to_common_clears_pending_document_files() -> None:
    gui = object.__new__(DocumentIngestionGUI)
    gui.selected_files = [Path("meeting.pdf")]
    gui.file_listbox = DummyListbox()
    gui.file_count_var = DummyVar()
    gui.current_file_var = DummyVar("meeting.pdf")
    gui.status_var = DummyVar("Ready")

    gui._clear_pending_files_for_project_switch(
        previous_project_code="24MM",
        new_project_code="COMMON",
    )

    assert gui.selected_files == []
    assert gui.file_listbox.deleted
    assert gui.file_count_var.value == "0 files selected"
    assert gui.current_file_var.value == "No file selected."
    assert "24MM -> COMMON" in gui.status_var.value
