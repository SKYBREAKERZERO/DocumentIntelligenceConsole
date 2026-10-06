"""RAG package public API with lazy imports.

Heavy runtime dependencies (psycopg, qdrant-client, embedding libraries) are
loaded only when the corresponding service is actually requested. This keeps
lightweight governance/configuration modules importable in diagnostics and
packaging environments.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "CurrentQdrantSyncService",
    "QdrantSyncCancelled",
    "QdrantSyncError",
    "QdrantSyncPlan",
    "QdrantSyncResult",
]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from app.rag import qdrant_sync

        return getattr(qdrant_sync, name)
    raise AttributeError(name)
