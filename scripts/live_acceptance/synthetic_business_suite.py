"""Author clearly labeled typical fixtures; exercise ONLY guarded isolated stores.

generate uses the PDF runtime's reportlab/openpyxl. run uses the project's
actual backend dependencies. Neither mode imports fixtures into production.
"""
from pathlib import Path
import argparse,hashlib,json,sys,time
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(Path(__file__).resolve().parent))
OUT=ROOT/"examples/synthetic_business_suite"
CODES=("990101","990201","990301")

OLD=[("2.1",2,"Ignition OFF response","The controller shall stop Bluetooth playback within 500 ms after ignition becomes OFF, unless an emergency call is active."),
     ("2.2",3,"Reception loss","When reception is lost, Bluetooth audio may be silent, but Bluetooth Audio mode shall remain active. If reception recovers within 3 seconds, playback shall resume automatically."),
     ("2.4",4,"Legacy pairing","The legacy PIN pairing prompt shall appear before Bluetooth playback starts."),
     ("2.5",5,"Related audio routing","See specification 990201 section 3.1 for emergency call audio priority.")]
NEW=[("2.1",2,"Ignition OFF response","The controller shall stop Bluetooth playback within 700 ms after ignition becomes OFF, unless an emergency call is active."),
     OLD[1],
     ("2.3",4,"USB interruption","If USB insertion pauses Bluetooth playback, playback shall resume only after USB is removed and the user selects Bluetooth Audio again."),OLD[3]]
DOCS=[("990101","1.00","Bluetooth_Audio",OLD),("990101","1.10","Bluetooth_Audio",NEW),
 ("990201","2.00","Audio_Routing",[("3.1",2,"Emergency call priority","An active emergency call shall have priority over Bluetooth playback. After the call ends, Bluetooth playback shall resume only if ignition is ON."),
    ("3.2",3,"Mute and stored volume","A mute request shall not change the stored volume level. The same volume level shall be restored when mute is cancelled.")]),
 ("990301","1.00","Touchscreen",[("4.1",2,"Approved glove input","With approved gloves, a touchscreen long press between 800 ms and 1500 ms shall be accepted. Any shorter touch shall be ignored."),
    ("4.2",3,"Driving restriction","While the vehicle is moving, video editing shall be disabled. This restriction shall not disable the audio volume control.")])]


def filename(code,version,name):return f"{code}_SYNTHETIC_{name}_Ver{version}.pdf"


def generate():
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import Paragraph
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.colors import HexColor
    from xml.sax.saxutils import escape
    from openpyxl import Workbook
    import shutil
    OUT.mkdir(parents=True,exist_ok=True)
    pdf_out=ROOT/"output/pdf/synthetic_business_suite";pdf_out.mkdir(parents=True,exist_ok=True)
    style=ParagraphStyle("body",fontName="Helvetica",fontSize=12,leading=19,textColor=HexColor("#193047"))
    generated=[]
    for code,version,name,clauses in DOCS:
        file=filename(code,version,name);pdf=canvas.Canvas(str(pdf_out/file),pagesize=A4,pageCompression=1)
        pdf.setTitle("SYNTHETIC TEST ONLY - "+name+" "+version)
        width,height=A4
        for page in range(1,max(c[1] for c in clauses)+1):
            pdf.setFillColor(HexColor("#12344d"));pdf.rect(0,height-18,width,18,fill=1,stroke=0)
            pdf.setFillColor(HexColor("#9c3b16"));pdf.setFont("Helvetica-Bold",10)
            pdf.drawString(48,height-48,"SYNTHETIC TEST ONLY - NOT AN ENTERPRISE SPECIFICATION")
            pdf.setFillColor(HexColor("#193047"));pdf.setFont("Helvetica",10)
            pdf.drawString(48,height-70,f"Fixture {code} | Revision {version} | Isolated acceptance data")
            if page==1:
                pdf.setFont("Helvetica-Bold",23);pdf.drawString(48,height-132,name.replace("_"," "))
                pdf.setFont("Helvetica",12);pdf.drawString(48,height-160,"Typical requirements and known-answer test anchors")
                y=height-220
                for sid,number,title,text in clauses:
                    pdf.setFont("Helvetica-Bold",12);pdf.drawString(48,y,f"{sid}  {title}")
                    pdf.setFont("Helvetica",11);pdf.drawRightString(width-48,y,f"Page {number}");y-=34
                note=Paragraph("All requirements, document identities and tickets in this suite are invented. "
                    "They are used only to validate version changes, references, retrieval and fail-closed behavior. "
                    "Do not import these fixtures into an enterprise knowledge base.",style)
                _,h=note.wrap(width-96,240);note.drawOn(pdf,48,y-h-20)
            else:
                for sid,number,title,text in clauses:
                    if number!=page:continue
                    pdf.setFont("Helvetica-Bold",18);pdf.drawString(48,height-125,f"{sid}  {title}")
                    p=Paragraph(escape(text),style);_,h=p.wrap(width-112,height-270)
                    assert height-170-h>170,"Fixture paragraph does not fit"
                    p.drawOn(pdf,56,height-170-h)
                    pdf.setStrokeColor(HexColor("#c6d4df"));pdf.line(48,145,width-48,145)
                    pdf.setFont("Helvetica",10);pdf.drawString(48,126,"Evidence anchor: exact clause text, revision and physical page above.")
                    pdf.drawString(48,109,"Similarity is not duplicate proof. Missing mapping means no conformance verdict.")
            pdf.setFont("Helvetica",9);pdf.drawString(48,38,"INTERNAL SYNTHETIC FIXTURE | Never a production source")
            pdf.drawRightString(width-48,38,f"Page {page}");pdf.showPage()
        pdf.save();shutil.copyfile(pdf_out/file,OUT/file);generated.append(file)
        chapter=str(clauses[0][0].split(".")[0])
        document={"file_name":file,"file_type":"PDF","chapters":[{"id":chapter,"title_en":name.replace("_"," "),"page_number":2}],
            "sections":[{"id":sid,"chapter_id":chapter,"title_en":title,"page_number":page} for sid,page,title,text in clauses],
            "contents":[{"chapter_id":chapter,"section_id":sid,"page_number":page,"text":text,"chunk_index":0,"token_count":len(text.split())} for sid,page,title,text in clauses],
            "metadata":{"project_code":"21MM","project_name":"Synthetic Acceptance 21MM","project_assignment_source":"USER_SELECTED",
                "region_scope":"JP","document_role":"SPEC_BODY","artifact_type":"SPEC_BODY",
                "specification_code_override":code,
                "acceptance_fixture":"SYNTHETIC_ONLY","synthetic_suite":"typical-enterprise-v1"}}
        path=OUT/(file+".json");path.write_text(json.dumps(document,ensure_ascii=False,indent=2),encoding="utf-8");generated.append(path.name)
    qa=Workbook();sheet=qa.active;sheet.title="SYNTHETIC QA"
    sheet.append(["要約","課題キー","課題タイプ","ステータス","優先度","説明","カスタム フィールド (参照仕様書)"])
    sheet.append(["Bluetooth audio is silent after reception loss","SYNQA-101","仕様問題管理","クローズ","高",
        "Synthetic observation: audio is silent after reception loss, while Bluetooth Audio mode remains active.",
        filename("990101","1.10","Bluetooth_Audio")+"\nP.3\n2.2\tReception loss"])
    sheet.append(["Reference points to an unavailable historical revision","SYNQA-102","仕様問題管理","オープン","中",
        "Synthetic missing-version test. The current version must not replace the requested historical one.",
        filename("990101","9.99","Bluetooth_Audio")+"\nP.3\n2.2\tReception loss"])
    qa.save(OUT/"SYNTHETIC_QA.xlsx");qa.close();generated.append("SYNTHETIC_QA.xlsx")
    bug=Workbook();sheet=bug.active;sheet.title="SYNTHETIC BUG"
    sheet.append(["要約","課題キー","課題タイプ","ステータス","優先度","説明","カスタム フィールド (効果確認結果)"])
    sheet.append(["Touchscreen long press is ignored with approved gloves","SYNBUG-201","バグ","オープン","高",
        "Synthetic observation: a 1000 ms long press with approved gloves is ignored.\n"+
        filename("990301","1.00","Touchscreen")+"\nP.2\n4.1\tApproved glove input",""])
    sheet.append(["USB album artwork is missing","SYNBUG-202","バグ","クローズ","中",
        "Synthetic observation: only USB album artwork is missing. Audio is normal.",
        "Bluetooth audio is silent after reception loss. This sentence is post-fix feedback only, not an observation."])
    bug.save(OUT/"SYNTHETIC_BUG.xlsx");bug.close();generated.append("SYNTHETIC_BUG.xlsx")
    manifest={"schema":"synthetic-enterprise-suite-v1","synthetic_only":True,"production_import_allowed":False,
        "codes":CODES,"files":{name:hashlib.sha256((OUT/name).read_bytes()).hexdigest() for name in generated},
        "expected_changes":{"from":"1.00","to":"1.10","MODIFIED":["2.1"],"ADDED":["2.3"],"REMOVED":["2.4"]},
        "expected_reference":{"source":"990101/2.5","target":"990201/3.1"},
        "expected_conformance":"UNDETERMINED: no approved applicability mapping"}
    (OUT/"manifest.json").write_text(json.dumps(manifest,indent=2),encoding="utf-8")
    (OUT/"README.md").write_text("# 典型企业场景合成测试集\n\n所有内容均为编造，编号 990xxx 不代表真实企业式样。只允许导入隔离验收库。\n\n"
        "包含蓝牙音频两个版本、音频路由、触摸屏手套操作，以及 QA/BUG 表格。每份 PDF 有封面、版本、章节和物理页码。\n\n"
        "PDF 与规范 JSON 从同一受控定义生成；这用于验证检索、版本和关联，不等于复杂企业 PDF 解析质量认证。\n\n"
        "生成：python scripts/live_acceptance/synthetic_business_suite.py generate（需 reportlab/openpyxl）。\n"
        "隔离验收：python scripts/live_acceptance/synthetic_business_suite.py run（必须已有 15432 隔离实例）。\n",encoding="utf-8")
    print(json.dumps({"generated_pdf":4,"canonical_json":4,"ticket_xlsx":2,"directory":str(OUT),"production_data_written":False}))


def run():
    from run_isolated_business import context,BASE
    from app.model.document import Document
    from app.storage.postgres_storage import PostgresStorage
    from app.database.connection import DatabaseConnection
    from app.config.config_loader import DatabaseConfig
    from app.qabug.importer import QABugImportPipeline
    from app.rag.qdrant_sync import CurrentQdrantSyncService
    from app.enterprise.qdrant_service import QdrantService
    from app.qabug.qdrant_sync import QABugQdrantManager
    from app.ai.retrieval import EnterpriseRetriever
    from app.ai.testing_workflow import TestingWorkflow
    from app.engineering_intelligence.persistence import backfill_version,backfill_content_relations,backfill_version_mappings
    import psycopg,fitz
    state,profiles,qprofile,qkey=context()
    manifest=json.loads((OUT/"manifest.json").read_text())
    for name,expected in manifest["files"].items():assert hashlib.sha256((OUT/name).read_bytes()).hexdigest()==expected
    cfg=DatabaseConfig(enabled=True,host="127.0.0.1",port=15432,database=profiles["21MM"].database,
        project_databases=state["databases"],user="dic_accept",password=state["password"],password_env="DIC_ACCEPT_ONLY",connect_timeout=5)
    storage=PostgresStorage(DatabaseConnection(cfg),project_code="21MM")
    # Retirement is explicitly pinned to the independently initialized test
    # server and verifies its actual data directory before any write. It never
    # uses production connection settings or a configurable database target.
    with psycopg.connect(host="127.0.0.1",port=15432,dbname="dic_accept_21mm",user="dic_accept",
            password=state["password"],connect_timeout=5) as fixture_conn:
        identity=fixture_conn.execute("SELECT inet_server_port(),current_database(),current_setting('data_directory')").fetchone()
        if (identity[0]!=15432 or identity[1]!="dic_accept_21mm"
                or Path(identity[2]).resolve()!=Path(state["cluster"]).resolve()):
            raise ValueError("Refusing writes outside the verified workspace test cluster")
        fixture_conn.execute("""UPDATE documents SET metadata=metadata || %s::jsonb
            WHERE metadata->>'synthetic_suite'='typical-enterprise-v1'
              AND coalesce(metadata->>'specification_code','')=''""",
            (json.dumps({"document_role":"METADATA_ONLY","qa_index_policy":"EXCLUDE",
                "fixture_retired_reason":"Superseded by explicit reserved-code fixture identity"}),))
    for code,version,name,clauses in DOCS:
        file=filename(code,version,name)
        doc=Document.model_validate_json((OUT/(file+".json")).read_text(encoding="utf-8"))
        assert doc.metadata["acceptance_fixture"]=="SYNTHETIC_ONLY"
        with fitz.open(OUT/file) as pdf:
            for sid,page,title,text in clauses:
                native=" ".join(pdf[page-1].get_text().split());assert " ".join(text.split()) in native
        assert storage.save(doc)
    importer=QABugImportPipeline()
    for kind in ("QA","BUG"):
        result=importer.import_postgres(OUT/("SYNTHETIC_"+kind+".xlsx"),record_type=kind,profile=profiles["QABUG"],password=state["password"])
        assert result.ok and result.records_upserted==2
    print("Synthetic 990xxx documents and tickets imported to isolated SQL",flush=True)
    sync=CurrentQdrantSyncService(postgres=profiles["21MM"],postgres_password=state["password"],qdrant=qprofile,
        qdrant_api_key=qkey,project_code="21MM",collection_name=state["collections"]["21MM"],batch_size=4)
    assert sync.sync().status=="READY"
    assert sync.sync().upserted_count==0
    manager=QABugQdrantManager(qdrant_service=QdrantService(qprofile,qkey),collection_name=state["collections"]["QABUG"])
    manager.sync_from_postgres(postgres_profile=profiles["QABUG"],password=state["password"],prune_stale=False,batch_size=4)
    assert manager.sync_from_postgres(postgres_profile=profiles["QABUG"],password=state["password"],prune_stale=False).points_upserted==0
    r=EnterpriseRetriever(postgres_profiles=profiles,postgres_password=state["password"],qdrant=qprofile,qdrant_api_key=qkey,collections=state["collections"],top_k=12)
    r._embedding_client=sync._embedding_client  # Actual loaded BGE reused, never a mock.
    kw=profiles["21MM"].dsn_kwargs();kw["password"]=state["password"]
    with psycopg.connect(**kw) as conn,conn.cursor() as cur:
        cur.execute("SELECT sp.id,sv.id,sv.document_id,sp.spec_code,sv.previous_version_id FROM specifications sp JOIN specification_versions sv ON sv.specification_id=sp.id WHERE sp.spec_code=ANY(%s) ORDER BY sv.id",(list(CODES),))
        versions=cur.fetchall()
        for sp,vid,doc,code,old in versions:backfill_version(cur,specification_id=sp,version_id=vid,document_id=doc,dry_run=False)
        for sp,vid,doc,code,old in versions:
            backfill_content_relations(cur,source_specification_id=sp,source_version_id=vid,source_spec_code=code)
            if old:backfill_version_mappings(cur,old_version_id=old,new_version_id=vid)
    results=[]
    def case(name,fn):
        start=time.monotonic();details=fn();results.append({"case":name,"status":"PASS","duration_ms":round((time.monotonic()-start)*1000),"details":details})
        print(json.dumps(results[-1],ensure_ascii=True),flush=True)
    def search():
        evidence=r._semantic_search("Bluetooth audio silent reception loss",("21MM",),entity_codes=("990101",))
        assert any(i.section_id=="2.2" and i.page_number==3 and i.specification_version=="v1.10" for i in evidence)
        return "current revision clause 2.2, physical page 3"
    case("symptom_to_exact_clause",search)
    def diff():
        facts=r.retrieve_spec_version_facts("21MM 990101 v1.00 到 v1.10 版本变更",scope="21MM")
        changes=[c for item in facts.evidence for c in item.metadata.get("changes",[])]
        for kind,section in (("ADDED","2.3"),("REMOVED","2.4"),("MODIFIED","2.1")):
            assert any(c["change_type"]==kind and c["section_id"]==section for c in changes),(kind,changes)
        return {k:sum(c["change_type"]==k for c in changes) for k in ("ADDED","REMOVED","MODIFIED")}
    case("version_add_remove_modify",diff)
    def relation():
        with psycopg.connect(**kw) as conn:
            rows=conn.execute("""SELECT s.spec_code,t.spec_code,su.section_id,tu.section_id FROM ei_content_relations rel
              JOIN ei_requirement_units su ON su.id=rel.source_unit_id JOIN ei_requirement_units tu ON tu.id=rel.target_unit_id
              JOIN specifications s ON s.id=su.specification_id JOIN specifications t ON t.id=tu.specification_id
              WHERE s.spec_code='990101' AND t.spec_code='990201' AND su.version_id=s.current_version_id""").fetchall()
        assert ("990101","990201","2.5","3.1") in rows
        return "literal two-ended reference candidate; not a confirmed dependency"
    case("cross_spec_clause_reference",relation)
    workflow=TestingWorkflow(r)
    for name,question,expected,source in (
        ("qa_ticket_to_historical_body","21MM 查询 SYNQA-101 引用的式样正文条款","REFERENCE_RESOLVED","990101"),
        ("bug_description_to_body","21MM 查询 SYNBUG-201 引用的式样正文条款","REFERENCE_RESOLVED","990301"),
        ("missing_version_no_current_substitution","21MM 查询 SYNQA-102 引用的式样正文条款","REFERENCE_INCOMPLETE",None),
        ("duplicate_stops_spec_fallback","Bluetooth audio is silent after reception loss；起BUG前查重","POSSIBLE_DUPLICATES",None),
        ("no_strong_match_then_spec","21MM Audio routing mute request volume level stability；起BUG前查重","SPEC_EVIDENCE_REVIEW_REQUIRED",None)):
        def check(question=question,expected=expected,source=source):
            result=workflow.run(question,scope="21MM")
            assert result.workflow["state"]==expected,result.workflow
            assert result.workflow["conformance"]=="UNDETERMINED"
            if source:assert any(i.specification_code==source for i in result.evidence)
            if expected=="POSSIBLE_DUPLICATES":assert "spec_semantic_sql_hydration" not in result.tools
            return {"state":result.workflow["state"],"conformance":result.workflow["conformance"]}
        case(name,check)
    def effect():
        evidence=r._semantic_search("Bluetooth audio is silent after reception loss",("QABUG",),observation_only=True)
        assert all(i.section_id not in {"BUG_EFFECT_RESULT","EFFECT_RESULT"} for i in evidence)
        assert any(i.metadata.get("issue_key")=="SYNQA-101" and float(i.score or 0)>=.72 for i in evidence)
        assert not any(i.metadata.get("issue_key")=="SYNBUG-202" and float(i.score or 0)>=.72 for i in evidence)
        return "post-fix feedback cannot produce a strong duplicate match"
    case("postfix_feedback_not_observation",effect)
    def scope():
        rows=r.retrieve_spec_lookup("24MM 990101 是什么式样",scope="24MM")
        assert not any(i.source_type=="SPEC_LOOKUP" for i in rows.evidence)
        return "21MM synthetic body never returned as 24MM authority"
    case("cross_project_isolation",scope)
    from qdrant_client import QdrantClient
    def stale():
        client=QdrantClient(url=qprofile.url,api_key=qkey or None,trust_env=False)
        with psycopg.connect(**kw) as conn:
            cid=conn.execute("SELECT c.id FROM contents c JOIN specification_versions v ON v.document_id=c.document_id JOIN specifications s ON s.current_version_id=v.id WHERE s.spec_code='990101' AND c.section_id='2.2'").fetchone()[0]
        collection=state["collections"]["21MM"];original=client.retrieve(collection,ids=[cid],with_payload=True)[0].payload
        try:
            client.set_payload(collection,payload={"content_hash":"0"*64},points=[cid],wait=True)
            evidence=r._semantic_search("Bluetooth audio silent reception loss",("21MM",),entity_codes=("990101",))
            assert all(i.content_id!=cid for i in evidence)
        finally:client.overwrite_payload(collection,payload=original,points=[cid],wait=True)
        return "stale test point rejected and original payload restored"
    case("stale_vector_hash_rejected",stale)
    report={"suite":"typical-enterprise-v1","fixture_type":"SYNTHETIC_ONLY","production_data_written":False,
        "cases":results,"passed":len(results),"failed":0,"automatic_conformance_certification":False,
        "note":"Known-answer synthetic behavior tests; not enterprise golden quality or concurrency signoff"}
    (BASE/"typical_suite_report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"report":str(BASE/"typical_suite_report.json"),"passed":len(results),"production_data_written":False}),flush=True)


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument("action",choices=("generate","run"));args=parser.parse_args()
    generate() if args.action=="generate" else run()
