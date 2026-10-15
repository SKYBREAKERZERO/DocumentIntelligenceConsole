"""Windows background watchdog entrypoint; start only after explicit opt-in."""
from __future__ import annotations
import os
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
os.chdir(root)
sys.path.insert(0, str(root))
from app.enterprise.backend_watchdog import run_watchdog  # noqa: E402
run_watchdog(root)
