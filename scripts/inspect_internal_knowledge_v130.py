"""Read-only local SQL/Qdrant corpus probe; never prints credentials/content."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from app.server.configuration import user_configuration_dir  # noqa: E402
from app.server.runtime import PersistentRAGRuntime  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only local SQL/Qdrant knowledge health check")
    parser.add_argument("--config-dir", type=Path, default=None, help="Directory of the current user's enterprise configuration")
    args = parser.parse_args()
    config_dir = args.config_dir or user_configuration_dir()
    try:
        # Import backend startup policy so transformer auto-download is disabled
        # even when this diagnostic is launched on its own.
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        runtime = PersistentRAGRuntime(config_dir)
        report = runtime.probe_internal_knowledge()
    except Exception as exc:
        report = {"policy": "INTERNAL_ONLY", "summary": "UNAVAILABLE", "error_type": type(exc).__name__}
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report.get("summary") == "READY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
