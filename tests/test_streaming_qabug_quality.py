from types import SimpleNamespace
import hashlib,tracemalloc
import pytest
from app.enterprise.profiles import PostgresProfile
from app.qabug.quality import QABugDataQualityValidator
from app.qabug.qdrant_sync import QABUG_PAYLOAD_INDEX_CONTRACT
from app.rag.qdrant_sync import QdrantSyncCancelled


class Rows:
    def __init__(self,rows):self.rows=list(rows)
    def fetchone(self):return self.rows[0]
    def fetchall(self):return self.rows


class Stores:
    def __init__(self,n=3):
        self.n=n;self.missing=set();self.corrupt=set();self.orphans=[];self.read_max=0;self.drift=False;self.counts=0
    def row(self,i):
        text='Synthetic requirement '+str(i);h=hashlib.sha256(text.encode()).hexdigest()
        return {'id':i,'qdrant_point_id':i,'content':text,'content_hash':h,'index_hash':h}
    def __enter__(self):return self
    def __exit__(self,*a):pass
    def execute(self,query,args=()):
        if 'NOT is_queryable' in query:return Rows([{'n':0}])
        if 'AS ceiling' in query:return Rows([{'n':self.n,'ceiling':self.n}])
        if 'LIMIT 256' in query:
            last,ceiling=args;self.read_max=max(self.read_max,min(256,ceiling-last))
            return Rows(self.row(i) for i in range(last+1,min(ceiling,last+256)+1))
        if 'ANY(%s)' in query:return Rows({'qdrant_point_id':i} for i in args[0] if 1<=i<=self.n)
        if 'count(*)' in query:return Rows([{'n':self.n}])
        raise AssertionError(query)
    def collection_exists(self,name):return True
    def payload_schema(self,name):return QABUG_PAYLOAD_INDEX_CONTRACT
    def count_collection(self,name):
        self.counts+=1
        return self.n-len(self.missing)+len(self.orphans)+(1 if self.drift and self.counts>1 else 0)
    def _client(self):return self
    def retrieve(self,name,ids,**kwargs):
        result=[]
        for i in ids:
            if i in self.missing:continue
            row=self.row(i);payload={**row,'content_id':i}
            if i in self.corrupt:payload['content']='CORRUPTED WITHOUT CHANGING CLAIMED HASH'
            result.append(SimpleNamespace(id=i,payload=payload))
        return result
    def scroll_page(self,name,limit,offset):
        last=offset or 0;end=min(self.n,last+limit)
        page=[{'id':i,'payload':{}} for i in range(last+1,end+1) if i not in self.missing]
        if end==self.n:page.extend({'id':i,'payload':{}} for i in self.orphans)
        return page,end if end<self.n else None


def validator(monkeypatch,n=3):
    stores=Stores(n)
    def connect(**kw):
        assert 'default_transaction_read_only=on' in kw['options'] and kw['autocommit']
        return stores
    monkeypatch.setattr('psycopg.connect',connect)
    return stores,QABugDataQualityValidator(postgres_profile=PostgresProfile(),password='test',qdrant_service=stores)


def test_quality_detects_missing_orphan_uuid_and_body_corruption(monkeypatch):
    stores,checker=validator(monkeypatch)
    assert checker.validate().status=='PASS'
    stores.missing={1};stores.corrupt={2};stores.orphans=['8e13c049-04ba-4465-94aa-73aa440d3495']
    result=checker.validate()
    assert result.status=='FAIL' and result.missing_points==1 and result.stale_points==1 and result.orphan_points==1


def test_quality_reports_unstable_counts_and_cancels_between_pages(monkeypatch):
    stores,checker=validator(monkeypatch,n=600);stores.drift=True
    result=checker.validate();assert result.status=='FAIL' and not result.snapshot_stable
    cancelled=[False];checker.cancel_check=lambda:cancelled[0]
    checker.progress=lambda e:cancelled.__setitem__(0,True)
    with pytest.raises(QdrantSyncCancelled):checker.validate()


def test_large_quality_scan_does_not_accumulate_source_or_vector_payloads(monkeypatch):
    stores,checker=validator(monkeypatch,n=30000)
    tracemalloc.start()
    try:
        result=checker.validate();_,peak=tracemalloc.get_traced_memory()
    finally:tracemalloc.stop()
    assert result.status=='PASS' and result.sql_chunks==30000 and stores.read_max<=256
    assert peak<6_000_000
