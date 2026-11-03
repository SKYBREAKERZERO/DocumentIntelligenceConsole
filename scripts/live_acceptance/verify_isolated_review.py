"""Validate controlled review migrations ONLY inside the synthetic private cluster.

Test identities exercise database guards, not human approval of enterprise data.
No production identities, grants or source data are read or changed.
"""
from pathlib import Path
import json,sys
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(Path(__file__).resolve().parent))
from isolated_environment import load_state,BASE
import psycopg
from psycopg import sql


def main():
    state=load_state()
    kwargs={k:state[k] for k in ("host","port","user","password")}
    kwargs.update(dbname=state["databases"]["21MM"],connect_timeout=5)
    owner="dic_accept_review_owner";reviewer="dic_accept_reviewer"
    with psycopg.connect(**kwargs,autocommit=True) as conn:
        for role in (owner,reviewer):
            if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname=%s",(role,)).fetchone():
                conn.execute(sql.SQL("CREATE ROLE {} {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS").format(
                    sql.Identifier(role),sql.SQL("NOLOGIN") if role==owner else sql.SQL("LOGIN PASSWORD {}").format(sql.Literal(state["password"]))))
        conn.execute(sql.SQL("GRANT CREATE ON DATABASE {} TO {}").format(sql.Identifier(kwargs["dbname"]),sql.Identifier(owner)))
        conn.execute(sql.SQL("GRANT ALL ON SCHEMA public TO {}").format(sql.Identifier(owner)))
        conn.execute(sql.SQL("GRANT ALL ON ALL TABLES IN SCHEMA public TO {}").format(sql.Identifier(owner)))
        conn.execute(sql.SQL("GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO {}").format(sql.Identifier(owner)))
        for (table,) in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename LIKE 'ei_%'"):
            conn.execute(sql.SQL("ALTER TABLE public.{} OWNER TO {}").format(sql.Identifier(table),sql.Identifier(owner)))
        conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(owner)))
        for name in ("upgrade_engineering_stage2_v1_33.sql","upgrade_engineering_stage2_v1_37.sql"):
            conn.execute((ROOT/"database"/name).read_text(encoding="utf-8-sig"))
        conn.execute("RESET ROLE")
        conn.execute("""INSERT INTO dic_review.authorized_reviewers(login_name,approved_by,justification)
            VALUES(%s,'SYNTHETIC_TEST_ONLY','Automated fixture validation, not enterprise human review')
            ON CONFLICT(login_name) DO NOTHING""",(reviewer,))
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public,dic_review TO {}").format(sql.Identifier(reviewer)))
        conn.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA public TO {}").format(sql.Identifier(reviewer)))
        conn.execute(sql.SQL("GRANT EXECUTE ON FUNCTION dic_review.apply_content_relation_review(bigint,text,text,text,text,text,text) TO {}").format(sql.Identifier(reviewer)))
        edge=conn.execute("""SELECT r.id,r.status,su.text_hash,tu.text_hash FROM ei_content_relations r
            JOIN ei_requirement_units su ON su.id=r.source_unit_id JOIN ei_requirement_units tu ON tu.id=r.target_unit_id
            JOIN specifications s ON s.id=su.specification_id
            WHERE su.version_id=s.current_version_id ORDER BY r.id LIMIT 1""").fetchone()
        assert edge,"Synthetic current relation candidate missing"
    reviewer_kwargs={**kwargs,"user":reviewer}
    direct_write_blocked=False
    with psycopg.connect(**reviewer_kwargs) as conn:
        try:
            conn.execute("UPDATE ei_content_relations SET status='CONFIRMED' WHERE id=%s",(edge[0],))
        except psycopg.Error:
            direct_write_blocked=True;conn.rollback()
        if edge[1]=="REVIEW_REQUIRED":
            conn.execute("SELECT dic_review.apply_content_relation_review(%s,%s,%s,%s,%s,%s,%s)",
                (edge[0],"21MM","REVIEW_REQUIRED","CONFIRMED",
                 "SYNTHETIC FIXTURE ONLY: exact same-clause reference verified by acceptance assertions",edge[2],edge[3]))
    assert direct_write_blocked
    from app.enterprise.profiles import PostgresProfile,QdrantProfile
    from app.ai.retrieval import EnterpriseRetriever
    profile=PostgresProfile(host="127.0.0.1",port=15432,database=kwargs["dbname"],user="dic_accept")
    retriever=EnterpriseRetriever(postgres_profiles={"21MM":profile},postgres_password=state["password"],
        qdrant=QdrantProfile(),qdrant_api_key="",collections={})
    evidence=retriever._content_relation_search(("21MM",),spec_codes=("550",))
    assert evidence,"Controlled synthetic relation must be visible after authenticated test transition"
    report={"fixture_type":"SYNTHETIC_ONLY","production_acceptance":False,"migrations_pass":True,
            "direct_write_blocked":direct_write_blocked,"authenticated_test_transition":True,
            "governed_relation_evidence":len(evidence),"enterprise_human_review_performed":False}
    (BASE/"review_report.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
    print(json.dumps(report),flush=True)


if __name__=="__main__":main()
