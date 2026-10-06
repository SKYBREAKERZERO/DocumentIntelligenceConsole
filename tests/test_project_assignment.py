from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from app.analyzer.specification_classifier import SpecificationClassifier
from app.config.config_loader import ConfigLoader
from app.model.document import Document
from app.project.project_registry import ProjectRegistry, ProjectRegistryError


class ProjectRegistryTests(unittest.TestCase):
    def test_resolves_gui_display_values(self) -> None:
        self.assertEqual(ProjectRegistry.resolve("21MM").code, "21MM")
        self.assertEqual(ProjectRegistry.resolve("24MM").code, "24MM")
        self.assertEqual(ProjectRegistry.resolve("Common").code, "COMMON")
        self.assertEqual(ProjectRegistry.resolve("共通").code, "COMMON")

    def test_rejects_missing_project(self) -> None:
        with self.assertRaises(ProjectRegistryError):
            ProjectRegistry.resolve("")

    def test_classifier_preserves_user_selected_project(self) -> None:
        document = Document(
            file_name="meeting_screenshot.png",
            file_type="png",
        )
        classifier = SpecificationClassifier(project_code="24MM")
        result = classifier.process(document)
        self.assertEqual(result.metadata["project_code"], "24MM")
        self.assertEqual(result.metadata["project_name"], "24MM")
        self.assertEqual(
            result.metadata["project_assignment_source"],
            "USER_SELECTED",
        )

    def test_common_mode_skips_specification_taxonomy_identity(self) -> None:
        document = Document(
            file_name="Bluetooth_market_news_2026-09-02.txt",
            file_type="txt",
        )
        classifier = SpecificationClassifier(project_code="COMMON")
        result = classifier.process(document)
        self.assertEqual(result.metadata["project_code"], "COMMON")
        self.assertEqual(result.metadata["project_name"], "Common")
        self.assertEqual(result.metadata["specification_classifier_status"], "NOT_APPLICABLE")
        self.assertIsNone(result.metadata["series"])
        self.assertIsNone(result.metadata["region_scope"])
        self.assertIsNone(result.metadata["spec_type"])
        self.assertIsNone(result.metadata["spec_subtype"])

    def test_config_supports_three_scope_databases(self) -> None:
        config_text = """
application:
  name: test
  environment: test
runtime:
  input_directory: input
  output_directory: output
  log_directory: logs
output:
  save_json: true
database:
  enabled: true
  host: 127.0.0.1
  port: 5432
  database: rag
  project_databases:
    21MM: db21
    24MM: db24
    COMMON: dbcommon
  user: postgres
  password_env: TEST_POSTGRES_PASSWORD
  connect_timeout: 5
chunk:
  max_length: 1000
logging:
  level: INFO
  file_name: test.log
"""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.yaml"
            path.write_text(config_text, encoding="utf-8")
            old = os.environ.get("TEST_POSTGRES_PASSWORD")
            os.environ["TEST_POSTGRES_PASSWORD"] = "secret"
            try:
                config = ConfigLoader.load(path)
            finally:
                if old is None:
                    os.environ.pop("TEST_POSTGRES_PASSWORD", None)
                else:
                    os.environ["TEST_POSTGRES_PASSWORD"] = old

        self.assertEqual(config.database.project_databases["21MM"], "db21")
        self.assertEqual(config.database.project_databases["24MM"], "db24")
        self.assertEqual(config.database.project_databases["COMMON"], "dbcommon")

    def test_common_database_falls_back_to_database_name(self) -> None:
        config_text = """
application:
  name: test
  environment: test
runtime:
  input_directory: input
  output_directory: output
  log_directory: logs
output:
  save_json: true
database:
  enabled: true
  host: 127.0.0.1
  port: 5432
  database: generic_rag
  project_databases:
    21MM: db21
    24MM: db24
  user: postgres
  password_env: TEST_POSTGRES_PASSWORD
  connect_timeout: 5
chunk:
  max_length: 1000
logging:
  level: INFO
  file_name: test.log
"""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.yaml"
            path.write_text(config_text, encoding="utf-8")
            old = os.environ.get("TEST_POSTGRES_PASSWORD")
            os.environ["TEST_POSTGRES_PASSWORD"] = "secret"
            try:
                config = ConfigLoader.load(path)
            finally:
                if old is None:
                    os.environ.pop("TEST_POSTGRES_PASSWORD", None)
                else:
                    os.environ["TEST_POSTGRES_PASSWORD"] = old
        self.assertEqual(config.database.project_databases["COMMON"], "generic_rag")


if __name__ == "__main__":
    unittest.main()
