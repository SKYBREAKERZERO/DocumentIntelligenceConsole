"""Actual cancellation/resume and missing-point repair in guarded synthetic data."""
from pathlib import Path
import sys,json
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(Path(__file__).resolve().parent))
from run_isolated_business import context
from isolated_environment import BASE
from app.enterprise.qdrant_service import QdrantService
from app.qabug.qdrant_sync import QABugQdrantManager
from app.qabug.sync_adapter import QABugCurrentQdrantSyncService
from app.rag.qdrant_sync import QdrantSyncCancelled
import psycopg


def main():
    state,profiles,qp,key=context();q=QdrantService(qp,key);name=state['collections']['QABUG']
    kw=dict(host='127.0.0.1',port=15432,dbname='dic_accept_qabug',user='dic_accept',password=state['password'])
    with psycopg.connect(**kw) as conn:
        identity=conn.execute("SELECT inet_server_port(),current_database(),current_setting('data_directory')").fetchone()
        assert identity[:2]==(15432,'dic_accept_qabug') and Path(identity[2]).resolve()==Path(state['cluster']).resolve()
        rows=conn.execute("SELECT c.id,c.qdrant_point_id FROM qabug_chunks c JOIN qabug_records r ON r.id=c.record_id WHERE c.is_current AND r.is_current AND r.is_queryable AND length(btrim(c.content))>0 ORDER BY c.id").fetchall()
    assert 4<len(rows)<1000 and name.startswith('dic_accept_')
    manager=QABugQdrantManager(qdrant_service=q,collection_name=name)
    args=dict(postgres_profile=profiles['QABUG'],password=state['password'],batch_size=2,prune_stale=False)
    assert manager.sync_from_postgres(**args).status=='READY'
    q.delete_points(name,[rows[0][1]])
    repaired=manager.sync_from_postgres(**args)
    assert repaired.points_upserted==1 and repaired.status=='READY'
    with psycopg.connect(**kw) as conn:
        conn.execute("UPDATE public.qabug_chunks SET embedding_status='PENDING' WHERE id=ANY(%s)",([r[0] for r in rows],))
    service=QABugCurrentQdrantSyncService(postgres=profiles['QABUG'],postgres_password=state['password'],qdrant=qp,qdrant_api_key=key,collection_name=name,batch_size=2)
    cancel=[False]
    def progress(event):
        if event['stage']=='qabug_embed':cancel[0]=True
    try:service.sync(full_rebuild=False,cancel_check=lambda:cancel[0],progress=progress)
    except QdrantSyncCancelled:pass
    else:raise AssertionError('Expected actual cancellation')
    cancelled=service.list_runs(limit=1)[0]
    assert cancelled['status']=='CANCELLED' and cancelled['upserted_count']==2,cancelled
    cancel[0]=False
    resumed=service.sync(full_rebuild=False);noop=service.sync(full_rebuild=False)
    assert resumed.status=='READY' and resumed.upserted_count==len(rows)-2
    assert noop.status=='READY' and noop.upserted_count==0
    from app.qabug.quality import QABugDataQualityValidator
    validator=QABugDataQualityValidator(postgres_profile=profiles['QABUG'],password=state['password'],qdrant_service=q,collection_name=name)
    quality=validator.validate();assert quality.status=='PASS',quality
    client=q._client();original=client.retrieve(name,ids=[rows[0][1]],with_payload=True)[0].payload
    try:
        client.set_payload(name,payload={'content':'SYNTHETIC corrupt body with unchanged claimed hash'},points=[rows[0][1]],wait=True)
        corrupted=validator.validate();assert corrupted.status=='FAIL' and corrupted.stale_points==1,corrupted
    finally:client.overwrite_payload(name,payload=original,points=[rows[0][1]],wait=True)
    assert validator.validate().status=='PASS'
    report={'fixture_type':'SYNTHETIC_ONLY','production_sql_written':False,'production_collections_written':False,
        'eligible_chunks':len(rows),'missing_point_with_sql_completed_repaired':repaired.points_upserted,
        'cancelled_audit_status':cancelled['status'],'cancelled_committed_count':cancelled['upserted_count'],
        'resume_upserted':resumed.upserted_count,'second_upserted':noop.upserted_count,
        'actual_dependencies':['PostgreSQL','Qdrant','cached BGE-M3'],'status':'PASS',
        'paged_quality':'PASS','same_hash_corrupt_payload_rejected':True,'quality_snapshot_counts_stable':quality.snapshot_stable}
    (BASE/'streaming_sync_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False),flush=True)


if __name__=='__main__':main()
