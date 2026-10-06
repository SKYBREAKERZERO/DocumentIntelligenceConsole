from pathlib import Path


def test_sync_auto_ensures_payload_index_contract():
    source = Path("app/rag/qdrant_sync.py").read_text(encoding="utf-8")
    assert "PAYLOAD_INDEX_CONTRACT" in source
    assert "ensure_payload_index_contract" in source
    assert source.count("self.ensure_payload_index_contract()") >= 2


def test_enterprise_ui_exposes_data_quality_and_repair_actions():
    source = Path("app/enterprise/data_module.py").read_text(encoding="utf-8")
    assert "Qdrant Governance & Data Quality" in source
    assert "Validate Data Quality" in source
    assert "Repair Payload Indexes" in source
    assert "validate_qdrant_quality" in source


def test_ai_health_integrates_governance_validation():
    source = Path("app/gui/ai_workspace.py").read_text(encoding="utf-8")
    assert "QdrantDataQualityValidator" in source
    assert "Governance {scope}" in source
    assert "payload index contract is incomplete" in source


def test_agentic_semantic_retrieval_uses_selective_filter_and_stale_hash_guard():
    source = Path("app/ai/retrieval.py").read_text(encoding="utf-8")
    assert 'FieldCondition(key="spec_code"' in source
    assert "semantic_query_hits" in source
    assert "Skipping stale Qdrant hit" in source
