"""Windows localhost backend supervisor, login startup, and LAN UI controls.

Does not expose the RAG backend or data services to LAN. No external calls.
"""
from __future__ import annotations

import ctypes
import json
import os
import time
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.server.configuration import user_configuration_dir

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "DocumentIntelligenceConsole"
LAN_RULE_NAME = "DIC-OpenWebUI-LAN-Only"


class DeploymentError(RuntimeError):
    pass


def _windows_only() -> None:
    if os.name != "nt":
        raise DeploymentError("Windows-only management operation")


def _win_quote(value: str | Path) -> str:
    """Use Windows CreateProcess-compatible quoting for trusted local paths."""
    return subprocess.list2cmdline([str(value)])


def get_backend_state(port: int, config_dir: Path | None = None) -> str:
    """Authenticated model discovery distinguishes our API from an unrelated listener."""
    config_dir = config_dir or user_configuration_dir()
    if not 1 <= port <= 65535:
        return "INVALID_PORT"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        if sock.connect_ex(("127.0.0.1", port)) != 0:
            return "STOPPED"
    token_file = config_dir / "local_api_token.txt"
    if not token_file.exists():
        return "PORT_OCCUPIED_UNVERIFIED"
    try:
        key = token_file.read_text(encoding="utf-8").strip()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/models",
            headers={"Authorization": f"Bearer {key}"},
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=1.5) as response:
            payload = json.load(response)
        names = {str(item.get("id")) for item in payload.get("data", []) if isinstance(item, dict)}
        return "RUNNING" if "document-intelligence-rag" in names else "PORT_OCCUPIED_UNVERIFIED"
    except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError):
        return "PORT_OCCUPIED_UNVERIFIED"


def _backend_command(project_root: Path) -> list[str]:
    if getattr(sys, "frozen", False):
        candidate = project_root / "DocumentIntelligenceBackend.exe"
        if not candidate.is_file():
            raise DeploymentError("Packaged EXE requires companion DocumentIntelligenceBackend.exe")
        return [str(candidate)]
    python = project_root / ".venv" / "Scripts" / "python.exe"
    if not python.is_file():
        python = Path(sys.executable)
    return [str(python), "-m", "app.server"]


def ensure_backend_started(project_root: Path, port: int, config_dir: Path | None = None) -> str:
    """Atomically start one localhost Backend across GUI and watchdog processes.

    A cross-process exclusive file lock closes the previous check/start race.
    Never kills an unknown listener or starts a service at system boot.
    """
    _windows_only()
    config_dir = config_dir or user_configuration_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    from app.enterprise.backend_watchdog import SingleInstanceLock, WatchdogAlreadyRunning
    try:
        with SingleInstanceLock(config_dir / "backend_spawn.lock"):
            # Re-check *inside* lock: another GUI/watchdog may have just spawned.
            state = get_backend_state(port, config_dir)
            if state != "STOPPED":
                return state
            marker = config_dir / "backend_spawn.json"
            try:
                last = json.loads(marker.read_text(encoding="utf-8"))
                if time.time() - float(last.get("at", 0)) < 25:
                    return "STARTING"
            except (OSError, ValueError, TypeError):
                pass
            cmd = _backend_command(Path(project_root))
            logdir = config_dir / "logs"
            logdir.mkdir(parents=True, exist_ok=True)
            logfile = logdir / "backend_startup.log"
            env = os.environ.copy()
            env.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_DATASETS_OFFLINE="1", PYTHONUNBUFFERED="1")
            env.setdefault("PYTHONIOENCODING", "utf-8")
            creation_flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            try:
                with logfile.open("ab") as output:
                    proc = subprocess.Popen(
                        cmd, cwd=str(project_root), env=env, stdin=subprocess.DEVNULL,
                        stdout=output, stderr=subprocess.STDOUT, creationflags=creation_flags,
                        close_fds=True,
                    )
                marker.write_text(json.dumps({"at": time.time(), "pid": getattr(proc, "pid", None)}), encoding="utf-8")
            except OSError:
                marker.unlink(missing_ok=True)
                raise
            return "STARTING"
    except WatchdogAlreadyRunning:
        # Another process holds the brief start lock; it owns spawn decision.
        return "STARTING"


def gui_autostart_command(project_root: Path, python_executable: Path | None = None) -> str:
    root = Path(project_root).resolve()
    if getattr(sys, "frozen", False):
        return _win_quote(Path(sys.executable).resolve())
    py = Path(python_executable or sys.executable).resolve()
    if py.name.lower() == "python.exe" and (py.with_name("pythonw.exe")).is_file():
        py = py.with_name("pythonw.exe")
    script = root / "scripts" / "startup_desktop.pyw"
    if not script.is_file():
        raise DeploymentError(f"GUI startup launcher missing: {script}")
    return f"{_win_quote(py)} {_win_quote(script)}"


def get_windows_login_autostart() -> bool:
    _windows_only()
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, RUN_NAME)
        return bool(value)
    except FileNotFoundError:
        return False


def set_windows_login_autostart(enabled: bool, project_root: Path) -> bool:
    """HKCU Run launches the GUI after *this user's login*, not before login."""
    _windows_only()
    import winreg
    if enabled:
        cmd = gui_autostart_command(project_root)
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, cmd)
    else:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, RUN_NAME)
        except FileNotFoundError:
            pass
    return get_windows_login_autostart()


def _lan_state_directory(config_dir: Path | None = None) -> Path:
    """Machine-level nonsecret state can be read by an ordinary logged-in GUI."""
    common = os.environ.get("ProgramData")
    return Path(common) / "DocumentIntelligenceConsole" if common else (config_dir or user_configuration_dir())


def read_lan_share(config_dir: Path | None = None) -> dict[str, Any]:
    path = _lan_state_directory(config_dir) / "lan_share.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(data, dict) and data.get("managed_by") == LAN_RULE_NAME:
            return data
    except (OSError, ValueError):
        pass
    return {}


def lan_share_status(config_dir: Path | None = None) -> str:
    report = _lan_state_directory(config_dir) / "lan_last_result.json"
    try:
        result = json.loads(report.read_text(encoding="utf-8-sig"))
        if isinstance(result, dict) and not result.get("ok"):
            return "配置失败：" + str(result.get("message", "请检查管理员执行日志"))
    except (OSError, ValueError, AttributeError):
        pass
    share = read_lan_share(config_dir)
    if not share:
        return "未配置；Open WebUI 仅供本机访问"
    ip = str(share.get("ip", ""))
    port = share.get("port", 3000)
    return f"已登记共享: http://{ip}:{port} （请从另一台电脑测试；不保证 Wi-Fi 无设备隔离）"


def request_lan_configuration(project_root: Path, action: str, port: int = 3000) -> None:
    """Raise a UAC prompt for an explicit GUI button action only.

    There is intentionally no LAN auto-expose when the GUI starts.
    """
    _windows_only()
    if action not in {"Enable", "Disable"} or not 1 <= port <= 65535:
        raise DeploymentError("Invalid LAN management request")
    script = Path(project_root).resolve() / "scripts" / "configure_lan_webui.ps1"
    if not script.is_file():
        raise DeploymentError("LAN configuration script not found")
    arguments = f'-NoProfile -NonInteractive -ExecutionPolicy Bypass -File {_win_quote(script)} -Action {action} -Port {port}'
    if action == "Enable":
        arguments += " -ConfirmInternalNetwork"
    # ShellExecuteW with runas displays Windows UAC; no elevation silently granted.
    result = ctypes.windll.shell32.ShellExecuteW(None, "runas", "powershell.exe", arguments, str(project_root), 1)
    if result <= 32:
        raise DeploymentError(f"Administrator approval cancelled or failed (code {result})")


# v1.08: independent watchdog survives closing the desktop administration UI.
WATCHDOG_RUN_NAME = "DocumentIntelligenceBackendWatchdog"


def get_backend_watchdog_autostart() -> bool:
    """Whether the current-user Windows login launches the backend watchdog."""
    _windows_only()
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, WATCHDOG_RUN_NAME)
        return bool(value)
    except FileNotFoundError:
        return False


def read_backend_watchdog_status(config_dir: Path | None = None) -> str:
    """Report only fresh supervisor heartbeats; not an inference from registry state."""
    directory = config_dir or user_configuration_dir()
    path = directory / "backend_watchdog_status.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        age = time.time() - float(data["updated_at"])
        if age < -30 or age > 45:
            return "STALE"
        state = str(data.get("status") or "UNKNOWN")
        if state not in {"RUNNING", "STARTING", "STOPPED", "PORT_OCCUPIED_UNVERIFIED",
                         "RESTART_CIRCUIT_OPEN", "START_FAILED", "ERROR"}:
            return "UNKNOWN"
        return state
    except (ValueError, OSError, TypeError, KeyError):
        return "NOT_RUNNING"


def set_backend_watchdog_autostart(enabled: bool, project_root: Path) -> bool:
    """Opt-in independent, bounded supervisor for Windows user sessions.

    Never terminates the backend, opens ports, or installs a system service.
    """
    _windows_only()
    import winreg
    config_dir = user_configuration_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    stop_file = config_dir / "backend_watchdog.stop"
    if enabled:
        root = Path(project_root).resolve()
        script = root / "scripts" / "backend_watchdog.pyw"
        pythonw = root / ".venv" / "Scripts" / "pythonw.exe"
        if not script.is_file() or not pythonw.is_file():
            raise DeploymentError("Install the v1.10 Python virtual environment and watchdog launcher first")
        command = subprocess.list2cmdline([str(pythonw), str(script)])
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, WATCHDOG_RUN_NAME, 0, winreg.REG_SZ, command)
        stop_file.unlink(missing_ok=True)
        # SingleInstanceLock means repeated GUI launches cannot spawn two supervisors.
        subprocess.Popen([str(pythonw), str(script)], cwd=str(root),
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True)
    else:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, WATCHDOG_RUN_NAME)
        except FileNotFoundError:
            pass
        stop_file.write_text("stop", encoding="ascii")
    return get_backend_watchdog_autostart()
