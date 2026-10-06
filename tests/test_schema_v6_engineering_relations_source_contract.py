from pathlib import Path


def test_schema_v6_contains_engineering_relation_objects():
    source = (Path(__file__).parents[1] / "app" / "storage" / "schema_manager.py").read_text(encoding="utf-8")
    assert "RAG_SCHEMA_VERSION = 6" in source
    for name in (
        "relation_extraction_runs",
        "engineering_entities",
        "spec_relations",
        "relation_evidence",
        "engineering_entity_catalog",
        "engineering_relation_catalog",
        "spec_current_relations",
        "relation_evidence_detail",
        "relation_extraction_status",
    ):
        assert name in source


def test_relation_extraction_runs_before_qdrant_indexing_boundary():
    source = (Path(__file__).parents[1] / "app" / "storage" / "postgres_storage.py").read_text(encoding="utf-8")
    assert "_extract_engineering_relations_safe" in source
    assert "EngineeringRelationManager" in source
    assert "engineering_relation_summary" in source
