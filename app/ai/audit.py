from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class AIQueryAuditRepository:
    """Local append-only operational audit for AI executions.

    It stores metadata, plan/trace, evidence identifiers and timings. Full user
    prompts/answers are intentionally excluded by default; chat history remains
    in the separate local session repository.
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
                CREATE TABLE IF NOT EXISTS ai_query_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL,
                    session_id TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT '',
                    scope TEXT NOT NULL DEFAULT 'AUTO',
                    resolved_scopes_json TEXT NOT NULL DEFAULT '[]',
                    plan_source TEXT NOT NULL DEFAULT '',
                    tools_json TEXT NOT NULL DEFAULT '[]',
                    intents_json TEXT NOT NULL DEFAULT '[]',
                    entities_json TEXT NOT NULL DEFAULT '[]',
                    evidence_ids_json TEXT NOT NULL DEFAULT '[]',
                    evidence_strength TEXT NOT NULL DEFAULT 'NONE',
                    planner_ms INTEGER NOT NULL DEFAULT 0,
                    semantic_ms INTEGER NOT NULL DEFAULT 0,
                    sql_ms INTEGER NOT NULL DEFAULT 0,
                    relation_ms INTEGER NOT NULL DEFAULT 0,
                    version_ms INTEGER NOT NULL DEFAULT 0,
                    total_ms INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'OK',
                    warning_count INTEGER NOT NULL DEFAULT 0,
                    trace_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_ai_query_audit_created_at ON ai_query_audit(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_ai_query_audit_request_id ON ai_query_audit(request_id);
                """
            )

    def append(self, *, session_id: str, model: str, scope: str, retrieval: Any, total_ms: int, status: str = "OK") -> None:
        evidence_ids = [
            {"evidence_id": x.evidence_id, "scope": x.scope, "type": x.source_type, "content_id": x.content_id, "relation_id": x.relation_id}
            for x in retrieval.evidence
        ]
        values = (
            str(retrieval.request_id or ""), str(session_id or ""), str(model or ""), str(scope or "AUTO"),
            json.dumps(list(retrieval.resolved_scopes), ensure_ascii=False), str(retrieval.plan_source or ""),
            json.dumps(list(retrieval.tools), ensure_ascii=False), json.dumps(list(retrieval.intents), ensure_ascii=False),
            json.dumps(list(retrieval.entities), ensure_ascii=False), json.dumps(evidence_ids, ensure_ascii=False),
            str(retrieval.evidence_strength or "NONE"), int(retrieval.planner_ms or 0), int(retrieval.semantic_ms or 0),
            int(retrieval.sql_ms or 0), int(retrieval.relation_ms or 0), int(retrieval.version_ms or 0), int(total_ms or 0),
            str(status or "OK"), len(retrieval.warnings), json.dumps(list(retrieval.trace), ensure_ascii=False), _now(),
        )
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO ai_query_audit(
                    request_id,session_id,model,scope,resolved_scopes_json,plan_source,tools_json,intents_json,entities_json,
                    evidence_ids_json,evidence_strength,planner_ms,semantic_ms,sql_ms,relation_ms,version_ms,total_ms,status,
                    warning_count,trace_json,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, values,
            )

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT * FROM ai_query_audit ORDER BY id DESC LIMIT ?", (max(1, min(limit, 1000)),)).fetchall()
        return [dict(x) for x in rows]
