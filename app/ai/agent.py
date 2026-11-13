from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Any

from app.ai.ollama_client import OllamaClient
from app.ai.internal_query import interpret_internal_query
from app.ai.retrieval import EnterpriseRetriever, EvidenceItem, RetrievalResult
from app.engineering_intelligence.temporal_query import parse_temporal_audit_ids
from app.engineering_intelligence.workflow import build_workflow
from app.engineering_intelligence.ontology import expand_engineering_query
from app.skills.live_workflows import (build_live_skill_workflow, evidence_facet, missing_evidence_facets)


ALLOWED_TOOLS = {
    "semantic_search",
    "sql_exact_search",
    "relation_search",
    "version_search",
    "unresolved_reference_search",
    "specification_catalog",
    "specification_lookup",
    "issue_search",
    "issue_statistics",
    "system_metadata",
}


@dataclass(frozen=True, slots=True)
class QueryPlan:
    scopes: tuple[str, ...]
    intents: tuple[str, ...]
    entities: tuple[str, ...]
    rewritten_queries: tuple[str, ...]
    tools: tuple[str, ...]
    planner_source: str = "fallback"
    summary: str = ""


@dataclass(frozen=True, slots=True)
class AgentTraceStep:
    stage: str
    detail: str
    duration_ms: int = 0
    status: str = "OK"


@dataclass(frozen=True, slots=True)
class AgentPlanResult:
    plan: QueryPlan
    duration_ms: int
    trace: tuple[AgentTraceStep, ...]


@dataclass(frozen=True, slots=True)
class FastRouteDecision:
    route: str
    reason: str
    intent: str = ""
    domain: str = "GENERAL"


class FastIntentRouter:
    """Deterministic enterprise front-door router.

    The UI still exposes a single AI AUTO experience.  This router only handles
    *unambiguous* intents that are safer and faster as structured operations
    (catalog/count/exact issue lookups). Ambiguous engineering questions are
    handed to the LLM planner, which selects from the read-only tool allow-list.
    Ordinary conversation never touches the engineering repository.
    """

    _DIRECT_PATTERNS = (
        r"^(?:你?好|您好|哈喽|嗨|早上好|上午好|下午好|晚上好)[!！。,.，\s]*$",
        r"^(?:hello|hi|hey|good\s+(?:morning|afternoon|evening))[!?.\s]*$",
        r"^(?:谢谢|多谢|感谢|thanks|thank\s+you)[!！。,.，\s]*$",
        r"^(?:再见|拜拜|bye|goodbye)[!！。,.，\s]*$",
        r"^(?:你是谁|你能做什么|介绍一下你自己|测试一下|test)[?？!！。,.，\s]*$",
    )

    _ENGINEERING_HINT = re.compile(
        r"(?:式样|式樣|仕様|规格|規格|spec(?:ification)?|section|章节|章節|requirement|要件|"
        r"接口|インターフェース|功能|機能|流程|フロー|处理|処理|设计|設計|条件|信号|signal|"
        r"profile|ecu|hmi|sequence|シーケンス|use[- ]?case|引用|参照|參照|reference|relation|关联|關聯|"
        r"版本|revision|rev|qdrant|chunk|evidence|证据|證據)",
        flags=re.I,
    )
    _SYSTEM_META_HINT = re.compile(
        r"(?=.*(?:qdrant|postgres(?:ql)?|sql|schema|table|view|索引|index|collection|集合|embedding|向量|rag|一键建表|一鍵建表|建库|建庫|payload))"
        r"(?=.*(?:当前|目前|现在|現在|这个工具|這個工具|配置|状态|狀態|有哪些|多少|列表|清单|清單|检查|檢查|一键|一鍵|创建|建立|索引|schema|table|collection))",
        flags=re.I,
    )

    _ISSUE_HINT = re.compile(
        r"(?:jira|(?<![A-Za-z0-9])bug(?![A-Za-z0-9])|(?<![A-Za-z0-9])qa(?![A-Za-z0-9])|"
        r"defect|issue|ticket|障害|不具合|バグ|課題|缺陷|问题单|問題單|"
        r"测试问题|測試問題|测试结果|測試結果|担当者|assignee|priority|severity)",
        flags=re.I,
    )
    # Do not use Unicode \\b here: Chinese/Japanese letters are word characters,
    # so e.g. "BUG-123现在" has no word-boundary after the numeric suffix.
    _ISSUE_KEY = re.compile(r"(?<![A-Z0-9])(?!SPEC-)([A-Z][A-Z0-9]{1,15}-\d{1,9})(?![A-Z0-9])", re.I)
    _CATALOG = re.compile(
        r"(?:库里|庫里|数据库里|資料庫裡|当前库|目前库|現在.*庫|current\s+(?:repository|database)).{0,10}"
        r"(?:有哪些|有什么|有什麼|几本|幾本|多少|数量|數量|列出|清单|清單|一覧|list).{0,8}"
        r"(?:式样书|式樣書|仕様書|规格|規格|spec(?:ification)?s?)|"
        r"(?:有哪些|有什么|有什麼|多少|几本|幾本|数量|數量|列出|清单|清單|一覧|list).{0,8}(?:式样书|式樣書|仕様書|规格|規格|spec(?:ification)?s?)|"
        r"(?:式样书|式樣書|仕様書|规格|規格|spec(?:ification)?s?).{0,8}(?:有哪些|有什么|有什麼|多少|几本|幾本|数量|數量|列出|清单|清單|一覧|list)",
        re.I,
    )
    _COUNT_HINT = re.compile(r"(?:多少|几个|幾個|几条|幾條|统计|統計|分布|count|how many|件数|何件|集計)", re.I)
    _ISSUE_FIND_ONE = re.compile(
        r"(?:找(?:一|1)(?:个|個|条|條)?|给我(?:找)?(?:一|1)(?:个|個|条|條)?|給我(?:找)?(?:一|1)(?:個|條)?|"
        r"推荐(?:一|1)(?:个|個|条|條)?|推薦(?:一|1)(?:個|條)?|举(?:一|1)(?:个|個)?|舉(?:一|1)(?:個)?|"
        r"one\s+(?:jira|bug|issue|ticket)|(?:jira|issue|ticket)\s*(?:号|號|key|id))",
        re.I,
    )
    _FOLLOWUP_HINT = re.compile(
        r"(?:这个|這個|该问题|該問題|这个问题|這個問題|这个bug|這個bug|这个qa|這個qa|这张票|這張票|它|那条|那條|上一个|上一個|上面|刚才|剛才|"
        r"负责人|負責人|谁负责|誰負責|状态呢|狀態呢|版本呢|具体呢|具體呢|为什么|為什麼|依据呢|依據呢|证据呢|證據呢|哪里写|哪裡寫|"
        r"关联哪些式样|關聯哪些式樣|涉及哪些式样|涉及哪些式樣|对应哪些条款|對應哪些條款|结合式样|結合式樣|"
        r"展开说说|展開說說|继续分析|繼續分析|详细分析|詳細分析|总结一下|總結一下|"
        r"回归测试|回歸測試|测试观点|測試觀點|测试点|測試點|测试手顺|測試手順|操作手顺|操作手順|测试用例|測試案例|"
        r"当前库|當前庫|这个结论|這個結論|能否证明|能證明|能够证明|能夠證明|缺什么材料|缺什麼材料|"
        r"根据目前|根據目前|再查相关|再查相關|下一步最小|明确要求|明確要求|语义候选|語義候選)",
        re.I,
    )

    @classmethod
    def contextualize(cls, question: str, conversation: list[dict[str, str]]) -> str:
        """Resolve conservative follow-up references from recent user turns.

        This is not free-form memory inference. Only explicit issue keys or
        specification identifiers from recent user messages are carried forward,
        and only when the new turn clearly looks referential.
        """
        current = re.sub(r"\s+", " ", str(question or "").strip())
        current = re.sub(r"按\s*章\s*[节節]", "按章节", current)
        if not current:
            return current
        # Multi-turn document navigation is resolved from explicit identities in
        # recent USER messages only. Once a user selected “21MM 508”, natural
        # follow-ups such as “章节内容展开” or “按目录继续” must not require them
        # to repeat the identifier, and must never trust an assistant-produced
        # title as conversation state.
        document_navigation = bool(re.search(
            r"(?:当前式样|當前式樣|当前规格|當前規格|流程|步骤|步驟|登录|登入|登出|切换|切換|恢复|恢復|保存|同步|下载|下載|上传|上傳|如何|怎么|怎麼|版本|版次|version|revision|章节|章節|目录|目錄|正文|内容|內容|条款|條款|原文|页码|頁碼|"
            r"展开|展開|继续|繼續|详细|詳細|具体|具體|说说|說說|介绍|介紹|概括|总结|總結|梳理|"
            r"测试点|測試點|测试项|測試項|周边测试|影響範囲|影响范围|还有哪些|還有哪些)",
            current,
            re.I,
        ))
        document_navigation = document_navigation or bool(re.search(r"\bv(?:er\.?)?\s*\d+(?:\.\d+)+", current, re.I))
        current_codes = EnterpriseRetriever._entity_spec_codes((), current)
        if not current_codes and re.search(
                r"式样|式樣|仕様|spec|性能要求|性能要件|章节|章節|版本|引用|参照|參照|关联|關聯", current, re.I):
            current_codes = tuple(dict.fromkeys(re.findall(r"(?<!\d)(\d{3,6})(?!\d)", current)))[:8]
        # Relation conversations carry two distinct entities: the selected
        # source document and the referenced target.  A target-only question
        # such as "哪部分引用100" must not replace the already selected 508;
        # and "把那部分内容给我" must inherit the last explicit 508 -> 100 pair.
        relation_words = re.search(
            r"(?:引用|参照|參照|参考|參考|依赖|依賴|关联|關聯|关系|關係|有关|有關|对应|對應|哪部分|哪段|那部分|该部分|該部分|侧.{0,8}(?:原文|章节|content_id)|側.{0,8}(?:原文|章節|content_id))",
            current, re.I,
        )
        recent_users: list[str] = []
        skipped_relation_current = False
        for item in reversed(conversation):
            if str(item.get("role") or "").lower() != "user":
                continue
            text = re.sub(r"\s+", " ", str(item.get("content") or "").strip())
            if not text:
                continue
            if not skipped_relation_current and text == current:
                skipped_relation_current = True
                continue
            recent_users.append(text)
            if len(recent_users) >= 12:
                break
        evidence_type_followup=bool(re.search(
            r"(?:明确|明確).{0,16}(?:候选|候選)|(?:候选|候選).{0,16}(?:明确|明確)",current,re.I))
        if evidence_type_followup:
            selected_code=""
            selected_scope=""
            topic=""
            for previous in recent_users:
                if not topic:
                    hit=re.search(r"(Bluetooth|MiraCast|Profile|蓝牙|藍牙|Handfree|DCM|Wi-?Fi)",previous,re.I)
                    if hit: topic=hit.group(1)
                codes=EnterpriseRetriever._entity_spec_codes((),previous)
                if not selected_code and len(codes)==1: selected_code=codes[0]
                scope_hit=re.search(r"(?<![A-Z0-9])(21MM|24MM|COMMON)(?![A-Z0-9])",previous,re.I)
                if not selected_scope and scope_hit: selected_scope=scope_hit.group(1).upper()
            suffix=(f"\nConversation specification: SPEC-{selected_code}" if selected_code else "")
            suffix+=(f"\nConversation scope: {selected_scope}" if selected_scope else "")
            suffix+=(f"\nConversation technical topic: {topic}" if topic else "")
            return current+suffix+"\nAnswer mode: separate explicit text evidence from semantic candidates"
        # A tester often describes a phenomenon and then asks which
        # specifications to inspect when ticket history is insufficient. Carry
        # only that user-authored phenomenon, never an assistant interpretation.
        symptom_followup=bool(re.search(
            r"(?:历史票|歷史票|QA|BUG).{0,18}(?:不足|没有|沒有|未找到).{0,30}"
            r"(?:查哪些|查哪本|哪些式样|哪些式樣|哪些内容|哪些內容|优先查|優先查)|"
            r"(?:再查相关式样|再查相關式樣|根据目前结果|根據目前結果|下一步最小确认|下一步最小確認)",
            current,re.I,
        ))
        if symptom_followup:
            for previous in recent_users:
                if cls._ISSUE_KEY.search(previous) or len(previous) < 8:
                    continue
                if re.search(r"(?:现象|現象|无法|無法|不能|没有|沒有|不显示|不表示|失败|失敗|恢复|恢復|连接|連接|切换|切換)",previous,re.I):
                    scope_match=re.search(r"(?<![A-Z0-9])(21MM|24MM|COMMON)(?![A-Z0-9])",previous,re.I)
                    suffix=f"\nConversation scope: {scope_match.group(1).upper()}" if scope_match else ""
                    return f"{current}\nConversation symptom: {previous}{suffix}"
        if relation_words:
            # Prefer the latest explicit source/target pair from a user turn.
            for previous in recent_users:
                pair_match = re.search(
                    r"(?<!\d)(\d{3,6})(?!\d).{0,50}(?:引用|参照|參照|参考|參考|依赖|依賴|关联|關聯|关系|關係|有关|有關|对应|對應|和|与|與)"
                    r".{0,50}(?<!\d)(\d{3,6})(?!\d)", previous, re.I,
                )
                if pair_match:
                    source, target = pair_match.group(1), pair_match.group(2)
                    return (
                        f"{current}\nConversation relation: SPEC-{source} references SPEC-{target}\n"
                        f"Conversation source specification: SPEC-{source}\n"
                        f"Conversation target specification: SPEC-{target}"
                    )
                previous_codes = EnterpriseRetriever._entity_spec_codes((), previous)
                if (len(previous_codes) >= 2 and re.search(
                        r"(?:引用|参照|參照|参考|參考|依赖|依賴|关联|關聯|关系|關係|有关|有關|对应|對應|和|与|與)", previous, re.I)):
                    return (
                        f"{current}\nConversation relation: SPEC-{previous_codes[0]} references SPEC-{previous_codes[1]}\n"
                        f"Conversation source specification: SPEC-{previous_codes[0]}\n"
                        f"Conversation target specification: SPEC-{previous_codes[1]}"
                    )
            # The current turn names only the target; inherit the most recent
            # different, unambiguous selected specification as the source.
            if len(current_codes) == 1:
                target = current_codes[0]
                for previous in recent_users:
                    previous_codes = EnterpriseRetriever._entity_spec_codes((), previous)
                    if len(previous_codes) == 1 and previous_codes[0] != target:
                        return (
                            f"{current}\nConversation relation: SPEC-{previous_codes[0]} references SPEC-{target}\n"
                            f"Conversation source specification: SPEC-{previous_codes[0]}\n"
                            f"Conversation target specification: SPEC-{target}"
                        )
            elif not current_codes:
                # Elliptical follow-up: recover the target from the latest
                # relation question and the source from the earlier selection.
                for index, previous in enumerate(recent_users):
                    previous_codes = EnterpriseRetriever._entity_spec_codes((), previous)
                    if (len(previous_codes) != 1 or not re.search(
                            r"(?:引用|参照|參照|参考|參考|依赖|依賴|哪部分|哪段)", previous, re.I)):
                        continue
                    target = previous_codes[0]
                    for earlier in recent_users[index + 1:]:
                        earlier_codes = EnterpriseRetriever._entity_spec_codes((), earlier)
                        if len(earlier_codes) == 1 and earlier_codes[0] != target:
                            return (
                                f"{current}\nConversation relation: SPEC-{earlier_codes[0]} references SPEC-{target}\n"
                                f"Conversation source specification: SPEC-{earlier_codes[0]}\n"
                                f"Conversation target specification: SPEC-{target}"
                            )
        # The current turn may repeat the specification number but omit the
        # project selected one turn earlier ("21MM 508" -> "508有哪些性能要求").
        # Keep that user-authored scope; otherwise the same numeric code across
        # projects becomes ambiguous and a valid follow-up incorrectly returns
        # no evidence.
        if current_codes and not re.search(
                r"(?<![A-Z0-9])(?:21MM|24MM|COMMON)(?![A-Z0-9])", current, re.I):
            for previous in recent_users:
                scope_match = re.search(
                    r"(?<![A-Z0-9])(21MM|24MM|COMMON)(?![A-Z0-9])", previous, re.I)
                if scope_match:
                    identity = (f"\nConversation specification: SPEC-{current_codes[0]}"
                                if len(current_codes) == 1 else "")
                    return f"{current}{identity}\nConversation scope: {scope_match.group(1).upper()}"
        if document_navigation and not current_codes:
            prior_users: list[str] = []
            skipped_current = False
            for item in reversed(conversation):
                if str(item.get("role") or "").lower() != "user":
                    continue
                text = re.sub(r"\s+", " ", str(item.get("content") or "").strip())
                if not text:
                    continue
                if not skipped_current and text == current:
                    skipped_current = True
                    continue
                prior_users.append(text)
                if len(prior_users) >= 12:
                    break
            for previous in prior_users:
                codes = EnterpriseRetriever._entity_spec_codes((), previous)
                if len(codes) != 1:
                    continue
                scope_match = re.search(r"(?<![A-Z0-9])(21MM|24MM|COMMON)(?![A-Z0-9])", previous, re.I)
                scope = scope_match.group(1).upper() if scope_match else ""
                return f"{current}\nConversation specification: SPEC-{codes[0]}" + (f"\nConversation scope: {scope}" if scope else "")
        # User-facing requests such as "按章节总结一下内容" or "总结一下"
        # refer to the last unambiguous named PDF, not to a fresh, broad search.
        # Reuse ONLY an explicit title written by a prior user. Do not mine
        # assistant responses, nor promote a document to a numeric spec code.
        content_request = current
        # Formatting suffixes must not discard the prior user-specified PDF.
        # Strip only a closed vocabulary of citation/location instructions;
        # a new scope, version, filename or technical condition is not stripped.
        formatting = re.fullmatch(
            r"(.+?)[，,；;]\s*(?:并|同时|请)(?:保留|给出|提供|附上|显示)"
            r"(?:原文|的|及|和|与|、|，|,|页码|章节|content_id|引用|出处|位置|信息|定位|\s)+[。.!！?？]*",
            current, re.I,
        )
        if formatting:
            content_request = formatting.group(1).strip()
        content_followup = bool(re.fullmatch(
            r"(?:请|請)?(?:按(?:章节|章節|章|目录|目錄)|逐(?:章|章节|章節))?"
            r"(?:概括|总结|總結|归纳|歸納|梳理|说明|說明|介绍|介紹)"
            r"(?:一下|下)?(?:这份|這份|该|該|其)?(?:文档|文件|规格书|規格書|式样书|式樣書|仕様書|的)?"
            r"(?:正文|技术内容|技術內容|章节内容|章節內容|内容|內容)?[。.!！?？\s]*",
            content_request, flags=re.I,
        ) or re.fullmatch(r"按(?:章节|章節|章|目录|目錄)(?:总结|總結|概括|梳理)(?:一下)?(?:内容|內容)?[。.!！?？\s]*", content_request))
        if content_followup:
            prior = [str(m.get('content') or '') for m in conversation
                     if str(m.get('role') or '').lower() == 'user'
                     and str(m.get('content') or '').strip() != current]
            # Bound the session scan and require exact versioned spec title.
            title_re = re.compile(
                r"([A-Za-z][A-Za-z0-9 _().&-]{2,140}?\b(?:spec|specification)\s+v\d+(?:\.\d+){1,3}(?:\.pdf)?)",
                flags=re.I,
            )
            for previous in reversed(prior[-12:]):
                match = title_re.search(previous)
                if not match:
                    continue
                document = match.group(1).strip()
                # Restrict the expression to the name rather than previous
                # commands, avoiding accidental SQL wildcard expansion.
                if len(document) > 150:
                    continue
                scopes = [re.search(r"(?<![A-Z0-9])(21MM|24MM)(?![A-Z0-9])", t, re.I)
                          for t in reversed(prior[-12:])]
                chosen_scope = next((m.group(1).upper() for m in scopes if m), '')
                return f"{current} {chosen_scope} {document}".strip()
        if not cls._FOLLOWUP_HINT.search(current) and not re.search(r"process|workflow|steps|summari[sz]e|summary|analy[sz]e|手順|フロー|流れ|要約|説明", current, re.I):
            return current
        if cls._ISSUE_KEY.search(current) or re.search(r"(?:SPEC[-_: ]*)\d{3,6}", current, re.I):
            return current

        prior_user: list[str] = []
        skipped_current = False
        for item in reversed(conversation):
            if str(item.get("role") or "").lower() != "user":
                continue
            text = re.sub(r"\s+", " ", str(item.get("content") or "").strip())
            if not text:
                continue
            if not skipped_current and text == current:
                skipped_current = True
                continue
            prior_user.append(text)
            if len(prior_user) >= 6:
                break

        for text in prior_user:
            issue = cls._ISSUE_KEY.search(text)
            if issue:
                return f"{current}\nConversation issue key: {issue.group(0).upper()}"
        # Resolve the most recent explicit specification identity before
        # interpreting generic intermediate turns such as "展开说明".  The old
        # loop returned "specification domain" on that intermediate turn and
        # never reached an earlier user message containing SPEC-508.
        for text in prior_user:
            codes = EnterpriseRetriever._entity_spec_codes((), text)
            if codes:
                scope_match = re.search(r"(?<![A-Z0-9])(21MM|24MM|COMMON)(?![A-Z0-9])", text, re.I)
                suffix = f"\nConversation scope: {scope_match.group(1).upper()}" if scope_match else ""
                return f"{current}\nConversation specification: SPEC-{codes[0]}{suffix}"
        for text in prior_user:
            if cls._ENGINEERING_HINT.search(text) or interpret_internal_query(text).specification_intent:
                # Only carry a small, explicit technical vocabulary, never raw
                # assistant output or arbitrary previous text/instructions.
                anchors = []
                for expr, name in (
                    (r"(?<![a-z0-9])BT(?![a-z0-9])|bluetooth|蓝牙|藍牙", "Bluetooth"),
                    (r"(?<![a-z0-9])DCM(?![a-z0-9])", "DCM"),
                    (r"(?<![a-z0-9])(?:HFP|BLE|HU)(?![a-z0-9])", None),
                    (r"(?<![A-Za-z0-9])21MM(?![A-Za-z0-9])|(?<![A-Za-z0-9])24MM(?![A-Za-z0-9])", None),
                ):
                    hit = re.search(expr, text, re.I)
                    if hit:
                        anchors.append(name or hit.group(0).upper())
                if anchors:
                    return f"{current}\nConversation technical topic: {', '.join(dict.fromkeys(anchors))}"
                return f"{current}\nConversation domain: specification"
        for text in prior_user:
            if cls._ISSUE_HINT.search(text):
                return f"{current}\nConversation domain: QA/BUG issue"
        return current

    @classmethod
    def classify(cls, question: str) -> FastRouteDecision:
        value = re.sub(r"\s+", " ", str(question or "").strip())
        if not value:
            return FastRouteDecision("DIRECT_CHAT", "empty-or-whitespace input", "DIRECT_CHAT", "GENERAL")

        # Open WebUI users commonly copy one row from the deterministic catalog
        # back into the chat to select that specification. Treat the catalog's
        # own wire format as an exact identity lookup instead of invoking Ollama.
        # The expression is intentionally strict so ordinary numbered prose is
        # not accidentally reclassified.
        if re.fullmatch(
            r"\d{3,6}\s*\|\s*[^|]{1,240}\s*\|\s*"
            r"body_version\s*=\s*[^|]{1,100}\s*\|\s*"
            r"status\s*=\s*[A-Z][A-Z0-9_-]{1,50}"
            r"(?:\s+(?:请|請)?(?:查询|查詢|查找|打开|打開|看看|这个|這個|这本|這本|你?上面写了有这本|你?上面寫了有這本).*)?",
            value,
            flags=re.I,
        ):
            return FastRouteDecision(
                "SPEC_LOOKUP",
                "catalog row selected for authoritative PostgreSQL identity lookup",
                "SPECIFICATION_LOOKUP",
                "SPEC",
            )
        if parse_temporal_audit_ids(value):
            return FastRouteDecision('ENGINEERING_AGENT',
                'explicit Stage 3 historical relation audit coordinates',
                'TEMPORAL_RELATION_AUDIT', 'SPEC')

        # Questions about this console's own RAG/SQL/Qdrant runtime must be
        # answered from live introspection/static contracts, never model memory.
        if cls._SYSTEM_META_HINT.search(value):
            return FastRouteDecision("SYSTEM_META", "runtime/index/schema introspection intent detected", "SYSTEM_METADATA", "SYSTEM")

        # Explicit cross-domain engineering questions must not be swallowed
        # by the single-database issue shortcut. A normal issue-key lookup
        # still uses the faster exact QA/BUG path.
        interpreted = interpret_internal_query(value)
        from app.ai.testing_workflow import testing_route
        testing=testing_route(value)
        if testing:
            return FastRouteDecision(testing,"fixed testing Skill: issue-first branches and literal references","TESTING_WORKFLOW","SPEC")
        cross_domain = bool(
            (interpreted.cross_domain and re.search(
                r"(?:相关|關聯|关联|关系|依赖|影响|有没有|有无|包括|哪些|查|搜索|找|对照|核对|符合|满足|滿足|一致|违背|違背|cross|relation|impact|change|diff)", value, re.I,
            ))
            or ((cls._ISSUE_HINT.search(value) or cls._ISSUE_KEY.search(value))
                and re.search(r"(?:21MM|24MM|式样书|仕様書|spec(?:ification)?|(?:^|\D)\d{3,6}(?!\d))", value, re.I)
                and re.search(r"(?:关联|关系|依赖|影响|版本|变更|对照|核对|符合|满足|滿足|一致|违背|違背|cross|relation|impact|change|diff)", value, re.I))
        )
        if cross_domain:
            return FastRouteDecision("ENGINEERING_AGENT", "explicit cross-domain specification and QA/BUG analysis", "CROSS_DOMAIN_ENGINEERING", "SPEC")

        # Jira/QA/BUG registers use structured SQL for exact keys, status,
        # priority and counts; semantic Qdrant is added only when free-text
        # symptom/description matching is useful.
        issue_key = cls._ISSUE_KEY.search(value)
        if cls._ISSUE_HINT.search(value) or issue_key:
            if cls._COUNT_HINT.search(value):
                return FastRouteDecision("ISSUE_STATS", "QA/BUG aggregate intent detected", "ISSUE_STATISTICS", "ISSUE")
            if issue_key:
                return FastRouteDecision("ISSUE_QUERY", "explicit QA/BUG issue key detected", "ISSUE_EXACT_LOOKUP", "ISSUE")
            if cls._ISSUE_FIND_ONE.search(value):
                return FastRouteDecision("ISSUE_QUERY", "QA/BUG single-result semantic search intent detected", "ISSUE_SEARCH_ONE", "ISSUE")
            return FastRouteDecision("ISSUE_QUERY", "QA/BUG search intent detected", "ISSUE_SEARCH", "ISSUE")

        # Entity-first read routing: the wording around an unambiguous
        # specification identity is irrelevant.  A single code plus a project,
        # specification noun, or short find/open command means “show the
        # registered record” unless the message explicitly asks for technical
        # content, comparison, relations, or an engineering conclusion.
        spec_codes = EnterpriseRetriever._entity_spec_codes((), value)
        if not spec_codes and re.search(
            r"(?:章节|章節|目录|目錄|性能|要求|要件|条款|條款|版本|变更|變更|差异|差異|引用|参照|參照|"
            r"关联|關聯|关系|關係|有关|有關|联系|聯繫|对应|對應|内容|內容)", value, re.I,
        ):
            spec_codes=tuple(dict.fromkeys(
                token for token in re.findall(r"(?<!\d)(\d{3,6})(?!\d)",value)
                if not re.fullmatch(r"(?:19|20)\d{2}",token)
            ))[:8]
        identity_context = bool(
            re.search(r"(?:21MM|24MM|COMMON|式样书?|式樣書?|仕様書|规格书?|規格書?|spec(?:ification)?)", value, re.I)
            or re.fullmatch(
                r"(?:(?:请|請)?(?:(?:给我|給我|帮我|幫我)\s*|(?:给我|給我|帮我|幫我)?\s*(?:找|查|搜|看|打开|打開)(?:一下|下|找|询|詢)?\s*))?"
                r"\d{3,6}(?:\s*(?:这本|這本))?[?？。.!！\s]*",
                value,
                re.I,
            )
        )
        engineering_detail = bool(re.search(
            r"(?:process|workflow|steps|手順|フロー|流れ|summari[sz]e|summary|analy[sz]e|要約|説明|流程|步骤|步驟|内容|內容|功能|機能|要求|要件|条款|條款|章节|章節|页码|頁碼|"
            r"展开|展開|继续|繼續|详细|詳細|具体|具體|说说|說說|介绍|介紹|"
            r"分析|总结|總結|概括|归纳|歸納|梳理|变动|變動|改动|改動|区别|區別|差异|差異|对比|對比|变更|變更|修改|关联|關聯|关系|關係|"
            r"引用|参照|參照|参考|參考|reference|references|依赖|依賴|dependency|dependencies|"
            r"上游|下游|被谁引用|被誰引用|影响|影響|涉及|联动|聯動|调用|調用|对接|對接|接口依据|"
            r"符合|满足|滿足|原因|为什么|為什麼|如何|怎么|怎麼|业务问题|業務問題|解决什么|解決什麼)",
            value,
            re.I,
        ))
        if len(spec_codes) == 1 and identity_context and not engineering_detail:
            return FastRouteDecision(
                "SPEC_LOOKUP",
                "entity-first authoritative specification identity lookup",
                "SPECIFICATION_LOOKUP",
                "SPEC",
            )
        # A name is an equally valid specification identity.  Users should not
        # need to know the numeric code before asking for a registered body.
        # Keep this before the generative engineering lane so a plain
        # "find <project> <name> specification" request is answered directly
        # from PostgreSQL and never rejected by the LLM citation gate.
        if (not spec_codes and identity_context and not engineering_detail
                and re.search(r"(?:找|查|搜|搜索|打开|打開|看看|定位|探して|検索して|調べて|見せて|find|search|lookup|open)", value, re.I)):
            return FastRouteDecision(
                "SPEC_LOOKUP",
                "named specification identity lookup from PostgreSQL",
                "SPECIFICATION_LOOKUP",
                "SPEC",
            )
        if spec_codes and re.search(
            r"(?:(?:哪些|什么|什麼|几个|幾個|所有|全部|列出).{0,8}(?:版本|版次)|"
            r"(?:版本|版次).{0,8}(?:哪些|多少|列表|清单|清單)|(?:有哪些|list).{0,8}versions?|"
            r"不同版本|各版本|版本之间|版本之間|历史版本|歷史版本|版本差异|版本差異|"
            r"\bv(?:er\.?)?\s*\d+(?:\.\d+)+.{0,45}(?:变动|變動|改动|改動|变化|變化|区别|區別|差异|差異|对比|對比|比较|比較)|"
            r"版本.{0,12}(?:改了什么|改了什麼|修改|变更|變更|变化|變化|差异|差異|对比|對比)|"
            r"revision|version.{0,12}(?:diff|change|history))",
            value, re.I,
        ):
            return FastRouteDecision(
                "SPEC_VERSION_FACTS",
                "verified specification body version comparison",
                "BODY_VERSION_FACTS",
                "SPEC",
            )
        # Relationship intent outranks words such as “具体内容”; otherwise a
        # source-target question is incorrectly expanded into the entire book.
        strong_relation=bool(re.search(
            r"(?:引用|参照|參照|参考|參考|reference|references|依赖|依賴|depend|dependency|"
            r"哪段|上游|下游|被谁引用|被誰引用|"
            r"涉及哪些式样|涉及哪些仕様|联动|聯動|调用|調用|对接|對接|接口关系|接口依据|设计依据|設計依據|来源依据|依据来自)",
            value,re.I))
        weak_relation=bool(re.search(r"(?:关联|關聯|关系|關係|对应|對應|有关|有關|联系|聯繫)",value,re.I))
        relation_has_two_specs=len(spec_codes)>=2 or bool(re.search(
            r"\d{3,6}.{0,35}(?:和|与|與|到|对|對).{0,35}\d{3,6}",value,re.I))
        if (spec_codes or identity_context) and (strong_relation or (weak_relation and relation_has_two_specs)):
            return FastRouteDecision(
                "SPEC_RELATIONS",
                "deterministic specification relation and content lookup",
                "RELATION_LOOKUP",
                "SPEC",
            )
        if spec_codes and re.search(
            r"(?:性能要求|性能要件|性能指标|性能指標|响应时间|響應時間|处理时间|處理時間|"
            r"时限|時限|耗时|耗時|performance requirements?|timing requirements?)",
            value, re.I,
        ):
            return FastRouteDecision(
                "SPEC_SUMMARY",
                "focused evidence-bounded specification requirement analysis",
                "SPEC_SUMMARY",
                "SPEC",
            )
        if spec_codes and re.search(
            r"(?:process|workflow|steps|手順|フロー|流れ|summari[sz]e|summary|analy[sz]e|要約|分析|説明|流程|步骤|步驟|如何|怎么|怎麼|总结|總結|概括|归纳|歸納|分析|说明|說明|介绍|介紹|说说|說說|按章(?:节|節)?展开|按章(?:节|節)?展開|主要解决什么|主要解決什麼|业务问题|業務問題)",
            value,
            re.I,
        ) and not re.search(r"(?:给出|显示|查看|展开|展開).{0,8}(?:原文|片段|逐字)", value, re.I):
            return FastRouteDecision(
                "SPEC_SUMMARY",
                "evidence-bounded specification analysis without raw-text dumping",
                "SPEC_SUMMARY",
                "SPEC",
            )
        if spec_codes and re.search(
            r"(?:第\s*[一二三四五六七八九十百零〇\d]+\s*(?:章|章节|章節)|"
            r"(?:chapter|chap(?:ter)?)\s*\d+|\d+(?:\.\d+)*\s*(?:章|章节|章節|节|節))"
            r".{0,20}(?:内容|內容|正文|原文|展开|展開|查看|显示|顯示|看看)?",
            value, re.I,
        ):
            return FastRouteDecision(
                "SPEC_CHAPTER_CONTENT",
                "explicit chapter/section content request from PostgreSQL",
                "SPEC_CHAPTER_CONTENT",
                "SPEC",
            )
        if spec_codes and re.search(
            r"(?:展开|展開|详细|詳細|具体|具體|说说|說說|介绍|介紹|概括|总结|總結|梳理)",
            value,
            re.I,
        ) and not re.search(r"(?:目录|目錄|table\s+of\s+contents|outline)", value, re.I):
            return FastRouteDecision(
                "SPEC_CHAPTER_CONTENT",
                "expand selected specification from PostgreSQL chapter contents",
                "SPEC_CHAPTER_CONTENT",
                "SPEC",
            )
        # "Which functional requirements does this versioned PDF contain?"
        # is NOT a request for a catalog of documents merely because the file
        # name contains "spec". Leave substantive engineering QA governed.
        if (re.search(r"\b(?:spec|specification)\s+v\d+(?:\.\d+)+", value, re.I)
                and re.search(r"(?:哪些|哪些功能|有哪些|要求|要件|功能|機能)", value, re.I)
                and re.search(r"(?:要求|要件|功能|機能|行为|行為|条件|例外)", value, re.I)
                and not re.search(r"(?:按目录|按目錄|按章节|按章節|目录总结|目錄總結)", value, re.I)):
            return FastRouteDecision("ENGINEERING_AGENT", "versioned PDF engineering requirement content", "ENGINEERING_QA", "SPEC")

        # Catalog/list intent outranks generic "can you find" existence
        # wording, otherwise "能查到哪些" incorrectly becomes a single lookup.
        if re.fullmatch(r"(?:你)?(?:现在|目前)?(?:能|可以)?查到(?:什么|什麼|哪些)(?:资料|文档|式样书|資料)?[?？。!！\s]*", value):
            return FastRouteDecision("SPEC_CATALOG", "query internal registered specification inventory, never general-model speculation", "SPECIFICATION_CATALOG", "SPEC")
        if (not (spec_codes and re.search(r"(?:章节|章節|内容|內容|要求|要件|technical topic)",value,re.I))) and (cls._CATALOG.search(value) or (
            re.search(r"(?:哪些|有什么|有什麼|列出|清单|一覧|list|多少|几本|幾本|能查到什么|能查到什麼)", value, re.I)
            and re.search(r"(?:式样书|式樣書|仕様書|规格书|規格書|spec(?:ification)?s?|知识库|資料庫)", value, re.I)
        )):
            return FastRouteDecision("SPEC_CATALOG", "authoritative PostgreSQL specification list", "SPECIFICATION_CATALOG", "SPEC")

        # Exact numeric document selection is an identity lookup, even when the
        # user phrases it as “find/open SPEC 508” rather than “does it exist?”.
        # Content/requirement/diff questions remain on the governed engineering
        # lane below; this shortcut only returns registered document metadata.
        if (re.search(r"(?:找(?:一下)?|查(?:一下|找|询)?|搜索|搜一下|打开(?:一下)?|看看)", value, re.I)
                and re.search(r"(?<!\d)(?:21MM|24MM)?\s*(?:的)?\s*\d{3,6}\s*(?:号|號)?\s*(?:式样书|式樣書|仕様書|规格书|規格書|spec(?:ification)?)", value, re.I)
                and not re.search(r"(?:内容|內容|功能|機能|要求|要件|章节|章節|页码|頁碼|分析|总结|總結|差异|差異|变更|變更|关联|關聯|关系|關係)", value, re.I)):
            return FastRouteDecision(
                "SPEC_LOOKUP",
                "explicit project and numeric specification identity lookup",
                "SPECIFICATION_LOOKUP",
                "SPEC",
            )

        # A stand-alone chapter-summary follow-up with no recoverable document
        # is still a structured request. Return a factual context-missing
        # explanation rather than calling Ollama on an ambiguous referent.
        if re.fullmatch(r"(?:请|請)?按(?:章节|章節|章)"
                        r"(?:总结|總結|概括|梳理)(?:一下)?(?:内容|內容)?[。.!！?？\s]*", value):
            return FastRouteDecision("SPEC_CHAPTER_CONTENT", "chapter content request without confirmed document", "SPEC_CHAPTER_CONTENT", "SPEC")

        # Local factual operations never invoke the 4B planner. A document
        # existence, table of contents or two-body-version inventory is a SQL
        # fact, not a generative engineering inference.
        if interpreted.specification_intent or cls._ENGINEERING_HINT.search(value):
            if (re.search(r"(?:相邻|相鄰|前后|前後|连续|連續|adjacent|consecutive)", value, re.I)
                    and re.search(r"(?:两个|兩個|2个|2個|版本|version)", value, re.I)
                    and re.search(r"(?:变更|變更|变化|變化|差异|差異|对比|diff|change)", value, re.I)):
                return FastRouteDecision("SPEC_VERSION_FACTS", "read adjacent verified body versions from PostgreSQL", "BODY_VERSION_FACTS", "SPEC")
            # Summarising chapter *contents* is different from listing the TOC.
            if (re.search(r"(?:按章节|按章節|逐章|逐章节|逐章節|章节内容|章節內容|正文内容|正文內容)", value, re.I)
                    and re.search(r"(?:总结|總結|概括|梳理|内容|內容|摘要)", value, re.I)
                    and not re.search(r"(?:按目录|按目錄|目录列表|目錄列表|目录标题|目錄標題)", value, re.I)):
                return FastRouteDecision("SPEC_CHAPTER_CONTENT", "read source PDF chapter content excerpts with page references", "SPEC_CHAPTER_CONTENT", "SPEC")
            if (re.search(r"(?:总结一下|總結一下|概括一下|总结|總結|概括)", value, re.I)
                    and re.search(r"\b(?:spec|specification)\s+v\d+(?:\.\d+)+", value, re.I)
                    and not re.search(r"(?:按目录|按目錄|目录标题|目錄標題)", value, re.I)):
                return FastRouteDecision("SPEC_CHAPTER_CONTENT", "follow-up summary of previously named PDF", "SPEC_CHAPTER_CONTENT", "SPEC")
            if re.search(r"(?:按目录|按目錄|按章节|按章節|目录总结|目錄總結|目录内容|目錄內容|table of contents|outline)", value, re.I):
                return FastRouteDecision("SPEC_OUTLINE", "read source document chapter/section directory", "SPECIFICATION_OUTLINE", "SPEC")
            if re.search(r"(?:有吗|有嗎|有没有|有沒有|是否存在|存在吗|存在嗎|登记|登記|录入|錄入|能查到|找得到|能找到|是否有|查得到|有没有这|have this|does.*exist)", value, re.I):
                return FastRouteDecision("SPEC_LOOKUP", "verify body filename and version in PostgreSQL", "SPECIFICATION_LOOKUP", "SPEC")

        # Prefix verbs in Chinese/Japanese are still an identity lookup when
        # the request is only to find/open a fully qualified versioned PDF.
        if (re.search(r"(?:^|\s)(?:查(?:一下|询|找)?|搜索|搜一下|找一下|看看|打开|打开一下)\s*", value)
                and re.search(r"\b(?:spec|specification)\s+v\d+(?:\.\d+)+\.pdf", value, re.I)
                and not re.search(r"(?:内容|內容|功能|機能|要求|要件|分析|总结|總結|目录|目錄|差异|差異|变更|變更)", value, re.I)):
            return FastRouteDecision("SPEC_LOOKUP", "prefixed exact PDF name lookup", "SPECIFICATION_LOOKUP", "SPEC")

        # A pasted filename or bare versioned specification title is a document
        # identity query, not a request for speculative engineering inference.
        # Constrain this to Latin title syntax with an explicit technical
        # specification marker and version or PDF extension.
        if (re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _().&\-]{3,180}", value)
                and re.search(r"\b(?:spec|specification)\b", value, re.I)
                and re.search(r"\bv\d+(?:\.\d+)+\b|\.pdf$", value, re.I)):
            return FastRouteDecision("SPEC_LOOKUP", "bare versioned specification title", "SPECIFICATION_LOOKUP", "SPEC")

        # Repository catalog/list/count questions must never be approximated from
        # Qdrant TopK. PostgreSQL specification_catalog is authoritative.
        if cls._CATALOG.search(value):
            return FastRouteDecision("SPEC_CATALOG", "specification catalog/list intent detected", "SPECIFICATION_CATALOG", "SPEC")

        # Exact identity questions can be satisfied from the catalog without an
        # expensive planner/vector round trip. Explanatory questions still go to
        # Agentic RAG below.
        identity_style = bool(
            re.search(r"(?:spec[-_: ]*)?\d{3,6}", value, flags=re.I)
            and re.search(
                r"(?:是什么(?:规格|式样|式樣|仕様)?|是什麼(?:規格|式樣|仕様)?|名称|名稱|叫什么|叫什麼|当前版本|目前版本|current\s+version|what\s+is)",
                value, flags=re.I,
            )
        )
        detail_request = bool(re.search(
            r"(?:功能|機能|接口|インターフェース|要求|要件|内容|內容|行为|行為|流程|フロー|处理|処理|设计|設計|条件|信号|signal|section|章节|章節|怎么|如何|为什么|為什麼|为何|分析|说明|說明)",
            value, flags=re.I,
        ))
        if identity_style and not detail_request:
            return FastRouteDecision("SPEC_LOOKUP", "exact specification identity intent detected", "SPECIFICATION_LOOKUP", "SPEC")

        if (interpreted.specification_intent or cls._ENGINEERING_HINT.search(value)
                or (len(EnterpriseRetriever._entity_spec_codes((), value)) >= 2
                    and re.search(r"(?:版本|历史|變更|变更|影响|關聯|关联|依赖|diff|relation|change)", value, re.I))
                or re.search(
                    r"(?:^|[\s,，:：])(\d{3,6})(?=\s*(?:的|是什么|是什麼|の|について|相关|关联|参照|参考|引用|式样|式樣|仕様|spec))",
                    value, flags=re.I,
                )):
            return FastRouteDecision("ENGINEERING_AGENT", "engineering/specification intent requires governed retrieval", "ENGINEERING_QA", "SPEC")

        for pattern in cls._DIRECT_PATTERNS:
            if re.fullmatch(pattern, value, flags=re.I):
                return FastRouteDecision("DIRECT_CHAT", "short conversational intent", "DIRECT_CHAT", "GENERAL")
        if len(value) <= 24 and re.search(r"(?:怎么样|怎么样了|在吗|可以吗|明白了|好的|ok|okay)", value, flags=re.I):
            return FastRouteDecision("DIRECT_CHAT", "short conversational intent", "DIRECT_CHAT", "GENERAL")

        # General/day-to-day questions are answered by Ollama without consulting
        # private engineering repositories.
        return FastRouteDecision("DIRECT_CHAT", "no engineering/issue repository intent detected", "GENERAL_CHAT", "GENERAL")


class QueryPlanner:
    """LLM-first query planner with a deterministic safety fallback.

    The planner never receives database credentials and never emits executable SQL.
    It can only select from a fixed read-only tool allow-list.
    """

    def __init__(self, ollama: OllamaClient, *, context_turns: int = 6) -> None:
        self.ollama = ollama
        self.context_turns = max(0, min(int(context_turns), 20))

    def plan(
        self,
        *,
        model: str,
        question: str,
        requested_scope: str,
        current_scope: str,
        conversation: list[dict[str, str]],
    ) -> AgentPlanResult:
        started = time.perf_counter()
        trace: list[AgentTraceStep] = []
        try:
            plan = self._plan_with_llm(
                model=model,
                question=question,
                requested_scope=requested_scope,
                current_scope=current_scope,
                conversation=conversation,
            )
            trace.append(AgentTraceStep("Planner", f"LLM plan accepted · tools={', '.join(plan.tools)}"))
        except Exception as exc:
            plan = self._fallback_plan(question, requested_scope, current_scope)
            trace.append(AgentTraceStep("Planner", f"Structured planner fallback · {type(exc).__name__}", status="DEGRADED"))
        elapsed = int((time.perf_counter() - started) * 1000)
        return AgentPlanResult(plan=plan, duration_ms=elapsed, trace=tuple(trace))

    def _plan_with_llm(
        self,
        *,
        model: str,
        question: str,
        requested_scope: str,
        current_scope: str,
        conversation: list[dict[str, str]],
    ) -> QueryPlan:
        history = []
        recent = conversation[-self.context_turns:] if self.context_turns else []
        for item in recent:
            role = str(item.get("role") or "").lower()
            if role in {"user", "assistant"}:
                history.append(f"{role}: {str(item.get('content') or '')[:700]}")
        prompt = f"""You are the retrieval planner for an enterprise engineering RAG system.
Return ONLY one JSON object. Never output SQL and never invent tool names.
Allowed tools: semantic_search, sql_exact_search, relation_search, version_search, unresolved_reference_search, specification_catalog, specification_lookup, issue_search, issue_statistics.

Rules:
- semantic_search: natural-language requirements, behavior, design explanations, fuzzy terminology.
- sql_exact_search: exact spec code/name/section/identifier lookup.
- relation_search: references, dependencies, upstream/downstream, related specifications and source-to-target REQUIREMENT-level evidence where available. An unreviewed candidate MUST NOT be called a confirmed dependency.
- version_search: revision/version/change/diff/history questions.
- unresolved_reference_search: unresolved/missing/unknown references.
- specification_catalog: authoritative list/count of registered specifications; never approximate catalog questions with vector TopK.
- specification_lookup: exact specification identity/current-version metadata.
- issue_search: Jira/QA/BUG issue rows; exact keys/status/priority/component/assignee are structured filters, free text may be semantically corroborated.
- issue_statistics: exact QA/BUG counts/distributions from PostgreSQL; never estimate counts from Qdrant.
- Use more than one tool when the question requires corroboration.
- scopes must use only 21MM, 24MM, COMMON, QABUG. QABUG is isolated QA/BUG data; do not mix it with specification scopes unless the user explicitly requests cross-domain analysis.
- If requested scope is explicit, respect it. AUTO may infer from the question/current workspace.
- rewritten_queries: 1 to 4 concise retrieval queries preserving technical identifiers.
- summary: one short operational explanation, not hidden reasoning.

Requested scope: {requested_scope}
Current workspace scope: {current_scope or '-'}
Recent conversation:
{chr(10).join(history) if history else '(none)'}

User question:
{question}

JSON schema:
{{
  "scopes": [],
  "intents": ["REQUIREMENT_LOOKUP"],
  "entities": [],
  "rewritten_queries": [],
  "tools": ["sql_exact_search", "semantic_search"],
  "summary": "Resolve the specification exactly, then retrieve supporting requirement text."
}}"""
        payload = self.ollama.chat_json(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            num_ctx=4096,
        )
        return self._validate_payload(payload, question, requested_scope, current_scope)

    def _validate_payload(self, raw: dict[str, Any], question: str, requested_scope: str, current_scope: str) -> QueryPlan:
        scopes = tuple(x for x in (str(v).upper().strip().replace("QA/BUG", "QABUG") for v in raw.get("scopes") or []) if x in {"21MM", "24MM", "COMMON", "QABUG"})
        if not scopes:
            scopes = EnterpriseRetriever.resolve_scopes(requested_scope, question, current_scope)
        tools = tuple(x for x in (str(v).strip() for v in raw.get("tools") or []) if x in ALLOWED_TOOLS)
        if not tools:
            tools = ("semantic_search", "sql_exact_search", "relation_search")
        tool_list = list(tools)
        q_lower = str(question or "").lower()
        if any(token in q_lower for token in ("引用", "依赖", "关系", "reference", "depend", "upstream", "downstream", "关联", "關聯")):
            if "relation_search" not in tool_list:
                tool_list.append("relation_search")
        if any(token in q_lower for token in ("版本", "revision", "rev", "diff", "变化", "变更", "历史", "修改了什么", "改了什么", "改动", "区别")):
            if "version_search" not in tool_list:
                tool_list.append("version_search")
        if parse_temporal_audit_ids(question):
            for mandatory in ('relation_search', 'version_search'):
                if mandatory not in tool_list:
                    tool_list.append(mandatory)
        if (interpret_internal_query(question).cross_domain
                or FastIntentRouter.classify(question).intent == "CROSS_DOMAIN_ENGINEERING"
                or (FastIntentRouter._ISSUE_HINT.search(question) and re.search(r"(?:关系|影响|关联|版本|变更|cross|relation|impact|diff)", question, re.I))):
            if "issue_search" not in tool_list and "issue_statistics" not in tool_list:
                tool_list.append("issue_search")
            # Dedicated issue corpus is accessed only on explicit cross-domain intent.
            if "QABUG" not in scopes:
                scopes = tuple((*scopes, "QABUG"))
        # Explicit user scope always wins against an LLM-suggested broadening.
        selected = str(requested_scope or "AUTO").strip().upper()
        if selected in {"21MM", "24MM", "COMMON", "QABUG", "QA/BUG"}:
            authoritative = selected.replace("QA/BUG", "QABUG")
            cross_requested = (interpret_internal_query(question).cross_domain
                               or FastIntentRouter.classify(question).intent == "CROSS_DOMAIN_ENGINEERING")
            scopes = (authoritative, "QABUG") if cross_requested and authoritative != "QABUG" else (authoritative,)
        # Question-explicit projects are authoritative even when UI scope is
        # AUTO. An LLM must not silently cross from 21MM to 24MM (or vice versa).
        if selected not in {"21MM", "24MM", "COMMON", "QABUG", "QA/BUG"}:
            q_upper = question.upper()
            question_scopes = (
                ("21MM", "24MM") if "21MM" in q_upper and "24MM" in q_upper else
                ("21MM",) if "21MM" in q_upper else
                ("24MM",) if "24MM" in q_upper else ()
            )
            if question_scopes:
                issue_cross = "issue_search" in tool_list and bool(
                    FastIntentRouter._ISSUE_HINT.search(question) or FastIntentRouter._ISSUE_KEY.search(question))
                scopes = (*question_scopes, "QABUG") if issue_cross else question_scopes
        interpreted = interpret_internal_query(question)
        if interpreted.specification_intent and re.search(r"式样|式樣|试样|試樣|仕様|specification|\bspec\b|文件名|文档名|书名", question, re.I):
            for mandatory in ("specification_lookup", "sql_exact_search", "semantic_search"):
                if mandatory not in tool_list:
                    tool_list.append(mandatory)
        tools = tuple(dict.fromkeys(tool_list))
        # Deterministic multilingual variants precede speculative model rewrites;
        # project and version scope always comes from the original question.
        queries = tuple(dict.fromkeys((
            *interpreted.queries,
            *expand_engineering_query(question, max_queries=3),
            *(str(v).strip()[:1200] for v in (raw.get("rewritten_queries") or []) if str(v).strip()),
        )))[:4]
        if not queries:
            queries = (question,)
        intents = tuple(str(v).strip().upper() for v in (raw.get("intents") or []) if str(v).strip())[:8]
        entities = tuple(str(v).strip() for v in (raw.get("entities") or []) if str(v).strip())[:12]
        summary = str(raw.get("summary") or "").strip()[:400]
        return QueryPlan(
            scopes=scopes,
            intents=intents or ("ENGINEERING_QA",),
            entities=entities,
            rewritten_queries=queries,
            tools=tools,
            planner_source="ollama-json",
            summary=summary or "LLM selected read-only retrieval tools and query expansions.",
        )

    @staticmethod
    def _fallback_plan(question: str, requested_scope: str, current_scope: str) -> QueryPlan:
        q = question.lower()
        tools: list[str] = []
        intents: list[str] = []
        if any(token in q for token in ("引用", "依赖", "关系", "reference", "depend", "upstream", "downstream", "关联")):
            tools.append("relation_search")
            intents.append("RELATION_LOOKUP")
        if any(token in q for token in ("版本", "rev", "revision", "diff", "变化", "变更", "历史", "修改了什么", "区别")):
            tools.append("version_search")
            intents.append("VERSION_ANALYSIS")
        if parse_temporal_audit_ids(question):
            for mandatory in ('relation_search', 'version_search'):
                if mandatory not in tools:
                    tools.append(mandatory)
            intents.append('TEMPORAL_RELATION_AUDIT')
        if any(token in q for token in ("未解析", "unresolved", "找不到", "未知引用", "缺失引用")):
            tools.append("unresolved_reference_search")
            intents.append("UNRESOLVED_REFERENCE")
        if re.search(r"(?:spec[-_: ]*)?\d{3,6}", q, flags=re.I) or any(token in q for token in ("section", "章节", "§")):
            tools.append("sql_exact_search")
            intents.append("EXACT_LOOKUP")
        # Natural language evidence is valuable in almost all engineering questions.
        if not tools or any(token in q for token in ("为什么", "如何", "怎么", "要求", "设计", "接口", "行为", "说明", "what", "how", "why")):
            tools.append("semantic_search")
            intents.append("SEMANTIC_QA")
        if "semantic_search" in tools and "sql_exact_search" not in tools:
            tools.append("sql_exact_search")
        if (FastIntentRouter._ISSUE_HINT.search(question) or FastIntentRouter._ISSUE_KEY.search(question)) and (
                interpret_internal_query(question).cross_domain
                or FastIntentRouter.classify(question).intent == "CROSS_DOMAIN_ENGINEERING"
                or re.search(r"(?:关联|关系|影响|变更|版本|符合|满足|滿足|cross|relation|impact|diff)", question, re.I)):
            tools.append("issue_search")
        interpreted = interpret_internal_query(question)
        if interpreted.specification_intent and re.search(r"式样|式樣|试样|試樣|仕様|specification|\bspec\b|文件名|文档名|书名", question, re.I):
            # Exact spec identifiers already use the authorized SQL exact route;
            # fuzzy title candidates are only needed when no code was supplied.
            if not EnterpriseRetriever._entity_spec_codes((), question):
                tools.append("specification_lookup")
            tools.extend(("sql_exact_search", "semantic_search"))
        dedup_tools = tuple(dict.fromkeys(tools))
        scopes = EnterpriseRetriever.resolve_scopes(requested_scope, question, current_scope)
        if "issue_search" in dedup_tools and "QABUG" not in scopes:
            scopes = (*scopes, "QABUG")
        entities = EnterpriseRetriever._entity_spec_codes((), question)
        return QueryPlan(
            scopes=scopes,
            intents=tuple(dict.fromkeys(intents)) or ("ENGINEERING_QA",),
            entities=entities,
            rewritten_queries=tuple(dict.fromkeys((*interpreted.queries, *expand_engineering_query(question, max_queries=3))))[:4],
            tools=dedup_tools,
            planner_source="deterministic-fallback",
            summary="Safe fallback selected read-only retrieval tools from explicit engineering terms.",
        )


class EvidenceGovernor:
    """Ranks evidence, assigns strength, and detects obvious source conflicts."""

    TYPE_WEIGHT = {
        "SPEC_CATALOG": 1.00,
        "SPEC_LOOKUP": 1.00,
        "ISSUE_STATS": 1.00,
        "ISSUE_EXACT": 0.99,
        "ISSUE_HYBRID": 0.96,
        "ISSUE_SQL_MATCH": 0.84,
        "RELATION_GRAPH": 1.03,
        "CONTENT_RELATION": 0.89,
        "STRUCTURAL_REFERENCE": 0.66,  # citation only, never reviewed dependency
        "STRUCTURAL_LAYOUT": 0.56,  # structural proximity, never dependency
        "REFERENCE_CHANGE_REVALIDATION": 0.61,  # recheck only, never invalidation
        "CHAPTER_VERSION_DIFF": 0.91,  # persisted body-to-body, hash-checked
        "TEMPORAL_RELATION_AUDIT": 0.73,  # mapping status only, NEVER relation validity
        "TEMPORAL_RELATION_UNVERIFIED": 0.28,  # insufficient evidence, diagnostic only
        "REQUIREMENT_VERSION_CHANGE": 0.92,
        "RELATION": 1.00,
        "VERSION_CHANGE_DETAIL": 1.02,
        "VERSION_HISTORY": 1.00,
        "VERSION_CHANGE": 0.98,
        "UNRESOLVED_REFERENCE": 0.95,
        "SQL_EXACT": 0.92,
        "HYBRID_CHUNK": 0.84,  # rank fusion is not an engineering confidence score
        "ISSUE_SEMANTIC": 0.86,
        "SEMANTIC": 0.82,
        "QDRANT": 0.84,
    }

    @classmethod
    def govern(cls, evidence: tuple[EvidenceItem, ...], limit: int, *, required_facets: tuple[str, ...] = ()) -> tuple[tuple[EvidenceItem, ...], str, tuple[str, ...]]:
        from app.ai.interaction_intelligence import evidence_usability

        def rank(item: EvidenceItem) -> float:
            base = cls.TYPE_WEIGHT.get(item.source_type, 0.70)
            semantic = max(0.0, min(1.0, float(item.score or 0.0)))
            if item.source_type in {"SEMANTIC", "QDRANT", "ISSUE_SEMANTIC", "ISSUE_SQL_MATCH"}:
                base = base * 0.55 + semantic * 0.45
            elif item.score is not None:
                base = base * 0.75 + semantic * 0.25
            status = str(item.metadata.get("status") or "").upper()
            if status == "CONFIRMED":
                base += 0.06
            if status in {"UNRESOLVED", "REJECTED"}:
                base -= 0.10
            usability, _ = evidence_usability(item.content)
            if usability == "REJECT" and item.source_type in {
                    "SEMANTIC", "QDRANT", "HYBRID_CHUNK", "ISSUE_SEMANTIC",
                    "TICKET_SPEC_SEMANTIC", "TICKET_ENTITY_MATCH"}:
                base -= 0.45
            elif usability == "LOW":
                base -= 0.12
            return base

        unique: dict[tuple[Any, ...], EvidenceItem] = {}
        for item in evidence:
            # A filename that explicitly names another engineering project is
            # not usable evidence for the current scope, even if legacy SQL or
            # Qdrant metadata linked it there. Keep only the dedicated conflict
            # diagnostic item so ordinary answers cannot cite the wrong body.
            filename_projects=tuple(dict.fromkeys(
                x.upper() for x in re.findall(
                    r"(?<![A-Z0-9])(21MM|24MM)(?![A-Z0-9])",str(item.file_name or ""),re.I
                )
            ))
            if (filename_projects and str(item.scope or "").upper() not in filename_projects
                    and item.source_type != "SPEC_SCOPE_CONFLICT"):
                continue
            issue_identity = str(
                item.metadata.get("issue_key")
                or item.metadata.get("logical_issue_id")
                or item.metadata.get("record_key")
                or ""
            ).strip().upper()
            if item.source_type == "STRUCTURAL_REFERENCE":
                anchor = item.metadata.get("anchor") or {}
                target = item.metadata.get("target") or {}
                source = item.metadata.get("source") or {}
                key = (item.scope, item.source_type, item.content_id,
                       source.get("text_hash"), anchor.get("start_offset"),
                       target.get("spec_code"), target.get("section"))
            elif item.source_type == "STRUCTURAL_LAYOUT":
                source = item.metadata.get("source") or {}
                target = item.metadata.get("target") or {}
                key = (item.scope, item.source_type, source.get("unit_id"),
                       target.get("unit_id"), item.metadata.get("relation_kind"))
            elif item.source_type == "REFERENCE_CHANGE_REVALIDATION":
                key = (item.scope, item.source_type, item.document_id,
                       item.metadata.get("reference_source_hash"),
                       item.metadata.get("change_set_id"), item.metadata.get("change_id"))
            elif item.source_type in {'TEMPORAL_RELATION_AUDIT', 'TEMPORAL_RELATION_UNVERIFIED'}:
                key = (item.scope, item.source_type, item.relation_id,
                       item.metadata.get('new_target_version_id'))
            elif item.source_type == "CHAPTER_VERSION_DIFF":
                key = (item.scope, item.source_type,
                       item.metadata.get("change_set_id"), item.metadata.get("change_id"))
            elif item.source_type.startswith("ISSUE_") and issue_identity:
                # Different Jira/QA/BUG records often have no specification fields
                # and SQL register evidence may have no content_id. Deduplicating
                # only by the old spec-oriented tuple collapsed an entire search
                # result set into one issue. Preserve one best evidence per logical
                # issue instead.
                key = (item.scope, item.source_type, "ISSUE", issue_identity)
            else:
                key = (item.scope, item.source_type, item.content_id, item.relation_id, item.specification_code, item.section_id)
            existing = unique.get(key)
            if existing is None or rank(item) > rank(existing):
                unique[key] = item
        # Preserve at least one available record from each required domain. A plain
        # global Top-K can silently drop BUG or history evidence in joint Q&A.
        semantic_types = {"SEMANTIC", "QDRANT", "HYBRID_CHUNK", "ISSUE_SEMANTIC",
                          "TICKET_SPEC_SEMANTIC", "TICKET_ENTITY_MATCH"}
        eligible = [item for item in unique.values()
                    if not (item.source_type in semantic_types
                            and evidence_usability(item.content)[0] == "REJECT")]
        ranked = sorted(eligible, key=rank, reverse=True)
        quota = max(1, int(limit))
        seeded: list[EvidenceItem] = []
        for facet in dict.fromkeys(required_facets):
            match = next((x for x in ranked if evidence_facet(x.source_type) == facet and x not in seeded), None)
            if match is not None and len(seeded) < quota:
                seeded.append(match)
        ordered = seeded + [x for x in ranked if x not in seeded][:quota - len(seeded)]
        normalized: list[EvidenceItem] = []
        for idx, item in enumerate(ordered, start=1):
            md = dict(item.metadata)
            usability, usability_reason = evidence_usability(item.content)
            md["governance_score"] = round(rank(item), 4)
            md["evidence_grade"] = "HIGH" if rank(item) >= 0.88 else "MEDIUM" if rank(item) >= 0.72 else "LOW"
            md["answer_usability"] = usability
            md["answer_usability_reason"] = usability_reason
            normalized.append(
                EvidenceItem(
                    evidence_id=str(idx), scope=item.scope, source_type=item.source_type, content=item.content,
                    content_id=item.content_id, score=item.score, document_id=item.document_id, file_name=item.file_name,
                    specification_code=item.specification_code, specification_name=item.specification_name,
                    specification_version=item.specification_version, chapter_title=item.chapter_title,
                    section_id=item.section_id, section_title=item.section_title, page_number=item.page_number,
                    relation_id=item.relation_id, metadata=md,
                )
            )

        high = sum(1 for item in normalized if item.metadata.get("evidence_grade") == "HIGH")
        medium = sum(1 for item in normalized if item.metadata.get("evidence_grade") == "MEDIUM")
        relation_confirmed = sum(
            1 for item in normalized
            if (item.source_type == "RELATION" and str(item.metadata.get("status") or "").upper() == "CONFIRMED")
            or (item.source_type == "RELATION_GRAPH" and int(item.metadata.get("confirmed_count") or 0) > 0)
        )
        if high >= 2 or relation_confirmed >= 1:
            strength = "STRONG"
        elif high >= 1 or medium >= 2:
            strength = "MODERATE"
        elif normalized:
            strength = "LIMITED"
        else:
            strength = "NONE"

        conflicts: list[str] = []
        # Detect conflicting version-change facts for the same spec/entity when both old/new texts disagree.
        version_items = [x for x in normalized if x.source_type == "VERSION_CHANGE"]
        grouped: dict[tuple[str, str], set[str]] = {}
        for item in version_items:
            key = (item.scope, item.specification_code or item.specification_name)
            text = re.sub(r"\s+", " ", item.content.strip())[:500]
            if text:
                grouped.setdefault(key, set()).add(text)
        for (scope, spec), values in grouped.items():
            if len(values) > 1:
                conflicts.append(f"Multiple version-change records exist for {scope}/{spec or 'specification'}; answer must identify revision context.")
        return tuple(normalized), strength, tuple(conflicts)


class EnterpriseAgent:
    """Coordinates the LLM planner and fixed read-only retrieval tools."""

    def __init__(self, ollama: OllamaClient, retriever: EnterpriseRetriever, *, evidence_limit: int = 8, planner_context_turns: int = 6) -> None:
        self.ollama = ollama
        self.retriever = retriever
        self.planner = QueryPlanner(ollama, context_turns=planner_context_turns)
        self.evidence_limit = evidence_limit

    def retrieve(
        self,
        *,
        model: str,
        question: str,
        scope: str,
        current_scope: str,
        conversation: list[dict[str, str]],
        cancel_check=None,
    ) -> RetrievalResult:
        request_id = "rag-" + uuid.uuid4().hex[:16]
        skill_flow = build_live_skill_workflow(question)
        if skill_flow.required_tools:
            # Explicit multi-domain Skills already define the mandatory lanes.
            # Do not spend a second LLM generation re-selecting fixed tools.
            native_plan = QueryPlanner._fallback_plan(question, scope, current_scope)
            native_plan = replace(native_plan, planner_source="native-skill-rules",
                summary="Explicit Skill workflow selected fixed read-only tools; model planning skipped.")
            plan_result = AgentPlanResult(plan=native_plan, duration_ms=0, trace=())
        else:
            plan_result = self.planner.plan(
                model=model,
                question=question,
                requested_scope=scope,
                current_scope=current_scope,
                conversation=conversation,
            )
        plan = plan_result.plan
        # Planner omissions cannot suppress a required SQL/issue/history lane.
        # The tool set stays fixed and read-only; project scopes are untouched.
        if skill_flow.required_tools:
            mandatory_tools = tuple(dict.fromkeys((*plan.tools, *skill_flow.required_tools)))
            if len(mandatory_tools) > 8:
                # Fixed read-only Skills take precedence over speculative LLM
                # choices; never drop mandatory lanes to fit an oversized plan.
                mandatory_tools = tuple(dict.fromkeys((
                    *skill_flow.required_tools,
                    *(t for t in plan.tools if t not in skill_flow.required_tools),
                )))[:8]
            plan = replace(plan, tools=mandatory_tools)
            if "issue_search" in skill_flow.required_tools and "QABUG" not in plan.scopes:
                # Add only the issue database for an explicit cross-domain
                # question. Never add an unrequested engineering project.
                plan = replace(plan, scopes=tuple((*plan.scopes, "QABUG"))[:4])
        try:
            graph = build_workflow(plan.tools, plan.scopes)
        except ValueError as exc:
            # Reject unsafe/oversized model plans and use the deterministic
            # read-only route, never arbitrary LLM-generated SQL/code.
            plan = self.planner._fallback_plan(question, scope, current_scope)
            graph = build_workflow(plan.tools, plan.scopes)
        trace: list[str] = [f"Planner [{plan.planner_source}] {plan.summary}"]
        trace.append("Workflow DAG [bounded read-only, sequential, controlled]: " + " → ".join(n.tool for n in graph.nodes))
        trace.append(f"Resolved scopes: {', '.join(plan.scopes)}")
        trace.append(f"Tools: {', '.join(plan.tools)}")
        if skill_flow.skills:
            trace.append("Skill workflow: " + ", ".join(skill_flow.skills))
        trace.append(f"Queries: {' | '.join(plan.rewritten_queries)}")

        retrieval = self.retriever.retrieve_agentic(
            question=question,
            scopes=plan.scopes,
            rewritten_queries=tuple(dict.fromkeys((question, *plan.rewritten_queries))),
            tools=plan.tools,
            entities=plan.entities,
            cancel_check=cancel_check,
        )
        governed, strength, conflicts = EvidenceGovernor.govern(retrieval.evidence, self.evidence_limit, required_facets=skill_flow.evidence_facets)
        trace.extend(retrieval.trace)
        missing = missing_evidence_facets(governed, skill_flow.evidence_facets)
        if missing:
            trace.append("Skill coverage incomplete: " + ", ".join(missing))
        trace.append(f"Evidence governance: {len(retrieval.evidence)} candidates → {len(governed)} selected · strength={strength}")

        # Adaptive recovery: weak first-pass evidence triggers a broader safe
        # read-only corroboration pass. This is still tool-gated; no arbitrary SQL.
        if (strength in {"NONE", "LIMITED"}
                and not parse_temporal_audit_ids(question)
                and not (cancel_check and cancel_check())):
            recovery_tools = tuple(dict.fromkeys((*plan.tools, "semantic_search", "sql_exact_search", "relation_search")))
            if recovery_tools != plan.tools:
                trace.append("Adaptive recovery: weak evidence triggered semantic + exact + relation corroboration")
                recovery = self.retriever.retrieve_agentic(
                    question=question, scopes=plan.scopes, rewritten_queries=tuple(dict.fromkeys((question, *plan.rewritten_queries))),
                    tools=recovery_tools, entities=plan.entities, cancel_check=cancel_check,
                )
                combined = tuple((*retrieval.evidence, *recovery.evidence))
                governed, strength, conflicts = EvidenceGovernor.govern(combined, self.evidence_limit, required_facets=skill_flow.evidence_facets)
                retrieval = RetrievalResult(
                    evidence=combined, resolved_scopes=plan.scopes,
                    semantic_ms=retrieval.semantic_ms + recovery.semantic_ms, sql_ms=retrieval.sql_ms + recovery.sql_ms,
                    relation_ms=retrieval.relation_ms + recovery.relation_ms, version_ms=retrieval.version_ms + recovery.version_ms,
                    warnings=tuple(dict.fromkeys((*retrieval.warnings, *recovery.warnings))),
                    trace=tuple((*retrieval.trace, *recovery.trace)),
                    candidate_suggestions=tuple({
                        (item.scope, item.metadata.get("candidate_id", item.evidence_id)): item
                        for item in (*retrieval.candidate_suggestions, *recovery.candidate_suggestions)
                    }.values()),
                )
                trace.extend(recovery.trace)
                trace.append(f"Recovery governance: {len(combined)} candidates → {len(governed)} selected · strength={strength}")
        if conflicts:
            trace.append("Conflict guard: " + " | ".join(conflicts))
        return RetrievalResult(
            evidence=governed,
            resolved_scopes=plan.scopes,
            semantic_ms=retrieval.semantic_ms,
            sql_ms=retrieval.sql_ms,
            relation_ms=retrieval.relation_ms,
            version_ms=retrieval.version_ms,
            planner_ms=plan_result.duration_ms,
            warnings=retrieval.warnings,
            plan_source=plan.planner_source,
            plan_summary=plan.summary,
            intents=plan.intents,
            entities=plan.entities,
            tools=plan.tools,
            rewritten_queries=plan.rewritten_queries,
            evidence_strength=strength,
            conflicts=conflicts,
            trace=tuple(trace),
            candidate_suggestions=retrieval.candidate_suggestions,
            request_id=request_id,
            route="ENGINEERING_AGENT",
            knowledge_source=(
                "POSTGRES+QDRANT+RELATIONS"
                if any(tool in plan.tools for tool in ("semantic_search", "relation_search", "version_search", "unresolved_reference_search"))
                else "POSTGRES_STRUCTURED"
            ),
        )
