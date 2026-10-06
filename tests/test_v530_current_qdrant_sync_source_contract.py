from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_v530_gui_registers_current_sync_page():
    source = (ROOT / "app/gui/application.py").read_text(encoding="utf-8")
    assert 'APP_VERSION = "5.3.2"' in source
    assert '"qdrant_sync": "SQL → Qdrant Sync"' in source
    assert '("qdrant_sync", "SQL → Qdrant Sync")' in source


def test_v530_sync_is_current_only_and_content_id_backed():
    source = (ROOT / "app/rag/qdrant_sync.py").read_text(encoding="utf-8")
    assert 'CURRENT_SOURCE_VIEW = "public.rag_current_chunks"' in source
    assert 'WHERE rc.project_code = %s' in source
    assert '"content_id": row.content_id' in source
    assert '"is_current": True' in source
    assert '"index_scope": "CURRENT"' in source


def test_v530_scope_collection_routing_is_explicit():
    source = (ROOT / "app/rag/qdrant_sync.py").read_text(encoding="utf-8")
    assert '"21MM": "rag_21mm_current_chunks"' in source
    assert '"24MM": "rag_24mm_current_chunks"' in source
    assert '"COMMON": "rag_common_current_chunks"' in source


def test_v530_incremental_and_integrity_controls_exist():
    source = (ROOT / "app/rag/qdrant_sync.py").read_text(encoding="utf-8")
    for token in (
        "new_ids",
        "changed_ids",
        "unchanged_ids",
        "stale_ids",
        "vector_delete_queue",
        "pg_try_advisory_lock",
        "qdrant_sync_runs",
        "READY_WITH_DRIFT",
    ):
        assert token in source


def test_v530_sync_ui_exposes_safe_workflow():
    source = (ROOT / "app/enterprise/data_module.py").read_text(encoding="utf-8")
    for label in ("Preview Sync", "Sync Now", "Full Rebuild", "Cancel"):
        assert label in source
    assert "Only public.rag_current_chunks" in source
    assert "Incremental sync embeds only New + Changed" in source


def test_v530_operational_schema_is_additive():
    source = (ROOT / "database/upgrade_qdrant_current_sync_v6_6.sql").read_text(encoding="utf-8")
    assert "ALTER TABLE public.embeddings" in source
    assert "CREATE TABLE IF NOT EXISTS public.qdrant_sync_runs" in source
    assert "document_ingestion_qdrant_current_sync" in source
    assert "DROP TABLE" not in source.upper()
    assert "TRUNCATE" not in source.upper()
