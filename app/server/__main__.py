from __future__ import annotations

import argparse
import os

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
    args = parser.parse_args()
    config_dir = user_configuration_dir()
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
