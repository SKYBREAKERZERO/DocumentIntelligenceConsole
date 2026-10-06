from __future__ import annotations

import argparse
import json
from dataclasses import asdict
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.enterprise.profiles import PostgresProfile, QdrantProfile
from app.enterprise.qdrant_service import QdrantService
from app.qabug.canonical import QABugCanonicalizer
from app.qabug.importer import QABugImportPipeline
from app.qabug.qdrant_sync import QABugQdrantManager
from app.qabug.schema_manager import QABugSchemaManager
from app.qabug.quality import QABugDataQualityValidator


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Enterprise QA/BUG RAG administration")
    sub = p.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--host", default="127.0.0.1")
    common.add_argument("--port", type=int, default=5432)
    common.add_argument("--database", default="rag_qabug")
    common.add_argument("--user", default="postgres")
    common.add_argument("--password", default=os.getenv("POSTGRES_PASSWORD", ""))

    sub.add_parser("init-db", parents=[common], help="Create/upgrade the dedicated QA/BUG database safely")

    exp = sub.add_parser("export-jsonl", help="Normalize a QA/BUG register to canonical JSONL without DB writes")
    exp.add_argument("input")
    exp.add_argument("--type", choices=("QA","BUG"), required=True)
    exp.add_argument("--output", required=True)
    exp.add_argument("--max-records", type=int)

    imp = sub.add_parser("import-sql", parents=[common], help="Normalize and idempotently upsert into PostgreSQL")
    imp.add_argument("input")
    imp.add_argument("--type", choices=("QA","BUG"), required=True)
    imp.add_argument("--max-records", type=int)

    vec = sub.add_parser("sync-qdrant", parents=[common], help="Embed pending QA/BUG chunks and upsert Qdrant")
    vec.add_argument("--qdrant-url", default="http://127.0.0.1:6333")
    vec.add_argument("--qdrant-api-key", default=os.getenv("QDRANT_API_KEY", ""))
    vec.add_argument("--collection", default="rag_qabug_current_chunks")
    vec.add_argument("--batch-size", type=int, default=16)
    vec.add_argument("--max-chunks", type=int)

    dq = sub.add_parser("validate", parents=[common], help="Validate SQL/Qdrant QA/BUG consistency")
    dq.add_argument("--qdrant-url", default="http://127.0.0.1:6333")
    dq.add_argument("--qdrant-api-key", default=os.getenv("QDRANT_API_KEY", ""))
    dq.add_argument("--collection", default="rag_qabug_current_chunks")

    map_p = sub.add_parser("mapping", help="Print source-to-canonical field mapping")
    map_p.add_argument("input")
    return p


def main() -> int:
    args = parser().parse_args()
    pipeline = QABugImportPipeline(QABugCanonicalizer())
    if args.command == "export-jsonl":
        report = pipeline.export_jsonl(args.input,args.output,record_type=args.type,max_records=args.max_records,
            progress=lambda n,key: print(f"normalized={n} last={key}", file=sys.stderr))
        print(json.dumps(asdict(report), ensure_ascii=False, indent=2)); return 0
    if args.command == "mapping":
        print(json.dumps(pipeline.field_mapping_report(args.input), ensure_ascii=False, indent=2)); return 0
    if args.command == "init-db":
        report = QABugSchemaManager(host=args.host,port=args.port,database=args.database,user=args.user,password=args.password).ensure_schema()
        print(report.status_text());
        for w in report.warnings: print("WARN:",w)
        return 0 if report.ready else 2
    pg = PostgresProfile(host=args.host,port=args.port,database=args.database,user=args.user)
    if args.command == "import-sql":
        report = pipeline.import_postgres(args.input,record_type=args.type,profile=pg,password=args.password,max_records=args.max_records,
            progress=lambda n,key: print(f"upsert progress={n} last={key}", file=sys.stderr))
        print(json.dumps(asdict(report), ensure_ascii=False, indent=2)); return 0 if report.ok else 2
    q = QdrantService(QdrantProfile(url=args.qdrant_url,timeout=30),args.qdrant_api_key)
    if args.command == "sync-qdrant":
        report = QABugQdrantManager(qdrant_service=q,collection_name=args.collection).sync_from_postgres(
            postgres_profile=pg,password=args.password,batch_size=args.batch_size,max_chunks=args.max_chunks,
            progress=lambda n,total,key: print(f"qdrant={n}/{total} last={key}", file=sys.stderr))
        print(json.dumps(asdict(report), ensure_ascii=False, indent=2)); return 0
    if args.command == "validate":
        report = QABugDataQualityValidator(postgres_profile=pg,password=args.password,qdrant_service=q,collection_name=args.collection).validate()
        print(json.dumps(asdict(report), ensure_ascii=False, indent=2)); return 0 if report.status == "PASS" else 2
    return 2

if __name__ == "__main__":
    raise SystemExit(main())
