from __future__ import annotations

import os
import queue
import threading
import time
import webbrowser
from dataclasses import asdict, dataclass
from pathlib import Path
from tkinter import BOTH, END, LEFT, RIGHT, X, Y
import tkinter as tk
from tkinter import messagebox
from tkinter import ttk as tkttk
from typing import Any

import psycopg
import ttkbootstrap as ttk

from app.ai.api_server import OpenAICompatibleServer
from app.ai.audit import AIQueryAuditRepository
from app.ai.config import AISettings, AISettingsStore
from app.ai.health_policy import (
    SEVERITY_ERROR,
    SEVERITY_OK,
    SEVERITY_WARNING,
    classify_quality_issue,
    classify_runtime,
    is_valid_empty_scope,
    qabug_chunk_fanout,
)
from app.ai.local_webui import validate_local_webui_url, open_webui_default_browser, local_service_status
from app.ai.ollama_client import OllamaClient, OllamaError
from app.ai.rag_service import EnterpriseRAGService
from app.ai.retrieval import EvidenceItem, RetrievalResult
from app.ai.session_repository import ChatSessionRepository
from app.enterprise.profiles import PostgresProfile, QdrantProfile
from app.enterprise.qdrant_service import QdrantService
from app.rag.qdrant_governance import QdrantDataQualityValidator
from app.qabug.quality import QABugDataQualityValidator
from app.project.project_registry import ProjectRegistry, ProjectRegistryError
from app.i18n import zh


@dataclass(frozen=True, slots=True)
class AIRuntimeSnapshot:
    settings: AISettings
    postgres_profiles: dict[str, PostgresProfile]
    postgres_password: str
    qdrant_profile: QdrantProfile
    qdrant_api_key: str
    current_scope: str


class AIWorkspaceModule:
    """Large enterprise AI workspace embedded in the desktop console."""

    SCOPE_VALUES = (
        "AUTO",
        "21MM",
        "24MM",
        "COMMON",
        "QA/BUG",
        "21MM + COMMON",
        "24MM + COMMON",
        "ALL",
    )
    def __init__(self, host: Any, pages: dict[str, ttk.Frame], user_config_dir: Path) -> None:
        self.host = host
        self.root = host.root
        self.user_config_dir = Path(user_config_dir)
        self.settings_store = AISettingsStore(self.user_config_dir / "ai_config.json")
        self.settings = self.settings_store.load()
        self.sessions = ChatSessionRepository(self.user_config_dir / "ai_chat.sqlite3")
        self.audit = AIQueryAuditRepository(self.user_config_dir / "ai_audit.sqlite3")
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.chat_thread: threading.Thread | None = None
        self.status_thread: threading.Thread | None = None
        self.cancel_event = threading.Event()
        self.current_session_id: str | None = None
        self.current_assistant_text = ""
        self.current_evidence: list[EvidenceItem] = []
        self.evidence_by_iid: dict[str, EvidenceItem] = {}
        self.model_names: list[str] = []
        self.api_server: OpenAICompatibleServer | None = None
        self._api_snapshot: AIRuntimeSnapshot | None = None
        self._api_current_scope = ""
        self._rag_service: EnterpriseRAGService | None = None
        self._rag_service_key: tuple[Any, ...] | None = None
        self._rag_service_lock = threading.RLock()
        self._rag_warmup_thread: threading.Thread | None = None
        self._trace_request_started = 0.0
        self._first_token_ms: int | None = None

        self.chat_page = pages["ai_chat"]
        self.settings_page = pages["ai_settings"]
        self.status_page = pages["ai_status"]
        self.audit_page = pages["ai_audit"]

        self._create_variables()
        self._configure_styles()
        self._build_chat_page()
        self._build_settings_page()
        self._build_status_page()
        self._build_audit_page()
        self._refresh_session_list()
        self.root.after(120, self._poll_events)
        self.root.after(300, self.refresh_models)
        # v1.04: no BGE-M3 GUI startup warmup; the independent Backend owns it.
        # v1.04: do not start an API tied to the GUI lifetime.
        # The dedicated backend is launched with start_backend.ps1.

    # ------------------------------------------------------------------
    # Variables / style
    # ------------------------------------------------------------------

    def _create_variables(self) -> None:
        self.model_var = tk.StringVar(value=self.settings.chat_model)
        self.scope_var = tk.StringVar(value="AUTO")
        self.workspace_status_var = tk.StringVar(value=zh("AI workspace ready"))
        self.runtime_metrics_var = tk.StringVar(value=zh("Ready"))
        self.session_search_var = tk.StringVar(value="")
        self.session_title_var = tk.StringVar(value=zh("New conversation"))
        self.ollama_chip_var = tk.StringVar(value="Ollama · checking")
        self.rag_chip_var = tk.StringVar(value="RAG")
        self.evidence_summary_var = tk.StringVar(value=zh("No evidence loaded"))
        self.agent_trace_summary_var = tk.StringVar(value="AUTO ROUTER · idle")
        self.agent_engine_var = tk.StringVar(value="AI AUTO · Enterprise RAG")

        s = self.settings
        self.cfg_ollama_url = tk.StringVar(value=s.ollama_url)
        self.cfg_chat_model = tk.StringVar(value=s.chat_model)
        self.cfg_temperature = tk.StringVar(value=str(s.temperature))
        self.cfg_num_ctx = tk.StringVar(value=str(s.num_ctx))
        self.cfg_top_k = tk.StringVar(value=str(s.top_k))
        self.cfg_evidence_limit = tk.StringVar(value=str(s.evidence_limit))
        self.cfg_planner_turns = tk.StringVar(value=str(s.planner_context_turns))
        self.cfg_prompt_budget = tk.StringVar(value=str(s.prompt_budget_tokens))
        self.cfg_generation_reserve = tk.StringVar(value=str(s.generation_reserve_tokens))
        self.cfg_history_budget = tk.StringVar(value=str(s.history_budget_tokens))
        self.cfg_evidence_budget = tk.StringVar(value=str(s.evidence_budget_tokens))
        self.cfg_answer_history_turns = tk.StringVar(value=str(s.answer_history_turns))
        self.cfg_simple_issue_evidence_limit = tk.StringVar(value=str(s.simple_issue_evidence_limit))
        self.cfg_audit_enabled = tk.BooleanVar(value=s.audit_enabled)
        self.cfg_max_context = tk.StringVar(value=str(s.max_context_chars))
        self.cfg_min_score = tk.StringVar(value=str(s.min_score))
        self.cfg_embedding_model = tk.StringVar(value=s.embedding_model)
        self.cfg_vector_dimension = tk.StringVar(value=str(s.vector_dimension))
        self.cfg_collection_21 = tk.StringVar(value=s.collections.get("21MM", ""))
        self.cfg_collection_24 = tk.StringVar(value=s.collections.get("24MM", ""))
        self.cfg_collection_common = tk.StringVar(value=s.collections.get("COMMON", ""))
        self.cfg_collection_qabug = tk.StringVar(value=s.collections.get("QABUG", "rag_qabug_current_chunks"))
        self.cfg_api_enabled = tk.BooleanVar(value=s.api_enabled)
        self.cfg_api_host = tk.StringVar(value=s.api_host)
        self.cfg_api_port = tk.StringVar(value=str(s.api_port))
        self.cfg_api_key = tk.StringVar(value=s.api_key)
        self.cfg_external_webui_url = tk.StringVar(value=s.external_webui_url)
        self.external_webui_status_var = tk.StringVar(value="未检查")
        self.cfg_ollama_keep_alive = tk.StringVar(value=s.ollama_keep_alive)
        self.cfg_ollama_connect_timeout = tk.StringVar(value=str(s.ollama_connect_timeout))
        self.cfg_ollama_read_timeout = tk.StringVar(value=str(s.ollama_read_timeout))
        self.cfg_ollama_first_load_timeout = tk.StringVar(value=str(s.ollama_first_load_timeout))
        self.cfg_ollama_stream_idle_timeout = tk.StringVar(value=str(s.ollama_stream_idle_timeout))
        self.cfg_system_prompt = s.system_prompt
        self.config_status_var = tk.StringVar(value=zh("Settings loaded"))

        self.status_ollama_var = tk.StringVar(value=zh("Not checked"))
        self.status_postgres_var = tk.StringVar(value=zh("Not checked"))
        self.status_qdrant_var = tk.StringVar(value=zh("Not checked"))
        self.status_rag_var = tk.StringVar(value=zh("Not checked"))
        self.status_api_var = tk.StringVar(value="独立 Backend · 由管理工具启动时自动检查")
        self.status_detail_var = tk.StringVar(value=zh("Run health check to validate the complete AI stack."))

    def _configure_styles(self) -> None:
        style = ttk.Style()
        style.configure("AIHeading.TLabel", font=("Microsoft YaHei UI", 20, "bold"))
        style.configure("AISection.TLabel", font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("AIChat.Treeview", rowheight=32, font=("Microsoft YaHei UI", 9))
        style.configure("AIEvidence.Treeview", rowheight=30, font=("Microsoft YaHei UI", 9))
        style.configure("AIStatus.TLabel", font=("Microsoft YaHei UI", 10, "bold"))

    # ------------------------------------------------------------------
    # Chat UI
    # ------------------------------------------------------------------

    def _build_chat_page(self) -> None:
        page = self.chat_page
        page.configure(padding=0)
        page.rowconfigure(1, weight=1)
        page.columnconfigure(0, weight=1)

        top = ttk.Frame(page, padding=(18, 13, 18, 10))
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(0, weight=1)

        brand = ttk.Frame(top)
        brand.grid(row=0, column=0, sticky="w")
        ttk.Label(brand, text="AI工作台", style="AIHeading.TLabel").pack(anchor="w")
        chips = ttk.Frame(brand)
        chips.pack(anchor="w", pady=(6, 0))
        ttk.Label(chips, textvariable=self.ollama_chip_var, bootstyle="info", font=("Microsoft YaHei UI", 8, "bold"), padding=(7, 3)).pack(side=LEFT)
        ttk.Label(chips, textvariable=self.rag_chip_var, bootstyle="success", font=("Microsoft YaHei UI", 8, "bold"), padding=(7, 3)).pack(side=LEFT, padx=(6, 0))

        controls = ttk.Frame(top)
        controls.grid(row=0, column=1, sticky="e", padx=(20, 0))
        ttk.Label(controls, text="Model", font=("Microsoft YaHei UI", 8, "bold")).grid(row=0, column=0, sticky="w")
        self.model_combo = ttk.Combobox(controls, textvariable=self.model_var, width=24, state="readonly", bootstyle="primary")
        self.model_combo.grid(row=1, column=0, padx=(0, 10))
        ttk.Button(controls, text="↻", width=3, command=self.refresh_models, bootstyle="secondary-outline").grid(row=1, column=1, padx=(0, 14))

        ttk.Label(controls, text="Knowledge Scope", font=("Microsoft YaHei UI", 8, "bold")).grid(row=0, column=2, sticky="w")
        ttk.Combobox(controls, textvariable=self.scope_var, values=self.SCOPE_VALUES, width=18, state="readonly", bootstyle="primary").grid(row=1, column=2, padx=(0, 14))

        # Historical source-contract marker only; the visible Agent Engine badge
        # was intentionally removed in v5.9.1-CN: AI AUTO · Agentic RAG

        ttk.Separator(page, orient="horizontal").grid(row=0, column=0, sticky="sew")

        paned = tk.PanedWindow(page, orient=tk.HORIZONTAL, sashwidth=6, sashrelief=tk.FLAT, bd=0, bg="#dfe3e8")
        paned.grid(row=1, column=0, sticky="nsew")
        self.chat_paned = paned

        left = ttk.Frame(paned, padding=(12, 12, 10, 12), width=260)
        center = ttk.Frame(paned, padding=(16, 12, 16, 10), width=860)
        right = ttk.Frame(paned, padding=(10, 12, 12, 12), width=350)
        paned.add(left, minsize=220, width=270, stretch="never")
        paned.add(center, minsize=540, stretch="always")
        paned.add(right, minsize=290, width=360, stretch="never")

        # Session rail
        ttk.Button(left, text="＋  New conversation", command=self.new_session, bootstyle="primary", width=24).pack(fill=X, pady=(0, 10))
        search_row = ttk.Frame(left)
        search_row.pack(fill=X, pady=(0, 8))
        ttk.Entry(search_row, textvariable=self.session_search_var).pack(side=LEFT, fill=X, expand=True)
        ttk.Button(search_row, text="Search", command=self._refresh_session_list, bootstyle="secondary-outline", width=7).pack(side=RIGHT, padx=(6, 0))

        ttk.Label(left, text="RECENT CHATS", bootstyle="secondary", font=("Microsoft YaHei UI", 8, "bold")).pack(fill=X, pady=(2, 5))
        history_host = ttk.Frame(left)
        history_host.pack(fill=BOTH, expand=True)
        self.session_tree = ttk.Treeview(history_host, show="tree", selectmode="browse", style="AIChat.Treeview", bootstyle="primary")
        session_scroll = ttk.Scrollbar(history_host, orient="vertical", command=self.session_tree.yview)
        self.session_tree.configure(yscrollcommand=session_scroll.set)
        self.session_tree.column("#0", width=225, minwidth=160, stretch=True)
        self.session_tree.pack(side=LEFT, fill=BOTH, expand=True)
        session_scroll.pack(side=RIGHT, fill=Y)
        self.session_tree.bind("<<TreeviewSelect>>", self._on_session_selected)

        session_actions = ttk.Frame(left)
        session_actions.pack(fill=X, pady=(8, 0))
        ttk.Button(session_actions, text="Rename", command=self.rename_current_session, bootstyle="secondary-outline").pack(side=LEFT, fill=X, expand=True, padx=(0, 4))
        ttk.Button(session_actions, text="Delete", command=self.delete_current_session, bootstyle="danger-outline").pack(side=RIGHT, fill=X, expand=True, padx=(4, 0))

        # Conversation center
        center.rowconfigure(1, weight=1)
        center.columnconfigure(0, weight=1)
        title_row = ttk.Frame(center)
        title_row.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(title_row, textvariable=self.session_title_var, font=("Microsoft YaHei UI", 13, "bold")).pack(side=LEFT)
        ttk.Label(title_row, textvariable=self.workspace_status_var, bootstyle="secondary", font=("Microsoft YaHei UI", 9)).pack(side=RIGHT)

        conversation_host = ttk.Frame(center, bootstyle="light")
        conversation_host.grid(row=1, column=0, sticky="nsew")
        conversation_host.rowconfigure(0, weight=1)
        conversation_host.columnconfigure(0, weight=1)
        self.conversation = tk.Text(
            conversation_host,
            wrap="word",
            borderwidth=0,
            relief="flat",
            padx=24,
            pady=20,
            font=("Microsoft YaHei UI", 11),
            bg="#ffffff",
            fg="#1f2937",
            insertbackground="#1f2937",
            state="disabled",
        )
        conv_scroll = ttk.Scrollbar(conversation_host, orient="vertical", command=self.conversation.yview)
        self.conversation.configure(yscrollcommand=conv_scroll.set)
        self.conversation.grid(row=0, column=0, sticky="nsew")
        conv_scroll.grid(row=0, column=1, sticky="ns")
        self.conversation.tag_configure("user_label", font=("Microsoft YaHei UI", 9, "bold"), foreground="#2563eb", spacing1=14, spacing3=5)
        self.conversation.tag_configure("assistant_label", font=("Microsoft YaHei UI", 9, "bold"), foreground="#111827", spacing1=18, spacing3=5)
        self.conversation.tag_configure("user_body", font=("Microsoft YaHei UI", 11), lmargin1=8, lmargin2=8, rmargin=12, spacing3=12)
        self.conversation.tag_configure("assistant_body", font=("Microsoft YaHei UI", 11), lmargin1=8, lmargin2=8, rmargin=12, spacing3=12)
        self.conversation.tag_configure("system_note", font=("Microsoft YaHei UI", 9), foreground="#6b7280", justify="center", spacing1=10, spacing3=10)

        composer_card = ttk.Frame(center, padding=(0, 10, 0, 0))
        composer_card.grid(row=2, column=0, sticky="ew")
        composer_card.columnconfigure(0, weight=1)
        input_host = ttk.Frame(composer_card, padding=8, bootstyle="light")
        input_host.grid(row=0, column=0, sticky="ew")
        input_host.columnconfigure(0, weight=1)
        self.composer = tk.Text(
            input_host,
            height=4,
            wrap="word",
            font=("Microsoft YaHei UI", 11),
            relief="flat",
            borderwidth=0,
            padx=8,
            pady=7,
            bg="#ffffff",
        )
        self.composer.grid(row=0, column=0, sticky="ew")
        self.composer.bind("<Return>", self._send_shortcut)
        actions = ttk.Frame(input_host)
        actions.grid(row=0, column=1, sticky="ns", padx=(8, 0))
        self.send_button = ttk.Button(actions, text="发送", command=self.send_message, bootstyle="primary", width=16)
        self.send_button.pack(fill=X)
        self.stop_button = ttk.Button(actions, text="Stop", command=self.stop_generation, bootstyle="danger-outline", width=16, state="disabled")
        self.stop_button.pack(fill=X, pady=(6, 0))
        ttk.Label(composer_card, textvariable=self.runtime_metrics_var, bootstyle="secondary", font=("Microsoft YaHei UI", 8)).grid(row=1, column=0, sticky="w", pady=(5, 0))

        # Evidence inspector
        evidence_header = ttk.Frame(right)
        evidence_header.pack(fill=X, pady=(0, 8))
        ttk.Label(evidence_header, text="Evidence Inspector", style="AISection.TLabel").pack(side=LEFT)
        ttk.Label(evidence_header, textvariable=self.evidence_summary_var, bootstyle="secondary", font=("Microsoft YaHei UI", 8)).pack(side=RIGHT)

        evidence_host = ttk.Frame(right)
        evidence_host.pack(fill=BOTH, expand=True)
        evidence_host.rowconfigure(0, weight=1)
        evidence_host.columnconfigure(0, weight=1)
        self.evidence_tree = ttk.Treeview(
            evidence_host,
            columns=("scope", "type", "score"),
            show="tree headings",
            style="AIEvidence.Treeview",
            bootstyle="primary",
            height=9,
        )
        self.evidence_tree.heading("#0", text="Source")
        self.evidence_tree.heading("scope", text="Scope")
        self.evidence_tree.heading("type", text="Type")
        self.evidence_tree.heading("score", text="Score")
        self.evidence_tree.column("#0", width=155, minwidth=105)
        self.evidence_tree.column("scope", width=65, anchor="center")
        self.evidence_tree.column("type", width=80, anchor="center")
        self.evidence_tree.column("score", width=58, anchor="e")
        evidence_scroll = ttk.Scrollbar(evidence_host, orient="vertical", command=self.evidence_tree.yview)
        self.evidence_tree.configure(yscrollcommand=evidence_scroll.set)
        self.evidence_tree.grid(row=0, column=0, sticky="nsew")
        evidence_scroll.grid(row=0, column=1, sticky="ns")
        self.evidence_tree.bind("<<TreeviewSelect>>", self._on_evidence_selected)

        detail_box = ttk.LabelFrame(right, text="Source detail", padding=8, bootstyle="secondary")
        detail_box.pack(fill=BOTH, expand=False, pady=(10, 0))
        self.evidence_detail = tk.Text(
            detail_box,
            height=18,
            wrap="word",
            font=("Consolas", 9),
            bg="#f8fafc",
            fg="#334155",
            relief="flat",
            borderwidth=0,
            padx=8,
            pady=8,
            state="disabled",
        )
        self.evidence_detail.pack(fill=BOTH, expand=True)

        trace_box = ttk.LabelFrame(right, text="处理轨迹 / Agent Trace / Audit", padding=8, bootstyle="info")
        trace_box.pack(fill=BOTH, expand=False, pady=(10, 0))
        ttk.Label(trace_box, textvariable=self.agent_trace_summary_var, bootstyle="secondary", font=("Microsoft YaHei UI", 8, "bold"), wraplength=320).pack(fill=X, anchor="w", pady=(0, 5))
        self.agent_trace = tk.Text(
            trace_box, height=10, wrap="word", font=("Consolas", 8), bg="#f8fafc", fg="#334155",
            relief="flat", borderwidth=0, padx=7, pady=7, state="disabled"
        )
        self.agent_trace.pack(fill=BOTH, expand=True)

        self._show_welcome()

    # ------------------------------------------------------------------
    # Settings UI
    # ------------------------------------------------------------------

    def _build_settings_page(self) -> None:
        page = self.settings_page
        title = ttk.Frame(page)
        title.pack(fill=X, pady=(0, 14))
        ttk.Label(title, text="AI / RAG 企业配置", style="AIHeading.TLabel").pack(side=LEFT)
        ttk.Button(title, text="保存配置", command=self.save_configuration, bootstyle="primary").pack(side=RIGHT)

        ttk.Label(
            page,
            text="企业 AI AUTO：日常提问不访问私有知识库；式样书目录/精确元数据走 PostgreSQL；QA/BUG/Jira 使用结构化精确查询并按需叠加 issue-only 语义检索；复杂工程问题由 Ollama Planner 从白名单只读工具中自动选择 SQL、Qdrant、关系、版本与未解析引用检索。PostgreSQL 为权威源，Qdrant 为派生语义索引，最终证据经过治理后再交给模型。",
            bootstyle="secondary",
            wraplength=1100,
        ).pack(fill=X, pady=(0, 12))

        row = ttk.Frame(page)
        row.pack(fill=X, pady=(0, 12))
        row.columnconfigure(0, weight=1)
        row.columnconfigure(1, weight=1)

        ollama = ttk.LabelFrame(row, text="Ollama 运行参数", padding=14, bootstyle="primary")
        ollama.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        retrieval = ttk.LabelFrame(row, text="企业 RAG 检索治理", padding=14, bootstyle="secondary")
        retrieval.grid(row=0, column=1, sticky="nsew", padx=(6, 0))

        self._labeled_entry(ollama, "Ollama Base URL", self.cfg_ollama_url, 0)
        self._labeled_entry(ollama, "默认聊天模型", self.cfg_chat_model, 1)
        self._labeled_entry(ollama, "Temperature", self.cfg_temperature, 2)
        self._labeled_entry(ollama, "上下文窗口 (num_ctx)", self.cfg_num_ctx, 3)
        self._labeled_entry(ollama, "Keep Alive", self.cfg_ollama_keep_alive, 4)
        self._labeled_entry(ollama, "连接超时 / Connect Timeout (s)", self.cfg_ollama_connect_timeout, 5)
        self._labeled_entry(ollama, "普通读取超时 / Read Timeout (s)", self.cfg_ollama_read_timeout, 6)
        self._labeled_entry(ollama, "首次加载超时 / First Load Timeout (s)", self.cfg_ollama_first_load_timeout, 7)
        self._labeled_entry(ollama, "流式空闲超时 / Stream Idle Timeout (s)", self.cfg_ollama_stream_idle_timeout, 8)
        ttk.Button(ollama, text="测试连接 / 刷新 Ollama 模型", command=self.refresh_models, bootstyle="primary-outline").grid(row=9, column=0, columnspan=2, sticky="w", pady=(10, 0))

        self._labeled_entry(retrieval, "候选 Top K", self.cfg_top_k, 0)
        self._labeled_entry(retrieval, "最终治理证据数", self.cfg_evidence_limit, 1)
        self._labeled_entry(retrieval, "最低语义分数", self.cfg_min_score, 2)
        self._labeled_entry(retrieval, "最大证据上下文字符数", self.cfg_max_context, 3)
        self._labeled_entry(retrieval, "Prompt 总预算 (tokens)", self.cfg_prompt_budget, 4)
        self._labeled_entry(retrieval, "生成预留 (tokens)", self.cfg_generation_reserve, 5)
        self._labeled_entry(retrieval, "Evidence 预算 (tokens)", self.cfg_evidence_budget, 6)
        self._labeled_entry(retrieval, "会话历史预算 (tokens)", self.cfg_history_budget, 7)
        self._labeled_entry(retrieval, "回答历史轮数", self.cfg_answer_history_turns, 8)
        self._labeled_entry(retrieval, "单条 BUG 检索候选数", self.cfg_simple_issue_evidence_limit, 9)
        self._labeled_entry(retrieval, "Planner 会话历史轮数", self.cfg_planner_turns, 10, state="readonly")
        self._labeled_entry(retrieval, "Embedding 模型", self.cfg_embedding_model, 11, state="readonly")
        self._labeled_entry(retrieval, "向量维度", self.cfg_vector_dimension, 12, state="readonly")
        ttk.Checkbutton(retrieval, text="记录仅元数据 AI 查询审计", variable=self.cfg_audit_enabled, bootstyle="round-toggle").grid(row=13, column=0, columnspan=2, sticky="w", pady=(8, 0))

        collections = ttk.LabelFrame(page, text="Current-only Qdrant Collections / 当前向量库", padding=14, bootstyle="secondary")
        collections.pack(fill=X, pady=(0, 12))
        collections.columnconfigure(1, weight=1)
        self._labeled_entry(collections, "21MM", self.cfg_collection_21, 0)
        self._labeled_entry(collections, "24MM", self.cfg_collection_24, 1)
        self._labeled_entry(collections, "COMMON", self.cfg_collection_common, 2)
        self._labeled_entry(collections, "QA/BUG", self.cfg_collection_qabug, 3)

        api = ttk.LabelFrame(page, text="外部 UI / Open WebUI · 本机开发者控制", padding=14, bootstyle="secondary")
        api.pack(fill=X, pady=(0, 12))
        self._labeled_entry(api, "外部 WebUI 地址", self.cfg_external_webui_url, 0)
        self._labeled_entry(api, "独立后端端口", self.cfg_api_port, 1)
        ttk.Label(api, text="后台只监听 127.0.0.1；密钥存储在用户配置目录 local_api_token.txt / DIC_API_KEY，不显示在界面。", bootstyle="secondary").grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 6))
        actions = ttk.Frame(api)
        actions.grid(row=3, column=0, columnspan=2, sticky="w", pady=(5, 5))
        ttk.Button(actions, text="系统浏览器打开", command=self.open_external_webui, bootstyle="primary-outline").pack(side=LEFT, padx=(0, 6))
        ttk.Button(actions, text="复制网址", command=self.copy_external_webui, bootstyle="secondary-outline").pack(side=LEFT, padx=(0, 6))
        ttk.Button(actions, text="刷新本地服务状态", command=self.refresh_external_webui_status, bootstyle="info-outline").pack(side=LEFT)
        ttk.Label(api, textvariable=self.external_webui_status_var, wraplength=1050, justify="left", bootstyle="secondary").grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 3))
        ttk.Label(api, text="调用模型：当前 Ollama 首选模型；Embedding：BAAI/bge-m3；知识库：PostgreSQL + Qdrant，均不允许公网访问。", bootstyle="secondary").grid(row=5, column=0, columnspan=2, sticky="w", pady=(4, 0))

        prompt = ttk.LabelFrame(page, text="企业 AI 系统 Prompt", padding=12, bootstyle="secondary")
        prompt.pack(fill=BOTH, expand=True, pady=(0, 10))
        self.system_prompt_text = tk.Text(prompt, height=10, wrap="word", font=("Microsoft YaHei UI", 10), padx=8, pady=8)
        self.system_prompt_text.pack(fill=BOTH, expand=True)
        self.system_prompt_text.insert("1.0", self.cfg_system_prompt)
        ttk.Label(page, textvariable=self.config_status_var, bootstyle="secondary").pack(anchor="w")

    def open_external_webui(self) -> None:
        try:
            open_webui_default_browser(self.cfg_external_webui_url.get())
        except ValueError as exc:
            messagebox.showerror("Open WebUI", str(exc))

    def copy_external_webui(self) -> None:
        try:
            address = validate_local_webui_url(self.cfg_external_webui_url.get())
            self.root.clipboard_clear()
            self.root.clipboard_append(address)
            self.config_status_var.set("已复制 Open WebUI 地址")
        except ValueError as exc:
            messagebox.showerror("Open WebUI", str(exc))

    def refresh_external_webui_status(self) -> None:
        try:
            address = validate_local_webui_url(self.cfg_external_webui_url.get())
            port = int(self.cfg_api_port.get())
        except (ValueError, TypeError) as exc:
            self.external_webui_status_var.set(f"配置错误：{exc}")
            return
        self.external_webui_status_var.set("检查中（后台异步，不阻塞界面）…")
        ollama = self.cfg_ollama_url.get()
        qdrant = self.host.qdrant_url_var.get()
        model = self.cfg_chat_model.get().strip() or self.model_var.get().strip() or "未配置"
        embedding = self.cfg_embedding_model.get()

        def check() -> None:
            try:
                statuses = local_service_status(webui_url=address, api_port=port, ollama_url=ollama, qdrant_url=qdrant)
                text = " · ".join(f"{name}: {status}" for name, status in statuses.items())
                result = f"{text} | 首选 LLM: {model} · Embedding: {embedding}"
            except Exception as exc:
                result = f"检测异常：{type(exc).__name__}"
            self.events.put(("external_webui_status", result))

        threading.Thread(target=check, name="local-webui-health", daemon=True).start()

    @staticmethod
    def _labeled_entry(parent: ttk.Frame, label: str, variable: tk.StringVar, row: int, *, state: str = "normal") -> ttk.Entry:
        ttk.Label(parent, text=label, font=("Microsoft YaHei UI", 9, "bold")).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=5)
        entry = ttk.Entry(parent, textvariable=variable, width=48, state=state)
        entry.grid(row=row, column=1, sticky="ew", pady=5)
        try:
            parent.columnconfigure(1, weight=1)
        except tk.TclError:
            pass
        return entry

    # ------------------------------------------------------------------
    # Audit UI
    # ------------------------------------------------------------------

    def _build_audit_page(self) -> None:
        page = self.audit_page
        title = ttk.Frame(page)
        title.pack(fill=X, pady=(0, 14))
        ttk.Label(title, text="AI Query Audit", style="AIHeading.TLabel").pack(side=LEFT)
        ttk.Button(title, text="Refresh", command=self.refresh_audit_view, bootstyle="primary-outline").pack(side=RIGHT)
        ttk.Label(
            page,
            text="Metadata-only operational audit. Prompts and answers are not duplicated here; the audit records planner source, tools, scopes, evidence identifiers, timings and execution status.",
            bootstyle="secondary", wraplength=1180,
        ).pack(fill=X, pady=(0, 12))

        host = ttk.Frame(page)
        host.pack(fill=BOTH, expand=True)
        host.columnconfigure(0, weight=3)
        host.columnconfigure(1, weight=2)
        host.rowconfigure(0, weight=1)
        self.audit_tree = ttk.Treeview(
            host, columns=("time","model","scope","strength","status","total"), show="tree headings", bootstyle="primary"
        )
        self.audit_tree.heading("#0", text="Request")
        for key, label in (("time","UTC Time"),("model","Model"),("scope","Scope"),("strength","Evidence"),("status","Status"),("total","Total ms")):
            self.audit_tree.heading(key, text=label)
        self.audit_tree.column("#0", width=190)
        self.audit_tree.column("time", width=150)
        self.audit_tree.column("model", width=150)
        self.audit_tree.column("scope", width=80, anchor="center")
        self.audit_tree.column("strength", width=90, anchor="center")
        self.audit_tree.column("status", width=80, anchor="center")
        self.audit_tree.column("total", width=80, anchor="e")
        self.audit_tree.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self.audit_tree.bind("<<TreeviewSelect>>", self._on_audit_selected)
        self.audit_rows: dict[str, dict[str, Any]] = {}

        detail = ttk.LabelFrame(host, text="Execution detail", padding=10, bootstyle="secondary")
        detail.grid(row=0, column=1, sticky="nsew")
        self.audit_detail = tk.Text(detail, wrap="word", font=("Consolas", 9), bg="#f8fafc", fg="#334155", relief="flat", padx=8, pady=8, state="disabled")
        self.audit_detail.pack(fill=BOTH, expand=True)
        self.refresh_audit_view()

    def refresh_audit_view(self) -> None:
        if not hasattr(self, "audit_tree"):
            return
        self.audit_tree.delete(*self.audit_tree.get_children())
        self.audit_rows.clear()
        for row in self.audit.recent(250):
            iid = f"audit::{row.get('id')}"
            self.audit_rows[iid] = row
            self.audit_tree.insert(
                "", END, iid=iid, text=str(row.get("request_id") or "-")[:24],
                values=(str(row.get("created_at") or "")[:19], str(row.get("model") or "")[:20], str(row.get("scope") or "AUTO"),
                        str(row.get("evidence_strength") or "NONE"), str(row.get("status") or ""), int(row.get("total_ms") or 0)),
            )

    def _on_audit_selected(self, _event=None) -> None:
        selection = self.audit_tree.selection() if hasattr(self, "audit_tree") else ()
        if not selection:
            return
        row = self.audit_rows.get(selection[0])
        if not row:
            return
        import json as _json
        def parsed(name: str):
            try:
                return _json.loads(row.get(name) or "[]")
            except Exception:
                return []
        lines = [
            f"请求 ID：{row.get('request_id') or '-'}", f"会话 ID：{row.get('session_id') or '-'}",
            f"模型：{row.get('model') or '-'}", f"请求范围：{row.get('scope') or '-'}",
            f"解析范围：{parsed('resolved_scopes_json')}", f"规划器：{row.get('plan_source') or '-'}",
            f"工具：{parsed('tools_json')}", f"意图：{parsed('intents_json')}", f"实体：{parsed('entities_json')}",
            f"证据强度：{row.get('evidence_strength') or 'NONE'}", f"证据 ID：{parsed('evidence_ids_json')}",
            "", "耗时", "-" * 48,
            f"Planner: {row.get('planner_ms') or 0} ms", f"Semantic: {row.get('semantic_ms') or 0} ms",
            f"SQL: {row.get('sql_ms') or 0} ms", f"Relations: {row.get('relation_ms') or 0} ms",
            f"Version: {row.get('version_ms') or 0} ms", f"Total: {row.get('total_ms') or 0} ms",
            "", "轨迹", "-" * 48, *[str(x) for x in parsed('trace_json')],
        ]
        self._set_text_state(self.audit_detail, True)
        self.audit_detail.delete("1.0", END)
        self.audit_detail.insert("1.0", "\n".join(lines))
        self._set_text_state(self.audit_detail, False)

    # ------------------------------------------------------------------
    # Status UI
    # ------------------------------------------------------------------

    def _build_status_page(self) -> None:
        page = self.status_page
        title = ttk.Frame(page)
        title.pack(fill=X, pady=(0, 14))
        ttk.Label(title, text="AI Service Status", style="AIHeading.TLabel").pack(side=LEFT)
        ttk.Button(title, text="Run full health check", command=self.run_health_check, bootstyle="primary").pack(side=RIGHT)

        cards = ttk.Frame(page)
        cards.pack(fill=X, pady=(0, 14))
        for i in range(4):
            cards.columnconfigure(i, weight=1, uniform="ai-status")
        for col, (name, variable) in enumerate(
            (
                ("Ollama", self.status_ollama_var),
                ("PostgreSQL", self.status_postgres_var),
                ("Qdrant", self.status_qdrant_var),
                ("RAG Runtime", self.status_rag_var),
            )
        ):
            card = ttk.LabelFrame(cards, text=name, padding=14, bootstyle="secondary")
            card.grid(row=0, column=col, sticky="nsew", padx=(0 if col == 0 else 5, 0 if col == 3 else 5))
            ttk.Label(card, textvariable=variable, style="AIStatus.TLabel", wraplength=250).pack(anchor="w")

        api = ttk.LabelFrame(page, text="OpenAI-compatible API / Open WebUI", padding=14, bootstyle="secondary")
        api.pack(fill=X, pady=(0, 14))
        api.columnconfigure(0, weight=1)
        ttk.Label(api, textvariable=self.status_api_var, font=("Microsoft YaHei UI", 10, "bold")).grid(row=0, column=0, sticky="w")
        controls = ttk.Frame(api)
        controls.grid(row=0, column=1, sticky="e")
        ttk.Button(controls, text="检查独立 API", command=self.refresh_external_webui_status, bootstyle="success-outline").pack(side=LEFT, padx=(0, 6))
        ttk.Button(controls, text="启动方法", command=self.start_api_server, bootstyle="secondary-outline").pack(side=LEFT)
        ttk.Label(api, text="独立进程：start_backend.ps1 · GET /api/v1/health · GET /v1/models · POST /v1/chat/completions", bootstyle="secondary").grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))

        detail = ttk.LabelFrame(page, text="Diagnostics", padding=14, bootstyle="secondary")
        detail.pack(fill=BOTH, expand=True)
        ttk.Label(detail, textvariable=self.status_detail_var, justify="left", wraplength=1180, font=("Consolas", 9)).pack(fill=BOTH, expand=True, anchor="nw")

    # ------------------------------------------------------------------
    # Runtime snapshots
    # ------------------------------------------------------------------

    def _current_scope(self) -> str:
        try:
            return ProjectRegistry.resolve(self.host.project_var.get()).code
        except ProjectRegistryError:
            return ""

    def _snapshot(self) -> AIRuntimeSnapshot:
        settings = self.settings
        host_value = self.host.postgres_host_var.get().strip() or "127.0.0.1"
        try:
            port = int(self.host.postgres_port_var.get().strip() or "5432")
        except ValueError:
            port = 5432
        user = self.host.postgres_user_var.get().strip() or "postgres"
        try:
            timeout = int(self.host.postgres_timeout_var.get().strip() or "10")
        except ValueError:
            timeout = 10
        db_names = dict(getattr(self.host, "_project_database_names", {}) or {})
        for definition in ProjectRegistry.PROJECTS.values():
            db_names.setdefault(definition.code, definition.database_default)
        profiles = {
            scope: PostgresProfile(
                host=host_value,
                port=port,
                database=db_names.get(scope) or ProjectRegistry.PROJECTS[scope].database_default,
                user=user,
                connect_timeout=timeout,
                sslmode="prefer",
            )
            for scope in ("21MM", "24MM", "COMMON", "QABUG")
        }
        try:
            qtimeout = float(self.host.qdrant_timeout_var.get().strip() or "10")
        except ValueError:
            qtimeout = 10.0
        return AIRuntimeSnapshot(
            settings=settings,
            postgres_profiles=profiles,
            postgres_password=self.host.postgres_password_var.get() or os.getenv("POSTGRES_PASSWORD", ""),
            qdrant_profile=QdrantProfile(
                url=self.host.qdrant_url_var.get().strip() or "http://127.0.0.1:6333",
                timeout=qtimeout,
            ),
            qdrant_api_key=self.host.qdrant_api_key_var.get(),
            current_scope=self._current_scope(),
        )

    @staticmethod
    def _runtime_service_key(snapshot: AIRuntimeSnapshot) -> tuple[Any, ...]:
        settings = snapshot.settings
        profile_key = tuple(
            sorted(
                (scope, profile.host, profile.port, profile.database, profile.user, profile.connect_timeout, profile.sslmode)
                for scope, profile in snapshot.postgres_profiles.items()
            )
        )
        return (
            settings.ollama_url,
            settings.temperature,
            settings.num_ctx,
            settings.planner_context_turns,
            settings.prompt_budget_tokens,
            settings.generation_reserve_tokens,
            settings.history_budget_tokens,
            settings.evidence_budget_tokens,
            settings.answer_history_turns,
            settings.simple_issue_evidence_limit,
            settings.embedding_model,
            settings.vector_dimension,
            settings.top_k,
            settings.evidence_limit,
            settings.min_score,
            settings.max_context_chars,
            settings.ollama_keep_alive,
            settings.ollama_connect_timeout,
            settings.ollama_read_timeout,
            settings.ollama_first_load_timeout,
            settings.ollama_stream_idle_timeout,
            tuple(sorted(settings.collections.items())),
            snapshot.qdrant_profile.url,
            snapshot.qdrant_profile.timeout,
            hash(snapshot.qdrant_api_key),
            hash(snapshot.postgres_password),
            profile_key,
        )

    def _service_from_snapshot(self, snapshot: AIRuntimeSnapshot) -> EnterpriseRAGService:
        key = self._runtime_service_key(snapshot)
        with self._rag_service_lock:
            if self._rag_service is not None and self._rag_service_key == key:
                return self._rag_service
            self._rag_service = EnterpriseRAGService(
                settings=snapshot.settings,
                postgres_profiles=snapshot.postgres_profiles,
                postgres_password=snapshot.postgres_password,
                qdrant_profile=snapshot.qdrant_profile,
                qdrant_api_key=snapshot.qdrant_api_key,
            )
            self._rag_service_key = key
            return self._rag_service

    def _invalidate_rag_service(self) -> None:
        with self._rag_service_lock:
            self._rag_service = None
            self._rag_service_key = None

    def _warmup_rag_runtime(self) -> None:
        if self._rag_warmup_thread and self._rag_warmup_thread.is_alive():
            return
        snapshot = self._snapshot()
        self.rag_chip_var.set("RAG · warming BGE-M3…")

        def worker() -> None:
            started = time.perf_counter()
            try:
                service = self._service_from_snapshot(snapshot)
                service.warmup()
                elapsed = int((time.perf_counter() - started) * 1000)
                self.events.put(("rag_warmup_done", elapsed))
            except Exception as exc:
                self.events.put(("rag_warmup_error", str(exc)))

        self._rag_warmup_thread = threading.Thread(target=worker, name="rag-bge-warmup", daemon=True)
        self._rag_warmup_thread.start()

    # ------------------------------------------------------------------
    # Chat actions
    # ------------------------------------------------------------------

    def new_session(self) -> None:
        if self.chat_thread and self.chat_thread.is_alive():
            return
        self.current_session_id = self.sessions.create_session(scope=self.scope_var.get(), model=self.model_var.get())
        self.session_title_var.set("New conversation")
        self._clear_conversation()
        self._clear_evidence()
        self._show_welcome()
        self._refresh_session_list(select_id=self.current_session_id)
        self.composer.focus_set()

    def _ensure_session(self) -> str:
        if self.current_session_id and self.sessions.get_session(self.current_session_id):
            return self.current_session_id
        self.current_session_id = self.sessions.create_session(scope=self.scope_var.get(), model=self.model_var.get())
        self._refresh_session_list(select_id=self.current_session_id)
        return self.current_session_id

    def _send_shortcut(self, event=None):
        # Enter sends immediately. Shift+Enter remains available for an
        # intentional newline in longer engineering questions.
        if event is not None and (int(getattr(event, "state", 0)) & 0x0001):
            return None
        self.send_message()
        return "break"

    def send_message(self) -> None:
        if self.chat_thread and self.chat_thread.is_alive():
            return
        question = self.composer.get("1.0", END).strip()
        if not question:
            return
        model = self.model_var.get().strip()
        if not model:
            messagebox.showwarning("AI model", "No Ollama model is selected. Start Ollama, load a model, then refresh the model list.")
            return
        session_id = self._ensure_session()
        prior = self.sessions.get_messages(session_id)
        self.sessions.append_message(session_id, "user", question, {"scope": self.scope_var.get(), "model": model})
        if not any(item.get("role") == "user" for item in prior):
            title = question.replace("\n", " ").strip()[:48] or "Conversation"
            self.sessions.update_session(session_id, title=title, scope=self.scope_var.get(), model=model)
            self.session_title_var.set(title)
        self.composer.delete("1.0", END)
        self._append_message("user", question)
        self._begin_assistant_message()
        self._clear_evidence()
        self.cancel_event.clear()
        self.current_assistant_text = ""
        self.send_button.configure(state="disabled")
        self.stop_button.configure(state="normal")

        route = EnterpriseRAGService.classify_question(question)
        route_labels = {
            "DIRECT_CHAT": "日常问答",
            "SPEC_CATALOG": "式样书目录 SQL",
            "SPEC_LOOKUP": "式样书精确 SQL",
            "ISSUE_STATS": "QA/BUG 精确统计",
            "ISSUE_QUERY": "QA/BUG 企业检索",
            "ENGINEERING_AGENT": "工程 Agentic RAG",
        }
        self.agent_engine_var.set(f"AI AUTO · {route_labels.get(route, route)}")
        if route == "DIRECT_CHAT":
            self.workspace_status_var.set("日常问答 · 已跳过企业知识库")
            self.runtime_metrics_var.set("DIRECT_CHAT · Qdrant 0 · SQL 0 · Planner 0")
        elif route in {"SPEC_CATALOG", "SPEC_LOOKUP", "ISSUE_STATS"}:
            self.workspace_status_var.set("正在执行 PostgreSQL 权威结构化查询…")
            self.runtime_metrics_var.set(f"{route} · PostgreSQL structured")
        elif route == "ISSUE_QUERY":
            self.workspace_status_var.set("正在检索 QA/BUG/Jira 结构化记录与相关证据…")
            self.runtime_metrics_var.set("ISSUE AUTO · SQL + 按需 Qdrant")
        else:
            self.workspace_status_var.set("正在规划企业只读检索并收集证据…")
            self.runtime_metrics_var.set("AGENTIC RAG · Planner + governed tools")

        self._trace_request_started = time.perf_counter()
        self._first_token_ms = None
        self._append_trace_stage("ROUTE", route, 0)

        snapshot = self._snapshot()
        messages = [
            {"role": str(item.get("role") or "user"), "content": str(item.get("content") or "")}
            for item in self.sessions.get_messages(session_id)
            if item.get("role") in {"user", "assistant"}
        ]
        scope = self.scope_var.get()

        def worker() -> None:
            started = time.perf_counter()
            first_token_ms: int | None = None
            try:
                service = self._service_from_snapshot(snapshot)
                captured: list[RetrievalResult] = []

                def on_retrieval(result: RetrievalResult) -> None:
                    captured.append(result)
                    self.events.put(("retrieval", result))

                def on_stage(stage: str, detail: str, duration_ms: int) -> None:
                    self.events.put(("ai_stage", {"stage": stage, "detail": detail, "duration_ms": duration_ms}))

                for token in service.answer_stream(
                    messages,
                    model=model,
                    scope=scope,
                    current_scope=snapshot.current_scope,
                    cancel_check=self.cancel_event.is_set,
                    on_retrieval=on_retrieval,
                    on_stage=on_stage,
                ):
                    if self.cancel_event.is_set():
                        break
                    if first_token_ms is None:
                        first_token_ms = int((time.perf_counter() - started) * 1000)
                        self.events.put(("ai_stage", {
                            "stage": "TTFT",
                            "detail": f"首 Token 到达 · {first_token_ms} ms",
                            "duration_ms": first_token_ms,
                        }))
                    self.events.put(("chunk", token))
                elapsed = int((time.perf_counter() - started) * 1000)
                if captured and snapshot.settings.audit_enabled:
                    try:
                        self.audit.append(
                            session_id=session_id,
                            model=model,
                            scope=scope,
                            retrieval=captured[-1],
                            total_ms=elapsed,
                            status="CANCELLED" if self.cancel_event.is_set() else "OK",
                        )
                    except Exception:
                        pass
                self.events.put(("chat_done", {
                    "session_id": session_id,
                    "elapsed_ms": elapsed,
                    "first_token_ms": first_token_ms or 0,
                    "cancelled": self.cancel_event.is_set(),
                }))
            except Exception as exc:
                self.events.put(("chat_error", {"session_id": session_id, "error": str(exc), "type": type(exc).__name__}))

        self.chat_thread = threading.Thread(target=worker, name="enterprise-ai-chat", daemon=True)
        self.chat_thread.start()

    def stop_generation(self) -> None:
        self.cancel_event.set()
        self.workspace_status_var.set("正在停止生成…")

    # ------------------------------------------------------------------
    # Chat rendering
    # ------------------------------------------------------------------

    def _set_text_state(self, widget: tk.Text, enabled: bool) -> None:
        widget.configure(state="normal" if enabled else "disabled")

    def _clear_conversation(self) -> None:
        self._set_text_state(self.conversation, True)
        self.conversation.delete("1.0", END)
        self._set_text_state(self.conversation, False)

    def _show_welcome(self) -> None:
        self._set_text_state(self.conversation, True)
        if not self.conversation.get("1.0", END).strip():
            # Keep a clean empty conversation surface on first load.
            pass
        self._set_text_state(self.conversation, False)

    def _append_message(self, role: str, content: str) -> None:
        self._set_text_state(self.conversation, True)
        if role == "user":
            self.conversation.insert(END, "你\n", "user_label")
            self.conversation.insert(END, content.strip() + "\n", "user_body")
        else:
            self.conversation.insert(END, "AI 助手\n", "assistant_label")
            self.conversation.insert(END, content.strip() + "\n", "assistant_body")
        self.conversation.see(END)
        self._set_text_state(self.conversation, False)

    def _begin_assistant_message(self) -> None:
        self._set_text_state(self.conversation, True)
        self.conversation.insert(END, "AI 助手\n", "assistant_label")
        self.assistant_insert_mark = self.conversation.index(END)
        self._set_text_state(self.conversation, False)

    def _append_assistant_chunk(self, token: str) -> None:
        self._set_text_state(self.conversation, True)
        self.conversation.insert(END, token, "assistant_body")
        self.conversation.see(END)
        self._set_text_state(self.conversation, False)

    def _finish_assistant_message(self) -> None:
        self._set_text_state(self.conversation, True)
        self.conversation.insert(END, "\n", "assistant_body")
        self._set_text_state(self.conversation, False)

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    def _refresh_session_list(self, select_id: str | None = None) -> None:
        if not hasattr(self, "session_tree"):
            return
        self.session_tree.delete(*self.session_tree.get_children())
        for row in self.sessions.list_sessions(self.session_search_var.get()):
            iid = str(row["id"])
            self.session_tree.insert("", END, iid=iid, text=str(row.get("title") or "Conversation"))
        target = select_id or self.current_session_id
        if target and self.session_tree.exists(target):
            self.session_tree.selection_set(target)
            self.session_tree.focus(target)
            self.session_tree.see(target)

    def _on_session_selected(self, _event=None) -> None:
        selection = self.session_tree.selection()
        if not selection:
            return
        session_id = selection[0]
        if session_id == self.current_session_id and self.conversation.get("1.0", END).strip():
            return
        if self.chat_thread and self.chat_thread.is_alive():
            return
        session = self.sessions.get_session(session_id)
        if not session:
            return
        self.current_session_id = session_id
        self.session_title_var.set(str(session.get("title") or "Conversation"))
        self.scope_var.set(str(session.get("scope") or "AUTO"))
        stored_model = str(session.get("model") or "")
        if stored_model:
            self.model_var.set(stored_model)
        self._clear_conversation()
        for item in self.sessions.get_messages(session_id):
            role = str(item.get("role") or "")
            if role in {"user", "assistant"}:
                self._append_message(role, str(item.get("content") or ""))
        self._clear_evidence()

    def rename_current_session(self) -> None:
        if not self.current_session_id:
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("Rename conversation")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.resizable(False, False)
        frame = ttk.Frame(dialog, padding=16)
        frame.pack(fill=BOTH, expand=True)
        value = tk.StringVar(value=self.session_title_var.get())
        ttk.Label(frame, text="Conversation name", font=("Microsoft YaHei UI", 9, "bold")).pack(anchor="w")
        entry = ttk.Entry(frame, textvariable=value, width=48)
        entry.pack(fill=X, pady=(6, 12))
        entry.select_range(0, END)
        entry.focus_set()

        def save() -> None:
            title = value.get().strip()
            if title:
                self.sessions.update_session(self.current_session_id or "", title=title)
                self.session_title_var.set(title)
                self._refresh_session_list(select_id=self.current_session_id)
            dialog.destroy()

        ttk.Button(frame, text="Save", command=save, bootstyle="primary").pack(side=RIGHT)
        ttk.Button(frame, text="Cancel", command=dialog.destroy, bootstyle="secondary-outline").pack(side=RIGHT, padx=(0, 6))

    def delete_current_session(self) -> None:
        if not self.current_session_id:
            return
        if not messagebox.askyesno("Delete conversation", "Delete this AI conversation from local history?"):
            return
        self.sessions.delete_session(self.current_session_id)
        self.current_session_id = None
        self.session_title_var.set("New conversation")
        self._clear_conversation()
        self._clear_evidence()
        self._show_welcome()
        self._refresh_session_list()

    # ------------------------------------------------------------------
    # Evidence / visible execution trace
    # ------------------------------------------------------------------

    def _append_trace_stage(self, stage: str, detail: str, duration_ms: int = 0) -> None:
        if not hasattr(self, "agent_trace"):
            return
        elapsed = int((time.perf_counter() - self._trace_request_started) * 1000) if self._trace_request_started else 0
        suffix = f" · {duration_ms} ms" if duration_ms else ""
        line = f"[{elapsed:>6} ms] {stage}: {detail}{suffix}\n"
        self._set_text_state(self.agent_trace, True)
        self.agent_trace.insert(END, line)
        self.agent_trace.see(END)
        self._set_text_state(self.agent_trace, False)
        self.agent_trace_summary_var.set(f"{stage} · {detail[:54]}")

    def _clear_evidence(self) -> None:
        self.current_evidence = []
        self.evidence_by_iid.clear()
        if hasattr(self, "evidence_tree"):
            self.evidence_tree.delete(*self.evidence_tree.get_children())
        if hasattr(self, "evidence_detail"):
            self._set_text_state(self.evidence_detail, True)
            self.evidence_detail.delete("1.0", END)
            self._set_text_state(self.evidence_detail, False)
        self.evidence_summary_var.set("尚未加载证据")
        self.agent_trace_summary_var.set("AUTO ROUTER · 等待请求")
        if hasattr(self, "agent_trace"):
            self._set_text_state(self.agent_trace, True)
            self.agent_trace.delete("1.0", END)
            self._set_text_state(self.agent_trace, False)

    def _show_retrieval(self, result: RetrievalResult) -> None:
        self._clear_evidence()
        self.current_evidence = list(result.evidence)
        for item in result.evidence:
            iid = f"ev::{item.evidence_id}"
            score = "-" if item.score is None else f"{item.score:.3f}"
            issue_key = str(item.metadata.get("issue_key") or "").strip()
            title = issue_key or item.specification_code or item.specification_name or item.file_name or item.source_type
            self.evidence_tree.insert("", END, iid=iid, text=title[:34], values=(item.scope, item.source_type, score))
            self.evidence_by_iid[iid] = item

        scopes = ", ".join(result.resolved_scopes) or "-"
        if result.route == "DIRECT_CHAT":
            self.evidence_summary_var.set("日常问答 · 未访问企业知识库")
            self.runtime_metrics_var.set("DIRECT_CHAT · Qdrant 0 · SQL 0 · Planner 0 ms")
            self.agent_trace_summary_var.set(f"{result.request_id or '-'} · DIRECT_CHAT")
            trace_lines = [
                f"Request: {result.request_id or '-'}",
                "Route: DIRECT_CHAT",
                "知识检索：跳过",
                "Qdrant queries: 0",
                "PostgreSQL knowledge queries: 0",
                "说明：仅显示可审计处理步骤，不显示模型私有思维链。",
                "", "执行轨迹", "-" * 44, *result.trace,
            ]
            self.workspace_status_var.set("正在使用 Ollama 进行日常问答…")
        else:
            source = result.knowledge_source or "ENTERPRISE_AUTO"
            self.evidence_summary_var.set(
                f"{len(result.evidence)} 条治理证据 · {source} · {scopes} · 强度 {result.evidence_strength}"
            )
            warnings = " | ".join(result.warnings)
            timings = [
                f"Planner {result.planner_ms} ms",
                f"SQL {result.sql_ms} ms",
                f"Semantic {result.semantic_ms} ms",
                f"Relation {result.relation_ms} ms",
                f"Version {result.version_ms} ms",
            ]
            if result.qdrant_ms or result.embedding_ms or result.hydration_ms:
                timings.extend([
                    f"Embedding {result.embedding_ms} ms",
                    f"Qdrant {result.qdrant_ms} ms",
                    f"Hydrate {result.hydration_ms} ms",
                ])
            self.runtime_metrics_var.set(
                f"{result.route} · {source} · 范围 {scopes} · 证据 {len(result.evidence)} ({result.evidence_strength}) · "
                + " · ".join(timings)
                + (f" · Warning: {warnings}" if warnings else "")
            )
            self.agent_trace_summary_var.set(
                f"{result.request_id or '-'} · {result.route} · {result.evidence_strength}"
            )
            trace_lines = [
                f"Request: {result.request_id or '-'}",
                f"Route: {result.route}",
                f"Knowledge source: {source}",
                f"Scopes: {scopes}",
                f"Intent: {', '.join(result.intents) or '-'}",
                f"Tools: {', '.join(result.tools) or '-'}",
                f"Entities: {', '.join(result.entities) or '-'}",
                f"Evidence: {len(result.evidence)} · {result.evidence_strength}",
                f"Planner: {result.planner_ms} ms",
                f"SQL: {result.sql_ms} ms",
                f"Semantic/Qdrant pipeline: {result.semantic_ms} ms",
                f"Relation: {result.relation_ms} ms",
                f"Version: {result.version_ms} ms",
                "说明：仅显示路由、工具、检索、证据和耗时，不显示模型私有思维链。",
            ]
            if result.plan_summary:
                trace_lines.append(f"Plan summary: {result.plan_summary}")
            if result.rewritten_queries:
                trace_lines.append("Queries: " + " | ".join(result.rewritten_queries[:4]))
            if result.conflicts:
                trace_lines.append("Conflicts: " + " | ".join(result.conflicts))
            trace_lines.extend(["", "执行轨迹", "-" * 44, *result.trace])
            route_status = {
                "SPEC_CATALOG": "正在根据 PostgreSQL 式样书目录生成严谨回答…",
                "SPEC_LOOKUP": "正在根据 PostgreSQL 精确规格元数据生成回答…",
                "ISSUE_STATS": "正在根据 QA/BUG 精确统计生成回答…",
                "ISSUE_QUERY": "正在根据 QA/BUG 记录与治理证据生成回答…",
                "ENGINEERING_AGENT": "正在根据企业工程证据生成回答…",
            }
            self.workspace_status_var.set(route_status.get(result.route, "正在根据企业治理证据生成回答…"))

        self._set_text_state(self.agent_trace, True)
        self.agent_trace.delete("1.0", END)
        self.agent_trace.insert("1.0", "\n".join(trace_lines))
        self._set_text_state(self.agent_trace, False)
        if result.evidence:
            first = self.evidence_tree.get_children()[0]
            self.evidence_tree.selection_set(first)
            self.evidence_tree.focus(first)
            self._on_evidence_selected()

    def _on_evidence_selected(self, _event=None) -> None:
        selection = self.evidence_tree.selection()
        if not selection:
            return
        item = self.evidence_by_iid.get(selection[0])
        if not item:
            return
        lines = [
            f"Evidence [{item.evidence_id}]",
            f"Scope: {item.scope}",
            f"Type: {item.source_type}",
            f"Score: {'-' if item.score is None else f'{item.score:.4f}'}",
            f"Evidence Grade: {item.metadata.get('evidence_grade', '-')}",
            f"Governance Score: {item.metadata.get('governance_score', '-')}",
            f"Content ID: {item.content_id if item.content_id is not None else '-'}",
            f"Relation ID: {item.relation_id if item.relation_id is not None else '-'}",
        ]
        if str(item.metadata.get("artifact_type") or "").upper() == "ISSUE_REGISTER" or item.source_type.startswith("ISSUE"):
            lines.extend([
                f"Issue Key: {item.metadata.get('issue_key') or '-'}",
                f"Issue Type: {item.metadata.get('issue_type') or '-'}",
                f"Status: {item.metadata.get('issue_status') or item.metadata.get('status') or '-'}",
                f"Priority: {item.metadata.get('priority') or '-'}",
                f"Component: {item.metadata.get('component') or '-'}",
                f"Assignee: {item.metadata.get('assignee') or '-'}",
                f"Fix Version: {item.metadata.get('fix_version') or '-'}",
                f"QA Result: {item.metadata.get('qa_result') or '-'}",
            ])
        else:
            lines.extend([
                f"Specification: {item.specification_code} {item.specification_name}".strip(),
                f"Version: {item.specification_version or '-'}",
                f"Section: {item.section_id} {item.section_title}".strip(),
                f"Page: {item.page_number if item.page_number is not None else '-'}",
            ])
        lines.extend([
            "",
            "SOURCE TEXT / 权威证据",
            "-" * 48,
            item.content,
        ])
        self._set_text_state(self.evidence_detail, True)
        self.evidence_detail.delete("1.0", END)
        self.evidence_detail.insert("1.0", "\n".join(lines))
        self._set_text_state(self.evidence_detail, False)

    # ------------------------------------------------------------------
    # Model / config
    # ------------------------------------------------------------------

    def _ollama_client_for_ui(self, url: str | None = None) -> OllamaClient:
        def _number(variable: tk.StringVar, fallback: float) -> float:
            try:
                return float(variable.get())
            except (TypeError, ValueError, tk.TclError):
                return float(fallback)

        return OllamaClient(
            url or self.cfg_ollama_url.get().strip() or self.settings.ollama_url,
            connect_timeout=_number(self.cfg_ollama_connect_timeout, self.settings.ollama_connect_timeout),
            read_timeout=_number(self.cfg_ollama_read_timeout, self.settings.ollama_read_timeout),
            first_load_timeout=_number(self.cfg_ollama_first_load_timeout, self.settings.ollama_first_load_timeout),
            stream_idle_timeout=_number(self.cfg_ollama_stream_idle_timeout, self.settings.ollama_stream_idle_timeout),
        )

    def refresh_models(self) -> None:
        url = self.cfg_ollama_url.get().strip() if hasattr(self, "cfg_ollama_url") else self.settings.ollama_url
        self.ollama_chip_var.set("Ollama · checking")

        def worker() -> None:
            try:
                models = self._ollama_client_for_ui(url).list_models()
                self.events.put(("models", models))
            except Exception as exc:
                self.events.put(("models_error", str(exc)))

        threading.Thread(target=worker, name="ollama-model-list", daemon=True).start()

    def save_configuration(self) -> None:
        try:
            settings = AISettings.from_mapping(
                {
                    "ollama_url": self.cfg_ollama_url.get().strip(),
                    "chat_model": self.cfg_chat_model.get().strip() or self.model_var.get().strip(),
                    "temperature": float(self.cfg_temperature.get()),
                    "num_ctx": int(self.cfg_num_ctx.get()),
                    "top_k": int(self.cfg_top_k.get()),
                    "evidence_limit": int(self.cfg_evidence_limit.get()),
                    "planner_context_turns": int(self.cfg_planner_turns.get()),
                    "prompt_budget_tokens": int(self.cfg_prompt_budget.get()),
                    "generation_reserve_tokens": int(self.cfg_generation_reserve.get()),
                    "history_budget_tokens": int(self.cfg_history_budget.get()),
                    "evidence_budget_tokens": int(self.cfg_evidence_budget.get()),
                    "answer_history_turns": int(self.cfg_answer_history_turns.get()),
                    "simple_issue_evidence_limit": int(self.cfg_simple_issue_evidence_limit.get()),
                    "audit_enabled": self.cfg_audit_enabled.get(),
                    "max_context_chars": int(self.cfg_max_context.get()),
                    "min_score": float(self.cfg_min_score.get()),
                    "retrieval_mode": "AI AUTO",
                    "embedding_model": self.cfg_embedding_model.get().strip(),
                    "vector_dimension": int(self.cfg_vector_dimension.get()),
                    "collections": {
                        "21MM": self.cfg_collection_21.get().strip(),
                        "24MM": self.cfg_collection_24.get().strip(),
                        "COMMON": self.cfg_collection_common.get().strip(),
                        "QABUG": self.cfg_collection_qabug.get().strip(),
                    },
                    "system_prompt": self.system_prompt_text.get("1.0", END).strip(),
                    "api_enabled": self.cfg_api_enabled.get(),
                    "api_host": self.cfg_api_host.get().strip(),
                    "api_port": int(self.cfg_api_port.get()),
                    "external_webui_url": validate_local_webui_url(self.cfg_external_webui_url.get()),
                    "api_host": "127.0.0.1",
                    "api_enabled": False,
                    "api_key": "",
                    "ollama_keep_alive": self.cfg_ollama_keep_alive.get().strip() or "30m",
                    "ollama_connect_timeout": float(self.cfg_ollama_connect_timeout.get()),
                    "ollama_read_timeout": float(self.cfg_ollama_read_timeout.get()),
                    "ollama_first_load_timeout": float(self.cfg_ollama_first_load_timeout.get()),
                    "ollama_stream_idle_timeout": float(self.cfg_ollama_stream_idle_timeout.get()),
                    "fast_intent_router": True,
                    "qdrant_only_knowledge": False,
                }
            )
        except ValueError as exc:
            messagebox.showerror("AI 配置", f"数值配置无效：{exc}")
            return
        self.settings_store.save(settings)
        self.settings = settings
        self._invalidate_rag_service()
        # Independent backend reads the saved config after restart; avoid GUI BGE model reload.
        self.model_var.set(settings.chat_model or self.model_var.get())
        self.config_status_var.set(f"Saved: {self.settings_store.path}")
        messagebox.showinfo("AI 配置", "AI / RAG 企业配置已保存。")

    # ------------------------------------------------------------------
    # Health / API
    # ------------------------------------------------------------------

    def run_health_check(self) -> None:
        if self.status_thread and self.status_thread.is_alive():
            return
        snapshot = self._snapshot()
        self.status_ollama_var.set("检查中…")
        self.status_postgres_var.set("检查中…")
        self.status_qdrant_var.set("检查中…")
        self.status_rag_var.set("检查中…")
        self.status_detail_var.set("正在执行端到端健康检查…")

        def worker() -> None:
            details: list[str] = []
            ollama = OllamaClient(
                snapshot.settings.ollama_url,
                connect_timeout=snapshot.settings.ollama_connect_timeout,
                read_timeout=snapshot.settings.ollama_read_timeout,
                first_load_timeout=snapshot.settings.ollama_first_load_timeout,
                stream_idle_timeout=snapshot.settings.ollama_stream_idle_timeout,
            ).health()
            ollama_severity = SEVERITY_OK if ollama["ok"] else SEVERITY_ERROR
            self.events.put(("health_item", ("ollama", ollama["message"], ollama_severity)))
            details.append(f"Ollama: {ollama['message']}")

            pg_ok = True
            pg_messages: list[str] = []
            pg_counts: dict[str, int] = {}
            for scope, profile in snapshot.postgres_profiles.items():
                try:
                    kwargs = profile.dsn_kwargs()
                    kwargs["password"] = snapshot.postgres_password
                    with psycopg.connect(**kwargs) as conn, conn.cursor() as cur:
                        if scope == "QABUG":
                            cur.execute("SELECT COUNT(*) FROM public.qabug_current_chunks WHERE LENGTH(BTRIM(content)) > 0")
                        else:
                            cur.execute(
                                "SELECT COUNT(*) FROM public.rag_current_chunks "
                                "WHERE project_code = %s AND LENGTH(BTRIM(content)) > 0",
                                (scope,),
                            )
                        count = int(cur.fetchone()[0])
                    pg_counts[scope] = count
                    pg_messages.append(f"{scope}/{profile.database}={count} chunks")
                except Exception as exc:
                    pg_ok = False
                    pg_messages.append(f"{scope}/{profile.database}=ERROR {exc}")
            pg_message = " · ".join(pg_messages)
            self.events.put(("health_item", ("postgres", pg_message, SEVERITY_OK if pg_ok else SEVERITY_ERROR)))
            details.append("PostgreSQL: " + pg_message)

            sync_warnings: list[str] = []
            critical_errors: list[str] = []
            q_ok = True
            q_message = "Not checked"
            try:
                qservice = QdrantService(snapshot.qdrant_profile, snapshot.qdrant_api_key)
                cols = qservice.test_connection()
                summaries = {item.name: item for item in qservice.list_collections()}
                q_message = f"Connected · {len(cols)} collections"
                details.append("Qdrant: " + q_message + " · " + ", ".join(cols[:10]))

                for scope, collection in snapshot.settings.collections.items():
                    summary = summaries.get(collection)
                    sql_count = pg_counts.get(scope)
                    if summary is None:
                        if is_valid_empty_scope(sql_count=sql_count, collection_exists=False):
                            details.append(
                                f"Index {scope}: SKIPPED · SQL current=0 · collection {collection} absent (valid empty scope)"
                            )
                            continue
                        sync_warnings.append(
                            f"{scope}: collection {collection} missing while SQL has {sql_count if sql_count is not None else '?'} current chunks"
                        )
                        continue

                    points = int(summary.points_count or 0)
                    details.append(
                        f"Index {scope}: SQL current={sql_count if sql_count is not None else '?'} · "
                        f"Qdrant={points} · vector={summary.vector_size} · status={summary.status}"
                    )
                    if str(summary.vector_size) not in {
                        str(snapshot.settings.vector_dimension),
                        f"{snapshot.settings.vector_dimension}.0",
                    }:
                        critical_errors.append(
                            f"{scope}: vector dimension {summary.vector_size} != {snapshot.settings.vector_dimension}"
                        )
                    if sql_count is not None and sql_count != points:
                        backlog = max(0, sql_count - points)
                        extra = max(0, points - sql_count)
                        delta = f"missing/backlog={backlog}" if backlog else f"orphan/extra={extra}"
                        sync_warnings.append(
                            f"{scope}: SQL current chunks={sql_count}, Qdrant points={points}, {delta}; run incremental SQL → Qdrant Sync"
                        )

                    if sql_count is None or (sql_count <= 0 and points <= 0):
                        continue

                    try:
                        if scope == "QABUG":
                            report = QABugDataQualityValidator(
                                postgres_profile=snapshot.postgres_profiles[scope],
                                password=snapshot.postgres_password,
                                qdrant_service=qservice,
                                collection_name=collection,
                            ).validate()
                            fanout = qabug_chunk_fanout(records=report.sql_records, chunks=report.sql_chunks)
                            details.append(
                                f"Governance {scope}: {report.status} · SQL records={report.sql_records} · SQL chunks={report.sql_chunks} "
                                f"· fan-out={fanout:.2f} chunks/record (structured field-level chunking) "
                                f"· quarantine={getattr(report, 'quarantined_records', 0)} · Qdrant={report.qdrant_points} "
                                f"· missing={report.missing_points} · orphans={report.orphan_points} · stale={report.stale_points}"
                            )
                            if report.missing_points or report.orphan_points or report.stale_points:
                                sync_warnings.append(
                                    f"{scope}: derived vector index is not current "
                                    f"(missing={report.missing_points}, orphan={report.orphan_points}, stale={report.stale_points}); "
                                    "use controlled incremental sync, not a forced full rebuild"
                                )
                            if report.missing_payload_indexes:
                                sync_warnings.append(
                                    f"{scope}: payload index contract is incomplete; {len(report.missing_payload_indexes)} index(es) missing; repair indexes"
                                )
                            if report.mismatched_payload_indexes:
                                critical_errors.append(
                                    f"{scope}: payload index type contract mismatch: {', '.join(report.mismatched_payload_indexes)}"
                                )
                            for warning in report.warnings:
                                details.append(f"Governance {scope} note: {warning}")
                        else:
                            validator = QdrantDataQualityValidator(
                                postgres=snapshot.postgres_profiles[scope],
                                postgres_password=snapshot.postgres_password,
                                qdrant=qservice,
                                project_code=scope,
                                collection_name=collection,
                                embedding_model=snapshot.settings.embedding_model,
                                embedding_version="current-index-v1",
                                vector_dimension=snapshot.settings.vector_dimension,
                            )
                            report = validator.validate()
                            details.append(
                                f"Governance {scope}: {report.status} · indexes={report.payload_index_present}/{report.payload_index_expected} "
                                f"· errors={report.error_count} · warnings={report.warning_count} "
                                f"· missing={len(report.missing_point_ids)} · orphans={len(report.orphan_point_ids)}"
                            )
                            for issue in report.issues:
                                classified = classify_quality_issue(
                                    code=issue.code, reported_severity=issue.severity
                                )
                                message = f"{scope}: {issue.code} · {issue.message}"
                                if classified == SEVERITY_ERROR:
                                    critical_errors.append(message)
                                else:
                                    sync_warnings.append(message)
                    except Exception as exc:
                        sync_warnings.append(f"{scope}: data-quality validation unavailable: {exc}")
            except Exception as exc:
                q_ok = False
                q_message = str(exc)
                details.append("Qdrant: ERROR " + q_message)

            self.events.put(("health_item", ("qdrant", q_message, SEVERITY_OK if q_ok else SEVERITY_ERROR)))

            # Keep messages compact: validators can emit one issue per point.
            sync_warnings = list(dict.fromkeys(sync_warnings))
            critical_errors = list(dict.fromkeys(critical_errors))
            dependency_ok = bool(ollama["ok"] and pg_ok and q_ok)
            decision = classify_runtime(
                dependencies_ok=dependency_ok,
                critical_errors=critical_errors,
                sync_warnings=sync_warnings,
            )
            if critical_errors:
                details.append("Critical Guard: " + " | ".join(critical_errors[:20]))
                if len(critical_errors) > 20:
                    details.append(f"Critical Guard: +{len(critical_errors) - 20} more issue(s)")
            if sync_warnings:
                details.append("Index Guard: " + " | ".join(sync_warnings[:20]))
                if len(sync_warnings) > 20:
                    details.append(f"Index Guard: +{len(sync_warnings) - 20} more warning(s)")
            self.events.put(("health_item", ("rag", decision.message, decision.severity)))
            details.append(f"RAG Runtime: {decision.severity} · {decision.message}")
            self.events.put(("health_done", "\n".join(details)))

        self.status_thread = threading.Thread(target=worker, name="ai-health-check", daemon=True)
        self.status_thread.start()

    def start_api_server(self) -> None:
        self.status_api_var.set("独立 Backend 由管理工具自动启动；可在 Settings 检查运行状态")
        messagebox.showinfo("本地 API", "独立 Backend 由管理工具自动确保运行；关闭桌面不会主动关闭 API。")

    def stop_api_server(self) -> None:
        if self.api_server:
            self.api_server.stop()
        self.api_server = None
        self.status_api_var.set("已停止")

    def save_configuration_silent(self) -> None:
        try:
            settings = AISettings.from_mapping(
                {
                    **asdict(self.settings),
                    "ollama_url": self.cfg_ollama_url.get().strip(),
                    "chat_model": self.cfg_chat_model.get().strip() or self.model_var.get().strip(),
                    "temperature": float(self.cfg_temperature.get()),
                    "num_ctx": int(self.cfg_num_ctx.get()),
                    "top_k": int(self.cfg_top_k.get()),
                    "evidence_limit": int(self.cfg_evidence_limit.get()),
                    "planner_context_turns": int(self.cfg_planner_turns.get()),
                    "prompt_budget_tokens": int(self.cfg_prompt_budget.get()),
                    "generation_reserve_tokens": int(self.cfg_generation_reserve.get()),
                    "history_budget_tokens": int(self.cfg_history_budget.get()),
                    "evidence_budget_tokens": int(self.cfg_evidence_budget.get()),
                    "answer_history_turns": int(self.cfg_answer_history_turns.get()),
                    "simple_issue_evidence_limit": int(self.cfg_simple_issue_evidence_limit.get()),
                    "audit_enabled": self.cfg_audit_enabled.get(),
                    "max_context_chars": int(self.cfg_max_context.get()),
                    "min_score": float(self.cfg_min_score.get()),
                    "retrieval_mode": "AI AUTO",
                    "collections": {
                        "21MM": self.cfg_collection_21.get().strip(),
                        "24MM": self.cfg_collection_24.get().strip(),
                        "COMMON": self.cfg_collection_common.get().strip(),
                        "QABUG": self.cfg_collection_qabug.get().strip(),
                    },
                    "system_prompt": self.system_prompt_text.get("1.0", END).strip(),
                    "api_enabled": self.cfg_api_enabled.get(),
                    "api_host": self.cfg_api_host.get().strip(),
                    "api_port": int(self.cfg_api_port.get()),
                    "external_webui_url": validate_local_webui_url(self.cfg_external_webui_url.get()),
                    "api_host": "127.0.0.1",
                    "api_enabled": False,
                    "api_key": "",
                    "ollama_keep_alive": self.cfg_ollama_keep_alive.get().strip() or "30m",
                    "ollama_connect_timeout": float(self.cfg_ollama_connect_timeout.get()),
                    "ollama_read_timeout": float(self.cfg_ollama_read_timeout.get()),
                    "ollama_first_load_timeout": float(self.cfg_ollama_first_load_timeout.get()),
                    "ollama_stream_idle_timeout": float(self.cfg_ollama_stream_idle_timeout.get()),
                    "fast_intent_router": True,
                    "qdrant_only_knowledge": False,
                }
            )
            self.settings_store.save(settings)
            self.settings = settings
            self._invalidate_rag_service()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Event loop / lifecycle
    # ------------------------------------------------------------------

    def _poll_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "models":
                    models = payload
                    self.model_names = [model.name for model in models]
                    self.model_combo.configure(values=self.model_names)
                    if self.model_names:
                        current = self.model_var.get().strip()
                        if current not in self.model_names:
                            preferred = self.settings.chat_model if self.settings.chat_model in self.model_names else self.model_names[0]
                            self.model_var.set(preferred)
                        if not self.cfg_chat_model.get().strip():
                            self.cfg_chat_model.set(self.model_var.get())
                    self.ollama_chip_var.set(f"Ollama · {len(self.model_names)} models")
                    self.status_ollama_var.set(f"已连接 · {len(self.model_names)} 个模型")
                elif kind == "models_error":
                    self.ollama_chip_var.set("Ollama · offline")
                    self.status_ollama_var.set(f"离线 · {payload}")
                elif kind == "rag_warmup_done":
                    self.rag_chip_var.set(f"RAG · 企业 AUTO · warm ({payload} ms)")
                elif kind == "rag_warmup_error":
                    self.rag_chip_var.set("RAG · 企业 AUTO · lazy")
                elif kind == "ai_stage":
                    stage = str(payload.get("stage") or "STEP")
                    detail = str(payload.get("detail") or "")
                    duration_ms = int(payload.get("duration_ms") or 0)
                    self._append_trace_stage(stage, detail, duration_ms)
                    if stage == "EMBEDDING":
                        self.workspace_status_var.set(detail)
                    elif stage == "QDRANT":
                        self.workspace_status_var.set(detail)
                    elif stage == "EVIDENCE":
                        self.workspace_status_var.set(detail)
                    elif stage == "OLLAMA":
                        self.workspace_status_var.set(detail)
                elif kind == "retrieval":
                    self._show_retrieval(payload)
                elif kind == "chunk":
                    token = str(payload)
                    self.current_assistant_text += token
                    self._append_assistant_chunk(token)
                elif kind == "chat_done":
                    self._finish_assistant_message()
                    if self.current_assistant_text.strip() and self.current_session_id == payload.get("session_id"):
                        metadata = {
                            "evidence": [asdict(item) for item in self.current_evidence],
                            "elapsed_ms": payload.get("elapsed_ms"),
                            "cancelled": payload.get("cancelled", False),
                        }
                        self.sessions.append_message(self.current_session_id, "assistant", self.current_assistant_text, metadata)
                    self.send_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                    self.workspace_status_var.set("已停止" if payload.get("cancelled") else "回答完成")
                    self.runtime_metrics_var.set(
                        self.runtime_metrics_var.get()
                        + f" · TTFT {payload.get('first_token_ms', 0)} ms · 总耗时 {payload.get('elapsed_ms', 0)} ms"
                    )
                    self._refresh_session_list(select_id=self.current_session_id)
                    self.refresh_audit_view()
                elif kind == "chat_error":
                    self._append_assistant_chunk(f"\n\n[AI runtime error] {payload.get('error')}")
                    self._finish_assistant_message()
                    self.send_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                    self.workspace_status_var.set("AI 请求失败")
                    self.runtime_metrics_var.set(f"错误 · {payload.get('type')}: {payload.get('error')}")
                elif kind == "external_webui_status":
                    self.external_webui_status_var.set(str(payload))
                elif kind == "health_item":
                    name, message, severity = payload
                    normalized = str(severity or SEVERITY_ERROR).upper()
                    prefix = f"{normalized} · "
                    if name == "ollama":
                        self.status_ollama_var.set(prefix + str(message))
                    elif name == "postgres":
                        self.status_postgres_var.set(prefix + str(message))
                    elif name == "qdrant":
                        self.status_qdrant_var.set(prefix + str(message))
                    elif name == "rag":
                        self.status_rag_var.set(prefix + str(message))
                elif kind == "health_done":
                    self.status_detail_var.set(str(payload))
        except queue.Empty:
            pass
        try:
            self.root.after(120, self._poll_events)
        except tk.TclError:
            pass

    def on_scope_changed(self) -> None:
        self._api_current_scope = self._current_scope()
        self.rag_chip_var.set(f"RAG · {self._api_current_scope or 'AUTO'}")

    def activate_page(self, page_key: str) -> None:
        if page_key == "ai_status":
            self.status_api_var.set(f"独立后台（不随 GUI 关闭） · http://127.0.0.1:{self.settings.api_port}/v1")

    def shutdown(self) -> None:
        self.cancel_event.set()
        self.stop_api_server()
