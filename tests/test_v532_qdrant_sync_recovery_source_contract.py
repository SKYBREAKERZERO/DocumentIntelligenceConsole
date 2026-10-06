from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_v532_service_recovers_interrupted_runs_and_persists_live_progress():
    source = (ROOT / "app/rag/qdrant_sync.py").read_text(encoding="utf-8")
    for token in (
        "recover_interrupted_runs",
        "_recover_interrupted_runs_locked",
        "status = 'INTERRUPTED'",
        "_update_run_progress",
        "qdrant_live_count",
        "Resume detected",
    ):
        assert token in source


def test_v532_ui_updates_plan_cards_and_live_history_during_sync():
    source = (ROOT / "app/enterprise/data_module.py").read_text(encoding="utf-8")
    for token in (
        "_apply_rag_sync_plan_payload",
        "_upsert_rag_sync_history_live",
        "Qdrant Before",
        "Qdrant Live",
        "Qdrant Final",
        "INTERRUPTED",
    ):
        assert token in source
