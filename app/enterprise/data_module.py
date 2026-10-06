from __future__ import annotations

import json
import logging
import os
import queue
import threading
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import BOTH, END, LEFT, RIGHT, X, Y, filedialog, messagebox
import tkinter as tk
from tkinter import ttk as tkttk
from typing import Any, Callable

import ttkbootstrap as ttk
from ttkbootstrap.constants import HORIZONTAL

try:
    import sqlparse
except ImportError:
    sqlparse = None

from app.enterprise.profiles import ConsoleProfile, PostgresProfile, QdrantProfile
from app.enterprise.widgets import ChartDialog, ERDiagramFrame, ExplainPlanDialog, RecordEditorDialog
from app.enterprise.postgres_service import PostgresService, QueryResult
from app.enterprise.qdrant_service import QdrantService
from app.project.project_registry import ProjectRegistry, ProjectRegistryError
from app.rag.qdrant_sync import (
    CurrentQdrantSyncService,
    QdrantSyncCancelled,
    QdrantSyncPlan,
    QdrantSyncResult,
)
from app.rag.qdrant_governance import (
    PAYLOAD_INDEX_CONTRACT,
    QdrantDataQualityValidator,
    QdrantQualityReport,
)
from app.qabug.qdrant_sync import QABUG_COLLECTION, QABUG_PAYLOAD_INDEX_CONTRACT, QABugQdrantManager
from app.qabug.quality import QABugDataQualityValidator, QABugQualityReport
from app.qabug.sync_adapter import QABugCurrentQdrantSyncService

LOGGER = logging.getLogger("document_intelligence_console.enterprise")


@dataclass
class AsyncEvent:
    kind: str
    payload: Any = None


class DataConsoleModule:
    """Embeddable PostgreSQL/RAG/Qdrant administration module.

    The module intentionally receives its PostgreSQL settings from the host
    application. This keeps Processing Scope and database routing as the
    single source of truth across ingestion, versioning and administration.
    """

    def __init__(self, host: Any, pages: dict[str, ttk.Frame]) -> None:
        self.host = host
        self.root = host.root
        self.event_queue: queue.Queue[AsyncEvent] = queue.Queue()
        self.active_threads: set[threading.Thread] = set()
        self.pg_service: PostgresService | None = None
        self.qdrant_service: QdrantService | None = None

        self.current_schema = ""
        self.current_table = ""
        self.current_object_type = ""
        self.current_table_page = 0
        self.current_table_result: QueryResult | None = None
        self.current_table_columns: list[dict[str, Any]] = []
        self.current_table_pk: list[str] = []
        self.table_row_map: dict[str, dict[str, Any]] = {}
        self.current_sql_result: QueryResult | None = None
        self.current_visual_result: QueryResult | None = None
        self.sql_history: list[str] = []
        self.rag_sync_thread: threading.Thread | None = None
        self.rag_sync_cancel_event = threading.Event()
        self._rag_sync_current_run_id = 0
        self._rag_sync_current_mode = ""
        self._rag_sync_current_started = ""

        # Tracks which PostgreSQL context has already been loaded by each live
        # administration page.  The signature changes automatically when the
        # global Processing Scope changes (for example 24MM -> rag_24mm).
        self._loaded_context: dict[str, tuple[str, int, str, str]] = {}

        self._create_vars()
        self._build_pages(pages)
        self._bind_shortcuts()
        self._rebuild_services()
        self._poll_events()

    def _create_vars(self) -> None:
        self.status_var = tk.StringVar(value="Ready")
        self.connection_var = tk.StringVar(value="PostgreSQL: not tested")
        self.qdrant_status_var = self.host.qdrant_status_var

        self.table_filter_var = tk.StringVar()
        self.table_sort_var = tk.StringVar()
        self.table_sort_dir_var = tk.StringVar(value="ASC")
        self.table_page_var = tk.StringVar(value="Page 1")
        self.table_title_var = tk.StringVar(value="Select a table")
        self.allow_table_write_var = tk.BooleanVar(value=False)

        self.sql_status_var = tk.StringVar(value="No query executed.")
        self.allow_sql_write_var = tk.BooleanVar(value=False)
        self.chunk_meta_var = tk.StringVar(value="Select a chunk.")
        self.doc_hierarchy_var = tk.StringVar(value="Specification → Version → Document → Chapter → Section → Chunk")

        # These are aliases to the host application's single global settings.
        self.pg_host_var = self.host.postgres_host_var
        self.pg_port_var = self.host.postgres_port_var
        self.pg_db_var = self.host.postgres_database_var
        self.pg_user_var = self.host.postgres_user_var
        self.pg_password_var = self.host.postgres_password_var
        self.pg_timeout_var = self.host.postgres_timeout_var
        self.pg_sslmode_var = tk.StringVar(value="prefer")

        self.q_url_var = self.host.qdrant_url_var
        self.q_key_var = self.host.qdrant_api_key_var
        self.q_timeout_var = self.host.qdrant_timeout_var
        self.page_size_var = self.host.console_page_size_var
        self.embedding_status_var = tk.StringVar(value="PENDING")
        self.rag_sync_collection_var = tk.StringVar(value="")
        self.rag_sync_model_var = tk.StringVar(value="BAAI/bge-m3")
        self.rag_sync_dimension_var = tk.StringVar(value="1024")
        self.rag_sync_batch_var = tk.StringVar(value="32")
        self.rag_sync_scope_var = tk.StringVar(value="-")
        self.rag_sync_database_var = tk.StringVar(value="-")
        self.rag_sync_source_var = tk.StringVar(value="public.rag_current_chunks · CURRENT ONLY")
        self.rag_sync_status_var = tk.StringVar(value="Preview the current-only source before syncing.")
        self.rag_sync_progress_var = tk.DoubleVar(value=0.0)
        self.rag_sync_progress_text_var = tk.StringVar(value="Idle")
        self.rag_sync_sql_count_var = tk.StringVar(value="SQL Current 0")
        self.rag_sync_qdrant_count_var = tk.StringVar(value="Qdrant 0")
        self.rag_sync_new_var = tk.StringVar(value="New 0")
        self.rag_sync_changed_var = tk.StringVar(value="Changed 0")
        self.rag_sync_unchanged_var = tk.StringVar(value="Unchanged 0")
        self.rag_sync_stale_var = tk.StringVar(value="Stale 0")
        self.rag_quality_status_var = tk.StringVar(value="Data Quality: not validated")
        self.rag_payload_index_var = tk.StringVar(value=f"Payload Indexes 0/{len(PAYLOAD_INDEX_CONTRACT)}")

        self.visual_schema_var = tk.StringVar(value="public")
        self.visual_base_var = tk.StringVar()
        self.visual_join_var = tk.StringVar()
        self.visual_join_type_var = tk.StringVar(value="INNER JOIN")
        self.visual_left_col_var = tk.StringVar()
        self.visual_right_col_var = tk.StringVar()
        self.visual_where_var = tk.StringVar()
        self.visual_order_var = tk.StringVar()
        self.visual_order_dir_var = tk.StringVar(value="ASC")
        self.visual_limit_var = tk.StringVar(value="200")
        self.erd_schema_var = tk.StringVar(value="public")

    def _profile_from_ui(self) -> ConsoleProfile:
        postgres = PostgresProfile(
            host=self.pg_host_var.get().strip(),
            port=int(self.pg_port_var.get().strip()),
            database=self.pg_db_var.get().strip(),
            user=self.pg_user_var.get().strip(),
            connect_timeout=int(self.pg_timeout_var.get().strip()),
            sslmode=self.pg_sslmode_var.get().strip() or "prefer",
        )
        qdrant = QdrantProfile(
            url=self.q_url_var.get().strip() or "http://127.0.0.1:6333",
            timeout=float(self.q_timeout_var.get().strip() or "10"),
        )
        return ConsoleProfile(
            postgres=postgres,
            qdrant=qdrant,
            page_size=max(10, min(int(self.page_size_var.get().strip() or "100"), 1000)),
            max_preview_chars=12000,
        )

    def _rebuild_services(self) -> None:
        self.profile = self._profile_from_ui()
        password = self.pg_password_var.get() or os.getenv("POSTGRES_PASSWORD", "")
        self.pg_service = PostgresService(self.profile.postgres, password)
        self.qdrant_service = QdrantService(self.profile.qdrant, self.q_key_var.get())

    def _build_pages(self, pages: dict[str, ttk.Frame]) -> None:
        self.db_tab = pages["database"]
        self.sql_tab = pages["sql"]
        self.visual_tab = pages["visual"]
        self.erd_tab = pages["erd"]
        self.doc_tab = pages["documents"]
        self.qdrant_tab = pages["qdrant"]
        self.embedding_tab = pages["embedding"]
        self.qdrant_sync_tab = pages["qdrant_sync"]

        self._build_database_tab()
        self._build_sql_tab()
        self._build_visual_query_tab()
        self._build_erd_tab()
        self._build_document_tab()
        self._build_qdrant_tab()
        self._build_embedding_tab()
        self._build_qdrant_sync_tab()

    def on_connection_verified(self) -> None:
        """Rebind all enterprise services to the password/profile just tested.

        Settings owns the credential entry, while the administration pages own
        long-lived service objects. Rebuilding here makes a successful Test
        Connection immediately authoritative for Dashboard, Explorer, ERD,
        Document Explorer and Embedding pages.
        """
        self._loaded_context.clear()
        self._rebuild_services()
        profile = self.profile.postgres
        self.connection_var.set(
            f"PostgreSQL ready: {profile.host}:{profile.port}/{profile.database}"
        )

    def on_scope_changed(self) -> None:
        """Invalidate cached table/document state after the global scope changes."""
        self.current_schema = ""
        self.current_table = ""
        self.current_object_type = ""
        self.current_table_page = 0
        self.current_table_result = None
        self.current_sql_result = None
        self.current_visual_result = None
        self.connection_var.set("PostgreSQL: scope changed; loading active database…")
        self._loaded_context.clear()
        if hasattr(self, "db_tree"):
            self._clear_tree(self.db_tree)
        if hasattr(self, "erd_frame"):
            self.erd_frame.set_model({"tables": [], "foreign_keys": []})
        self._rebuild_services()

        page_labels = {
            "Database Explorer": "database",
            "Visual Query": "visual",
            "ER Diagram": "erd",
            "Document Explorer": "documents",
            "Embedding Monitor": "embedding",
            "SQL → Qdrant Sync": "qdrant_sync",
        }
        active = page_labels.get(self.host.current_page_var.get())
        if active:
            self.root.after_idle(lambda target=active: self.activate_page(target, force=True))

    def _postgres_context_signature(self) -> tuple[str, int, str, str]:
        profile = self._profile_from_ui().postgres
        return (profile.host, profile.port, profile.database, profile.user)

    def activate_page(self, page_key: str, *, force: bool = False) -> None:
        """Load live data when a unified-console page becomes visible.

        Database Explorer and ER Diagram are expected to reflect the active
        Processing Scope immediately.  Repeated navigation within the same
        PostgreSQL context is cached to avoid unnecessary metadata scans; the
        explicit Refresh buttons always bypass this cache.
        """
        try:
            signature = self._postgres_context_signature()
        except Exception as exc:
            self.connection_var.set(f"PostgreSQL settings invalid: {exc}")
            return

        if page_key == "database":
            if force or self._loaded_context.get("database") != signature or not self.db_tree.get_children():
                self.refresh_database_tree()
        elif page_key == "erd":
            if force or self._loaded_context.get("erd") != signature or not (self.erd_frame.model.get("tables") or []):
                self.load_erd()
        elif page_key == "visual":
            if force or self._loaded_context.get("visual") != signature:
                self.refresh_visual_builder()
                self._loaded_context["visual"] = signature
        elif page_key == "documents":
            if force or self._loaded_context.get("documents") != signature:
                self.refresh_documents()
                self._loaded_context["documents"] = signature
        elif page_key == "embedding":
            if force or self._loaded_context.get("embedding") != signature:
                self.refresh_embedding_status()
                self._loaded_context["embedding"] = signature
        elif page_key == "qdrant_sync":
            if force or self._loaded_context.get("qdrant_sync") != signature:
                self.refresh_qdrant_sync_context()
                self._loaded_context["qdrant_sync"] = signature

    def dashboard_snapshot(self) -> dict[str, Any]:
        """Return a compact operational snapshot for the unified dashboard.

        Important: this method is executed by a background worker.  Tkinter
        variables must never be read from that worker, so the PostgreSQL
        service is rebuilt on the UI thread before this method is submitted.
        """
        service = self.pg_service
        assert service is not None
        info = service.test_connection()
        # Keep the operational dashboard useful during a rolling upgrade.
        # Core v5 counters are read independently from the v6 relation views
        # so a database that has not yet run Initialize / Upgrade Complete
        # Database does not make the entire dashboard fail.
        base_counts_sql = """
        SELECT
            (SELECT count(*) FROM public.documents) AS documents,
            (SELECT count(*) FROM public.specifications) AS specifications,
            (SELECT count(*) FROM public.specification_versions) AS versions,
            (SELECT count(*) FROM public.specification_versions sv
                JOIN public.specifications s ON s.current_version_id = sv.id) AS current_versions,
            (SELECT count(*) FROM public.specification_changes) AS changes,
            (SELECT count(*) FROM public.contents WHERE embedding_status IN ('DONE', 'COMPLETED')) AS embeddings_done,
            (SELECT count(*) FROM public.contents WHERE embedding_status = 'PENDING') AS embeddings_pending,
            (SELECT count(*) FROM public.contents WHERE embedding_status = 'FAILED') AS embeddings_failed
        """
        counts: dict[str, Any] = {
            "documents": 0,
            "specifications": 0,
            "versions": 0,
            "current_versions": 0,
            "changes": 0,
            "relations": 0,
            "unresolved_relations": 0,
            "embeddings_done": 0,
            "embeddings_pending": 0,
            "embeddings_failed": 0,
        }
        try:
            result = service.execute_sql(base_counts_sql, allow_write=False, max_rows=10)
            if result.rows:
                counts.update(result.rows[0])
        except Exception:
            # Very old / partially upgraded schemas should still report the
            # basic tables that are available instead of leaving the UI blank.
            for key, table in (("documents", "documents"), ("specifications", "specifications"), ("versions", "specification_versions")):
                try:
                    counts[key] = service.table_count("public", table)
                except Exception:
                    counts[key] = 0

        try:
            relation_result = service.execute_sql(
                """
                SELECT
                    count(*) FILTER (
                        WHERE is_current_extraction IS TRUE
                          AND is_current_source_version IS TRUE
                    )::INTEGER AS relations,
                    count(*) FILTER (
                        WHERE is_current_extraction IS TRUE
                          AND is_current_source_version IS TRUE
                          AND status = 'UNRESOLVED'
                    )::INTEGER AS unresolved_relations
                FROM public.engineering_relation_catalog
                """,
                allow_write=False,
                max_rows=10,
            )
            if relation_result.rows:
                counts.update(relation_result.rows[0])
        except Exception:
            # Schema v5 database before the v5.2/v6 migration: relation
            # counters intentionally stay zero; Settings will surface the
            # required schema upgrade.
            pass
        return {"connection": info, "counts": counts}

    def shutdown(self) -> None:
        # Threads are daemon threads; this exists so the host can inspect state.
        return

    def _run_async(
        self,
        label: str,
        fn: Callable[[], Any],
        on_success: Callable[[Any], None],
    ) -> None:
        self.status_var.set(label)
        self.host.global_status_var.set(label)

        def worker() -> None:
            thread = threading.current_thread()
            try:
                result = fn()
                self.event_queue.put(AsyncEvent("success", (on_success, result, label)))
            except Exception as exc:
                LOGGER.exception("Background operation failed: %s", label)
                self.event_queue.put(
                    AsyncEvent(
                        "error",
                        (label, type(exc).__name__, str(exc), traceback.format_exc()),
                    )
                )
            finally:
                self.event_queue.put(AsyncEvent("thread_done", thread))

        thread = threading.Thread(target=worker, daemon=True)
        self.active_threads.add(thread)
        thread.start()

    def _poll_events(self) -> None:
        """Drain worker results without allowing one UI callback to kill the pump.

        v5.0.1 could remain forever at "Connecting…" if a success callback
        raised a Tcl/Tk or page-initialization exception: the exception escaped
        this method and the periodic queue poll was never scheduled again.
        The dispatcher is now self-healing and always reschedules itself.
        """
        try:
            while True:
                try:
                    event = self.event_queue.get_nowait()
                except queue.Empty:
                    break

                try:
                    if event.kind == "success":
                        callback, result, label = event.payload
                        callback(result)
                        self.status_var.set(f"{label} - done")
                        self.host.global_status_var.set(f"{label} - done")
                    elif event.kind == "error":
                        label, error_type, message, details = event.payload
                        self.status_var.set(f"{label} - failed")
                        self.host.global_status_var.set(f"{label} - failed")
                        if label.startswith("Refresh database explorer") or label.startswith("Load ER diagram"):
                            self.connection_var.set(f"PostgreSQL error: {message}")
                        LOGGER.error("%s\n%s", message, details)
                        messagebox.showerror(label, f"{error_type}: {message}", parent=self.root)
                    elif event.kind == "rag_sync_progress":
                        self._handle_rag_sync_progress(event.payload)
                    elif event.kind == "rag_sync_preview_success":
                        self._show_qdrant_sync_plan(event.payload)
                        self._set_rag_sync_busy(False)
                    elif event.kind == "rag_sync_success":
                        self._show_qdrant_sync_result(event.payload)
                        self._set_rag_sync_busy(False)
                    elif event.kind == "rag_sync_cancelled":
                        self.rag_sync_status_var.set(str(event.payload or "Sync cancelled."))
                        self.rag_sync_progress_text_var.set("Cancelled")
                        self._set_rag_sync_busy(False)
                        self._refresh_rag_sync_history()
                    elif event.kind == "rag_sync_error":
                        message = str(event.payload or "Unknown SQL → Qdrant sync error")
                        self.rag_sync_status_var.set(f"Failed: {message}")
                        self.rag_sync_progress_text_var.set("Failed")
                        self._set_rag_sync_busy(False)
                        messagebox.showerror("SQL → Qdrant Sync", message, parent=self.root)
                        self._refresh_rag_sync_history()
                    elif event.kind == "thread_done":
                        self.active_threads.discard(event.payload)
                except Exception as exc:
                    # A rendering callback must not permanently stop all later
                    # PostgreSQL/Qdrant responses from reaching the UI.
                    LOGGER.exception("Enterprise UI callback failed")
                    self.status_var.set(f"UI callback failed: {type(exc).__name__}")
                    self.host.global_status_var.set(f"UI callback failed: {type(exc).__name__}")
                    self.connection_var.set(f"Console UI error: {exc}")
                    try:
                        messagebox.showerror(
                            "Console UI Error",
                            f"{type(exc).__name__}: {exc}",
                            parent=self.root,
                        )
                    except Exception:
                        LOGGER.exception("Unable to display Console UI Error dialog")
        finally:
            try:
                self.root.after(120, self._poll_events)
            except tk.TclError:
                # Normal during application shutdown.
                pass

    def _bind_shortcuts(self) -> None:
        self.root.bind("<Control-Return>", lambda _e: self.run_sql())
        self.root.bind("<F5>", self._f5_action)
        self.root.bind("<Control-s>", self._ctrl_s_action)

    def _f5_action(self, _event=None) -> None:
        page = self.host.current_page_var.get()
        if page == "SQL Editor":
            self.run_sql()
        elif page == "Database Explorer":
            if self.current_table:
                self.load_current_table()
            else:
                self.refresh_database_tree()
        elif page == "Document Explorer":
            self.refresh_documents()
        elif page == "Embedding Monitor":
            self.refresh_embedding_status()
        elif page == "Qdrant Browser":
            self.refresh_qdrant()

    def _ctrl_s_action(self, _event=None) -> None:
        if self.host.current_page_var.get() == "SQL Editor":
            self.save_sql_file()

    @staticmethod
    def _clear_tree(tree: tkttk.Treeview) -> None:
        tree.delete(*tree.get_children())

    @staticmethod
    def _display_value(value: Any, max_chars: int = 260) -> str:
        if value is None:
            return "<NULL>"
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)
        text = str(value).replace("\r", " ").replace("\n", " ↵ ")
        return text if len(text) <= max_chars else text[: max_chars - 1] + "…"

    def _populate_table_tree(
        self,
        tree: tkttk.Treeview,
        rows: list[dict[str, Any]],
        columns: list[str],
        *,
        row_map: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        tree.delete(*tree.get_children())
        tree["columns"] = columns
        tree["show"] = "headings"
        for name in columns:
            tree.heading(name, text=name)
            tree.column(name, width=155, minwidth=70, stretch=True)
        if row_map is not None:
            row_map.clear()
        for row in rows:
            iid = tree.insert(
                "",
                END,
                values=[self._display_value(row.get(name)) for name in columns],
            )
            if row_map is not None:
                row_map[iid] = dict(row)

    def _new_table_view(
        self,
        parent: tk.Widget,
        *,
        selectmode: str = "extended",
    ) -> tuple[tkttk.Treeview, tkttk.Scrollbar, tkttk.Scrollbar]:
        frame = ttk.Frame(parent)
        frame.pack(fill=BOTH, expand=True)
        tree = tkttk.Treeview(frame, selectmode=selectmode)
        ybar = tkttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        xbar = tkttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        tree.grid(row=0, column=0, sticky="nsew")
        ybar.grid(row=0, column=1, sticky="ns")
        xbar.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        return tree, ybar, xbar

    def _build_database_tab(self) -> None:
        pane = ttk.Panedwindow(self.db_tab, orient=HORIZONTAL)
        pane.pack(fill=BOTH, expand=True)

        left = ttk.Frame(pane, padding=4)
        right = ttk.Frame(pane, padding=4)
        pane.add(left, weight=1)
        pane.add(right, weight=5)

        left_toolbar = ttk.Frame(left)
        left_toolbar.pack(fill=X, pady=(0, 5))
        ttk.Button(left_toolbar, text="Refresh", command=lambda: self.activate_page("database", force=True), bootstyle="primary").pack(side=LEFT)
        ttk.Button(left_toolbar, text="ERD", command=self._open_selected_schema_erd).pack(side=LEFT, padx=(6, 0))

        ttk.Label(
            left,
            textvariable=self.connection_var,
            bootstyle="secondary",
            wraplength=215,
            justify="left",
        ).pack(fill=X, pady=(0, 6))

        self.db_tree = tkttk.Treeview(left, show="tree")
        self.db_tree.pack(fill=BOTH, expand=True)
        self.db_tree.bind("<<TreeviewSelect>>", self._on_db_tree_select)

        title_line = ttk.Frame(right)
        title_line.pack(fill=X, pady=(0, 5))
        ttk.Label(title_line, textvariable=self.table_title_var, font=("Microsoft YaHei UI", 12, "bold")).pack(side=LEFT)
        ttk.Checkbutton(
            title_line,
            text="Enable table writes",
            variable=self.allow_table_write_var,
            bootstyle="danger-round-toggle",
        ).pack(side=RIGHT)

        toolbar = ttk.Frame(right)
        toolbar.pack(fill=X, pady=(0, 6))
        ttk.Button(toolbar, text="＋ Add", command=self.add_table_row, bootstyle="success").pack(side=LEFT)
        ttk.Button(toolbar, text="✎ Edit", command=self.edit_table_row, bootstyle="primary").pack(side=LEFT, padx=(5, 0))
        ttk.Button(toolbar, text="− Delete", command=self.delete_table_rows, bootstyle="danger").pack(side=LEFT, padx=(5, 10))
        ttk.Button(toolbar, text="⟳ Refresh", command=self.load_current_table).pack(side=LEFT)

        ttk.Label(toolbar, text="Filter").pack(side=LEFT, padx=(12, 0))
        filter_entry = ttk.Entry(toolbar, textvariable=self.table_filter_var, width=24)
        filter_entry.pack(side=LEFT, padx=5)
        filter_entry.bind("<Return>", lambda _e: self.load_current_table(reset_page=True))

        ttk.Label(toolbar, text="Sort").pack(side=LEFT, padx=(8, 0))
        self.table_sort_combo = ttk.Combobox(toolbar, textvariable=self.table_sort_var, state="readonly", width=18)
        self.table_sort_combo.pack(side=LEFT, padx=5)
        ttk.Combobox(
            toolbar,
            textvariable=self.table_sort_dir_var,
            values=["ASC", "DESC"],
            state="readonly",
            width=6,
        ).pack(side=LEFT)
        ttk.Button(toolbar, text="Apply", command=lambda: self.load_current_table(reset_page=True)).pack(side=LEFT, padx=5)
        ttk.Button(toolbar, text="Export CSV", command=self.export_current_table_csv, bootstyle="success-outline").pack(side=RIGHT)

        self.table_notebook = ttk.Notebook(right)
        self.table_notebook.pack(fill=BOTH, expand=True)

        data_page = ttk.Frame(self.table_notebook, padding=4)
        struct_page = ttk.Frame(self.table_notebook, padding=4)
        index_page = ttk.Frame(self.table_notebook, padding=4)
        constraint_page = ttk.Frame(self.table_notebook, padding=4)
        ddl_page = ttk.Frame(self.table_notebook, padding=4)

        self.table_notebook.add(data_page, text=" Data ")
        self.table_notebook.add(struct_page, text=" Structure ")
        self.table_notebook.add(index_page, text=" Indexes ")
        self.table_notebook.add(constraint_page, text=" Constraints ")
        self.table_notebook.add(ddl_page, text=" DDL ")

        self.table_data_tree, _, _ = self._new_table_view(data_page)
        self.table_data_tree.bind("<Double-1>", lambda _e: self.edit_table_row())
        self.table_data_tree.bind("<Button-3>", self._show_table_context_menu)
        self.table_structure_tree, _, _ = self._new_table_view(struct_page)
        self.table_index_tree, _, _ = self._new_table_view(index_page)
        self.table_constraint_tree, _, _ = self._new_table_view(constraint_page)

        ddl_controls = ttk.Frame(ddl_page)
        ddl_controls.pack(fill=X, pady=(0, 5))
        ttk.Button(ddl_controls, text="Refresh DDL", command=self.load_current_table).pack(side=LEFT)
        ttk.Button(ddl_controls, text="Save DDL", command=self.save_ddl_file).pack(side=LEFT, padx=(6, 0))
        self.ddl_text = tk.Text(ddl_page, wrap="none", font=("Cascadia Mono", 10), undo=False)
        self.ddl_text.pack(fill=BOTH, expand=True)

        pager = ttk.Frame(right)
        pager.pack(fill=X, pady=(6, 0))
        ttk.Button(pager, text="Previous", command=self.previous_table_page).pack(side=LEFT)
        ttk.Label(pager, textvariable=self.table_page_var).pack(side=LEFT, padx=10)
        ttk.Button(pager, text="Next", command=self.next_table_page).pack(side=LEFT)
        ttk.Label(
            pager,
            text="Edit/Delete require a primary key. Views are read-only.",
            bootstyle="secondary",
        ).pack(side=RIGHT)

    def refresh_database_tree(self) -> None:
        self._rebuild_services()
        service = self.pg_service
        assert service is not None
        signature = self._postgres_context_signature()
        self.connection_var.set(
            f"Connecting: {self.profile.postgres.host}:{self.profile.postgres.port}/{self.profile.postgres.database} …"
        )

        def job():
            snapshot = service.metadata_snapshot()
            return {
                "connection": snapshot["connection"],
                "schemas": snapshot["schemas"],
                "signature": signature,
            }

        self._run_async("Refresh database explorer", job, self._show_database_tree)

    def _show_database_tree(self, payload: dict[str, Any]) -> None:
        data = payload.get("schemas") or {}
        info = payload.get("connection") or {}
        signature = payload.get("signature") or self._postgres_context_signature()
        self._clear_tree(self.db_tree)
        database_name = str(info.get("database") or self.profile.postgres.database)
        db_root = self.db_tree.insert("", END, text=database_name, open=True, values=("database",))
        for schema, objects in data.items():
            schema_id = self.db_tree.insert(db_root, END, text=schema, values=("schema", schema), open=schema == "public")
            groups: dict[str, str] = {}
            for group in ("TABLE", "VIEW", "MATERIALIZED VIEW", "SEQUENCE"):
                matching = [o for o in objects if o["object_type"] == group]
                if matching:
                    groups[group] = self.db_tree.insert(schema_id, END, text=f"{group.title()}s ({len(matching)})", values=("group", schema, group), open=group == "TABLE")
                    for item in matching:
                        self.db_tree.insert(
                            groups[group],
                            END,
                            text=item["object_name"],
                            values=("object", schema, item["object_name"], item["object_type"]),
                        )
        schemas = list(data)
        self.visual_schema_combo["values"] = schemas
        self.erd_schema_combo["values"] = schemas
        if schemas and self.visual_schema_var.get() not in schemas:
            self.visual_schema_var.set(schemas[0])
        if schemas and self.erd_schema_var.get() not in schemas:
            self.erd_schema_var.set(schemas[0])
        # Do not perform more synchronous PostgreSQL work inside this Tk
        # success callback. Visual Query will load lazily when its page opens.
        # This keeps Database Explorer responsive even on a remote/slow DB.
        self._loaded_context["database"] = signature
        self._loaded_context.pop("visual", None)
        object_count = sum(len(items) for items in data.values())
        self.connection_var.set(
            f"Connected: {database_name} · {len(data)} schema(s) · {object_count} object(s)"
        )

    def _on_db_tree_select(self, _event=None) -> None:
        selection = self.db_tree.selection()
        if not selection:
            return
        values = self.db_tree.item(selection[0]).get("values") or []
        if len(values) >= 4 and values[0] == "object":
            schema, name, object_type = str(values[1]), str(values[2]), str(values[3])
            if object_type in {"TABLE", "VIEW", "MATERIALIZED VIEW"}:
                self.current_schema = schema
                self.current_table = name
                self.current_object_type = object_type
                self.load_current_table(reset_page=True)
        elif len(values) >= 2 and values[0] == "schema":
            self.current_schema = str(values[1])

    def load_current_table(self, reset_page: bool = False) -> None:
        if not self.current_table:
            return
        if reset_page:
            self.current_table_page = 0

        self._rebuild_services()
        service = self.pg_service
        assert service is not None
        schema, table = self.current_schema, self.current_table
        page_size = self.profile.page_size
        offset = self.current_table_page * page_size
        filter_text = self.table_filter_var.get()
        sort_column = self.table_sort_var.get()
        sort_direction = self.table_sort_dir_var.get()

        def job():
            columns = service.table_columns(schema, table)
            return {
                "data": service.fetch_table_rows(
                    schema,
                    table,
                    limit=page_size,
                    offset=offset,
                    filter_text=filter_text,
                    sort_column=sort_column,
                    sort_direction=sort_direction,
                ),
                "structure": columns,
                "pk": service.primary_key_columns(schema, table),
                "indexes": service.table_indexes(schema, table),
                "constraints": service.table_constraints(schema, table),
                "ddl": service.table_ddl(schema, table) if self.current_object_type == "TABLE" else f"-- DDL generation is available for base tables.\n-- Selected object: {schema}.{table} ({self.current_object_type})",
            }

        self._run_async(f"Load {schema}.{table}", job, self._show_table_bundle)

    def _show_table_bundle(self, bundle: dict[str, Any]) -> None:
        result: QueryResult = bundle["data"]
        self.current_table_result = result
        self.current_table_columns = bundle["structure"]
        self.current_table_pk = bundle["pk"]
        self.table_title_var.set(
            f"{self.current_schema}.{self.current_table}  ·  {self.current_object_type}  ·  PK: {', '.join(self.current_table_pk) if self.current_table_pk else '<none>'}"
        )
        self._populate_table_tree(
            self.table_data_tree,
            result.rows,
            result.columns,
            row_map=self.table_row_map,
        )
        self.table_sort_combo["values"] = [""] + result.columns

        for tree, rows in (
            (self.table_structure_tree, bundle["structure"]),
            (self.table_index_tree, bundle["indexes"]),
            (self.table_constraint_tree, bundle["constraints"]),
        ):
            self._populate_table_tree(tree, rows, list(rows[0].keys()) if rows else [])

        self.ddl_text.configure(state="normal")
        self.ddl_text.delete("1.0", END)
        self.ddl_text.insert("1.0", bundle["ddl"])
        self.ddl_text.configure(state="disabled")

        total = result.total_rows if result.total_rows is not None else "?"
        first = self.current_table_page * self.profile.page_size + 1 if result.rowcount else 0
        last = self.current_table_page * self.profile.page_size + result.rowcount
        self.table_page_var.set(
            f"Page {self.current_table_page + 1} · rows {first}-{last} / {total} · {result.elapsed_ms} ms"
        )

    def previous_table_page(self) -> None:
        if self.current_table_page > 0:
            self.current_table_page -= 1
            self.load_current_table()

    def next_table_page(self) -> None:
        if self.current_table_result is None:
            return
        total = self.current_table_result.total_rows or 0
        if (self.current_table_page + 1) * self.profile.page_size < total:
            self.current_table_page += 1
            self.load_current_table()

    def _table_writes_allowed(self, action: str) -> bool:
        if self.current_object_type != "TABLE":
            messagebox.showwarning(action, "Only base tables can be modified.", parent=self.root)
            return False
        if not self.allow_table_write_var.get():
            messagebox.showwarning(
                action,
                "Table write mode is disabled. Enable 'Enable table writes' first.",
                parent=self.root,
            )
            return False
        return True

    def add_table_row(self) -> None:
        if not self.current_table or not self._table_writes_allowed("Add Row"):
            return
        dialog = RecordEditorDialog(
            self.root,
            title=f"Add row · {self.current_schema}.{self.current_table}",
            columns=self.current_table_columns,
            initial=None,
        )
        self.root.wait_window(dialog)
        if dialog.result is None:
            return
        values = dict(dialog.result)
        # Allow database defaults/identity to operate when left blank.
        for col in self.current_table_columns:
            name = col["column_name"]
            if name not in values:
                continue
            if col.get("is_identity") == "YES" or col.get("is_generated") not in {None, "NEVER", ""}:
                values.pop(name, None)
            elif values[name] == "" and col.get("column_default") is not None:
                values.pop(name, None)
        if not messagebox.askyesno(
            "Confirm INSERT",
            f"Insert one row into {self.current_schema}.{self.current_table}?",
            parent=self.root,
        ):
            return
        service = self.pg_service
        assert service is not None
        self._run_async(
            "Insert row",
            lambda: service.insert_row(self.current_schema, self.current_table, values),
            lambda _row: self.load_current_table(reset_page=False),
        )

    def _selected_table_rows(self) -> list[dict[str, Any]]:
        return [self.table_row_map[iid] for iid in self.table_data_tree.selection() if iid in self.table_row_map]

    def edit_table_row(self) -> None:
        if not self.current_table or not self._table_writes_allowed("Edit Row"):
            return
        rows = self._selected_table_rows()
        if len(rows) != 1:
            messagebox.showwarning("Edit Row", "Select exactly one row.", parent=self.root)
            return
        if not self.current_table_pk:
            messagebox.showwarning("Edit Row", "Safe editing requires a primary key.", parent=self.root)
            return
        initial = rows[0]
        dialog = RecordEditorDialog(
            self.root,
            title=f"Edit row · {self.current_schema}.{self.current_table}",
            columns=self.current_table_columns,
            initial=initial,
            readonly_columns=set(self.current_table_pk),
        )
        self.root.wait_window(dialog)
        if dialog.result is None:
            return

        changes: dict[str, Any] = {}
        for name, value in dialog.result.items():
            if name in self.current_table_pk:
                continue
            original = initial.get(name)
            if original is None and value is None:
                continue
            if original is not None and value is not None and str(original) == str(value):
                continue
            changes[name] = value
        if not changes:
            self.status_var.set("No row changes detected")
            return
        pk_values = {name: initial.get(name) for name in self.current_table_pk}
        if not messagebox.askyesno(
            "Confirm UPDATE",
            f"Update {self.current_schema}.{self.current_table}\nPK: {pk_values}\nChanged columns: {', '.join(changes)}?",
            parent=self.root,
        ):
            return
        service = self.pg_service
        assert service is not None
        self._run_async(
            "Update row",
            lambda: service.update_row(
                self.current_schema,
                self.current_table,
                pk_values=pk_values,
                changes=changes,
            ),
            lambda _row: self.load_current_table(),
        )

    def delete_table_rows(self) -> None:
        if not self.current_table or not self._table_writes_allowed("Delete Rows"):
            return
        rows = self._selected_table_rows()
        if not rows:
            messagebox.showwarning("Delete Rows", "Select one or more rows.", parent=self.root)
            return
        if not self.current_table_pk:
            messagebox.showwarning("Delete Rows", "Safe deletion requires a primary key.", parent=self.root)
            return
        pk_rows = [{name: row.get(name) for name in self.current_table_pk} for row in rows]
        preview = "\n".join(str(item) for item in pk_rows[:8])
        if len(pk_rows) > 8:
            preview += f"\n... +{len(pk_rows) - 8} rows"
        if not messagebox.askyesno(
            "Confirm DELETE",
            f"Delete {len(pk_rows)} row(s) from {self.current_schema}.{self.current_table}?\n\nPrimary keys:\n{preview}",
            parent=self.root,
        ):
            return
        service = self.pg_service
        assert service is not None
        self._run_async(
            "Delete rows",
            lambda: service.delete_rows(self.current_schema, self.current_table, pk_rows=pk_rows),
            lambda count: self._after_delete(count),
        )

    def _after_delete(self, count: int) -> None:
        self.status_var.set(f"Deleted {count} row(s)")
        self.load_current_table()

    def _show_table_context_menu(self, event) -> None:
        iid = self.table_data_tree.identify_row(event.y)
        if iid:
            if iid not in self.table_data_tree.selection():
                self.table_data_tree.selection_set(iid)
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="Edit selected row", command=self.edit_table_row)
        menu.add_command(label="Delete selected row(s)", command=self.delete_table_rows)
        menu.add_separator()
        menu.add_command(label="Copy selected row", command=self._copy_selected_row)
        menu.tk_popup(event.x_root, event.y_root)

    def _copy_selected_row(self) -> None:
        rows = self._selected_table_rows()
        if not rows:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(json.dumps(rows[0], ensure_ascii=False, default=str, indent=2))

    def export_current_table_csv(self) -> None:
        if not self.current_table_result:
            messagebox.showwarning("Export", "No table data is loaded.", parent=self.root)
            return
        path = filedialog.asksaveasfilename(
            parent=self.root,
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv")],
            initialfile=f"{self.current_table or 'table'}.csv",
        )
        if path:
            PostgresService.write_csv(Path(path), self.current_table_result)

    def save_ddl_file(self) -> None:
        if not self.current_table:
            return
        path = filedialog.asksaveasfilename(
            parent=self.root,
            defaultextension=".sql",
            filetypes=[("SQL", "*.sql")],
            initialfile=f"{self.current_table}_ddl.sql",
        )
        if path:
            Path(path).write_text(self.ddl_text.get("1.0", END), encoding="utf-8")

    def _open_selected_schema_erd(self) -> None:
        if self.current_schema:
            self.erd_schema_var.set(self.current_schema)
        self.host.navigate("erd")
        self.load_erd()

    def _build_sql_tab(self) -> None:
        controls = ttk.Frame(self.sql_tab)
        controls.pack(fill=X, pady=(0, 6))
        ttk.Button(controls, text="▶ Run", command=self.run_sql, bootstyle="primary").pack(side=LEFT)
        ttk.Button(controls, text="Run Selection", command=lambda: self.run_sql(selection_only=True)).pack(side=LEFT, padx=(5, 0))
        ttk.Button(controls, text="EXPLAIN", command=lambda: self.explain_sql(False)).pack(side=LEFT, padx=(12, 0))
        ttk.Button(controls, text="EXPLAIN ANALYZE", command=lambda: self.explain_sql(True), bootstyle="warning-outline").pack(side=LEFT, padx=(5, 0))
        ttk.Button(controls, text="Format", command=self.format_sql).pack(side=LEFT, padx=(12, 0))
        ttk.Button(controls, text="Open", command=self.open_sql_file).pack(side=LEFT, padx=(5, 0))
        ttk.Button(controls, text="Save", command=self.save_sql_file).pack(side=LEFT, padx=(5, 0))
        ttk.Checkbutton(
            controls,
            text="Enable write SQL",
            variable=self.allow_sql_write_var,
            bootstyle="danger-round-toggle",
        ).pack(side=LEFT, padx=16)
        ttk.Button(controls, text="Chart", command=self.chart_sql_result, bootstyle="info-outline").pack(side=RIGHT)
        ttk.Button(controls, text="Export Result", command=self.export_sql_result, bootstyle="success-outline").pack(side=RIGHT, padx=(0, 6))

        split = ttk.Panedwindow(self.sql_tab, orient=tk.VERTICAL)
        split.pack(fill=BOTH, expand=True)

        editor_frame = ttk.Frame(split)
        result_frame = ttk.Frame(split)
        split.add(editor_frame, weight=2)
        split.add(result_frame, weight=3)

        self.sql_editor = tk.Text(editor_frame, wrap="none", font=("Cascadia Mono", 10), undo=True)
        self.sql_editor.pack(fill=BOTH, expand=True)
        self.sql_editor.insert(
            "1.0",
            "SELECT table_schema, table_name\nFROM information_schema.tables\nWHERE table_schema = 'public'\nORDER BY table_name;",
        )

        status = ttk.Frame(result_frame)
        status.pack(fill=X, pady=(0, 4))
        ttk.Label(status, textvariable=self.sql_status_var).pack(side=LEFT)
        ttk.Label(status, text="Ctrl+Enter / F5 = Run · Ctrl+S = Save", bootstyle="secondary").pack(side=RIGHT)
        self.sql_result_tree, _, _ = self._new_table_view(result_frame)

    def _sql_text(self, selection_only: bool = False) -> str:
        if selection_only:
            try:
                return self.sql_editor.get("sel.first", "sel.last").strip()
            except tk.TclError:
                pass
        return self.sql_editor.get("1.0", END).strip()

    def run_sql(self, selection_only: bool = False) -> None:
        statement = self._sql_text(selection_only)
        if not statement:
            return
        self._rebuild_services()
        service = self.pg_service
        assert service is not None
        allow_write = self.allow_sql_write_var.get()
        if not service.is_read_only_sql(statement):
            if not allow_write:
                messagebox.showwarning(
                    "SQL Write Blocked",
                    "This statement is not read-only. Enable 'Enable write SQL' first.",
                    parent=self.root,
                )
                return
            if not messagebox.askyesno(
                "Confirm SQL Write",
                "This SQL may modify database data or schema. Execute it?",
                parent=self.root,
            ):
                return
        if not self.sql_history or self.sql_history[-1] != statement:
            self.sql_history.append(statement)
            self.sql_history = self.sql_history[-100:]
        self._run_async(
            "Execute SQL",
            lambda: service.execute_sql(statement, allow_write=allow_write),
            self._show_sql_result,
        )

    def _show_sql_result(self, result: QueryResult) -> None:
        self.current_sql_result = result
        self._populate_table_tree(self.sql_result_tree, result.rows, result.columns)
        self.sql_status_var.set(f"{result.command} · {result.rowcount} row(s) · {result.elapsed_ms} ms")
        if result.command not in {"SELECT", "WITH", "SHOW", "EXPLAIN", "VALUES", "TABLE"}:
            messagebox.showinfo(
                "SQL",
                f"{result.command} completed. Affected rows: {result.rowcount}",
                parent=self.root,
            )

    def explain_sql(self, analyze: bool) -> None:
        statement = self._sql_text(selection_only=True) or self._sql_text()
        if not statement:
            return
        if analyze and not messagebox.askyesno(
            "EXPLAIN ANALYZE",
            "EXPLAIN ANALYZE executes the SELECT query. Continue?",
            parent=self.root,
        ):
            return
        self._rebuild_services()
        service = self.pg_service
        assert service is not None
        self._run_async(
            "EXPLAIN ANALYZE" if analyze else "EXPLAIN",
            lambda: service.explain_json(statement, analyze=analyze),
            lambda plan: ExplainPlanDialog(self.root, plan),
        )

    def format_sql(self) -> None:
        if sqlparse is None:
            messagebox.showwarning("Format SQL", "sqlparse is not installed.", parent=self.root)
            return
        statement = self._sql_text()
        formatted = sqlparse.format(statement, reindent=True, keyword_case="upper")
        self.sql_editor.delete("1.0", END)
        self.sql_editor.insert("1.0", formatted)

    def open_sql_file(self) -> None:
        path = filedialog.askopenfilename(parent=self.root, filetypes=[("SQL", "*.sql"), ("Text", "*.txt"), ("All", "*.*")])
        if path:
            self.sql_editor.delete("1.0", END)
            self.sql_editor.insert("1.0", Path(path).read_text(encoding="utf-8"))

    def save_sql_file(self) -> None:
        path = filedialog.asksaveasfilename(parent=self.root, defaultextension=".sql", filetypes=[("SQL", "*.sql")])
        if path:
            Path(path).write_text(self.sql_editor.get("1.0", END).rstrip() + "\n", encoding="utf-8")

    def chart_sql_result(self) -> None:
        if not self.current_sql_result or not self.current_sql_result.rows:
            messagebox.showwarning("Chart", "No SQL result is available.", parent=self.root)
            return
        ChartDialog(self.root, self.current_sql_result)

    def export_sql_result(self) -> None:
        if not self.current_sql_result:
            messagebox.showwarning("Export", "No SQL result is available.", parent=self.root)
            return
        path = filedialog.asksaveasfilename(parent=self.root, defaultextension=".csv", filetypes=[("CSV", "*.csv")], initialfile="query_result.csv")
        if path:
            PostgresService.write_csv(Path(path), self.current_sql_result)

    def _build_visual_query_tab(self) -> None:
        split = ttk.Panedwindow(self.visual_tab, orient=HORIZONTAL)
        split.pack(fill=BOTH, expand=True)
        controls = ttk.Frame(split, padding=5)
        workspace = ttk.Frame(split, padding=5)
        split.add(controls, weight=2)
        split.add(workspace, weight=5)

        row = 0
        ttk.Label(controls, text="Schema", font=("Microsoft YaHei UI", 10, "bold")).grid(row=row, column=0, sticky="w", pady=4)
        self.visual_schema_combo = ttk.Combobox(controls, textvariable=self.visual_schema_var, state="readonly")
        self.visual_schema_combo.grid(row=row, column=1, sticky="ew", pady=4)
        self.visual_schema_combo.bind("<<ComboboxSelected>>", lambda _e: self.refresh_visual_builder())
        row += 1

        ttk.Label(controls, text="Base table").grid(row=row, column=0, sticky="w", pady=4)
        self.visual_base_combo = ttk.Combobox(controls, textvariable=self.visual_base_var, state="readonly")
        self.visual_base_combo.grid(row=row, column=1, sticky="ew", pady=4)
        self.visual_base_combo.bind("<<ComboboxSelected>>", lambda _e: self._visual_table_changed())
        row += 1

        ttk.Label(controls, text="Join type").grid(row=row, column=0, sticky="w", pady=4)
        ttk.Combobox(controls, textvariable=self.visual_join_type_var, values=["INNER JOIN", "LEFT JOIN", "RIGHT JOIN", "FULL JOIN"], state="readonly").grid(row=row, column=1, sticky="ew", pady=4)
        row += 1

        ttk.Label(controls, text="Join table").grid(row=row, column=0, sticky="w", pady=4)
        self.visual_join_combo = ttk.Combobox(controls, textvariable=self.visual_join_var, state="readonly")
        self.visual_join_combo.grid(row=row, column=1, sticky="ew", pady=4)
        self.visual_join_combo.bind("<<ComboboxSelected>>", lambda _e: self._visual_table_changed())
        row += 1

        ttk.Label(controls, text="Base join column").grid(row=row, column=0, sticky="w", pady=4)
        self.visual_left_col_combo = ttk.Combobox(controls, textvariable=self.visual_left_col_var, state="readonly")
        self.visual_left_col_combo.grid(row=row, column=1, sticky="ew", pady=4)
        self.visual_left_col_combo.bind("<<ComboboxSelected>>", lambda _e: self._draw_visual_model())
        row += 1

        ttk.Label(controls, text="Join column").grid(row=row, column=0, sticky="w", pady=4)
        self.visual_right_col_combo = ttk.Combobox(controls, textvariable=self.visual_right_col_var, state="readonly")
        self.visual_right_col_combo.grid(row=row, column=1, sticky="ew", pady=4)
        self.visual_right_col_combo.bind("<<ComboboxSelected>>", lambda _e: self._draw_visual_model())
        row += 1

        ttk.Label(controls, text="SELECT columns").grid(row=row, column=0, columnspan=2, sticky="w", pady=(10, 3))
        row += 1
        self.visual_columns_list = tk.Listbox(controls, selectmode="extended", height=10, exportselection=False)
        self.visual_columns_list.grid(row=row, column=0, columnspan=2, sticky="nsew")
        row += 1

        ttk.Label(controls, text="WHERE expression").grid(row=row, column=0, sticky="w", pady=(9, 4))
        ttk.Entry(controls, textvariable=self.visual_where_var).grid(row=row, column=1, sticky="ew", pady=(9, 4))
        row += 1
        ttk.Label(controls, text="ORDER BY").grid(row=row, column=0, sticky="w", pady=4)
        self.visual_order_combo = ttk.Combobox(controls, textvariable=self.visual_order_var, state="readonly")
        self.visual_order_combo.grid(row=row, column=1, sticky="ew", pady=4)
        row += 1
        ttk.Label(controls, text="Direction").grid(row=row, column=0, sticky="w", pady=4)
        ttk.Combobox(controls, textvariable=self.visual_order_dir_var, values=["ASC", "DESC"], state="readonly").grid(row=row, column=1, sticky="ew", pady=4)
        row += 1
        ttk.Label(controls, text="LIMIT").grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(controls, textvariable=self.visual_limit_var).grid(row=row, column=1, sticky="ew", pady=4)
        row += 1

        actions = ttk.Frame(controls)
        actions.grid(row=row, column=0, columnspan=2, sticky="ew", pady=10)
        ttk.Button(actions, text="Generate SQL", command=self.generate_visual_sql, bootstyle="primary").pack(side=LEFT)
        ttk.Button(actions, text="Run", command=self.run_visual_sql, bootstyle="success").pack(side=LEFT, padx=(6, 0))
        ttk.Button(actions, text="Chart", command=self.chart_visual_result).pack(side=LEFT, padx=(6, 0))
        ttk.Button(actions, text="Open in SQL Editor", command=self.visual_to_sql_editor).pack(side=RIGHT)
        controls.columnconfigure(1, weight=1)
        controls.rowconfigure(7, weight=1)

        self.visual_canvas = tk.Canvas(workspace, height=250, background="#fbfbfc", highlightthickness=1, highlightbackground="#d8dde3")
        self.visual_canvas.pack(fill=X, pady=(0, 6))
        ttk.Label(workspace, text="Generated SQL", font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")
        self.visual_sql_text = tk.Text(workspace, height=9, wrap="none", font=("Cascadia Mono", 10))
        self.visual_sql_text.pack(fill=X, pady=(3, 6))
        ttk.Label(workspace, text="Result", font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")
        self.visual_result_tree, _, _ = self._new_table_view(workspace)

    def refresh_visual_builder(self) -> None:
        schema = self.visual_schema_var.get().strip()
        if not schema:
            return
        self._rebuild_services()
        service = self.pg_service
        assert service is not None
        try:
            tables = [item["table_name"] for item in service.list_tables(schema) if item["table_type"] == "TABLE"]
        except Exception:
            return
        self.visual_base_combo["values"] = tables
        self.visual_join_combo["values"] = [""] + tables
        if tables and self.visual_base_var.get() not in tables:
            self.visual_base_var.set(tables[0])
        if self.visual_join_var.get() not in tables:
            self.visual_join_var.set("")
        self._visual_table_changed()

    def _visual_table_changed(self) -> None:
        schema = self.visual_schema_var.get()
        base = self.visual_base_var.get()
        join = self.visual_join_var.get()
        if not base:
            return
        self._rebuild_services()
        service = self.pg_service
        assert service is not None
        try:
            base_cols = [c["column_name"] for c in service.table_columns(schema, base)]
            join_cols = [c["column_name"] for c in service.table_columns(schema, join)] if join else []
        except Exception as exc:
            self.status_var.set(f"Visual Query metadata failed: {exc}")
            return
        self.visual_left_col_combo["values"] = base_cols
        self.visual_right_col_combo["values"] = join_cols
        if base_cols and self.visual_left_col_var.get() not in base_cols:
            self.visual_left_col_var.set(base_cols[0])
        if join_cols and self.visual_right_col_var.get() not in join_cols:
            common = next((c for c in base_cols if c in join_cols), join_cols[0])
            self.visual_right_col_var.set(common)
            if common in base_cols:
                self.visual_left_col_var.set(common)
        self.visual_columns_list.delete(0, END)
        options = [f"t1.{c}" for c in base_cols] + [f"t2.{c}" for c in join_cols]
        for item in options:
            self.visual_columns_list.insert(END, item)
        self.visual_order_combo["values"] = [""] + options
        self._draw_visual_model()

    def _draw_visual_model(self) -> None:
        c = self.visual_canvas
        c.delete("all")
        base, join = self.visual_base_var.get(), self.visual_join_var.get()
        if not base:
            return
        c.create_rectangle(50, 55, 330, 185, fill="white", outline="#798794")
        c.create_rectangle(50, 55, 330, 88, fill="#e9eef4", outline="#798794")
        c.create_text(65, 72, anchor="w", text=f"t1 · {base}", font=("Microsoft YaHei UI", 10, "bold"))
        c.create_text(65, 112, anchor="w", text=f"Join key: {self.visual_left_col_var.get() or '-'}")
        if join:
            c.create_rectangle(480, 55, 760, 185, fill="white", outline="#798794")
            c.create_rectangle(480, 55, 760, 88, fill="#e9eef4", outline="#798794")
            c.create_text(495, 72, anchor="w", text=f"t2 · {join}", font=("Microsoft YaHei UI", 10, "bold"))
            c.create_text(495, 112, anchor="w", text=f"Join key: {self.visual_right_col_var.get() or '-'}")
            c.create_line(330, 120, 480, 120, arrow=tk.LAST, fill="#6e7d8c", width=2)
            c.create_text(405, 103, text=self.visual_join_type_var.get(), fill="#555")

    @staticmethod
    def _qident(name: str) -> str:
        return '"' + name.replace('"', '""') + '"'

    def generate_visual_sql(self) -> str:
        schema, base, join = self.visual_schema_var.get(), self.visual_base_var.get(), self.visual_join_var.get()
        if not schema or not base:
            messagebox.showwarning("Visual Query", "Select a schema and base table.", parent=self.root)
            return ""
        selected = [self.visual_columns_list.get(i) for i in self.visual_columns_list.curselection()]
        select_sql = ",\n    ".join(selected) if selected else "t1.*"
        lines = [f"SELECT\n    {select_sql}", f"FROM {self._qident(schema)}.{self._qident(base)} AS t1"]
        if join:
            left = self.visual_left_col_var.get()
            right = self.visual_right_col_var.get()
            if not left or not right:
                messagebox.showwarning("Visual Query", "Choose both join columns.", parent=self.root)
                return ""
            lines.append(
                f"{self.visual_join_type_var.get()} {self._qident(schema)}.{self._qident(join)} AS t2\n"
                f"    ON t1.{self._qident(left)} = t2.{self._qident(right)}"
            )
        where = self.visual_where_var.get().strip()
        if where:
            lines.append(f"WHERE {where}")
        order = self.visual_order_var.get().strip()
        if order:
            alias, _, column = order.partition(".")
            if alias in {"t1", "t2"} and column:
                lines.append(f"ORDER BY {alias}.{self._qident(column)} {self.visual_order_dir_var.get()}")
        try:
            limit = max(1, min(int(self.visual_limit_var.get()), 10000))
        except ValueError:
            limit = 200
            self.visual_limit_var.set("200")
        lines.append(f"LIMIT {limit};")
        statement = "\n".join(lines)
        self.visual_sql_text.delete("1.0", END)
        self.visual_sql_text.insert("1.0", statement)
        return statement

    def run_visual_sql(self) -> None:
        statement = self.generate_visual_sql()
        if not statement:
            return
        self._rebuild_services()
        service = self.pg_service
        assert service is not None
        self._run_async(
            "Run visual query",
            lambda: service.execute_sql(statement, allow_write=False),
            self._show_visual_result,
        )

    def _show_visual_result(self, result: QueryResult) -> None:
        self.current_visual_result = result
        self._populate_table_tree(self.visual_result_tree, result.rows, result.columns)

    def visual_to_sql_editor(self) -> None:
        statement = self.generate_visual_sql()
        if statement:
            self.sql_editor.delete("1.0", END)
            self.sql_editor.insert("1.0", statement)
            self.host.navigate("sql")

    def chart_visual_result(self) -> None:
        if self.current_visual_result and self.current_visual_result.rows:
            ChartDialog(self.root, self.current_visual_result)
        else:
            messagebox.showwarning("Chart", "Run the visual query first.", parent=self.root)

    def _build_erd_tab(self) -> None:
        toolbar = ttk.Frame(self.erd_tab)
        toolbar.pack(fill=X, pady=(0, 6))
        ttk.Label(toolbar, text="Schema").pack(side=LEFT)
        self.erd_schema_combo = ttk.Combobox(toolbar, textvariable=self.erd_schema_var, state="readonly", width=24)
        self.erd_schema_combo.pack(side=LEFT, padx=6)
        ttk.Button(toolbar, text="Load / Refresh ERD", command=self.load_erd, bootstyle="primary").pack(side=LEFT)
        ttk.Label(toolbar, text="Double-click a table to open its Data tab.", bootstyle="secondary").pack(side=RIGHT)
        self.erd_frame = ERDiagramFrame(self.erd_tab, on_open_table=self._erd_open_table)
        self.erd_frame.pack(fill=BOTH, expand=True)

    def load_erd(self) -> None:
        schema = self.erd_schema_var.get().strip() or "public"
        self.erd_schema_var.set(schema)
        self._rebuild_services()
        service = self.pg_service
        assert service is not None
        signature = self._postgres_context_signature()

        def job():
            info = service.test_connection()
            schemas = service.list_schemas()
            chosen = schema if schema in schemas else ("public" if "public" in schemas else (schemas[0] if schemas else schema))
            return {
                "connection": info,
                "schemas": schemas,
                "schema": chosen,
                "model": service.schema_erd_model(chosen),
                "signature": signature,
            }

        self._run_async(
            f"Load ER diagram: {schema}",
            job,
            self._show_erd_model,
        )

    def _show_erd_model(self, payload: dict[str, Any]) -> None:
        schemas = payload.get("schemas") or []
        schema = str(payload.get("schema") or "public")
        self.erd_schema_combo["values"] = schemas
        self.erd_schema_var.set(schema)
        self.erd_frame.set_model(payload.get("model") or {"tables": [], "foreign_keys": []})
        self._loaded_context["erd"] = payload.get("signature") or self._postgres_context_signature()
        info = payload.get("connection") or {}
        database_name = str(info.get("database") or self.profile.postgres.database)
        table_count = len((payload.get("model") or {}).get("tables") or [])
        self.connection_var.set(f"Connected: {database_name} · ERD {schema} · {table_count} table(s)")

    def _erd_open_table(self, schema: str, table: str) -> None:
        self.current_schema = schema
        self.current_table = table
        self.current_object_type = "TABLE"
        self.host.navigate("database")
        self.load_current_table(reset_page=True)

    def _build_document_tab(self) -> None:
        pane = ttk.Panedwindow(self.doc_tab, orient=HORIZONTAL)
        pane.pack(fill=BOTH, expand=True)
        left = ttk.Frame(pane, padding=5)
        right = ttk.Frame(pane, padding=5)
        pane.add(left, weight=3)
        pane.add(right, weight=2)

        toolbar = ttk.Frame(left)
        toolbar.pack(fill=X, pady=(0, 5))
        ttk.Button(toolbar, text="Refresh Documents", command=self.refresh_documents, bootstyle="primary").pack(side=LEFT)
        ttk.Label(toolbar, textvariable=self.doc_hierarchy_var, bootstyle="secondary").pack(side=RIGHT)

        self.doc_tree = tkttk.Treeview(left, show="tree")
        self.doc_tree.pack(fill=BOTH, expand=True)
        self.doc_tree.bind("<<TreeviewOpen>>", self._on_doc_tree_open)
        self.doc_tree.bind("<<TreeviewSelect>>", self._on_doc_tree_select)

        ttk.Label(right, textvariable=self.chunk_meta_var, font=("Microsoft YaHei UI", 10, "bold")).pack(fill=X, pady=(0, 5))
        self.chunk_text = tk.Text(right, wrap="word", font=("Cascadia Mono", 9))
        self.chunk_text.pack(fill=BOTH, expand=True)
        self.chunk_text.configure(state="disabled")

    def refresh_documents(self) -> None:
        self._rebuild_services()
        service = self.pg_service
        assert service is not None
        try:
            project = ProjectRegistry.resolve(self.host.project_var.get())
        except ProjectRegistryError as exc:
            messagebox.showerror("Load documents", str(exc), parent=self.root)
            return
        self.doc_hierarchy_var.set(
            "QA/BUG Record → Semantic Section → Chunk"
            if project.code == "QABUG"
            else "Specification → Version → Document → Chapter → Section → Chunk"
        )

        def job():
            if project.code == "QABUG":
                # QA/BUG intentionally does not own specification tables such
                # as documents/chapters/sections/contents. The shared browser
                # must read the dedicated authoritative QA/BUG schema instead.
                ready = service.execute_sql(
                    "SELECT to_regclass('public.qabug_records') AS records, "
                    "to_regclass('public.qabug_chunks') AS chunks",
                    max_rows=1,
                )
                row = ready.rows[0] if ready.rows else {}
                if not row.get("records") or not row.get("chunks"):
                    raise RuntimeError(
                        "QA/BUG database is not initialized. Open Settings and run "
                        "'QA/BUG 一键建库 / 建表 / Qdrant 索引' first."
                    )
                result = service.execute_sql(
                    """
                    SELECT r.id AS record_id, r.record_type, r.record_key, r.issue_key,
                           r.issue_id, r.issue_type, r.status_norm, r.priority_norm,
                           r.component, r.summary, r.source_row_number, sf.source_file_name,
                           COUNT(c.id) FILTER (WHERE c.is_current) AS chunk_count
                    FROM public.qabug_records r
                    LEFT JOIN public.qabug_source_files sf ON sf.id=r.source_file_id
                    LEFT JOIN public.qabug_chunks c ON c.record_id=r.id
                    WHERE r.is_current AND r.is_queryable
                    GROUP BY r.id, r.record_type, r.record_key, r.issue_key, r.issue_id,
                             r.issue_type, r.status_norm, r.priority_norm, r.component,
                             r.summary, r.source_row_number, sf.source_file_name
                    ORDER BY r.record_type, COALESCE(r.issue_key, r.record_key)
                    LIMIT 5000
                    """,
                    max_rows=5000,
                )
                return {"versioned": [], "generic": [], "qabug": list(result.rows), "domain": "QABUG"}

            if not service.has_rag_schema():
                raise RuntimeError("Required public tables are missing: documents, chapters, sections, contents")
            try:
                return service.list_specification_documents()
            except Exception:
                # Schema v3 / legacy fallback.
                return {"versioned": [], "generic": [
                    {"document_id": row["document_id"], "document_title": row["title"], "project_code": ""}
                    for row in service.list_documents()
                ]}

        self._run_async("Load documents", job, self._show_documents)

    def _show_documents(self, payload: dict[str, list[dict[str, Any]]]) -> None:
        self._clear_tree(self.doc_tree)
        qabug = payload.get("qabug") or []
        if payload.get("domain") == "QABUG":
            roots: dict[str, str] = {}
            for row in qabug:
                record_type = str(row.get("record_type") or "OTHER").upper()
                root = roots.get(record_type)
                if root is None:
                    root = self.doc_tree.insert(
                        "", END, text=f"{record_type} Records", values=("group", record_type), open=True
                    )
                    roots[record_type] = root
                issue_key = str(row.get("issue_key") or row.get("record_key") or row.get("record_id"))
                summary = str(row.get("summary") or "").strip()
                status = str(row.get("status_norm") or "UNSPECIFIED")
                chunks = int(row.get("chunk_count") or 0)
                text = f"{issue_key}  [{status}]  {summary}  · {chunks} chunk(s)"
                iid = self.doc_tree.insert(
                    root, END, text=text,
                    values=("qabug_record", int(row["record_id"]), issue_key, record_type),
                )
                self.doc_tree.insert(iid, END, text="Loading…", values=("placeholder",))
            if not qabug:
                self.doc_tree.insert("", END, text="No queryable QA/BUG records. Import a QA/BUG list first.", values=("group",))
            self.chunk_meta_var.set(f"QA/BUG records: {len(qabug)} · select a record/chunk")
            return

        versioned = payload.get("versioned") or []
        generic = payload.get("generic") or []

        spec_nodes: dict[int, str] = {}
        for row in versioned:
            spec_id = int(row["specification_id"])
            spec_node = spec_nodes.get(spec_id)
            if spec_node is None:
                spec_node = self.doc_tree.insert(
                    "",
                    END,
                    text=str(row.get("canonical_name") or row.get("specification_key") or spec_id),
                    values=("specification", spec_id),
                    open=True,
                )
                spec_nodes[spec_id] = spec_node
            role = str(row.get("version_role") or "HISTORY")
            version_label = str(row.get("version_label") or "<version>")
            version_node = self.doc_tree.insert(
                spec_node,
                END,
                text=f"{version_label}  [{role}]",
                values=("version", row["document_id"], version_label),
            )
            document_node = self.doc_tree.insert(
                version_node,
                END,
                text=str(row.get("document_title") or row["document_id"]),
                values=("document", row["document_id"]),
            )
            self.doc_tree.insert(document_node, END, text="Loading…", values=("placeholder",))

        if generic:
            generic_root = self.doc_tree.insert(
                "", END, text=f"Generic / Unversioned Documents ({len(generic)})", values=("group",), open=True
            )
            for row in generic:
                iid = self.doc_tree.insert(
                    generic_root,
                    END,
                    text=str(row.get("document_title") or row["document_id"]),
                    values=("document", row["document_id"]),
                )
                self.doc_tree.insert(iid, END, text="Loading…", values=("placeholder",))

    def _on_doc_tree_open(self, _event=None) -> None:
        selection = self.doc_tree.selection()
        if not selection:
            return
        iid = selection[0]
        values = self.doc_tree.item(iid).get("values") or []
        if not values:
            return
        kind = values[0]
        children = self.doc_tree.get_children(iid)
        if not children or self.doc_tree.item(children[0]).get("values", [""])[0] != "placeholder":
            return
        self.doc_tree.delete(children[0])
        service = self.pg_service
        assert service is not None

        if kind == "qabug_record":
            record_id = int(values[1])

            def load_qabug_chunks():
                result = service.execute_sql(
                    f"""
                    SELECT c.id AS content_id, c.chunk_key, c.section_type,
                           c.section_order - 1 AS chunk_index, c.content, c.content_hash,
                           c.index_hash, c.embedding_status, c.qdrant_point_id,
                           c.qdrant_collection, c.embedding_model, c.embedding_version
                    FROM public.qabug_chunks c
                    WHERE c.record_id={record_id} AND c.is_current
                    ORDER BY c.section_order, c.id
                    """,
                    max_rows=5000,
                )
                return list(result.rows)

            self._run_async(
                "Load QA/BUG chunks",
                load_qabug_chunks,
                lambda rows: self._insert_qabug_chunks(iid, rows),
            )
        elif kind == "document":
            document_id = str(values[1])
            self._run_async(
                "Load chapters",
                lambda: service.list_chapters(document_id),
                lambda rows: self._insert_chapters(iid, document_id, rows),
            )
        elif kind == "chapter":
            document_id, chapter_id = str(values[1]), str(values[2])
            self._run_async(
                "Load sections",
                lambda: service.list_sections(document_id, chapter_id),
                lambda rows: self._insert_sections(iid, document_id, chapter_id, rows),
            )
        elif kind == "section":
            document_id, section_id = str(values[1]), str(values[3])
            self._run_async(
                "Load chunks",
                lambda: service.list_contents(document_id, section_id),
                lambda rows: self._insert_chunks(iid, rows),
            )

    def _insert_qabug_chunks(self, parent: str, rows: list[dict[str, Any]]) -> None:
        for row in rows:
            section = str(row.get("section_type") or "CONTENT")
            status = str(row.get("embedding_status") or "PENDING")
            point_id = row.get("qdrant_point_id")
            self.doc_tree.insert(
                parent,
                END,
                text=f"{section} · Chunk {row.get('chunk_index', 0)} · Vector {status} · point {point_id}",
                values=("chunk", json.dumps(row, ensure_ascii=False, default=str)),
            )

    def _insert_chapters(self, parent: str, document_id: str, rows: list[dict[str, Any]]) -> None:
        for row in rows:
            iid = self.doc_tree.insert(
                parent,
                END,
                text=f"{row['chapter_id']}  {row['title']}",
                values=("chapter", document_id, row["chapter_id"]),
            )
            self.doc_tree.insert(iid, END, text="Loading…", values=("placeholder",))

    def _insert_sections(self, parent: str, document_id: str, chapter_id: str, rows: list[dict[str, Any]]) -> None:
        node_by_id: dict[str, str] = {}
        pending = list(rows)
        for _ in range(len(rows) + 2):
            changed = False
            next_pending = []
            for row in pending:
                section_id = str(row["section_id"])
                parent_id = row.get("parent_section_id")
                parent_node = parent
                if parent_id:
                    parent_node = node_by_id.get(str(parent_id), "")
                    if not parent_node:
                        next_pending.append(row)
                        continue
                iid = self.doc_tree.insert(
                    parent_node,
                    END,
                    text=f"{section_id}  {row['title']}",
                    values=("section", document_id, chapter_id, section_id),
                )
                self.doc_tree.insert(iid, END, text="Loading…", values=("placeholder",))
                node_by_id[section_id] = iid
                changed = True
            pending = next_pending
            if not pending or not changed:
                break
        for row in pending:
            section_id = str(row["section_id"])
            iid = self.doc_tree.insert(
                parent,
                END,
                text=f"{section_id}  {row['title']}",
                values=("section", document_id, chapter_id, section_id),
            )
            self.doc_tree.insert(iid, END, text="Loading…", values=("placeholder",))

    def _insert_chunks(self, parent: str, rows: list[dict[str, Any]]) -> None:
        for row in rows:
            self.doc_tree.insert(
                parent,
                END,
                text=f"Chunk {row.get('chunk_index')} · page {row.get('page_number')} · {row.get('token_count')} tokens",
                values=("chunk", json.dumps(row, ensure_ascii=False, default=str)),
            )

    def _on_doc_tree_select(self, _event=None) -> None:
        selection = self.doc_tree.selection()
        if not selection:
            return
        values = self.doc_tree.item(selection[0]).get("values") or []
        if not values or values[0] != "chunk":
            return
        row = json.loads(values[1])
        meta = {k: v for k, v in row.items() if k != "content"}
        self.chunk_meta_var.set(" · ".join(f"{k}={v}" for k, v in meta.items()))
        self.chunk_text.configure(state="normal")
        self.chunk_text.delete("1.0", END)
        self.chunk_text.insert("1.0", row.get("content") or "")
        self.chunk_text.configure(state="disabled")

    # ------------------------------------------------------------------
    # SQL -> Qdrant current-only sync
    # ------------------------------------------------------------------

    def _build_qdrant_sync_tab(self) -> None:
        header = ttk.Frame(self.qdrant_sync_tab)
        header.pack(fill=X, pady=(0, 10))
        title = ttk.Frame(header)
        title.pack(side=LEFT, fill=X, expand=True)
        ttk.Label(
            title,
            text="SQL → Qdrant Current Index",
            font=("Microsoft YaHei UI", 16, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            title,
            text=(
                "PostgreSQL is the source of truth. Only public.rag_current_chunks "
                "is indexed for normal QA; historical versions remain in SQL."
            ),
            bootstyle="secondary",
            wraplength=980,
            justify=LEFT,
        ).pack(anchor="w", pady=(3, 0))
        ttk.Button(
            header,
            text="Refresh Context",
            command=self.refresh_qdrant_sync_context,
            bootstyle="secondary-outline",
        ).pack(side=RIGHT)

        policy = ttk.LabelFrame(
            self.qdrant_sync_tab,
            text="Current-only indexing policy",
            padding=10,
            bootstyle="secondary",
        )
        policy.pack(fill=X, pady=(0, 10))
        policy.columnconfigure(1, weight=1)
        fields = (
            ("Scope", self.rag_sync_scope_var),
            ("Database", self.rag_sync_database_var),
            ("Source", self.rag_sync_source_var),
        )
        for idx, (label, variable) in enumerate(fields):
            ttk.Label(policy, text=f"{label}:", font=("Microsoft YaHei UI", 9, "bold")).grid(
                row=idx, column=0, sticky="w", padx=(0, 8), pady=2
            )
            ttk.Label(policy, textvariable=variable, bootstyle="primary").grid(
                row=idx, column=1, sticky="w", pady=2
            )
        ttk.Label(
            policy,
            text=(
                "Historical chunks are intentionally excluded from this collection. "
                "Version-history QA can use a separate history index later."
            ),
            bootstyle="secondary",
            style="Small.TLabel",
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))

        config = ttk.LabelFrame(
            self.qdrant_sync_tab,
            text="Index configuration",
            padding=10,
            bootstyle="secondary",
        )
        config.pack(fill=X, pady=(0, 10))
        config.columnconfigure(1, weight=3)
        config.columnconfigure(3, weight=2)
        config.columnconfigure(5, weight=1)

        ttk.Label(config, text="Collection").grid(row=0, column=0, sticky="w", padx=(0, 6))
        ttk.Entry(config, textvariable=self.rag_sync_collection_var).grid(
            row=0, column=1, sticky="ew", padx=(0, 8)
        )
        ttk.Button(
            config,
            text="Scope Default",
            command=self._reset_rag_sync_collection,
            bootstyle="secondary-outline",
        ).grid(row=0, column=2, sticky="w", padx=(0, 16))
        ttk.Label(config, text="Embedding model").grid(row=0, column=3, sticky="w", padx=(0, 6))
        ttk.Entry(config, textvariable=self.rag_sync_model_var).grid(
            row=0, column=4, sticky="ew", padx=(0, 16)
        )
        ttk.Label(config, text="Dimension").grid(row=0, column=5, sticky="w", padx=(0, 6))
        ttk.Entry(config, textvariable=self.rag_sync_dimension_var, width=8).grid(
            row=0, column=6, sticky="w", padx=(0, 16)
        )
        ttk.Label(config, text="Batch").grid(row=0, column=7, sticky="w", padx=(0, 6))
        ttk.Entry(config, textvariable=self.rag_sync_batch_var, width=7).grid(
            row=0, column=8, sticky="w"
        )

        cards = ttk.Frame(self.qdrant_sync_tab)
        cards.pack(fill=X, pady=(0, 10))
        card_vars = (
            self.rag_sync_sql_count_var,
            self.rag_sync_qdrant_count_var,
            self.rag_sync_new_var,
            self.rag_sync_changed_var,
            self.rag_sync_unchanged_var,
            self.rag_sync_stale_var,
        )
        for idx, variable in enumerate(card_vars):
            cards.columnconfigure(idx, weight=1, uniform="rag-sync-cards")
            frame = ttk.LabelFrame(cards, text="", padding=(9, 7), bootstyle="secondary")
            frame.grid(row=0, column=idx, sticky="ew", padx=(0 if idx == 0 else 4, 0))
            ttk.Label(frame, textvariable=variable, font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")

        actions = ttk.Frame(self.qdrant_sync_tab)
        actions.pack(fill=X, pady=(0, 8))
        self.rag_sync_preview_button = ttk.Button(
            actions,
            text="Preview Sync",
            command=self.preview_qdrant_sync,
            bootstyle="secondary-outline",
        )
        self.rag_sync_preview_button.pack(side=LEFT)
        self.rag_sync_now_button = ttk.Button(
            actions,
            text="Sync Now",
            command=lambda: self.start_qdrant_sync(full_rebuild=False),
            bootstyle="success",
        )
        self.rag_sync_now_button.pack(side=LEFT, padx=(8, 0))
        self.rag_sync_rebuild_button = ttk.Button(
            actions,
            text="Full Rebuild",
            command=lambda: self.start_qdrant_sync(full_rebuild=True),
            bootstyle="danger-outline",
        )
        self.rag_sync_rebuild_button.pack(side=LEFT, padx=(8, 0))
        self.rag_sync_cancel_button = ttk.Button(
            actions,
            text="Cancel",
            command=self.cancel_qdrant_sync,
            state="disabled",
            bootstyle="secondary-outline",
        )
        self.rag_sync_cancel_button.pack(side=LEFT, padx=(8, 0))
        ttk.Label(
            actions,
            text="Incremental sync embeds only New + Changed and removes known Stale points.",
            bootstyle="secondary",
            style="Small.TLabel",
        ).pack(side=RIGHT)

        governance = ttk.LabelFrame(
            self.qdrant_sync_tab,
            text="Qdrant Governance & Data Quality",
            padding=9,
            bootstyle="info",
        )
        governance.pack(fill=X, pady=(0, 8))
        ttk.Button(
            governance,
            text="Validate Data Quality",
            command=self.validate_qdrant_quality,
            bootstyle="info-outline",
        ).pack(side=LEFT)
        ttk.Button(
            governance,
            text="Repair Payload Indexes",
            command=self.repair_qdrant_payload_indexes,
            bootstyle="warning-outline",
        ).pack(side=LEFT, padx=(8, 0))
        ttk.Label(
            governance,
            textvariable=self.rag_quality_status_var,
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(side=LEFT, padx=(16, 0))
        ttk.Label(
            governance,
            textvariable=self.rag_payload_index_var,
            bootstyle="secondary",
        ).pack(side=RIGHT)

        progress_frame = ttk.Frame(self.qdrant_sync_tab)
        progress_frame.pack(fill=X, pady=(0, 8))
        self.rag_sync_progress = ttk.Progressbar(
            progress_frame,
            variable=self.rag_sync_progress_var,
            maximum=100,
            bootstyle="success-striped",
        )
        self.rag_sync_progress.pack(side=LEFT, fill=X, expand=True)
        ttk.Label(
            progress_frame,
            textvariable=self.rag_sync_progress_text_var,
            width=42,
            anchor="e",
            bootstyle="secondary",
            style="Small.TLabel",
        ).pack(side=RIGHT, padx=(10, 0))
        ttk.Label(
            self.qdrant_sync_tab,
            textvariable=self.rag_sync_status_var,
            wraplength=1200,
            justify=LEFT,
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(fill=X, pady=(0, 8))

        pane = ttk.Panedwindow(self.qdrant_sync_tab, orient=HORIZONTAL)
        pane.pack(fill=BOTH, expand=True)
        history = ttk.LabelFrame(pane, text="Sync History", padding=6, bootstyle="secondary")
        log = ttk.LabelFrame(pane, text="Operational Log", padding=6, bootstyle="secondary")
        pane.add(history, weight=3)
        pane.add(log, weight=2)

        self.rag_sync_history_tree = ttk.Treeview(
            history,
            columns=(
                "id", "mode", "status", "source", "new", "changed", "stale",
                "upserted", "deleted", "qdrant", "started",
            ),
            show="headings",
            selectmode="browse",
            height=14,
        )
        definitions = (
            ("id", "Run", 55, "center"),
            ("mode", "Mode", 95, "center"),
            ("status", "Status", 115, "center"),
            ("source", "SQL", 65, "center"),
            ("new", "New", 55, "center"),
            ("changed", "Changed", 65, "center"),
            ("stale", "Stale", 55, "center"),
            ("upserted", "Upserted", 65, "center"),
            ("deleted", "Deleted", 60, "center"),
            ("qdrant", "Qdrant", 65, "center"),
            ("started", "Started", 145, "center"),
        )
        for column, title_text, width, anchor in definitions:
            self.rag_sync_history_tree.heading(column, text=title_text)
            self.rag_sync_history_tree.column(column, width=width, minwidth=45, anchor=anchor)
        self.rag_sync_history_tree.tag_configure("RUNNING", foreground="#1F4E8C")
        self.rag_sync_history_tree.tag_configure("READY", foreground="#255D35")
        self.rag_sync_history_tree.tag_configure("READY_WITH_DRIFT", foreground="#8A5315")
        self.rag_sync_history_tree.tag_configure("INTERRUPTED", foreground="#8A5315")
        self.rag_sync_history_tree.tag_configure("CANCELLED", foreground="#5B6472")
        self.rag_sync_history_tree.tag_configure("FAILED", foreground="#8B3145")
        history_scroll = ttk.Scrollbar(history, orient="vertical", command=self.rag_sync_history_tree.yview)
        self.rag_sync_history_tree.configure(yscrollcommand=history_scroll.set)
        self.rag_sync_history_tree.pack(side=LEFT, fill=BOTH, expand=True)
        history_scroll.pack(side=RIGHT, fill=Y)

        self.rag_sync_log = tk.Text(
            log,
            wrap="word",
            height=14,
            font=("Consolas", 9),
            state="disabled",
        )
        log_scroll = ttk.Scrollbar(log, orient="vertical", command=self.rag_sync_log.yview)
        self.rag_sync_log.configure(yscrollcommand=log_scroll.set)
        self.rag_sync_log.pack(side=LEFT, fill=BOTH, expand=True)
        log_scroll.pack(side=RIGHT, fill=Y)

    def _current_rag_sync_project(self):
        try:
            return ProjectRegistry.resolve(self.host.project_var.get())
        except ProjectRegistryError as exc:
            raise RuntimeError(str(exc)) from exc

    def _reset_rag_sync_collection(self) -> None:
        project = self._current_rag_sync_project()
        self.rag_sync_collection_var.set(
            QABUG_COLLECTION
            if project.code == "QABUG"
            else CurrentQdrantSyncService.default_collection_name(project.code)
        )

    def refresh_qdrant_sync_context(self) -> None:
        self._rebuild_services()
        project = self._current_rag_sync_project()
        self.rag_sync_scope_var.set(project.display_name)
        self.rag_sync_database_var.set(self.profile.postgres.database)
        default_collection = (
            QABUG_COLLECTION
            if project.code == "QABUG"
            else CurrentQdrantSyncService.default_collection_name(project.code)
        )
        current_collection = self.rag_sync_collection_var.get().strip()
        known_defaults = {
            CurrentQdrantSyncService.default_collection_name(code)
            for code in ("21MM", "24MM", "COMMON")
        } | {QABUG_COLLECTION}
        if not current_collection or current_collection in known_defaults:
            self.rag_sync_collection_var.set(default_collection)
        if project.code == "QABUG":
            self.rag_sync_source_var.set("public.qabug_current_chunks · QA/BUG CURRENT ONLY")
            self.rag_sync_status_var.set(
                f"Ready: QA/BUG / {self.profile.postgres.database} / public.qabug_current_chunks. "
                "SQL is authoritative; missing or stale Qdrant points are repaired incrementally."
            )
            self.rag_payload_index_var.set(
                f"Payload Indexes ?/{len(QABUG_PAYLOAD_INDEX_CONTRACT)}"
            )
        else:
            self.rag_sync_source_var.set("public.rag_current_chunks · CURRENT ONLY")
            self.rag_sync_status_var.set(
                f"Ready: {project.display_name} / {self.profile.postgres.database} / current-only source. "
                "Interrupted runs are recovered automatically when the sync lock is free."
            )
        self.rag_sync_progress_var.set(0.0)
        self.rag_sync_progress_text_var.set("Idle")
        self.rag_quality_status_var.set("Data Quality: not validated")
        if project.code != "QABUG":
            self.rag_payload_index_var.set(f"Payload Indexes ?/{len(PAYLOAD_INDEX_CONTRACT)}")
        self._append_rag_sync_log(
            f"Context: scope={project.code} database={self.profile.postgres.database} "
            f"collection={self.rag_sync_collection_var.get()}"
        )
        self._refresh_rag_sync_history()

    def _make_rag_sync_service(self) -> CurrentQdrantSyncService:
        self._rebuild_services()
        project = self._current_rag_sync_project()
        password = self.pg_password_var.get() or os.getenv("POSTGRES_PASSWORD", "")
        try:
            dimension = int(self.rag_sync_dimension_var.get().strip() or "1024")
            batch_size = int(self.rag_sync_batch_var.get().strip() or "32")
        except ValueError as exc:
            raise RuntimeError("Dimension and Batch must be integers.") from exc
        if project.code == "QABUG":
            return QABugCurrentQdrantSyncService(
                postgres=self.profile.postgres,
                postgres_password=password,
                qdrant=self.profile.qdrant,
                qdrant_api_key=self.q_key_var.get(),
                collection_name=self.rag_sync_collection_var.get().strip() or QABUG_COLLECTION,
                embedding_model=self.rag_sync_model_var.get().strip() or "BAAI/bge-m3",
                embedding_version="current-index-v1",
                vector_dimension=dimension,
                batch_size=batch_size,
            )
        return CurrentQdrantSyncService(
            postgres=self.profile.postgres,
            postgres_password=password,
            qdrant=self.profile.qdrant,
            qdrant_api_key=self.q_key_var.get(),
            project_code=project.code,
            collection_name=self.rag_sync_collection_var.get().strip(),
            embedding_model=self.rag_sync_model_var.get().strip() or "BAAI/bge-m3",
            embedding_version="current-index-v1",
            vector_dimension=dimension,
            batch_size=batch_size,
        )

    def _make_qdrant_quality_validator(self):
        service = self._make_rag_sync_service()
        password = self.pg_password_var.get() or os.getenv("POSTGRES_PASSWORD", "")
        if service.project_code == "QABUG":
            return QABugDataQualityValidator(
                postgres_profile=self.profile.postgres,
                password=password,
                qdrant_service=service.qdrant,
                collection_name=service.collection_name,
            )
        return QdrantDataQualityValidator(
            postgres=self.profile.postgres,
            postgres_password=password,
            qdrant=service.qdrant,
            project_code=service.project_code,
            collection_name=service.collection_name,
            embedding_model=service.embedding_model,
            embedding_version=service.embedding_version,
            vector_dimension=service.vector_dimension,
        )

    def validate_qdrant_quality(self) -> None:
        try:
            validator = self._make_qdrant_quality_validator()
        except Exception as exc:
            messagebox.showerror("Qdrant Data Quality", str(exc), parent=self.root)
            return
        self.rag_quality_status_var.set("Data Quality: validating…")

        def show(report) -> None:
            if isinstance(report, QABugQualityReport):
                error_count = (
                    report.missing_points + report.orphan_points + report.stale_points
                    + len(report.missing_payload_indexes) + len(report.mismatched_payload_indexes)
                )
                warning_count = len(report.warnings)
                self.rag_quality_status_var.set(
                    f"Data Quality: {report.status} · errors={error_count} · warnings={warning_count}"
                )
                present = len(QABUG_PAYLOAD_INDEX_CONTRACT) - len(report.missing_payload_indexes)
                self.rag_payload_index_var.set(
                    f"Payload Indexes {present}/{len(QABUG_PAYLOAD_INDEX_CONTRACT)}"
                )
                self._append_rag_sync_log(
                    f"QA/BUG DQ: SQL records={report.sql_records}, SQL chunks={report.sql_chunks}, "
                    f"Qdrant={report.qdrant_points}, missing={report.missing_points}, "
                    f"orphan={report.orphan_points}, stale={report.stale_points}, "
                    f"quarantine={report.quarantined_records}."
                )
                if report.missing_payload_indexes:
                    self._append_rag_sync_log(
                        "Missing QA/BUG payload indexes: " + ", ".join(report.missing_payload_indexes)
                    )
                if report.mismatched_payload_indexes:
                    self._append_rag_sync_log(
                        "Mismatched QA/BUG payload indexes: " + ", ".join(report.mismatched_payload_indexes)
                    )
                for warning in report.warnings:
                    self._append_rag_sync_log("[WARN] " + warning)
                return

            self.rag_quality_status_var.set(
                f"Data Quality: {report.status} · errors={report.error_count} · warnings={report.warning_count}"
            )
            self.rag_payload_index_var.set(
                f"Payload Indexes {report.payload_index_present}/{report.payload_index_expected}"
            )
            self._append_rag_sync_log("\n".join(report.summary_lines()))
            for issue in report.issues[:80]:
                suffix = f" · content_id={issue.content_id}" if issue.content_id is not None else ""
                self._append_rag_sync_log(
                    f"[{issue.severity}] {issue.code}: {issue.message}{suffix}"
                )
            if len(report.issues) > 80:
                self._append_rag_sync_log(f"... {len(report.issues) - 80} additional issue(s) omitted from UI log")

        self._run_async("Validate Qdrant data quality", validator.validate, show)

    def repair_qdrant_payload_indexes(self) -> None:
        try:
            service = self._make_rag_sync_service()
        except Exception as exc:
            messagebox.showerror("Qdrant Payload Indexes", str(exc), parent=self.root)
            return
        if not service.qdrant.collection_exists(service.collection_name):
            messagebox.showwarning(
                "Qdrant Payload Indexes",
                f"Collection {service.collection_name} does not exist. Run Sync Now first.",
                parent=self.root,
            )
            return
        self.rag_quality_status_var.set("Data Quality: repairing payload indexes…")

        def job():
            return service.ensure_payload_index_contract(repair_mismatched=True)

        def show(result) -> None:
            created = len(result.created)
            repaired = len(result.repaired)
            unchanged = len(result.unchanged)
            contract_size = (
                len(QABUG_PAYLOAD_INDEX_CONTRACT)
                if getattr(service, "project_code", "") == "QABUG"
                else len(PAYLOAD_INDEX_CONTRACT)
            )
            self.rag_payload_index_var.set(
                f"Payload Indexes {created + repaired + unchanged}/{contract_size}"
            )
            self.rag_quality_status_var.set("Data Quality: payload index contract ready")
            self._append_rag_sync_log(
                f"Payload index contract: created={created}, repaired={repaired}, unchanged={unchanged}."
            )
            for item in (*result.created, *result.repaired):
                self._append_rag_sync_log(
                    f"[{item.action}] {item.field_name}: {item.expected_type}"
                )

        self._run_async("Repair Qdrant payload indexes", job, show)

    def preview_qdrant_sync(self) -> None:
        if self.rag_sync_thread is not None and self.rag_sync_thread.is_alive():
            return
        try:
            service = self._make_rag_sync_service()
        except Exception as exc:
            messagebox.showerror("SQL → Qdrant Sync", str(exc), parent=self.root)
            return
        self.rag_sync_cancel_event.clear()
        self._rag_sync_current_run_id = 0
        self._set_rag_sync_busy(True, cancellable=True)
        self.rag_sync_status_var.set("Previewing SQL current chunks against Qdrant sync state…")
        self.rag_sync_progress_text_var.set("Previewing")
        self.rag_sync_progress_var.set(0.0)

        def progress(payload: dict[str, Any]) -> None:
            self.event_queue.put(AsyncEvent("rag_sync_progress", payload))

        def worker() -> None:
            thread = threading.current_thread()
            try:
                plan = service.preview(
                    progress=progress,
                    cancel_check=self.rag_sync_cancel_event.is_set,
                )
                self.event_queue.put(AsyncEvent("rag_sync_preview_success", plan))
            except QdrantSyncCancelled as exc:
                self.event_queue.put(AsyncEvent("rag_sync_cancelled", str(exc)))
            except Exception as exc:
                LOGGER.exception("Qdrant sync preview failed")
                self.event_queue.put(AsyncEvent("rag_sync_error", f"{type(exc).__name__}: {exc}"))
            finally:
                self.event_queue.put(AsyncEvent("thread_done", thread))

        thread = threading.Thread(target=worker, daemon=True)
        self.rag_sync_thread = thread
        self.active_threads.add(thread)
        thread.start()

    def start_qdrant_sync(self, *, full_rebuild: bool) -> None:
        if self.rag_sync_thread is not None and self.rag_sync_thread.is_alive():
            return
        if full_rebuild:
            confirmed = messagebox.askyesno(
                "Full Rebuild Current Qdrant Index",
                (
                    "This will delete and recreate the selected Qdrant collection, then "
                    "re-embed every current SQL chunk.\n\nHistorical SQL data is not deleted.\n\nContinue?"
                ),
                parent=self.root,
            )
            if not confirmed:
                return

        try:
            service = self._make_rag_sync_service()
        except Exception as exc:
            messagebox.showerror("SQL → Qdrant Sync", str(exc), parent=self.root)
            return

        self.rag_sync_cancel_event.clear()
        self._rag_sync_current_run_id = 0
        self._rag_sync_current_mode = "FULL_REBUILD" if full_rebuild else "INCREMENTAL"
        self._rag_sync_current_started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._set_rag_sync_busy(True, cancellable=True)
        self.rag_sync_progress_var.set(0.0)
        mode_text = "Full rebuild" if full_rebuild else "Incremental sync"
        self.rag_sync_status_var.set(f"{mode_text} started…")
        self.rag_sync_progress_text_var.set("Preparing")
        self._append_rag_sync_log(f"{mode_text}: start")

        def progress(payload: dict[str, Any]) -> None:
            self.event_queue.put(AsyncEvent("rag_sync_progress", payload))

        def worker() -> None:
            thread = threading.current_thread()
            try:
                result = service.sync(
                    full_rebuild=full_rebuild,
                    progress=progress,
                    cancel_check=self.rag_sync_cancel_event.is_set,
                )
                self.event_queue.put(AsyncEvent("rag_sync_success", result))
            except QdrantSyncCancelled as exc:
                self.event_queue.put(AsyncEvent("rag_sync_cancelled", str(exc)))
            except Exception as exc:
                LOGGER.exception("SQL -> Qdrant sync failed")
                self.event_queue.put(AsyncEvent("rag_sync_error", f"{type(exc).__name__}: {exc}"))
            finally:
                self.event_queue.put(AsyncEvent("thread_done", thread))

        thread = threading.Thread(target=worker, daemon=True)
        self.rag_sync_thread = thread
        self.active_threads.add(thread)
        thread.start()

    def cancel_qdrant_sync(self) -> None:
        if self.rag_sync_thread is None or not self.rag_sync_thread.is_alive():
            return
        self.rag_sync_cancel_event.set()
        self.rag_sync_status_var.set("Cancellation requested; current batch will finish safely.")
        self.rag_sync_progress_text_var.set("Cancelling…")
        self._append_rag_sync_log("Cancellation requested by user")

    def _set_rag_sync_busy(self, busy: bool, *, cancellable: bool = False) -> None:
        state = "disabled" if busy else "normal"
        for button_name in (
            "rag_sync_preview_button",
            "rag_sync_now_button",
            "rag_sync_rebuild_button",
        ):
            button = getattr(self, button_name, None)
            if button is not None:
                button.configure(state=state)
        if hasattr(self, "rag_sync_cancel_button"):
            self.rag_sync_cancel_button.configure(
                state=("normal" if busy and cancellable else "disabled")
            )

    def _handle_rag_sync_progress(self, payload: dict[str, Any]) -> None:
        percent = float(payload.get("percent") or 0.0)
        message = str(payload.get("message") or "Working…")
        stage = str(payload.get("stage") or "")

        self.rag_sync_progress_var.set(max(0.0, min(100.0, percent)))

        if stage == "plan_done":
            self._apply_rag_sync_plan_payload(payload, qdrant_label="Qdrant Before")
        elif stage == "run_started":
            self._apply_rag_sync_plan_payload(payload, qdrant_label="Qdrant Before")
            self._rag_sync_current_run_id = int(payload.get("run_id") or 0)
            self._rag_sync_current_mode = str(payload.get("mode") or "")
            self._rag_sync_current_started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._upsert_rag_sync_history_live(payload)
        elif stage in {"upsert", "delete", "verify", "done"}:
            qdrant_live = payload.get("qdrant_live_count")
            if qdrant_live is not None:
                label = "Qdrant Final" if stage == "done" else "Qdrant Live"
                self.rag_sync_qdrant_count_var.set(f"{label} {qdrant_live}")
            self._upsert_rag_sync_history_live(payload)

        upserted = payload.get("upserted_count")
        work_count = payload.get("work_count")
        if upserted is not None and work_count is not None and int(work_count or 0) > 0:
            live = payload.get("qdrant_live_count")
            live_text = f" · Qdrant Live {live}" if live is not None else ""
            self.rag_sync_progress_text_var.set(
                f"{percent:.1f}% · Upserted {upserted}/{work_count}{live_text}"
            )
        else:
            self.rag_sync_progress_text_var.set(f"{percent:.1f}% · {message}")

        self.rag_sync_status_var.set(message)
        if getattr(self, "_last_rag_sync_log_message", None) != message:
            self._append_rag_sync_log(message)
            self._last_rag_sync_log_message = message

    def _apply_rag_sync_plan_payload(
        self,
        payload: dict[str, Any],
        *,
        qdrant_label: str = "Qdrant",
    ) -> None:
        if payload.get("source_count") is not None:
            self.rag_sync_sql_count_var.set(f"SQL Current {int(payload.get('source_count') or 0)}")
        qdrant_count = payload.get("qdrant_count")
        if qdrant_count is not None:
            qdrant_value = int(qdrant_count)
            self.rag_sync_qdrant_count_var.set(
                f"{qdrant_label} {qdrant_value if qdrant_value >= 0 else '?'}"
            )
        for key, variable, label in (
            ("new_count", self.rag_sync_new_var, "New"),
            ("changed_count", self.rag_sync_changed_var, "Changed"),
            ("unchanged_count", self.rag_sync_unchanged_var, "Unchanged"),
            ("stale_count", self.rag_sync_stale_var, "Stale"),
        ):
            if payload.get(key) is not None:
                variable.set(f"{label} {int(payload.get(key) or 0)}")

    def _upsert_rag_sync_history_live(self, payload: dict[str, Any]) -> None:
        if not hasattr(self, "rag_sync_history_tree"):
            return
        run_id = int(payload.get("run_id") or self._rag_sync_current_run_id or 0)
        if run_id <= 0:
            return
        self._rag_sync_current_run_id = run_id
        mode = str(payload.get("mode") or self._rag_sync_current_mode or "INCREMENTAL")
        self._rag_sync_current_mode = mode
        status = str(payload.get("status") or "RUNNING")
        iid = f"rag-sync-run-{run_id}"

        existing_values = ()
        if self.rag_sync_history_tree.exists(iid):
            existing_values = tuple(self.rag_sync_history_tree.item(iid).get("values") or ())

        def existing(index: int, default: Any = 0) -> Any:
            return existing_values[index] if len(existing_values) > index else default

        qdrant_value = payload.get("qdrant_live_count")
        if qdrant_value is None:
            qdrant_value = existing(9, "-")
        started = existing(10, self._rag_sync_current_started or datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        values = (
            run_id,
            mode,
            status,
            int(payload.get("source_count") if payload.get("source_count") is not None else existing(3, 0) or 0),
            int(payload.get("new_count") if payload.get("new_count") is not None else existing(4, 0) or 0),
            int(payload.get("changed_count") if payload.get("changed_count") is not None else existing(5, 0) or 0),
            int(payload.get("stale_count") if payload.get("stale_count") is not None else existing(6, 0) or 0),
            int(payload.get("upserted_count") if payload.get("upserted_count") is not None else existing(7, 0) or 0),
            int(payload.get("deleted_count") if payload.get("deleted_count") is not None else existing(8, 0) or 0),
            qdrant_value,
            started,
        )
        if self.rag_sync_history_tree.exists(iid):
            self.rag_sync_history_tree.item(iid, values=values, tags=(status,))
        else:
            self.rag_sync_history_tree.insert(
                "",
                0,
                iid=iid,
                values=values,
                tags=(status,),
            )

    def _show_qdrant_sync_plan(self, plan: QdrantSyncPlan) -> None:
        self.rag_sync_sql_count_var.set(f"SQL Current {plan.source_count}")
        self.rag_sync_qdrant_count_var.set(f"Qdrant Current {plan.qdrant_count if plan.qdrant_count >= 0 else '?'}")
        self.rag_sync_new_var.set(f"New {plan.new_count}")
        self.rag_sync_changed_var.set(f"Changed {plan.changed_count}")
        self.rag_sync_unchanged_var.set(f"Unchanged {plan.unchanged_count}")
        self.rag_sync_stale_var.set(f"Stale {plan.stale_count}")
        self.rag_sync_progress_var.set(0.0)
        self.rag_sync_progress_text_var.set("Preview ready")
        if not plan.work_count and not plan.stale_count and plan.qdrant_count == plan.source_count:
            summary = "Current Qdrant index is already synchronized with PostgreSQL."
        else:
            summary = (
                f"Preview: SQL={plan.source_count}, Qdrant={plan.qdrant_count}, "
                f"New={plan.new_count}, Changed={plan.changed_count}, "
                f"Unchanged={plan.unchanged_count}, Stale={plan.stale_count}."
            )
        if plan.warnings:
            summary += "  Warning: " + " | ".join(plan.warnings)
        self.rag_sync_status_var.set(summary)
        self._append_rag_sync_log(summary)
        self._refresh_rag_sync_history()

    def _show_qdrant_sync_result(self, result: QdrantSyncResult) -> None:
        plan = result.plan
        self.rag_sync_sql_count_var.set(f"SQL Current {plan.source_count}")
        self.rag_sync_qdrant_count_var.set(f"Qdrant Final {result.final_qdrant_count}")
        self.rag_sync_new_var.set(f"New {plan.new_count}")
        self.rag_sync_changed_var.set(f"Changed {plan.changed_count}")
        self.rag_sync_unchanged_var.set(f"Unchanged {plan.unchanged_count}")
        self.rag_sync_stale_var.set(f"Stale {plan.stale_count}")
        self.rag_sync_progress_var.set(100.0)
        self.rag_sync_progress_text_var.set(f"100% · {result.status}")
        summary = (
            f"Run {result.run_id} {result.mode}: {result.status}. "
            f"Upserted={result.upserted_count}, Deleted={result.deleted_count}, "
            f"Qdrant={result.final_qdrant_count}, Elapsed={result.elapsed_seconds:.1f}s."
        )
        if result.warnings:
            summary += "  " + " | ".join(result.warnings)
        self.rag_sync_status_var.set(summary)
        self._append_rag_sync_log(summary)
        self._refresh_rag_sync_history()

    def _refresh_rag_sync_history(self) -> None:
        if not hasattr(self, "rag_sync_history_tree"):
            return
        try:
            service = self._make_rag_sync_service()
        except Exception as exc:
            self._append_rag_sync_log(f"History unavailable: {exc}")
            return
        self._run_async(
            "Load Qdrant sync history",
            lambda: service.list_runs(limit=50),
            self._show_rag_sync_history,
        )

    def _show_rag_sync_history(self, rows: list[dict[str, Any]]) -> None:
        if not hasattr(self, "rag_sync_history_tree"):
            return
        tree = self.rag_sync_history_tree
        for iid in tree.get_children():
            tree.delete(iid)
        interrupted = 0
        for row in rows:
            run_id = int(row.get("id") or 0)
            status = str(row.get("status") or "-")
            if status == "INTERRUPTED":
                interrupted += 1
            qdrant_after = row.get("qdrant_after_count")
            if qdrant_after is None or int(qdrant_after) < 0:
                qdrant_after = "-"
            tree.insert(
                "",
                END,
                iid=f"rag-sync-run-{run_id}" if run_id else None,
                values=(
                    run_id or "-",
                    row.get("mode") or "-",
                    status,
                    row.get("source_count") or 0,
                    row.get("new_count") or 0,
                    row.get("changed_count") or 0,
                    row.get("stale_count") or 0,
                    row.get("upserted_count") or 0,
                    row.get("deleted_count") or 0,
                    qdrant_after,
                    str(row.get("started_at") or "")[:19],
                ),
                tags=(status,),
            )
        if interrupted:
            self._append_rag_sync_log(
                f"Recovered history: {interrupted} interrupted run(s) retained for audit; completed batches remain reusable."
            )

    def _append_rag_sync_log(self, message: str) -> None:
        if not hasattr(self, "rag_sync_log"):
            return
        text = str(message or "").strip()
        if not text:
            return
        self.rag_sync_log.configure(state="normal")
        self.rag_sync_log.insert(END, text + "\n")
        self.rag_sync_log.see(END)
        self.rag_sync_log.configure(state="disabled")

    def _build_qdrant_tab(self) -> None:
        pane = ttk.Panedwindow(self.qdrant_tab, orient=HORIZONTAL)
        pane.pack(fill=BOTH, expand=True)
        left = ttk.Frame(pane, padding=5)
        right = ttk.Frame(pane, padding=5)
        pane.add(left, weight=2)
        pane.add(right, weight=3)

        toolbar = ttk.Frame(left)
        toolbar.pack(fill=X, pady=(0, 5))
        ttk.Button(toolbar, text="Refresh Collections", command=self.refresh_qdrant, bootstyle="primary").pack(side=LEFT)
        ttk.Label(toolbar, textvariable=self.qdrant_status_var, bootstyle="secondary").pack(side=RIGHT)
        self.q_collection_tree, _, _ = self._new_table_view(left, selectmode="browse")
        self.q_collection_tree.bind("<<TreeviewSelect>>", self._on_q_collection_select)

        ttk.Label(right, text="Points / Payloads", font=("Microsoft YaHei UI", 10, "bold")).pack(fill=X, pady=(0, 5))
        self.q_points_tree, _, _ = self._new_table_view(right)

    def refresh_qdrant(self) -> None:
        self._rebuild_services()
        service = self.qdrant_service
        assert service is not None
        self._run_async("Load Qdrant collections", service.list_collections, self._show_qdrant_collections)

    def _show_qdrant_collections(self, rows) -> None:
        dictionaries = [row.__dict__ for row in rows]
        columns = list(dictionaries[0]) if dictionaries else []
        self._populate_table_tree(self.q_collection_tree, dictionaries, columns)
        self.qdrant_status_var.set(f"Qdrant: {len(dictionaries)} collection(s)")

    def _on_q_collection_select(self, _event=None) -> None:
        selection = self.q_collection_tree.selection()
        if not selection:
            return
        values = self.q_collection_tree.item(selection[0]).get("values") or []
        if not values:
            return
        collection = str(values[0])
        service = self.qdrant_service
        assert service is not None
        self._run_async(
            f"Load Qdrant points: {collection}",
            lambda: service.scroll_points(collection, limit=200),
            self._show_qdrant_points,
        )

    def _show_qdrant_points(self, rows: list[dict[str, Any]]) -> None:
        normalized = [
            {"id": row["id"], "payload": json.dumps(row.get("payload") or {}, ensure_ascii=False, default=str)}
            for row in rows
        ]
        self._populate_table_tree(self.q_points_tree, normalized, ["id", "payload"])

    def _build_embedding_tab(self) -> None:
        controls = ttk.Frame(self.embedding_tab)
        controls.pack(fill=X, pady=(0, 6))
        ttk.Button(controls, text="Refresh Summary", command=self.refresh_embedding_status, bootstyle="primary").pack(side=LEFT)
        ttk.Label(controls, text="Status").pack(side=LEFT, padx=(16, 5))
        ttk.Combobox(
            controls,
            textvariable=self.embedding_status_var,
            values=["PENDING", "PROCESSING", "COMPLETED", "DONE", "FAILED"],
            state="readonly",
            width=14,
        ).pack(side=LEFT)
        ttk.Button(controls, text="Load Rows", command=self.load_embedding_rows).pack(side=LEFT, padx=5)

        pane = ttk.Panedwindow(self.embedding_tab, orient=HORIZONTAL)
        pane.pack(fill=BOTH, expand=True)
        left = ttk.Frame(pane)
        right = ttk.Frame(pane)
        pane.add(left, weight=2)
        pane.add(right, weight=5)
        self.embedding_summary_tree, _, _ = self._new_table_view(left)
        self.embedding_rows_tree, _, _ = self._new_table_view(right)

    def refresh_embedding_status(self) -> None:
        self._rebuild_services()
        service = self.pg_service
        assert service is not None
        self._run_async(
            "Embedding status summary",
            service.embedding_status_summary,
            lambda rows: self._populate_table_tree(self.embedding_summary_tree, rows, list(rows[0]) if rows else []),
        )

    def load_embedding_rows(self) -> None:
        status = self.embedding_status_var.get()
        self._rebuild_services()
        service = self.pg_service
        assert service is not None
        self._run_async(
            f"Embedding rows: {status}",
            lambda: service.embedding_rows(status),
            lambda rows: self._populate_table_tree(self.embedding_rows_tree, rows, list(rows[0]) if rows else []),
        )
