"""Enterprise QA/BUG RAG domain.

PostgreSQL is the authoritative store. Qdrant is a derived semantic index.
The domain is intentionally isolated from specification repositories.
"""

from .canonical import QABUG_JSON_SCHEMA_VERSION, QABugCanonicalizer
from .schema_manager import QABUG_SCHEMA_VERSION, QABugSchemaManager

__all__ = [
    "QABUG_JSON_SCHEMA_VERSION",
    "QABUG_SCHEMA_VERSION",
    "QABugCanonicalizer",
    "QABugSchemaManager",
]
