from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from typing import Any

import yaml

from app.ai.config import AISettings, AISettingsStore
from app.enterprise.credential_store import get_secret
from app.enterprise.profiles import PostgresProfile, QdrantProfile
from app.project.project_registry import ProjectRegistry


class LocalConfigurationError(ValueError):
    """Refuse nonlocal data connections and insecure API credentials."""


def user_configuration_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if os.name == "nt" and local:
        return Path(local) / "DocumentIntelligenceConsole"
    return Path.home() / ".document-intelligence-console"


def require_local_url(url: str, *, label: str) -> str:
    value = str(url or "").strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise LocalConfigurationError(f"{label} must use a local-only http://127.0.0.1 endpoint")
    try:
        port = parsed.port
    except ValueError as exc:
        raise LocalConfigurationError(f"{label} port is invalid") from exc
    if parsed.path not in ("", "/") or port is None or not 1 <= port <= 65535:
        raise LocalConfigurationError(f"{label} must be an origin with a valid port")
    return value


def require_local_host(host: str) -> str:
    if str(host).strip().lower() not in {"127.0.0.1", "localhost", "::1"}:
        raise LocalConfigurationError("Backend and database must listen on loopback only")
    return str(host).strip()


def load_profiles(config_dir: Path) -> tuple[dict[str, PostgresProfile], str, QdrantProfile, str]:
    config_path = Path(os.environ.get("CONFIG_FILE") or config_dir / "config.yaml")
    if not config_path.is_file():
        raise LocalConfigurationError(f"Database settings file not found: {config_path}. Save PostgreSQL Settings from the desktop console first.")
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict) or not isinstance(data.get("database"), dict):
        raise LocalConfigurationError("Invalid database settings format")
    db = data["database"]
    host = require_local_host(db.get("host", "127.0.0.1"))
    user = str(db.get("user") or "postgres")
    port = int(db.get("port") or 5432)
    if not 1 <= port <= 65535:
        raise LocalConfigurationError("Invalid PostgreSQL port")
    timeout = max(1, min(int(db.get("connect_timeout") or 10), 30))
    db_map = db.get("project_databases") or {}
    if not isinstance(db_map, dict):
        raise LocalConfigurationError("Invalid project_databases mapping")
    profiles = {
        code: PostgresProfile(
            host=host, port=port,
            database=str(db_map.get(code) or (db.get("database") if code == "COMMON" else definition.database_default)),
            user=user, connect_timeout=timeout, sslmode="prefer"
        )
        for code, definition in ProjectRegistry.PROJECTS.items()
    }
    # Preserve the GUI's keyring / explicit environment fallback contract.
    password = get_secret("postgres", f"{host}:{port}/{user}", "POSTGRES_PASSWORD")
    if not password:
        password = os.getenv(str(db.get("password_env") or "POSTGRES_PASSWORD"), "")
    console_path = config_dir / "console_profile.json"
    console: dict[str, Any] = {}
    if console_path.is_file():
        try:
            raw = json.loads(console_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                console = raw
        except (OSError, json.JSONDecodeError):
            pass
    # The Qdrant value used by the GUI is persisted in config.yaml on current versions.
    qraw = data.get("qdrant") or {}
    if not isinstance(qraw, dict):
        qraw = {}
    qurl = str(qraw.get("url") or console.get("qdrant_url") or "http://127.0.0.1:6333")
    qurl = require_local_url(qurl, label="Qdrant")
    qtimeout = max(1.0, min(float(qraw.get("timeout") or console.get("qdrant_timeout") or 10), 60.0))
    qkey = get_secret("qdrant", qurl, "QDRANT_API_KEY")
    return profiles, password, QdrantProfile(url=qurl, timeout=qtimeout), qkey


def load_settings(config_dir: Path) -> AISettings:
    settings = AISettingsStore(config_dir / "ai_config.json").load()
    require_local_url(settings.ollama_url, label="Ollama")
    require_local_url(settings.external_webui_url, label="Open WebUI")
    require_local_host(settings.api_host)
    return settings


def api_token(config_dir: Path) -> str:
    override = os.getenv("DIC_API_KEY", "").strip()
    if override:
        if len(override) < 32 or override in {"local-rag-key", "test-key"}:
            raise LocalConfigurationError("DIC_API_KEY must be at least 32 characters and nondefault")
        return override
    token_path = config_dir / "local_api_token.txt"
    if token_path.exists():
        token = token_path.read_text(encoding="utf-8").strip()
        if len(token) < 32:
            raise LocalConfigurationError("Existing local_api_token.txt is insecure; replace it manually")
        return token
    config_dir.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(48)
    with token_path.open("x", encoding="utf-8") as file:
        file.write(token + "\n")
    try:
        token_path.chmod(0o600)
    except OSError:
        pass
    return token
