"""Exact ID/body-hash audit of existing local production sources. No mutations."""
from pathlib import Path
import argparse,json,sys,time,uuid
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from app.server.configuration import load_profiles,load_settings,user_configuration_dir
from app.storage.primary_document_policy import PrimarySpecificationDocumentPolicy
from app.rag.index_audit import reconcile_index


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scopes",nargs="+",choices=("21MM","24MM","COMMON","QABUG"),default=["QABUG"])
    args=parser.parse_args()
    import psycopg
    from psycopg.rows import dict_row
    from qdrant_client import QdrantClient
    profiles,password,qprofile,qkey=load_profiles(user_configuration_dir())
    settings=load_settings(user_configuration_dir())
    output=ROOT/"output/index_audit"/uuid.uuid4().hex
    output.mkdir(parents=True)
    client=QdrantClient(url=qprofile.url,api_key=qkey or None,timeout=30,trust_env=False)
    reports={}
    for scope in args.scopes:
        start=time.monotonic()
        collection=settings.collections[scope]
        exists=client.collection_exists(collection)
        before=client.get_collection(collection).points_count if exists else 0
        def actual():
            if not exists:return
            offset=None
            while True:
                points,offset=client.scroll(collection,limit=1024,offset=offset,with_vectors=False,
                    with_payload=["content_id","content_hash"])
                for point in points:
                    yield {"point_id":point.id,**(point.payload or {})}
                if offset is None:break
        kwargs=profiles[scope].dsn_kwargs()
        kwargs.update(password=password,options="-c default_transaction_read_only=on -c statement_timeout=90000")
        with psycopg.connect(**kwargs,row_factory=dict_row) as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            with conn.cursor(name="dic_readonly_index_audit") as cur:
                if scope=="QABUG":
                    cur.execute("""SELECT c.qdrant_point_id AS point_id,c.id AS content_id,c.content_hash
                        FROM public.qabug_chunks c JOIN public.qabug_records r ON r.id=c.record_id
                        WHERE c.is_current AND r.is_current AND r.is_queryable AND length(btrim(c.content))>0""")
                else:
                    primary=PrimarySpecificationDocumentPolicy.sql_primary_predicate("d")
                    cur.execute(f"""SELECT rc.content_id AS point_id,rc.content_id,rc.content_hash
                        FROM public.rag_current_chunks rc JOIN public.documents d USING(document_id)
                        WHERE rc.project_code=%s AND length(btrim(rc.content))>0 AND {primary}""",(scope,))
                cur.itersize=2048
                report=reconcile_index(output/(scope+".sqlite3"),cur,actual())
            conn.rollback()
        after=client.get_collection(collection).points_count if exists else 0
        report.update(collection=collection,collection_exists=exists,qdrant_count_before=before,
            qdrant_count_after=after,count_stable=before==after,elapsed_seconds=round(time.monotonic()-start,2),
            consistency="SQL repeatable-read snapshot; Qdrant scroll is not a transactional snapshot")
        if before!=after:report["id_and_body_coverage_complete"]=False
        reports[scope]=report
        (output/"summary.json").write_text(json.dumps(reports,indent=2),encoding="utf-8")
        print(json.dumps({"scope":scope,**report}),flush=True)
    print("Local audit report: "+str(output/"summary.json"))


if __name__=="__main__":main()
