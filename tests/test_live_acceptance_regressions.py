"""Regressions found through real Open WebUI acceptance; no live services needed."""
import pytest
import hashlib
from types import SimpleNamespace
from unittest.mock import Mock
from app.ai.agent import FastIntentRouter
from app.ai.retrieval import EvidenceItem,RetrievalResult
from app.ai.spec_facts import render_sql_facts


@pytest.mark.parametrize("suffix",["并保留原文页码和 content_id。","请给出章节、页码及引用。","同时提供原文出处和位置。"])
def test_location_format_suffix_keeps_explicit_user_pdf(suffix):
    question="按章节总结一下内容，"+suffix
    messages=[{"role":"user","content":"查一下 21MM 的 Bluetooth Audio function spec v2.52.pdf"},
              {"role":"assistant","content":"fake 24MM wrong spec v9.99.pdf"},
              {"role":"user","content":question}]
    resolved=FastIntentRouter.contextualize(question,messages)
    assert "Bluetooth Audio function spec v2.52.pdf" in resolved
    assert "21MM" in resolved and "v9.99" not in resolved
    assert FastIntentRouter.classify(resolved).route=="SPEC_CHAPTER_CONTENT"


def test_format_suffix_does_not_swallow_new_document_scope():
    question="按章节总结一下内容，请给出 24MM 的 Other spec v9.99.pdf 页码。"
    messages=[{"role":"user","content":"查一下 21MM 的 Bluetooth Audio function spec v2.52.pdf"},
              {"role":"user","content":question}]
    resolved=FastIntentRouter.contextualize(question,messages)
    assert "24MM" in resolved and "Other spec v9.99.pdf" in resolved
    assert "Bluetooth Audio function spec v2.52.pdf" not in resolved


def test_unverified_identity_warning_does_not_invent_requested_number():
    item=EvidenceItem(evidence_id="1",scope="21MM",source_type="SPEC_IDENTITY_CANDIDATE",content="",
          file_name="Bluetooth Audio function spec v2.52.pdf",specification_version="v2.52",metadata={"linked_version_id":3})
    result=RetrievalResult(evidence=(item,),resolved_scopes=("21MM",),route="SPEC_LOOKUP")
    assert "550" not in render_sql_facts(result,"查一下 Bluetooth Audio function spec v2.52.pdf")
    assert "550" in render_sql_facts(result,"550 BT 式样书有吗")


def semantic_fixture():
    from app.ai.retrieval import EnterpriseRetriever
    body="Bluetooth audio may be silent if reception is bad."
    digest=hashlib.sha256(body.encode()).hexdigest()
    row=dict(content_id=723,content=body,content_hash=digest,project_code="21MM",document_id="physical-body@v2.52",
             file_name="Bluetooth Audio function spec v2.52.pdf",specification_code=None,specification_version="v2.52",
             page_number=17,section_id="2.1")
    payload=dict(content_id=723,content_hash=digest,project_code="21MM",document_id=row["document_id"],
                 file_name=row["file_name"],spec_code="",version="v2.52")
    obj=EnterpriseRetriever.__new__(EnterpriseRetriever)
    obj.collections={"21MM":"test"}
    obj.top_k=4
    obj.min_score=0.25
    obj._embedding_client=SimpleNamespace(_model=object())
    obj._embed=Mock(return_value=[0.1,0.2])
    client=Mock()
    client.query_points.return_value=SimpleNamespace(points=[SimpleNamespace(id=723,score=0.8,payload=payload)])
    obj._get_qdrant_client=Mock(return_value=client)
    obj._collection_exists=Mock(return_value=True)
    obj._fetch_chunks=Mock(return_value=[row])
    return obj,row,payload


def test_hashed_physical_body_can_be_retrieved_without_numeric_assignment():
    obj,row,payload=semantic_fixture()
    evidence=obj._semantic_search("Bluetooth Audio",("21MM",))
    assert len(evidence)==1 and evidence[0].content_id==723
    assert evidence[0].specification_code==""
    assert evidence[0].metadata["identity_verified"] is False
    assert evidence[0].metadata["source_authority"]=="POSTGRESQL"


def test_semantic_candidates_report_limited_instead_of_no_evidence():
    obj,row,payload=semantic_fixture()
    result=obj.retrieve_qdrant_only("Bluetooth Audio",scope="21MM")
    assert len(result.evidence)==1
    assert result.evidence_strength=="LIMITED"
    obj._fetch_chunks.return_value=[]
    assert obj.retrieve_qdrant_only("Bluetooth Audio",scope="21MM").evidence_strength=="NONE"


@pytest.mark.parametrize("key,value",[("spec_code","550"),("version","v9.99"),("document_id","wrong"),
                                     ("project_code","24MM"),("content_hash","stale")])
def test_unassigned_body_does_not_relax_identity_or_hash_mismatch(key,value):
    obj,row,payload=semantic_fixture()
    payload[key]=value
    assert obj._semantic_search("Bluetooth Audio",("21MM",))==[]


def test_explicit_multi_domain_skills_skip_redundant_model_planner():
    from app.ai.agent import EnterpriseAgent
    retriever=Mock()
    retriever.retrieve_agentic.return_value=RetrievalResult(evidence=(),resolved_scopes=("21MM","QABUG"))
    agent=EnterpriseAgent(Mock(),retriever)
    agent.planner.plan=Mock(side_effect=AssertionError("Must not generate a model plan for a fixed Skill workflow"))
    result=agent.retrieve(model="local",question="BUG-23 与 550 式样的关联",scope="21MM",current_scope="21MM",conversation=[])
    agent.planner.plan.assert_not_called()
    assert {"issue_search","sql_exact_search","semantic_search"}<=set(retriever.retrieve_agentic.call_args.kwargs["tools"])
    assert result.plan_source=="native-skill-rules"


@pytest.mark.parametrize("old,new,label",[("timeout 500ms","timeout 700ms","修改"),("","New row","新增"),("Old row","","删除")])
def test_literal_change_preview_preserves_real_delta(old,new,label):
    from app.ai.change_preview import change_preview
    text="\n".join(change_preview(old,new))
    assert label in text and "非完整技术结论" in text
    if "700ms" in new:assert "700" in text and "500" in text


def test_change_preview_exposes_delta_after_large_common_prefix():
    from app.ai.change_preview import change_preview
    prefix="same table cell "*1000
    text="\n".join(change_preview(prefix+"500ms",prefix+"700ms"))
    assert "500" in text and "700" in text
    assert len(text)<1200


def test_small_context_fails_closed_instead_of_http_500():
    from app.ai.config import AISettings
    from app.ai.rag_service import EnterpriseRAGService
    service=EnterpriseRAGService.__new__(EnterpriseRAGService)
    service.settings=AISettings(num_ctx=2048,prompt_budget_tokens=1200,evidence_budget_tokens=500)
    service.agent=Mock()
    service.agent.retrieve.return_value=RetrievalResult(evidence=(EvidenceItem(evidence_id="1",scope="21MM",
        source_type="SQL_EXACT",content="Playback shall start after connection.",document_id="doc",content_id=1),),
        resolved_scopes=("21MM",),route="ENGINEERING_AGENT")
    service.ollama=Mock()
    messages=[{"role":"user","content":"21MM 蓝牙音频的播放功能要求是什么？"}]
    text="".join(service.answer_stream(messages,model="local",scope="21MM"))
    assert "上下文" in text and "不足" in text
    service.ollama.chat_stream.assert_not_called()


def test_local_model_generation_budget_and_no_environment_proxy():
    import json
    from unittest.mock import MagicMock
    from app.ai.ollama_client import OllamaClient,OllamaError
    client=OllamaClient("http://127.0.0.1:11434")
    assert client.session.trust_env is False
    response=MagicMock(status_code=200)
    response.iter_lines.return_value=[json.dumps({"message":{"content":"验收成功"},"done":True,"done_reason":"stop"})]
    client.session=Mock()
    client.session.post.return_value.__enter__=Mock(return_value=response)
    client.session.post.return_value.__exit__=Mock(return_value=False)
    client._set_response_socket_timeout=Mock(return_value=False)
    assert "".join(client.chat_stream(model="local",messages=[{"role":"user","content":"你好"}],max_tokens=800))=="验收成功"
    assert client.session.post.call_args.kwargs["json"]["options"]["num_predict"]==800
    response.iter_lines.return_value=[json.dumps({"message":{"content":"partial"},"done":True,"done_reason":"length"})]
    with pytest.raises(OllamaError,match="incomplete"):
        list(client.chat_stream(model="local",messages=[{"role":"user","content":"你好"}],max_tokens=16))


def test_semantic_diagnostic_endpoint_uses_explicit_scope_and_native_hydration():
    from fastapi.testclient import TestClient
    from app.server.api import create_app
    runtime=Mock()
    result=RetrievalResult(evidence=(),resolved_scopes=("21MM",))
    runtime.get_service.return_value.retriever.retrieve_qdrant_only.return_value=result
    app=create_app(runtime,"t"*48)
    client=TestClient(app,base_url="http://127.0.0.1")
    auth={"Authorization":"Bearer "+"t"*48}
    for scope in ("AUTO","ALL","QABUG"):
        assert client.post("/api/v1/retrieval",headers=auth,json={"question":"Bluetooth Audio","scope":scope,"operation":"semantic"}).status_code==400
    response=client.post("/api/v1/retrieval",headers=auth,json={"question":"Bluetooth Audio","scope":"21MM","operation":"semantic"})
    assert response.status_code==200
    runtime.get_service.return_value.retriever.retrieve_qdrant_only.assert_called_once_with("Bluetooth Audio",scope="21MM",current_scope="")
    assert app.state.factual_admissions.snapshot()["active"]==0
