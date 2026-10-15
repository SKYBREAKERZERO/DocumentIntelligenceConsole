"""Stage 1 v1.29 local preflight; live checks require explicit flags."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.engineering_intelligence.stage1_environment_smoke import (
    inspect_dependency_manifest, probe_gui_import, probe_postgresql_readonly,
)
from scripts.stage1_release_readiness import _exclusive_write


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage 1 v1.29 local/opt-in read-only acceptance smoke")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--check-gui-import", action="store_true")
    parser.add_argument("--check-db", action="store_true")
    parser.add_argument("--db-host")
    parser.add_argument("--db-port", type=int, default=5432)
    parser.add_argument("--db-name")
    parser.add_argument("--db-user")
    parser.add_argument("--db-password-env", default="DIC_STAGE1_RO_PASSWORD")
    args = parser.parse_args(argv)
    if args.check_db and (not args.db_host or not args.db_name or not args.db_user):
        parser.error("--check-db requires --db-host --db-name --db-user")
    if not args.check_db and any((args.db_host, args.db_name, args.db_user)):
        parser.error("Database fields require explicit --check-db")
    if not args.db_password_env.startswith("DIC_") or not args.db_password_env.isidentifier():
        parser.error("Use a DIC_ prefixed password environment variable")
    try:
        manifest = inspect_dependency_manifest(ROOT / "requirements.txt")
        if args.check_db:
            password = os.getenv(args.db_password_env, "")
            db = probe_postgresql_readonly(host=args.db_host, port=args.db_port,
                                           database=args.db_name, user=args.db_user,
                                           password=password)
        else:
            db = {"status": "NOT_REQUESTED", "verification": "NOT_EXECUTED"}
        gui = probe_gui_import(ROOT) if args.check_gui_import else {
            "status": "NOT_REQUESTED", "verification": "NOT_EXECUTED"}
        report = {
            "schema": "stage1-v129-environment-smoke-v1", "release": "v1.29",
            "dependency_manifest": manifest, "windows_gui_import": gui,
            "postgresql_readonly_probe": db,
            "windows_gui_interactive_accepted": False,
            "database_source_chain_verified_by_this_probe": False,
            "human_gold_verified_by_this_probe": False,
            "stage1_production_acceptance": "NOT_VERIFIED",
        }
        _exclusive_write(args.out, report)
    except (OSError, ValueError, TypeError):
        print(json.dumps({"status": "NOT_VERIFIED", "error": "PRECHECK_FAILED_REDACTED"}), file=sys.stderr)
        return 2
    print(json.dumps({"report_written": True, "dependency_manifest": manifest["status"],
                      "gui_import": gui["status"], "postgresql_readonly": db["status"],
                      "production_acceptance": "NOT_VERIFIED"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
