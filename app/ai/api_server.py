from __future__ import annotations

import json
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from app.ai.rag_service import EnterpriseRAGService


class OpenAICompatibleServer:
    """Small dependency-free OpenAI-compatible gateway for Open WebUI.

    It intentionally exposes only the endpoints needed by a local Open WebUI
    integration: health, models and chat completions.
    """

    def __init__(
        self,
        host: str,
        port: int,
        api_key: str,
        service_factory: Callable[[], EnterpriseRAGService],
        model_provider: Callable[[], list[str]],
        current_scope_provider: Callable[[], str],
    ) -> None:
        self.host = host
        self.port = int(port)
        self.api_key = str(api_key or "")
        if self.host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Embedded API must bind to loopback")
        if len(self.api_key) < 32 or self.api_key == "local-rag-key":
            raise ValueError("Embedded API requires a strong per-install token")
        self.service_factory = service_factory
        self.model_provider = model_provider
        self.current_scope_provider = current_scope_provider
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and self._server)

    def start(self) -> None:
        if self.running:
            return
        outer = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "DocumentIntelligenceRAG/1.04"

            def log_message(self, fmt: str, *args: Any) -> None:
                return

            def _auth_ok(self) -> bool:
                import hmac
                value = self.headers.get("Authorization", "")
                return hmac.compare_digest(value, f"Bearer {outer.api_key}")

            def _send_json(self, status: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                if self.path in {"/health", "/ready"}:
                    if not self._auth_ok():
                        self._send_json(401, {"error": {"message": "Unauthorized"}})
                        return
                    self._send_json(200, {"status": "ok", "service": "Document Intelligence Enterprise AI AUTO", "version": "1.04", "mode": "ENTERPRISE AUTO"})
                    return
                if self.path == "/v1/models":
                    if not self._auth_ok():
                        self._send_json(401, {"error": {"message": "Unauthorized"}})
                        return
                    now = int(time.time())
                    models = [
                        {"id": name, "object": "model", "created": now, "owned_by": "ollama"}
                        for name in outer.model_provider()
                    ]
                    self._send_json(200, {"object": "list", "data": models})
                    return
                self._send_json(404, {"error": {"message": "Not found"}})

            def do_POST(self) -> None:
                if self.path != "/v1/chat/completions":
                    self._send_json(404, {"error": {"message": "Not found"}})
                    return
                if not self._auth_ok():
                    self._send_json(401, {"error": {"message": "Unauthorized"}})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                    model = str(body.get("model") or "")
                    messages = [
                        {"role": str(item.get("role") or "user"), "content": str(item.get("content") or "")}
                        for item in (body.get("messages") or [])
                        if isinstance(item, dict)
                    ]
                    scope = str(body.get("scope") or "AUTO")
                    service = outer.service_factory()
                    if body.get("stream"):
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                        self.send_header("Cache-Control", "no-cache")
                        self.end_headers()
                        completion_id = "chatcmpl-" + uuid.uuid4().hex
                        for token in service.answer_stream(
                            messages,
                            model=model,
                            scope=scope,
                            current_scope=outer.current_scope_provider(),
                        ):
                            payload = {
                                "id": completion_id,
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": model,
                                "choices": [{"index": 0, "delta": {"content": token}, "finish_reason": None}],
                            }
                            self.wfile.write(("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode("utf-8"))
                            self.wfile.flush()
                        self.wfile.write(b"data: [DONE]\n\n")
                        self.wfile.flush()
                        return
                    text, retrieval, _ = service.answer(
                        messages,
                        model=model,
                        scope=scope,
                        current_scope=outer.current_scope_provider(),
                    )
                    self._send_json(
                        200,
                        {
                            "id": "chatcmpl-" + uuid.uuid4().hex,
                            "object": "chat.completion",
                            "created": int(time.time()),
                            "model": model,
                            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                            "rag": {
                                "request_id": retrieval.request_id,
                                "mode": "AI AUTO",
                                "planner": retrieval.plan_source,
                                "scopes": list(retrieval.resolved_scopes),
                                "tools": list(retrieval.tools),
                                "intents": list(retrieval.intents),
                                "evidence_count": len(retrieval.evidence),
                                "evidence_strength": retrieval.evidence_strength,
                                "conflicts": list(retrieval.conflicts),
                                "warnings": list(retrieval.warnings),
                            },
                        },
                    )
                except Exception as exc:
                    self._send_json(500, {"error": {"message": str(exc), "type": type(exc).__name__}})

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, name="openai-rag-api", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        server = self._server
        self._server = None
        if server:
            server.shutdown()
            server.server_close()
        self._thread = None
