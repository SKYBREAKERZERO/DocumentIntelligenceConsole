"""Batch, crash/retry, race and bounded-allocation checks; no live services."""
from contextlib import contextmanager
from types import SimpleNamespace
import hashlib,tracemalloc
import pytest
from app.qabug.qdrant_sync import QABugQdrantManager,QABugQdrantError
from app.qabug.streaming_sync import make_payload
from app.enterprise.profiles import PostgresProfile
from app.rag.qdrant_sync import QdrantSyncCancelled


class Result:
    def __init__(self,rows=()):self.rows=list(rows)
    def fetchone(self):return self.rows[0]
    def fetchall(self):return self.rows


class Database:
    def __init__(self,n=7,complete=False):
        self.n=n;self.complete=complete;self.done=set();self.locked=True;self.unlocked=False;self.max_page=0;self.race=False
    def row(self,i):
        text=f"Synthetic source {i}";h=hashlib.sha256(text.encode()).hexdigest()
        return dict(chunk_id=i,record_id=i,qdrant_point_id=i+1000000,content=text,content_hash=h,index_hash=h,
            embedding_status="COMPLETED" if self.complete or i in self.done else "PENDING",embedded_content_hash=h,
            embedding_model="local",embedding_version="v1",issue_key=f"TEST-{i}",section_type="DESCRIPTION")
    def __enter__(self):return self
    def __exit__(self,*args):pass
    @contextmanager
    def transaction(self):
        saved=set(self.done)
        try:yield
        except Exception:self.done=saved;raise
    def execute(self,query,args=()):
        q=" ".join(str(query).split())
        if "pg_try_advisory_lock" in q:return Result([{"locked":self.locked}])
        if "pg_advisory_unlock" in q:self.unlocked=True;return Result([{"ok":True}])
        if "AS ceiling" in q:return Result([{"n":self.n,"ceiling":self.n}])
        if "LIMIT %s" in q:
            last,ceiling,size=args;self.max_page=max(self.max_page,size)
            return Result(self.row(i) for i in range(last+1,min(ceiling,last+size)+1))
        if "FOR UPDATE OF c" in q:
            rows=[]
            for i in args[0]:
                row=self.row(i);row["id"]=i
                if self.race and i==1:
                    if self.race=='body':row['content']='modified without updating stored hash'
                    else:row["index_hash"]="changed"
                rows.append(row)
            return Result(rows)
        if q.startswith("UPDATE public.qabug_chunks"):self.done.add(int(args[-1]));return Result()
        if q.startswith("INSERT INTO public.qabug_embedding_state"):return Result()
        if "SELECT count(*) AS n" in q:return Result([{"n":self.n}])
        if "FOR UPDATE" in q:return Result()
        raise AssertionError(q)


class Client:
    def __init__(self,db):self.db=db;self.points={};self.calls=[];self.ack=True;self.fail_on=None;self.retrieve_max=0
    def retrieve(self,name,ids,**kw):
        self.retrieve_max=max(self.retrieve_max,len(ids));rows=[]
        for pid in ids:
            payload=self.points.get(pid)
            if payload is None and self.db.complete:payload=make_payload(self.db.row(pid-1000000),"local","v1")
            if payload:rows.append(SimpleNamespace(id=pid,payload=payload))
        return rows
    def upsert(self,collection_name,points,wait):
        self.calls.append([p.id for p in points])
        if self.fail_on==len(self.calls):raise OSError("simulated disconnect")
        for p in points:self.points[p.id]=p.payload
        return SimpleNamespace(status="completed" if self.ack else "acknowledged")


def setup(monkeypatch,n=7,complete=False):
    db=Database(n,complete);client=Client(db)
    q=SimpleNamespace(_client=lambda:client,count_collection=lambda name:db.n if db.complete else len(client.points))
    manager=QABugQdrantManager(qdrant_service=q,collection_name="test",vector_dimension=2)
    manager.ensure_contract=lambda:SimpleNamespace(mismatched=())
    monkeypatch.setattr("psycopg.connect",lambda **kw:db)
    embed=SimpleNamespace(embed_texts=lambda texts:[[.1,.2] for t in texts])
    monkeypatch.setattr("app.embedding.embedding_client.EmbeddingClient",lambda config:embed)
    def run(**kw):return manager.sync_from_postgres(postgres_profile=PostgresProfile(),password="test",embedding_model="local",embedding_version="v1",batch_size=2,prune_stale=False,**kw)
    return db,client,embed,run


def test_cancel_retains_committed_batch_and_resumes_without_reembedding(monkeypatch):
    db,client,embed,run=setup(monkeypatch);cancel=[False];events=[]
    with pytest.raises(QdrantSyncCancelled):
        run(progress=lambda *a:cancel.__setitem__(0,True),cancel_check=lambda:cancel[0],checkpoint=events.append)
    assert db.done=={1,2} and db.unlocked
    assert events[-1]["phase"]=="BATCH_COMMITTED" and events[-1]["upserted"]==2
    cancel[0]=False;result=run()
    assert result.points_upserted==5 and result.unchanged_count==2 and result.status=="READY"
    assert run().points_upserted==0


def test_vector_disconnect_does_not_advance_sql_for_failed_batch(monkeypatch):
    db,client,embed,run=setup(monkeypatch);client.fail_on=2
    with pytest.raises(OSError):run()
    assert db.done=={1,2}
    client.fail_on=None
    assert run().points_upserted==5


def test_unacknowledged_vector_write_is_replayed_even_if_point_appears(monkeypatch):
    db,client,embed,run=setup(monkeypatch);client.ack=False
    with pytest.raises(QABugQdrantError,match="acknowledged"):run()
    assert not db.done
    client.ack=True
    assert run().points_upserted==7


def test_source_mutation_during_embedding_is_not_marked_complete(monkeypatch):
    db,client,embed,run=setup(monkeypatch);db.race=True
    result=run()
    assert 1 not in db.done and 1000001 not in client.points
    assert result.status=="PARTIAL" and result.source_changed_count
    db.race=False
    assert run().points_upserted==1


def test_source_body_race_is_rejected_even_when_stored_hash_was_not_updated(monkeypatch):
    db,client,embed,run=setup(monkeypatch);db.race='body'
    result=run()
    assert 1 not in db.done and 1000001 not in client.points and result.status=='PARTIAL'


def test_explicit_limit_retains_remaining_work(monkeypatch):
    db,client,embed,run=setup(monkeypatch)
    result=run(max_chunks=1)
    assert result.limited and result.status=="PARTIAL" and len(db.done)==1
    assert run().points_upserted==6


def test_limit_on_final_page_is_still_partial(monkeypatch):
    db,client,embed,run=setup(monkeypatch,n=2,complete=True)
    result=run(max_chunks=1,force_reembed=True)
    assert result.limited and result.status=="PARTIAL" and len(client.calls[0])==1


@pytest.mark.parametrize("vectors",[[],[[1.0]],[[float('nan'),.2],[.1,.2]]])
def test_malformed_embedding_cannot_create_checkpoint(monkeypatch,vectors):
    db,client,embed,run=setup(monkeypatch);embed.embed_texts=lambda texts:vectors
    with pytest.raises(QABugQdrantError):run()
    assert not db.done and not client.calls


def test_collection_lock_contention_prevents_write(monkeypatch):
    db,client,embed,run=setup(monkeypatch);db.locked=False
    with pytest.raises(QABugQdrantError,match="owns"):run()
    assert not client.calls and not db.done


def test_full_recalculation_keeps_collection_and_updates_all_current_rows(monkeypatch):
    db,client,embed,run=setup(monkeypatch,complete=True)
    assert run(force_reembed=True).points_upserted==7


def test_large_unchanged_source_has_bounded_page_memory(monkeypatch):
    db,client,embed,run=setup(monkeypatch,n=20000,complete=True)
    tracemalloc.start()
    try:
        result=run();_,peak=tracemalloc.get_traced_memory()
    finally:tracemalloc.stop()
    assert result.scanned_count==20000 and not result.points_upserted
    assert db.max_page<=128 and client.retrieve_max<=128
    assert peak<6_000_000
