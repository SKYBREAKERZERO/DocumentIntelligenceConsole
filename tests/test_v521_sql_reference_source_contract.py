from pathlib import Path

from app.analyzer.specification_version_resolver import SpecificationVersionResolver
from app.relations.extractor import EXTRACTOR_VERSION


ROOT = Path(__file__).resolve().parents[1]


def test_v521_sql_reference_release_contract():
    assert EXTRACTOR_VERSION == "engineering-relations-v5-toyota-dependency-graph"
    policy = (ROOT / "config" / "engineering_relation_rules.yaml").read_text(encoding="utf-8")
    assert "qdrant_reference_resolution_enabled: false" in policy
    assert "exact_registry_first: true" in policy
    assert "REFERENCE_SECTION_CODE" in policy
    assert "EXACT_QUOTED_SPEC_TITLE" in policy


def test_v621_sql_reference_upgrade_contract():
    sql = (ROOT / "database" / "upgrade_spec_reference_sql_v6_2.sql").read_text(encoding="utf-8")
    assert "normalize_spec_reference_code" in sql
    assert "normalize_spec_reference_title" in sql
    assert "spec_reference_registry" in sql
    assert "spec_reference_current_edges" in sql
    assert "spec_reference_unresolved_current" in sql
    assert "DROP TABLE" not in sql.upper()
    assert "TRUNCATE" not in sql.upper()


def test_sample_spec_filenames_resolve_expected_codes_and_versions():
    assert SpecificationVersionResolver._extract_spec_code(
        "1001_User_Profile_Main_Function_Spec_Rev1.0.docx"
    ) == "1001"
    assert SpecificationVersionResolver._extract_version(
        "1001_User_Profile_Main_Function_Spec_Rev1.0.docx"
    ) == "Rev1.0"
    assert SpecificationVersionResolver._extract_spec_code(
        "2007_User_Profile_Communication_Interface_Spec_Rev2.1.docx"
    ) == "2007"
    assert SpecificationVersionResolver._extract_version(
        "2007_User_Profile_Communication_Interface_Spec_Rev2.1.docx"
    ) == "Rev2.1"
