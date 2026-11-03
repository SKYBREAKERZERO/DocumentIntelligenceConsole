"""Bound Open WebUI requests before any model or database work begins."""
from __future__ import annotations

from typing import Any

MAX_REQUEST_BYTES = 1_048_576
MAX_MESSAGES = 50
MAX_MESSAGE_CHARS = 16_000
MAX_TOTAL_CHARS = 64_000


class UnsafeRequest(ValueError):
    pass


def check_request(body: Any, *, allowed_models: list[str]) -> tuple[str, list[dict[str, str]], str]:
    if not isinstance(body, dict):
        raise UnsafeRequest("Expected JSON object")
    model = body.get("model")
    if not isinstance(model, str) or model not in allowed_models:
        raise UnsafeRequest("Unknown or disabled model")
    scope = body.get("scope", "AUTO")
    if not isinstance(scope, str) or len(scope) > 80:
        raise UnsafeRequest("Invalid scope")
    raw = body.get("messages")
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_MESSAGES:
        raise UnsafeRequest("Invalid message count")
    messages: list[dict[str, str]] = []
    total = 0
    for value in raw:
        if not isinstance(value, dict) or value.get("role") not in {"user", "assistant", "system"}:
            raise UnsafeRequest("Invalid chat message")
        content = value.get("content")
        if not isinstance(content, str) or len(content) > MAX_MESSAGE_CHARS:
            raise UnsafeRequest("Message too large or invalid")
        total += len(content)
        if total > MAX_TOTAL_CHARS:
            raise UnsafeRequest("Conversation too large")
        messages.append({"role": value["role"], "content": content})
    if not any(m["role"] == "user" and m["content"].strip() for m in messages):
        raise UnsafeRequest("A non-empty user message is required")
    if body.get("stream", False) not in {False, True, 0, 1}:
        raise UnsafeRequest("Invalid stream setting")
    return model, messages, scope
