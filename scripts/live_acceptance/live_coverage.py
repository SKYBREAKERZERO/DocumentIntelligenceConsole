from pathlib import Path
import sys,json
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from app.server.configuration import load_profiles,user_configuration_dir
import psycopg
from psycopg.rows import dict_row
profiles,password,_,_=load_profiles(user_configuration_dir())
report={}
def query(scope,statement):
    kwargs=profiles[scope].dsn_kwargs()
    kwargs.update(password=password,connect_timeout=3,options="-c default_transaction_read_only=on -c statement_timeout=12000")
    try:
        with psycopg.connect(**kwargs,row_factory=dict_row) as conn,conn.cursor() as cur:
            cur.execute(statement)
            return cur.fetchall()
    except Exception as e:return {"error_type":type(e).__name__,"sqlstate":getattr(e,"sqlstate",None)}
report["qabug_current_chunks"]=query("QABUG","SELECT count(*) AS exact_rows FROM public.qabug_current_chunks")
for scope in ("21MM","24MM"):
    report[scope]={
      "relations":query(scope,"SELECT count(*) AS exact_rows FROM public.spec_relations"),
      "relation_columns":query(scope,"SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name='engineering_relation_catalog' ORDER BY ordinal_position"),
      "change_type_counts":query(scope,"SELECT change_type,count(*) AS exact_rows FROM public.specification_changes GROUP BY change_type"),
      "source_current_count":query(scope,"SELECT count(*) AS exact_rows FROM public.rag_current_chunks")}
report["common_current_chunks"]=query("COMMON","SELECT count(*) AS exact_rows FROM public.rag_current_chunks")
(ROOT/"live_acceptance/coverage.json").write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str),encoding="utf-8")
print(json.dumps(report,ensure_ascii=True,indent=2,default=str))
