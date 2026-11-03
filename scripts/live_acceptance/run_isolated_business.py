"""Real SQL/Qdrant/BGE acceptance with explicitly synthetic business fixtures.

Only the guarded private cluster and generated dic_accept_* collections may be
written. Production SQL is not opened. Existing Qdrant names are never targeted.
"""
from pathlib import Path
import argparse,json,os,sys,time
from dataclasses import asdict
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(Path(__file__).resolve().parent))
os.environ.update(HF_HUB_OFFLINE="1",TRANSFORMERS_OFFLINE="1",HF_DATASETS_OFFLINE="1")
from isolated_environment import load_state,BASE
from app.enterprise.profiles import PostgresProfile
from app.server.configuration import load_profiles,user_configuration_dir,load_settings


def context():
    state=load_state()
    profiles={s:PostgresProfile(host="127.0.0.1",port=15432,database=n,user="dic_accept",connect_timeout=5)
              for s,n in state["databases"].items()}
    _,_,qp,qkey=load_profiles(user_configuration_dir())
    production=load_settings(user_configuration_dir()).collections
    for s,name in state["collections"].items():
        if name!=f"dic_accept_{state['run_id']}_{s.lower()}" or name in production.values():
            raise ValueError("Refusing non-acceptance collection")
    return state,profiles,qp,qkey


def ingest(state,profiles):
    from app.model.document import Document
    from app.model.chapter import Chapter
    from app.model.section import Section
    from app.model.content import Content
    from app.storage.postgres_storage import PostgresStorage
    from app.database.connection import DatabaseConnection
    from app.config.config_loader import DatabaseConfig
    fixtures=BASE/"synthetic_fixtures"
    fixtures.mkdir(exist_ok=True)
    def storage(scope):
        config=DatabaseConfig(enabled=True,host="127.0.0.1",port=15432,database=profiles[scope].database,
            project_databases=state["databases"],user="dic_accept",password=state["password"],
            password_env="DIC_ACCEPT_ONLY",connect_timeout=5)
        return PostgresStorage(DatabaseConnection(config),project_code=scope)
    docs=[("550","1.00","Bluetooth_Audio","2.1",17,"The controller shall stop Bluetooth playback within 500 ms after ignition OFF."),
          ("550","1.01","Bluetooth_Audio","2.1",17,"The controller shall stop Bluetooth playback within 700 ms after ignition OFF."),
          ("531","2.02","Connected_Radio","2.2",25,"The FM radio shall display the station logo when a broadcast logo is available.")]
    for code,version,name,section,page,text in docs:
        contents=[Content(chapter_id="2",section_id=section,text=text,page_number=page,token_count=25)]
        sections=[Section(id=section,chapter_id="2",title_en="Playback requirement",page_number=page)]
        if code=="550":
            contents.append(Content(chapter_id="2",section_id="2.3",text="See specification 531 section 2.2 for the FM radio station logo requirement.",page_number=18,token_count=22))
            sections.append(Section(id="2.3",chapter_id="2",title_en="Related specification",page_number=18))
        doc=Document(file_name=f"{code}_{name}_Ver{version}.pdf",file_type="PDF",
            chapters=[Chapter(id="2",title_en="Requirements",page_number=page)],sections=sections,contents=contents,
            metadata={"project_code":"21MM","project_name":"Acceptance 21MM","region_scope":"JP",
                "project_assignment_source":"USER_SELECTED","document_role":"SPEC_BODY","artifact_type":"SPEC_BODY",
                "acceptance_fixture":"SYNTHETIC_ONLY"})
        path=fixtures/(doc.file_name+".json")
        path.write_text(doc.model_dump_json(indent=2),encoding="utf-8")
        restored=Document.model_validate_json(path.read_text(encoding="utf-8"))
        assert storage("21MM").save(restored)
    from app.pipeline.pipeline_factory import PipelineFactory
    source=fixtures/"synthetic_internal_note.txt"
    source.write_text("1 Acceptance Requirements\n"+"The internal acceptance note requires local SQL and Qdrant storage. "*20,encoding="utf-8")
    document=PipelineFactory.create(source,save_json=True,save_database=False,project_code="COMMON").run(
        file_path=source,output=fixtures/"synthetic_internal_note.json")
    assert storage("COMMON").save(document)
    # Exercise all supported native parser families through JSON and real SQL.
    native_formats=["TXT"]
    body="The synthetic controller shall preserve the local requirement source and page location. "*10
    for suffix in (".pdf",".docx",".xlsx",".pptx"):
        path=fixtures/("synthetic_native_note"+suffix)
        if suffix==".pdf":
            import fitz
            with fitz.open() as pdf:
                page=pdf.new_page();page.insert_textbox(fitz.Rect(40,40,550,790),"1 Requirements\n"+body,fontsize=10)
                pdf.save(path)
        elif suffix==".docx":
            from docx import Document as WordDocument
            word=WordDocument();word.add_heading("1 Requirements",level=1);word.add_paragraph(body);word.save(path)
        elif suffix==".xlsx":
            from openpyxl import Workbook
            book=Workbook();book.active.append(["Item","Requirement"]);book.active.append(["Local storage",body]);book.save(path);book.close()
        else:
            from pptx import Presentation
            from pptx.util import Inches
            deck=Presentation();slide=deck.slides.add_slide(deck.slide_layouts[6])
            slide.shapes.add_textbox(Inches(1),Inches(1),Inches(8),Inches(5)).text_frame.text="1 Requirements\n"+body
            deck.save(path)
        doc=PipelineFactory.create(path,save_json=True,save_database=False,project_code="COMMON").run(
            file_path=path,output=path.with_suffix(suffix+".json"))
        assert doc.contents and storage("COMMON").save(doc)
        native_formats.append(suffix[1:].upper())
    from openpyxl import Workbook
    from app.qabug.importer import QABugImportPipeline
    book=Workbook();sheet=book.active;sheet.title="QA"
    sheet.append(["要約","課題キー","課題タイプ","ステータス","優先度","説明","カスタム フィールド (参照仕様書)"])
    sheet.append(["Bluetooth audio is silent after switching track","ACCEPTQA-88","仕様問題管理","クローズ","高",
        "Bluetooth audio is silent after switching track. Synthetic fixture, not a production defect.",
        "531_Connected_Radio_Ver2.02.pdf\nP.25\n2.2\tFM radio station logo"])
    ticket=fixtures/"synthetic_qa.xlsx";book.save(ticket);book.close()
    importer=QABugImportPipeline()
    json_report=importer.export_jsonl(ticket,fixtures/"synthetic_qa.jsonl",record_type="QA")
    sql_report=importer.import_postgres(ticket,record_type="QA",profile=profiles["QABUG"],password=state["password"])
    assert sql_report.ok and sql_report.records_upserted==1
    report={"stage":"INGEST","fixture_type":"SYNTHETIC_ONLY","specification_json_documents":3,
        "native_formats_to_json_sql":native_formats,"qa_xlsx_jsonl":json_report.jsonl_written,"qa_sql_records":sql_report.records_upserted}
    (BASE/"ingest_report.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
    print(json.dumps(report),flush=True)


def sync(state,profiles,qp,qkey):
    from app.rag.qdrant_sync import CurrentQdrantSyncService
    from app.qabug.qdrant_sync import QABugQdrantManager
    from app.enterprise.qdrant_service import QdrantService
    reports={}
    for scope in ("21MM","COMMON"):
        service=CurrentQdrantSyncService(postgres=profiles[scope],postgres_password=state["password"],
            qdrant=qp,qdrant_api_key=qkey,project_code=scope,collection_name=state["collections"][scope],batch_size=4)
        print("SYNC synthetic "+scope,flush=True)
        result=service.sync()
        assert result.failed_count==0 and result.status=="READY",asdict(result)
        second=service.sync()
        assert second.upserted_count==0,"Second sync must be idempotent"
        reports[scope]={"upserted":result.upserted_count,"second_upserted":second.upserted_count,"status":result.status}
    manager=QABugQdrantManager(qdrant_service=QdrantService(qp,qkey),collection_name=state["collections"]["QABUG"])
    result=manager.sync_from_postgres(postgres_profile=profiles["QABUG"],password=state["password"],prune_stale=False,batch_size=4)
    second=manager.sync_from_postgres(postgres_profile=profiles["QABUG"],password=state["password"],prune_stale=False,batch_size=4)
    assert second.points_upserted==0
    reports["QABUG"]={"upserted":result.points_upserted,"second_upserted":second.points_upserted}
    (BASE/"sync_report.json").write_text(json.dumps(reports,indent=2),encoding="utf-8")
    print(json.dumps({"stage":"SYNC","reports":reports}),flush=True)


def verify(state,profiles,qp,qkey):
    import psycopg
    from app.ai.retrieval import EnterpriseRetriever
    from app.ai.testing_workflow import TestingWorkflow,render_testing_workflow
    from app.engineering_intelligence.persistence import backfill_version,backfill_content_relations,backfill_version_mappings
    retriever=EnterpriseRetriever(postgres_profiles=profiles,postgres_password=state["password"],qdrant=qp,
        qdrant_api_key=qkey,collections=state["collections"],top_k=8)
    report={"fixture_type":"SYNTHETIC_ONLY","production_sql_written":False,"production_collections_written":False}
    kwargs=profiles["21MM"].dsn_kwargs();kwargs["password"]=state["password"]
    with psycopg.connect(**kwargs) as conn,conn.cursor() as cur:
        cur.execute("SELECT sv.specification_id,sv.id,sv.document_id,sp.spec_code,sv.previous_version_id FROM specification_versions sv JOIN specifications sp ON sp.id=sv.specification_id ORDER BY sv.id")
        versions=cur.fetchall()
        for spec,vid,doc,code,previous in versions:
            backfill_version(cur,specification_id=spec,version_id=vid,document_id=doc,dry_run=False)
        relation=[];mapping=[]
        for spec,vid,doc,code,previous in versions:
            relation.append(backfill_content_relations(cur,source_specification_id=spec,source_version_id=vid,source_spec_code=code))
            if previous:mapping.append(backfill_version_mappings(cur,old_version_id=previous,new_version_id=vid))
        report.update(backfilled_versions=len(versions),relation_backfills=relation,version_mappings=mapping)
        cur.execute("SELECT count(*) FROM ei_content_relations WHERE status='REVIEW_REQUIRED'")
        report["unreviewed_content_relations"]=cur.fetchone()[0]
        assert report["unreviewed_content_relations"]>=1
    print("WARMUP actual cached BGE for synthetic business checks",flush=True)
    retriever.warmup_embedding()
    sample=retriever.retrieve_qdrant_only("Bluetooth playback ignition OFF",scope="21MM")
    assert any(i.specification_code=="550" and i.page_number==17 and i.section_id=="2.1" for i in sample.evidence)
    report["clause_locator"]=True
    facts=retriever.retrieve_spec_version_facts("21MM 550 Bluetooth Audio v1.00 到 v1.01 版本变更",scope="21MM")
    report["saved_version_diff"]={"evidence_count":len(facts.evidence),
        "ready":any(i.metadata.get("change_set_ready") for i in facts.evidence),
        "changes":sum(int(i.metadata.get("change_count") or 0) for i in facts.evidence)}
    assert report["saved_version_diff"]["ready"] and report["saved_version_diff"]["changes"]>=1
    for name,question in (
        ("ticket_reference","21MM 查询 QA ACCEPTQA-88 引用的式样正文条款及页码"),
        ("duplicate","Bluetooth audio is silent after switching track；起 BUG 前先查重。"),
        ("spec_fallback","21MM FM radio station logo is missing；起 BUG 前先查重，没有再查式样。")):
        result=TestingWorkflow(retriever).run(question,scope="21MM")
        report[name]=result.workflow
        (BASE/(name+".md")).write_text(render_testing_workflow(result),encoding="utf-8")
        if name=="ticket_reference":assert result.workflow["state"]=="REFERENCE_RESOLVED"
        if name=="duplicate":assert result.workflow["state"]=="POSSIBLE_DUPLICATES"
        if name=="spec_fallback":assert result.workflow["state"]=="SPEC_EVIDENCE_REVIEW_REQUIRED"
        assert result.workflow["conformance"]=="UNDETERMINED"
        print(json.dumps({"stage":"BUSINESS","case":name,"state":result.workflow["state"],"elapsed_ms":result.workflow["elapsed_ms"]}),flush=True)
    report["status"]="PASS_SYNTHETIC_PATHS_NOT_PRODUCTION_ACCEPTANCE"
    (BASE/"business_report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str),encoding="utf-8")


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage",choices=("ingest","sync","verify","all"))
    args=parser.parse_args()
    state,profiles,qp,qkey=context()
    if args.stage in ("ingest","all"):ingest(state,profiles)
    if args.stage in ("sync","all"):sync(state,profiles,qp,qkey)
    if args.stage in ("verify","all"):verify(state,profiles,qp,qkey)
