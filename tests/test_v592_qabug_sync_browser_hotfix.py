from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_MODULE = (ROOT / "app/enterprise/data_module.py").read_text(encoding="utf-8")
QSYNC = (ROOT / "app/qabug/streaming_sync.py").read_text(encoding="utf-8")
ADAPTER = (ROOT / "app/qabug/sync_adapter.py").read_text(encoding="utf-8")
SCHEMA = (ROOT / "app/qabug/schema_manager.py").read_text(encoding="utf-8")


def test_qabug_document_browser_does_not_require_spec_tables():
    block = DATA_MODULE[DATA_MODULE.index("def refresh_documents"):DATA_MODULE.index("def _show_documents")]
    assert 'project.code == "QABUG"' in block
    assert "public.qabug_records" in block
    assert '"domain": "QABUG"' in block
    # The legacy error remains only in the non-QABUG branch.
    assert block.index('project.code == "QABUG"') < block.index('Required public tables are missing')


def test_qabug_document_tree_loads_qabug_chunks_directly():
    block = DATA_MODULE[DATA_MODULE.index("def _on_doc_tree_open"):DATA_MODULE.index("def _insert_qabug_chunks")]
    assert 'kind == "qabug_record"' in block
    assert "FROM public.qabug_chunks" in block
    assert "service.list_chapters" in block  # specification path still exists


def test_shared_sql_qdrant_sync_routes_qabug_to_dedicated_adapter():
    block = DATA_MODULE[DATA_MODULE.index("def _make_rag_sync_service"):DATA_MODULE.index("def _make_qdrant_quality_validator")]
    assert 'project.code == "QABUG"' in block
    assert "QABugCurrentQdrantSyncService" in block
    assert "CurrentQdrantSyncService" in block
    assert "QABUG_COLLECTION" in block


def test_qabug_sync_adapter_uses_only_qabug_authoritative_tables():
    assert "public.qabug_chunks" in ADAPTER
    assert "public.qabug_records" in ADAPTER
    executable = ADAPTER[ADAPTER.index("def _load_sql_fingerprints"):]
    assert "public.rag_current_chunks" not in executable
    assert "FROM public.documents" not in executable
    assert "FROM public.sections" not in executable
    assert "QABUG_PAYLOAD_INDEX_CONTRACT" in ADAPTER


def test_incremental_qabug_sync_repairs_missing_live_qdrant_points():
    # A SQL row marked COMPLETED must still be re-embedded/upserted if the live
    # Qdrant point is gone, stale, or carries a mismatched fingerprint.
    assert "by_id" in QSYNC
    assert "payload is None" in QSYNC
    assert '("content_hash","index_hash")' in QSYNC
    from app.qabug.streaming_sync import needs_sync,make_payload
    row={"chunk_id":1,"content":"source","content_hash":"a","index_hash":"b",
         "embedding_status":"COMPLETED","embedded_content_hash":"a","embedding_model":"m","embedding_version":"v"}
    payload=make_payload(row,"m","v")
    assert not needs_sync(row,payload,"m","v")
    assert needs_sync(row,None,"m","v")
    for key in ("content_hash","index_hash","content"):
        assert needs_sync(row,{**payload,key:"corrupt"},"m","v")
    assert "all_rows" not in QSYNC
    assert "scroll_all_points" not in QSYNC
    assert "LIMIT %s" in QSYNC


def test_qabug_schema_v2_contains_sync_audit_table():
    assert "QABUG_SCHEMA_VERSION = 2" in SCHEMA
    assert '"qabug_qdrant_sync_runs"' in SCHEMA
    assert "CREATE TABLE IF NOT EXISTS public.qabug_qdrant_sync_runs" in SCHEMA
