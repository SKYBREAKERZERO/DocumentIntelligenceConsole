"""Restore schema/project metadata after an intentionally cleared project DB."""
from pathlib import Path
import sys
import psycopg

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from app.server.configuration import load_profiles,user_configuration_dir
from app.storage.schema_manager import SchemaManager
from app.storage.configuration_schema_manager import ConfigurationSchemaManager

profiles,password,_,_=load_profiles(user_configuration_dir())
for scope in ("21MM","24MM"):
    profile=profiles[scope]
    if profile.database not in {"rag_21mm","rag_24mm"}:
        raise RuntimeError(f"Unexpected database: {profile.database}")
    kwargs=profile.dsn_kwargs();kwargs["password"]=password
    with psycopg.connect(**kwargs) as conn,conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM public.documents")
        if int(cur.fetchone()[0]) != 0:
            raise RuntimeError(f"Refusing nonempty database: {profile.database}")
    common=dict(host=profile.host,port=profile.port,database=profile.database,user=profile.user,
                password=password,connect_timeout=profile.connect_timeout,
                project_code=scope,project_name=scope)
    rag=SchemaManager(**common).ensure_schema()
    config=ConfigurationSchemaManager(**common).ensure_schema()
    print(scope,rag.status_text(),config.status_text())
    with psycopg.connect(**kwargs) as conn,conn.cursor() as cur:
        cur.execute("SELECT project_code,project_name FROM public.project_info")
        print("project_info",cur.fetchall())
