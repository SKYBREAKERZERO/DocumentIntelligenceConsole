from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from app.ai.ollama_client import OllamaClient
from app.ai.rag_service import EnterpriseRAGService
from app.server.configuration import load_profiles, load_settings


class PersistentRAGRuntime:
    """Long-lived per-process RAG engine: refreshing the UI does not reload BGE-M3."""

    def __init__(self, config_dir: Path):
        self.config_dir = Path(config_dir)
        self.settings = load_settings(self.config_dir)
        self.profiles, self.password, self.qdrant, self.qdrant_key = load_profiles(self.config_dir)
        self._instance: EnterpriseRAGService | None = None
        self._lock = threading.RLock()
        self._warmup_state = "NOT_STARTED"
        self._warmup_error = ""

    @property
    def preferred_model(self) -> str:
        return self.settings.chat_model.strip()

    def get_service(self) -> EnterpriseRAGService:
        with self._lock:
            if self._instance is None:
                self._instance = EnterpriseRAGService(
                    settings=self.settings,
                    postgres_profiles=self.profiles,
                    postgres_password=self.password,
                    qdrant_profile=self.qdrant,
                    qdrant_api_key=self.qdrant_key,
                )
            return self._instance

    def warmup_async(self) -> None:
        if self._warmup_state != "NOT_STARTED":
            return
        self._warmup_state = "WARMING"

        def work() -> None:
            try:
                self.get_service().warmup()
                self._warmup_state = "READY"
            except Exception as exc:
                self._warmup_error = type(exc).__name__
                self._warmup_state = "DEGRADED"

        threading.Thread(target=work, name="dic-local-bge-warmup", daemon=True).start()

    def local_status(self) -> dict[str, Any]:
        return {
            "service": "DocumentIntelligenceConsole",
            "mode": "LOCAL_ONLY",
            "embedding_model": self.settings.embedding_model,
            "vector_dimension": self.settings.vector_dimension,
            "preferred_model": self.preferred_model or "NOT_CONFIGURED",
            "rag_loaded": self._instance is not None,
            "embedding": self._warmup_state,
            "embedding_error_type": self._warmup_error,
            "collections": self.settings.collections,
            "configured_scopes": sorted(self.profiles),
        }

    def probe_dependencies(self) -> dict[str, Any]:
        """Live probe on explicit request; 2s deadlines; no source document reads."""
        from app.ai.local_webui import local_service_status
        states = local_service_status(
            webui_url=self.settings.external_webui_url,
            api_port=self.settings.api_port,
            ollama_url=self.settings.ollama_url,
            qdrant_url=self.qdrant.url,
        )
        try:
            import psycopg
            profile = self.profiles["COMMON"]
            with psycopg.connect(**profile.dsn_kwargs(), password=self.password, connect_timeout=2) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    states["PostgreSQL"] = "ONLINE" if cur.fetchone()[0] == 1 else "ERROR"
        except Exception:
            states["PostgreSQL"] = "OFFLINE"
        return {"mode": "LOCAL_ONLY", "dependencies": states, "runtime": self.local_status()}

    def probe_internal_knowledge(self) -> dict[str, Any]:
        """Authenticated read-only live probe: never invent corpus availability.

        This is intentionally separate from /ready. A configured collection,
        a reachable empty collection, an absent collection, and an unreachable
        Qdrant process are four different states.
        """
        import json
        from urllib.error import HTTPError, URLError
        from urllib.parse import quote
        from urllib.request import Request, build_opener, ProxyHandler

        report: dict[str, Any] = {
            "policy": "INTERNAL_ONLY",
            "read_only": True,
            "scopes": {},
            "summary": "CHECK_REQUIRED",
            "model_route": "document-intelligence-rag",
        }
        # Ignore HTTP_PROXY/HTTPS_PROXY so a localhost knowledge probe cannot
        # be redirected to an external corporate/public proxy.
        local_http = build_opener(ProxyHandler({}))
        try:
            import psycopg
        except ImportError:
            psycopg = None
        for scope in ("21MM", "24MM", "COMMON", "QABUG"):
            db_status = "NOT_CONFIGURED"
            pg = self.profiles.get(scope)
            if pg:
                if psycopg is None:
                    db_status = "DRIVER_MISSING"
                else:
                    try:
                        with psycopg.connect(
                            **pg.dsn_kwargs(), password=self.password, connect_timeout=2
                        ) as conn:
                            with conn.cursor() as cur:
                                cur.execute("BEGIN READ ONLY")
                                cur.execute("SELECT 1")
                                ok = (cur.fetchone() or (None,))[0] == 1
                                db_status = "REACHABLE" if ok else "ERROR"
                            conn.rollback()
                    except Exception:
                        db_status = "UNREACHABLE"
            collection = self.settings.collections.get(scope, "")
            qdrant_status = "NOT_CONFIGURED"
            points_count: int | None = None
            if collection:
                # load_profiles() guarantees the origin is 127.0.0.1/::1 only.
                origin = self.qdrant.url.rstrip("/")
                path = quote(collection, safe="")
                req = Request(f"{origin}/collections/{path}", method="GET")
                if self.qdrant_key:
                    req.add_header("api-key", self.qdrant_key)
                try:
                    with local_http.open(req, timeout=2.0) as response:
                        data = json.load(response)
                    info = data.get("result") or {}
                    points = info.get("points_count")
                    if isinstance(points, int) and points >= 0:
                        points_count = points
                        qdrant_status = "EMPTY" if points == 0 else "AVAILABLE"
                    else:
                        qdrant_status = "REACHABLE_COUNT_UNKNOWN"
                except HTTPError as exc:
                    qdrant_status = "COLLECTION_MISSING" if exc.code == 404 else "UNAVAILABLE"
                except (URLError, TimeoutError, OSError, ValueError):
                    qdrant_status = "UNREACHABLE"
            report["scopes"][scope] = {
                "postgres": db_status,
                "qdrant": qdrant_status,
                "qdrant_points": points_count,
            }
        statuses = list(report["scopes"].values())
        report["summary"] = (
            "READY" if all(x["postgres"] == "REACHABLE" and x["qdrant"] == "AVAILABLE" for x in statuses)
            else "DEGRADED"
        )
        return report

    def local_model_names(self) -> list[str]:
        return [x.name for x in OllamaClient(
            self.settings.ollama_url,
            connect_timeout=min(3, self.settings.ollama_connect_timeout),
            read_timeout=5,
            first_load_timeout=10,
        ).list_models()]
