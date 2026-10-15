from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from app.enterprise import local_deployment as deployment


def test_gui_startup_command_uses_project_launcher(tmp_path: Path):
    launch = tmp_path / "scripts" / "startup_desktop.pyw"
    launch.parent.mkdir(parents=True)
    launch.write_text("print('start')", encoding="utf-8")
    command = deployment.gui_autostart_command(tmp_path, python_executable=Path(sys.executable))
    assert "startup_desktop.pyw" in command
    assert str(launch) in command


def test_gui_startup_command_refuses_missing_launcher(tmp_path: Path):
    with pytest.raises(deployment.DeploymentError, match="missing"):
        deployment.gui_autostart_command(tmp_path)


def test_backend_state_stopped_on_unbound_local_port(tmp_path: Path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    assert deployment.get_backend_state(port, tmp_path) == "STOPPED"


def test_backend_state_refuses_unknown_listener(tmp_path: Path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        assert deployment.get_backend_state(port, tmp_path) == "PORT_OCCUPIED_UNVERIFIED"


def test_lan_state_is_not_exposure_without_admin_action(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("ProgramData", raising=False)
    assert deployment.read_lan_share(tmp_path) == {}
    assert "未配置" in deployment.lan_share_status(tmp_path)
    (tmp_path / "lan_share.json").write_text(json.dumps({
        "managed_by": deployment.LAN_RULE_NAME,
        "ip": "192.168.210.186", "port": 3000, "interface": "Wi-Fi"
    }), encoding="utf-8")
    assert "192.168.210.186:3000" in deployment.lan_share_status(tmp_path)


def test_lan_state_rejects_unowned_file(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("ProgramData", raising=False)
    (tmp_path / "lan_share.json").write_text(json.dumps({
        "managed_by": "some-other-program", "ip": "192.168.1.1", "port": 3000,
    }), encoding="utf-8")
    assert deployment.read_lan_share(tmp_path) == {}


def test_windows_admin_actions_rejected_on_other_platforms(tmp_path: Path):
    if os.name == "nt":
        pytest.skip("Cross-platform refusal case")
    with pytest.raises(deployment.DeploymentError):
        deployment.ensure_backend_started(tmp_path, 18080)
    with pytest.raises(deployment.DeploymentError):
        deployment.request_lan_configuration(tmp_path, "Enable")


def test_windows_lan_script_has_strict_guards():
    script = Path(__file__).resolve().parents[1] / "scripts" / "configure_lan_webui.ps1"
    source = script.read_text(encoding="ascii")
    for required in (
        "ConfirmInternalNetwork", "Assert-Administrator", "Get-ActiveWifi",
        "IsInRole", "-RemoteAddress LocalSubnet", "-InterfaceAlias",
        "Get-NetTCPConnection -State Listen", "18080", "6333", "11434",
        "portproxy", "-Action Allow", "Remove-ManagedRule",
        "127.0.0.1", "ProgramData",
    ):
        assert required in source
    assert "0.0.0.0" not in source


def test_backend_spawn_is_local_and_no_cloud_env(monkeypatch, tmp_path: Path):
    """Dry-run spawn: never launch a real background server during testing."""
    monkeypatch.setattr(deployment, "_windows_only", lambda: None)
    monkeypatch.setattr(deployment, "get_backend_state", lambda *a, **k: "STOPPED")
    monkeypatch.setattr(deployment, "_backend_command", lambda root: [sys.executable, "-m", "app.server"])
    called = {}

    def fake_popen(cmd, **kwargs):
        called.update(cmd=cmd, **kwargs)
        return object()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    result = deployment.ensure_backend_started(tmp_path, 18080, tmp_path / "profile")
    assert result == "STARTING"
    assert called["cwd"] == str(tmp_path)
    assert called["env"]["HF_HUB_OFFLINE"] == "1"
    assert called["env"]["TRANSFORMERS_OFFLINE"] == "1"
    assert (tmp_path / "profile" / "logs" / "backend_startup.log").exists()
    # Duplicate GUI launch must not spawn again before the first backend binds.
    def must_not_spawn(*args, **kwargs):
        pytest.fail("Duplicate API spawn")
    monkeypatch.setattr(subprocess, "Popen", must_not_spawn)
    assert deployment.ensure_backend_started(tmp_path, 18080, tmp_path / "profile") == "STARTING"
