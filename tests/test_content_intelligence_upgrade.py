from pathlib import Path
from types import SimpleNamespace

from app.ai.agent import FastIntentRouter
from app.ai.spec_facts import render_spec_summary
from app.ai.testing_workflow import explicit_references, testing_route, ticket_spec_query


ROOT = Path(__file__).resolve().parents[1]


def test_content_relation_wording_is_generic():
    assert FastIntentRouter.classify("508的4.8.4对应190哪段内容").route == "SPEC_RELATIONS"
    assert FastIntentRouter.classify("508引用了哪些式样书").route == "SPEC_RELATIONS"


def test_named_specification_lookup_bypasses_model_gate():
    from app.ai.spec_facts import lookup_keywords
    question = "查一下21MM的user profile式样书"
    assert FastIntentRouter.classify(question).route == "SPEC_LOOKUP"
    assert lookup_keywords(question) == ("user profile",)


def test_named_lookup_supports_all_title_languages():
    from app.ai.spec_facts import lookup_keywords
    cases = {
        "查一下21MM的Navigation HMI式样书": "Navigation HMI",
        "帮我找24MM的用户配置规格书": "用户配置",
        "検索して 21MM メータ連携 仕様書": "メータ連携",
        "打开COMMON的Audio Settings Spec": "Audio Settings",
    }
    for question, expected in cases.items():
        assert FastIntentRouter.classify(question).route == "SPEC_LOOKUP"
        assert lookup_keywords(question) == (expected,)


def test_chapter_followups_are_structured_and_language_independent():
    from app.ai.spec_facts import requested_section_selector
    contextual = "第一章内容\nConversation specification: SPEC-508"
    assert FastIntentRouter.classify(contextual).route == "SPEC_CHAPTER_CONTENT"
    assert requested_section_selector(contextual) == "1"
    assert requested_section_selector("Chapter 4.8.4 content") == "4.8.4"
    assert requested_section_selector("第十二章总结") == "12"


def test_multiturn_summary_recovers_earlier_explicit_specification():
    conversation = [
        {"role": "user", "content": "508"},
        {"role": "assistant", "content": "已定位508"},
        {"role": "user", "content": "展开说明"},
        {"role": "assistant", "content": "AI总结"},
        {"role": "user", "content": "把每一章都概括一下"},
    ]
    contextual = FastIntentRouter.contextualize("把每一章都概括一下", conversation)
    assert "SPEC-508" in contextual
    assert FastIntentRouter.classify(contextual).route == "SPEC_SUMMARY"


def test_relation_followups_keep_source_target_pair():
    conversation = [
        {"role": "user", "content": "508"},
        {"role": "assistant", "content": "已定位508"},
        {"role": "user", "content": "总结第一章"},
        {"role": "assistant", "content": "AI总结"},
        {"role": "user", "content": "哪部分内容引用的100式样书"},
    ]
    first = FastIntentRouter.contextualize("哪部分内容引用的100式样书", conversation)
    assert "source specification: SPEC-508" in first
    assert "target specification: SPEC-100" in first
    assert FastIntentRouter.classify(first).route == "SPEC_RELATIONS"
    conversation.extend([
        {"role": "assistant", "content": "section=1.2.1 content_id=786"},
        {"role": "user", "content": "把那部分内容给我"},
    ])
    second = FastIntentRouter.contextualize("把那部分内容给我", conversation)
    assert "SPEC-508 references SPEC-100" in second
    assert FastIntentRouter.classify(second).route == "SPEC_RELATIONS"
    explicit_conversation = [
        {"role": "user", "content": "508"},
        {"role": "assistant", "content": "已定位508"},
        {"role": "user", "content": "我说的是508的哪部分内容引用了100式样书"},
        {"role": "assistant", "content": "section=1.2.1 content_id=786"},
        {"role": "user", "content": "把那部分内容给我"},
    ]
    explicit_followup = FastIntentRouter.contextualize("把那部分内容给我", explicit_conversation)
    assert "SPEC-508 references SPEC-100" in explicit_followup


def test_plain_symptom_enters_pre_ticket_workflow():
    assert testing_route("蓝牙连接后切换用户，Profile没有恢复。") == "PRE_TICKET_CHECK"
    assert testing_route("CarPlay切换用户后偶发黑屏并重启") == "PRE_TICKET_CHECK"
    assert testing_route("这个现象之前有没有人报过？") == "PRE_TICKET_CHECK"
    assert testing_route("这是不是BUG，还是符合式样？") == "CONFORMANCE_CHECK"
    assert testing_route("ABC-123涉及哪本式样、哪段要求？") == "TICKET_SPEC_TRACE"


def test_real_world_relation_wording_routes_by_intent():
    for question in (
        "508和542之间有什么关系",
        "508涉及哪些式样",
        "508与蓝牙式样如何联动",
        "508的接口依据来自哪本式样",
    ):
        assert FastIntentRouter.classify(question).route == "SPEC_RELATIONS", question


def test_cpu_summary_is_position_backed_and_bounded():
    row = {
        "excerpt": "DRBFM レベルチェック表 評価結果 必達 機能要件 変更点 整合性",
        "section_id": "2.1", "content_id": 99, "page_number": 2,
    }
    item = SimpleNamespace(
        scope="24MM", file_name="150_check.xlsx", specification_version="v1",
        document_id="24MM:150@v1", specification_name="仕様書レベルチェック表",
        metadata={"excerpts": [row]},
    )
    answer = render_spec_summary(SimpleNamespace(evidence=(item,)))
    assert "分析摘要" in answer
    assert "section=2.1, content_id=99, page=2" in answer
    assert "不代表整本式样" in answer


def test_spec_summary_uses_local_ai_with_explicit_fallback():
    source = (ROOT / "app/ai/rag_service.py").read_text(encoding="utf-8")
    assert 'if retrieval.route == "SPEC_SUMMARY"' in source
    assert "OLLAMA_SUMMARY" in source
    assert "self.ollama.chat_stream(" in source
    assert "不是AI总结" in source
    assert "下面按章节概括" in source
    assert "NATURAL CONVERSATION STYLE" in source
    assert "Do not expose hidden reasoning or chain-of-thought" in source
    assert "chapter_evidence" in source
    assert r"\[\s*E\s*(\d+)\s*\]" in source


def test_validation_failure_degrades_to_source_locations_not_block_message():
    source = (ROOT / "app/ai/rag_service.py").read_text(encoding="utf-8")
    assert "def _safe_evidence_fallback" in source
    assert "yield self._safe_evidence_fallback" in source
    assert 'yield REJECTED_ANSWER' not in source
    assert "我找到了下面这些相关内容" in source


def test_ticket_to_spec_trace_continues_without_literal_reference_and_formats_sections():
    workflow = (ROOT / "app/ai/testing_workflow.py").read_text(encoding="utf-8")
    service = (ROOT / "app/ai/rag_service.py").read_text(encoding="utf-8")
    assert "ticket_to_spec_semantic_search" in workflow
    assert "semantic_query=ticket_spec_query" in workflow
    assert "## 票内明确引用" in workflow
    assert "## 根据票据内容检索出的相关式样候选" in workflow
    assert 'if retrieval.route == "ISSUE_QUERY"' in service
    assert "yield render_issue_query(retrieval)" in service


def test_ticket_feature_entity_then_clause_analysis_is_packaged():
    workflow = (ROOT / "app/ai/testing_workflow.py").read_text(encoding="utf-8")
    service = (ROOT / "app/ai/rag_service.py").read_text(encoding="utf-8")
    assert "def ticket_feature_entities" in workflow
    assert 'source_type="TICKET_ENTITY_MATCH"' in workflow
    assert "entity_codes=entity_codes" in workflow
    assert "found_entities=self.retriever._semantic_search" in workflow
    assert 'retrieval.route in {"TICKET_SPEC_TRACE","PRE_TICKET_CHECK"}' in service
    assert "结合现象与式样内容的分析" in service
    assert "只引用提供的[R编号]" in service


def test_ticket_regression_design_followup_keeps_issue_and_requires_evidence_locations():
    conversation = [
        {"role": "user", "content": "查一下MM21PROBL-138"},
        {"role": "assistant", "content": "已找到该票。"},
        {"role": "user", "content": "结合内容整理回归测试手顺和测试观点"},
    ]
    contextual = FastIntentRouter.contextualize(conversation[-1]["content"], conversation)
    assert "MM21PROBL-138" in contextual
    assert testing_route(contextual) == "TICKET_SPEC_TRACE"
    workflow = (ROOT / "app/ai/testing_workflow.py").read_text(encoding="utf-8")
    service = (ROOT / "app/ai/rag_service.py").read_text(encoding="utf-8")
    assert "test_design_requested" in workflow
    assert "回归测试建议与测试手顺" in service
    assert "前置条件、操作步骤、预期结果、观察点、优先级、依据[R编号]" in service


def test_issue_relation_followup_and_inline_filename_citation_are_resolved():
    conversation = [
        {"role": "user", "content": "查一下MM21PROBL-708"},
        {"role": "assistant", "content": "已找到该票。"},
        {"role": "user", "content": "关联哪些式样书"},
    ]
    contextual = FastIntentRouter.contextualize(conversation[-1]["content"], conversation)
    assert "MM21PROBL-708" in contextual
    assert testing_route(contextual) == "TICKET_SPEC_TRACE"
    refs = explicit_references(
        "参照仕様書: Handsfree Function Specification_v2.02.pdf ページ:93 9.7.3 表示設定初期化"
    )
    assert refs == [{
        "file_name": "Handsfree Function Specification_v2.02.pdf",
        "code": "", "version": "2.02", "section": "9.7.3", "page": 93,
    }]


def test_qa_and_bug_share_generic_ticket_to_spec_reasoning_fields():
    qa = SimpleNamespace(metadata={
        "summary": "Bluetooth显示条件确认",
        "procedure_expected": "进入Bluetooth Device Setting画面确认开关",
        "answer": "由Handsfree式样定义功能条件",
        "final_answer": "HMI侧无显示定义",
        "rationale": "参照Handsfree与HMI职责边界",
        "actual_behavior": "开关未显示",
    })
    query = ticket_spec_query(qa)
    for value in ("Bluetooth显示条件确认", "进入Bluetooth", "Handsfree式样", "HMI侧", "职责边界", "开关未显示"):
        assert value in query
    workflow = (ROOT / "app/ai/testing_workflow.py").read_text(encoding="utf-8")
    service = (ROOT / "app/ai/rag_service.py").read_text(encoding="utf-8")
    for field in ("procedure_expected", "answer", "final_answer", "rationale", "impact_analysis"):
        assert field in workflow
    assert "QA/BUG记录" in service


def test_common_spec_questions_use_focused_routes_not_raw_evidence_fallbacks():
    assert FastIntentRouter.classify("508有哪些性能要求").route == "SPEC_SUMMARY"
    assert FastIntentRouter.classify("508不同版本改了什么").route == "SPEC_VERSION_FACTS"
    assert FastIntentRouter.classify("508和542具体哪些内容有关").route == "SPEC_RELATIONS"
    workflow = (ROOT / "app/ai/testing_workflow.py").read_text(encoding="utf-8")
    service = (ROOT / "app/ai/rag_service.py").read_text(encoding="utf-8")
    facts = (ROOT / "app/ai/spec_facts.py").read_text(encoding="utf-8")
    assert "def render_symptom_workflow" in workflow
    assert 'retrieval.route in {"TICKET_SPEC_TRACE","PRE_TICKET_CHECK"}' in service
    assert "只回答用户询问的性能/时限要求" in service
    assert "已登记正文版本" in facts


def test_ticket_analysis_followup_keeps_exact_issue_and_invokes_model_analysis():
    conversation = [
        {"role": "user", "content": "总结一下MM21PROBL-708的内容"},
        {"role": "assistant", "content": "已返回票据摘要。"},
        {"role": "user", "content": "展开说说这个bug"},
    ]
    contextual = FastIntentRouter.contextualize(conversation[-1]["content"], conversation)
    assert "MM21PROBL-708" in contextual
    assert testing_route(contextual) == "TICKET_SPEC_TRACE"
    assert testing_route("分析一下MM21PROBL-708") == "TICKET_SPEC_TRACE"
    assert testing_route("为什么MM21PROBL-708判断为仕様通り") == "TICKET_SPEC_TRACE"
    assert testing_route("蓝牙设置项没显示，是否为仕様通り？") == "CONFORMANCE_CHECK"
    workflow = (ROOT / "app/ai/testing_workflow.py").read_text(encoding="utf-8")
    service = (ROOT / "app/ai/rag_service.py").read_text(encoding="utf-8")
    assert "ticket_analysis_requested" in workflow
    assert "## 模型总结与分析" in service
    assert "if ticket:" in service
    assert "TICKET_RECORDED_BUT_BODY_NOT_RESOLVED" in service
    assert "不得用‘搜不到定义’" in service


def test_conversation_state_and_question_frame_separate_history_from_new_judgment():
    from app.ai.interaction_intelligence import ConversationState, QuestionFrame
    messages = [
        {"role": "user", "content": "总结一下MM21PROBL-708"},
        {"role": "assistant", "content": "票据说当时判断为仕様通り"},
        {"role": "user", "content": "为什么这样判断？"},
    ]
    state = ConversationState.from_messages(messages)
    assert state.active_issue == "MM21PROBL-708"
    assert state.active_spec == ""  # ticket suffix 708 is not a specification code
    frame = QuestionFrame.parse(messages[-1]["content"], state)
    assert frame.goal == "EXPLAIN_RECORDED_TICKET_CONCLUSION"
    current = QuestionFrame.parse("现在它还是仕様通り吗？", state)
    assert current.goal == "ASSESS_CURRENT_CONFORMANCE"


def test_answer_evidence_governor_drops_uuid_and_header_noise():
    from app.ai.agent import EvidenceGovernor
    from app.ai.interaction_intelligence import evidence_usability
    from app.ai.retrieval import EvidenceItem
    noise = ("EEC5B0D0-333E-44F0-9799-237EDBF3E97F_221E714D-2089-4787-8538-37C7C665EF7B "
             "[Row 82] Column F: B | Column G: - | Column S: B | Column T: - | "
             "Column AB: B | Column AC: - | Column AG: B | Column AH: -")
    assert evidence_usability("[Header row 41] Screen Image")[0] == "REJECT"
    assert evidence_usability(noise)[0] == "REJECT"
    useful = EvidenceItem(evidence_id="", scope="21MM", source_type="SEMANTIC",
        content="HU shall download the user profile within 5 seconds from ACC On.", content_id=2, score=.7)
    rejected = EvidenceItem(evidence_id="", scope="21MM", source_type="SEMANTIC",
        content=noise, content_id=1, score=.99)
    governed, _, _ = EvidenceGovernor.govern((rejected, useful), 5)
    assert [x.content_id for x in governed] == [2]


def test_explicit_spec_followup_inherits_user_selected_project_scope():
    messages = [
        {"role": "user", "content": "查一下21MM的508式样书"},
        {"role": "assistant", "content": "已找到"},
        {"role": "user", "content": "508有哪些性能要求"},
    ]
    contextual = FastIntentRouter.contextualize(messages[-1]["content"], messages)
    assert "Conversation specification: SPEC-508" in contextual
    assert "Conversation scope: 21MM" in contextual
    assert FastIntentRouter.classify(contextual).route == "SPEC_SUMMARY"


def test_natural_relation_pair_requests_only_the_named_target():
    source = (ROOT / "app/ai/retrieval.py").read_text(encoding="utf-8")
    assert "具体哪些内容" in source
    assert "有关|有關" in source


def test_plain_ticket_summary_does_not_force_specification_retrieval():
    workflow = (ROOT / "app/ai/testing_workflow.py").read_text(encoding="utf-8")
    service = (ROOT / "app/ai/rag_service.py").read_text(encoding="utf-8")
    assert 'workflow["spec_evidence_requested"]' in workflow
    assert 'if not workflow["spec_evidence_requested"]' in workflow
    assert "requested_clause_limit" in service
    assert "max_tokens=900 if test_design else 650" in service


def test_schema_and_future_import_hooks_are_packaged():
    manager = (ROOT / "app/storage/schema_manager.py").read_text(encoding="utf-8")
    storage = (ROOT / "app/storage/postgres_storage.py").read_text(encoding="utf-8")
    migration = (ROOT / "database/upgrade_engineering_intelligence_v7.sql").read_text(encoding="utf-8")
    assert "upgrade_engineering_intelligence_v7.sql" in manager
    assert "backfill_version(" in storage and "backfill_content_relations(" in storage
    assert "CREATE TABLE IF NOT EXISTS public.ei_requirement_units" in migration
    assert "CREATE TABLE IF NOT EXISTS public.ei_content_relations" in migration


def test_authenticated_exact_content_locator_is_packaged():
    api = (ROOT / "app/server/api.py").read_text(encoding="utf-8")
    assert '@app.get("/api/v1/intelligence/content/{scope}/{content_id}")' in api
    assert "Depends(require_auth)" in api


def test_symptom_followup_carries_user_phenomenon_into_spec_fallback():
    messages = [
        {"role": "user", "content": "蓝牙切换用户后Profile没有恢复，先查历史QA/BUG"},
        {"role": "assistant", "content": "当前相似票不足以确认。"},
        {"role": "user", "content": "如果历史票不足以判断，应该优先查哪些式样的哪些部分？"},
    ]
    contextual = FastIntentRouter.contextualize(messages[-1]["content"], messages)
    assert "Conversation symptom:" in contextual
    assert "蓝牙切换用户后Profile没有恢复" in contextual
    from app.ai.testing_workflow import symptom_text
    assert "蓝牙切换用户后Profile没有恢复" in symptom_text(contextual)


def test_normal_ticket_relation_view_exposes_unresolved_explicit_citations():
    source = (ROOT / "app/ai/testing_workflow.py").read_text(encoding="utf-8")
    assert 'unresolved=list((result.workflow or {}).get("unresolved_references") or [])' in source
    assert "票内明确记载，但当前库中尚未精确匹配到该版本正文" in source
    assert "evidence_items=[] if w.get('kind')=='CONFORMANCE_CHECK'" in source


def test_technical_topic_related_sections_are_not_misrouted_as_spec_relations():
    messages = [
        {"role":"user","content":"查一下21MM的508式样书"},
        {"role":"assistant","content":"已找到"},
        {"role":"user","content":"其中哪些章节和Bluetooth有关？"},
    ]
    contextual=FastIntentRouter.contextualize(messages[-1]["content"],messages)
    assert FastIntentRouter.classify(contextual).route != "SPEC_RELATIONS"
    assert FastIntentRouter.classify("508和542具体哪些内容有关").route == "SPEC_RELATIONS"


def test_outgoing_source_target_question_is_not_incoming_relation_search():
    source=(ROOT / "app/ai/retrieval.py").read_text(encoding="utf-8")
    assert "incoming_requested = (not source_codes) and bool" in source


def test_evidence_type_followup_preserves_spec_and_technical_topic():
    messages=[
        {"role":"user","content":"查一下21MM的508式样书"},
        {"role":"assistant","content":"已找到"},
        {"role":"user","content":"其中哪些章节和Bluetooth有关？"},
        {"role":"assistant","content":"返回若干位置"},
        {"role":"user","content":"这些是明确要求还是语义候选？分开说明"},
    ]
    contextual=FastIntentRouter.contextualize(messages[-1]["content"],messages)
    assert "Conversation specification: SPEC-508" in contextual
    assert "Conversation technical topic: Bluetooth" in contextual
    assert "separate explicit text evidence" in contextual
