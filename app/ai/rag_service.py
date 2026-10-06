from __future__ import annotations

import re
import time
import uuid
from dataclasses import asdict, dataclass, replace
from typing import Any, Callable, Iterable

from app.ai.agent import EvidenceGovernor, EnterpriseAgent, FastIntentRouter
from app.ai.config import AISettings
from app.ai.ollama_client import OllamaClient
from app.ai.retrieval import EnterpriseRetriever, EvidenceItem, RetrievalResult
from app.enterprise.profiles import PostgresProfile, QdrantProfile


@dataclass(frozen=True, slots=True)
class RAGRunInfo:
    request_id: str
    resolved_scopes: tuple[str, ...]
    evidence_count: int
    evidence_strength: str
    planner_ms: int
    semantic_ms: int
    sql_ms: int
    relation_ms: int
    version_ms: int
    llm_ms: int
    warnings: tuple[str, ...]


class EnterpriseRAGService:
    """Enterprise AUTO QA runtime.

    One user-facing mode, multiple governed internal paths:
    - General/day-to-day conversation -> Ollama only, no private retrieval.
    - Specification catalog/identity -> authoritative PostgreSQL structured path.
    - Jira/QA/BUG -> structured issue filters/counts plus issue-only Qdrant
      semantic corroboration when free-text matching is useful.
    - Other engineering questions -> LLM planner + fixed read-only retrieval
      tools (SQL exact, Qdrant semantic, relation, version, unresolved refs).

    PostgreSQL remains the source of truth; Qdrant is a derived semantic index.
    No model-generated SQL is ever executed.
    """

    def __init__(
        self,
        *,
        settings: AISettings,
        postgres_profiles: dict[str, PostgresProfile],
        postgres_password: str,
        qdrant_profile: QdrantProfile,
        qdrant_api_key: str = "",
    ) -> None:
        self.settings = settings
        self.ollama = OllamaClient(
            settings.ollama_url,
            connect_timeout=settings.ollama_connect_timeout,
            read_timeout=settings.ollama_read_timeout,
            first_load_timeout=settings.ollama_first_load_timeout,
            stream_idle_timeout=settings.ollama_stream_idle_timeout,
        )
        self.retriever = EnterpriseRetriever(
            postgres_profiles=postgres_profiles,
            postgres_password=postgres_password,
            qdrant=qdrant_profile,
            qdrant_api_key=qdrant_api_key,
            collections=settings.collections,
            embedding_model=settings.embedding_model,
            vector_dimension=settings.vector_dimension,
            top_k=max(settings.top_k, settings.evidence_limit),
            min_score=settings.min_score,
        )
        self.agent = EnterpriseAgent(
            self.ollama,
            self.retriever,
            evidence_limit=settings.evidence_limit,
            planner_context_turns=settings.planner_context_turns,
        )

    @staticmethod
    def _latest_user_message(messages: list[dict[str, str]]) -> str:
        for item in reversed(messages):
            if str(item.get("role") or "").lower() == "user":
                return str(item.get("content") or "").strip()
        return ""

    @staticmethod
    def classify_question(question: str) -> str:
        return FastIntentRouter.classify(question).route

    def warmup(self) -> None:
        """Warm the BGE query model; Ollama stays managed by keep_alive."""
        self.retriever.warmup_embedding()

    @staticmethod
    def _emit_stage(
        callback: Callable[[str, str, int], None] | None,
        stage: str,
        detail: str,
        duration_ms: int = 0,
    ) -> None:
        if callback:
            try:
                callback(stage, detail, int(duration_ms))
            except Exception:
                pass

    @staticmethod
    def _renumber_governed(result: RetrievalResult, limit: int) -> RetrievalResult:
        governed, strength, conflicts = EvidenceGovernor.govern(result.evidence, limit)
        trace = tuple((*result.trace, f"Evidence governance: {len(result.evidence)} candidates → {len(governed)} selected · strength={strength}"))
        return replace(result, evidence=governed, evidence_strength=strength, conflicts=conflicts, trace=trace)

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """Conservative tokenizer-free estimate for local multilingual models.

        CJK characters are close to one token each for the supported Qwen-family
        models. ASCII prose is approximated at four characters/token, with a
        small allowance for punctuation. The estimate intentionally errs high so
        Ollama never receives a prompt already known to exceed ``num_ctx``.
        """
        value = str(text or "")
        if not value:
            return 0
        cjk = len(re.findall(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]", value))
        ascii_chars = len(re.findall(r"[\x00-\x7f]", value))
        non_ascii_other = max(0, len(value) - cjk - ascii_chars)
        punctuation = len(re.findall(r"[{}\[\]():;,./\\|<>_=+\-*#]", value))
        return max(1, cjk + non_ascii_other + (ascii_chars + 3) // 4 + punctuation // 6)

    @classmethod
    def _truncate_to_tokens(cls, text: str, budget: int) -> str:
        value = str(text or "").strip()
        budget = max(0, int(budget))
        if not value or budget <= 0:
            return ""
        if cls._estimate_tokens(value) <= budget:
            return value
        # Binary search a character boundary because tokenizers are intentionally
        # not loaded on the GUI thread just to budget prompts.
        lo, hi = 1, len(value)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if cls._estimate_tokens(value[:mid]) <= max(1, budget - 8):
                lo = mid
            else:
                hi = mid - 1
        return value[:lo].rstrip() + " …[truncated]"

    def _runtime_context_for(self, retrieval: RetrievalResult) -> int:
        configured = max(2048, int(self.settings.num_ctx))
        if retrieval.route in {"DIRECT_CHAT", "SPEC_CATALOG", "SPEC_LOOKUP", "ISSUE_STATS", "ISSUE_QUERY"}:
            return min(configured, 4096)
        return min(configured, 8192)

    def _effective_evidence_limit(self, question: str, result: RetrievalResult) -> int:
        intents = set(result.intents or ())
        if result.route in {"SPEC_CATALOG", "SPEC_LOOKUP", "ISSUE_STATS"}:
            return 1 if result.route != "SPEC_CATALOG" else min(3, self.settings.evidence_limit)
        if result.route == "ISSUE_QUERY":
            if "ISSUE_EXACT_LOOKUP" in intents:
                return 1
            if "ISSUE_SEARCH_ONE" in intents:
                return min(self.settings.simple_issue_evidence_limit, self.settings.evidence_limit)
            return min(3, self.settings.evidence_limit)
        return self.settings.evidence_limit

    @staticmethod
    def _compact_issue_body(item: EvidenceItem, *, search_mode: bool) -> str:
        md = item.metadata or {}
        if not search_mode:
            return item.content.strip()
        # Search/list answers need identity + relevance, not the complete Jira
        # export row. Exact follow-ups can retrieve the full record by issue key.
        fields = (
            ("Issue Key", "issue_key"),
            ("Record Type", "record_type"),
            ("Summary", "summary"),
            ("Status", "status_raw"),
            ("Priority", "priority_raw"),
            ("Component", "component"),
            ("Assignee", "assignee"),
            ("Release Version", "release_plan_version"),
        )
        lines: list[str] = []
        for label, key in fields:
            value = str(md.get(key) or "").strip()
            if value:
                lines.append(f"{label}: {value}")
        if item.score is not None:
            lines.append(f"Semantic relevance: {float(item.score):.4f}")
        if not lines:
            return item.content.strip()[:1200]
        return "\n".join(lines)

    def _build_context_budgeted(
        self,
        evidence: tuple[EvidenceItem, ...],
        *,
        route: str,
        intents: tuple[str, ...],
        token_budget: int,
    ) -> str:
        if not evidence or token_budget <= 0:
            return ""
        search_mode = route == "ISSUE_QUERY" and any(x in {"ISSUE_SEARCH", "ISSUE_SEARCH_ONE"} for x in intents)
        parts: list[str] = []
        used = 0
        for item in evidence:
            grade = str(item.metadata.get("evidence_grade") or "")
            header = f"[{item.evidence_id}] {item.citation_label} | source={item.source_type}"
            if grade:
                header += f" | grade={grade}"
            if item.score is not None:
                header += f" | score={item.score:.4f}"
            body = self._compact_issue_body(item, search_mode=search_mode) if route == "ISSUE_QUERY" else item.content.strip()
            # Per-evidence caps prevent a single Jira comment stream or large spec
            # chunk from monopolizing the prompt.
            per_item = 420 if search_mode else 900 if route == "ISSUE_QUERY" else 1050
            body = self._truncate_to_tokens(body, per_item)
            block = header + "\n" + body
            block_tokens = self._estimate_tokens(block)
            remaining = token_budget - used
            if remaining <= 32:
                break
            if block_tokens > remaining:
                body_budget = max(0, remaining - self._estimate_tokens(header) - 10)
                body = self._truncate_to_tokens(body, body_budget)
                if not body:
                    break
                block = header + "\n" + body
                block_tokens = self._estimate_tokens(block)
            parts.append(block)
            used += block_tokens
        return "\n\n".join(parts)

    def _bounded_history(self, messages: list[dict[str, str]], *, token_budget: int) -> list[dict[str, str]]:
        """Keep the latest turn plus a bounded amount of recent conversational context."""
        max_messages = max(1, int(self.settings.answer_history_turns) * 2)
        candidates: list[dict[str, str]] = []
        for item in messages[-max_messages:]:
            role = str(item.get("role") or "").lower()
            if role in {"user", "assistant"}:
                candidates.append({"role": role, "content": str(item.get("content") or "")})
        if not candidates:
            return []
        # Always preserve the current/latest user message in full. Older turns are
        # admitted from newest to oldest only while the history budget allows.
        latest_user_index = max((i for i, x in enumerate(candidates) if x["role"] == "user"), default=len(candidates)-1)
        latest = candidates[latest_user_index]
        latest_tokens = self._estimate_tokens(latest["content"])
        remaining = max(0, int(token_budget) - latest_tokens)
        kept_rev: list[dict[str, str]] = [latest]
        for item in reversed(candidates[:latest_user_index]):
            if remaining <= 16:
                break
            content = item["content"].strip()
            if not content:
                continue
            cost = self._estimate_tokens(content)
            if cost > remaining:
                content = self._truncate_to_tokens(content, remaining)
                cost = self._estimate_tokens(content)
            if content:
                kept_rev.append({"role": item["role"], "content": content})
                remaining -= cost
        return list(reversed(kept_rev))

    def prepare(
        self,
        messages: list[dict[str, str]],
        *,
        model: str,
        scope: str = "AUTO",
        current_scope: str = "",
        retrieval_mode: str | None = None,  # retained for old API clients; ignored
        cancel_check: Callable[[], bool] | None = None,
        on_stage: Callable[[str, str, int], None] | None = None,
    ) -> tuple[list[dict[str, str]], RetrievalResult]:
        del retrieval_mode
        question = self._latest_user_message(messages)
        if not question:
            raise ValueError("A user question is required.")

        routing_question = FastIntentRouter.contextualize(question, messages)
        decision = FastIntentRouter.classify(routing_question)
        scope_norm = str(scope or "AUTO").strip().upper().replace("QA/BUG", "QABUG").replace("QA_BUG", "QABUG")
        # Explicit QA/BUG scope is an operator boundary. Except for obvious
        # greetings/thanks, route repository questions into the dedicated
        # issue domain even when the wording omits "BUG", "QA" or "Jira".
        if scope_norm == "QABUG" and decision.route not in {"ISSUE_STATS", "ISSUE_QUERY", "SYSTEM_META"}:
            conversational = bool(__import__("re").fullmatch(
                r"(?:你?好|您好|hello|hi|hey|谢谢|感謝|thanks|再见|拜拜|bye)[!！。,.，\s]*",
                question.strip(), flags=__import__("re").I,
            ))
            if not conversational:
                count_like = bool(__import__("re").search(r"(?:多少|几个|幾個|几条|幾條|统计|統計|分布|count|how many|件数|何件|集計)", question, __import__("re").I))
                from app.ai.agent import FastRouteDecision
                decision = FastRouteDecision(
                    "ISSUE_STATS" if count_like else "ISSUE_QUERY",
                    "explicit QA/BUG scope selected",
                    "ISSUE_STATISTICS" if count_like else "ISSUE_LOOKUP",
                    "ISSUE",
                )
        context_note = " · follow-up entity resolved" if routing_question != question else ""
        self._emit_stage(on_stage, "ROUTE", f"{decision.route} · {decision.reason}{context_note}")

        if decision.route == "DIRECT_CHAT":
            retrieval = RetrievalResult(
                evidence=(),
                resolved_scopes=(),
                plan_source="enterprise-auto-router",
                plan_summary="General/day-to-day request; private engineering retrieval skipped.",
                intents=(decision.intent or "GENERAL_CHAT",),
                tools=(),
                rewritten_queries=(question,),
                evidence_strength="NONE",
                trace=(
                    "Route: DIRECT_CHAT",
                    "Private repository retrieval: skipped",
                    "Qdrant queries: 0",
                    "PostgreSQL knowledge queries: 0",
                ),
                request_id="chat-" + uuid.uuid4().hex[:16],
                route="DIRECT_CHAT",
                knowledge_source="NONE",
            )
            self._emit_stage(on_stage, "RETRIEVAL", "日常/通用问题 · 未检索企业知识库")
            system = (
                "You are the local assistant inside Document Intelligence Console Enterprise. "
                "This request is not a repository query. Answer normally in the user's language. "
                "Do not claim that private specifications, QA/BUG registers, PostgreSQL, or Qdrant were searched. "
                "Do not expose chain-of-thought; provide only the useful final answer."
            )
        else:
            if decision.route == "SYSTEM_META":
                raw = self.retriever.retrieve_system_meta(
                    routing_question, scope=scope, current_scope=current_scope
                )
                self._emit_stage(on_stage, "SYSTEM", f"运行时/索引元数据检查完成 · {raw.sql_ms} ms")
            elif decision.route == "SPEC_CATALOG":
                raw = self.retriever.retrieve_spec_catalog(
                    routing_question, scope=scope, current_scope=current_scope
                )
                self._emit_stage(on_stage, "SQL", f"式样书目录查询完成 · {raw.sql_ms} ms")
            elif decision.route == "SPEC_LOOKUP":
                raw = self.retriever.retrieve_spec_lookup(
                    routing_question, scope=scope, current_scope=current_scope
                )
                self._emit_stage(on_stage, "SQL", f"式样书精确查询完成 · {raw.sql_ms} ms")
            elif decision.route == "ISSUE_STATS":
                raw = self.retriever.retrieve_issue_register(
                    routing_question,
                    scope=scope,
                    current_scope=current_scope,
                    statistics=True,
                    cancel_check=cancel_check,
                )
                self._emit_stage(on_stage, "SQL", f"QA/BUG 精确统计完成 · {raw.sql_ms} ms")
            elif decision.route == "ISSUE_QUERY":
                raw = self.retriever.retrieve_issue_register(
                    routing_question,
                    scope=scope,
                    current_scope=current_scope,
                    statistics=False,
                    cancel_check=cancel_check,
                )
                self._emit_stage(
                    on_stage,
                    "RETRIEVAL",
                    f"QA/BUG 检索完成 · SQL={raw.sql_ms} ms · semantic={raw.semantic_ms} ms",
                )
            else:
                self._emit_stage(on_stage, "PLANNER", "Ollama 正在规划只读检索工具…")
                raw = self.agent.retrieve(
                    model=model,
                    question=routing_question,
                    scope=scope,
                    current_scope=current_scope,
                    conversation=messages,
                    cancel_check=cancel_check,
                )
                self._emit_stage(
                    on_stage,
                    "RETRIEVAL",
                    f"Agent 检索完成 · planner={raw.planner_ms} ms · Qdrant={raw.semantic_ms} ms · SQL={raw.sql_ms} ms",
                )

            if routing_question != question:
                raw = replace(
                    raw,
                    trace=tuple((*raw.trace, f"Conversation follow-up resolved for retrieval: {routing_question.splitlines()[-1]}")),
                )
            effective_limit = self._effective_evidence_limit(question, raw)
            retrieval = self._renumber_governed(raw, effective_limit)
            if not retrieval.request_id:
                retrieval = replace(retrieval, request_id="rag-" + uuid.uuid4().hex[:16])
            self._emit_stage(
                on_stage,
                "EVIDENCE",
                f"最终证据 {len(retrieval.evidence)} 条 · 强度 {retrieval.evidence_strength} · {retrieval.knowledge_source}",
            )

            runtime_ctx = self._runtime_context_for(retrieval)
            prompt_budget = min(
                int(self.settings.prompt_budget_tokens),
                max(1200, runtime_ctx - int(self.settings.generation_reserve_tokens)),
            )
            history = self._bounded_history(
                messages,
                token_budget=min(int(self.settings.history_budget_tokens), max(200, prompt_budget // 4)),
            )
            history_tokens = sum(self._estimate_tokens(x.get("content", "")) for x in history)

            # Keep user-customizable grounding instructions, but cap them so an
            # old oversized system prompt cannot consume the full 4096 context.
            system = self._truncate_to_tokens(self.settings.system_prompt.strip(), 650)
            system += "\n\nENTERPRISE AUTO RETRIEVAL GOVERNANCE\n"
            system += f"Request ID: {retrieval.request_id}\n"
            system += f"Route: {retrieval.route}\n"
            system += f"Resolved scopes: {', '.join(retrieval.resolved_scopes) or '-'}\n"
            system += f"Evidence strength: {retrieval.evidence_strength}\n"
            system += f"Knowledge source: {retrieval.knowledge_source}\n"
            system += f"Tools: {', '.join(retrieval.tools) or '-'}\n"
            if retrieval.conflicts:
                system += "Conflict guard: " + " | ".join(retrieval.conflicts) + "\n"

            if decision.domain == "SYSTEM":
                system += (
                    "\nSYSTEM/RUNTIME ANSWER POLICY\n"
                    "- Treat live PostgreSQL/Qdrant metadata and the shipped static initialization contract as authoritative.\n"
                    "- Never invent tables, indexes, collection counts, vector dimensions, payload indexes, or schema versions.\n"
                    "- Distinguish configured contract from live runtime state. Never expose secrets.\n"
                )
            elif decision.domain == "ISSUE":
                system += (
                    "\nQA/BUG/JIRA ANSWER POLICY\n"
                    "- PostgreSQL structured fields are authoritative; Qdrant contributes relevance only.\n"
                    "- Never infer root cause, owner, resolution, version, severity, pass/fail or closure unless recorded.\n"
                    "- For counts use ISSUE_STATS only; vector TopK is never a count.\n"
                    "- For search/list answers preserve exact issue keys and recorded status.\n"
                    "- For ISSUE_SEARCH_ONE, return the best supported matching issue first and keep the answer short.\n"
                )
            else:
                system += (
                    "\nSPECIFICATION ANSWER POLICY\n"
                    "- PostgreSQL catalog/current-version metadata are authoritative.\n"
                    "- RELATION_GRAPH evidence is the AI-readable form of the Engineering Relations graph; use its exact incoming/outgoing edges and preserve CONFIRMED/UNRESOLVED/AMBIGUOUS status.\n"
                    "- VERSION_HISTORY and VERSION_CHANGE_DETAIL are the authoritative persisted version/change records; identify the exact from-version and to-version and enumerate recorded changes rather than guessing.\n"
                    "- Requirement/behavior claims must cite retrieved section evidence.\n"
                    "- Keep revisions separate and unresolved references unresolved.\n"
                )

            fixed_tokens = self._estimate_tokens(system)
            final_instruction = (
                "\n\nAnswer repository-specific claims only from the evidence above. "
                "Use inline citations [1], [2], etc. matching the evidence blocks exactly. "
                "If a requested fact is absent, say it is not established by current evidence."
            )
            instruction_tokens = self._estimate_tokens(final_instruction)
            available_for_evidence = max(
                0,
                prompt_budget - fixed_tokens - history_tokens - instruction_tokens - 80,
            )
            evidence_budget = min(int(self.settings.evidence_budget_tokens), available_for_evidence)
            context = self._build_context_budgeted(
                retrieval.evidence,
                route=retrieval.route,
                intents=retrieval.intents,
                token_budget=evidence_budget,
            )
            if context:
                system += "\nRETRIEVED ENTERPRISE EVIDENCE\n" + context + final_instruction
            else:
                system += (
                    "\nNo usable enterprise evidence was retrieved within the governed prompt budget. "
                    "State that the current indexed/structured data does not provide enough evidence. "
                    "Do not answer repository-specific facts from model memory."
                )

            trace = tuple((*retrieval.trace,
                           f"Prompt budget: ctx={runtime_ctx} prompt<={prompt_budget} history≈{history_tokens} evidence<={evidence_budget}"))
            retrieval = replace(retrieval, trace=trace)
            self._emit_stage(
                on_stage,
                "PROMPT",
                f"Prompt Budget · ctx={runtime_ctx} · prompt≤{prompt_budget} · evidence≤{evidence_budget} tokens",
            )

        # Direct chat is also bounded so a long conversation cannot overflow the
        # same local context window. Repository paths calculated their history
        # above because evidence needs the remaining budget.
        runtime_ctx = self._runtime_context_for(retrieval)
        prompt_budget = min(
            int(self.settings.prompt_budget_tokens),
            max(1200, runtime_ctx - int(self.settings.generation_reserve_tokens)),
        )
        if decision.route == "DIRECT_CHAT":
            history = self._bounded_history(
                messages,
                token_budget=min(int(self.settings.history_budget_tokens), max(300, prompt_budget - self._estimate_tokens(system) - 120)),
            )
        prepared = [{"role": "system", "content": system}, *history]

        # Final preflight: discard only oldest history first. The current user
        # question and governed system/evidence block are never silently dropped.
        def prompt_tokens(items: list[dict[str, str]]) -> int:
            return sum(self._estimate_tokens(x.get("content", "")) + 4 for x in items) + 8

        while len(prepared) > 2 and prompt_tokens(prepared) > prompt_budget:
            prepared.pop(1)
        if prompt_tokens(prepared) > prompt_budget and len(prepared) >= 2:
            latest = prepared[-1]
            latest_cost = self._estimate_tokens(latest.get("content", "")) + 16
            system_budget = max(400, prompt_budget - latest_cost)
            prepared[0]["content"] = self._truncate_to_tokens(prepared[0]["content"], system_budget)
        final_estimate = prompt_tokens(prepared)
        if final_estimate > prompt_budget:
            # This can only happen with a single extremely long user message.
            # Fail locally with an actionable error instead of letting Ollama
            # return HTTP 400 exceeded_context_size_error.
            raise ValueError(
                f"Prompt exceeds configured budget after governance: estimated={final_estimate}, "
                f"budget={prompt_budget}, context={runtime_ctx}. Shorten the current question or increase num_ctx."
            )
        return prepared, retrieval

    def answer_stream(
        self,
        messages: list[dict[str, str]],
        *,
        model: str,
        scope: str = "AUTO",
        current_scope: str = "",
        retrieval_mode: str | None = None,
        cancel_check: Callable[[], bool] | None = None,
        on_retrieval: Callable[[RetrievalResult], None] | None = None,
        on_stage: Callable[[str, str, int], None] | None = None,
    ) -> Iterable[str]:
        prepared, retrieval = self.prepare(
            messages,
            model=model,
            scope=scope,
            current_scope=current_scope,
            retrieval_mode=retrieval_mode,
            cancel_check=cancel_check,
            on_stage=on_stage,
        )
        if on_retrieval:
            on_retrieval(retrieval)
        self._emit_stage(
            on_stage,
            "OLLAMA",
            (
                f"正在调用 {model} · keep_alive={self.settings.ollama_keep_alive} · thinking=OFF · "
                f"connect={self.settings.ollama_connect_timeout:.0f}s · read={self.settings.ollama_read_timeout:.0f}s · "
                f"first-load={self.settings.ollama_first_load_timeout:.0f}s · stream-idle={self.settings.ollama_stream_idle_timeout:.0f}s"
            ),
        )

        # CPU-friendly route-aware context. Prompt preflight in ``prepare`` has
        # already guaranteed that the request fits inside this window.
        runtime_ctx = self._runtime_context_for(retrieval)

        yield from self.ollama.chat_stream(
            model=model,
            messages=prepared,
            temperature=self.settings.temperature,
            num_ctx=runtime_ctx,
            cancel_check=cancel_check,
            keep_alive=self.settings.ollama_keep_alive,
            think=False,
        )

    def answer(self, messages: list[dict[str, str]], **kwargs: Any) -> tuple[str, RetrievalResult, int]:
        captured: list[RetrievalResult] = []
        started = time.perf_counter()
        text = "".join(self.answer_stream(messages, on_retrieval=captured.append, **kwargs))
        llm_ms = int((time.perf_counter() - started) * 1000)
        retrieval = captured[0] if captured else RetrievalResult(evidence=(), resolved_scopes=())
        return text, retrieval, llm_ms

    def _build_context(self, evidence: tuple[EvidenceItem, ...]) -> str:
        if not evidence:
            return ""
        parts: list[str] = []
        total = 0
        for item in evidence:
            grade = str(item.metadata.get("evidence_grade") or "")
            header = f"[{item.evidence_id}] {item.citation_label} | source={item.source_type}"
            if grade:
                header += f" | grade={grade}"
            if item.score is not None:
                header += f" | score={item.score:.4f}"
            body = item.content.strip()
            block = header + "\n" + body
            if total + len(block) > self.settings.max_context_chars:
                remaining = self.settings.max_context_chars - total
                if remaining > 500:
                    parts.append(block[:remaining])
                break
            parts.append(block)
            total += len(block)
        return "\n\n".join(parts)

    @staticmethod
    def evidence_to_dict(item: EvidenceItem) -> dict[str, Any]:
        return asdict(item)
