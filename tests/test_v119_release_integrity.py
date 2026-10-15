from __future__ import annotations

import tempfile
from pathlib import Path

from scripts.verify_runtime_package import missing_internal_imports
from app.output.project_output_router import ProjectOutputRouter


def test_release_includes_every_absolute_internal_application_import() -> None:
    root = Path(__file__).resolve().parents[1]
    assert missing_internal_imports(root) == []


def test_gui_output_routing_module_is_present() -> None:
    root = Path(__file__).resolve().parents[1]
    assert (root / "app/output/__init__.py").is_file()
    assert (root / "app/output/project_output_router.py").is_file()
    assert (root / "app/gui/application.py").is_file()


def test_guard_detects_absent_modules_before_gui_launch() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "app").mkdir()
        (root / "scripts").mkdir()
        (root / "app" / "entry.py").write_text(
            "from app.output.project_output_router import ProjectOutputRouter\n",encoding="utf-8"
        )
        assert ("app/entry.py", "app.output.project_output_router") in missing_internal_imports(root)
        (root / "app" / "output").mkdir()
        (root / "app" / "output" / "project_output_router.py").write_text(
            "class ProjectOutputRouter: pass\n", encoding="utf-8"
        )
        assert missing_internal_imports(root) == []


def test_json_routing_existing_contract() -> None:
    with tempfile.TemporaryDirectory() as td:
        route = ProjectOutputRouter.resolve_json_output_path(
            output_directory=td, project_code="21MM", file_stem="test", save_database=False
        )
        assert route == Path(td).resolve() / "21MM" / "test.json"
