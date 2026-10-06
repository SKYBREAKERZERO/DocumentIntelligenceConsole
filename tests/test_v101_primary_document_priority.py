from pathlib import Path

from app.storage.primary_document_policy import PrimarySpecificationDocumentPolicy


ROOT = Path(__file__).resolve().parents[1]


def test_primary_body_outranks_cover_sheet_even_when_legacy_metadata_says_primary():
    body = PrimarySpecificationDocumentPolicy.role_rank(
        file_name="550_Bluetooth_Audio_Function_Spec_v2.50.pdf",
        metadata={"document_role": "PRIMARY"},
    )
    cover = PrimarySpecificationDocumentPolicy.role_rank(
        file_name="550_Bluetooth_Audio_Function_Spec_Cover_Sheet_v2.50.pdf",
        metadata={"document_role": "PRIMARY"},
    )
    assert body == 100
    assert cover < body
    assert PrimarySpecificationDocumentPolicy.is_primary(
        file_name="550_Bluetooth_Audio_Function_Spec_v2.50.pdf",
        metadata={},
    )
    assert not PrimarySpecificationDocumentPolicy.is_primary(
        file_name="550_Bluetooth_Audio_Function_Spec_Cover_Sheet_v2.50.pdf",
        metadata={},
    )


def test_supporting_roles_are_never_primary_content():
    for role in ("SUPPORTING", "METADATA_ONLY", "TRANSLATION", "ISSUE_REGISTER"):
        assert not PrimarySpecificationDocumentPolicy.is_primary(
            file_name="550_spec.pdf", metadata={"document_role": role}
        )


def test_retrieval_and_version_compare_use_same_primary_policy():
    retrieval = (ROOT / "app/ai/retrieval.py").read_text(encoding="utf-8")
    history = (ROOT / "app/storage/specification_history_service.py").read_text(encoding="utf-8")
    changes = (ROOT / "app/storage/specification_change_manager.py").read_text(encoding="utf-8")
    assert "PrimarySpecificationDocumentPolicy" in retrieval
    assert "PRIMARY_CONTENT" in retrieval
    assert "PrimarySpecificationDocumentPolicy" in history
    assert "PrimarySpecificationDocumentPolicy" in changes
    assert "Version comparison requires specification body documents" in changes


def test_relation_and_qdrant_paths_reject_cover_sheet_as_authoritative_source():
    retrieval = (ROOT / "app/ai/retrieval.py").read_text(encoding="utf-8")
    qdrant = (ROOT / "app/rag/qdrant_sync.py").read_text(encoding="utf-8")
    assert "preferred PRIMARY specification body" in retrieval
    assert "latest READY extraction for that body" in retrieval
    assert "PrimarySpecificationDocumentPolicy.sql_primary_predicate" in qdrant


def test_schema_read_models_use_preferred_primary_version():
    schema = (ROOT / "app/storage/schema_manager.py").read_text(encoding="utf-8")
    assert "preferred PRIMARY" not in schema or "primary-document policy" in schema
    assert "PrimarySpecificationDocumentPolicy.sql_primary_predicate" in schema
    assert "CASE WHEN sv.id = sp.current_version_id THEN 0 ELSE 1 END" in schema
    assert "document_role" in schema
    assert "supporting_document_type" in schema


def test_realistic_550_cover_sheet_name_is_never_primary():
    assert not PrimarySpecificationDocumentPolicy.is_primary(
        file_name="550 Bluetooth Audio Function Spec Cover Sheet 改(電子承認).pdf",
        metadata={"document_role": "PRIMARY"},
    )


def test_v101_repairs_legacy_current_and_builds_body_only_history():
    schema = (ROOT / "app/storage/schema_manager.py").read_text(encoding="utf-8")
    versions = (ROOT / "app/storage/specification_version_manager.py").read_text(encoding="utf-8")
    assert "_repair_primary_specification_current_pointers" in schema
    assert "current_is_primary" in schema
    assert "primary_previous_version_id" in schema
    assert "never become immutable business versions in v1.01" in schema
    assert "_preferred_primary_current_version_id" in versions
    assert 'sql_primary_predicate("neighbor_doc")' in versions


def test_v101_qdrant_overfetches_before_rejecting_legacy_cover_points():
    retrieval = (ROOT / "app/ai/retrieval.py").read_text(encoding="utf-8")
    sync = (ROOT / "app/rag/qdrant_sync.py").read_text(encoding="utf-8")
    assert "candidate_limit = self.top_k if scope == \"QABUG\" else max(self.top_k * 4, 32)" in retrieval
    assert 'FieldCondition(key="document_role", match=MatchValue(value="SUPPORTING"))' in retrieval
    assert '"evidence_role": "PRIMARY_CONTENT"' in sync
    assert '"qa_index_policy": "INCLUDE"' in sync
