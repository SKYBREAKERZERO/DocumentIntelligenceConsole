"""Single-host resource protection and safe operational counters.

No prompts, document text, issue keys or credentials are recorded in metrics.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


class AdmissionController:
    """Bound expensive chats; reject overload instead of exhausting local LLM memory."""

    def __init__(self, max_active: int = 2):
        if not 1 <= max_active <= 8:
            raise ValueError("max_active must be between 1 and 8")
        self.max_active = max_active
        self._lock = threading.Lock()
        self._active = 0
        self._peak = 0
        self._accepted = 0
        self._rejected = 0
        self._completed = 0
        self._failed = 0
        self._started = time.monotonic()

    def acquire(self) -> RequestLease | None:
        with self._lock:
            if self._active >= self.max_active:
                self._rejected += 1
                return None
            self._active += 1
            self._accepted += 1
            self._peak = max(self._peak, self._active)
        return RequestLease(self)

    def _finish(self, *, failed: bool) -> None:
        with self._lock:
            if self._active == 0:
                raise RuntimeError("Invalid admission release")
            self._active -= 1
            self._completed += 1
            if failed:
                self._failed += 1

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "max_active": self.max_active,
                "active": self._active,
                "peak_active": self._peak,
                "accepted": self._accepted,
                "rejected": self._rejected,
                "completed": self._completed,
                "failed": self._failed,
                "uptime_seconds": int(time.monotonic() - self._started),
            }


@dataclass(slots=True)
class RequestLease:
    controller: AdmissionController
    _released: bool = field(default=False, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def release(self, *, failed: bool = False) -> None:
        """Idempotent: callable by both stream-finally and response background cleanup."""
        with self._lock:
            if self._released:
                return
            self._released = True
            self.controller._finish(failed=failed)
