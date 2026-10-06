from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ChatSessionRepository:
    """Small local audit store for desktop chat history.

    The engineering source of truth remains PostgreSQL. This SQLite file only
    stores local chat sessions/messages so AI workspace history survives restarts.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _initialize(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    scope TEXT NOT NULL DEFAULT 'AUTO',
                    model TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_ai_messages_session_id
                    ON messages(session_id, id);
                CREATE INDEX IF NOT EXISTS idx_ai_sessions_updated_at
                    ON sessions(updated_at DESC);
                """
            )

    def create_session(self, *, title: str = "New conversation", scope: str = "AUTO", model: str = "") -> str:
        session_id = uuid.uuid4().hex
        now = _now()
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO sessions(id,title,scope,model,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (session_id, title, scope, model, now, now),
            )
        return session_id

    def list_sessions(self, search: str = "", limit: int = 200) -> list[dict[str, Any]]:
        like = f"%{search.strip()}%"
        with self._lock, self._connect() as conn:
            if search.strip():
                rows = conn.execute(
                    "SELECT * FROM sessions WHERE title LIKE ? ORDER BY updated_at DESC LIMIT ?",
                    (like, max(1, min(limit, 1000))),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM sessions ORDER BY updated_at DESC LIMIT ?",
                    (max(1, min(limit, 1000)),),
                ).fetchall()
        return [dict(row) for row in rows]

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        return dict(row) if row else None

    def get_messages(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT role,content,metadata_json,created_at FROM messages WHERE session_id=? ORDER BY id",
                (session_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
            except json.JSONDecodeError:
                item["metadata"] = {}
            result.append(item)
        return result

    def append_message(self, session_id: str, role: str, content: str, metadata: dict[str, Any] | None = None) -> None:
        now = _now()
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO messages(session_id,role,content,metadata_json,created_at) VALUES(?,?,?,?,?)",
                (session_id, role, content, json.dumps(metadata or {}, ensure_ascii=False, default=str), now),
            )
            conn.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now, session_id))

    def update_session(self, session_id: str, *, title: str | None = None, scope: str | None = None, model: str | None = None) -> None:
        fields: list[str] = []
        params: list[Any] = []
        for name, value in (("title", title), ("scope", scope), ("model", model)):
            if value is not None:
                fields.append(f"{name}=?")
                params.append(value)
        if not fields:
            return
        fields.append("updated_at=?")
        params.append(_now())
        params.append(session_id)
        with self._lock, self._connect() as conn:
            conn.execute(f"UPDATE sessions SET {', '.join(fields)} WHERE id=?", params)

    def delete_session(self, session_id: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
            conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
