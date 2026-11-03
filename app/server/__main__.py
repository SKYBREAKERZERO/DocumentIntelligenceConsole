from __future__ import annotations

import argparse
import os
from pathlib import Path

# Strict local inference: block automatic model downloads from public hubs.
# Pre-cache BGE-M3 locally before enabling this security policy.
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_DATASETS_OFFLINE"] = "1"

import uvicorn

from app.server.api import create_app
from app.server.configuration import api_token, load_settings, user_configuration_dir, require_local_host
from app.server.runtime import PersistentRAGRuntime


def main() -> None:
    parser = argparse.ArgumentParser(description="Local-only DocumentIntelligence backend")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--no-warmup", action="store_true")
    parser.add_argument("--config-dir",type=Path,default=None,help="Explicit isolated/operator configuration directory")
    args = parser.parse_args()
    config_dir = args.config_dir.resolve() if args.config_dir else user_configuration_dir()
    if args.config_dir is not None:
        # An explicitly selected isolated configuration must not inherit a
        # production CONFIG_FILE from the launching shell. Process-local only.
        os.environ["CONFIG_FILE"]=str(config_dir/"config.yaml")
    settings = load_settings(config_dir)
    # Strictly refuse 0.0.0.0 and LAN bindings; LAN is a separate, audited phase.
    host = require_local_host(settings.api_host)
    port = args.port or settings.api_port
    token = api_token(config_dir)
    runtime = PersistentRAGRuntime(config_dir)
    if not args.no_warmup:
        runtime.warmup_async()
    app = create_app(runtime, token)
    print(f"Local backend: http://{host}:{port}/v1; token file: {config_dir / 'local_api_token.txt'}")
    uvicorn.run(app, host=host, port=port, log_level="warning", access_log=False, workers=1, reload=False)


if __name__ == "__main__":
    main()
