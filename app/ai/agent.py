from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.ai.ollama_client import OllamaClient
from app.ai.internal_query import interpret_internal_query
from app.ai.retrieval import EnterpriseRetriever, EvidenceItem, RetrievalResult
from app.engineering_intelligence.workflow import build_workflow
from app.engineering_intelligence.ontology import expand_engineering_query


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
        r"(?:这个|這個|该问题|該問題|这个问题|這個問題|它|那条|那條|上一个|上一個|上面|刚才|剛才|"
        r"负责人|負責人|谁负责|誰負責|状态呢|狀態呢|版本呢|具体呢|具體呢|为什么|為什麼|依据呢|依據呢|证据呢|證據呢|哪里写|哪裡寫)",
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
        if not current or not cls._FOLLOWUP_HINT.search(current):
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
        for text in prior_user:
            if cls._ENGINEERING_HINT.search(text) or interpret_internal_query(text).specification_intent:
                codes = EnterpriseRetriever._entity_spec_codes((), text)
                if codes:
                    return f"{current}\nConversation specification: SPEC-{codes[0]}"
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

        # Questions about this console's own RAG/SQL/Qdrant runtime must be
        # answered from live introspection/static contracts, never model memory.
        if cls._SYSTEM_META_HINT.search(value):
            return FastRouteDecision("SYSTEM_META", "runtime/index/schema introspection intent detected", "SYSTEM_METADATA", "SYSTEM")

        # Explicit cross-domain engineering questions must not be swallowed
        # by the single-database issue shortcut. A normal issue-key lookup
        # still uses the faster exact QA/BUG path.
        interpreted = interpret_internal_query(value)
        cross_domain = bool(
            (interpreted.cross_domain and re.search(
                r"(?:相关|關聯|关联|关系|依赖|影响|有没有|有无|包括|哪些|查|搜索|找|对照|核对|cross|relation|impact|change|diff)", value, re.I,
            ))
            or (cls._ISSUE_HINT.search(value)
                and re.search(r"(?:21MM|24MM|式样书|仕様書|spec(?:ification)?|(?:^|\D)\d{3,6}(?!\d))", value, re.I)
                and re.search(r"(?:关联|关系|依赖|影响|版本|变更|对照|核对|cross|relation|impact|change|diff)", value, re.I))
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

        if interpreted.specification_intent or cls._ENGINEERING_HINT.search(value) or re.search(
            r"(?:^|[\s,，:：])(\d{3,6})(?=\s*(?:的|是什么|是什麼|の|について|相关|关联|参照|参考|引用|式样|式樣|仕様|spec))",
            value, flags=re.I,
        ):
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
  "scopes": ["24MM"],
  "intents": ["REQUIREMENT_LOOKUP"],
  "entities": ["SPEC-1001"],
  "rewritten_queries": ["SPEC-1001 User Profile communication requirements"],
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
        if interpret_internal_query(question).cross_domain or (FastIntentRouter._ISSUE_HINT.search(question) and re.search(r"(?:关系|影响|关联|版本|变更|cross|relation|impact|diff)", question, re.I)):
            if "issue_search" not in tool_list and "issue_statistics" not in tool_list:
                tool_list.append("issue_search")
            # Dedicated issue corpus is accessed only on explicit cross-domain intent.
            if "QABUG" not in scopes:
                scopes = tuple((*scopes, "QABUG"))
        # Explicit user scope always wins against an LLM-suggested broadening.
        selected = str(requested_scope or "AUTO").strip().upper()
        if selected in {"21MM", "24MM", "COMMON", "QABUG", "QA/BUG"}:
            authoritative = selected.replace("QA/BUG", "QABUG")
            cross_requested = interpret_internal_query(question).cross_domain
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
                issue_cross = "issue_search" in tool_list and bool(FastIntentRouter._ISSUE_HINT.search(question))
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
        if FastIntentRouter._ISSUE_HINT.search(question) and (interpret_internal_query(question).cross_domain or re.search(r"(?:关联|关系|影响|变更|版本|cross|relation|impact|diff)", question, re.I)):
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
        "REQUIREMENT_VERSION_CHANGE": 0.92,
        "RELATION": 1.00,
        "VERSION_CHANGE_DETAIL": 1.02,
        "VERSION_HISTORY": 1.00,
        "VERSION_CHANGE": 0.98,
        "UNRESOLVED_REFERENCE": 0.95,
        "SQL_EXACT": 0.92,
        "ISSUE_SEMANTIC": 0.86,
        "SEMANTIC": 0.82,
        "QDRANT": 0.84,
    }

    @classmethod
    def govern(cls, evidence: tuple[EvidenceItem, ...], limit: int) -> tuple[tuple[EvidenceItem, ...], str, tuple[str, ...]]:
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
            return base

        unique: dict[tuple[Any, ...], EvidenceItem] = {}
        for item in evidence:
            issue_identity = str(
                item.metadata.get("issue_key")
                or item.metadata.get("logical_issue_id")
                or item.metadata.get("record_key")
                or ""
            ).strip().upper()
            if item.source_type.startswith("ISSUE_") and issue_identity:
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
        ordered = sorted(unique.values(), key=rank, reverse=True)[: max(1, limit)]
        normalized: list[EvidenceItem] = []
        for idx, item in enumerate(ordered, start=1):
            md = dict(item.metadata)
            md["governance_score"] = round(rank(item), 4)
            md["evidence_grade"] = "HIGH" if rank(item) >= 0.88 else "MEDIUM" if rank(item) >= 0.72 else "LOW"
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
        plan_result = self.planner.plan(
            model=model,
            question=question,
            requested_scope=scope,
            current_scope=current_scope,
            conversation=conversation,
        )
        plan = plan_result.plan
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
        trace.append(f"Queries: {' | '.join(plan.rewritten_queries)}")

        retrieval = self.retriever.retrieve_agentic(
            question=question,
            scopes=plan.scopes,
            rewritten_queries=tuple(dict.fromkeys((question, *plan.rewritten_queries))),
            tools=plan.tools,
            entities=plan.entities,
            cancel_check=cancel_check,
        )
        governed, strength, conflicts = EvidenceGovernor.govern(retrieval.evidence, self.evidence_limit)
        trace.extend(retrieval.trace)
        trace.append(f"Evidence governance: {len(retrieval.evidence)} candidates → {len(governed)} selected · strength={strength}")

        # Adaptive recovery: weak first-pass evidence triggers a broader safe
        # read-only corroboration pass. This is still tool-gated; no arbitrary SQL.
        if strength in {"NONE", "LIMITED"} and not (cancel_check and cancel_check()):
            recovery_tools = tuple(dict.fromkeys((*plan.tools, "semantic_search", "sql_exact_search", "relation_search")))
            if recovery_tools != plan.tools:
                trace.append("Adaptive recovery: weak evidence triggered semantic + exact + relation corroboration")
                recovery = self.retriever.retrieve_agentic(
                    question=question, scopes=plan.scopes, rewritten_queries=tuple(dict.fromkeys((question, *plan.rewritten_queries))),
                    tools=recovery_tools, entities=plan.entities, cancel_check=cancel_check,
                )
                combined = tuple((*retrieval.evidence, *recovery.evidence))
                governed, strength, conflicts = EvidenceGovernor.govern(combined, self.evidence_limit)
                retrieval = RetrievalResult(
                    evidence=combined, resolved_scopes=plan.scopes,
                    semantic_ms=retrieval.semantic_ms + recovery.semantic_ms, sql_ms=retrieval.sql_ms + recovery.sql_ms,
                    relation_ms=retrieval.relation_ms + recovery.relation_ms, version_ms=retrieval.version_ms + recovery.version_ms,
                    warnings=tuple(dict.fromkeys((*retrieval.warnings, *recovery.warnings))),
                    trace=tuple((*retrieval.trace, *recovery.trace)),
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
            request_id=request_id,
            route="ENGINEERING_AGENT",
            knowledge_source=(
                "POSTGRES+QDRANT+RELATIONS"
                if any(tool in plan.tools for tool in ("semantic_search", "relation_search", "version_search", "unresolved_reference_search"))
                else "POSTGRES_STRUCTURED"
            ),
        )
