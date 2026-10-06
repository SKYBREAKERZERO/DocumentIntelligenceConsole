from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict

from app.enterprise.profiles import PostgresProfile, QdrantProfile
from app.enterprise.qdrant_service import QdrantService
from app.rag.qdrant_governance import PAYLOAD_INDEX_CONTRACT, QdrantDataQualityValidator
from app.rag.qdrant_sync import CurrentQdrantSyncService


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate/repair enterprise Qdrant current-index governance.")
    parser.add_argument("--scope", required=True, choices=["21MM", "24MM", "COMMON"])
    parser.add_argument("--db", required=True, help="PostgreSQL database name, e.g. rag_21mm")
    parser.add_argument("--pg-host", default="127.0.0.1")
    parser.add_argument("--pg-port", type=int, default=5432)
    parser.add_argument("--pg-user", default="postgres")
    parser.add_argument("--pg-password", default=os.getenv("POSTGRES_PASSWORD", ""))
    parser.add_argument("--qdrant-url", default="http://127.0.0.1:6333")
    parser.add_argument("--qdrant-api-key", default=os.getenv("QDRANT_API_KEY", ""))
    parser.add_argument("--collection", default="")
    parser.add_argument("--embedding-model", default="BAAI/bge-m3")
    parser.add_argument("--embedding-version", default="current-index-v1")
    parser.add_argument("--dimension", type=int, default=1024)
    parser.add_argument("--repair-indexes", action="store_true")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON report.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    collection = args.collection or CurrentQdrantSyncService.default_collection_name(args.scope)
    pg = PostgresProfile(
        host=args.pg_host,
        port=args.pg_port,
        database=args.db,
        user=args.pg_user,
        connect_timeout=10,
        sslmode="prefer",
    )
    qprofile = QdrantProfile(url=args.qdrant_url, timeout=30.0)
    qservice = QdrantService(qprofile, args.qdrant_api_key)

    if args.repair_indexes:
        result = qservice.ensure_payload_indexes(
            collection,
            PAYLOAD_INDEX_CONTRACT,
            repair_mismatched=True,
        )
        print(
            f"Payload indexes: created={len(result.created)} repaired={len(result.repaired)} "
            f"unchanged={len(result.unchanged)}"
        )

    validator = QdrantDataQualityValidator(
        postgres=pg,
        postgres_password=args.pg_password,
        qdrant=qservice,
        project_code=args.scope,
        collection_name=collection,
        embedding_model=args.embedding_model,
        embedding_version=args.embedding_version,
        vector_dimension=args.dimension,
    )
    report = validator.validate()
    if args.json:
        print(json.dumps(asdict(report), ensure_ascii=False, indent=2, default=str))
    else:
        for line in report.summary_lines():
            print(line)
        for issue in report.issues:
            suffix = f" content_id={issue.content_id}" if issue.content_id is not None else ""
            print(f"[{issue.severity}] {issue.code}: {issue.message}{suffix}")
    return 0 if report.status != "FAIL" else 2


if __name__ == "__main__":
    raise SystemExit(main())
