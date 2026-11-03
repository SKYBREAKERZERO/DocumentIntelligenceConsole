"""Read-only local acceptance inventory; never print credentials or source bodies."""
from pathlib import Path
import json
import sys
import urllib.request
import urllib.error

ROOT=Path(__file__).resolve().parents[2]
PROJECT=ROOT
sys.path.insert(0,str(PROJECT))
from app.server.configuration import user_configuration_dir,load_profiles,load_settings

OUTPUT=ROOT/"live_acceptance"
OUTPUT.mkdir(exist_ok=True)
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
report={"read_only":True,"configuration":{},"http":{},"postgres":{}}


def get(url,key="",timeout=4):
    headers={"Authorization":"Bearer "+key} if key else {}
    req=urllib.request.Request(url,headers=headers)
    try:
        with opener.open(req,timeout=timeout) as r:
            raw=r.read(1_000_000)
            try: data=json.loads(raw)
            except (ValueError,UnicodeError): data={"html":b"<html" in raw.lower(),"bytes":len(raw)}
            return {"status":r.status,"data":data}
    except urllib.error.HTTPError as e:return {"status":e.code}
    except (OSError,urllib.error.URLError) as e:return {"error_type":type(e).__name__}


config=user_configuration_dir()
report["configuration"]={"directory":str(config),"config_exists":(config/"config.yaml").is_file(),
                         "ai_config_exists":(config/"ai_config.json").is_file(),"token_exists":(config/"local_api_token.txt").is_file()}
try:
    settings=load_settings(config)
    report["configuration"].update(api_host=settings.api_host,api_port=settings.api_port,
       webui=settings.external_webui_url,ollama=settings.ollama_url,chat_model=settings.chat_model,
       embedding_model=settings.embedding_model,vector_dimension=settings.vector_dimension,collections=settings.collections)
    tags=get(settings.ollama_url+"/api/tags")
    if "data" in tags:tags["data"]={"models":[{"name":m.get("name"),"size":m.get("size")} for m in tags["data"].get("models",[])]}
    report["http"]["ollama"]=tags
    report["http"]["webui"]=get(settings.external_webui_url+"/")
    token=(config/"local_api_token.txt").read_text(encoding="utf-8-sig").strip() if (config/"local_api_token.txt").is_file() else ""
    api=f"http://127.0.0.1:{settings.api_port}"
    for name,path in (("backend_models","/v1/models"),("backend_health","/api/v1/health"),("backend_metrics","/api/v1/runtime/metrics")):
        report["http"][name]=get(api+path,token)
except Exception as e:report["configuration"]["settings_error"]=type(e).__name__
try:
    profiles,password,qprofile,qkey=load_profiles(config)
    req=urllib.request.Request(qprofile.url+"/collections",headers={"api-key":qkey} if qkey else {})
    try:
        with opener.open(req,timeout=4) as r:report["http"]["qdrant"]={"status":r.status,"data":json.load(r)}
    except Exception as e:report["http"]["qdrant"]={"error_type":type(e).__name__}
    import psycopg
    for scope,profile in profiles.items():
        summary={"database":profile.database,"host":profile.host,"port":profile.port,"password_available":bool(password)}
        try:
            kwargs=profile.dsn_kwargs()
            kwargs.update(password=password,connect_timeout=3,options="-c default_transaction_read_only=on -c statement_timeout=4000")
            with psycopg.connect(**kwargs) as conn,conn.cursor() as cur:
                cur.execute("SELECT current_database(),current_setting('transaction_read_only')")
                summary.update(status="REACHABLE",read_only=cur.fetchone()[1])
                cur.execute("""SELECT c.relname,c.reltuples::bigint FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                  WHERE n.nspname='public' AND c.relkind='r' AND c.relname=ANY(%s) ORDER BY c.relname""",
                  (["documents","contents","specifications","specification_versions","specification_change_sets",
                    "engineering_relations","qabug_records","qabug_chunks","ei_requirement_units","ei_content_relations"],))
                summary["estimated_rows"]={name:count for name,count in cur.fetchall()}
        except Exception as e:summary.update(status="UNAVAILABLE",error_type=type(e).__name__,sqlstate=getattr(e,"sqlstate",None))
        report["postgres"][scope]=summary
except Exception as e:report["configuration"]["profile_error"]=type(e).__name__
(OUTPUT/"inventory.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
print(json.dumps(report,ensure_ascii=True,indent=2))
