from unittest.mock import Mock

from app.ai.config import AISettings
from app.ai.retrieval import EvidenceItem, RetrievalResult
from app.ai.test_design import LOCAL_MODEL, TestDesignService as DesignService, load_test_design_settings


def service():
    svc = DesignService.__new__(DesignService)
    svc.settings = AISettings()
    svc.retriever = Mock()
    svc.ollama = Mock()
    svc.ollama.chat_json.return_value = {"tests": [{
        "title": "网络切换", "kind": "exploration", "setup": "准备同一用户的配置",
        "actions": ["加载期间切换网络", "记录屏幕状态"],
        "observe": "配置是否继续加载以及是否显示提示", "source": "",
    }]}
    return svc


def test_blank_form_requests_target_without_retrieval_or_generation():
    svc = service()
    text, result, _ = svc.answer([{"role": "user", "content": "生成测试用例\n测试对象：\n范围与关注点："}], model="fake")
    assert "请填写测试对象" in text and result.route == "TEST_DESIGN"
    assert not svc.retriever.mock_calls and not svc.ollama.mock_calls


def test_workflow_description_returns_provisional_exploration():
    svc = service()
    text, result, _ = svc.answer([{"role": "user", "content": "生成探索测试观点\n测试对象：车机加载配置\n范围与关注点：网络切换"}], model="fake")
    assert "没有取得可核对" in text and "测试设计草案" in text
    assert "### 探索观点" in text and "网络切换" in text and "待确认" in text
    assert not result.evidence and not svc.retriever.mock_calls
    mode = svc.ollama.chat_json.call_args.kwargs["schema"]["properties"]["tests"]["items"]["properties"]["kind"]
    assert mode["enum"] == ["exploration"]


def test_spec_form_resolves_bare_id_and_cites_real_excerpt():
    svc = service()
    item = EvidenceItem(evidence_id="1", scope="21MM", source_type="SPEC_CHAPTER_CONTENT",
                        document_id="demo", content="", specification_version="v1",
                        metadata={"document_role": "SPEC_BODY", "excerpts": [
                            {"section_id": "3.4", "content_id": 17, "excerpt": "车辆先识别用户，再加载配置。"}]})
    svc.retriever.retrieve_spec_chapter_content.return_value = RetrievalResult(evidence=(item,), resolved_scopes=("21MM",))
    svc.ollama.chat_json.return_value["tests"][0].update(kind="case", source="1")
    text, result, _ = svc.answer([{"role": "user", "content": "生成测试用例\n测试对象：508\n范围与关注点：配置加载"}],
                                 model="fake", scope="21MM")
    assert svc.retriever.retrieve_spec_chapter_content.call_args.args[0].startswith("SPEC-508")
    assert "### 测试用例" in text and "section=3.4" in text
    assert "content_id=17" in text and "车辆先识别用户" in text and result.evidence == (item,)


def test_model_cannot_attach_an_unavailable_source():
    svc = service()
    svc.ollama.chat_json.return_value["tests"][0]["source"] = "999"
    text, _, _ = svc.answer([{"role": "user", "content": "试试网络切换时的用户加载。"}], model="fake")
    assert "未能生成可用" in text and "999" not in text


def test_design_honors_an_explicit_count_even_when_model_returns_extra():
    svc = service()
    row = svc.ollama.chat_json.return_value["tests"][0]
    svc.ollama.chat_json.side_effect = [
        {"tests": [row, {**row, "title": "第二个场景"}]}, {"tests": [row]},
    ]
    message = "生成探索测试观点\n测试对象：车机加载配置\n范围与关注点：仅1条，观察网络切换"
    text, _, _ = svc.answer([{"role": "user", "content": message}], model="fake")
    assert "网络切换" in text and "第二个场景" not in text
    schema = svc.ollama.chat_json.call_args.kwargs["schema"]["properties"]["tests"]
    assert schema["minItems"] == schema["maxItems"] == 1


def test_multi_spec_selection_keeps_sections_and_versions_separate():
    svc = service()
    def document(query, **kwargs):
        code = "550" if "SPEC-550" in query else "703"
        item = EvidenceItem(evidence_id=code, scope="21MM", source_type="SPEC_CHAPTER_CONTENT",
                            document_id=f"demo-{code}", content="", specification_code=code,
                            metadata={"identity_verified": True, "excerpts": [
                                {"section_id": "2", "content_id": int(code), "excerpt": f"该章节描述功能 {code}。"}]})
        return RetrievalResult(evidence=(item,), resolved_scopes=("21MM",))
    svc.retriever.retrieve_spec_chapter_content.side_effect = document
    evidence, passages, _, warnings = svc._sources(
        "21MM SPEC-550 v2.0 第2节、21MM SPEC-703 v3.0 第5节\n覆盖切换",
        "AUTO", "", None)
    assert [call.args[0] for call in svc.retriever.retrieve_spec_chapter_content.call_args_list] == [
        "21MM SPEC-550 v2.0 第2节", "21MM SPEC-703 v3.0 第5节",
    ]
    assert len(evidence) == len(passages) == 2 and not warnings


def test_bug_regression_uses_exact_ticket_without_treating_it_as_spec():
    svc = service()
    ticket = EvidenceItem(evidence_id="", scope="QABUG", source_type="ISSUE_EXACT", content="Demo ticket",
                          document_id="demo-qa", metadata={"issue_key": "DEMO-123",
                              "phenomenon": "切换音源后无声", "repro_steps": "播放音频时切换来源"})
    svc.retriever.retrieve_issue_register.return_value = RetrievalResult(evidence=(ticket,), resolved_scopes=("QABUG",))
    svc.ollama.chat_json.return_value["tests"][0].update(
        kind="case", title="切换音源回归", actions=["播放音频时切换来源"], observe="记录切换后是否无声", source="1")
    answer, result, _ = svc.answer([{"role": "user", "content": "生成测试用例\n测试对象：DEMO-123\n范围与关注点：回归"}], model="fake")
    assert "切换音源回归" in answer and "DEMO-123" in answer and "待确认" in answer
    assert result.evidence == (ticket,) and not svc.retriever.retrieve_spec_chapter_content.called


def test_review_uses_corrected_actions_instead_of_draft_assumptions():
    svc = service()
    row = svc.ollama.chat_json.return_value["tests"][0]
    svc.ollama.chat_json.side_effect = [
        {"tests": [{**row, "setup": "必须绑定品牌账户"}]},
        {"tests": [{**row, "setup": "准备待测功能和可切换的网络"}]},
    ]
    answer, _, _ = svc.answer([{"role": "user", "content": "观察网络切换对音频播放有什么影响。"}], model="fake")
    assert "品牌账户" not in answer and "可切换的网络" in answer
    assert "1. 加载期间切换网络" in answer and "2. 记录屏幕状态" in answer


def test_task_profile_uses_local_inference_independent_of_shared_provider(tmp_path):
    settings = load_test_design_settings(tmp_path, AISettings(chat_model="remote", inference_provider="approved_openai"))
    assert settings.inference_provider == "ollama"
    assert settings.chat_model == LOCAL_MODEL
