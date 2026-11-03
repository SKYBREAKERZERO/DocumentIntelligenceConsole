"""Opt-in read-only PostgreSQL -> isolated Qdrant BGE-M3 Sparse sidecar.

Default is PREVIEW. Explicit --write makes Qdrant sidecar changes ONLY.
No PostgreSQL mutation, no dense collection migration or destructive rebuild.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from typing import Any

from app.ai.stage4_sparse import (
    BGEM3SparseEncoder, MODEL_NAME, SPARSE_NAME, ensure_sparse_collection, sparse_collection,
)
from app.storage.primary_document_policy import PrimarySpecificationDocumentPolicy


def fetch_bounded_sources(conn: Any, *, project: str, max_chunks: int) -> list[dict[str, Any]]:
    if not 1 <= max_chunks <= 20000 or project not in {"21MM", "24MM", "COMMON"}:
        raise ValueError("Invalid project or scan cap")
    primary = PrimarySpecificationDocumentPolicy.sql_primary_predicate("d")
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT rc.content_id, rc.document_id, rc.file_name, rc.project_code,
                       rc.specification_code, rc.specification_version, rc.content_hash,
                       rc.content, rc.page_number
                FROM public.rag_current_chunks rc
                JOIN public.documents d ON d.document_id = rc.document_id
                WHERE rc.project_code = %s AND {primary}
                ORDER BY rc.content_id ASC LIMIT %s""",
            (project, max_chunks + 1),
        )
        rows = [dict(r) for r in cur.fetchall()]
    if len(rows) > max_chunks:
        raise ValueError("Index size exceeds --max-chunks; no partial index is permitted")
    ids: set[int] = set()
    for row in rows:
        cid = int(row["content_id"])
        if cid in ids or cid < 0 or not str(row.get("document_id") or "").strip():
            raise ValueError("Invalid/duplicate source identity")
        ids.add(cid)
        if not row.get("content") or not row.get("content_hash"):
            raise ValueError("Source is missing authoritative content/hash")
        if len(str(row["content"])) > 32768:
            raise ValueError("Source content is too long for the bounded sparse encoder")
        if not PrimarySpecificationDocumentPolicy.is_primary(
            file_name=str(row.get("file_name") or ""), metadata=row,
        ):
            raise ValueError("Source query returned non-body document")
    return rows


def make_sparse_points(rows: list[dict[str, Any]], encodings: Any, *, project: str) -> list[dict[str, Any]]:
    if len(rows) != len(encodings):
        raise ValueError("Sparse source/embedding count mismatch")
    result = []
    for row, encoding in zip(rows, encodings, strict=True):
        result.append({
            "id": int(row["content_id"]),
            "vector": {SPARSE_NAME: {"indices": encoding.indices, "values": encoding.values}},
            "payload": {
                "content_id": int(row["content_id"]), "project_code": project,
                "document_id": str(row["document_id"]),
                "file_name": str(row["file_name"]),
                "spec_code": str(row.get("specification_code") or ""),
                "version": str(row.get("specification_version") or ""),
                "content_hash": str(row["content_hash"]),
                "document_role": "PRIMARY", "evidence_role": "PRIMARY_CONTENT",
                "source_view": "public.rag_current_chunks", "index_scope": "CURRENT",
                "sparse_model": MODEL_NAME, "sparse_vector_name": SPARSE_NAME,
            },
        })
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project", required=True, choices=("21MM", "24MM", "COMMON"))
    ap.add_argument("--dense-collection", required=True)
    ap.add_argument("--max-chunks", type=int, default=10000)
    ap.add_argument("--batch-size", type=int, default=8)
    ap.add_argument("--write", action="store_true", help="Explicitly allow sidecar Qdrant writes")
    args = ap.parse_args()
    sidecar = sparse_collection(args.dense_collection)
    dsn = os.environ.get("DIC_STAGE4_PG_DSN", "")
    if not dsn:
        ap.error("Set DIC_STAGE4_PG_DSN for the exact project database (read-only identity)")
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        rows = fetch_bounded_sources(conn, project=args.project, max_chunks=args.max_chunks)
    print(json.dumps({"project": args.project, "sidecar": sidecar, "source_count": len(rows),
                      "mode": "WRITE" if args.write else "PREVIEW", "dense_unchanged": True}))
    if not args.write:
        return 0
    if not rows:
        raise RuntimeError("Refusing to create an empty sparse index")
    if not 1 <= args.batch_size <= 64:
        raise ValueError("batch-size must be 1..64")
    from qdrant_client import QdrantClient
    from qdrant_client.models import PointStruct, SparseVector
    qurl = os.environ.get("DIC_STAGE4_QDRANT_URL", "")
    if not qurl:
        raise RuntimeError("DIC_STAGE4_QDRANT_URL is required for --write")
    client = QdrantClient(url=qurl, api_key=os.environ.get("DIC_STAGE4_QDRANT_KEY") or None, timeout=60)
    ensure_sparse_collection(client, sidecar)
    encoder = BGEM3SparseEncoder(batch_size=args.batch_size)
    total = 0
    for offset in range(0, len(rows), args.batch_size):
        batch = rows[offset:offset + args.batch_size]
        vectors = encoder.encode([str(row["content"]) for row in batch])
        points = make_sparse_points(batch, vectors, project=args.project)
        client.upsert(collection_name=sidecar, points=[PointStruct(
            id=pt["id"], vector={SPARSE_NAME: SparseVector(**pt["vector"][SPARSE_NAME])},
            payload=pt["payload"],
        ) for pt in points], wait=True)
        total += len(points)
    print(json.dumps({"indexed": total, "sidecar": sidecar,
                      "note": "Append/upsert only; old stale IDs are rejected at retrieval by SQL hydration"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
