from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_SYSTEM_PROMPT = """You are the enterprise knowledge assistant inside Document Intelligence Console.

Grounding policy:
- For repository-specific claims, use only the evidence supplied by the retrieval layer.
- Cite supported claims inline with [1], [2], etc. matching the supplied evidence blocks.
- Never invent specification names/codes/versions/sections, Jira/QA/BUG fields, relationships, counts, owners, status, priority, root cause, resolution, dates, or test results.
- If evidence is missing, stale, conflicting, ambiguous, or insufficient, say so explicitly instead of filling gaps from model memory.
- Treat PostgreSQL structured catalog/count/status results as authoritative for exact list/count/metadata questions; do not estimate them from vector TopK.
- For specifications, default to the current approved/current version unless the user explicitly asks for history/comparison. Keep different revisions separate and identify conflicts.
- QA/BUG is an isolated knowledge domain. For QA/BUG questions, use only QA/BUG evidence unless the user explicitly asks to cross-reference specifications.
- For QA/BUG/Jira records, distinguish current structured fields, historical comments, and analyst hypotheses. Do not infer root cause, fix, responsibility, severity, closure, version, test result, or final answer unless source evidence states it.
- Exact QA/BUG list/count/status/owner/version/date/filter questions are PostgreSQL-first. Qdrant TopK is never a count and never overrides authoritative SQL fields.
- When source fields conflict, show the conflict and provenance instead of silently reconciling it. When evidence is insufficient, return a bounded no-answer.
- For general/day-to-day questions that are not repository questions, answer normally and do not claim to have searched private engineering data.
- Preserve identifiers and technical terminology. Answer in the user's language unless another language is explicitly requested.
- Prefer a concise conclusion first, then evidence/caveats where useful. Do not expose hidden reasoning or chain-of-thought."""


@dataclass(slots=True)
class AISettings:
    settings_schema_version: int = 2
    ollama_url: str = "http://127.0.0.1:11434"
    chat_model: str = ""
    temperature: float = 0.2
    num_ctx: int = 4096
    top_k: int = 12
    max_context_chars: int = 12000
    min_score: float = 0.25
    retrieval_mode: str = "AI AUTO"  # legacy field retained for config compatibility
    evidence_limit: int = 4
    planner_context_turns: int = 4
    # v5.9.3 prompt budget governance. These defaults are tuned for a 4096-token
    # CPU-local Ollama context and are enforced again at runtime even when an old
    # settings file contains a larger legacy max_context_chars value.
    prompt_budget_tokens: int = 3200
    generation_reserve_tokens: int = 800
    history_budget_tokens: int = 650
    evidence_budget_tokens: int = 1500
    answer_history_turns: int = 3
    simple_issue_evidence_limit: int = 2
    audit_enabled: bool = True
    embedding_model: str = "BAAI/bge-m3"
    vector_dimension: int = 1024
    collections: dict[str, str] = field(
        default_factory=lambda: {
            "21MM": "rag_21mm_current_chunks",
            "24MM": "rag_24mm_current_chunks",
            "COMMON": "rag_common_current_chunks",
            "QABUG": "rag_qabug_current_chunks",
        }
    )
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    api_enabled: bool = False
    api_host: str = "127.0.0.1"
    api_port: int = 18080
    api_key: str = ""
    external_webui_url: str = "http://127.0.0.1:3000"
    ollama_keep_alive: str = "30m"
    ollama_connect_timeout: float = 10.0
    ollama_read_timeout: float = 600.0
    ollama_first_load_timeout: float = 900.0
    ollama_stream_idle_timeout: float = 180.0
    fast_intent_router: bool = True
    qdrant_only_knowledge: bool = False  # legacy compatibility; enterprise AUTO is multi-source

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> "AISettings":
        base = cls()
        values = asdict(base)
        for key in list(values):
            if key in raw and raw[key] is not None:
                values[key] = raw[key]
        collections = raw.get("collections")
        if isinstance(collections, dict):
            merged = dict(base.collections)
            for key, value in collections.items():
                if str(key).upper() in merged and str(value).strip():
                    merged[str(key).upper()] = str(value).strip()
            values["collections"] = merged
        try:
            values["settings_schema_version"] = max(1, int(values["settings_schema_version"]))
            values["temperature"] = min(2.0, max(0.0, float(values["temperature"])))
            values["num_ctx"] = max(2048, int(values["num_ctx"]))
            values["top_k"] = max(1, min(50, int(values["top_k"])))
            values["evidence_limit"] = max(1, min(20, int(values["evidence_limit"])))
            values["planner_context_turns"] = max(0, min(20, int(values["planner_context_turns"])))
            values["prompt_budget_tokens"] = max(1200, min(64000, int(values["prompt_budget_tokens"])))
            values["generation_reserve_tokens"] = max(256, min(16000, int(values["generation_reserve_tokens"])))
            values["history_budget_tokens"] = max(0, min(16000, int(values["history_budget_tokens"])))
            values["evidence_budget_tokens"] = max(256, min(32000, int(values["evidence_budget_tokens"])))
            values["answer_history_turns"] = max(0, min(12, int(values["answer_history_turns"])))
            values["simple_issue_evidence_limit"] = max(1, min(5, int(values["simple_issue_evidence_limit"])))
            values["max_context_chars"] = max(4000, min(100000, int(values["max_context_chars"])))
            values["min_score"] = min(1.0, max(-1.0, float(values["min_score"])))
            values["vector_dimension"] = max(1, int(values["vector_dimension"]))
            values["api_port"] = max(1, min(65535, int(values["api_port"])))
            values["ollama_connect_timeout"] = max(1.0, min(120.0, float(values["ollama_connect_timeout"])))
            values["ollama_read_timeout"] = max(30.0, min(3600.0, float(values["ollama_read_timeout"])))
            values["ollama_first_load_timeout"] = max(60.0, min(3600.0, float(values["ollama_first_load_timeout"])))
            values["ollama_stream_idle_timeout"] = max(15.0, min(1200.0, float(values["ollama_stream_idle_timeout"])))
        except (TypeError, ValueError):
            return base
        return cls(**values)


class AISettingsStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> AISettings:
        try:
            if not self.path.is_file():
                return AISettings()
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return AISettings()
            # v5.8 migration: old desktop defaults were tuned for large/GPU-backed
            # contexts and caused long CPU stalls.  Only migrate values that exactly
            # match the previous shipped defaults; explicit custom values are kept.
            schema_version = int(raw.get("settings_schema_version") or 1)
            if schema_version < 2:
                legacy_defaults = {
                    "num_ctx": (32768, 4096),
                    "top_k": (8, 12),
                    "evidence_limit": (8, 4),
                    "max_context_chars": (24000, 12000),
                }
                for key, (old, new) in legacy_defaults.items():
                    if raw.get(key) == old:
                        raw[key] = new
                raw["qdrant_only_knowledge"] = False
            # v5.9.3 fields are additive and therefore do not require a file
            # schema bump: missing keys automatically inherit the dataclass
            # defaults, while explicit user values remain untouched.
            raw.setdefault("prompt_budget_tokens", 3200)
            raw.setdefault("generation_reserve_tokens", 800)
            raw.setdefault("history_budget_tokens", 650)
            raw.setdefault("evidence_budget_tokens", 1500)
            raw.setdefault("answer_history_turns", 3)
            raw.setdefault("simple_issue_evidence_limit", 2)
            raw["settings_schema_version"] = 2
            return AISettings.from_mapping(raw)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return AISettings()

    def save(self, settings: AISettings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(asdict(settings), ensure_ascii=False, indent=2)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(payload, encoding="utf-8")
        temp.replace(self.path)
