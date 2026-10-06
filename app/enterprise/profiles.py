from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PostgresProfile:
    host: str = "127.0.0.1"
    port: int = 5432
    database: str = "rag"
    user: str = "postgres"
    connect_timeout: int = 10
    sslmode: str = "prefer"

    def dsn_kwargs(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "dbname": self.database,
            "user": self.user,
            "connect_timeout": self.connect_timeout,
            "sslmode": self.sslmode,
        }


@dataclass(frozen=True)
class QdrantProfile:
    url: str = "http://127.0.0.1:6333"
    timeout: float = 10.0


@dataclass(frozen=True)
class ConsoleProfile:
    postgres: PostgresProfile
    qdrant: QdrantProfile
    page_size: int = 100
    max_preview_chars: int = 12000
