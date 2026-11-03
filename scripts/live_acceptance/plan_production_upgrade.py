"""Read-only, explicit migration inventory. This command has NO apply mode."""
from pathlib import Path
import hashlib,json,sys,uuid
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from app.server.configuration import load_profiles,user_configuration_dir

ENGINEERING=["upgrade_engineering_intelligence_v7.sql","upgrade_engineering_stage1_v2.sql",
    "upgrade_engineering_stage1_v3.sql","upgrade_engineering_stage1_v4.sql",
    "upgrade_engineering_stage2_v1_33.sql","upgrade_engineering_stage2_v1_37.sql"]
QABUG=["upgrade_bug_spec_candidates_v1_38.sql","upgrade_conformance_checks_v1.sql"]


def main():
    import psycopg
    profiles,password,_,_=load_profiles(user_configuration_dir())
    report={"mode":"READ_ONLY_PLAN","production_changes_executed":False,"automatic_apply":False,"scopes":{},
        "preconditions":["Verified database and Qdrant backups with restore rehearsal",
            "Reviewed version/body identity catalog and exact target configuration",
            "Explicit operator approval before production DDL or vector writes",
            "Separate reviewed non-login function owner and human reviewer enrollment",
            "Do not approve enterprise relations or software/spec mapping automatically"],
        "rollback":"Preserve original data; disable new routes if necessary. Restore derived state only from verified backups; never auto-drop source tables."}
    for scope in ("21MM","24MM","QABUG"):
        kwargs=profiles[scope].dsn_kwargs();kwargs.update(password=password,options="-c default_transaction_read_only=on -c statement_timeout=5000")
        names=ENGINEERING if scope!="QABUG" else QABUG
        with psycopg.connect(**kwargs) as conn:
            tables=conn.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' AND (tablename LIKE 'ei_%' OR tablename IN ('qabug_spec_candidates','qabug_software_spec_versions')) ORDER BY tablename").fetchall()
            report["scopes"][scope]={"database":profiles[scope].database,"server_version":conn.info.server_version,
                "existing_derived_tables":[r[0] for r in tables],
                "candidate_migrations":[{"file":"database/"+name,"sha256":hashlib.sha256((ROOT/"database"/name).read_bytes()).hexdigest()} for name in names],
                "migration_selection":"Review prerequisites and installed objects; not an unconditional execution list",
                "production_apply_performed":False}
    folder=ROOT/"output/production_plan"/uuid.uuid4().hex;folder.mkdir(parents=True)
    (folder/"plan.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"plan":str(folder/"plan.json"),"mode":report["mode"],"production_changes_executed":False}))


if __name__=="__main__":main()
