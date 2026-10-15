from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.enterprise.backend_watchdog import (
    BackendWatchdog,
    MAX_RESTART_ATTEMPTS,
    SingleInstanceLock,
    WatchdogAlreadyRunning,
)
from app.server.api import MODEL_ID, configured_chat_limit, create_app
from app.server.resilience import AdmissionController

KEY = "k" * 64
HEADERS = {"Authorization": "Bearer " + KEY}


class FakeRAG:
    def __init__(self, *, raises=False):
        self.raises = raises
        self.counter = 0

    def answer(self, messages, **kwargs):
        self.counter += 1
        if self.raises:
            raise ValueError("private document content and credentials must not leak")
        return "Answer [1]", SimpleNamespace(
            request_id="run-1", route="ENGINEERING_AGENT",
            resolved_scopes=("21MM",), evidence=("x",), tools=("sql_exact_search",), warnings=(),
        ), 5

    def answer_stream(self, messages, **kwargs):
        if self.raises:
            raise ValueError("private document content and credentials must not leak")
        yield "Answer "
        yield "[1]"


class FakeRuntime:
    preferred_model = "local-qwen"

    def __init__(self, service=None, embedding="READY"):
        self.service = service or FakeRAG()
        self.embedding = embedding

    def get_service(self):
        return self.service

    def local_status(self):
        return {"mode": "LOCAL_ONLY", "embedding": self.embedding, "rag_loaded": True}


def payload(stream=False):
    return {"model": MODEL_ID, "messages": [{"role": "user", "content": "511 version"}], "stream": stream}


def test_admission_limit_cannot_oversubscribe_and_is_idempotent():
    controller = AdmissionController(1)
    lease = controller.acquire()
    assert lease is not None
    assert controller.acquire() is None
    lease.release()
    lease.release()  # stream completion and background cleanup may both call release
    assert controller.snapshot()["active"] == 0
    assert controller.snapshot()["rejected"] == 1
    assert controller.snapshot()["completed"] == 1
    assert controller.acquire() is not None


def test_concurrent_admission_is_thread_safe():
    controller = AdmissionController(2)
    gate = threading.Barrier(12)
    leases = []
    guard = threading.Lock()

    def work():
        gate.wait()
        item = controller.acquire()
        with guard:
            if item:
                leases.append(item)

    threads = [threading.Thread(target=work) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(leases) == 2
    assert controller.snapshot()["rejected"] == 10
    for lease in leases:
        lease.release()
    assert controller.snapshot()["active"] == 0


@pytest.mark.parametrize("limit", [0, 9, -4])
def test_limit_config_validation(limit):
    with pytest.raises(ValueError):
        AdmissionController(limit)


def test_environment_limit_rejects_unbounded_configuration(monkeypatch):
    monkeypatch.setenv("DIC_MAX_ACTIVE_CHATS", "999")
    with pytest.raises(ValueError):
        configured_chat_limit()
    monkeypatch.setenv("DIC_MAX_ACTIVE_CHATS", "2")
    assert configured_chat_limit() == 2


def test_api_rejects_busy_and_recovers():
    app = create_app(FakeRuntime(), KEY, max_active_chats=1)
    lease = app.state.chat_admissions.acquire()
    assert lease
    with TestClient(app, base_url="http://localhost") as client:
        response = client.post("/v1/chat/completions", headers=HEADERS, json=payload())
        assert response.status_code == 429
        assert response.headers["retry-after"] == "3"
        lease.release()
        response = client.post("/v1/chat/completions", headers=HEADERS, json=payload())
        assert response.status_code == 200
        assert response.json()["rag"]["request_id"] == "run-1"
        metrics = client.get("/api/v1/runtime/metrics", headers=HEADERS)
        assert metrics.status_code == 200
        assert metrics.json()["chat"]["active"] == 0
        assert metrics.json()["chat"]["rejected"] == 1
        assert "messages" not in str(metrics.json())


def test_stream_and_error_release_admission():
    app = create_app(FakeRuntime(FakeRAG(raises=True)), KEY, max_active_chats=1)
    with TestClient(app, base_url="http://localhost") as client:
        response = client.post("/v1/chat/completions", headers=HEADERS, json=payload(stream=True))
        assert response.status_code == 200
        assert "Local RAG error" in response.text
        assert "private document content" not in response.text
        state = client.get("/api/v1/runtime/metrics", headers=HEADERS).json()["chat"]
        assert state["active"] == 0
        assert state["failed"] == 1
        response = client.post("/v1/chat/completions", headers=HEADERS, json=payload())
        assert response.status_code == 503
        assert "private document" not in response.text
        assert client.get("/api/v1/runtime/metrics", headers=HEADERS).json()["chat"]["active"] == 0


def test_metric_and_readiness_are_authenticated_and_readiness_is_honest():
    app = create_app(FakeRuntime(embedding="WARMING"), KEY, max_active_chats=1)
    with TestClient(app, base_url="http://localhost") as client:
        assert client.get("/api/v1/runtime/metrics").status_code == 401
        ready = client.get("/api/v1/health/ready", headers=HEADERS)
        assert ready.status_code == 503
        assert ready.json()["dependency_checks"] == "NOT_PERFORMED"


def test_watchdog_only_restores_stopped_local_backend(tmp_path):
    state = {"value": "STOPPED"}
    started = []
    def check(port, config):
        return state["value"]
    def start(root, port, config):
        started.append((root, port, config))
        state["value"] = "STARTING"
        return "STARTING"
    watchdog = BackendWatchdog(tmp_path, tmp_path, check=check, start=start, clock=lambda: 2)
    assert watchdog.tick(18080) == "STARTING"
    assert watchdog.tick(18080) == "STARTING"
    assert len(started) == 1
    state["value"] = "PORT_OCCUPIED_UNVERIFIED"
    assert watchdog.tick(18080) == "PORT_OCCUPIED_UNVERIFIED"
    assert len(started) == 1


def test_watchdog_crash_loop_circuit_opens(tmp_path):
    started = []
    now = [1000.0]
    watchdog = BackendWatchdog(
        tmp_path, tmp_path,
        check=lambda *args: "STOPPED",
        start=lambda *args: started.append(1) or "STARTING",
        clock=lambda: now[0],
    )
    for _ in range(MAX_RESTART_ATTEMPTS):
        watchdog.tick(18080)
    assert watchdog.tick(18080) == "RESTART_CIRCUIT_OPEN"
    assert len(started) == MAX_RESTART_ATTEMPTS
    now[0] += 601
    assert watchdog.tick(18080) == "STARTING"


def test_watchdog_single_instance_lock(tmp_path):
    lockfile = tmp_path / "lock"
    with SingleInstanceLock(lockfile):
        with pytest.raises(WatchdogAlreadyRunning):
            with SingleInstanceLock(lockfile):
                pass
    with SingleInstanceLock(lockfile):
        pass


def test_watchdog_heartbeat_reports_stale_and_fresh(tmp_path):
    import json
    import time
    from app.enterprise.local_deployment import read_backend_watchdog_status

    assert read_backend_watchdog_status(tmp_path) == "NOT_RUNNING"
    path = tmp_path / "backend_watchdog_status.json"
    path.write_text(json.dumps({"status": "RUNNING", "updated_at": int(time.time())}), encoding="utf-8")
    assert read_backend_watchdog_status(tmp_path) == "RUNNING"
    path.write_text(json.dumps({"status": "RUNNING", "updated_at": int(time.time() - 120)}), encoding="utf-8")
    assert read_backend_watchdog_status(tmp_path) == "STALE"
    path.write_text(json.dumps({"status": "RUNNING", "updated_at": "garbage"}), encoding="utf-8")
    assert read_backend_watchdog_status(tmp_path) == "NOT_RUNNING"


def test_windows_watchdog_management_is_ascii_and_never_opens_backend_ports():
    root = Path(__file__).resolve().parents[1]
    for filename in ("manage_backend_watchdog.ps1", "backup_authoritative_sql.ps1", "check_enterprise_readiness.ps1"):
        source = (root / "scripts" / filename).read_bytes()
        assert all(b < 128 for b in source), "PowerShell 5.1 script must be ASCII-compatible"
    source = (root / "scripts" / "manage_backend_watchdog.ps1").read_text()
    assert "HKCU:" in source
    assert "pythonw.exe" in source
    assert "New-NetFirewallRule" not in source
    assert "netsh interface portproxy" not in source


def test_preserves_sql_and_qdrant_schema_and_tracks_current_api_version():
    root = Path(__file__).resolve().parents[1]
    from app.server.api import create_app

    assert create_app(FakeRuntime(), KEY).version == "1.20"
    assert (root / "database" / "upgrade_engineering_intelligence_v7.sql").is_file()
    assert (root / "scripts" / "qdrant_loopback_rebind.ps1").is_file()
    assert (root / "README.md").read_text(encoding="utf-8").startswith("# DocumentIntelligenceConsole Enterprise v1.30")
