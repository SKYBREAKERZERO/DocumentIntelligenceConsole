"""Actual local embedding + Qdrant + SQL hydration probe; no mutations."""
from pathlib import Path
import json,os,sys,time
os.environ.update(HF_HUB_OFFLINE="1",TRANSFORMERS_OFFLINE="1",HF_DATASETS_OFFLINE="1",PGOPTIONS="-c default_transaction_read_only=on -c statement_timeout=15000")
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from app.server.configuration import user_configuration_dir
from app.server.runtime import PersistentRAGRuntime
runtime=PersistentRAGRuntime(user_configuration_dir())
result={"read_only":True}
start=time.monotonic()
try:
    service=runtime.get_service()
    print('Starting actual cached embedding model',flush=True)
    vector=service.retriever._embed("Bluetooth Audio playback silence")
    result.update(embedding_dimension=len(vector),embedding_ms=round((time.monotonic()-start)*1000))
    print(json.dumps({"embedding_dimension":len(vector)}),flush=True)
    client=service.retriever._get_qdrant_client()
    response=client.query_points(collection_name=runtime.settings.collections["21MM"],query=vector,limit=3,with_payload=True)
    hits=[]
    for point in response.points:
        payload=point.payload or {}
        hits.append({"point_id":str(point.id),"score":point.score,"content_id":payload.get("content_id"),
                     "document_id":payload.get("document_id"),"scope":payload.get("project_code")})
    result["raw_qdrant_hits"]=hits
    ids=[int(h["content_id"]) for h in hits if h.get("content_id") is not None]
    hydrated=service.retriever._fetch_chunks("21MM",ids)
    result.update(hydrated_rows=len(hydrated),sample_locators=[{k:r.get(k) for k in ("id","document_id","page_number","section_id")} for r in hydrated])
    try:
        native=service.retriever._semantic_search("Bluetooth Audio playback silence",("21MM",))
        result["native_verified_evidence"]=[{"content_id":x.content_id,"document_id":x.document_id,
           "page_number":x.page_number,"section_id":x.section_id,"chars":len(x.content)} for x in native[:5]]
        result["native_verified_count"]=len(native)
    except Exception as e:
        result["native_error_type"]=type(e).__name__
        result["native_error"]=str(e)[:600]
    result["status"]="PASS" if hits and len(hydrated)==len(ids) and result.get("native_verified_count",0)>0 else "PARTIAL"
except Exception as e:
    message=str(e)
    for secret in (runtime.password,runtime.qdrant_key):
        if secret:message=message.replace(secret,"<redacted>")
    result.update(status="FAIL",error_type=type(e).__name__,error=message[:800])
result["elapsed_ms"]=round((time.monotonic()-start)*1000)
(ROOT/"live_acceptance/vector_probe.json").write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str),encoding="utf-8")
print(json.dumps(result,ensure_ascii=True,indent=2,default=str),flush=True)
