from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app.ai.config import AISettings
from app.ai.answer_policy import matches_language
from app.ai.retrieval import EvidenceItem, RetrievalResult
from app.ai.rag_service import EnterpriseRAGService
from app.ai.request_understanding import RequestContract
from app.ai.spec_query import MODEL_ID, LOCAL_MODEL, SpecificationQueryService, load_spec_settings
from app.server.api import create_app


@pytest.mark.parametrize("answer,lang", [
    ("式样目录：550《ブルートゥースオーディオ機能仕様書》", "zh"),
    ("Specification catalog: 550《蓝牙音频功能式样书》", "en"),
])
def test_source_titles_do_not_override_the_answer_language(answer, lang):
    assert matches_language(answer, lang)


@pytest.mark.parametrize("stream", [False, True])
def test_dedicated_api_uses_only_spec_model_and_service(stream):
    result = RetrievalResult(evidence=(), resolved_scopes=("21MM",), route="SPEC_VERSION_FACTS")
    service = SimpleNamespace(
        settings=SimpleNamespace(chat_model=LOCAL_MODEL),
        answer=Mock(return_value=("已查询历史版本", result, 0)),
        answer_stream=Mock(return_value=iter(["已查询历史版本"])),
    )
    runtime = SimpleNamespace(
        preferred_model="old-model", spec_preferred_model=LOCAL_MODEL,
        get_service=Mock(side_effect=AssertionError("Omnibus service invoked")),
        get_spec_service=Mock(return_value=service),
    )
    client = TestClient(create_app(runtime, "t" * 48), base_url="http://127.0.0.1")
    response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + "t" * 48},
                           json={"model": MODEL_ID, "messages": [{"role": "user", "content": "比较历史版本"}],
                                 "stream": stream, "scope": "21MM"})
    assert response.status_code == 200
    assert MODEL_ID in response.text
    called = service.answer_stream if stream else service.answer
    assert called.call_args.kwargs["model"] == LOCAL_MODEL
    runtime.get_service.assert_not_called()
    assert client.post("/v1/chat/completions", json={
        "model": MODEL_ID, "messages": [{"role": "user", "content": "查询"}]}).status_code == 401
    assert client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + "t" * 48},
                       json={"model": MODEL_ID, "messages": [{"role": "user", "content": "查询"}],
                             "scope": "QABUG"}).status_code == 400


def test_task_config_does_not_inherit_remote_inference_or_issue_collection(tmp_path):
    settings = load_spec_settings(tmp_path, AISettings(
        chat_model="remote-model", inference_provider="approved_openai"))
    assert settings.chat_model == LOCAL_MODEL
    assert settings.inference_provider == "ollama"
    assert "QABUG" not in settings.collections
    assert settings.num_ctx == 8192


@pytest.mark.parametrize("task,reader,route", [
    ("COMPARE", "retrieve_spec_version_facts", "SPEC_VERSION_FACTS"),
    ("RELATIONS", "retrieve_spec_relations", "SPEC_RELATIONS"),
    ("QUOTE", "retrieve_spec_chapter_content", "SPEC_CHAPTER_CONTENT"),
])
def test_spec_readers_preserve_conversation_and_project(monkeypatch, task, reader, route):
    service = SpecificationQueryService.__new__(SpecificationQueryService)
    service.settings = AISettings(chat_model=LOCAL_MODEL)
    service.ollama = Mock()
    service.ollama.chat_json.return_value = {"operations": [{
        "COMPARE": "versions", "RELATIONS": "relations", "QUOTE": "quote"}[task]]}
    service.retriever = Mock()
    getattr(service.retriever, reader).return_value = RetrievalResult(
        evidence=(), resolved_scopes=("24MM",), route=route)
    contract = RequestContract(task, "SPEC-508", 0, ("answer",), 0, ("check",), "SPEC")
    monkeypatch.setattr("app.ai.spec_query.understand", lambda *a, **kw: (contract, "查询24MM的SPEC-508"))
    question = {"COMPARE": "它以前的版本呢？", "RELATIONS": "它引用了哪些其他式样？",
                "QUOTE": "请逐字摘录它的第1章原文"}[task]
    _, result = service.prepare([
        {"role": "user", "content": "查询24MM的SPEC-508"},
        {"role": "assistant", "content": "此前的回答"},
        {"role": "user", "content": question},
    ], model=LOCAL_MODEL, scope="24MM")
    call = getattr(service.retriever, reader).call_args
    assert "SPEC-508" in call.args[0]
    assert question in call.args[0]
    assert call.kwargs["scope"] == "24MM"
    assert result.route == "SPEC_MODEL_ANSWER"
    assert result.workflow["spec_parts"][0]["route"] == route
    assert result.workflow["task_profile"] == "spec-query"
    assert result.request_id.startswith("spec-")
    service.retriever.retrieve_issue_register.assert_not_called()


@pytest.mark.parametrize("question", [
    "根据21MM SPEC-508正文概括主要功能，并给出引用依据。",
    "根据21MM SPEC-508说明主要功能，不要复述原文。",
    "Summarize 21MM SPEC-508 without quoting verbatim.",
])
def test_request_for_citations_cannot_trigger_verbatim_body_dump(monkeypatch, question):
    service = SpecificationQueryService.__new__(SpecificationQueryService)
    service.settings = AISettings(chat_model=LOCAL_MODEL)
    service.ollama = Mock()
    service.ollama.chat_json.return_value = {"operations": ["body"]}
    service.retriever = Mock()
    service._read_body = Mock(return_value=RetrievalResult(
        evidence=(), resolved_scopes=("21MM",), route="SPEC_SUMMARY"))
    contract = RequestContract("QUOTE", "SPEC-508", 0, ("quote",), 0, ("quote",), "SPEC")
    monkeypatch.setattr("app.ai.spec_query.understand", lambda *a, **kw: (contract, question))
    _, result = service.prepare([{"role": "user", "content": question}], model=LOCAL_MODEL)
    service._read_body.assert_called_once()
    service.retriever.retrieve_spec_chapter_content.assert_not_called()
    assert result.workflow["request_contract"]["task"] == "ANALYSIS"


def test_lookup_work_can_require_history_instead_of_identity():
    service = SpecificationQueryService.__new__(SpecificationQueryService)
    service.settings = AISettings(chat_model=LOCAL_MODEL)
    service.ollama = Mock()
    service.ollama.chat_json.return_value = {"operations": ["versions", "relations"]}
    contract = RequestContract("LOOKUP", "SPEC-508", 0, ("history and links",), 0, ("check",), "SPEC")
    assert service._plan_operations("查它的历史版本及工程关系", contract, LOCAL_MODEL, allowed={"versions", "relations"}) == (("versions", "relations"), ())
    service.ollama.chat_json.return_value = {"operations": ["issue_search"]}
    with pytest.raises(ValueError):
        service._plan_operations("查它的历史版本", contract, LOCAL_MODEL, allowed={"versions"})


def test_combined_history_and_relationships_use_both_read_models(monkeypatch):
    service = SpecificationQueryService.__new__(SpecificationQueryService)
    service.settings = AISettings(chat_model=LOCAL_MODEL)
    service.ollama = Mock()
    service.ollama.chat_json.return_value = {"operations": ["versions", "relations"]}
    service.retriever = Mock()
    service.retriever.retrieve_spec_version_facts.return_value = RetrievalResult(
        evidence=(), resolved_scopes=("21MM",), route="SPEC_VERSION_FACTS")
    service.retriever.retrieve_spec_relations.return_value = RetrievalResult(
        evidence=(), resolved_scopes=("21MM",), route="SPEC_RELATIONS")
    contract = RequestContract("LOOKUP", "SPEC-508", 0, ("history and links",), 0, ("check",), "SPEC")
    monkeypatch.setattr("app.ai.spec_query.understand", lambda *a, **kw: (contract, "21MM SPEC-508"))
    answer, result, _ = service.answer(
        [{"role": "user", "content": "查询21MM SPEC-508历史版本和工程关系"}], model=LOCAL_MODEL)
    assert result.route == "SPEC_MODEL_ANSWER"
    assert [part["route"] for part in result.workflow["spec_parts"]] == [
        "SPEC_VERSION_FACTS", "SPEC_RELATIONS"]
    service.retriever.retrieve_spec_version_facts.assert_called_once()
    service.retriever.retrieve_spec_relations.assert_called_once()
    service.retriever.retrieve_spec_lookup.assert_not_called()


def test_catalog_keeps_japanese_titles_in_model_evidence(monkeypatch):
    from app.ai.retrieval import EvidenceItem
    service = SpecificationQueryService.__new__(SpecificationQueryService)
    service.settings = AISettings(chat_model=LOCAL_MODEL)
    service.ollama = Mock()
    service.ollama.chat_json.return_value = {"operations": ["catalog"]}
    service.retriever = Mock()
    title = "ブルートゥースオーディオ機能仕様書"
    item = EvidenceItem(evidence_id="", scope="21MM", source_type="SPEC_CATALOG",
                       content="式样目录\\n- 550 | " + title + " | body_version=1",
                       metadata={"rows": [{"canonical_name": title}]})
    service.retriever.retrieve_spec_catalog.return_value = RetrievalResult(
        evidence=(item,), resolved_scopes=("21MM",), route="SPEC_CATALOG")
    contract = RequestContract("LOOKUP", "21MM", 0, ("list books",), 0, ("check",), "SPEC")
    monkeypatch.setattr("app.ai.spec_query.understand", lambda *a, **kw: (contract, "21MM"))
    _, result = service.prepare(
        [{"role": "user", "content": "列出21MM式样书"}], model=LOCAL_MODEL)
    assert result.route == "SPEC_MODEL_ANSWER"
    assert f"《{title}》" in result.evidence[0].content
    assert "\n- 550" in result.evidence[0].content


def test_issue_request_never_touches_retriever(monkeypatch):
    service = SpecificationQueryService.__new__(SpecificationQueryService)
    service.settings = AISettings(chat_model=LOCAL_MODEL)
    service.ollama = Mock()
    service.retriever = Mock()
    contract = RequestContract("LOOKUP", "BUG-123", 0, ("answer",), 0, ("check",), "ISSUE")
    monkeypatch.setattr("app.ai.spec_query.understand", lambda *a, **kw: (contract, "BUG-123"))
    answer, result, _ = service.answer(
        [{"role": "user", "content": "查询BUG-123"}], model=LOCAL_MODEL)
    assert result.route == "SPEC_SCOPE_REQUIRED"
    assert "对应入口" in answer
    assert not service.retriever.mock_calls


def test_followup_identity_comes_from_user_history_even_if_subject_selection_is_wrong(monkeypatch):
    service = SpecificationQueryService.__new__(SpecificationQueryService)
    service.settings = AISettings(chat_model=LOCAL_MODEL)
    service.ollama = Mock()
    service.ollama.chat_json.return_value = {"operations": ["relations"]}
    service.retriever = Mock()
    service.retriever.retrieve_spec_relations.return_value = RetrievalResult(
        evidence=(), resolved_scopes=("21MM",), route="SPEC_RELATIONS")
    contract = RequestContract("RELATIONS", "21MM", 0, ("references",), 0, ("check",), "SPEC")
    monkeypatch.setattr("app.ai.spec_query.understand", lambda *a, **kw: (contract, "查询21MM式样书目录"))
    service.prepare([
        {"role": "user", "content": "查询21MM式样书目录"},
        {"role": "user", "content": "查询21MM SPEC-508历史正文版本"},
        {"role": "user", "content": "它引用了哪些其他式样？"},
    ], model=LOCAL_MODEL, scope="21MM")
    query = service.retriever.retrieve_spec_relations.call_args.args[0]
    assert "SPEC-508" in query
    assert "查询21MM式样书目录" not in query


def test_untrusted_section_selection_falls_back_to_verified_body():
    service = SpecificationQueryService.__new__(SpecificationQueryService)
    service.settings = AISettings(chat_model=LOCAL_MODEL)
    service.ollama = Mock()
    service.ollama.chat_json.return_value = {"sections": ["forged-section"]}
    service.retriever = Mock()
    outline_item = SimpleNamespace(metadata={"outline": [{"section_id": "1", "title": "Overview"}]})
    service.retriever.retrieve_spec_outline.return_value = RetrievalResult(
        evidence=(outline_item,), resolved_scopes=("21MM",))
    service.retriever.retrieve_spec_chapter_content.return_value = RetrievalResult(
        evidence=(), resolved_scopes=("21MM",))
    result = service._read_body("解释SPEC-508功能", scope="21MM", current_scope="",
                                model=LOCAL_MODEL, cancel_check=None)
    assert result.route == "SPEC_SUMMARY"
    assert service.retriever.retrieve_spec_chapter_content.call_count == 1
    assert "forged-section" not in str(service.retriever.retrieve_spec_chapter_content.call_args)


def test_explicit_chapter_skips_model_section_selection():
    service = SpecificationQueryService.__new__(SpecificationQueryService)
    service.settings = AISettings(chat_model=LOCAL_MODEL)
    service.ollama = Mock()
    service.retriever = Mock()
    service.retriever.retrieve_spec_chapter_content.return_value = RetrievalResult(
        evidence=(), resolved_scopes=("21MM",))
    result = service._read_body("概括21MM SPEC-508第1章的适用范围", scope="21MM",
                                current_scope="", model=LOCAL_MODEL, cancel_check=None)
    assert result.route == "SPEC_SUMMARY"
    service.retriever.retrieve_spec_outline.assert_not_called()
    service.ollama.chat_json.assert_not_called()


@pytest.mark.parametrize("service_type,understand_path", [
    (EnterpriseRAGService, "app.ai.rag_service.understand"),
    (SpecificationQueryService, "app.ai.spec_query.understand"),
])
@pytest.mark.parametrize("detail", [
    "TYPE C 和 TYPE A 在这本式样书中是什么意思",
    "切换账号时哪些条件必须满足",
    "文档规定的异常处理流程是什么",
])
def test_content_question_cannot_publish_identity_record(monkeypatch, service_type, understand_path, detail):
    service = service_type.__new__(service_type)
    service.settings = AISettings(chat_model=LOCAL_MODEL)
    service.ollama = Mock()
    service.ollama.chat_json.return_value = {"operations": ["body"], "terms": ["TYPE C", "TYPE A"]}
    service.retriever = Mock()
    service._read_body = Mock(return_value=RetrievalResult(
        evidence=(EvidenceItem(evidence_id="", scope="21MM", source_type="SPEC_CHAPTER_CONTENT",
                               content="verified body", document_id="doc-508", specification_code="508",
                               metadata={"excerpts": ({"content_id": 11,
                                   "excerpt": "正文定义 [1] <tag> </details>", "section_id": "2"},)}),),
        resolved_scopes=("21MM",), route="SPEC_SUMMARY"))
    question = "请查找相关式样，解释要求或流程，并引用依据。\n式样或功能：508\n问题：" + detail
    contract = RequestContract("LOOKUP", "508", 0, ("identify document",), 0, ("check",), "SPEC")
    monkeypatch.setattr(understand_path, lambda *a, **kw: (contract, question))
    draft_call = Mock(side_effect=[
        ("冗长的结论。" * 50, "正文对该功能给出了明确规定。[E1]", ["[E1] 正文定义"]),
        ("根据正文给出解释。", "正文对该功能给出了明确规定。[E1]", ["[E1] 正文定义"]),
    ])
    monkeypatch.setattr("app.ai.rag_service.draft_from_evidence", draft_call)
    monkeypatch.setattr("app.ai.rag_service.review", lambda *a, **kw: (True, []))
    answer, result, _ = service.answer([{"role": "user", "content": question}], model=LOCAL_MODEL)
    assert result.route == "SPEC_MODEL_ANSWER"
    assert "根据正文给出解释" in answer and "document_id=doc-508" in answer
    conclusion, details = answer.split('<details type="spec_evidence" ')
    assert "正文定义" not in conclusion and "document_id" not in conclusion
    assert "分析依据" in details and "正文定义" in details and "<<<SPEC_BASIS>>>" not in answer
    assert "正文定义 [1] &lt;tag&gt; &lt;/details&gt;" in details
    assert details.count("</details>") == 1
    assert draft_call.call_count == 2
    service.retriever.retrieve_spec_lookup.assert_not_called()
    assert "SPEC-508" in service._read_body.call_args.args[0]
    assert service._read_body.call_args.kwargs["focus_terms"] == ("TYPE C", "TYPE A")
    schema = service.ollama.chat_json.call_args.kwargs["schema"]
    allowed = schema["properties"]["operations"]["items"]["enum"]
    assert "identity" not in allowed and "quote" not in allowed


def test_out_of_policy_model_operation_cannot_access_a_reader(monkeypatch):
    service = SpecificationQueryService.__new__(SpecificationQueryService)
    service.settings = AISettings(chat_model=LOCAL_MODEL)
    service.ollama = Mock()
    service.ollama.chat_json.return_value = {"operations": ["identity"]}
    service.retriever = Mock()
    question = "508式样中定义了哪些驾驶员识别条件？"
    contract = RequestContract("LOOKUP", "508", 0, ("identify",), 0, ("check",), "SPEC")
    monkeypatch.setattr("app.ai.spec_query.understand", lambda *a, **kw: (contract, question))
    _, result = service.prepare([{"role": "user", "content": question}], model=LOCAL_MODEL)
    assert result.route == "REQUEST_UNDERSTANDING_FAILED"
    assert not service.retriever.mock_calls


def test_qa_card_searches_only_ticket_register(monkeypatch):
    service = EnterpriseRAGService.__new__(EnterpriseRAGService)
    service.settings = AISettings(chat_model=LOCAL_MODEL)
    service.ollama = Mock()
    service.retriever = Mock()
    service.retriever.retrieve_issue_register.return_value = RetrievalResult(
        evidence=(), resolved_scopes=("QABUG",), route="ISSUE_QUERY")
    question = (
        "请查询相关 QA 和 BUG 记录，整理处理结论并附上来源。\n"
        "问题或 BUG：用户配置无法加载\n功能或版本：车机用户配置"
    )
    contract = RequestContract("ANALYSIS", "用户配置无法加载", 0, ("查找记录",), 0, ("总结",), "ISSUE")
    monkeypatch.setattr("app.ai.rag_service.understand", lambda *a, **kw: (contract, question))
    _, result = service.prepare([{"role": "user", "content": question}], model=LOCAL_MODEL)
    assert result.route == "ISSUE_QUERY"
    call = service.retriever.retrieve_issue_register.call_args
    assert call.kwargs["scope"] == "QABUG"
    assert call.args[0].split() == ["用户配置无法加载", "车机用户配置"]


def test_document_location_remains_a_direct_lookup(monkeypatch):
    service = SpecificationQueryService.__new__(SpecificationQueryService)
    service.settings = AISettings(chat_model=LOCAL_MODEL)
    service.ollama = Mock()
    service.ollama.chat_json.return_value = {"operations": ["identity"], "terms": []}
    service.retriever = Mock()
    service.retriever.retrieve_spec_lookup.return_value = RetrievalResult(
        evidence=(), resolved_scopes=("21MM",), route="SPEC_LOOKUP")
    question = "查找21MM的式样508"
    contract = RequestContract("LOOKUP", "508", 0, ("locate",), 0, ("check",), "SPEC")
    monkeypatch.setattr("app.ai.spec_query.understand", lambda *a, **kw: (contract, question))
    _, result = service.prepare([{"role": "user", "content": question}], model=LOCAL_MODEL)
    assert result.route == "SPEC_LOOKUP"
    service.retriever.retrieve_spec_lookup.assert_called_once()
    service.retriever.retrieve_spec_chapter_content.assert_not_called()
