"""Read-only paged QA/BUG cross-store validation; no corpus-sized payload maps."""
import hashlib
from app.qabug.streaming_sync import ELIGIBLE


def validate(validator):
    import psycopg
    from psycopg.rows import dict_row
    from app.qabug.quality import QABugQualityReport
    from app.qabug.qdrant_sync import QABUG_PAYLOAD_INDEX_CONTRACT
    from app.rag.qdrant_sync import QdrantSyncCancelled
    def check():
        if validator.cancel_check and validator.cancel_check():raise QdrantSyncCancelled('QA/BUG quality validation cancelled')
    def header(conn):
        return conn.execute(f'SELECT count(*) AS n,coalesce(max(c.id),0) AS ceiling FROM public.qabug_chunks c JOIN public.qabug_records r ON r.id=c.record_id WHERE {ELIGIBLE}').fetchone()
    check();q=validator.qdrant;name=validator.collection_name
    exists=q.collection_exists(name);client=q._client() if exists else None
    before=q.count_collection(name) if exists else 0
    schema=q.payload_schema(name) if exists else {}
    missing_indexes=tuple(k for k in QABUG_PAYLOAD_INDEX_CONTRACT if not schema.get(k))
    mismatched=tuple(k for k,v in QABUG_PAYLOAD_INDEX_CONTRACT.items() if schema.get(k) and schema[k]!=v)
    kw=validator.postgres.dsn_kwargs();kw.update(password=validator.password,options='-c default_transaction_read_only=on -c statement_timeout=30000')
    missing=orphan=stale=scanned=qscanned=bad_source=0;warnings=[];last=0
    with psycopg.connect(**kw,row_factory=dict_row,autocommit=True) as conn:
        records=int(conn.execute('SELECT count(*) AS n FROM public.qabug_records WHERE is_current AND is_queryable').fetchone()['n'])
        quarantined=int(conn.execute('SELECT count(*) AS n FROM public.qabug_records WHERE is_current AND NOT is_queryable').fetchone()['n'])
        initial=header(conn);total=int(initial['n']);ceiling=int(initial['ceiling'])
        while last<ceiling:
            check()
            rows=conn.execute(f'''SELECT c.id,c.qdrant_point_id,c.content_hash,c.index_hash,c.content FROM public.qabug_chunks c
                JOIN public.qabug_records r ON r.id=c.record_id WHERE {ELIGIBLE} AND c.id>%s AND c.id<=%s ORDER BY c.id LIMIT 256''',(last,ceiling)).fetchall()
            if not rows:break
            last=int(rows[-1]['id']);scanned+=len(rows)
            points=client.retrieve(name,ids=[int(r['qdrant_point_id']) for r in rows],with_payload=True,with_vectors=False) if client else []
            payloads={str(p.id):dict(p.payload or {}) for p in points}
            for row in rows:
                payload=payloads.get(str(row['qdrant_point_id']))
                intact=hashlib.sha256(row['content'].encode()).hexdigest()==row['content_hash']
                if not intact:bad_source+=1
                if payload is None:missing+=1;continue
                if (not intact or any(str(row[k] or '')!=str(payload.get(k) or '') for k in ('content_hash','index_hash','content'))
                    or str(payload.get('content_id') or '')!=str(row['id'])):stale+=1
            if validator.progress:validator.progress({'stage':'qabug_quality','scanned':scanned,'total':total})
        if exists:
            offset=None;seen=set()
            while True:
                check();points,next_offset=q.scroll_page(name,limit=512,offset=offset);qscanned+=len(points)
                ids=[int(p['id']) for p in points if str(p['id']).isdigit() and int(p['id'])<=2**63-1]
                eligible={str(row['qdrant_point_id']) for row in conn.execute(f'''SELECT c.qdrant_point_id FROM public.qabug_chunks c
                    JOIN public.qabug_records r ON r.id=c.record_id WHERE {ELIGIBLE} AND c.qdrant_point_id=ANY(%s)''',(ids,)).fetchall()}
                orphan+=sum(str(p['id']) not in eligible for p in points)
                if next_offset is None:break
                marker=repr(next_offset)
                if marker in seen:raise RuntimeError('Repeated Qdrant offset during QA/BUG quality validation')
                seen.add(marker);offset=next_offset
        check();final=header(conn)
    after=q.count_collection(name) if exists else 0
    stable=initial==final and scanned==total and before==after==qscanned
    if quarantined:warnings.append(f'Quarantined non-queryable records: {quarantined}')
    if bad_source:warnings.append(f'SQL source body/hash integrity failures: {bad_source}')
    if not stable:warnings.append('Source or collection counts changed while scanning; repeat validation')
    passed=stable and not (missing or orphan or stale or bad_source or missing_indexes or mismatched)
    return QABugQualityReport('PASS' if passed else 'FAIL',records,total,after,missing,orphan,stale,
        missing_indexes,mismatched,tuple(warnings),quarantined,bad_source,stable)
