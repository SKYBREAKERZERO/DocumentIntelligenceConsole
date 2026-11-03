"""Bounded QA/BUG sync with per-batch durable SQL checkpoints.

Never materializes the corpus or collection. Live points, not SQL flags alone,
determine repair work. A crash after vector upsert is safely replayable.
"""
import hashlib
import math

ELIGIBLE="c.is_current AND r.is_current AND r.is_queryable AND length(btrim(c.content))>0"
FIELDS="""c.id AS chunk_id,c.record_id,c.qdrant_point_id,c.chunk_key,c.section_type,c.content,c.content_hash,c.index_hash,c.metadata,
 c.embedding_status,c.embedded_content_hash,c.embedding_model,c.embedding_version,
 r.record_type,r.record_key,r.issue_key,r.issue_id,r.project_key,r.issue_type,r.status_norm,r.status_raw,r.priority_norm,r.priority_raw,
 r.resolution_norm,r.component,r.assignee,r.reporter,r.release_plan_version,r.affected_version,r.software_version,r.grade,
 r.destination,r.model,r.source_row_number,sf.source_file_name"""


def needs_sync(row,payload,model,version):
    return (payload is None or any(str(payload.get(k) or "")!=str(row.get(k) or "") for k in ("content_hash","index_hash"))
        or ("content" in row and str(payload.get("content") or "")!=str(row.get("content") or ""))
        or str(payload.get("content_id") or "")!=str(row.get("chunk_id",row.get("content_id")))
        or str(payload.get("embedding_model") or "")!=model or str(payload.get("embedding_version") or "")!=version
        or str(row.get("embedding_status") or "")!="COMPLETED"
        or str(row.get("embedded_content_hash") or "")!=str(row.get("content_hash") or "")
        or str(row.get("embedding_model") or "")!=model or str(row.get("embedding_version") or "")!=version)


def make_payload(row,model,version):
    fields={k:str(row.get(k) or "") for k in ("record_type","record_key","issue_key","issue_id","project_key","issue_type",
        "status_raw","priority_raw","component","assignee","reporter","software_version","grade","destination","model","content_hash","index_hash","content")}
    fields.update(artifact_type="QABUG_REGISTER",status=str(row.get("status_norm") or "UNSPECIFIED"),
        priority=str(row.get("priority_norm") or "UNSPECIFIED"),resolution=str(row.get("resolution_norm") or "UNSPECIFIED"),
        release_version=str(row.get("release_plan_version") or ""),affected_version=str(row.get("affected_version") or ""),
        section_type=str(row.get("section_type") or "CONTENT"),source_file=str(row.get("source_file_name") or ""),
        source_row=int(row.get("source_row_number") or 0),embedding_model=model,embedding_version=version,
        index_scope="CURRENT",source_view="public.qabug_current_chunks",content_id=int(row["chunk_id"]))
    return fields


def sync(manager,*,postgres_profile,password,embedding_model,embedding_version,batch_size,max_chunks,progress,prune_stale,
         cancel_check=None,force_reembed=False,checkpoint=None):
    import psycopg
    from psycopg.rows import dict_row
    from qdrant_client.models import PointStruct
    from app.embedding.embedding_client import EmbeddingClient,EmbeddingClientConfig
    from app.qabug.qdrant_sync import QABugQdrantError,QABugQdrantReport,QABUG_PAYLOAD_INDEX_CONTRACT
    from app.rag.qdrant_sync import QdrantSyncCancelled
    if int(batch_size)<1:raise ValueError("batch_size must be positive")
    if max_chunks is not None and int(max_chunks)<0:raise ValueError("max_chunks cannot be negative")
    size=min(128,int(batch_size));limit=int(max_chunks) if max_chunks else None
    def check():
        if cancel_check and cancel_check():raise QdrantSyncCancelled("QA/BUG sync cancelled; committed batches retained")
    kwargs=postgres_profile.dsn_kwargs();kwargs.update(password=password,options="-c statement_timeout=30000 -c lock_timeout=3000")
    lock_key=int.from_bytes(hashlib.sha256((postgres_profile.database+"/"+manager.collection_name).encode()).digest()[:8],"big",signed=True)
    client=manager.qdrant._client();embedder=None
    upserted=scanned=unchanged=changed=0;limited=False;warnings=[]
    with psycopg.connect(**kwargs,row_factory=dict_row,autocommit=True) as conn:
        if not conn.execute("SELECT pg_try_advisory_lock(%s) AS locked",(lock_key,)).fetchone()["locked"]:
            raise QABugQdrantError("Another QA/BUG synchronization owns this collection")
        try:
            if checkpoint:checkpoint({"phase":"LOCK_ACQUIRED","upserted":0})
            check()
            contract=manager.ensure_contract()
            if contract.mismatched:raise QABugQdrantError("Payload contract mismatch; explicit repair required")
            head=conn.execute(f"SELECT count(*) AS n,coalesce(max(c.id),0) AS ceiling FROM public.qabug_chunks c JOIN public.qabug_records r ON r.id=c.record_id WHERE {ELIGIBLE}").fetchone()
            total=int(head["n"]);ceiling=int(head["ceiling"]);last=0
            while last<ceiling:
                check()
                page=list(conn.execute(f"""SELECT {FIELDS} FROM public.qabug_chunks c JOIN public.qabug_records r ON r.id=c.record_id
                    LEFT JOIN public.qabug_source_files sf ON sf.id=r.source_file_id
                    WHERE {ELIGIBLE} AND c.id>%s AND c.id<=%s ORDER BY c.id LIMIT %s""",(last,ceiling,size)).fetchall())
                if not page:break
                last=int(page[-1]["chunk_id"]);scanned+=len(page)
                points=client.retrieve(manager.collection_name,ids=[int(r["qdrant_point_id"]) for r in page],with_payload=True,with_vectors=False)
                by_id={int(p.id):dict(p.payload or {}) for p in points}
                pending=[r for r in page if force_reembed or needs_sync(r,by_id.get(int(r["qdrant_point_id"])),embedding_model,embedding_version)]
                unchanged+=len(page)-len(pending)
                truncated=False
                if limit is not None:
                    remaining=max(0,limit-upserted);truncated=len(pending)>remaining
                    pending=pending[:remaining]
                valid=[]
                for row in pending:
                    if hashlib.sha256(str(row["content"]).encode()).hexdigest()!=str(row.get("content_hash") or ""):
                        changed+=1
                    else:valid.append(row)
                if valid:
                    check()
                    if embedder is None:
                        embedder=EmbeddingClient(EmbeddingClientConfig(model_name=embedding_model,expected_dimension=manager.vector_dimension,
                            batch_size=size,normalize_embeddings=True,show_progress_bar=False,trust_remote_code=False))
                    vectors=embedder.embed_texts([r["content"] for r in valid])
                    if len(vectors)!=len(valid):raise QABugQdrantError("Embedding count mismatch; checkpoint not advanced")
                    normalized=[]
                    for vector in vectors:
                        values=[float(v) for v in vector]
                        if len(values)!=manager.vector_dimension or not all(math.isfinite(v) for v in values):
                            raise QABugQdrantError("Invalid embedding dimension/value; checkpoint not advanced")
                        normalized.append(values)
                    check()
                    with conn.transaction():
                        # Follow importer lock order: parent records, then chunks.
                        conn.execute("SELECT id FROM public.qabug_records WHERE id=ANY(%s) ORDER BY id FOR UPDATE",(sorted({r["record_id"] for r in valid}),)).fetchall()
                        live={int(r["id"]):r for r in conn.execute(f"""SELECT c.id,c.qdrant_point_id,c.content,c.content_hash,c.index_hash FROM public.qabug_chunks c
                            JOIN public.qabug_records r ON r.id=c.record_id WHERE c.id=ANY(%s) AND {ELIGIBLE} ORDER BY c.id FOR UPDATE OF c""",
                            ([r["chunk_id"] for r in valid],)).fetchall()}
                        admitted=[];out=[]
                        for row,vector in zip(valid,normalized):
                            fresh=live.get(int(row["chunk_id"]))
                            if not fresh or any(str(fresh.get(k) or "")!=str(row.get(k) or "") for k in ("qdrant_point_id","content","content_hash","index_hash")):
                                changed+=1;continue
                            admitted.append(row)
                            out.append(PointStruct(id=int(row["qdrant_point_id"]),vector=vector,payload=make_payload(row,embedding_model,embedding_version)))
                        if out:
                            result=client.upsert(collection_name=manager.collection_name,points=out,wait=True)
                            if str(getattr(result,"status","")).split(".")[-1].lower()!="completed":
                                raise QABugQdrantError("Vector write not acknowledged; SQL checkpoint not advanced")
                            for row in admitted:
                                conn.execute("""UPDATE public.qabug_chunks SET embedding_status='COMPLETED',embedding_model=%s,embedding_version=%s,
                                    embedded_content_hash=%s,embedded_at=now(),qdrant_collection=%s,updated_at=now() WHERE id=%s""",
                                    (embedding_model,embedding_version,row["content_hash"],manager.collection_name,row["chunk_id"]))
                                conn.execute("""INSERT INTO public.qabug_embedding_state(chunk_id,qdrant_collection,qdrant_point_id,content_hash,index_hash,
                                    embedding_model,embedding_version,vector_dimension,synced_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,now())
                                    ON CONFLICT(chunk_id) DO UPDATE SET qdrant_collection=excluded.qdrant_collection,qdrant_point_id=excluded.qdrant_point_id,
                                    content_hash=excluded.content_hash,index_hash=excluded.index_hash,embedding_model=excluded.embedding_model,
                                    embedding_version=excluded.embedding_version,vector_dimension=excluded.vector_dimension,synced_at=now()""",
                                    (row["chunk_id"],manager.collection_name,row["qdrant_point_id"],row["content_hash"],row["index_hash"],embedding_model,embedding_version,manager.vector_dimension))
                    upserted+=len(admitted)
                    if checkpoint:checkpoint({"phase":"BATCH_COMMITTED","upserted":upserted,"scanned":scanned,"last_id":last})
                if progress:progress(scanned,total,str(page[-1].get("issue_key") or ""))
                if limit is not None and upserted>=limit:
                    limited=truncated or scanned<total
                    break
            stale=0
            if prune_stale and not limited:
                offset=None;seen=set()
                while True:
                    check()
                    page,next_offset=manager.qdrant.scroll_page(manager.collection_name,limit=512,offset=offset)
                    numeric=[int(p["id"]) for p in page if str(p["id"]).isdigit() and int(p["id"])<=2**63-1]
                    eligible={str(r["qdrant_point_id"]) for r in conn.execute(f"""SELECT c.qdrant_point_id FROM public.qabug_chunks c
                        JOIN public.qabug_records r ON r.id=c.record_id WHERE c.qdrant_point_id=ANY(%s) AND {ELIGIBLE}""",(numeric,)).fetchall()}
                    remove=[p["id"] for p in page if str(p["id"]) not in eligible]
                    check()
                    if remove:
                        stale+=manager.qdrant.delete_points(manager.collection_name,remove)
                        if checkpoint:checkpoint({"phase":"PRUNE_COMMITTED","upserted":upserted,"deleted":stale})
                    if next_offset is None:break
                    marker=repr(next_offset)
                    if marker in seen:raise QABugQdrantError("Repeated Qdrant page offset")
                    seen.add(marker);offset=next_offset
            final=conn.execute(f"SELECT count(*) AS n FROM public.qabug_chunks c JOIN public.qabug_records r ON r.id=c.record_id WHERE {ELIGIBLE}").fetchone()["n"]
            if int(final)!=total or (scanned<total and not limited):changed=max(changed,1)
            if not limited and manager.qdrant.count_collection(manager.collection_name)!=int(final):changed=max(changed,1)
            if changed:warnings.append("SQL source changed or failed integrity verification; repeat incremental sync")
            if limited:warnings.append("Explicit chunk limit reached; remaining batches retained for next run")
            return QABugQdrantReport(collection=manager.collection_name,vector_dimension=manager.vector_dimension,indexed_fields=len(QABUG_PAYLOAD_INDEX_CONTRACT),
                points_upserted=upserted,points_skipped=changed,stale_points_deleted=stale,warnings=tuple(warnings),
                scanned_count=scanned,unchanged_count=unchanged,source_changed_count=changed,limited=limited,snapshot_upper_id=ceiling)
        finally:conn.execute("SELECT pg_advisory_unlock(%s)",(lock_key,))
