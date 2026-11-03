"""Read-only post-clear verification for the two project databases."""
from pathlib import Path
import sys
import psycopg

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.server.configuration import load_profiles,user_configuration_dir

profiles,password,_,_=load_profiles(user_configuration_dir())
for scope in ("21MM","24MM"):
    kwargs=profiles[scope].dsn_kwargs();kwargs["password"]=password
    with psycopg.connect(**kwargs) as conn, conn.cursor() as cur:
        values={}
        for table in ("documents","contents","specifications","specification_versions"):
            cur.execute(f"SELECT count(*) FROM public.{table}")
            values[table]=int(cur.fetchone()[0])
        cur.execute("SELECT count(*) FROM public.schema_version")
        values["schema_version"]=int(cur.fetchone()[0])
        print(scope,values)
