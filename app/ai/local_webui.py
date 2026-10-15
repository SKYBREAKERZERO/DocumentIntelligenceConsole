from __future__ import annotations

import ipaddress
import socket
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass
from urllib.parse import urlparse


class LocalWebUIError(ValueError):
    pass


def validate_local_webui_url(raw: str) -> str:
    url = str(raw or "").strip().rstrip("/")
    parts = urlparse(url)
    try:
        valid_port = bool(parts.port and 1 <= parts.port <= 65535)
    except ValueError:
        valid_port = False
    if parts.scheme != "http" or parts.hostname not in {"127.0.0.1", "localhost"} or parts.username or parts.password or parts.query or parts.fragment or parts.path or not valid_port:
        raise LocalWebUIError("External UI must be a local origin such as http://127.0.0.1:3000")
    return url


def open_webui_default_browser(raw: str) -> bool:
    return webbrowser.open(validate_local_webui_url(raw), new=2)


def http_probe(raw: str, path: str = "/", timeout: float = 1.5) -> str:
    url = validate_local_webui_url(raw) + path
    try:
        request = urllib.request.Request(url, method="GET")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=timeout) as response:
            return "ONLINE" if 200 <= response.status < 500 else "ERROR"
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            return "ONLINE_AUTH_REQUIRED"
        return "ERROR"
    except (OSError, urllib.error.URLError, ValueError):
        return "OFFLINE"


def local_service_status(*, webui_url: str, api_port: int, ollama_url: str, qdrant_url: str) -> dict[str, str]:
    """Short timeouts; GUI callers must invoke from a worker thread."""
    return {
        "Open WebUI": http_probe(webui_url),
        "Backend API": http_probe(f"http://127.0.0.1:{api_port}", "/v1/models"),
        "Ollama": http_probe(ollama_url, "/api/tags"),
        "Qdrant": http_probe(qdrant_url, "/"),
    }
