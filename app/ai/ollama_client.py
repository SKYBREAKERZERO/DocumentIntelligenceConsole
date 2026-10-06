from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable

import requests


class OllamaError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class OllamaModel:
    name: str
    size: int = 0
    modified_at: str = ""
    family: str = ""
    parameter_size: str = ""
    quantization_level: str = ""


class OllamaClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float | None = None,
        connect_timeout: float = 10.0,
        read_timeout: float = 600.0,
        first_load_timeout: float = 900.0,
        stream_idle_timeout: float = 180.0,
    ) -> None:
        self.base_url = str(base_url or "http://127.0.0.1:11434").rstrip("/")
        # ``timeout`` is kept for callers from older releases. It maps to the
        # steady-state read timeout but no longer controls cold-model loading.
        if timeout is not None:
            read_timeout = float(timeout)
        self.connect_timeout = max(1.0, float(connect_timeout))
        self.read_timeout = max(15.0, float(read_timeout))
        self.first_load_timeout = max(self.read_timeout, float(first_load_timeout))
        self.stream_idle_timeout = max(15.0, float(stream_idle_timeout))
        self.timeout = self.read_timeout  # compatibility attribute
        self.session = requests.Session()

    @staticmethod
    def _set_response_socket_timeout(response: requests.Response, seconds: float) -> bool:
        """Best-effort switch from cold-start timeout to stream-idle timeout.

        requests/urllib3 expose no public API for changing the read timeout after
        headers are received. The nested socket exists on the standard urllib3
        HTTPResponse used by requests; failure is harmless and leaves the larger
        read timeout in place.
        """
        candidates = (
            lambda: response.raw._fp.fp.raw._sock,  # type: ignore[attr-defined]
            lambda: response.raw._connection.sock,  # type: ignore[attr-defined]
        )
        for resolver in candidates:
            try:
                sock = resolver()
                if sock is not None:
                    sock.settimeout(float(seconds))
                    return True
            except Exception:
                continue
        return False


    @staticmethod
    def _normalize_messages(messages: Any) -> list[dict[str, str]]:
        """Normalize GUI/agent messages to the strict Ollama chat contract.

        Tk variables, structured evidence and planner objects must never be
        passed through verbatim.  Each message is reduced to role/content and
        content is always a UTF-8 JSON-safe string. This prevents the HTTP 400
        failures caused by dict/list content leaking into /api/chat.
        """
        if not isinstance(messages, (list, tuple)):
            raise OllamaError("Ollama messages must be a list/tuple of chat messages.")
        normalized: list[dict[str, str]] = []
        for index, item in enumerate(messages):
            if not isinstance(item, dict):
                raise OllamaError(f"Ollama message #{index + 1} must be an object.")
            role = str(item.get("role") or "").strip().lower()
            if role not in {"system", "user", "assistant", "tool"}:
                raise OllamaError(f"Ollama message #{index + 1} has unsupported role {role!r}.")
            content = item.get("content", "")
            if content is None:
                content_text = ""
            elif isinstance(content, str):
                content_text = content
            elif isinstance(content, (dict, list, tuple)):
                content_text = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
            else:
                content_text = str(content)
            normalized.append({"role": role, "content": content_text})
        if not normalized:
            raise OllamaError("Ollama messages cannot be empty.")
        return normalized

    @staticmethod
    def _raise_for_status(response: requests.Response, *, operation: str) -> None:
        """Raise a diagnostic error preserving Ollama's response body.

        The previous client delegated to requests.raise_for_status(), which
        reduced useful 400 diagnostics to "Bad Request". Enterprise support
        needs the actual Ollama validation error while avoiding prompt logging.
        """
        if 200 <= int(response.status_code) < 300:
            return
        detail = ""
        try:
            payload = response.json()
            if isinstance(payload, dict):
                detail = str(payload.get("error") or payload)
            else:
                detail = str(payload)
        except Exception:
            detail = str(response.text or "").strip()
        detail = detail[:4000] or response.reason or "Unknown Ollama HTTP error"
        raise OllamaError(
            f"Ollama {operation} failed: HTTP {response.status_code} {response.reason or ''}. "
            f"Endpoint={response.request.url if response.request else '-'}; Error={detail}"
        )

    def health(self) -> dict[str, Any]:
        try:
            response = self.session.get(
                f"{self.base_url}/api/tags",
                timeout=(self.connect_timeout, min(self.read_timeout, 8.0)),
            )
            self._raise_for_status(response, operation="health check")
            payload = response.json()
            models = payload.get("models") or []
            return {"ok": True, "model_count": len(models), "message": f"Connected · {len(models)} models"}
        except Exception as exc:
            return {"ok": False, "model_count": 0, "message": str(exc)}

    def list_models(self) -> list[OllamaModel]:
        try:
            response = self.session.get(
                f"{self.base_url}/api/tags",
                timeout=(self.connect_timeout, min(self.read_timeout, 12.0)),
            )
            self._raise_for_status(response, operation="model listing")
            payload = response.json()
        except OllamaError:
            raise
        except Exception as exc:
            raise OllamaError(f"Cannot reach Ollama at {self.base_url}: {exc}") from exc
        result: list[OllamaModel] = []
        for item in payload.get("models") or []:
            details = item.get("details") or {}
            name = str(item.get("name") or item.get("model") or "").strip()
            if not name:
                continue
            result.append(
                OllamaModel(
                    name=name,
                    size=int(item.get("size") or 0),
                    modified_at=str(item.get("modified_at") or ""),
                    family=str(details.get("family") or ""),
                    parameter_size=str(details.get("parameter_size") or ""),
                    quantization_level=str(details.get("quantization_level") or ""),
                )
            )
        return result

    @staticmethod
    def _looks_like_reasoning_prelude(text: str) -> bool:
        """Detect common accidental chain-of-thought preambles conservatively.

        Some thinking-tuned local models have emitted reasoning inside the
        content field even when ``think=false``. We only suppress strong
        meta-reasoning signatures (for example "首先，用户要求…我需要…") or
        explicit <think> blocks; ordinary answers beginning with "首先" are
        not suppressed.
        """
        value = re.sub(r"\s+", " ", str(text or "").strip())[:500]
        return bool(re.search(
            r"^(?:<think>|首先[，,]?\s*用户(?:要求|问|消息)|用户(?:要求|问的是).{0,80}(?:我需要|需要我)|"
            r"我需要(?:先|用|回答|确保|检查)|(?:we need|the user asks|the user wants|i need to)\b)",
            value, flags=re.I,
        ))

    def chat_stream(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        temperature: float = 0.2,
        num_ctx: int = 4096,
        cancel_check=None,
        keep_alive: str | int | None = "30m",
        think: bool | None = False,
    ) -> Iterable[str]:
        if not str(model or "").strip():
            raise OllamaError("No Ollama chat model is selected.")
        normalized_messages = self._normalize_messages(messages)
        body = {
            "model": str(model).strip(),
            "messages": normalized_messages,
            "stream": True,
            "options": {
                "temperature": float(temperature),
                "num_ctx": int(num_ctx),
            },
        }
        if keep_alive is not None:
            body["keep_alive"] = keep_alive
        if think is not None:
            body["think"] = bool(think)
        first_payload_seen = False
        visibility_decided = False
        suppress_reasoning = False
        probe = ""
        try:
            with self.session.post(
                f"{self.base_url}/api/chat",
                json=body,
                timeout=(self.connect_timeout, self.first_load_timeout),
                stream=True,
            ) as response:
                self._raise_for_status(response, operation="chat request")
                for raw_line in response.iter_lines(decode_unicode=True):
                    if cancel_check and cancel_check():
                        return
                    if not raw_line:
                        continue
                    if not first_payload_seen:
                        first_payload_seen = True
                        self._set_response_socket_timeout(response, self.stream_idle_timeout)
                    try:
                        payload = json.loads(raw_line)
                    except json.JSONDecodeError:
                        continue
                    if payload.get("error"):
                        raise OllamaError(str(payload["error"]))
                    message = payload.get("message") or {}
                    # Native Ollama may return reasoning in message.thinking.
                    # Never surface it in the enterprise UI; only final content is
                    # streamed to the user.
                    content = str(message.get("content") or "")
                    if content:
                        if suppress_reasoning:
                            if "</think>" in content:
                                content = content.split("</think>", 1)[1]
                                suppress_reasoning = False
                                visibility_decided = True
                                if content:
                                    yield content
                        elif visibility_decided:
                            # Remove any complete late think block without ever
                            # exposing it to the UI.
                            content = re.sub(r"<think>.*?</think>", "", content, flags=re.I | re.S)
                            if content:
                                yield content
                        else:
                            probe += content
                            if "</think>" in probe:
                                tail = probe.split("</think>", 1)[1]
                                probe = ""
                                visibility_decided = True
                                if tail:
                                    yield tail
                            elif "<think>" in probe or self._looks_like_reasoning_prelude(probe):
                                suppress_reasoning = True
                                probe = ""
                            elif len(probe) >= 240 or "\n" in probe:
                                visibility_decided = True
                                yield probe
                                probe = ""
                    if payload.get("done"):
                        if probe and not suppress_reasoning:
                            yield probe
                        return
        except OllamaError:
            raise
        except requests.exceptions.ReadTimeout as exc:
            stage = "first model load/token" if not first_payload_seen else "stream idle"
            configured = self.first_load_timeout if not first_payload_seen else self.stream_idle_timeout
            raise OllamaError(
                f"Ollama {stage} timed out after {configured:.0f}s. "
                "Increase the corresponding timeout or use a smaller/non-thinking local model."
            ) from exc
        except Exception as exc:
            raise OllamaError(f"Ollama chat request failed: {exc}") from exc

    def chat_json(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        temperature: float = 0.0,
        num_ctx: int = 4096,
        keep_alive: str | int | None = "30m",
    ) -> dict[str, Any]:
        """Request a strict JSON response for planning/classification.

        The planner never receives database credentials or executable SQL. Cold
        local CPU models can take longer than ordinary API calls, so structured
        planning uses the configured first-load timeout instead of a hard 120s.
        """
        if not str(model or "").strip():
            raise OllamaError("No Ollama model is selected for structured planning.")
        normalized_messages = self._normalize_messages(messages)
        body = {
            "model": str(model).strip(),
            "messages": normalized_messages,
            "stream": False,
            "format": "json",
            "think": False,
            "options": {"temperature": float(temperature), "num_ctx": int(num_ctx)},
        }
        if keep_alive is not None:
            body["keep_alive"] = keep_alive
        try:
            response = self.session.post(
                f"{self.base_url}/api/chat",
                json=body,
                timeout=(self.connect_timeout, self.first_load_timeout),
            )
            self._raise_for_status(response, operation="structured chat request")
            payload = response.json()
            if payload.get("error"):
                raise OllamaError(str(payload["error"]))
            content = str((payload.get("message") or {}).get("content") or "").strip()
            if not content:
                raise OllamaError("Ollama returned an empty structured response.")
            # Defensive cleanup for models that still wrap final JSON in think tags.
            if "</think>" in content:
                content = content.split("</think>", 1)[1].strip()
            if content.startswith("```json") and content.endswith("```"):
                content = content[7:-3].strip()
            parsed = json.loads(content)
            if not isinstance(parsed, dict):
                raise OllamaError("Structured planner response is not a JSON object.")
            return parsed
        except (json.JSONDecodeError, OllamaError):
            raise
        except requests.exceptions.ReadTimeout as exc:
            raise OllamaError(
                f"Ollama structured planner timed out after {self.first_load_timeout:.0f}s."
            ) from exc
        except Exception as exc:
            raise OllamaError(f"Ollama structured request failed: {exc}") from exc

    def chat(self, **kwargs: Any) -> str:
        return "".join(self.chat_stream(**kwargs))

