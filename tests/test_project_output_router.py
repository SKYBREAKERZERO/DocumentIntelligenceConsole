from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.output.project_output_router import ProjectOutputRouter


class ProjectOutputRouterTests(unittest.TestCase):
    def test_json_only_21mm_uses_project_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = ProjectOutputRouter.resolve_json_output_path(
                output_directory=temp_dir,
                project_code="21MM",
                file_stem="spec-a",
                save_database=False,
            )
            self.assertEqual(path, Path(temp_dir).resolve() / "21MM" / "spec-a.json")

    def test_json_only_24mm_uses_project_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = ProjectOutputRouter.resolve_json_output_path(
                output_directory=temp_dir,
                project_code="24MM",
                file_stem="spec-b",
                save_database=False,
            )
            self.assertEqual(path, Path(temp_dir).resolve() / "24MM" / "spec-b.json")

    def test_json_only_common_uses_common_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = ProjectOutputRouter.resolve_json_output_path(
                output_directory=temp_dir,
                project_code="COMMON",
                file_stem="news-a",
                save_database=False,
            )
            self.assertEqual(path, Path(temp_dir).resolve() / "COMMON" / "news-a.json")

    def test_database_enabled_preserves_existing_root_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = ProjectOutputRouter.resolve_json_output_path(
                output_directory=temp_dir,
                project_code="21MM",
                file_stem="spec-c",
                save_database=True,
            )
            self.assertEqual(path, Path(temp_dir).resolve() / "spec-c.json")

    def test_ensure_json_only_directory_creates_all_scope_folders(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = ProjectOutputRouter.ensure_json_output_directory(
                output_directory=temp_dir,
                project_code="COMMON",
                save_database=False,
            )
            self.assertTrue(directory.is_dir())
            self.assertEqual(directory.name, "COMMON")
            self.assertTrue((Path(temp_dir).resolve() / "21MM").is_dir())
            self.assertTrue((Path(temp_dir).resolve() / "24MM").is_dir())
            self.assertTrue((Path(temp_dir).resolve() / "COMMON").is_dir())


if __name__ == "__main__":
    unittest.main()
