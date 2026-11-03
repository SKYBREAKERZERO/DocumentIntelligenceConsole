"""Private loopback PostgreSQL cluster for acceptance. Never connects to production SQL.

Setup creates only a new workspace directory; stop targets that exact cluster.
Qdrant collection names are generated here but created by the acceptance runner.
Private state contains a test-only password and is excluded from release packages.
"""
from pathlib import Path
import argparse
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
BASE=ROOT/"output/isolated_acceptance"
STATE=BASE/"private_state.json"


def load_state():
    data=json.loads(STATE.read_text(encoding="utf-8"))
    folder=Path(data["cluster"]).resolve()
    if folder.parent!=BASE.resolve() or folder.name!="pgdata" or data["port"]!=15432:
        raise ValueError("Unexpected acceptance cluster identity")
    if data["host"]!="127.0.0.1" or data["user"]!="dic_accept":
        raise ValueError("Unexpected acceptance connection")
    return data


def command(binary,*args):
    exe=shutil.which(binary)
    if not exe:raise RuntimeError(f"PostgreSQL tool unavailable: {binary}")
    # Windows daemon children inherit pipes; communicate() would wait for the
    # long-lived postgres process even after pg_ctl itself exits successfully.
    with (BASE/(binary+"-command.log")).open("ab") as log:
        result=subprocess.run([exe,*map(str,args)],stdout=log,stderr=log,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0,timeout=90)
    if result.returncode:
        raise RuntimeError(f"{binary} failed; inspect the local acceptance log")


def setup():
    if STATE.exists():raise RuntimeError("Acceptance state already exists; use status or resume, never overwrite")
    with socket.socket() as sock:
        if sock.connect_ex(("127.0.0.1",15432))==0:
            raise RuntimeError("Acceptance port is occupied; refusing to reuse another cluster")
    BASE.mkdir(parents=True,exist_ok=True)
    folder=(BASE/"pgdata").resolve()
    if folder.exists():raise RuntimeError("Existing cluster directory requires inspection")
    password=secrets.token_urlsafe(36)
    pwfile=BASE/"pg_password.private"
    pwfile.write_text(password+"\n",encoding="ascii")
    command("initdb","-D",folder,"-U","dic_accept","--encoding=UTF8","--locale=C",
            "--auth=scram-sha-256","--pwfile",pwfile)
    with (folder/"postgresql.conf").open("a",encoding="utf-8") as f:
        f.write("\nlisten_addresses='127.0.0.1'\nport=15432\nmax_connections=20\nshared_buffers='64MB'\n")
    data={"host":"127.0.0.1","port":15432,"user":"dic_accept","password":password,
          "cluster":str(folder),"run_id":uuid.uuid4().hex[:12],
          "databases":{s:"dic_accept_"+s.lower() for s in ("21MM","24MM","COMMON","QABUG")}}
    data["collections"]={s:f"dic_accept_{data['run_id']}_{s.lower()}" for s in data["databases"]}
    STATE.write_text(json.dumps(data,indent=2),encoding="utf-8")
    start(data)
    initialize(data)


def start(data):
    command("pg_ctl","-D",data["cluster"],"-l",BASE/"postgres.log","-w","start")


def initialize(data):
    import psycopg
    from psycopg import sql
    from app.storage.schema_manager import SchemaManager
    from app.qabug.schema_manager import QABugSchemaManager
    kwargs={k:data[k] for k in ("host","port","user","password")}
    kwargs["connect_timeout"]=5
    with psycopg.connect(**kwargs,dbname="postgres",autocommit=True) as conn:
        for name in data["databases"].values():
            if not conn.execute("SELECT 1 FROM pg_database WHERE datname=%s",(name,)).fetchone():
                conn.execute(sql.SQL("CREATE DATABASE {} ENCODING 'UTF8'").format(sql.Identifier(name)))
    for scope,name in data["databases"].items():
        if scope=="QABUG":
            report=QABugSchemaManager(**kwargs,database=name).ensure_schema(create_database=False)
            migrations=["upgrade_bug_spec_candidates_v1_38.sql"]
        else:
            report=SchemaManager(**kwargs,database=name,project_code=scope,project_name="Acceptance "+scope).ensure_schema()
            migrations=["upgrade_engineering_intelligence_v7.sql","upgrade_engineering_stage1_v2.sql",
                        "upgrade_engineering_stage1_v3.sql","upgrade_engineering_stage1_v4.sql"] if scope!="COMMON" else []
        if not report.ready:raise RuntimeError("Isolated schema not ready: "+scope)
        with psycopg.connect(**kwargs,dbname=name,autocommit=True) as conn:
            for migration in migrations:
                conn.execute((ROOT/"database"/migration).read_text(encoding="utf-8-sig"))
        print(json.dumps({"scope":scope,"database":name,"schema":"READY","migrations":migrations}),flush=True)
    print("Isolated SQL ready on loopback port 15432; no production SQL connection used.")


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action",choices=("setup","initialize","start","serve","stop","status"))
    action=parser.parse_args().action
    if action=="setup":setup()
    else:
        state=load_state()
        if action=="initialize":initialize(state)
        elif action=="start":start(state)
        elif action=="serve":
            start(state)
            print("Acceptance cluster active on 127.0.0.1:15432",flush=True)
            try:
                while (Path(state["cluster"])/"postmaster.pid").is_file():time.sleep(1)
            finally:
                if (Path(state["cluster"])/"postmaster.pid").is_file():
                    command("pg_ctl","-D",state["cluster"],"-m","fast","-w","stop")
        elif action=="stop":command("pg_ctl","-D",state["cluster"],"-m","fast","-w","stop")
        else:
            import psycopg
            with psycopg.connect(**{k:state[k] for k in ("host","port","user","password")},dbname="postgres") as conn:
                print(json.dumps({"identity":conn.execute("SELECT current_user,current_setting('port'),current_setting('data_directory')").fetchone(),
                                  "databases":state["databases"],"collections":state["collections"]}))
