from __future__ import annotations

from app.gui.application import DocumentIngestionGUI


def test_default_postgres_profile_uses_single_default_name_per_scope() -> None:
    profile = DocumentIngestionGUI._default_postgres_profile()
    assert profile["database"] == "rag"
    assert profile["project_databases"] == {
        "21MM": "rag_21mm",
        "24MM": "rag_24mm",
        "COMMON": "rag",
        "QABUG": "rag_qabug",
    }
