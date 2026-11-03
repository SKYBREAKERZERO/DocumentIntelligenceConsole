"""Install/test optional check authority ONLY in the guarded synthetic cluster."""
from pathlib import Path
import sys,json
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(Path(__file__).resolve().parent))
from isolated_environment import load_state,BASE
from run_isolated_business import context
import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


def main():
    state,profiles,qprofile,qkey=context()
    kw=dict(host="127.0.0.1",port=15432,dbname="dic_accept_qabug",user="dic_accept",password=state["password"],connect_timeout=5)
    owner="dic_accept_conformance_owner";reviewer="dic_accept_reviewer"
    with psycopg.connect(**kw,autocommit=True) as conn:
        actual=conn.execute("SELECT inet_server_port(),current_database(),current_setting('data_directory')").fetchone()
        assert actual[:2]==(15432,"dic_accept_qabug") and Path(actual[2]).resolve()==Path(state["cluster"]).resolve()
        if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname=%s",(owner,)).fetchone():
            conn.execute(sql.SQL("CREATE ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE").format(sql.Identifier(owner)))
        conn.execute(sql.SQL("GRANT CREATE ON DATABASE dic_accept_qabug TO {}").format(sql.Identifier(owner)))
        conn.execute(sql.SQL("GRANT USAGE,CREATE ON SCHEMA public TO {}").format(sql.Identifier(owner)))
        conn.execute(sql.SQL("ALTER TABLE public.qabug_software_spec_versions OWNER TO {}").format(sql.Identifier(owner)))
        conn.execute(sql.SQL("GRANT ALL ON ALL TABLES IN SCHEMA public TO {}").format(sql.Identifier(owner)))
        conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(owner)))
        conn.execute((ROOT/"database/upgrade_conformance_checks_v1.sql").read_text(encoding="utf-8"))
        conn.execute("RESET ROLE")
        conn.execute("""INSERT INTO dic_conformance.authorized_reviewers(login_name,approved_by,justification)
          VALUES(%s,'SYNTHETIC_TEST','Automated synthetic authority validation; not enterprise human approval') ON CONFLICT DO NOTHING""",(reviewer,))
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA dic_conformance TO {}").format(sql.Identifier(reviewer)))
        conn.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA dic_conformance TO {}").format(sql.Identifier(reviewer)))
        conn.execute(sql.SQL("GRANT SELECT ON public.qabug_software_spec_versions TO {}").format(sql.Identifier(reviewer)))
        for function in ("review(text,bigint,text,text,text)","fingerprint(text,bigint)"):
            conn.execute(sql.SQL("GRANT EXECUTE ON FUNCTION dic_conformance."+function+" TO {}").format(sql.Identifier(reviewer)))
    spec_kw=profiles["21MM"].dsn_kwargs();spec_kw["password"]=state["password"]
    with psycopg.connect(**spec_kw,row_factory=dict_row) as conn:
        source=dict(conn.execute("""SELECT v.id AS version_id,v.version_label,c.document_id,c.id AS content_id,c.content_hash,c.content
           FROM specifications s JOIN specification_versions v ON v.id=s.current_version_id JOIN contents c ON c.document_id=v.document_id
           WHERE s.spec_code='990101' AND c.section_id='2.1'""").fetchone())
    with psycopg.connect(**kw) as conn:
        conn.execute("""INSERT INTO public.qabug_software_spec_versions(issue_version_field,software_version,model,destination,spec_scope,spec_code,
           spec_version_id,spec_version_label,spec_document_id) VALUES('software_version','SYN-SW-1','SYN-MODEL','JP','21MM','990101',%s,%s,%s) ON CONFLICT DO NOTHING""",
           (source["version_id"],source["version_label"],source["document_id"]))
        mid=conn.execute("SELECT id FROM public.qabug_software_spec_versions WHERE software_version='SYN-SW-1' AND spec_code='990101'").fetchone()[0]
    def review(kind,ident,new):
        with psycopg.connect(**{**kw,"user":reviewer}) as conn:
            fingerprint=conn.execute("SELECT dic_conformance.fingerprint(%s,%s)",(kind,ident)).fetchone()[0]
            conn.execute("SELECT dic_conformance.review(%s,%s,%s,%s,%s)",(kind,ident,fingerprint,new,"SYNTHETIC ONLY: known-answer fixture authority test"))
    def status(kind,ident):
        with psycopg.connect(**kw) as conn:
            table="public.qabug_software_spec_versions" if kind=="MAPPING" else "dic_conformance.checks"
            return conn.execute("SELECT status FROM "+table+" WHERE id=%s",(ident,)).fetchone()[0]
    if status("MAPPING",mid)=="REVOKED":review("MAPPING",mid,"PENDING")
    if status("MAPPING",mid)=="PENDING":review("MAPPING",mid,"APPROVED")
    rule={"kind":"MAXIMUM","measurement_key":"stop_latency_ms","measurement_label":"停播耗时（毫秒）",
          "conditions":{"ignition":"OFF","emergency_call":False},"anchor":"within 700 ms","unit":"ms","expected":700}
    with psycopg.connect(**kw) as conn:
        fp=conn.execute("SELECT dic_conformance.fingerprint('MAPPING',%s)",(mid,)).fetchone()[0]
        conn.execute("""INSERT INTO dic_conformance.checks(mapping_id,mapping_fingerprint,content_id,document_id,content_hash,section_id,rule_json)
          VALUES(%s,%s,%s,%s,%s,'2.1',%s) ON CONFLICT DO NOTHING""",(mid,fp,source["content_id"],source["document_id"],source["content_hash"],Jsonb(rule)))
        cid=conn.execute("SELECT id FROM dic_conformance.checks WHERE mapping_id=%s AND content_id=%s AND rule_json=%s",(mid,source["content_id"],Jsonb(rule))).fetchone()[0]
    if status("CHECK",cid)=="REVOKED":review("CHECK",cid,"PENDING")
    if status("CHECK",cid)=="PENDING":review("CHECK",cid,"APPROVED")
    from app.ai.retrieval import EnterpriseRetriever
    from app.ai.conformance import ConformanceService
    r=EnterpriseRetriever(postgres_profiles=profiles,postgres_password=state["password"],qdrant=qprofile,qdrant_api_key=qkey,collections=state["collections"])
    service=ConformanceService(r);ctx={"software_version":"SYN-SW-1","model":"SYN-MODEL","destination":"JP"}
    obs={"ignition":"OFF","emergency_call":False,"stop_latency_ms":600};results=[]
    for name,context_,values,expected in (
        ("condition_pass",ctx,obs,"MATCHED_CONFIGURED_CHECKS"),
        ("condition_fail",ctx,{**obs,"stop_latency_ms":900},"POTENTIAL_NONCONFORMANCE"),
        ("missing_exception_condition",ctx,{"ignition":"OFF","stop_latency_ms":600},"UNDETERMINED"),
        ("exception_active",ctx,{**obs,"emergency_call":True},"UNDETERMINED"),
        ("wrong_software",{**ctx,"software_version":"UNKNOWN"},obs,"UNDETERMINED"),
        ("wrong_model",{**ctx,"model":"OTHER"},obs,"UNDETERMINED")):
        value,evidence=service.assess("21MM","990101",context_,values)
        assert value["verdict"]==expected,value
        results.append({"case":name,"verdict":value["verdict"],"evidence":len(evidence)})
    blocked=False
    try:
        with psycopg.connect(**kw) as conn:conn.execute("UPDATE dic_conformance.checks SET rule_json='{}' WHERE id=%s",(cid,))
    except psycopg.Error:blocked=True
    assert blocked
    review("CHECK",cid,"REVOKED")
    assert service.assess("21MM","990101",ctx,obs)[0]["verdict"]=="UNDETERMINED"
    review("CHECK",cid,"PENDING");review("CHECK",cid,"APPROVED")
    review("MAPPING",mid,"REVOKED")
    assert service.assess("21MM","990101",ctx,obs)[0]["verdict"]=="UNDETERMINED"
    review("MAPPING",mid,"PENDING");review("MAPPING",mid,"APPROVED")
    from app.ai.testing_workflow import TestingWorkflow,render_testing_workflow
    chat=TestingWorkflow(r).run("是否符合式样？式样编号=990101；软件版本=SYN-SW-1；车型=SYN-MODEL；地域=JP；stop_latency_ms=900；ignition=OFF；emergency_call=false",scope="21MM")
    assert chat.workflow["conformance"]=="POTENTIAL_NONCONFORMANCE",chat.workflow
    assert "疑似不符合" in render_testing_workflow(chat)
    assert 'content_id=' in render_testing_workflow(chat) and '页码=2' in render_testing_workflow(chat) and 'SHA256=' in render_testing_workflow(chat)
    # Committed source drift must be rejected even when approval still exists.
    with psycopg.connect(**spec_kw) as conn:
        conn.execute('UPDATE contents SET content=content || %s WHERE id=%s',(' SYNTHETIC DRIFT',source['content_id']))
    try:assert service.assess('21MM','990101',ctx,obs)[0]['verdict']=='UNDETERMINED'
    finally:
        with psycopg.connect(**spec_kw) as conn:conn.execute('UPDATE contents SET content=%s WHERE id=%s',(source['content'],source['content_id']))
    # Even authority-owner edits cannot reuse a prior review fingerprint.
    with psycopg.connect(**kw) as conn:
        conn.execute(sql.SQL('SET ROLE {}').format(sql.Identifier(owner)))
        conn.execute("UPDATE public.qabug_software_spec_versions SET spec_version_label='SYNTHETIC_CHANGED' WHERE id=%s",(mid,))
    try:assert service.assess('21MM','990101',ctx,obs)[0]['verdict']=='UNDETERMINED'
    finally:
        with psycopg.connect(**kw) as conn:
            conn.execute(sql.SQL('SET ROLE {}').format(sql.Identifier(owner)))
            conn.execute('UPDATE public.qabug_software_spec_versions SET spec_version_label=%s WHERE id=%s',(source['version_label'],mid))
    from scripts.manage_conformance import Operator
    operator=Operator(profiles,state['password']);operator.require_ready()
    inspected=operator.inspect('CHECK',cid)
    assert inspected['source']['content']==source['content'] and not inspected.get('source_validation_error')
    proposal=operator.propose({'mapping_id':mid,'content_id':source['content_id'],'rule_json':rule},'CHECK')
    assert proposal['mode']=='DRY_RUN' and not proposal['approved']
    # Exercise authentication, schema validation and OpenAI chat contract on the
    # actual service object, using only synthetic SQL profiles.
    from app.server.api import create_app
    from app.ai.rag_service import EnterpriseRAGService
    from app.ai.config import AISettings
    from fastapi.testclient import TestClient
    from types import SimpleNamespace
    engine=EnterpriseRAGService(settings=AISettings(collections=state['collections']),postgres_profiles=profiles,
        postgres_password=state['password'],qdrant_profile=qprofile,qdrant_api_key=qkey)
    runtime=SimpleNamespace(get_service=lambda:engine,preferred_model='')
    token='synthetic-test-only-not-valid-on-production-12345'
    client=TestClient(create_app(runtime,token),base_url='http://localhost');auth={'Authorization':'Bearer '+token}
    request={'scope':'21MM','specification_code':'990101','context':ctx,'observations':{**obs,'stop_latency_ms':900}}
    assert client.post('/api/v1/testing/assess',json=request).status_code==401
    api=client.post('/api/v1/testing/assess',headers=auth,json=request)
    assert api.status_code==200 and api.json()['assessment']['verdict']=='POTENTIAL_NONCONFORMANCE',api.text
    assert api.json()['evidence'][0]['content_id']==source['content_id']
    assert client.post('/api/v1/testing/assess',headers=auth,json={**request,'scope':'AUTO'}).status_code==400
    question='21MM 是否符合式样？式样编号=990101；软件版本=SYN-SW-1；车型=SYN-MODEL；地域=JP；stop_latency_ms=900；ignition=OFF；emergency_call=false'
    body={'model':'document-intelligence-rag','scope':'21MM','stream':False,'messages':[{'role':'user','content':question}]}
    response=client.post('/v1/chat/completions',headers=auth,json=body)
    assert response.status_code==200 and '疑似不符合' in response.json()['choices'][0]['message']['content'],response.text
    stream=client.post('/v1/chat/completions',headers=auth,json={**body,'stream':True})
    assert stream.status_code==200 and 'data: [DONE]' in stream.text and '疑似不符合' in stream.text,stream.text
    report={"fixture_type":"SYNTHETIC_ONLY","enterprise_human_review_performed":False,"production_schema_modified":False,
        "cases":results,"direct_approved_rule_edit_blocked":blocked,"revoked_check_rejected":True,"revoked_mapping_rejected":True,
        "chat_route":"PASS","complete_specification_certified":False,'source_hash_drift_rejected':True,
        'mapping_fingerprint_drift_rejected':True,'operator_inspect_and_dry_run':'PASS',
        'actual_service_api_contract':{'authentication':'PASS','assessment':'PASS','explicit_scope':'PASS','openai_chat':'PASS','openai_sse':'PASS'},
        'browser_ui_validation_this_run':False}
    (BASE/"conformance_report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(report,ensure_ascii=False),flush=True)


if __name__=="__main__":main()
