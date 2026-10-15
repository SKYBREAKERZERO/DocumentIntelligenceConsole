"""Stage 1 v1.29 offline environment checks and opt-in read-only integration probes.

A successful probe proves only that this invocation executed; never signs off
Windows GUI usability, SQL schema fidelity, data lineage, or human Gold review.
"""
from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable


def inspect_dependency_manifest(path: Path) -> dict[str, Any]:
    """Verify pip-readable UTF-8 and exact requirement syntax before install."""
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 100_000:
        return {"status": "BLOCKED", "reason": "MISSING_OR_OVERSIZED"}
    try:
        raw = path.read_bytes()
        if raw.startswith((b"\xff\xfe", b"\xfe\xff")) or b"\x00" in raw:
            return {"status": "BLOCKED", "reason": "NOT_UTF8"}
        lines = raw.decode("utf-8-sig").splitlines()
        from packaging.requirements import Requirement
        requirements = [Requirement(line.strip()) for line in lines
                        if line.strip() and not line.lstrip().startswith("#")]
        keys = [r.name.lower().replace("_", "-") for r in requirements]
        if not requirements or len(keys) != len(set(keys)):
            return {"status": "BLOCKED", "reason": "EMPTY_OR_DUPLICATE_PACKAGE"}
        pywin32 = next((r for r in requirements if r.name.lower() == "pywin32"), None)
        if pywin32 is None or pywin32.marker is None or (
            pywin32.marker.evaluate({"sys_platform": "linux"}) or not
            pywin32.marker.evaluate({"sys_platform": "win32"})
        ):
            return {"status": "BLOCKED", "reason": "PYWIN32_PLATFORM_MARKER_INVALID"}
        return {"status": "PASS", "count": len(requirements),
                "pywin32_windows_only": True, "is_exact_pinned": all(
                    len(r.specifier) == 1 and str(r.specifier).startswith("==") for r in requirements)}
    except (UnicodeError, ValueError, OSError, ImportError):
        return {"status": "BLOCKED", "reason": "INVALID_MANIFEST"}


def probe_gui_import(root: Path, *, platform_name: str | None = None,
                     runner: Callable[..., Any] = subprocess.run) -> dict[str, str]:
    """Windows-only noninteractive import probe; does not open Tk windows."""
    if (platform_name if platform_name is not None else sys.platform) != "win32":
        return {"status": "NOT_WINDOWS", "verification": "NOT_EXECUTED"}
    if root.is_symlink() or not (root / "app/gui/application.py").is_file():
        return {"status": "BLOCKED", "verification": "PACKAGE_MISSING"}
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root)
    try:
        result = runner([sys.executable, "-c", "import app.gui.application; print('GUI_IMPORT_OK')"],
                        env=env, cwd=str(root), capture_output=True, text=True, timeout=25, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return {"status": "BLOCKED", "verification": "IMPORT_PROBE_FAILED"}
    passed = result.returncode == 0 and result.stdout.strip().splitlines()[-1:] == ["GUI_IMPORT_OK"]
    return {"status": "PASS" if passed else "BLOCKED", "verification":
            "IMPORT_ONLY_NOT_INTERACTIVE_GUI" if passed else "IMPORT_PROBE_FAILED"}


def probe_postgresql_readonly(*, host: str, port: int, database: str, user: str,
                              password: str, timeout: int = 5,
                              connector: Callable[..., Any] | None = None) -> dict[str, str]:
    """Explicit opt-in only, transaction READ ONLY + rollback; no SQL write operations."""
    if (not isinstance(port, int) or not 1 <= port <= 65535
            or not 1 <= timeout <= 15
            or not isinstance(host, str) or not 0 < len(host) <= 253
            or not re.fullmatch(r"[A-Za-z0-9.:-]+", host)
            or not isinstance(database, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,63}", database)
            or not isinstance(user, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,63}", user)
            or not isinstance(password, str) or not password):
        return {"status": "BLOCKED", "verification": "INVALID_PARAMETERS"}
    if connector is None:
        try:
            import psycopg
        except ImportError:
            return {"status": "BLOCKED", "verification": "PSYCOPG_NOT_INSTALLED"}
        connector = psycopg.connect
    conn = None
    try:
        # Force PostgreSQL into a default read-only state before any SQL executes.
        conn = connector(host=host, port=port, dbname=database, user=user,
                         password=password, connect_timeout=timeout, autocommit=True,
                         options="-c default_transaction_read_only=on -c statement_timeout=5000")
        with conn.cursor() as cursor:
            cursor.execute("BEGIN TRANSACTION READ ONLY")
            try:
                cursor.execute("SELECT current_database(), current_setting('transaction_read_only')")
                row = cursor.fetchone()
            finally:
                cursor.execute("ROLLBACK")
        passed = (isinstance(row, (tuple, list)) and len(row) >= 2
                  and row[0] == database and str(row[1]).lower() in ("on", "true"))
        return {"status": "PASS" if passed else "BLOCKED", "verification":
                "LIVE_READ_ONLY_TRANSACTION" if passed else "DATABASE_IDENTITY_OR_READONLY_MISMATCH"}
    except Exception:
        # In particular: never print connection errors, DSNs, SQL traces or passwords.
        return {"status": "BLOCKED", "verification": "CONNECTION_OR_QUERY_FAILED_REDACTED"}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
