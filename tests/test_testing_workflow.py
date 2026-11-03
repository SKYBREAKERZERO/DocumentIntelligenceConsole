"""Business branch, identity, deadline and negative-claim regressions."""
import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,MagicMock
import pytest
from app.ai.retrieval import EvidenceItem,RetrievalResult
from app.ai.testing_workflow import TestingWorkflow as Workflow,explicit_references,render_testing_workflow
from app.ai.agent import FastIntentRouter
from app.ai.retrieval_deadline import retrieval_budget,remaining_ms
from app.rag.index_audit import reconcile_index


def issue(score=.9,key="QA-88"):
    return EvidenceItem(evidence_id="",scope="QABUG",source_type="ISSUE_SEMANTIC",content="Bluetooth is silent",
        score=score,metadata={"issue_key":key})


def test_effect_confirmation_is_available_for_spec_retrieval():
    from app.ai.testing_workflow import ticket_spec_query
    ticket=issue(key="MM21PROBL-56138")
    ticket.metadata.update({
        "summary":"Incorrect text on Audio Pause banner",
        "analysis_result":"[2024/3/6 NEU] 仕様通り",
        "effect_result":"Defined in AllWords 21UP_HENREN; HENRENUP1-13F2G-007 applies to 980B after RC2407D.U",
        "effect_version":"RC2407D.U",
    })
    query=ticket_spec_query(ticket)
    assert "AllWords 21UP_HENREN" in query
    assert "HENRENUP1-13F2G-007" in query
    assert "980B after RC2407D.U" in query


def test_possible_duplicate_stops_spec_fallback_and_deduplicates_ticket_chunks():
    r=Mock();r._semantic_search.return_value=[issue(.8),issue(.95)]
    result=Workflow(r).run("21MM 蓝牙无声，起BUG前查重",scope="21MM")
    assert result.workflow["state"]=="POSSIBLE_DUPLICATES"
    assert result.workflow["conformance"]=="UNDETERMINED"
    assert result.workflow["negative_search_means_absent"] is False
    assert len(result.evidence)==1 and result.evidence[0].score==.95
    r._semantic_search.assert_called_once()
    assert "不存在历史票" in render_testing_workflow(result)


def test_no_strong_match_falls_back_without_claiming_no_history():
    r=Mock();r._semantic_search.side_effect=[[issue(.4)],[]]
    result=Workflow(r).run("21MM FM异常；起BUG前查重",scope="21MM")
    assert result.workflow["state"]=="INSUFFICIENT_EVIDENCE"
    assert r._semantic_search.call_args_list[0].args[1]==("QABUG",)
    assert r._semantic_search.call_args_list[1].args[1]==("21MM",)
    assert result.workflow["coverage"]=="UNVERIFIED"
    assert result.workflow["duplicate_verdict"]=="UNDETERMINED"


def test_no_implicit_project_and_no_model_decision():
    r=Mock();r._semantic_search.return_value=[]
    result=Workflow(r).run("起BUG前查重，蓝牙没有声音")
    assert result.workflow["state"]=="PROJECT_REQUIRED"
    assert r._semantic_search.call_count==1


def test_cancelled_before_query():
    r=Mock()
    result=Workflow(r).run("起BUG前查重",cancel_check=lambda:True)
    assert result.workflow["state"]=="CANCELLED"
    r._semantic_search.assert_not_called()


def test_vector_failure_is_visible_even_if_spec_fallback_returns_data():
    r=Mock();r._semantic_search.side_effect=[RuntimeError("secret-dsn"),[]]
    result=Workflow(r).run("起BUG前查重",scope="24MM")
    assert result.warnings and "secret-dsn" not in str(result)
    assert result.workflow["duplicate_verdict"]=="UNDETERMINED"


def test_explicit_ticket_reference_preserves_pages_and_historical_version():
    refs=explicit_references("531_Connected_Radio_Function_Ver2.02.docx\nP.25\n2.2.4.\tLogo order\nP.35\n2.6.4.1.2\tHD logo")
    assert [(r["code"],r["version"],r["page"],r["section"]) for r in refs]==[
        ("531","2.02",25,"2.2.4"),("531","2.02",35,"2.6.4.1.2")]


@pytest.mark.parametrize("text",["531 Radio section 2.2", "531_Radio.pdf\nP.25\n2.2", "See newest version of 531"])
def test_ambiguous_reference_not_invented(text):
    assert explicit_references(text)==[]


def test_multi_file_reference_does_not_leak_page_between_blocks():
    refs=explicit_references("531_Radio_Ver2.02.pdf\nP.25\n2.2\n550_Audio_Ver1.00.pdf\n3.1")
    assert refs[0]["page"]==25 and refs[1]["page"] is None


def test_ticket_exact_can_resolve_without_ei_tables_and_is_not_conformance_mapping():
    r=Mock();r._qabug_issue_sql_search.return_value=[EvidenceItem(evidence_id="",scope="QABUG",source_type="ISSUE_EXACT",
        content="Synthetic ticket",metadata={"issue_key":"QA-88","reference_spec":"531_Radio_Ver2.02.pdf\nP.25\n2.2"})]
    workflow=Workflow(r)
    workflow.resolve_reference=Mock(return_value=[EvidenceItem(evidence_id="",scope="21MM",source_type="TICKET_SPEC_CLAUSE",content="shall show logo")])
    result=workflow.run("查询QA-88明确引用的式样正文",scope="21MM")
    assert result.workflow["state"]=="REFERENCE_RESOLVED"
    assert result.workflow["applicable_version_mapping_verified"] is False
    assert r._semantic_search.call_count==0
    assert workflow.resolve_reference.call_args.args[0]["version"]=="2.02"


def test_reference_hash_mismatch_rejected_and_ambiguous_documents_rejected():
    r=MagicMock();connection=r._connect.return_value.__enter__.return_value
    cursor=connection.cursor.return_value.__enter__.return_value
    row={"content":"source","content_hash":"wrong","document_id":"d"}
    cursor.fetchall.return_value=[row]
    ref={"code":"531","version":"2.02","file_name":"531_Radio_Ver2.02.pdf","section":"2.2","page":25}
    assert Workflow(r).resolve_reference(ref,("21MM",))==[]
    cursor.fetchall.return_value=[row,{**row,"document_id":"other"}]
    assert Workflow(r).resolve_reference(ref,("21MM",))==[]


@pytest.mark.parametrize("q,route",[("起 BUG 前先查 QA/BUG 库是否有相同现象，没有再查式样","PRE_TICKET_CHECK"),
    ("查询 QA MM21SPMAN-88。找到票内引用的531式样正文条款","TICKET_SPEC_TRACE"),
    ("查询QA-88状态","ISSUE_QUERY")])
def test_fast_route_branches(q,route):assert FastIntentRouter.classify(q).route==route


def test_deadline_nested_budget_cannot_extend_outer_and_resets():
    assert remaining_ms() is None
    with retrieval_budget(1):
        original=remaining_ms()
        with retrieval_budget(120):assert remaining_ms()<=original
    assert remaining_ms() is None


def test_raw_reference_content_is_fenced_instead_of_executed_markdown():
    result=RetrievalResult(evidence=(issue(),),resolved_scopes=("QABUG",),workflow={"state":"INSUFFICIENT_EVIDENCE","steps":[]})
    from dataclasses import replace
    result=replace(result,evidence=(replace(issue(),content='```\n![leak](https://example.invalid/private)\n```'),))
    text=render_testing_workflow(result)
    assert '````text\n```' in text


def test_exact_index_reconciliation_detects_missing_extra_and_changed(tmp_path):
    expected=[{"point_id":n,"content_id":n,"content_hash":"h"} for n in (1,2,3)]
    actual=[{"point_id":1,"content_id":1,"content_hash":"h"},{"point_id":2,"content_id":99,"content_hash":"stale"},
            {"point_id":4,"content_id":4,"content_hash":"h"}]
    result=reconcile_index(tmp_path/"audit.sqlite3",iter(expected),iter(actual))
    assert (result["missing_points"],result["extra_points"],result["source_id_mismatch"],result["body_hash_mismatch"])==(1,1,1,1)
    assert not result["id_and_body_coverage_complete"]
    assert result["automatic_repair"] is False


def test_empty_coverage_does_not_imply_nonempty_corpus(tmp_path):
    result=reconcile_index(tmp_path/"empty.sqlite3",[],[])
    assert result["eligible_sql_points"]==0 and result["actual_unique_points"]==0


def test_no_evidence_from_models_on_fixed_workflow():
    from app.ai.rag_service import EnterpriseRAGService
    from app.ai.config import AISettings
    service=EnterpriseRAGService.__new__(EnterpriseRAGService)
    service.settings=AISettings();service.retriever=Mock();service.retriever._semantic_search.return_value=[]
    service.ollama=Mock();service.agent=Mock()
    answer="".join(service.answer_stream([{"role":"user","content":"起BUG前查重，21MM无声"}],model="local",scope="21MM"))
    assert "证据不足" in answer
    service.ollama.chat_stream.assert_not_called();service.agent.retrieve.assert_not_called()


def test_duplicate_search_uses_sql_observation_identity_and_rejects_postfix_fields():
    from test_live_acceptance_regressions import semantic_fixture
    r,row,payload=semantic_fixture()
    r.collections={"QABUG":"test"}
    row.update(project_code="QABUG",document_id="QA:REAL-1",issue_key="REAL-1",section_id="BUG_EFFECT_RESULT")
    payload.update(project_code="QABUG",document_id="QA:REAL-1",issue_key="FAKE-2",section_type="SUMMARY")
    assert r._semantic_search("silent",("QABUG",),observation_only=True)==[]
    row["section_id"]="SUMMARY"
    evidence=r._semantic_search("silent",("QABUG",),observation_only=True)
    assert evidence[0].metadata["issue_key"]=="REAL-1"
    assert evidence[0].metadata["record_key"]=="QA:REAL-1"


def test_chat_api_exposes_fixed_workflow_state_without_occupying_model_lane():
    from fastapi.testclient import TestClient
    from app.server.api import create_app
    runtime=Mock();runtime.preferred_model="local"
    result=RetrievalResult(evidence=(),resolved_scopes=("21MM","QABUG"),route="PRE_TICKET_CHECK",
        workflow={"state":"INSUFFICIENT_EVIDENCE","conformance":"UNDETERMINED"})
    runtime.get_service.return_value.answer.return_value=("证据不足",result,0)
    app=create_app(runtime,"t"*48)
    client=TestClient(app,base_url="http://127.0.0.1")
    response=client.post("/v1/chat/completions",headers={"Authorization":"Bearer "+"t"*48},json={
        "model":"document-intelligence-rag","messages":[{"role":"user","content":"21MM起BUG前查重"}]})
    assert response.status_code==200
    assert response.json()["rag"]["workflow"]["conformance"]=="UNDETERMINED"
    assert app.state.factual_admissions.snapshot()["active"]==0


def test_explicit_server_config_cannot_inherit_production_config_file(tmp_path,monkeypatch):
    import app.server.__main__ as cli
    import sys,os
    monkeypatch.setattr(sys,"argv",["backend","--config-dir",str(tmp_path),"--no-warmup"])
    monkeypatch.setenv("CONFIG_FILE","unrelated-production.yaml")
    monkeypatch.setattr(cli,"load_settings",Mock(return_value=SimpleNamespace(api_host="127.0.0.1",api_port=18081)))
    runtime=Mock();monkeypatch.setattr(cli,"PersistentRAGRuntime",runtime)
    monkeypatch.setattr(cli,"api_token",Mock(return_value="t"*48))
    monkeypatch.setattr(cli,"create_app",Mock())
    monkeypatch.setattr(cli.uvicorn,"run",Mock())
    cli.main()
    assert os.environ["CONFIG_FILE"]==str(tmp_path.resolve()/"config.yaml")
    runtime.assert_called_once_with(tmp_path.resolve())


def test_planner_template_does_not_seed_fake_document_identities():
    from app.ai.agent import QueryPlanner
    model=Mock();model.chat_json.return_value={"scopes":["21MM"],"tools":["semantic_search"],"entities":[],"rewritten_queries":["550 playback"]}
    QueryPlanner(model).plan(model="local",question="21MM 550 playback requirements",requested_scope="21MM",current_scope="",conversation=[])
    prompt=model.chat_json.call_args.kwargs["messages"][0]["content"]
    assert "1001" not in prompt and "2007" not in prompt and "User Profile" not in prompt
    assert "550 playback" in prompt


def test_bug_description_reference_resolves_and_duplicate_reference_fields_do_not_duplicate():
    ref="990301_SYNTHETIC_Touchscreen_Ver1.00.pdf\nP.2\n4.1\tApproved glove input"
    r=Mock();r._qabug_issue_sql_search.return_value=[EvidenceItem(evidence_id="",scope="QABUG",source_type="ISSUE_EXACT",content="Synthetic BUG",
        metadata={"issue_key":"SYNBUG-201","description":"Actual observation.\n"+ref,"actual_result":ref})]
    workflow=Workflow(r)
    workflow.resolve_reference=Mock(return_value=[EvidenceItem(evidence_id="",scope="21MM",source_type="TICKET_SPEC_CLAUSE",content="shall accept long press")])
    result=workflow.run("21MM SYNBUG-201 引用的式样正文条款",scope="21MM")
    assert result.workflow["state"]=="REFERENCE_RESOLVED"
    workflow.resolve_reference.assert_called_once()
    assert workflow.resolve_reference.call_args.args[0]["source_field"]=="description"


def test_unscoped_source_text_cannot_expand_engineering_project():
    r=Mock();r._qabug_issue_sql_search.return_value=[EvidenceItem(evidence_id="",scope="QABUG",source_type="ISSUE_EXACT",content="Synthetic BUG",
        metadata={"issue_key":"SYNBUG-201","description":"24MM\n990301_SYNTHETIC_Touchscreen_Ver1.00.pdf\nP.2\n4.1"})]
    workflow=Workflow(r);workflow.resolve_reference=Mock(return_value=[])
    result=workflow.run("21MM SYNBUG-201 引用的式样正文",scope="21MM")
    assert result.workflow["state"]=="REFERENCE_INCOMPLETE"
    assert workflow.resolve_reference.call_args.args[1]==("21MM",)

