from __future__ import annotations

import argparse
import os
from dataclasses import asdict

from app.enterprise.profiles import PostgresProfile, QdrantProfile
from app.rag.qdrant_sync import CurrentQdrantSyncService


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Preview or synchronize PostgreSQL rag_current_chunks to the scope-specific Qdrant current collection."
    )
    parser.add_argument("--project", required=True, choices=("21MM", "24MM", "COMMON"))
    parser.add_argument("--database", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5432)
    parser.add_argument("--user", default="postgres")
    parser.add_argument("--qdrant-url", default="http://127.0.0.1:6333")
    parser.add_argument("--collection", default="")
    parser.add_argument("--model", default="BAAI/bge-m3")
    parser.add_argument("--dimension", type=int, default=1024)
    parser.add_argument("--batch", type=int, default=32)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preview", action="store_true")
    mode.add_argument("--full-rebuild", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    password = os.getenv("POSTGRES_PASSWORD", "")
    if not password:
        raise SystemExit("POSTGRES_PASSWORD is required.")
    qdrant_key = os.getenv("QDRANT_API_KEY", "")

    service = CurrentQdrantSyncService(
        postgres=PostgresProfile(
            host=args.host,
            port=args.port,
            database=args.database,
            user=args.user,
        ),
        postgres_password=password,
        qdrant=QdrantProfile(url=args.qdrant_url),
        qdrant_api_key=qdrant_key,
        project_code=args.project,
        collection_name=args.collection or None,
        embedding_model=args.model,
        vector_dimension=args.dimension,
        batch_size=args.batch,
    )

    if args.preview:
        plan = service.preview()
        print(
            f"PREVIEW project={plan.project_code} collection={plan.collection_name} "
            f"sql={plan.source_count} qdrant={plan.qdrant_count} "
            f"new={plan.new_count} changed={plan.changed_count} "
            f"unchanged={plan.unchanged_count} stale={plan.stale_count}"
        )
        for warning in plan.warnings:
            print(f"WARNING {warning}")
        return 0

    def progress(event: dict) -> None:
        print(f"[{event.get('percent', 0):5.1f}%] {event.get('message', '')}")

    result = service.sync(full_rebuild=args.full_rebuild, progress=progress)
    print(
        f"DONE run={result.run_id} mode={result.mode} status={result.status} "
        f"upserted={result.upserted_count} deleted={result.deleted_count} "
        f"qdrant={result.final_qdrant_count} elapsed={result.elapsed_seconds:.1f}s"
    )
    for warning in result.warnings:
        print(f"WARNING {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
