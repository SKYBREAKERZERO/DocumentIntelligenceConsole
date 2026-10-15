"""Per-user Windows backend watchdog; no admin or public network binding.

Only starts an authenticated, stopped localhost backend. Never kills unrelated
listeners and never changes PostgreSQL, Qdrant, Ollama or LAN firewall rules.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable

from app.enterprise.local_deployment import ensure_backend_started, get_backend_state
from app.server.configuration import load_settings, user_configuration_dir

MAX_RESTART_ATTEMPTS = 5
RESTART_WINDOW_SECONDS = 600
WATCHDOG_INTERVAL_SECONDS = 15


class WatchdogAlreadyRunning(RuntimeError):
    pass


class SingleInstanceLock:
    """Windows byte-range file lock; POSIX flock only for cross-platform tests."""

    def __init__(self, path: Path):
        self.path = path
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        self.handle = self.path.open("r+b")
        try:
            self.handle.seek(0, os.SEEK_END)
            if self.handle.tell() == 0:
                self.handle.write(b"0")
                self.handle.flush()
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt
                try:
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise WatchdogAlreadyRunning("Backend watchdog already active") from exc
            else:
                import fcntl
                try:
                    fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    raise WatchdogAlreadyRunning("Backend watchdog already active") from exc
        except BaseException:
            self.handle.close()
            self.handle = None
            raise
        return self

    def __exit__(self, *_):
        if self.handle is not None:
            try:
                if os.name == "nt":
                    import msvcrt
                    self.handle.seek(0)
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.handle, fcntl.LOCK_UN)
            finally:
                self.handle.close()
                self.handle = None


class BackendWatchdog:
    """Detect disappearance and recover it, with crash-loop suppression."""

    def __init__(
        self, project_root: Path, config_dir: Path,
        *, check: Callable[[int, Path], str] = get_backend_state,
        start: Callable[[Path, int, Path], str] = ensure_backend_started,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.project_root = Path(project_root)
        self.config_dir = Path(config_dir)
        self.check = check
        self.start = start
        self.clock = clock
        self.attempts: deque[float] = deque()

    def tick(self, port: int) -> str:
        state = self.check(port, self.config_dir)
        if state != "STOPPED":
            # Crucially do not attempt to take over an unknown port listener.
            return state
        now = self.clock()
        while self.attempts and now - self.attempts[0] > RESTART_WINDOW_SECONDS:
            self.attempts.popleft()
        if len(self.attempts) >= MAX_RESTART_ATTEMPTS:
            return "RESTART_CIRCUIT_OPEN"
        self.attempts.append(now)
        try:
            return self.start(self.project_root, port, self.config_dir)
        except Exception:
            logging.getLogger("dic.watchdog").exception("Backend start failed")
            return "START_FAILED"


def run_watchdog(project_root: Path, *, interval_seconds: float = WATCHDOG_INTERVAL_SECONDS) -> None:
    if os.name != "nt":
        raise RuntimeError("The persistent background watchdog requires Windows")
    if not 5 <= interval_seconds <= 300:
        raise ValueError("Invalid watchdog polling interval")
    config_dir = user_configuration_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    logdir = config_dir / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("dic.watchdog")
    logger.setLevel(logging.INFO)
    handler = RotatingFileHandler(logdir / "backend_watchdog.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    stop_file = config_dir / "backend_watchdog.stop"
    try:
        with SingleInstanceLock(config_dir / "backend_watchdog.lock"):
            watchdog = BackendWatchdog(project_root, config_dir)
            last_status = ""
            while not stop_file.exists():
                try:
                    settings = load_settings(config_dir)
                    state = watchdog.tick(settings.api_port)
                except Exception:
                    state = "ERROR"
                    logger.exception("Backend watchdog iteration failed")
                if state != last_status:
                    logger.info("Backend state changed to %s", state)
                    last_status = state
                # Atomic publication; never stores application data or secrets.
                status_path = config_dir / "backend_watchdog_status.json"
                tmp = status_path.with_suffix(".tmp")
                tmp.write_text(json.dumps({"status": state, "updated_at": int(time.time()),
                                           "watchdog_pid": os.getpid()}), encoding="utf-8")
                os.replace(tmp, status_path)
                time.sleep(interval_seconds)
            logger.info("Watchdog requested to stop; backend process left running")
    except WatchdogAlreadyRunning:
        logger.info("Another watchdog holds the single-instance lock")
    finally:
        logger.removeHandler(handler)
        handler.close()


def main() -> None:
    project_root = Path(__file__).resolve().parents[2]
    run_watchdog(project_root)


if __name__ == "__main__":
    main()
