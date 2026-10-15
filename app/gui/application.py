from __future__ import annotations

import difflib
import json
import logging
import re
import os
import queue
import sys
import threading
import traceback
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from tkinter import (
    BOTH,
    DISABLED,
    END,
    EXTENDED,
    LEFT,
    NORMAL,
    RIGHT,
    VERTICAL,
    WORD,
    X,
    Y,
)
import tkinter as tk
from tkinter import filedialog, messagebox
import ttkbootstrap as ttk
from typing import Any

import yaml

from app.i18n import install_zh_cn_ui, zh
install_zh_cn_ui(ttk, tk, messagebox)


# ============================================================
# User PostgreSQL Configuration
# ============================================================
#
# Distribution remains a single EXE.
#
# Runtime PostgreSQL settings are stored outside the EXE:
#
# Windows:
#   %LOCALAPPDATA%\DocumentIntelligenceConsole\config.yaml
#
# The password is NEVER written to this YAML file.
# It is provided through POSTGRES_PASSWORD.
#
# ConfigLoader already supports CONFIG_FILE, therefore the GUI
# can point the existing database layer to this user-writable
# configuration without changing the document pipelines.
# ============================================================


def get_user_config_directory() -> Path:
    """Return the unified v5 per-user configuration directory."""
    if os.name == "nt":
        local_app_data = os.getenv("LOCALAPPDATA")
        if local_app_data and local_app_data.strip():
            return Path(local_app_data) / "DocumentIntelligenceConsole"
    return Path.home() / ".document-intelligence-console"


def get_legacy_user_config_directory() -> Path:
    """v4 compatibility path used for transparent first-run migration."""
    if os.name == "nt":
        local_app_data = os.getenv("LOCALAPPDATA")
        if local_app_data and local_app_data.strip():
            return Path(local_app_data) / "DocumentIngestionPlatform"
    return Path.home() / ".document-ingestion-platform"


USER_CONFIG_DIR = get_user_config_directory()
USER_POSTGRES_CONFIG_FILE = USER_CONFIG_DIR / "config.yaml"
LEGACY_USER_POSTGRES_CONFIG_FILE = get_legacy_user_config_directory() / "config.yaml"


DIFF_PALETTE = {
    "added": {"bg": "#E8F5E9", "fg": "#255D35"},
    "removed": {"bg": "#FDECEC", "fg": "#8B3145"},
    "modified_old": {"bg": "#FFF1E6", "fg": "#8A5315"},
    "modified_new": {"bg": "#EAF2FF", "fg": "#1F4E8C"},
    "moved": {"bg": "#F2ECFF", "fg": "#5D4C8A"},
    "muted": {"bg": "#F7F8FA", "fg": "#5B6472"},
}


def activate_user_postgres_config() -> None:
    """Activate the unified config, falling back to the v4 config once."""
    existing_config_file = os.getenv("CONFIG_FILE")
    if existing_config_file and existing_config_file.strip():
        return

    if USER_POSTGRES_CONFIG_FILE.is_file():
        os.environ["CONFIG_FILE"] = str(USER_POSTGRES_CONFIG_FILE)
        return

    if LEGACY_USER_POSTGRES_CONFIG_FILE.is_file():
        os.environ["CONFIG_FILE"] = str(LEGACY_USER_POSTGRES_CONFIG_FILE)


def hydrate_postgres_password_from_windows_user_env() -> None:
    """
    Load a remembered POSTGRES_PASSWORD directly from HKCU.

    This avoids depending on whether the parent PowerShell / Explorer
    process has refreshed its environment after the password was saved.
    """

    if os.getenv(
        "POSTGRES_PASSWORD"
    ):
        return

    if os.name != "nt":
        return

    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            "Environment",
            0,
            winreg.KEY_READ,
        ) as key:

            value, _ = (
                winreg.QueryValueEx(
                    key,
                    "POSTGRES_PASSWORD",
                )
            )

        if (
            isinstance(
                value,
                str,
            )
            and value
        ):
            os.environ[
                "POSTGRES_PASSWORD"
            ] = value

    except (
        FileNotFoundError,
        OSError,
    ):
        pass


activate_user_postgres_config()
hydrate_postgres_password_from_windows_user_env()


from app.pipeline.pipeline_factory import PipelineFactory
from app.config.config_loader import DatabaseConfig
from app.database.connection import DatabaseConnection
from app.model.chapter import Chapter
from app.model.content import Content
from app.model.document import Document
from app.model.section import Section
from app.project.project_registry import ProjectRegistry, ProjectRegistryError
from app.output.project_output_router import ProjectOutputRouter
from app.storage.postgres_storage import PostgresStorage
from app.storage.project_database_router import ProjectDatabaseRouter
from app.storage.schema_manager import SchemaManager, RAG_SCHEMA_VERSION
from app.storage.configuration_schema_manager import (
    ConfigurationSchemaManager,
    CONFIG_SCHEMA_VERSION,
)
from app.storage.specification_history_service import SpecificationHistoryService
from app.storage.version_compare_policy import (
    is_forward_version_compare,
    legal_version_compare_values,
)
from app.enterprise.credential_store import get_secret, set_secret
from app.enterprise.data_module import DataConsoleModule
from app.enterprise.profiles import PostgresProfile, QdrantProfile
from app.enterprise.qdrant_service import QdrantService
from app.qabug.schema_manager import QABugSchemaManager, QABUG_SCHEMA_VERSION, DEFAULT_QABUG_DATABASE
from app.qabug.importer import QABugImportPipeline
from app.qabug.qdrant_sync import QABugQdrantManager, QABUG_COLLECTION
from app.qabug.quality import QABugDataQualityValidator
from app.relations.audit_service import EngineeringRelationAuditService
from app.relations.graph_service import EngineeringRelationGraphService, RelationGraph
from app.gui.ai_workspace import AIWorkspaceModule



# ============================================================
# Application Constants
# ============================================================

APP_NAME = "文档智能控制台"
# APP_NAME = "Document Intelligence Console Enterprise"
APP_VERSION = "1.30"
# APP_VERSION = "5.7.3-CN"
# APP_VERSION = "5.6.0"
# Historical source-contract compatibility markers. These are comments only;
# APP_VERSION = "5.5.0"
# several legacy regression tests intentionally grep the release where the
# protected UI behavior was introduced. Runtime release is v1.04.
# APP_VERSION = "5.2.8"
# APP_VERSION = "5.3.2"

SUPPORTED_SUFFIXES = {
    ".pdf",
    ".docx",
    ".pptx",
    ".ppt",
    ".xlsx",
    ".txt",
    ".png",
    ".jpg",
    ".jpeg",
}

JSON_FILE_TYPES = [
    (
        "Document Ingestion JSON",
        "*.json",
    ),
    (
        "All Files",
        "*.*",
    ),
]


SUPPORTED_FILE_TYPES = [
    (
        "Supported Documents",
        (
            "*.pdf *.docx *.pptx *.ppt *.xlsx "
            "*.txt *.png *.jpg *.jpeg"
        ),
    ),
    (
        "PDF Documents",
        "*.pdf",
    ),
    (
        "Word Documents",
        "*.docx",
    ),
    (
        "PowerPoint Documents",
        "*.pptx *.ppt",
    ),
    (
        "Excel Documents",
        "*.xlsx",
    ),
    (
        "Text Documents",
        "*.txt",
    ),
    (
        "Image Documents (OCR)",
        "*.png *.jpg *.jpeg",
    ),
    (
        "All Files",
        "*.*",
    ),
]


# ============================================================
# Runtime Path
# ============================================================


def get_base_directory() -> Path:
    """
    Python:
        Project root

    PyInstaller:
        EXE directory
    """

    if getattr(
        sys,
        "frozen",
        False,
    ):
        return Path(
            sys.executable
        ).resolve().parent

    return Path(
        __file__
    ).resolve().parents[2]


BASE_DIR = get_base_directory()

LOG_DIR = (
    USER_CONFIG_DIR
    / "logs"
)

DEFAULT_OUTPUT_DIR = (
    BASE_DIR
    / "output"
)


# ============================================================
# Logging
# ============================================================


def configure_gui_logging() -> logging.Logger:
    """
    GUI 独立日志。

    文件：
        logs/gui.log

    支持日志轮转：
        5 MB × 5
    """

    LOG_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    logger = logging.getLogger(
        "document_ingestion.gui"
    )

    logger.setLevel(
        logging.INFO
    )

    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        (
            "%(asctime)s | "
            "%(levelname)s | "
            "%(name)s | "
            "%(message)s"
        )
    )

    file_handler = RotatingFileHandler(
        filename=(
            LOG_DIR
            / "gui.log"
        ),
        maxBytes=(
            5
            * 1024
            * 1024
        ),
        backupCount=5,
        encoding="utf-8",
    )

    file_handler.setFormatter(
        formatter
    )

    logger.addHandler(
        file_handler
    )

    return logger


LOGGER = configure_gui_logging()


# ============================================================
# Data Models
# ============================================================


@dataclass(frozen=True)
class PostgreSQLSettings:
    host: str
    port: int
    database: str
    user: str
    connect_timeout: int


@dataclass(frozen=True)
class ProcessingOptions:
    output_directory: Path
    save_json: bool
    save_database: bool
    project_code: str
    database_config: DatabaseConfig | None = None


@dataclass(frozen=True)
class FileFailure:
    file_path: Path
    error_type: str
    error_message: str


@dataclass(frozen=True)
class BatchSummary:
    success: int
    failed: int
    total: int
    warnings: int = 0
    cancelled: bool = False


# ============================================================
# GUI
# ============================================================


class DocumentIngestionGUI:
    """
    Desktop GUI for Document Intelligence Console Enterprise.

    GUI 层只负责：

        - 文件选择
        - 参数输入
        - 用户交互
        - 进度显示
        - 日志显示
        - 后台任务调度

    实际文档解析仍交给：

        PipelineFactory
    """

    def __init__(
        self,
        root: tk.Tk,
    ) -> None:

        self.root = root

        self.selected_files: list[
            Path
        ] = []

        self.selected_json_files: list[
            Path
        ] = []

        self.json_import_failures: list[
            FileFailure
        ] = []

        self.json_import_thread: (
            threading.Thread
            | None
        ) = None

        self.failures: list[
            FileFailure
        ] = []

        self.event_queue: queue.Queue[
            tuple[str, Any]
        ] = queue.Queue()

        self.processing_thread: (
            threading.Thread
            | None
        ) = None

        self.postgres_test_thread: (
            threading.Thread
            | None
        ) = None

        self.postgres_schema_thread: (
            threading.Thread
            | None
        ) = None

        self.qabug_admin_thread: (
            threading.Thread
            | None
        ) = None
        self.qabug_import_thread: (
            threading.Thread
            | None
        ) = None

        self.version_history_thread: (
            threading.Thread
            | None
        ) = None
        self.relation_audit_thread: (
            threading.Thread
            | None
        ) = None
        self._relation_rows: list[dict[str, Any]] = []
        self._relation_by_iid: dict[str, dict[str, Any]] = {}
        self._relation_evidence_by_iid: dict[str, dict[str, Any]] = {}
        self._relation_evidence_group_by_iid: dict[str, dict[str, Any]] = {}
        self._selected_relation_id: int | None = None
        self._visible_relation_rows: list[dict[str, Any]] = []
        self._relation_graph_root_by_label: dict[str, str | None] = {"Auto (recommended)": EngineeringRelationGraphService.AUTO_ROOT_ID}
        self._relation_graph_node_items: dict[int, str] = {}
        self._relation_graph_edge_items: dict[int, int] = {}
        self._relation_graph_zoom: float = 1.0
        self._relation_graph_zoom_min: float = 0.35
        self._relation_graph_zoom_max: float = 2.50
        self._relation_graph_last_root_base_point: tuple[float, float] | None = None
        self._relation_graph_manual_positions: dict[str, tuple[float, float]] = {}
        self._relation_graph_last_positions: dict[str, tuple[float, float]] = {}
        self._relation_graph_node_tag_by_id: dict[str, str] = {}
        self._relation_graph_drag_state: tuple[str, float, float, float, float] | None = None
        self._relation_graph_tooltip: tk.Toplevel | None = None
        self._relation_graph_tooltip_label: ttk.Label | None = None
        self._manual_relation_spec_by_label: dict[str, dict[str, Any]] = {}
        self._manual_relation_spec_label_by_id: dict[int, str] = {}
        self._manual_relation_rows_by_iid: dict[str, dict[str, Any]] = {}
        self._selected_manual_relation_id: int | None = None
        self._version_catalog_by_iid: dict[str, dict[str, Any]] = {}
        self._version_rows_by_label: dict[str, dict[str, Any]] = {}
        # Semantic business-version order returned by SpecificationHistoryService.
        # Newest -> oldest.  The compare selectors use this list to make invalid
        # directions impossible to select in the UI.
        self._version_labels_desc: list[str] = []
        self._version_change_by_iid: dict[str, dict[str, Any]] = {}
        self._selected_specification_id: int | None = None
        self._version_catalog_rows: list[dict[str, Any]] = []
        self._spec_display_to_id: dict[str, int] = {}
        self._spec_id_to_display: dict[int, str] = {}
        self._change_timeline_by_iid: dict[str, dict[str, Any]] = {}
        self._change_detail_by_iid: dict[str, dict[str, Any]] = {}
        self._change_detail_rows: list[dict[str, Any]] = []
        self._selected_change_set_id: int | None = None
        self._suppress_spec_tree_event = False
        self._spec_context_generation = 0
        self._last_ingest_specification_key: str | None = None
        self._last_ingest_specification_name: str | None = None
        self._last_ingest_version: str | None = None
        self._pending_open_spec_key: str | None = None
        self._pending_open_spec_page: str | None = None

        # Enterprise shell navigation state. The sidebar uses one stable,
        # scrollable Treeview with collapsible groups. This prevents vertical
        # overflow without rebuilding the sidebar or disturbing heavy pages.
        self._sidebar_page_to_group: dict[str, str] = {}
        self._sidebar_open_group = "AI WORKSPACE"
        self._current_page_key = "ai_chat"

        self.postgres_connection_verified = False

        # Tracks the currently active processing scope so any scope switch
        # (21MM / 24MM / Common) invalidates a pending batch selected under
        # the previous scope.
        self._last_selected_project_code: str | None = None

        self.is_processing = False

        self.cancel_requested = (
            threading.Event()
        )

        self._configure_window()

        self._configure_styles()

        self._create_variables()

        self._build_ui()

        self._bind_postgres_setting_traces()

        self._poll_events()

        # Start the dedicated API lazily after rendering the desktop shell.
        # It is a detached process and survives closing the GUI.
        self.root.after(500, self._ensure_local_backend_running)

        LOGGER.info(
            "GUI application started | "
            "version=%s | "
            "base_dir=%s",
            APP_VERSION,
            BASE_DIR,
        )

    # ========================================================
    # Window
    # ========================================================

    def _configure_window(
        self,
    ) -> None:

        # Keep the title bar product-only. Build version remains available in
        # diagnostics/About/logs but is intentionally not shown in the window.
        self.root.title(APP_NAME)

        self.root.geometry(
            "1900x1080"
        )

        self.root.minsize(
            1440,
            850,
        )

        self.root.protocol(
            "WM_DELETE_WINDOW",
            self._on_close,
        )

    def _configure_styles(
        self,
    ) -> None:
        """
        Configure a modern light desktop appearance.

        Business logic is intentionally untouched in this step.
        """

        style = ttk.Style()

        style.configure(
            "AppHeader.TLabel",
            font=(
                "Microsoft YaHei UI",
                22,
                "bold",
            ),
        )

        style.configure(
            "AppSubtitle.TLabel",
            font=(
                "Microsoft YaHei UI",
                10,
            ),
        )

        style.configure(
            "Card.TLabelframe.Label",
            font=(
                "Microsoft YaHei UI",
                10,
                "bold",
            ),
        )

        style.configure(
            "Small.TLabel",
            font=(
                "Microsoft YaHei UI",
                9,
            ),
        )

        style.configure(
            "StatusBold.TLabel",
            font=(
                "Microsoft YaHei UI",
                10,
                "bold",
            ),
        )

        # Compact navigation tree: one stable widget, small rows, scrollable.
        # This replaces the old always-expanded button rail, which overflowed
        # vertically on 800-900px desktop windows.
        style.configure(
            "Sidebar.Treeview",
            font=("Microsoft YaHei UI", 9),
            rowheight=27,
            borderwidth=0,
        )

    # ========================================================
    # Variables
    # ========================================================

    def _create_variables(
        self,
    ) -> None:

        self.project_var = tk.StringVar(value="")
        self.project_target_database_var = tk.StringVar(
            value="Select a project to determine the target database."
        )

        self.output_directory_var = (
            tk.StringVar(
                value=str(
                    DEFAULT_OUTPUT_DIR
                )
            )
        )

        self.save_json_var = (
            tk.BooleanVar(
                value=True
            )
        )

        self.save_database_var = (
            tk.BooleanVar(
                value=False
            )
        )

        self.status_var = (
            tk.StringVar(
                value="Ready"
            )
        )

        self.current_file_var = (
            tk.StringVar(
                value="No file selected."
            )
        )

        self.progress_var = (
            tk.DoubleVar(
                value=0
            )
        )

        self.progress_text_var = (
            tk.StringVar(
                value="0%"
            )
        )

        self.summary_var = (
            tk.StringVar(
                value=(
                    "Success: 0    "
                    "Failed: 0    "
                    "Total: 0"
                )
            )
        )

        self.file_count_var = (
            tk.StringVar(
                value=(
                    "0 files selected"
                )
            )
        )

        self.json_file_count_var = (
            tk.StringVar(
                value=(
                    "0 JSON files selected"
                )
            )
        )

        # JSON -> PostgreSQL uses PostgreSQL Settings as the single source of
        # truth for its target.  These variables are read-only presentation
        # fields on the import page; there is deliberately no second scope /
        # database selector there.
        self.json_import_target_scope_var = tk.StringVar(
            value="Not selected"
        )
        self.json_import_target_database_var = tk.StringVar(
            value="Not configured"
        )
        self.json_import_target_host_var = tk.StringVar(
            value="Not configured"
        )

        self.json_import_status_var = (
            tk.StringVar(
                value="Ready"
            )
        )

        self.json_import_current_file_var = (
            tk.StringVar(
                value=(
                    "Select one or more JSON files."
                )
            )
        )

        self.json_import_progress_var = (
            tk.DoubleVar(
                value=0
            )
        )

        self.json_import_progress_text_var = (
            tk.StringVar(
                value="0%"
            )
        )

        self.json_import_summary_var = (
            tk.StringVar(
                value=(
                    "Success: 0    "
                    "Failed: 0    "
                    "Total: 0"
                )
            )
        )

        # Specification version history / deterministic diff page.
        self.version_target_scope_var = tk.StringVar(value="Not selected")
        self.version_target_database_var = tk.StringVar(value="Not configured")
        self.version_target_host_var = tk.StringVar(value="Not configured")
        self.version_status_var = tk.StringVar(value="Select a scope and refresh.")
        self.version_master_count_var = tk.StringVar(value="0 specifications")
        self.version_selected_master_var = tk.StringVar(value="No specification selected")
        self.version_compare_from_var = tk.StringVar(value="")
        self.version_compare_to_var = tk.StringVar(value="")
        self.version_compare_summary_var = tk.StringVar(value="Select two versions to compare.")
        self.version_added_var = tk.StringVar(value="Added 0")
        self.version_modified_var = tk.StringVar(value="Modified 0")
        self.version_removed_var = tk.StringVar(value="Removed 0")
        self.version_moved_var = tk.StringVar(value="Moved 0")
        self.version_diff_granularity_var = tk.StringVar(value="WORD")
        self.version_change_filter_var = tk.StringVar(value="ALL")
        self.version_change_visible_var = tk.StringVar(value="0 shown")
        self._version_compare_changes: list[dict[str, Any]] = []
        self._diff_mode_buttons: list[tuple[tk.Button, str]] = []
        self._version_filter_buttons: list[tuple[tk.Button, str]] = []
        self._change_center_filter_buttons: list[tuple[tk.Button, str]] = []

        # v5.1 Specification Management Center shared state.
        self.spec_catalog_search_var = tk.StringVar(value="")
        self.spec_catalog_type_var = tk.StringVar(value="All")
        self.spec_catalog_region_var = tk.StringVar(value="All")
        self.spec_catalog_status_var = tk.StringVar(value="All")
        self.spec_catalog_visible_count_var = tk.StringVar(value="0 shown")
        self.spec_active_selector_var = tk.StringVar(value="")
        self.spec_detail_name_var = tk.StringVar(value="No specification selected")
        self.spec_detail_key_var = tk.StringVar(value="-")
        self.spec_detail_current_var = tk.StringVar(value="-")
        self.spec_detail_versions_var = tk.StringVar(value="0")
        self.spec_detail_history_var = tk.StringVar(value="0")
        self.spec_detail_type_var = tk.StringVar(value="-")
        self.spec_detail_region_var = tk.StringVar(value="-")
        self.spec_detail_status_var = tk.StringVar(value="-")
        self.spec_detail_updated_var = tk.StringVar(value="-")
        self.change_center_status_var = tk.StringVar(value="Select a specification to review its change timeline.")
        self.change_center_summary_var = tk.StringVar(value="No change set selected")
        self.change_type_filter_var = tk.StringVar(value="ALL")
        self.change_search_var = tk.StringVar(value="")
        self.last_ingest_result_var = tk.StringVar(value="No versioned specification imported in this session.")

        postgres_profile = (
            self._read_postgres_profile()
        )

        self.postgres_host_var = (
            tk.StringVar(
                value=postgres_profile[
                    "host"
                ]
            )
        )

        self.postgres_port_var = (
            tk.StringVar(
                value=str(
                    postgres_profile[
                        "port"
                    ]
                )
            )
        )

        self._project_database_names = dict(
            postgres_profile.get("project_databases", {})
        )
        for definition in ProjectRegistry.PROJECTS.values():
            self._project_database_names.setdefault(
                definition.code,
                definition.database_default,
            )

        # The GUI exposes one Database field.  Its value changes with the
        # selected processing scope and is remembered independently per scope.
        self.postgres_database_var = tk.StringVar(
            value=postgres_profile.get("database", "rag") or "rag"
        )

        self.postgres_user_var = (
            tk.StringVar(
                value=postgres_profile[
                    "user"
                ]
            )
        )

        self.postgres_timeout_var = (
            tk.StringVar(
                value=str(
                    postgres_profile[
                        "connect_timeout"
                    ]
                )
            )
        )

        # Password fields are intentionally never populated from disk.
        self.postgres_password_var = (
            tk.StringVar(
                value=""
            )
        )

        self.postgres_remember_password_var = (
            tk.BooleanVar(
                value=False
            )
        )

        self.postgres_connection_status_var = (
            tk.StringVar(
                value=(
                    "Not tested"
                )
            )
        )

        self.postgres_schema_status_var = (
            tk.StringVar(
                value=(
                    "RAG schema not verified"
                )
            )
        )
        self.qabug_admin_status_var = tk.StringVar(
            value="QA/BUG：未初始化 / 未验证"
        )

        self.postgres_password_status_var = (
            tk.StringVar(
                value=(
                    "Password available for this session"
                    if os.getenv(
                        "POSTGRES_PASSWORD"
                    )
                    else (
                        "Password is not configured"
                    )
                )
            )
        )

        self.postgres_config_path_var = (
            tk.StringVar(
                value=str(
                    USER_POSTGRES_CONFIG_FILE
                )
            )
        )

        console_profile = self._read_console_profile()
        self.qdrant_url_var = tk.StringVar(
            value=str(console_profile.get("qdrant_url", "http://127.0.0.1:6333"))
        )
        self.qdrant_timeout_var = tk.StringVar(
            value=str(console_profile.get("qdrant_timeout", 10.0))
        )
        qdrant_account = self.qdrant_url_var.get().strip() or "http://127.0.0.1:6333"
        self.qdrant_api_key_var = tk.StringVar(
            value=get_secret("qdrant", qdrant_account, "QDRANT_API_KEY")
        )
        self.qdrant_remember_key_var = tk.BooleanVar(value=bool(self.qdrant_api_key_var.get()))
        self.qdrant_status_var = tk.StringVar(value="Qdrant: not tested")
        self.console_page_size_var = tk.StringVar(
            value=str(console_profile.get("page_size", 100))
        )
        # Kept only for backward-compatible config reads. v5.1.3 uses a
        # permanently mapped navigation rail, so no group is opened/closed.
        self._sidebar_start_group = str(
            console_profile.get("sidebar_open_group", "AI WORKSPACE")
        ).strip().upper() or "AI WORKSPACE"

        self.global_status_var = tk.StringVar(value="Ready")
        self.current_page_var = tk.StringVar(value=zh("Enterprise AI Agent"))

    @staticmethod
    def _read_console_profile() -> dict[str, Any]:
        defaults: dict[str, Any] = {
            "qdrant_url": "http://127.0.0.1:6333",
            "qdrant_timeout": 10.0,
            "page_size": 100,
            "sidebar_open_group": "AI WORKSPACE",
        }
        candidates = [
            USER_POSTGRES_CONFIG_FILE,
            LEGACY_USER_POSTGRES_CONFIG_FILE,
            Path(__file__).resolve().parents[2] / "config" / "config.yaml",
        ]
        for path in candidates:
            try:
                if not path.is_file():
                    continue
                raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                if not isinstance(raw, dict):
                    continue
                qdrant = raw.get("qdrant") or {}
                console = raw.get("console") or {}
                if isinstance(qdrant, dict):
                    url = qdrant.get("url")
                    timeout = qdrant.get("timeout")
                    if isinstance(url, str) and url.strip():
                        defaults["qdrant_url"] = url.strip()
                    try:
                        if float(timeout) > 0:
                            defaults["qdrant_timeout"] = float(timeout)
                    except (TypeError, ValueError):
                        pass
                if isinstance(console, dict):
                    try:
                        size = int(console.get("page_size", defaults["page_size"]))
                        defaults["page_size"] = max(10, min(size, 1000))
                    except (TypeError, ValueError):
                        pass
                    group = str(console.get("sidebar_open_group", "AI WORKSPACE")).strip().upper()
                    if group in {"AI WORKSPACE", "INGESTION", "SPECIFICATION", "DATA", "RAG & VECTOR", "ADMINISTRATION"}:
                        defaults["sidebar_open_group"] = group
                break
            except (OSError, yaml.YAMLError):
                continue
        return defaults

    # ========================================================
    # Main UI
    # ========================================================

    def _create_scrollable_page(self, parent: ttk.Frame) -> tuple[ttk.Frame, ttk.Frame, tk.Canvas]:
        """Create a right-side page with a persistent vertical scrollbar.

        The application used to place every page directly in the right content
        host. On 1280x800 / laptop-height displays, lower controls could be
        clipped with no way to reach them. The outer Canvas gives every page a
        vertical viewport while the inner ttk.Frame preserves the existing page
        builders and their pack/grid contracts.
        """
        host = ttk.Frame(parent)
        host.rowconfigure(0, weight=1)
        host.columnconfigure(0, weight=1)

        style = ttk.Style()
        background = style.lookup("TFrame", "background") or self.root.cget("background")
        canvas = tk.Canvas(
            host,
            highlightthickness=0,
            borderwidth=0,
            background=background,
        )
        scrollbar = ttk.Scrollbar(host, orient=VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        page = ttk.Frame(canvas, padding=(10, 8, 10, 8))
        window_id = canvas.create_window((0, 0), window=page, anchor="nw")

        def sync_geometry(_event: Any | None = None) -> None:
            # Defer until all child geometry requests for the current event have
            # settled. This keeps fill/expand pages at least viewport-height and
            # allows taller pages to grow naturally into the scroll region.
            def apply() -> None:
                if not canvas.winfo_exists() or not page.winfo_exists():
                    return
                viewport_w = max(1, canvas.winfo_width())
                viewport_h = max(1, canvas.winfo_height())
                requested_h = max(1, page.winfo_reqheight())
                content_h = max(viewport_h, requested_h)
                canvas.itemconfigure(window_id, width=viewport_w, height=content_h)
                canvas.configure(scrollregion=(0, 0, viewport_w, content_h))

            self.root.after_idle(apply)

        page.bind("<Configure>", sync_geometry, add="+")
        canvas.bind("<Configure>", sync_geometry, add="+")
        canvas.bind(
            "<MouseWheel>",
            lambda event: canvas.yview_scroll(int(-1 * (event.delta / 120)), "units"),
            add="+",
        )
        return host, page, canvas

    def _build_ui(self) -> None:
        """Build the enterprise shell with a compact, scrollable navigation tree."""
        shell = ttk.Frame(self.root, padding=0)
        shell.pack(fill=BOTH, expand=True)

        self._build_unified_header(shell)

        body = ttk.Frame(shell, padding=(10, 8, 12, 8))
        body.pack(fill=BOTH, expand=True)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)
        self._body_frame = body

        sidebar = ttk.Frame(body, padding=(8, 4, 8, 4), width=238)
        sidebar.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        sidebar.grid_propagate(False)
        self._sidebar_frame = sidebar

        content = ttk.Frame(body)
        content.grid(row=0, column=1, sticky="nsew")
        content.rowconfigure(0, weight=1)
        content.columnconfigure(0, weight=1)

        self._pages: dict[str, ttk.Frame] = {}
        self._page_hosts: dict[str, ttk.Frame] = {}
        self._page_scroll_canvases: dict[str, tk.Canvas] = {}
        self._nav_buttons: dict[str, ttk.Button] = {}  # legacy compatibility; no longer populated
        self._nav_items: dict[str, str] = {}
        self._sidebar_group_items: dict[str, str] = {}

        page_defs = {
            "ai_chat": "Enterprise AI Agent",
            "ai_settings": "AI & RAG Configuration",
            "ai_status": "AI Service Status",
            "ai_audit": "AI Query Audit",
            "document_import": "Document Import",
            "json_import": "JSON Import",
            "spec_catalog": "Specification Catalog",
            "spec_versions": "Version History",
            "spec_compare": "Version Compare",
            "spec_changes": "Change Center",
            "spec_relations": "Engineering Relations",
            "database": "Database Explorer",
            "sql": "SQL Editor",
            "visual": "Visual Query",
            "erd": "ER Diagram",
            "documents": "Document Explorer",
            "embedding": "Embedding Monitor",
            "qdrant_sync": "SQL → Qdrant Sync",
            "qdrant": "Qdrant",
            "settings": "Settings",
        }
        for key in page_defs:
            if key == "ai_chat":
                # The AI workspace is a full-height, three-pane chat surface.
                # Do not wrap it in the generic vertical scrolling Canvas.
                host = ttk.Frame(content)
                frame = ttk.Frame(host, padding=0)
                frame.pack(fill=BOTH, expand=True)
                canvas = None
            else:
                host, frame, canvas = self._create_scrollable_page(content)
            host.grid(row=0, column=0, sticky="nsew")
            self._page_hosts[key] = host
            self._pages[key] = frame
            if canvas is not None:
                self._page_scroll_canvases[key] = canvas

        self._build_sidebar(sidebar, page_defs)

        self._build_document_conversion_page(self._pages["document_import"])
        self._build_json_import_page(self._pages["json_import"])
        self._build_specification_catalog_page(self._pages["spec_catalog"])
        self._build_specification_versions_page(self._pages["spec_versions"])
        self._build_specification_compare_page(self._pages["spec_compare"])
        self._build_specification_changes_page(self._pages["spec_changes"])
        self._build_engineering_relations_page(self._pages["spec_relations"])
        self._build_postgres_settings_page(self._pages["settings"])

        self.enterprise_module = DataConsoleModule(
            self,
            {
                "database": self._pages["database"],
                "sql": self._pages["sql"],
                "visual": self._pages["visual"],
                "erd": self._pages["erd"],
                "documents": self._pages["documents"],
                "embedding": self._pages["embedding"],
                "qdrant_sync": self._pages["qdrant_sync"],
                "qdrant": self._pages["qdrant"],
            },
        )

        self.ai_module = AIWorkspaceModule(
            self,
            {
                "ai_chat": self._pages["ai_chat"],
                "ai_settings": self._pages["ai_settings"],
                "ai_status": self._pages["ai_status"],
                "ai_audit": self._pages["ai_audit"],
            },
            USER_CONFIG_DIR,
        )

        self._build_app_status_bar(shell)
        self.navigate("ai_chat")

    def _build_unified_header(self, parent: ttk.Frame) -> None:
        # Compact header: keep only the product name and processing scope.
        # The old capability subtitle and visible version badge consumed useful
        # vertical/horizontal space on 1366x768 / 1600x900 engineering laptops.
        header = ttk.Frame(parent, padding=(20, 8, 20, 8))
        header.pack(fill=X)

        brand = ttk.Frame(header)
        brand.pack(side=LEFT, fill=X, expand=True)
        ttk.Label(
            brand,
            text=APP_NAME,
            style="AppHeader.TLabel",
            bootstyle="dark",
        ).pack(anchor="w")

        context = ttk.Frame(header)
        context.pack(side=RIGHT, padx=(20, 0))
        ttk.Label(context, text="SQL 库 / 处理范围", font=("Microsoft YaHei UI", 9, "bold")).grid(
            row=0, column=0, sticky="e", padx=(0, 8)
        )
        self.header_project_combobox = ttk.Combobox(
            context,
            textvariable=self.project_var,
            values=ProjectRegistry.display_values(),
            state="readonly",
            width=13,
            bootstyle="primary",
        )
        self.header_project_combobox.grid(row=0, column=1, sticky="ew")
        self.header_project_combobox.bind("<<ComboboxSelected>>", self._on_project_selected)
        ttk.Label(context, textvariable=self.project_target_database_var, bootstyle="secondary").grid(
            row=1, column=0, columnspan=2, sticky="e", pady=(4, 0)
        )
        # APP_VERSION remains available in About/build diagnostics, but the
        # header badge is intentionally omitted to preserve workspace width.

        ttk.Separator(parent, orient="horizontal", bootstyle="secondary").pack(fill=X)

    def _build_sidebar(self, parent: ttk.Frame, page_defs: dict[str, str]) -> None:
        """Build a compact hierarchical navigation tree.

        The previous button rail permanently displayed every page, so the
        navigation exceeded the available height on normal 800-900px windows.
        A Treeview keeps the geometry stable while allowing groups to collapse
        and the entire rail to scroll. Only the active group is expanded
        automatically; no application page is removed.
        """
        ttk.Label(
            parent,
            text="NAVIGATION",
            bootstyle="secondary",
            font=("Microsoft YaHei UI", 8, "bold"),
        ).pack(fill=X, padx=4, pady=(0, 5))

        nav_host = ttk.Frame(parent)
        nav_host.pack(fill=BOTH, expand=True)

        tree = ttk.Treeview(
            nav_host,
            show="tree",
            selectmode="browse",
            style="Sidebar.Treeview",
            bootstyle="primary",
        )
        scrollbar = ttk.Scrollbar(nav_host, orient=VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.column("#0", width=205, minwidth=155, stretch=True)
        tree.pack(side=LEFT, fill=BOTH, expand=True)
        scrollbar.pack(side=RIGHT, fill=Y)
        self._sidebar_tree = tree

        groups = [
            ("AI WORKSPACE", [
                ("ai_chat", "Enterprise AI Agent"),
                ("ai_settings", "AI & RAG Configuration"),
                ("ai_status", "AI Service Status"),
                ("ai_audit", "AI Query Audit"),
            ]),
            ("INGESTION", [
                ("document_import", "Document Import"),
                ("json_import", "JSON → PostgreSQL"),
            ]),
            ("SPECIFICATION", [
                ("spec_catalog", "Specification Catalog"),
                ("spec_versions", "Version History"),
                ("spec_compare", "Version Compare"),
                ("spec_changes", "Change Center"),
                ("spec_relations", "Engineering Relations"),
            ]),
            ("DATA", [
                ("database", "Database Explorer"),
                ("sql", "SQL Editor"),
                ("visual", "Visual Query"),
                ("erd", "ER Diagram"),
            ]),
            ("RAG & VECTOR", [
                ("documents", "Document Explorer"),
                ("qdrant_sync", "SQL → Qdrant Sync"),
            ]),
            ("ADMINISTRATION", [("settings", "Settings")]),
        ]

        start_group = self._sidebar_start_group or self._sidebar_open_group or "AI WORKSPACE"
        for group_name, items in groups:
            group_iid = f"group::{group_name}"
            tree.insert(
                "",
                END,
                iid=group_iid,
                text=group_name,
                open=(group_name == start_group),
                tags=("sidebar-group",),
            )
            self._sidebar_group_items[group_name] = group_iid
            for key, label in items:
                item_iid = f"page::{key}"
                tree.insert(group_iid, END, iid=item_iid, text=label, tags=("sidebar-page",))
                self._nav_items[key] = item_iid
                self._sidebar_page_to_group[key] = group_name

        tree.tag_configure("sidebar-group", font=("Microsoft YaHei UI", 8, "bold"))
        tree.tag_configure("sidebar-page", font=("Microsoft YaHei UI", 9))
        tree.bind("<<TreeviewSelect>>", self._on_sidebar_tree_selected)
        tree.bind("<<TreeviewOpen>>", self._on_sidebar_group_opened)

    def _on_sidebar_tree_selected(self, _event: Any | None = None) -> None:
        """Navigate only when a leaf page item is selected."""
        selection = self._sidebar_tree.selection()
        if not selection:
            return
        iid = selection[0]
        if not iid.startswith("page::"):
            return
        page_key = iid.split("::", 1)[1]
        if page_key == self._current_page_key:
            return
        self.navigate(page_key)

    def _on_sidebar_group_opened(self, _event: Any | None = None) -> None:
        """Keep the navigation compact by collapsing sibling groups."""
        selected = self._sidebar_tree.focus()
        if not selected.startswith("group::"):
            return
        group_name = selected.split("::", 1)[1]
        self._sidebar_open_group = group_name
        # Run after Tk has applied the requested open state.
        self.root.after_idle(lambda name=group_name: self._collapse_other_sidebar_groups(name))

    def _collapse_other_sidebar_groups(self, keep_group: str) -> None:
        for group_name, iid in self._sidebar_group_items.items():
            if group_name != keep_group:
                self._sidebar_tree.item(iid, open=False)

    def _sync_sidebar_selection_for_page(self, page_key: str) -> None:
        """Select the active page and expand exactly its navigation group."""
        group_name = self._sidebar_page_to_group.get(page_key, "AI WORKSPACE")
        self._sidebar_open_group = group_name
        group_iid = self._sidebar_group_items.get(group_name)
        if group_iid:
            self._sidebar_tree.item(group_iid, open=True)
            self._collapse_other_sidebar_groups(group_name)
        item_iid = self._nav_items.get(page_key)
        if item_iid:
            self._sidebar_tree.selection_set(item_iid)
            self._sidebar_tree.focus(item_iid)
            self._sidebar_tree.see(item_iid)

    def navigate(self, page_key: str) -> None:
        frame = self._pages.get(page_key)
        host = self._page_hosts.get(page_key)
        if frame is None or host is None:
            return
        self._current_page_key = page_key
        self._sync_sidebar_selection_for_page(page_key)
        host.tkraise()
        labels = {
            "ai_chat": "Enterprise AI Agent", "ai_audit": "AI Query Audit",
            "ai_settings": "AI & RAG Configuration", "ai_status": "AI Service Status",
            "document_import": "Document Import",
            "json_import": "JSON → PostgreSQL", "spec_catalog": "Specification Catalog",
            "spec_versions": "Version History", "spec_compare": "Version Compare",
            "spec_changes": "Change Center", "spec_relations": "Engineering Relations",
            "database": "Database Explorer", "sql": "SQL Editor",
            "visual": "Visual Query", "erd": "ER Diagram",
            "documents": "Document Explorer", "embedding": "Embedding Monitor",
            "qdrant_sync": "SQL → Qdrant Sync", "qdrant": "Qdrant Browser", "settings": "Settings",
        }
        self.current_page_var.set(zh(labels.get(page_key, page_key)))
        if page_key.startswith("spec_"):
            if not self._version_catalog_rows and not (self.version_history_thread and self.version_history_thread.is_alive()):
                self.root.after_idle(self.refresh_version_catalog)
            elif page_key == "spec_changes" and self._selected_specification_id is not None:
                self.root.after_idle(self.refresh_change_timeline)
            elif page_key == "spec_relations":
                self.root.after_idle(self.refresh_engineering_relations)

        # Administration pages are live views and share the active Processing Scope.
        if hasattr(self, "enterprise_module"):
            self.root.after_idle(
                lambda target=page_key: self.enterprise_module.activate_page(target)
            )
        if hasattr(self, "ai_module"):
            self.root.after_idle(
                lambda target=page_key: self.ai_module.activate_page(target)
            )

    def _build_app_status_bar(self, parent: ttk.Frame) -> None:
        ttk.Separator(parent, orient="horizontal", bootstyle="secondary").pack(fill=X)
        bar = ttk.Frame(parent, padding=(14, 6, 14, 7))
        bar.pack(fill=X)
        ttk.Label(bar, textvariable=self.current_page_var, font=("Microsoft YaHei UI", 9, "bold")).pack(side=LEFT)
        ttk.Label(bar, textvariable=self.global_status_var, bootstyle="secondary").pack(side=RIGHT)


    def _build_document_conversion_page(
        self,
        parent: ttk.Frame,
    ) -> None:
        """
        Build the existing document conversion workspace.

        Business flow:
            PDF / DOCX / PPTX / PPT / XLSX / TXT / PNG / JPG
                -> Pipeline
                -> JSON / PostgreSQL
        """

        workspace = ttk.Frame(
            parent
        )

        workspace.pack(
            fill=BOTH,
            expand=True,
        )

        workspace.columnconfigure(
            0,
            weight=5,
            uniform="main-columns",
        )

        workspace.columnconfigure(
            1,
            weight=4,
            uniform="main-columns",
        )

        workspace.columnconfigure(
            2,
            weight=5,
            uniform="main-columns",
        )

        workspace.rowconfigure(
            0,
            weight=1,
        )

        left_panel = ttk.Frame(
            workspace
        )

        left_panel.grid(
            row=0,
            column=0,
            sticky="nsew",
            padx=(
                0,
                8,
            ),
        )

        center_panel = ttk.Frame(
            workspace
        )

        center_panel.grid(
            row=0,
            column=1,
            sticky="nsew",
            padx=8,
        )

        right_panel = ttk.Frame(
            workspace
        )

        right_panel.grid(
            row=0,
            column=2,
            sticky="nsew",
            padx=(
                8,
                0,
            ),
        )

        self._build_input_section(
            left_panel
        )

        self._build_output_section(
            center_panel
        )

        self._build_options_section(
            center_panel
        )

        self._build_action_section(
            center_panel
        )

        self._build_progress_section(
            right_panel
        )

        self._build_log_section(
            right_panel
        )

    # ========================================================
    # Header
    # ========================================================

    def _build_header(
        self,
        parent: ttk.Frame,
    ) -> None:

        frame = ttk.Frame(
            parent
        )

        frame.pack(
            fill=X,
            pady=(
                0,
                14,
            ),
        )

        title_area = ttk.Frame(
            frame
        )

        title_area.pack(
            side=LEFT,
            fill=X,
            expand=True,
        )

        ttk.Label(
            title_area,
            text=APP_NAME,
            style="AppHeader.TLabel",
            bootstyle="dark",
        ).pack(
            anchor="w"
        )

        ttk.Label(
            title_area,
            text=(
                "Document conversion · Structured JSON · "
                "PostgreSQL storage"
            ),
            style="AppSubtitle.TLabel",
            bootstyle="secondary",
        ).pack(
            anchor="w",
            pady=(
                4,
                0,
            ),
        )

        info_area = ttk.Frame(
            frame
        )

        info_area.pack(
            side=RIGHT,
            anchor="ne",
        )

        ttk.Label(
            info_area,
            text=(
                "PDF   DOCX   PPTX/PPT   XLSX   TXT   PNG/JPG"
            ),
            bootstyle="secondary",
        ).pack(
            anchor="e"
        )

        ttk.Label(
            info_area,
            text=(
                f"v{APP_VERSION}"
            ),
            bootstyle="primary",
            font=(
                "Microsoft YaHei UI",
                9,
                "bold",
            ),
        ).pack(
            anchor="e",
            pady=(
                5,
                0,
            ),
        )

    # ========================================================
    # Input Files
    # ========================================================

    def _build_input_section(
        self,
        parent: ttk.Frame,
    ) -> None:

        frame = ttk.LabelFrame(
            parent,
            text="1. Input Files",
            padding=14,
            style="Card.TLabelframe",
            bootstyle="secondary",
        )

        frame.pack(
            fill=BOTH,
            expand=True,
        )

        project_frame = ttk.Frame(frame)
        project_frame.pack(fill=X, pady=(0, 12))

        ttk.Label(
            project_frame,
            text="Processing Scope (required)",
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(anchor="w", pady=(0, 6))

        self.project_combobox = ttk.Combobox(
            project_frame,
            textvariable=self.project_var,
            values=ProjectRegistry.display_values(),
            state="readonly",
            bootstyle="primary",
        )
        self.project_combobox.pack(fill=X)
        self.project_combobox.bind(
            "<<ComboboxSelected>>",
            self._on_project_selected,
        )

        ttk.Label(
            project_frame,
            textvariable=self.project_target_database_var,
            bootstyle="secondary",
            style="Small.TLabel",
        ).pack(anchor="w", pady=(5, 0))

        ttk.Separator(
            frame,
            orient="horizontal",
            bootstyle="secondary",
        ).pack(fill=X, pady=(0, 10))

        toolbar = ttk.Frame(
            frame
        )

        toolbar.pack(
            fill=X,
            pady=(
                0,
                10,
            ),
        )

        self.add_files_button = (
            ttk.Button(
                toolbar,
                text="＋  Add Files",
                command=(
                    self.select_files
                ),
                bootstyle="primary",
            )
        )

        self.add_files_button.pack(
            side=LEFT
        )

        self.add_folder_button = ttk.Button(
            toolbar,
            text="＋  Add Folder",
            command=self.select_folder_files,
            bootstyle="primary-outline",
        )
        self.add_folder_button.pack(side=LEFT, padx=(8, 0))

        self.remove_files_button = (
            ttk.Button(
                toolbar,
                text="Remove",
                command=(
                    self.remove_selected_files
                ),
                bootstyle="secondary-outline",
            )
        )

        self.remove_files_button.pack(
            side=LEFT,
            padx=(
                8,
                0,
            ),
        )

        self.clear_files_button = (
            ttk.Button(
                toolbar,
                text="Clear",
                command=(
                    self.clear_files
                ),
                bootstyle="secondary-outline",
            )
        )

        self.clear_files_button.pack(
            side=LEFT,
            padx=(
                8,
                0,
            ),
        )

        list_container = ttk.Frame(
            frame
        )

        list_container.pack(
            fill=BOTH,
            expand=True,
        )

        self.file_listbox = (
            tk.Listbox(
                list_container,
                height=24,
                selectmode=EXTENDED,
                activestyle="none",
                font=(
                    "Microsoft YaHei UI",
                    10,
                ),
                borderwidth=1,
                relief="solid",
                highlightthickness=0,
                selectborderwidth=0,
            )
        )

        scrollbar = ttk.Scrollbar(
            list_container,
            orient=VERTICAL,
            command=(
                self.file_listbox
                .yview
            ),
            bootstyle="round",
        )

        self.file_listbox.configure(
            yscrollcommand=(
                scrollbar.set
            )
        )

        self.file_listbox.pack(
            side=LEFT,
            fill=BOTH,
            expand=True,
        )

        scrollbar.pack(
            side=RIGHT,
            fill=Y,
        )

        footer = ttk.Frame(
            frame
        )

        footer.pack(
            fill=X,
            pady=(
                10,
                0,
            ),
        )

        self.supported_types_label = ttk.Label(
            footer,
            text="Supported: PDF / DOCX / PPTX / PPT / XLSX / TXT / PNG / JPG",
            bootstyle="secondary",
            style="Small.TLabel",
        )
        self.supported_types_label.pack(side=LEFT)

        ttk.Label(
            footer,
            textvariable=(
                self.file_count_var
            ),
            bootstyle="primary",
            font=(
                "Microsoft YaHei UI",
                9,
                "bold",
            ),
        ).pack(
            side=RIGHT
        )

    def _on_project_selected(self, _event: Any | None = None) -> None:
        try:
            project = ProjectRegistry.resolve(self.project_var.get())
        except ProjectRegistryError:
            self.project_target_database_var.set(
                "Select a project to determine the target database."
            )
            return

        previous_project_code = getattr(
            self,
            "_last_selected_project_code",
            None,
        )

        if (
            previous_project_code
            and previous_project_code != project.code
        ):
            self._clear_pending_files_for_project_switch(
                previous_project_code=previous_project_code,
                new_project_code=project.code,
            )

        self._last_selected_project_code = project.code

        # Processing scope owns a canonical default database name.  Always
        # auto-fill it when 21MM / 24MM / Common is selected so a stale user
        # config from an older version cannot accidentally route data to the
        # wrong database.  The field remains editable after the auto-fill; if
        # the user deliberately enters another name and saves/tests without
        # changing scope, that explicit value is still honored.
        database_name = project.database_default
        self._project_database_names[project.code] = database_name
        if self.postgres_database_var.get().strip() != database_name:
            self.postgres_database_var.set(database_name)

        self.project_target_database_var.set(
            f"Target PostgreSQL: {database_name or '(not configured)'}"
        )
        self._refresh_json_import_target_info()
        self._refresh_version_target_info()
        self.postgres_connection_verified = False
        if hasattr(self, "postgres_create_tables_button"):
            self.postgres_create_tables_button.configure(state=DISABLED)
        for name in ("qabug_setup_button", "qabug_import_qa_button", "qabug_import_bug_button", "qabug_quality_button"):
            if hasattr(self, name):
                getattr(self, name).configure(state=DISABLED)

        self._refresh_unified_import_scope_ui(project.code)

        self.global_status_var.set(
            f"Scope: {project.code} · PostgreSQL: {database_name}"
        )

        # Specification lifecycle pages are scope-bound.  Invalidate every
        # cached row/result so a late worker from the previous database can
        # never repaint the new scope with stale data.
        self._spec_context_generation += 1
        self._version_catalog_rows.clear()
        self._version_catalog_by_iid.clear()
        self._version_rows_by_label.clear()
        self._version_labels_desc.clear()
        self._change_timeline_by_iid.clear()
        self._change_detail_by_iid.clear()
        self._change_detail_rows.clear()
        self._selected_specification_id = None
        self._selected_change_set_id = None
        self._relation_rows.clear()
        self._relation_by_iid.clear()
        self._relation_evidence_by_iid.clear()
        self._selected_relation_id = None
        self.spec_active_selector_var.set("")
        self.spec_detail_name_var.set("No specification selected")
        self.spec_detail_key_var.set("-")
        self.spec_detail_current_var.set("-")
        self.spec_detail_versions_var.set("0")
        self.spec_detail_history_var.set("0")
        self.spec_detail_type_var.set("-")
        self.spec_detail_region_var.set("-")
        self.spec_detail_status_var.set("-")
        self.spec_detail_updated_var.set("-")
        if hasattr(self, "version_master_tree"):
            self.version_master_tree.delete(*self.version_master_tree.get_children())
        if hasattr(self, "version_history_tree"):
            self.version_history_tree.delete(*self.version_history_tree.get_children())
        if hasattr(self, "change_timeline_tree"):
            self.change_timeline_tree.delete(*self.change_timeline_tree.get_children())
        if hasattr(self, "change_center_tree"):
            self.change_center_tree.delete(*self.change_center_tree.get_children())
        if hasattr(self, "relation_registry_tree"):
            self.relation_registry_tree.delete(*self.relation_registry_tree.get_children())
            self._clear_relation_evidence()
        self._clear_version_change_display()
        self.change_center_status_var.set("Scope changed. Reloading specification lifecycle data…")
        if self._current_page_key in {"spec_catalog", "spec_versions", "spec_compare", "spec_changes"}:
            self.root.after_idle(self.refresh_version_catalog)
        elif self._current_page_key == "spec_relations":
            self.root.after_idle(self.refresh_engineering_relations)

        if hasattr(self, "enterprise_module"):
            self.enterprise_module.on_scope_changed()
        if hasattr(self, "ai_module"):
            self.ai_module.on_scope_changed()

    def _clear_pending_files_for_project_switch(
        self,
        *,
        previous_project_code: str,
        new_project_code: str,
    ) -> None:
        """
        Clear pending document inputs when the user changes project.

        A batch selected under one processing scope must never be silently
        reused after switching to another scope.  The clear is intentionally automatic
        and does not show a confirmation dialog.
        """

        pending_count = len(self.selected_files)

        if pending_count == 0:
            return

        self.selected_files.clear()

        if hasattr(self, "file_listbox"):
            self.file_listbox.delete(0, END)

        if hasattr(self, "file_count_var"):
            self._update_file_count()

        if hasattr(self, "current_file_var"):
            self.current_file_var.set("No file selected.")

        message = (
            f"Project changed {previous_project_code} -> {new_project_code}; "
            f"cleared {pending_count} pending input file(s)."
        )

        if hasattr(self, "status_var"):
            self.status_var.set(message)

        LOGGER.info(message)

    # ========================================================
    # Output
    # ========================================================

    def _build_output_section(
        self,
        parent: ttk.Frame,
    ) -> None:

        frame = ttk.LabelFrame(
            parent,
            text="2. Output",
            padding=14,
            style="Card.TLabelframe",
            bootstyle="secondary",
        )

        frame.pack(
            fill=X,
            pady=(
                0,
                12,
            ),
        )

        ttk.Label(
            frame,
            text="Output Directory",
            font=(
                "Microsoft YaHei UI",
                9,
                "bold",
            ),
        ).pack(
            anchor="w",
            pady=(
                0,
                6,
            ),
        )

        row = ttk.Frame(
            frame
        )

        row.pack(
            fill=X
        )

        self.output_entry = (
            ttk.Entry(
                row,
                textvariable=(
                    self.output_directory_var
                ),
                bootstyle="primary",
            )
        )

        self.output_entry.pack(
            side=LEFT,
            fill=X,
            expand=True,
            padx=(
                0,
                8,
            ),
        )

        self.output_button = (
            ttk.Button(
                row,
                text="Browse",
                command=(
                    self.select_output_directory
                ),
                bootstyle="secondary-outline",
            )
        )

        self.output_button.pack(
            side=RIGHT
        )

    # ========================================================
    # Processing Options
    # ========================================================

    def _build_options_section(
        self,
        parent: ttk.Frame,
    ) -> None:

        frame = ttk.LabelFrame(
            parent,
            text="3. Processing Options",
            padding=14,
            style="Card.TLabelframe",
            bootstyle="secondary",
        )

        frame.pack(
            fill=X,
            pady=(
                0,
                12,
            ),
        )

        ttk.Label(
            frame,
            text="Output Targets",
            font=(
                "Microsoft YaHei UI",
                9,
                "bold",
            ),
        ).pack(
            anchor="w",
            pady=(
                0,
                9,
            ),
        )

        self.json_checkbox = (
            ttk.Checkbutton(
                frame,
                text="Generate JSON",
                variable=(
                    self.save_json_var
                ),
                bootstyle="primary-round-toggle",
            )
        )

        self.json_checkbox.pack(
            anchor="w",
            pady=(
                0,
                10,
            ),
        )

        self.database_checkbox = (
            ttk.Checkbutton(
                frame,
                text="Save to PostgreSQL",
                variable=(
                    self.save_database_var
                ),
                bootstyle="primary-round-toggle",
            )
        )

        self.database_checkbox.pack(
            anchor="w"
        )

        ttk.Separator(
            frame,
            orient="horizontal",
            bootstyle="secondary",
        ).pack(
            fill=X,
            pady=(
                14,
                10,
            ),
        )

        self.output_policy_label = ttk.Label(
            frame,
            text=(
                "PostgreSQL password is requested only when database output is enabled. "
                "In JSON-only mode, files are separated by processing scope."
            ),
            bootstyle="secondary",
            wraplength=330,
            justify=LEFT,
            style="Small.TLabel",
        )
        self.output_policy_label.pack(anchor="w")

    # ========================================================
    # Actions
    # ========================================================

    def _build_action_section(
        self,
        parent: ttk.Frame,
    ) -> None:

        frame = ttk.LabelFrame(
            parent,
            text="4. Actions",
            padding=14,
            style="Card.TLabelframe",
            bootstyle="secondary",
        )

        frame.pack(
            fill=X,
        )

        self.convert_button = (
            ttk.Button(
                frame,
                text="▶  Convert",
                command=(
                    self.start_processing
                ),
                bootstyle="primary",
            )
        )

        self.convert_button.pack(
            fill=X,
            ipady=5,
        )

        self.cancel_button = (
            ttk.Button(
                frame,
                text="Cancel",
                command=(
                    self.cancel_processing
                ),
                state=DISABLED,
                bootstyle="danger-outline",
            )
        )

        self.cancel_button.pack(
            fill=X,
            pady=(
                10,
                0,
            ),
        )

        self.last_ingest_frame = ttk.LabelFrame(
            frame,
            text="Latest Specification Result",
            padding=8,
            bootstyle="secondary",
        )
        self.last_ingest_frame.pack(fill=X, pady=(10, 0))
        ttk.Label(
            self.last_ingest_frame,
            textvariable=self.last_ingest_result_var,
            wraplength=320,
            justify=LEFT,
            bootstyle="secondary",
            style="Small.TLabel",
        ).pack(fill=X, anchor="w")
        last_actions = ttk.Frame(self.last_ingest_frame)
        last_actions.pack(fill=X, pady=(6, 0))
        self.open_last_spec_button = ttk.Button(
            last_actions,
            text="Open Specification",
            command=lambda: self._open_last_ingested_specification("spec_versions"),
            state=DISABLED,
            bootstyle="secondary-outline",
        )
        self.open_last_spec_button.pack(side=LEFT, fill=X, expand=True, padx=(0, 4))
        self.open_last_changes_button = ttk.Button(
            last_actions,
            text="View Changes",
            command=lambda: self._open_last_ingested_specification("spec_changes"),
            state=DISABLED,
            bootstyle="secondary-outline",
        )
        self.open_last_changes_button.pack(side=LEFT, fill=X, expand=True, padx=(4, 0))

        ttk.Label(
            frame,
            text=(
                "Cancel stops after the current "
                "document finishes."
            ),
            bootstyle="secondary",
            wraplength=330,
            justify=LEFT,
            style="Small.TLabel",
        ).pack(
            anchor="w",
            pady=(
                12,
                0,
            ),
        )

    # ========================================================
    # Progress
    # ========================================================

    def _build_progress_section(
        self,
        parent: ttk.Frame,
    ) -> None:

        frame = ttk.LabelFrame(
            parent,
            text="5. Processing Status",
            padding=14,
            style="Card.TLabelframe",
            bootstyle="secondary",
        )

        frame.pack(
            fill=X,
            pady=(
                0,
                12,
            ),
        )

        header = ttk.Frame(
            frame
        )

        header.pack(
            fill=X
        )

        ttk.Label(
            header,
            textvariable=(
                self.status_var
            ),
            style="StatusBold.TLabel",
            bootstyle="dark",
        ).pack(
            side=LEFT
        )

        ttk.Label(
            header,
            textvariable=(
                self.progress_text_var
            ),
            bootstyle="primary",
            font=(
                "Microsoft YaHei UI",
                10,
                "bold",
            ),
        ).pack(
            side=RIGHT
        )

        self.progress_bar = (
            ttk.Progressbar(
                frame,
                variable=(
                    self.progress_var
                ),
                maximum=100,
                bootstyle="primary-striped",
            )
        )

        self.progress_bar.pack(
            fill=X,
            pady=(
                12,
                10,
            ),
        )

        ttk.Label(
            frame,
            textvariable=(
                self.current_file_var
            ),
            wraplength=400,
            justify=LEFT,
            bootstyle="secondary",
        ).pack(
            anchor="w",
            pady=(
                0,
                8,
            ),
        )

        ttk.Separator(
            frame,
            orient="horizontal",
            bootstyle="secondary",
        ).pack(
            fill=X,
            pady=(
                2,
                8,
            ),
        )

        ttk.Label(
            frame,
            textvariable=(
                self.summary_var
            ),
            bootstyle="secondary",
            style="Small.TLabel",
        ).pack(
            anchor="w"
        )

    # ========================================================
    # Log
    # ========================================================

    def _build_log_section(
        self,
        parent: ttk.Frame,
    ) -> None:

        frame = ttk.LabelFrame(
            parent,
            text="Execution Log",
            padding=14,
            style="Card.TLabelframe",
            bootstyle="secondary",
        )

        frame.pack(
            fill=BOTH,
            expand=True,
        )

        text_container = ttk.Frame(
            frame
        )

        text_container.pack(
            fill=BOTH,
            expand=True,
        )

        self.log_text = tk.Text(
            text_container,
            state=DISABLED,
            wrap=WORD,
            height=20,
            font=(
                "Cascadia Mono",
                9,
            ),
            borderwidth=1,
            relief="solid",
            highlightthickness=0,
            padx=10,
            pady=10,
        )

        scrollbar = ttk.Scrollbar(
            text_container,
            orient=VERTICAL,
            command=(
                self.log_text
                .yview
            ),
            bootstyle="round",
        )

        self.log_text.configure(
            yscrollcommand=(
                scrollbar.set
            )
        )

        self.log_text.pack(
            side=LEFT,
            fill=BOTH,
            expand=True,
        )

        scrollbar.pack(
            side=RIGHT,
            fill=Y,
        )

    # ========================================================
    # Status Bar
    # ========================================================

    def _build_status_bar(
        self,
        parent: ttk.Frame,
    ) -> None:

        ttk.Separator(
            parent,
            orient="horizontal",
            bootstyle="secondary",
        ).pack(
            fill=X,
            pady=(
                14,
                8,
            ),
        )

        frame = ttk.Frame(
            parent
        )

        frame.pack(
            fill=X
        )

        ttk.Label(
            frame,
            text="Ready",
            bootstyle="success",
            font=(
                "Microsoft YaHei UI",
                9,
                "bold",
            ),
        ).pack(
            side=LEFT
        )

        ttk.Label(
            frame,
            text=(
                "Document Intelligence Console Enterprise"
            ),
            bootstyle="secondary",
            style="Small.TLabel",
        ).pack(
            side=LEFT,
            padx=(
                12,
                0,
            ),
        )

        ttk.Label(
            frame,
            text=(
                f"Version {APP_VERSION}"
            ),
            bootstyle="secondary",
            style="Small.TLabel",
        ).pack(
            side=RIGHT
        )

    # ========================================================
    # Input Files
    # ========================================================

    # ========================================================
    # PostgreSQL Settings Page
    # ========================================================

    def _build_postgres_settings_page(
        self,
        parent: ttk.Frame,
    ) -> None:
        """
        PostgreSQL connection configuration page.

        The page configures:
            - Host
            - Port
            - Database
            - User
            - Password
            - Connect timeout

        Non-secret settings:
            %LOCALAPPDATA%/DocumentIntelligenceConsole/config.yaml

        Password:
            POSTGRES_PASSWORD
            optionally persisted in the current Windows user's
            environment by the existing credential mechanism.
        """

        workspace = ttk.Frame(
            parent
        )

        workspace.pack(
            fill=BOTH,
            expand=True,
        )

        workspace.columnconfigure(
            0,
            weight=3,
        )

        workspace.columnconfigure(
            1,
            weight=2,
        )

        workspace.rowconfigure(
            0,
            weight=1,
        )

        left_panel = ttk.Frame(
            workspace
        )

        left_panel.grid(
            row=0,
            column=0,
            sticky="nsew",
            padx=(
                0,
                10,
            ),
        )

        right_panel = ttk.Frame(
            workspace
        )

        right_panel.grid(
            row=0,
            column=1,
            sticky="nsew",
            padx=(
                10,
                0,
            ),
        )

        self._build_postgres_connection_card(
            left_panel
        )

        self._build_postgres_actions_card(
            left_panel
        )

        self._build_qdrant_settings_card(
            left_panel
        )

        self._build_local_webui_settings_card(left_panel)

        self._build_postgres_status_card(
            right_panel
        )

        self._build_postgres_help_card(
            right_panel
        )

    def _open_stage1_review_window(self) -> None:
        """Launch authorized local reviewer GUI without sharing backend tokens."""
        import subprocess

        if getattr(sys, "frozen", False):
            messagebox.showwarning(
                "Stage 1 审核",
                "此源码版审核窗口未打包进旧 EXE。请用 Python 3.12 源码版启动。",
                parent=self.root,
            )
            return
        try:
            project_root = Path(__file__).resolve().parents[2]
            subprocess.Popen(
                [sys.executable, "-m", "app.gui.stage1_review_window"],
                cwd=str(project_root),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
        except (OSError, ValueError) as exc:
            messagebox.showerror("Stage 1 审核", str(exc), parent=self.root)

    # v1.04: developer-only local external UI & backend diagnostics.
    # This card belongs to the tool's main Settings page (not to Open WebUI).
    def _build_local_webui_settings_card(self, parent: ttk.Frame) -> None:
        from app.ai.config import AISettingsStore

        self._local_ui_settings_store = AISettingsStore(USER_CONFIG_DIR / "ai_config.json")
        existing = self._local_ui_settings_store.load()
        self.local_webui_url_var = tk.StringVar(value=existing.external_webui_url)
        self.local_backend_port_var = tk.StringVar(value=str(existing.api_port))
        self.local_webui_health_var = tk.StringVar(value="尚未检测 · 状态为实时探测结果，非模拟")

        card = ttk.LabelFrame(parent, text="3. 外部 UI / Open WebUI · 本机开发者面板", padding=16, style="Card.TLabelframe", bootstyle="secondary")
        card.pack(fill=X, pady=(10, 12))
        card.columnconfigure(1, weight=1)
        ttk.Label(card, text="Open WebUI 网址").grid(row=0, column=0, sticky="w", pady=5)
        ttk.Entry(card, textvariable=self.local_webui_url_var).grid(row=0, column=1, sticky="ew", pady=5, padx=(10, 0))
        ttk.Label(card, text="本地 API 端口").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Entry(card, textvariable=self.local_backend_port_var).grid(row=1, column=1, sticky="ew", pady=5, padx=(10, 0))
        bar = ttk.Frame(card)
        bar.grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 5))
        ttk.Button(bar, text="默认浏览器打开", command=self._open_local_webui, bootstyle="primary-outline").pack(side=LEFT, padx=(0, 6))
        ttk.Button(bar, text="复制网址", command=self._copy_local_webui, bootstyle="secondary-outline").pack(side=LEFT, padx=(0, 6))
        ttk.Button(bar, text="保存 WebUI 配置", command=self._save_local_webui, bootstyle="success-outline").pack(side=LEFT, padx=(0, 6))
        ttk.Button(bar, text="检查服务状态", command=self._check_local_webui_services, bootstyle="info-outline").pack(side=LEFT)
        ttk.Label(card, textvariable=self.local_webui_health_var, justify="left", wraplength=520, bootstyle="secondary").grid(row=3, column=0, columnspan=2, sticky="w", pady=(7, 0))
        ttk.Label(card, text="独立后台保持 localhost:18080；局域网只共享 Open WebUI，不共享 SQL/Qdrant/Ollama。", bootstyle="secondary", wraplength=520).grid(row=4, column=0, columnspan=2, sticky="w", pady=(7, 0))

        manager = ttk.LabelFrame(card, text="后台进程与 Windows 登录自启动", padding=9, bootstyle="secondary")
        manager.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        manager.columnconfigure(0, weight=1)
        self.local_backend_status_var = tk.StringVar(value="启动后自动检测后台（关闭 GUI 不停止 Backend）")
        ttk.Label(manager, textvariable=self.local_backend_status_var, wraplength=480, bootstyle="secondary").grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Button(manager, text="确保 Backend 运行", command=self._ensure_local_backend_running, bootstyle="primary-outline").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.local_autostart_var = tk.BooleanVar(value=False)
        if os.name == "nt":
            try:
                from app.enterprise.local_deployment import get_windows_login_autostart
                self.local_autostart_var.set(get_windows_login_autostart())
            except Exception:
                LOGGER.exception("Could not read Windows login autostart")
        ttk.Checkbutton(
            manager, text="Windows 当前用户登录时自动启动管理工具及 Backend",
            variable=self.local_autostart_var, command=self._toggle_windows_login_autostart,
            bootstyle="round-toggle",
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        # Independent watchdog: unlike the GUI autostart checkbox, this stays
        # alive after the desktop window is closed and may restore the Backend.
        self.local_watchdog_var = tk.BooleanVar(value=False)
        if os.name == "nt":
            try:
                from app.enterprise.local_deployment import get_backend_watchdog_autostart
                self.local_watchdog_var.set(get_backend_watchdog_autostart())
            except Exception:
                LOGGER.exception("Could not read watchdog startup registration")
        ttk.Checkbutton(
            manager, text="后台独立看护（登录后自动恢复；关闭 GUI 仍监测）",
            variable=self.local_watchdog_var, command=self._toggle_backend_watchdog,
            bootstyle="round-toggle",
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Button(
            manager, text="查看看护状态", command=self._refresh_backend_watchdog,
            bootstyle="secondary-outline",
        ).grid(row=4, column=0, sticky="w", pady=(6, 0))

        lan = ttk.LabelFrame(card, text="当前 Wi-Fi 局域网共享（仅 Open WebUI）", padding=9, bootstyle="secondary")
        lan.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        from app.enterprise.local_deployment import lan_share_status
        self.local_lan_status_var = tk.StringVar(value=lan_share_status())
        ttk.Label(lan, textvariable=self.local_lan_status_var, wraplength=500, bootstyle="secondary").pack(anchor="w")
        controls = ttk.Frame(lan)
        controls.pack(anchor="w", pady=(8, 0))
        ttk.Button(controls, text="一键配置当前 Wi-Fi", command=lambda: self._configure_lan_share("Enable"), bootstyle="warning-outline").pack(side=LEFT, padx=(0, 6))
        ttk.Button(controls, text="关闭局域网共享", command=lambda: self._configure_lan_share("Disable"), bootstyle="danger-outline").pack(side=LEFT, padx=(0, 6))
        ttk.Button(controls, text="刷新", command=self._refresh_lan_share_status, bootstyle="secondary-outline").pack(side=LEFT)
        ttk.Label(lan, text="仅用于受批准的公司内网测试；HTTP 无加密，正式多人处理机密文档前必须配置内网 HTTPS。需要管理员 UAC 授权，网络隔离策略不能通过本工具绕过。", wraplength=520, bootstyle="secondary").pack(anchor="w", pady=(6, 0))

    def _ensure_local_backend_running(self) -> None:
        """Never block Tk while starting Python/BGE-M3 or probing the API."""
        from app.enterprise.local_deployment import ensure_backend_started, get_backend_state
        try:
            port = int(self.local_backend_port_var.get()) if hasattr(self, "local_backend_port_var") else 18080
        except ValueError:
            self.local_backend_status_var.set("Backend API 端口无效")
            return
        if hasattr(self, "local_backend_status_var"):
            self.local_backend_status_var.set("正在后台检测 / 启动独立 API…")

        def worker() -> None:
            try:
                result = ensure_backend_started(BASE_DIR, port, USER_CONFIG_DIR)
                # First boot needs time to parse config and load the model.
                if result == "STARTING":
                    import time
                    for _ in range(20):
                        time.sleep(0.5)
                        if get_backend_state(port, USER_CONFIG_DIR) == "RUNNING":
                            result = "RUNNING"
                            break
                message = {
                    "RUNNING": "Backend 已运行（独立进程）；关闭工具后仍可供 WebUI 使用",
                    "STARTING": "Backend 已启动，仍在初始化；稍后刷新健康状态",
                    "PORT_OCCUPIED_UNVERIFIED": "18080 已被其他服务占用或认证失败，未重复启动",
                    "BACKEND_BINARY_REQUIRED": "EXE 部署缺少单独的 Backend 可执行文件",
                }.get(result, result)
            except Exception as exc:
                message = f"Backend 启动失败：{type(exc).__name__}: {exc}；检查本地 logs/backend_startup.log"
            try:
                self.root.after(0, lambda: self.local_backend_status_var.set(message))
            except tk.TclError:
                pass

        threading.Thread(target=worker, daemon=True, name="dic-backend-auto-launch").start()

    def _toggle_windows_login_autostart(self) -> None:
        from app.enterprise.local_deployment import set_windows_login_autostart, get_windows_login_autostart
        try:
            desired = self.local_autostart_var.get()
            actual = set_windows_login_autostart(desired, BASE_DIR)
            self.local_autostart_var.set(actual)
            self.local_backend_status_var.set("已设置当前用户登录时自启动" if actual else "已关闭 Windows 登录自启动")
        except Exception as exc:
            LOGGER.exception("Windows autostart update failed")
            try:
                self.local_autostart_var.set(get_windows_login_autostart())
            except Exception:
                self.local_autostart_var.set(False)
            messagebox.showerror("Windows 登录自启动", str(exc), parent=self.root)

    def _toggle_backend_watchdog(self) -> None:
        from app.enterprise.local_deployment import (
            get_backend_watchdog_autostart, set_backend_watchdog_autostart,
        )
        try:
            actual = set_backend_watchdog_autostart(self.local_watchdog_var.get(), BASE_DIR)
            self.local_watchdog_var.set(actual)
            self.local_backend_status_var.set(
                "后台独立看护已启用；需 Windows 用户登录，非双机高可用" if actual
                else "后台独立看护已关闭；现有 Backend 不会被停止"
            )
        except Exception as exc:
            LOGGER.exception("Failed to update backend watchdog")
            try:
                self.local_watchdog_var.set(get_backend_watchdog_autostart())
            except Exception:
                self.local_watchdog_var.set(False)
            messagebox.showerror("Backend 独立看护", str(exc), parent=self.root)

    def _refresh_backend_watchdog(self) -> None:
        from app.enterprise.local_deployment import read_backend_watchdog_status
        self.local_backend_status_var.set(
            "独立看护状态：" + read_backend_watchdog_status(USER_CONFIG_DIR)
        )

    def _refresh_lan_share_status(self) -> None:
        from app.enterprise.local_deployment import lan_share_status
        self.local_lan_status_var.set(lan_share_status())

    def _configure_lan_share(self, action: str) -> None:
        from app.ai.local_webui import validate_local_webui_url
        from app.enterprise.local_deployment import request_lan_configuration
        from urllib.parse import urlparse
        try:
            url = validate_local_webui_url(self.local_webui_url_var.get())
            port = urlparse(url).port
            if action == "Enable":
                approved = messagebox.askyesno(
                    "公司 Wi-Fi 内网共享确认",
                    "只共享 Open WebUI，不开放 Backend/数据库/模型。\n\n"
                    "需要公司 IT 授权，且所在 Wi-Fi 必须为可信内部网络。\n"
                    "当前方案是 HTTP 明文，仅供连接验证，正式处理公司机密资料前请配置内部 HTTPS。\n\n"
                    "要向 Windows 请求管理员权限来配置端口转发和受限防火墙规则吗？",
                    parent=self.root,
                )
                if not approved:
                    return
            else:
                if not messagebox.askyesno("关闭内网共享", "撤销本工具建立的端口转发和防火墙规则？", parent=self.root):
                    return
            request_lan_configuration(BASE_DIR, action, port=port)
            self.local_lan_status_var.set("管理员操作已请求；请在 UAC 窗口确认，完成后点击刷新并从其他电脑测试")
        except Exception as exc:
            messagebox.showerror("Wi-Fi 局域网共享", str(exc), parent=self.root)

    def _save_local_webui(self) -> None:
        from dataclasses import replace
        from app.ai.local_webui import validate_local_webui_url

        try:
            url = validate_local_webui_url(self.local_webui_url_var.get())
            port = int(self.local_backend_port_var.get())
            if not 1 <= port <= 65535:
                raise ValueError("端口必须位于 1–65535")
            old = self._local_ui_settings_store.load()
            self._local_ui_settings_store.save(replace(old, external_webui_url=url, api_port=port, api_host="127.0.0.1", api_enabled=False, api_key=""))
            self.local_webui_health_var.set(f"已保存 · {url} · API 端口 {port}（重启后端生效）")
        except (ValueError, OSError) as exc:
            messagebox.showerror("外部 UI 设置", str(exc), parent=self.root)

    def _open_local_webui(self) -> None:
        from app.ai.local_webui import open_webui_default_browser
        try:
            open_webui_default_browser(self.local_webui_url_var.get())
        except ValueError as exc:
            messagebox.showerror("Open WebUI", str(exc), parent=self.root)

    def _copy_local_webui(self) -> None:
        from app.ai.local_webui import validate_local_webui_url
        try:
            value = validate_local_webui_url(self.local_webui_url_var.get())
            self.root.clipboard_clear()
            self.root.clipboard_append(value)
            self.local_webui_health_var.set("WebUI 网址已复制")
        except ValueError as exc:
            messagebox.showerror("Open WebUI", str(exc), parent=self.root)

    def _check_local_webui_services(self) -> None:
        from app.ai.local_webui import local_service_status, validate_local_webui_url
        from app.ai.config import AISettingsStore

        try:
            webui = validate_local_webui_url(self.local_webui_url_var.get())
            port = int(self.local_backend_port_var.get())
        except ValueError as exc:
            self.local_webui_health_var.set(f"配置错误: {exc}")
            return
        ai = AISettingsStore(USER_CONFIG_DIR / "ai_config.json").load()
        ollama = ai.ollama_url
        qdrant = self.qdrant_url_var.get()
        db_host = self.postgres_host_var.get()
        db_port = self.postgres_port_var.get()
        db_user = self.postgres_user_var.get()
        db_pass = self.postgres_password_var.get() or os.getenv("POSTGRES_PASSWORD", "")
        db_name = self.postgres_database_var.get() or "rag"
        self.local_webui_health_var.set("检查中…（异步）")

        def worker() -> None:
            status = local_service_status(webui_url=webui, api_port=port, ollama_url=ollama, qdrant_url=qdrant)
            try:
                import psycopg
                with psycopg.connect(host=db_host, port=int(db_port), dbname=db_name, user=db_user, password=db_pass, connect_timeout=2) as conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT 1")
                        status["PostgreSQL"] = "ONLINE" if cur.fetchone()[0] == 1 else "ERROR"
            except Exception:
                status["PostgreSQL"] = "OFFLINE"
            snapshot = " | ".join(f"{name}: {state}" for name, state in status.items())
            summary = f"{snapshot}  |  首选模型: {ai.chat_model or '未配置'}  |  Embedding: {ai.embedding_model}"
            try:
                self.root.after(0, lambda: self.local_webui_health_var.set(summary))
            except tk.TclError:
                pass

        threading.Thread(target=worker, name="dic-settings-local-health", daemon=True).start()

    def _build_postgres_connection_card(
        self,
        parent: ttk.Frame,
    ) -> None:

        frame = ttk.LabelFrame(
            parent,
            text="1. Connection",
            padding=18,
            style="Card.TLabelframe",
            bootstyle="secondary",
        )

        frame.pack(
            fill=X,
            pady=(
                0,
                12,
            ),
        )

        frame.columnconfigure(
            1,
            weight=1,
        )

        fields = (
            (
                "Host",
                self.postgres_host_var,
            ),
            (
                "Port",
                self.postgres_port_var,
            ),
            (
                "Database",
                self.postgres_database_var,
            ),
            (
                "User",
                self.postgres_user_var,
            ),
            (
                "Connect Timeout (sec)",
                self.postgres_timeout_var,
            ),
        )

        self.postgres_setting_entries: list[
            ttk.Entry
        ] = []

        ttk.Label(
            frame,
            text="Processing Scope",
            font=("Microsoft YaHei UI", 9, "bold"),
        ).grid(row=0, column=0, sticky="w", padx=(0, 14), pady=7)

        self.postgres_project_combobox = ttk.Combobox(
            frame,
            textvariable=self.project_var,
            values=ProjectRegistry.display_values(),
            state="readonly",
            bootstyle="primary",
        )
        self.postgres_project_combobox.grid(
            row=0, column=1, sticky="ew", pady=7
        )
        self.postgres_project_combobox.bind(
            "<<ComboboxSelected>>", self._on_project_selected
        )

        for row_index, (
            label_text,
            variable,
        ) in enumerate(
            fields
        ):

            ttk.Label(
                frame,
                text=label_text,
                font=(
                    "Microsoft YaHei UI",
                    9,
                    "bold",
                ),
            ).grid(
                row=row_index + 1,
                column=0,
                sticky="w",
                padx=(
                    0,
                    14,
                ),
                pady=7,
            )

            entry = ttk.Entry(
                frame,
                textvariable=variable,
                bootstyle="primary",
            )

            entry.grid(
                row=row_index + 1,
                column=1,
                sticky="ew",
                pady=7,
            )

            self.postgres_setting_entries.append(
                entry
            )

        password_row = len(
            fields
        ) + 1

        ttk.Label(
            frame,
            text="Password",
            font=(
                "Microsoft YaHei UI",
                9,
                "bold",
            ),
        ).grid(
            row=password_row,
            column=0,
            sticky="w",
            padx=(
                0,
                14,
            ),
            pady=7,
        )

        self.postgres_password_entry = (
            ttk.Entry(
                frame,
                textvariable=(
                    self.postgres_password_var
                ),
                show="●",
                bootstyle="primary",
            )
        )

        self.postgres_password_entry.grid(
            row=password_row,
            column=1,
            sticky="ew",
            pady=7,
        )

        self.postgres_setting_entries.append(
            self.postgres_password_entry
        )

        self.postgres_remember_checkbox = (
            ttk.Checkbutton(
                frame,
                text=(
                    "Remember password on this computer "
                    "(Windows user environment)"
                ),
                variable=(
                    self.postgres_remember_password_var
                ),
                bootstyle="success-round-toggle",
            )
        )

        self.postgres_remember_checkbox.grid(
            row=(
                password_row
                + 1
            ),
            column=1,
            sticky="w",
            pady=(
                10,
                4,
            ),
        )

        ttk.Label(
            frame,
            text=(
                "The password is never written to config.yaml "
                "or application logs."
            ),
            wraplength=560,
            justify=LEFT,
            bootstyle="secondary",
            style="Small.TLabel",
        ).grid(
            row=(
                password_row
                + 2
            ),
            column=1,
            sticky="w",
            pady=(
                3,
                0,
            ),
        )

    def _build_postgres_actions_card(
        self,
        parent: ttk.Frame,
    ) -> None:

        frame = ttk.LabelFrame(
            parent,
            text="2. Actions",
            padding=18,
            style="Card.TLabelframe",
            bootstyle="secondary",
        )

        frame.pack(
            fill=X,
        )

        button_row = ttk.Frame(
            frame
        )

        button_row.pack(
            fill=X
        )

        self.postgres_test_button = (
            ttk.Button(
                button_row,
                text="✓  Test Connection",
                command=(
                    self.start_postgres_connection_test
                ),
                bootstyle="primary",
            )
        )

        self.postgres_test_button.pack(
            side=LEFT,
            fill=X,
            expand=True,
        )

        self.postgres_save_button = (
            ttk.Button(
                button_row,
                text="Save Settings",
                command=(
                    self.save_postgres_settings
                ),
                bootstyle="success",
            )
        )

        self.postgres_save_button.pack(
            side=LEFT,
            fill=X,
            expand=True,
            padx=(
                10,
                0,
            ),
        )

        secondary_row = ttk.Frame(
            frame
        )

        secondary_row.pack(
            fill=X,
            pady=(
                10,
                0,
            ),
        )

        self.postgres_defaults_button = (
            ttk.Button(
                secondary_row,
                text="Restore Defaults",
                command=(
                    self.restore_postgres_defaults
                ),
                bootstyle="secondary-outline",
            )
        )

        self.postgres_defaults_button.pack(
            side=LEFT,
            fill=X,
            expand=True,
        )

        self.postgres_clear_password_button = (
            ttk.Button(
                secondary_row,
                text="Clear Saved Password",
                command=(
                    self.clear_saved_postgres_password
                ),
                bootstyle="danger-outline",
            )
        )

        self.postgres_clear_password_button.pack(
            side=LEFT,
            fill=X,
            expand=True,
            padx=(
                10,
                0,
            ),
        )

        # v1.14: separate human-review process, never an Open WebUI action.
        ttk.Button(
            frame,
            text="Stage 1 · 工程证据人工审核（独立窗口）",
            command=self._open_stage1_review_window,
            bootstyle="info-outline",
        ).pack(fill=X, pady=(12, 0))

        schema_row = ttk.Frame(
            frame
        )

        schema_row.pack(
            fill=X,
            pady=(
                12,
                0,
            ),
        )

        self.postgres_create_tables_button = (
            ttk.Button(
                schema_row,
                text="▦  Initialize / Upgrade Enterprise Database",
                command=(
                    self.start_postgres_schema_initialization
                ),
                state=DISABLED,
                bootstyle="info",
            )
        )

        self.postgres_create_tables_button.pack(
            fill=X,
            ipady=4,
        )

        ttk.Label(
            schema_row,
            text=(
                "Enabled only after a successful connection test. "
                "Non-destructive: upgrades public RAG schema + app_config control plane; "
                "existing business data, settings and vectors are preserved."
            ),
            wraplength=560,
            justify=LEFT,
            bootstyle="secondary",
            style="Small.TLabel",
        ).pack(
            anchor="w",
            pady=(
                8,
                0,
            ),
        )

        qabug_box = ttk.LabelFrame(
            frame,
            text="QA/BUG 独立企业 RAG 数据域",
            padding=12,
            bootstyle="info",
        )
        qabug_box.pack(fill=X, pady=(14, 0))

        ttk.Label(
            qabug_box,
            text=(
                "独立 PostgreSQL: rag_qabug · 独立 Qdrant: rag_qabug_current_chunks。"
                "与 21MM / 24MM / COMMON 式样书数据隔离；初始化为幂等、非破坏操作。"
            ),
            wraplength=560,
            justify=LEFT,
            bootstyle="secondary",
            style="Small.TLabel",
        ).pack(anchor="w")

        self.qabug_setup_button = ttk.Button(
            qabug_box,
            text="▦  QA/BUG 一键建库 / 建表 / Qdrant 索引",
            command=self.start_qabug_environment_setup,
            state=DISABLED,
            bootstyle="info",
        )
        self.qabug_setup_button.pack(fill=X, ipady=3, pady=(10, 0))

        ttk.Label(
            qabug_box,
            text="QA/BUG 文件统一在“文档导入”页面导入：右上角选择 QA/BUG，添加 QA/BUG List 后直接处理。",
            wraplength=560, justify=LEFT, bootstyle="secondary", style="Small.TLabel",
        ).pack(anchor="w", pady=(8, 0))

        self.qabug_quality_button = ttk.Button(
            qabug_box, text="✓ QA/BUG 数据质量检查",
            command=self.start_qabug_quality_validation,
            state=DISABLED, bootstyle="success-outline",
        )
        self.qabug_quality_button.pack(fill=X, pady=(8, 0))

        ttk.Label(
            qabug_box, textvariable=self.qabug_admin_status_var,
            wraplength=560, justify=LEFT, bootstyle="secondary",
            style="Small.TLabel",
        ).pack(anchor="w", pady=(8, 0))

    def _build_qdrant_settings_card(
        self,
        parent: ttk.Frame,
    ) -> None:
        frame = ttk.LabelFrame(
            parent,
            text="3. Qdrant / Console",
            padding=18,
            style="Card.TLabelframe",
            bootstyle="secondary",
        )
        frame.pack(fill=X, pady=(12, 0))
        frame.columnconfigure(1, weight=1)

        fields = [
            ("Qdrant URL", self.qdrant_url_var, False),
            ("API Key", self.qdrant_api_key_var, True),
            ("Timeout (sec)", self.qdrant_timeout_var, False),
            ("Console Page Size", self.console_page_size_var, False),
        ]
        for row, (label, variable, secret) in enumerate(fields):
            ttk.Label(frame, text=label, font=("Microsoft YaHei UI", 9, "bold")).grid(
                row=row, column=0, sticky="w", padx=(0, 14), pady=6
            )
            ttk.Entry(
                frame,
                textvariable=variable,
                show="●" if secret else "",
                bootstyle="primary",
            ).grid(row=row, column=1, sticky="ew", pady=6)

        ttk.Checkbutton(
            frame,
            text="Remember Qdrant API key in OS credential store",
            variable=self.qdrant_remember_key_var,
            bootstyle="success-round-toggle",
        ).grid(row=len(fields), column=1, sticky="w", pady=(8, 4))

        actions = ttk.Frame(frame)
        actions.grid(row=len(fields) + 1, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        ttk.Button(
            actions,
            text="Open Qdrant Browser",
            command=self._open_qdrant_browser,
            bootstyle="info-outline",
        ).pack(side=LEFT)
        ttk.Label(actions, textvariable=self.qdrant_status_var, bootstyle="secondary").pack(
            side=RIGHT
        )

    def _open_qdrant_browser(self) -> None:
        self.navigate("qdrant")
        if hasattr(self, "enterprise_module"):
            try:
                self.enterprise_module.refresh_qdrant()
            except Exception as exc:
                self.qdrant_status_var.set(f"Qdrant: {exc}")

    def _build_postgres_status_card(
        self,
        parent: ttk.Frame,
    ) -> None:

        frame = ttk.LabelFrame(
            parent,
            text="Status",
            padding=18,
            style="Card.TLabelframe",
            bootstyle="secondary",
        )

        frame.pack(
            fill=X,
            pady=(
                0,
                12,
            ),
        )

        ttk.Label(
            frame,
            text="Connection",
            font=(
                "Microsoft YaHei UI",
                9,
                "bold",
            ),
        ).pack(
            anchor="w"
        )

        self.postgres_connection_status_label = (
            ttk.Label(
                frame,
                textvariable=(
                    self.postgres_connection_status_var
                ),
                bootstyle="secondary",
                font=(
                    "Microsoft YaHei UI",
                    11,
                    "bold",
                ),
            )
        )

        self.postgres_connection_status_label.pack(
            anchor="w",
            pady=(
                4,
                14,
            ),
        )

        ttk.Label(
            frame,
            text="Credential",
            font=(
                "Microsoft YaHei UI",
                9,
                "bold",
            ),
        ).pack(
            anchor="w"
        )

        self.postgres_password_status_label = (
            ttk.Label(
                frame,
                textvariable=(
                    self.postgres_password_status_var
                ),
                bootstyle=(
                    "success"
                    if os.getenv(
                        "POSTGRES_PASSWORD"
                    )
                    else "warning"
                ),
            )
        )

        self.postgres_password_status_label.pack(
            anchor="w",
            pady=(
                4,
                14,
            ),
        )

        ttk.Label(
            frame,
            text="Database Schema",
            font=(
                "Microsoft YaHei UI",
                9,
                "bold",
            ),
        ).pack(
            anchor="w"
        )

        self.postgres_schema_status_label = (
            ttk.Label(
                frame,
                textvariable=(
                    self.postgres_schema_status_var
                ),
                bootstyle="secondary",
                wraplength=430,
                justify=LEFT,
            )
        )

        self.postgres_schema_status_label.pack(
            anchor="w",
            pady=(
                4,
                14,
            ),
        )

        ttk.Label(
            frame,
            text="User config file",
            font=(
                "Microsoft YaHei UI",
                9,
                "bold",
            ),
        ).pack(
            anchor="w"
        )

        ttk.Label(
            frame,
            textvariable=(
                self.postgres_config_path_var
            ),
            wraplength=430,
            justify=LEFT,
            bootstyle="secondary",
            style="Small.TLabel",
        ).pack(
            anchor="w",
            pady=(
                4,
                0,
            ),
        )

    def _build_postgres_help_card(
        self,
        parent: ttk.Frame,
    ) -> None:

        frame = ttk.LabelFrame(
            parent,
            text="New Computer Setup",
            padding=18,
            style="Card.TLabelframe",
            bootstyle="secondary",
        )

        frame.pack(
            fill=BOTH,
            expand=True,
        )

        instructions = (
            "1. Install / prepare PostgreSQL or enter a remote DB host.",
            "2. Select 21MM, 24MM, or Common.",
            "3. Database auto-fills: 21MM=rag_21mm, 24MM=rag_24mm, Common=rag.",
            "4. Enter the PostgreSQL password.",
            "5. Click Test Connection, then Initialize / Upgrade Enterprise Database.",
            "6. Repeat Initialize / Upgrade once for each physical project database (21MM / 24MM / Common).",
            "7. The app_config control plane is upgraded independently from public business data.",
            "8. Use Document Conversion or JSON → PostgreSQL.",
        )

        for instruction in instructions:

            ttk.Label(
                frame,
                text=instruction,
                wraplength=430,
                justify=LEFT,
                bootstyle="secondary",
            ).pack(
                anchor="w",
                pady=(
                    0,
                    10,
                ),
            )

        ttk.Separator(
            frame,
            orient="horizontal",
            bootstyle="secondary",
        ).pack(
            fill=X,
            pady=(
                4,
                12,
            ),
        )

        ttk.Label(
            frame,
            text=(
                "The distributed application can still be a single EXE. "
                "This page creates only a per-user runtime configuration "
                "under LocalAppData."
            ),
            wraplength=430,
            justify=LEFT,
            bootstyle="secondary",
            style="Small.TLabel",
        ).pack(
            anchor="w"
        )

    # ========================================================
    # PostgreSQL Settings - Load / Validate / Save
    # ========================================================

    @staticmethod
    def _default_postgres_profile() -> dict[
        str,
        Any,
    ]:

        return {
            "host": "127.0.0.1",
            "port": 5432,
            "database": "rag",
            "project_databases": {
                definition.code: definition.database_default
                for definition in ProjectRegistry.PROJECTS.values()
            },
            "user": "postgres",
            "connect_timeout": 10,
        }

    @classmethod
    def _read_postgres_profile(
        cls,
    ) -> dict[str, Any]:

        profile = dict(
            cls._default_postgres_profile()
        )

        candidate_paths: list[Path] = []

        environment_path = os.getenv(
            "CONFIG_FILE"
        )

        if (
            environment_path
            and environment_path.strip()
        ):
            candidate_paths.append(
                Path(
                    environment_path
                ).expanduser()
            )

        if USER_POSTGRES_CONFIG_FILE not in candidate_paths:
            candidate_paths.append(
                USER_POSTGRES_CONFIG_FILE
            )

        # Development fallback only.
        candidate_paths.append(
            (
                Path(
                    __file__
                ).resolve().parents[2]
                / "config"
                / "config.yaml"
            )
        )

        for path in candidate_paths:

            try:
                if not path.is_file():
                    continue

                with path.open(
                    "r",
                    encoding="utf-8",
                ) as file:

                    raw = yaml.safe_load(
                        file
                    )

                if not isinstance(
                    raw,
                    dict,
                ):
                    continue

                database = raw.get(
                    "database"
                )

                if not isinstance(
                    database,
                    dict,
                ):
                    continue

                host = database.get(
                    "host"
                )

                port = database.get(
                    "port"
                )

                database_name = (
                    database.get(
                        "database"
                    )
                )

                project_databases = database.get(
                    "project_databases",
                    {},
                )

                user = database.get(
                    "user"
                )

                timeout = database.get(
                    "connect_timeout"
                )

                if (
                    isinstance(
                        host,
                        str,
                    )
                    and host.strip()
                ):
                    profile[
                        "host"
                    ] = host.strip()

                try:
                    parsed_port = int(
                        port
                    )

                    if (
                        1
                        <= parsed_port
                        <= 65535
                    ):
                        profile[
                            "port"
                        ] = parsed_port

                except (
                    TypeError,
                    ValueError,
                ):
                    pass

                if (
                    isinstance(
                        database_name,
                        str,
                    )
                    and database_name.strip()
                ):
                    profile[
                        "database"
                    ] = (
                        database_name.strip()
                    )

                if isinstance(project_databases, dict):
                    for project_code in ProjectRegistry.PROJECTS:
                        project_database = project_databases.get(project_code)
                        if isinstance(project_database, str) and project_database.strip():
                            profile["project_databases"][project_code] = (
                                project_database.strip()
                            )

                # Common/generic mode uses database.database as its backwards-
                # compatible fallback when no explicit COMMON mapping exists.
                if (
                    "COMMON" not in project_databases
                    and isinstance(database_name, str)
                    and database_name.strip()
                ):
                    profile["project_databases"]["COMMON"] = database_name.strip()

                if (
                    isinstance(
                        user,
                        str,
                    )
                    and user.strip()
                ):
                    profile[
                        "user"
                    ] = user.strip()

                try:
                    parsed_timeout = int(
                        timeout
                    )

                    if parsed_timeout > 0:
                        profile[
                            "connect_timeout"
                        ] = parsed_timeout

                except (
                    TypeError,
                    ValueError,
                ):
                    pass

                break

            except (
                OSError,
                yaml.YAMLError,
            ):
                continue

        return profile

    def _collect_postgres_settings(
        self,
    ) -> PostgreSQLSettings | None:

        host = (
            self.postgres_host_var
            .get()
            .strip()
        )

        try:
            project = ProjectRegistry.resolve(self.project_var.get())
        except ProjectRegistryError:
            messagebox.showwarning(
                "PostgreSQL Settings",
                "Select 21MM, 24MM, Common, or QA/BUG first. Connection test and "
                "schema initialization operate on the selected scope's database.",
                parent=self.root,
            )
            return None

        database = self.postgres_database_var.get().strip()
        if database:
            self._project_database_names[project.code] = database

        user = (
            self.postgres_user_var
            .get()
            .strip()
        )

        if not host:
            messagebox.showwarning(
                "PostgreSQL Settings",
                "Host cannot be empty.",
                parent=self.root,
            )

            return None

        if not database:
            messagebox.showwarning(
                "PostgreSQL Settings",
                "Database cannot be empty.",
                parent=self.root,
            )

            return None

        if not user:
            messagebox.showwarning(
                "PostgreSQL Settings",
                "User cannot be empty.",
                parent=self.root,
            )

            return None

        try:
            port = int(
                self.postgres_port_var
                .get()
                .strip()
            )

        except ValueError:

            messagebox.showwarning(
                "PostgreSQL Settings",
                "Port must be an integer.",
                parent=self.root,
            )

            return None

        if not (
            1
            <= port
            <= 65535
        ):
            messagebox.showwarning(
                "PostgreSQL Settings",
                (
                    "Port must be between "
                    "1 and 65535."
                ),
                parent=self.root,
            )

            return None

        try:
            timeout = int(
                self.postgres_timeout_var
                .get()
                .strip()
            )

        except ValueError:

            messagebox.showwarning(
                "PostgreSQL Settings",
                (
                    "Connect timeout must "
                    "be an integer."
                ),
                parent=self.root,
            )

            return None

        if timeout <= 0:

            messagebox.showwarning(
                "PostgreSQL Settings",
                (
                    "Connect timeout must "
                    "be greater than 0."
                ),
                parent=self.root,
            )

            return None

        return PostgreSQLSettings(
            host=host,
            port=port,
            database=database,
            user=user,
            connect_timeout=timeout,
        )

    def save_postgres_settings(
        self,
    ) -> None:

        if self.is_processing:
            return

        settings = (
            self._collect_postgres_settings()
        )

        if settings is None:
            return

        password = (
            self.postgres_password_var
            .get()
        )

        remember = bool(
            self.postgres_remember_password_var
            .get()
        )

        if (
            remember
            and not password
            and not os.getenv(
                "POSTGRES_PASSWORD"
            )
        ):
            messagebox.showwarning(
                "PostgreSQL Settings",
                (
                    "Enter a password before enabling "
                    "'Remember password on this computer'."
                ),
                parent=self.root,
            )

            self.postgres_password_entry.focus_set()

            return

        # Password is applied to the current process only when the user
        # actually typed one. An existing session password is preserved.
        if password:

            os.environ[
                "POSTGRES_PASSWORD"
            ] = password

            if remember:

                try:
                    self._persist_postgres_password(
                        password
                    )

                except OSError as exc:

                    messagebox.showwarning(
                        "PostgreSQL Settings",
                        (
                            "The settings will be saved and the password "
                            "will work for this session, but Windows could "
                            "not remember the password.\n\n"
                            f"Reason: {exc}"
                        ),
                        parent=self.root,
                    )

        try:
            qdrant_timeout = float(self.qdrant_timeout_var.get().strip() or "10")
            if qdrant_timeout <= 0:
                raise ValueError
        except ValueError:
            messagebox.showwarning(
                "Settings",
                "Qdrant timeout must be a positive number.",
                parent=self.root,
            )
            return

        try:
            page_size = max(10, min(int(self.console_page_size_var.get().strip() or "100"), 1000))
        except ValueError:
            messagebox.showwarning(
                "Settings",
                "Console page size must be an integer between 10 and 1000.",
                parent=self.root,
            )
            return

        qdrant_url = self.qdrant_url_var.get().strip() or "http://127.0.0.1:6333"
        qdrant_key = self.qdrant_api_key_var.get()
        if self.qdrant_remember_key_var.get():
            if qdrant_key:
                if not set_secret("qdrant", qdrant_url, qdrant_key):
                    messagebox.showwarning(
                        "Settings",
                        "Qdrant settings will be saved, but the OS credential store could not remember the API key.",
                        parent=self.root,
                    )
        else:
            set_secret("qdrant", qdrant_url, "")

        user_config = {
            "database": {
                "enabled": True,
                "host": settings.host,
                "port": settings.port,
                "database": "rag",
                "project_databases": {
                    code: self._project_database_names.get(code, "rag") or "rag"
                    for code in ProjectRegistry.PROJECTS
                },
                "user": settings.user,
                "password_env": "POSTGRES_PASSWORD",
                "connect_timeout": settings.connect_timeout,
            },
            "qdrant": {
                "url": qdrant_url,
                "timeout": qdrant_timeout,
            },
            "console": {
                "page_size": page_size,
                "default_retrieval_view": "rag_current_chunks",
                "sidebar_open_group": self._sidebar_open_group or "AI WORKSPACE",
            },
        }

        try:

            USER_CONFIG_DIR.mkdir(
                parents=True,
                exist_ok=True,
            )

            temporary_path = (
                USER_POSTGRES_CONFIG_FILE
                .with_suffix(
                    ".yaml.tmp"
                )
            )

            with temporary_path.open(
                "w",
                encoding="utf-8",
                newline="\n",
            ) as file:

                yaml.safe_dump(
                    user_config,
                    file,
                    allow_unicode=True,
                    sort_keys=False,
                )

            temporary_path.replace(
                USER_POSTGRES_CONFIG_FILE
            )

        except OSError as exc:

            messagebox.showerror(
                "PostgreSQL Settings",
                (
                    "Unable to save PostgreSQL settings.\n\n"
                    f"{exc}"
                ),
                parent=self.root,
            )

            return

        # Make the newly saved config active immediately for all later
        # PipelineFactory / PostgresStorage objects in this process.
        os.environ[
            "CONFIG_FILE"
        ] = str(
            USER_POSTGRES_CONFIG_FILE
        )

        self.postgres_config_path_var.set(
            str(
                USER_POSTGRES_CONFIG_FILE
            )
        )

        self._refresh_postgres_password_status()

        self.postgres_connection_status_var.set(
            (
                "Settings saved. "
                "Connection test recommended."
            )
        )

        self.postgres_connection_status_label.configure(
            bootstyle="primary"
        )

        LOGGER.info(
            (
                "PostgreSQL settings saved | "
                "host=%s | port=%s | "
                "database=%s | scope_databases=%s | user=%s | "
                "config=%s"
            ),
            settings.host,
            settings.port,
            settings.database,
            self._project_database_names,
            settings.user,
            USER_POSTGRES_CONFIG_FILE,
        )

        if hasattr(self, "enterprise_module"):
            self.enterprise_module._rebuild_services()

        messagebox.showinfo(
            "Settings",
            (
                "Unified settings were saved.\n\n"
                "PostgreSQL and Qdrant secrets are not written to config.yaml."
            ),
            parent=self.root,
        )

        # Clear plaintext from the visible field after applying it.
        self.postgres_password_var.set(
            ""
        )

    def restore_postgres_defaults(
        self,
    ) -> None:

        if self.is_processing:
            return

        self.postgres_connection_verified = False

        defaults = (
            self._default_postgres_profile()
        )

        self.postgres_host_var.set(
            defaults["host"]
        )

        self.postgres_port_var.set(
            str(
                defaults["port"]
            )
        )

        self._project_database_names = dict(defaults["project_databases"])
        try:
            active_project = ProjectRegistry.resolve(self.project_var.get())
        except ProjectRegistryError:
            self.postgres_database_var.set(defaults["database"])
        else:
            self.postgres_database_var.set(
                self._project_database_names.get(
                    active_project.code,
                    active_project.database_default,
                )
            )
        self._on_project_selected()

        self.postgres_user_var.set(
            defaults["user"]
        )

        self.postgres_timeout_var.set(
            str(
                defaults[
                    "connect_timeout"
                ]
            )
        )

        self.postgres_password_var.set(
            ""
        )

        self.postgres_remember_password_var.set(
            False
        )

        self.qdrant_url_var.set("http://127.0.0.1:6333")
        self.qdrant_timeout_var.set("10.0")
        self.qdrant_api_key_var.set("")
        self.qdrant_remember_key_var.set(False)
        self.console_page_size_var.set("100")
        self._sidebar_open_group = "AI WORKSPACE"
        self._sync_sidebar_selection_for_page(self._current_page_key)
        self.qdrant_status_var.set("Qdrant: defaults loaded")

        self.postgres_connection_status_var.set(
            "Defaults loaded (not saved)"
        )

        self.postgres_connection_status_label.configure(
            bootstyle="secondary"
        )

    # ========================================================
    # PostgreSQL Settings - Test Connection
    # ========================================================

    def start_postgres_connection_test(
        self,
    ) -> None:

        if self.is_processing:
            return

        if (
            self.postgres_test_thread
            is not None
            and self.postgres_test_thread.is_alive()
        ):
            return

        settings = (
            self._collect_postgres_settings()
        )

        if settings is None:
            return

        typed_password = (
            self.postgres_password_var
            .get()
        )

        password = (
            typed_password
            or os.getenv(
                "POSTGRES_PASSWORD"
            )
        )

        if not password:

            messagebox.showwarning(
                "PostgreSQL Settings",
                (
                    "Enter the PostgreSQL password "
                    "before testing the connection."
                ),
                parent=self.root,
            )

            self.postgres_password_entry.focus_set()

            return

        # Make a typed password available to the current session.
        # Test Connection never persists the password by itself.
        if typed_password:

            os.environ[
                "POSTGRES_PASSWORD"
            ] = typed_password

            self._refresh_postgres_password_status()

        self.postgres_connection_status_var.set(
            "Testing connection..."
        )

        self.postgres_connection_status_label.configure(
            bootstyle="warning"
        )

        self.postgres_test_button.configure(
            state=DISABLED
        )

        self.postgres_test_thread = (
            threading.Thread(
                target=(
                    self._test_postgres_connection_worker
                ),
                kwargs={
                    "settings": settings,
                    "password": password,
                },
                daemon=True,
                name=(
                    "postgres-connection-test"
                ),
            )
        )

        self.postgres_test_thread.start()

    def _test_postgres_connection_worker(
        self,
        *,
        settings: PostgreSQLSettings,
        password: str,
    ) -> None:

        try:
            import psycopg

            with psycopg.connect(
                host=settings.host,
                port=settings.port,
                dbname=settings.database,
                user=settings.user,
                password=password,
                connect_timeout=(
                    settings.connect_timeout
                ),
            ) as connection:

                with connection.cursor() as cursor:

                    cursor.execute(
                        (
                            "SELECT "
                            "current_database(), "
                            "current_user"
                        )
                    )

                    row = cursor.fetchone()

            database_name = (
                row[0]
                if row
                else settings.database
            )

            user_name = (
                row[1]
                if row
                else settings.user
            )

            self._emit(
                "postgres_test_result",
                {
                    "success": True,
                    "message": (
                        "Connected successfully"
                    ),
                    "detail": (
                        f"Database: {database_name} | "
                        f"User: {user_name}"
                    ),
                },
            )

        except Exception as exc:

            self._emit(
                "postgres_test_result",
                {
                    "success": False,
                    "message": (
                        "Connection failed"
                    ),
                    "detail": str(
                        exc
                    ),
                },
            )

    def _handle_postgres_test_result(
        self,
        payload: Any,
    ) -> None:

        self.postgres_test_button.configure(
            state=(
                DISABLED
                if self.is_processing
                else NORMAL
            )
        )

        if not isinstance(
            payload,
            dict,
        ):
            return

        success = bool(
            payload.get(
                "success"
            )
        )

        message = str(
            payload.get(
                "message",
                "",
            )
        )

        detail = str(
            payload.get(
                "detail",
                "",
            )
        )

        if success:

            self.postgres_connection_verified = True

            self.postgres_create_tables_button.configure(
                state=(
                    DISABLED
                    if self.is_processing
                    else NORMAL
                )
            )
            if hasattr(self, "qabug_setup_button"):
                self.qabug_setup_button.configure(state=DISABLED if self.is_processing else NORMAL)
            # Import/quality become enabled after one-click initialization succeeds;
            # keeping them disabled here prevents accidental writes into an unverified schema.

            self.postgres_connection_status_var.set(
                (
                    f"{message}\n"
                    f"{detail}"
                )
            )

            self.postgres_connection_status_label.configure(
                bootstyle="success"
            )

            LOGGER.info(
                "PostgreSQL connection test succeeded."
            )

            # Make the successful Settings connection immediately available to
            # every embedded Console page.  v5.0.1 could leave those pages with
            # a stale service instance even though Test Connection succeeded.
            if hasattr(self, "enterprise_module"):
                self.enterprise_module.on_connection_verified()

            messagebox.showinfo(
                "PostgreSQL Connection",
                (
                    "Connection successful.\n\n"
                    f"{detail}"
                ),
                parent=self.root,
            )

            return

        self.postgres_connection_verified = False

        self.postgres_create_tables_button.configure(
            state=DISABLED
        )
        for name in ("qabug_setup_button", "qabug_import_qa_button", "qabug_import_bug_button", "qabug_quality_button"):
            if hasattr(self, name):
                getattr(self, name).configure(state=DISABLED)

        self.postgres_schema_status_var.set(
            "RAG schema not verified"
        )

        self.postgres_schema_status_label.configure(
            bootstyle="secondary"
        )

        self.postgres_connection_status_var.set(
            (
                f"{message}\n"
                f"{detail}"
            )
        )

        self.postgres_connection_status_label.configure(
            bootstyle="danger"
        )

        LOGGER.warning(
            "PostgreSQL connection test failed: %s",
            detail,
        )

        messagebox.showerror(
            "PostgreSQL Connection",
            (
                "Connection failed.\n\n"
                f"{detail}"
            ),
            parent=self.root,
        )

    # ========================================================
    # PostgreSQL Settings - Required Tables
    # ========================================================

    def _bind_postgres_setting_traces(
        self,
    ) -> None:
        """
        Invalidate the verified connection whenever connection input changes.

        This prevents the schema initialization button from remaining enabled
        after the user edits Host / Port / Database / User / Timeout / Password.
        """

        variables = (
            self.postgres_host_var,
            self.postgres_port_var,
            self.postgres_database_var,
            self.postgres_user_var,
            self.postgres_timeout_var,
            self.postgres_password_var,
        )

        for variable in variables:

            variable.trace_add(
                "write",
                self._on_postgres_settings_changed,
            )

    def _on_postgres_settings_changed(
        self,
        *_args: Any,
    ) -> None:

        self.postgres_connection_verified = False

        if hasattr(
            self,
            "postgres_create_tables_button",
        ):
            self.postgres_create_tables_button.configure(
                state=DISABLED
            )
        for name in ("qabug_setup_button", "qabug_import_qa_button", "qabug_import_bug_button", "qabug_quality_button"):
            if hasattr(self, name):
                getattr(self, name).configure(state=DISABLED)

        if hasattr(
            self,
            "postgres_schema_status_var",
        ):
            self.postgres_schema_status_var.set(
                "RAG schema not verified"
            )

        if hasattr(
            self,
            "postgres_schema_status_label",
        ):
            self.postgres_schema_status_label.configure(
                bootstyle="secondary"
            )

        # Keep the per-scope database mapping and the Document Conversion
        # target hint in sync while the single Database field is edited.
        try:
            project = ProjectRegistry.resolve(self.project_var.get())
        except ProjectRegistryError:
            pass
        else:
            database_name = self.postgres_database_var.get().strip()
            if database_name:
                self._project_database_names[project.code] = database_name
            if hasattr(self, "project_target_database_var"):
                self.project_target_database_var.set(
                    f"Target PostgreSQL: {database_name or '(not configured)'}"
                )

        self._refresh_json_import_target_info()
        self._refresh_version_target_info()
        if hasattr(self, "enterprise_module"):
            try:
                self.enterprise_module._rebuild_services()
            except Exception:
                pass

    def start_postgres_schema_initialization(
        self,
    ) -> None:
        """Initialize or non-destructively upgrade the RAG database schema."""

        if self.is_processing:
            return

        if not self.postgres_connection_verified:
            messagebox.showwarning(
                "PostgreSQL RAG Schema",
                (
                    "Test the PostgreSQL connection successfully "
                    "before initializing the RAG database."
                ),
                parent=self.root,
            )
            return

        if (
            self.postgres_schema_thread is not None
            and self.postgres_schema_thread.is_alive()
        ):
            return

        settings = self._collect_postgres_settings()
        if settings is None:
            return

        project = ProjectRegistry.resolve(self.project_var.get())
        if project.code == "QABUG":
            self.start_qabug_environment_setup()
            return

        password = (
            self.postgres_password_var.get()
            or os.getenv("POSTGRES_PASSWORD")
        )
        if not password:
            messagebox.showwarning(
                "PostgreSQL RAG Schema",
                (
                    "The PostgreSQL password is not available. "
                    "Enter the password and test the connection again."
                ),
                parent=self.root,
            )
            self.postgres_password_entry.focus_set()
            return

        confirmed = messagebox.askyesno(
            "Initialize / Upgrade Enterprise Database",
            (
                f"Initialize or upgrade PostgreSQL to RAG Schema v{RAG_SCHEMA_VERSION} "
                f"and Configuration Schema v{CONFIG_SCHEMA_VERSION}?\n\n"
                "Master / taxonomy tables:\n"
                "  • project_info\n"
                "  • spec_types\n"
                "  • spec_subtypes\n"
                "  • specifications\n"
                "  • specification_versions\n\n"
                "Core document tables:\n"
                "  • documents\n"
                "  • chapters\n"
                "  • sections\n"
                "  • contents\n"
                "  • embeddings\n\n"
                "Support / read models:\n"
                "  • vector_delete_queue\n"
                "  • schema_version (component registry)\n"
                "  • rag_chunks (all versions)\n"
                "  • rag_current_chunks (default/latest)\n"
                "  • specification_catalog\n"
                "  • specification_version_history\n"
                "  • RAG / Worker indexes\n\n"
                "Enterprise configuration control plane (app_config):\n"
                "  • app_settings / project_scopes\n"
                "  • ai_model_profiles / embedding_profiles\n"
                "  • rag_runtime_profiles / vector_collection_registry\n"
                "  • prompt_templates / prompt_versions\n"
                "  • data_quality_rules\n\n"
                "Safety guarantees:\n"
                "  • no DROP / TRUNCATE / business DELETE\n"
                "  • existing settings are preserved\n"
                "  • raw passwords / API keys are never seeded into PostgreSQL\n"
                "  • Qdrant points and embeddings are not rebuilt\n"
                "  • existing history is preserved\n"
                "Legacy duplicate indexes are reported for review."
            ),
            parent=self.root,
        )
        if not confirmed:
            return

        self.postgres_create_tables_button.configure(state=DISABLED)
        self.postgres_schema_status_var.set(
            f"Initializing / upgrading Enterprise Database (RAG v{RAG_SCHEMA_VERSION} / Config v{CONFIG_SCHEMA_VERSION})..."
        )
        self.postgres_schema_status_label.configure(bootstyle="warning")

        self.postgres_schema_thread = threading.Thread(
            target=self._initialize_postgres_schema_worker,
            kwargs={
                "settings": settings,
                "password": password,
                "project_code": project.code,
                "project_name": project.display_name,
            },
            daemon=True,
            name="postgres-rag-schema-initializer",
        )
        self.postgres_schema_thread.start()

    def _initialize_postgres_schema_worker(
        self,
        *,
        settings: PostgreSQLSettings,
        password: str,
        project_code: str,
        project_name: str,
    ) -> None:
        try:
            manager = SchemaManager(
                host=settings.host,
                port=settings.port,
                database=settings.database,
                user=settings.user,
                password=password,
                connect_timeout=settings.connect_timeout,
                project_code=project_code,
                project_name=project_name,
            )
            report = manager.ensure_schema()

            config_manager = ConfigurationSchemaManager(
                host=settings.host,
                port=settings.port,
                database=settings.database,
                user=settings.user,
                password=password,
                connect_timeout=settings.connect_timeout,
                project_code=project_code,
                project_name=project_name,
            )
            config_report = config_manager.ensure_schema()

            detail_lines = [report.status_text(), config_report.status_text()]
            if report.created_objects:
                detail_lines.append(
                    "Created / upgraded: "
                    + ", ".join(report.created_objects)
                )
            if report.equivalent_indexes_reused:
                detail_lines.append(
                    "Equivalent indexes reused: "
                    + str(len(report.equivalent_indexes_reused))
                )
            if report.duplicate_index_groups:
                detail_lines.append(
                    "Legacy duplicate indexes detected: "
                    + str(len(report.duplicate_index_groups))
                    + " group(s); left untouched for safe review."
                )
            if report.warnings:
                detail_lines.extend(report.warnings)
            if config_report.created_objects:
                detail_lines.append(
                    "Configuration objects ensured: "
                    + str(len(config_report.created_objects))
                )
            detail_lines.append(
                "Configuration defaults newly seeded: "
                + str(config_report.seeded_defaults)
            )
            if config_report.warnings:
                detail_lines.extend(config_report.warnings)

            enterprise_ready = report.ready and config_report.ready
            self._emit(
                "postgres_schema_result",
                {
                    "success": enterprise_ready,
                    "message": (
                        f"Enterprise Database is ready (RAG v{RAG_SCHEMA_VERSION} / Config v{CONFIG_SCHEMA_VERSION})"
                        if enterprise_ready
                        else "Enterprise database verification failed"
                    ),
                    "detail": "\n".join(detail_lines),
                    "duplicate_indexes": list(report.duplicate_index_groups),
                    "config_ready": config_report.ready,
                    "config_version": config_report.version,
                },
            )

        except Exception as exc:
            self._emit(
                "postgres_schema_result",
                {
                    "success": False,
                    "message": "Enterprise database initialization / upgrade failed",
                    "detail": str(exc),
                    "duplicate_indexes": [],
                },
            )

    def _handle_postgres_schema_result(
        self,
        payload: Any,
    ) -> None:
        if not isinstance(payload, dict):
            return

        success = bool(payload.get("success"))
        message = str(payload.get("message", ""))
        detail = str(payload.get("detail", ""))

        self.postgres_schema_status_var.set(
            f"{message}\n{detail}" if detail else message
        )

        if success:
            self.postgres_schema_status_label.configure(bootstyle="success")
            LOGGER.info(
                "PostgreSQL RAG schema is ready | version=%s",
                RAG_SCHEMA_VERSION,
            )
            messagebox.showinfo(
                "PostgreSQL Enterprise Database",
                (
                    f"RAG Schema v{RAG_SCHEMA_VERSION} + Configuration Schema v{CONFIG_SCHEMA_VERSION} are ready.\n\n"
                    "Master / taxonomy tables:\n"
                    "project_info / spec_types / spec_subtypes /\n"
                    "specifications / specification_versions\n\n"
                    "Core tables:\n"
                    "documents / chapters / sections / contents / embeddings\n\n"
                    "Views:\n"
                    "rag_chunks = all versions\n"
                    "rag_current_chunks = default/current only\n"
                    "specification_catalog = master + current version\n"
                    "specification_version_history = Main / History\n\n"
                    "Worker indexes: ready\n\n"
                    "Configuration control plane:\n"
                    "app_config.app_settings / project_scopes / model + embedding profiles /\n"
                    "RAG runtime / vector registry / prompts / data-quality rules\n\n"
                    "Existing configuration values and business data were preserved.\n"
                    "Any legacy duplicate indexes were left untouched."
                ),
                parent=self.root,
            )
        else:
            self.postgres_schema_status_label.configure(bootstyle="danger")
            LOGGER.error(
                "PostgreSQL RAG schema initialization failed: %s",
                detail,
            )
            messagebox.showerror(
                "PostgreSQL RAG Schema",
                f"{message}.\n\n{detail}",
                parent=self.root,
            )

        self.postgres_create_tables_button.configure(
            state=(
                NORMAL
                if self.postgres_connection_verified and not self.is_processing
                else DISABLED
            )
        )

    # ========================================================
    # Enterprise QA/BUG RAG Administration
    # ========================================================

    def _qabug_runtime_profiles(self) -> tuple[PostgresProfile, QdrantService, str]:
        """Build typed QA/BUG runtime profiles from the unified Settings page."""
        settings = self._collect_postgres_settings()
        if settings is None:
            raise ValueError("PostgreSQL 设置无效。")
        password = self.postgres_password_var.get() or os.getenv("POSTGRES_PASSWORD", "")
        if not password:
            raise ValueError("PostgreSQL 密码不可用，请先输入密码并测试连接。")
        target_db = self._project_database_names.get("QABUG", DEFAULT_QABUG_DATABASE) or DEFAULT_QABUG_DATABASE
        pg = PostgresProfile(
            host=settings.host, port=settings.port, database=target_db,
            user=settings.user, connect_timeout=settings.connect_timeout,
        )
        try:
            q_timeout = float(self.qdrant_timeout_var.get().strip() or "10")
        except ValueError as exc:
            raise ValueError("Qdrant Timeout 必须是数字。") from exc
        q_url = self.qdrant_url_var.get().strip() or "http://127.0.0.1:6333"
        q_key = self.qdrant_api_key_var.get() or get_secret("qdrant", q_url, "QDRANT_API_KEY")
        qdrant = QdrantService(QdrantProfile(url=q_url, timeout=max(1.0, q_timeout)), q_key)
        return pg, qdrant, password

    def start_qabug_environment_setup(self) -> None:
        """Safely create/upgrade the dedicated QA/BUG PostgreSQL + Qdrant contract."""
        if self.is_processing or (self.qabug_admin_thread and self.qabug_admin_thread.is_alive()):
            return
        if not self.postgres_connection_verified:
            messagebox.showwarning(
                "QA/BUG 企业 RAG",
                "请先在 Settings 中成功执行 PostgreSQL Test Connection。\n\n"
                "一键初始化会复用当前服务器、端口、用户和密码，但目标数据库固定隔离为 rag_qabug。",
                parent=self.root,
            )
            return
        try:
            pg, qdrant, password = self._qabug_runtime_profiles()
        except Exception as exc:
            messagebox.showerror("QA/BUG 企业 RAG", str(exc), parent=self.root)
            return
        if not messagebox.askyesno(
            "QA/BUG 一键初始化 / 升级",
            f"将初始化/升级独立 QA/BUG 数据域：\n\n"
            f"PostgreSQL: {pg.database}\nQdrant: {QABUG_COLLECTION}\n"
            f"Vector: BAAI/bge-m3 · 1024 · Cosine\n\n"
            "会创建缺失的表、视图、索引和 Qdrant Payload Index。\n"
            "不会 DROP / TRUNCATE / DELETE 既有业务数据，也不会重建现有式样书向量。\n\n继续？",
            parent=self.root,
        ):
            return
        for name in ("qabug_setup_button", "qabug_import_qa_button", "qabug_import_bug_button", "qabug_quality_button"):
            if hasattr(self, name):
                getattr(self, name).configure(state=DISABLED)
        self.qabug_admin_status_var.set("QA/BUG：正在初始化 PostgreSQL Schema + Qdrant Contract...")
        self.qabug_admin_thread = threading.Thread(
            target=self._qabug_environment_setup_worker,
            args=(pg, qdrant, password), daemon=True, name="qabug-enterprise-setup",
        )
        self.qabug_admin_thread.start()

    def _qabug_environment_setup_worker(self, pg: PostgresProfile, qdrant: QdrantService, password: str) -> None:
        try:
            schema = QABugSchemaManager(
                host=pg.host, port=pg.port, database=pg.database, user=pg.user,
                password=password, connect_timeout=pg.connect_timeout,
            ).ensure_schema(create_database=True)
            indexes = QABugQdrantManager(qdrant_service=qdrant).ensure_contract(repair_mismatched=True)
            self._emit("qabug_setup_result", {
                "success": bool(schema.ready and indexes.ok),
                "schema": schema.status_text(),
                "created_database": schema.created_database,
                "created_indexes": len(indexes.created),
                "repaired_indexes": len(indexes.repaired),
                "unchanged_indexes": len(indexes.unchanged),
                "mismatched_indexes": [f"{x.field_name}: {x.actual_type}->{x.expected_type}" for x in indexes.mismatched],
                "warnings": list(schema.warnings),
                "database": pg.database,
                "collection": QABUG_COLLECTION,
            })
        except Exception as exc:
            self._emit("qabug_setup_result", {"success": False, "error": str(exc)})

    def _handle_qabug_setup_result(self, payload: Any) -> None:
        data = payload if isinstance(payload, dict) else {"success": False, "error": str(payload)}
        success = bool(data.get("success"))
        if success:
            self._project_database_names["QABUG"] = str(data.get("database") or DEFAULT_QABUG_DATABASE)
            detail = (
                f"QA/BUG Ready · PostgreSQL={data.get('database')} · Qdrant={data.get('collection')} · "
                f"Payload Index 新建 {data.get('created_indexes', 0)} / 修复 {data.get('repaired_indexes', 0)} / 已存在 {data.get('unchanged_indexes', 0)}"
            )
            self.qabug_admin_status_var.set(detail)
            messagebox.showinfo("QA/BUG 企业 RAG", detail + "\n\n" + str(data.get("schema") or ""), parent=self.root)
        else:
            detail = str(data.get("error") or "QA/BUG 初始化校验失败")
            mismatched = data.get("mismatched_indexes") or []
            if mismatched:
                detail += "\nPayload Index 类型冲突：\n" + "\n".join(mismatched)
            self.qabug_admin_status_var.set("QA/BUG：初始化失败 · " + detail)
            messagebox.showerror("QA/BUG 企业 RAG", detail, parent=self.root)
        enabled = self.postgres_connection_verified and not self.is_processing
        if hasattr(self, "qabug_setup_button"):
            self.qabug_setup_button.configure(state=NORMAL if enabled else DISABLED)
        if hasattr(self, "qabug_import_qa_button"):
            self.qabug_import_qa_button.configure(state=NORMAL if success and enabled else DISABLED)
        if hasattr(self, "qabug_import_bug_button"):
            self.qabug_import_bug_button.configure(state=NORMAL if success and enabled else DISABLED)
        if hasattr(self, "qabug_quality_button"):
            self.qabug_quality_button.configure(state=NORMAL if success and enabled else DISABLED)

    def start_qabug_file_import(self, record_type: str) -> None:
        """Normalize -> validate -> SQL UPSERT -> BGE-M3 -> Qdrant for QA/BUG registers."""
        record_type = str(record_type).upper().strip()
        if record_type not in {"QA", "BUG"}:
            return
        if self.is_processing or (self.qabug_import_thread and self.qabug_import_thread.is_alive()):
            return
        try:
            pg, qdrant, password = self._qabug_runtime_profiles()
        except Exception as exc:
            messagebox.showerror("QA/BUG 导入", str(exc), parent=self.root)
            return
        selected = filedialog.askopenfilename(
            title=f"选择 {record_type} List",
            filetypes=[
                ("QA/BUG Register", "*.xlsx *.xlsm *.csv *.tsv *.txt"),
                ("Excel", "*.xlsx *.xlsm"), ("Text / CSV", "*.txt *.tsv *.csv"), ("All files", "*.*"),
            ],
        )
        if not selected:
            return
        if not messagebox.askyesno(
            f"导入 {record_type} 企业 RAG",
            f"文件：{selected}\n\n"
            "处理链：格式/编码识别 → 字段标准化 → 企业 Canonical JSON → PostgreSQL 幂等 UPSERT → "
            "BGE-M3 Embedding → 独立 Qdrant UPSERT → Hash/索引校验。\n\n"
            "未知 Jira Custom Field 会保留在 raw_json/custom_fields，不会静默丢弃。\n"
            "大文件可能需要较长时间。继续？",
            parent=self.root,
        ):
            return
        for name in ("qabug_setup_button", "qabug_import_qa_button", "qabug_import_bug_button", "qabug_quality_button"):
            if hasattr(self, name):
                getattr(self, name).configure(state=DISABLED)
        self.qabug_admin_status_var.set(f"QA/BUG：正在导入 {record_type} · {Path(selected).name}")
        self.qabug_import_thread = threading.Thread(
            target=self._qabug_file_import_worker,
            args=(selected, record_type, pg, qdrant, password), daemon=True, name=f"qabug-import-{record_type.lower()}",
        )
        self.qabug_import_thread.start()

    def _qabug_file_import_worker(self, selected: str, record_type: str, pg: PostgresProfile, qdrant: QdrantService, password: str) -> None:
        try:
            QABugSchemaManager(
                host=pg.host, port=pg.port, database=pg.database, user=pg.user,
                password=password, connect_timeout=pg.connect_timeout,
            ).ensure_schema(create_database=True)
            QABugQdrantManager(qdrant_service=qdrant).ensure_contract()
            pipeline = QABugImportPipeline()
            sql_report = pipeline.import_postgres(
                selected, record_type=record_type, profile=pg, password=password,
                progress=lambda n, key: self._emit("log", f"QA/BUG SQL: {n} records · {key}"),
            )
            vector_report = QABugQdrantManager(qdrant_service=qdrant).sync_from_postgres(
                postgres_profile=pg, password=password,
                progress=lambda n, total, key: self._emit("log", f"QA/BUG Qdrant: {n}/{total} chunks · {key}"),
            )
            quality = QABugDataQualityValidator(
                postgres_profile=pg, password=password, qdrant_service=qdrant,
            ).validate()
            self._emit("qabug_import_result", {
                "success": bool(sql_report.ok and quality.status == "PASS"),
                "record_type": record_type, "file": selected,
                "rows_seen": sql_report.rows_seen, "records": sql_report.records_upserted,
                "chunks": sql_report.chunks_upserted, "failed": sql_report.rows_failed,
                "quarantined": getattr(sql_report, "rows_quarantined", 0),
                "points": vector_report.points_upserted, "quality": quality.status,
                "quality_detail": {
                    "sql_records": quality.sql_records, "sql_chunks": quality.sql_chunks,
                    "qdrant_points": quality.qdrant_points, "missing": quality.missing_points,
                    "orphan": quality.orphan_points, "stale": quality.stale_points,
                    "quarantined": getattr(quality, "quarantined_records", 0),
                }, "errors": sql_report.errors[:20],
            })
        except Exception as exc:
            self._emit("qabug_import_result", {"success": False, "record_type": record_type, "file": selected, "error": str(exc)})

    def _handle_qabug_import_result(self, payload: Any) -> None:
        data = payload if isinstance(payload, dict) else {"success": False, "error": str(payload)}
        success = bool(data.get("success"))
        if success:
            q = data.get("quality_detail") or {}
            detail = (
                f"{data.get('record_type')} 导入完成 · Records={data.get('records', 0)} · "
                f"Chunks={data.get('chunks', 0)} · Qdrant Upsert={data.get('points', 0)} · DQ={data.get('quality')}\n"
                f"SQL records/chunks={q.get('sql_records', 0)}/{q.get('sql_chunks', 0)} · "
                f"Qdrant={q.get('qdrant_points', 0)} · missing/orphan/stale={q.get('missing', 0)}/{q.get('orphan', 0)}/{q.get('stale', 0)}"
            )
            self.qabug_admin_status_var.set("QA/BUG：" + detail.replace("\n", " · "))
            messagebox.showinfo("QA/BUG 导入", detail, parent=self.root)
        else:
            detail = str(data.get("error") or "QA/BUG 导入或数据质量校验失败")
            errors = data.get("errors") or []
            if errors:
                detail += "\n\n前 20 个错误：\n" + "\n".join(str(x) for x in errors)
            self.qabug_admin_status_var.set("QA/BUG：导入失败 · " + detail)
            messagebox.showerror("QA/BUG 导入", detail, parent=self.root)
        enabled = self.postgres_connection_verified and not self.is_processing
        for name in ("qabug_setup_button", "qabug_import_qa_button", "qabug_import_bug_button", "qabug_quality_button"):
            if hasattr(self, name):
                getattr(self, name).configure(state=NORMAL if enabled else DISABLED)

    def start_qabug_quality_validation(self) -> None:
        if self.is_processing or (self.qabug_admin_thread and self.qabug_admin_thread.is_alive()):
            return
        try:
            pg, qdrant, password = self._qabug_runtime_profiles()
        except Exception as exc:
            messagebox.showerror("QA/BUG 数据质量", str(exc), parent=self.root)
            return
        self.qabug_admin_status_var.set("QA/BUG：正在执行 SQL ↔ Qdrant 一致性检查...")
        self.qabug_admin_thread = threading.Thread(
            target=self._qabug_quality_worker, args=(pg, qdrant, password), daemon=True, name="qabug-quality",
        )
        self.qabug_admin_thread.start()

    def _qabug_quality_worker(self, pg: PostgresProfile, qdrant: QdrantService, password: str) -> None:
        try:
            report = QABugDataQualityValidator(postgres_profile=pg, password=password, qdrant_service=qdrant).validate()
            self._emit("qabug_quality_result", {"success": report.status == "PASS", "report": report})
        except Exception as exc:
            self._emit("qabug_quality_result", {"success": False, "error": str(exc)})

    def _handle_qabug_quality_result(self, payload: Any) -> None:
        data = payload if isinstance(payload, dict) else {"success": False, "error": str(payload)}
        report = data.get("report")
        if report is not None:
            detail = (
                f"DQ={report.status} · SQL Records={report.sql_records} · SQL Chunks={report.sql_chunks} · "
                f"Quarantine={getattr(report, 'quarantined_records', 0)} · Qdrant={report.qdrant_points} · "
                f"Missing={report.missing_points} · Orphan={report.orphan_points} · "
                f"Stale={report.stale_points} · Missing Index={len(report.missing_payload_indexes)} · "
                f"Index Mismatch={len(report.mismatched_payload_indexes)}"
            )
        else:
            detail = str(data.get("error") or "Validation failed")
        self.qabug_admin_status_var.set("QA/BUG：" + detail)
        if data.get("success"):
            messagebox.showinfo("QA/BUG 数据质量", detail, parent=self.root)
        else:
            messagebox.showwarning("QA/BUG 数据质量", detail, parent=self.root)

    def start_qabug_jsonl_export(self) -> None:
        """Offline canonical JSONL export for mapping/review before persistence."""
        selected = filedialog.askopenfilename(
            title="选择 QA/BUG List",
            filetypes=[("QA/BUG Register", "*.xlsx *.xlsm *.csv *.tsv *.txt"), ("All files", "*.*")],
        )
        if not selected:
            return
        try:
            record_type, confidence, scores = QABugImportPipeline().detect_record_type(selected)
        except Exception as exc:
            messagebox.showerror("Enterprise RAG JSONL", f"无法识别 QA/BUG 类型：{exc}", parent=self.root)
            return
        if confidence < 0.60:
            answer = messagebox.askyesnocancel(
                "确认 QA/BUG 类型",
                f"自动识别置信度较低：QA={scores.get('QA', 0)} / BUG={scores.get('BUG', 0)}。\n\n"
                "选择“是”按 QA 导出；选择“否”按 BUG 导出；取消则停止。",
                parent=self.root,
            )
            if answer is None:
                return
            record_type = "QA" if answer else "BUG"
        output = filedialog.asksaveasfilename(
            title="保存 Enterprise RAG JSONL",
            defaultextension=".jsonl",
            initialfile=f"{Path(selected).stem}_{record_type.lower()}_enterprise_rag.jsonl",
            filetypes=[("JSON Lines", "*.jsonl"), ("All files", "*.*")],
        )
        if not output:
            return
        try:
            report = QABugImportPipeline().export_jsonl(selected, output, record_type=record_type)
            messagebox.showinfo(
                "Enterprise RAG JSONL",
                f"导出完成：{report.jsonl_written} records\nType={record_type}\n{output}", parent=self.root,
            )
        except Exception as exc:
            messagebox.showerror("Enterprise RAG JSONL", str(exc), parent=self.root)

    # ========================================================
    # PostgreSQL Settings - Password Management
    # ========================================================

    def _refresh_postgres_password_status(
        self,
    ) -> None:

        available = bool(
            os.getenv(
                "POSTGRES_PASSWORD"
            )
        )

        self.postgres_password_status_var.set(
            (
                "Password available for this session"
                if available
                else (
                    "Password is not configured"
                )
            )
        )

        self.postgres_password_status_label.configure(
            bootstyle=(
                "success"
                if available
                else "warning"
            )
        )

    def clear_saved_postgres_password(
        self,
    ) -> None:

        if self.is_processing:
            return

        self.postgres_connection_verified = False

        confirmed = messagebox.askyesno(
            "Clear PostgreSQL Password",
            (
                "Remove the PostgreSQL password from "
                "this application session and from the "
                "current Windows user's saved environment?"
            ),
            parent=self.root,
        )

        if not confirmed:
            return

        os.environ.pop(
            "POSTGRES_PASSWORD",
            None,
        )

        try:
            self._delete_persisted_postgres_password()

        except OSError as exc:

            messagebox.showwarning(
                "PostgreSQL Password",
                (
                    "The password was cleared from this session, "
                    "but Windows could not remove the saved value.\n\n"
                    f"Reason: {exc}"
                ),
                parent=self.root,
            )

        self.postgres_password_var.set(
            ""
        )

        self.postgres_remember_password_var.set(
            False
        )

        self._refresh_postgres_password_status()

        self.postgres_connection_status_var.set(
            "Saved password cleared"
        )

        self.postgres_connection_status_label.configure(
            bootstyle="secondary"
        )

    @staticmethod
    def _delete_persisted_postgres_password(
    ) -> None:

        if os.name != "nt":
            return

        import winreg

        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                "Environment",
                0,
                winreg.KEY_SET_VALUE,
            ) as key:

                winreg.DeleteValue(
                    key,
                    "POSTGRES_PASSWORD",
                )

        except FileNotFoundError:
            return

        try:
            import ctypes

            HWND_BROADCAST = 0xFFFF
            WM_SETTINGCHANGE = 0x001A
            SMTO_ABORTIFHUNG = 0x0002

            ctypes.windll.user32.SendMessageTimeoutW(
                HWND_BROADCAST,
                WM_SETTINGCHANGE,
                0,
                "Environment",
                SMTO_ABORTIFHUNG,
                5000,
                None,
            )

        except Exception:
            pass

    # ========================================================
    # v5.1 Specification Management Center
    # ========================================================

    def _build_spec_page_header(self, parent: ttk.Frame, title: str, subtitle: str) -> ttk.Frame:
        header = ttk.Frame(parent)
        header.pack(fill=X, pady=(0, 10))
        text_area = ttk.Frame(header)
        text_area.pack(side=LEFT, fill=X, expand=True)
        ttk.Label(text_area, text=title, font=("Microsoft YaHei UI", 18, "bold")).pack(anchor="w")
        ttk.Label(text_area, text=subtitle, bootstyle="secondary", style="Small.TLabel").pack(anchor="w", pady=(2, 0))
        return header

    def _build_spec_selector(self, parent: ttk.Frame, *, on_change: Any | None = None) -> ttk.Combobox:
        box = ttk.Combobox(
            parent,
            textvariable=self.spec_active_selector_var,
            state="readonly",
            width=46,
        )
        if on_change is None:
            on_change = self._on_spec_selector_selected
        box.bind("<<ComboboxSelected>>", on_change)
        return box

    def _build_specification_catalog_page(self, parent: ttk.Frame) -> None:
        header = self._build_spec_page_header(
            parent,
            "Specification Catalog",
            "Master data, Current version, lifecycle status and direct navigation into version/change intelligence.",
        )
        ttk.Button(header, text="↻  Refresh", command=self.refresh_version_catalog, bootstyle="primary").pack(side=RIGHT)

        filters = ttk.LabelFrame(parent, text="Search & Filters", padding=10, bootstyle="secondary")
        filters.pack(fill=X, pady=(0, 10))
        filters.columnconfigure(1, weight=2)
        filters.columnconfigure(3, weight=1)
        filters.columnconfigure(5, weight=1)
        filters.columnconfigure(7, weight=1)
        ttk.Label(filters, text="Search").grid(row=0, column=0, sticky="w", padx=(0, 6))
        search = ttk.Entry(filters, textvariable=self.spec_catalog_search_var)
        search.grid(row=0, column=1, sticky="ew", padx=(0, 12))
        search.bind("<KeyRelease>", lambda _e: self._apply_spec_catalog_filters())
        ttk.Label(filters, text="Type").grid(row=0, column=2, sticky="w", padx=(0, 6))
        self.spec_catalog_type_combo = ttk.Combobox(filters, textvariable=self.spec_catalog_type_var, values=("All",), state="readonly", width=16)
        self.spec_catalog_type_combo.grid(row=0, column=3, sticky="ew", padx=(0, 12))
        self.spec_catalog_type_combo.bind("<<ComboboxSelected>>", lambda _e: self._apply_spec_catalog_filters())
        ttk.Label(filters, text="Region").grid(row=0, column=4, sticky="w", padx=(0, 6))
        self.spec_catalog_region_combo = ttk.Combobox(filters, textvariable=self.spec_catalog_region_var, values=("All",), state="readonly", width=16)
        self.spec_catalog_region_combo.grid(row=0, column=5, sticky="ew", padx=(0, 12))
        self.spec_catalog_region_combo.bind("<<ComboboxSelected>>", lambda _e: self._apply_spec_catalog_filters())
        ttk.Label(filters, text="Status").grid(row=0, column=6, sticky="w", padx=(0, 6))
        self.spec_catalog_status_combo = ttk.Combobox(filters, textvariable=self.spec_catalog_status_var, values=("All",), state="readonly", width=14)
        self.spec_catalog_status_combo.grid(row=0, column=7, sticky="ew", padx=(0, 8))
        self.spec_catalog_status_combo.bind("<<ComboboxSelected>>", lambda _e: self._apply_spec_catalog_filters())
        ttk.Button(filters, text="Reset", command=self._reset_spec_catalog_filters, bootstyle="secondary-outline").grid(row=0, column=8, sticky="e")
        ttk.Label(filters, textvariable=self.spec_catalog_visible_count_var, bootstyle="secondary", style="Small.TLabel").grid(
            row=1, column=0, columnspan=9, sticky="e", pady=(6, 0)
        )

        split = ttk.Panedwindow(parent, orient="horizontal")
        split.pack(fill=BOTH, expand=True)
        list_panel = ttk.LabelFrame(split, text="Specification Masters", padding=8, bootstyle="secondary")
        detail_panel = ttk.LabelFrame(split, text="Selected Specification", padding=12, bootstyle="secondary")
        split.add(list_panel, weight=5)
        split.add(detail_panel, weight=2)

        tree_wrap = ttk.Frame(list_panel)
        tree_wrap.pack(fill=BOTH, expand=True)
        self.version_master_tree = ttk.Treeview(
            tree_wrap,
            columns=("name", "current", "versions", "type", "region", "status", "updated"),
            show="headings",
            selectmode="browse",
            height=25,
        )
        for column, title, width, anchor in (
            ("name", "Specification", 260, "w"),
            ("current", "Current", 80, "center"),
            ("versions", "Versions", 70, "center"),
            ("type", "Type", 110, "center"),
            ("region", "Region", 105, "center"),
            ("status", "Status", 85, "center"),
            ("updated", "Updated", 130, "center"),
        ):
            self.version_master_tree.heading(column, text=title)
            self.version_master_tree.column(column, width=width, minwidth=60, anchor=anchor, stretch=(column == "name"))
        vs = ttk.Scrollbar(tree_wrap, orient=VERTICAL, command=self.version_master_tree.yview)
        hs = ttk.Scrollbar(tree_wrap, orient="horizontal", command=self.version_master_tree.xview)
        self.version_master_tree.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        self.version_master_tree.grid(row=0, column=0, sticky="nsew")
        vs.grid(row=0, column=1, sticky="ns")
        hs.grid(row=1, column=0, sticky="ew")
        tree_wrap.rowconfigure(0, weight=1)
        tree_wrap.columnconfigure(0, weight=1)
        self.version_master_tree.bind("<<TreeviewSelect>>", self._on_version_master_selected)
        self.version_master_tree.bind("<Double-1>", lambda _e: self._open_selected_spec_page("spec_versions"))

        ttk.Label(detail_panel, textvariable=self.spec_detail_name_var, font=("Microsoft YaHei UI", 12, "bold"), wraplength=360, justify=LEFT).pack(anchor="w", pady=(0, 10))
        details = ttk.Frame(detail_panel)
        details.pack(fill=X)
        detail_rows = (
            ("Specification Key", self.spec_detail_key_var),
            ("Current", self.spec_detail_current_var),
            ("Versions", self.spec_detail_versions_var),
            ("History", self.spec_detail_history_var),
            ("Type", self.spec_detail_type_var),
            ("Region", self.spec_detail_region_var),
            ("Status", self.spec_detail_status_var),
            ("Last Updated", self.spec_detail_updated_var),
        )
        for i, (label, var) in enumerate(detail_rows):
            ttk.Label(details, text=label, font=("Microsoft YaHei UI", 9, "bold")).grid(row=i, column=0, sticky="nw", padx=(0, 8), pady=3)
            ttk.Label(details, textvariable=var, wraplength=260, justify=LEFT, bootstyle="secondary").grid(row=i, column=1, sticky="nw", pady=3)
        details.columnconfigure(1, weight=1)
        ttk.Separator(detail_panel, orient="horizontal").pack(fill=X, pady=12)
        actions = ttk.Frame(detail_panel)
        actions.pack(fill=X)
        for label, page, style in (
            ("Open Versions", "spec_versions", "primary"),
            ("Compare Versions", "spec_compare", "secondary-outline"),
            ("View Changes", "spec_changes", "secondary-outline"),
            ("Engineering Relations", "spec_relations", "secondary-outline"),
        ):
            ttk.Button(actions, text=label, command=lambda p=page: self._open_selected_spec_page(p), bootstyle=style).pack(fill=X, pady=3, ipady=2)
        ttk.Label(
            detail_panel,
            text="Current is the default RAG target. Historical versions remain immutable and queryable explicitly.",
            wraplength=340,
            justify=LEFT,
            bootstyle="secondary",
            style="Small.TLabel",
        ).pack(anchor="w", pady=(14, 0))

        self.version_refresh_button = ttk.Button(parent, text="Refresh", command=self.refresh_version_catalog, bootstyle="primary")
        self.version_refresh_button.pack_forget()
        self.version_master_count_var.set("0 specifications")
        self._refresh_version_target_info()

    def _build_specification_versions_page(self, parent: ttk.Frame) -> None:
        header = self._build_spec_page_header(
            parent,
            "Version History",
            "Immutable business versions ordered semantically, independent of import sequence.",
        )
        controls = ttk.Frame(header)
        controls.pack(side=RIGHT)
        ttk.Label(controls, text="Specification").pack(side=LEFT, padx=(0, 6))
        self.spec_versions_selector = self._build_spec_selector(controls)
        self.spec_versions_selector.pack(side=LEFT, padx=(0, 8))
        ttk.Button(controls, text="Refresh", command=self._refresh_selected_specification, bootstyle="secondary-outline").pack(side=LEFT)

        summary = ttk.LabelFrame(parent, text="Active Specification", padding=12, bootstyle="secondary")
        summary.pack(fill=X, pady=(0, 10))
        ttk.Label(summary, textvariable=self.version_selected_master_var, font=("Microsoft YaHei UI", 11, "bold")).pack(side=LEFT, fill=X, expand=True)
        ttk.Button(summary, text="Compare", command=lambda: self.navigate("spec_compare"), bootstyle="primary").pack(side=RIGHT, padx=(6, 0))
        ttk.Button(summary, text="Relations", command=lambda: self.navigate("spec_relations"), bootstyle="secondary-outline").pack(side=RIGHT, padx=(6, 0))
        ttk.Button(summary, text="Changes", command=lambda: self.navigate("spec_changes"), bootstyle="secondary-outline").pack(side=RIGHT)

        history = ttk.LabelFrame(parent, text="Main Branch History", padding=8, bootstyle="secondary")
        history.pack(fill=BOTH, expand=True)
        wrap = ttk.Frame(history)
        wrap.pack(fill=BOTH, expand=True)
        self.version_history_tree = ttk.Treeview(
            wrap,
            columns=("version", "role", "previous", "added", "modified", "removed", "moved", "changes", "imported"),
            show="headings",
            selectmode="browse",
            height=25,
        )
        for column, title, width, anchor in (
            ("version", "Version", 85, "center"), ("role", "Role", 85, "center"),
            ("previous", "Previous", 85, "center"), ("added", "+", 55, "center"),
            ("modified", "~", 55, "center"), ("removed", "-", 55, "center"),
            ("moved", "↔", 55, "center"), ("changes", "Total Δ", 75, "center"),
            ("imported", "Imported", 145, "center"),
        ):
            self.version_history_tree.heading(column, text=title)
            self.version_history_tree.column(column, width=width, minwidth=45, anchor=anchor, stretch=(column == "imported"))
        vs = ttk.Scrollbar(wrap, orient=VERTICAL, command=self.version_history_tree.yview)
        hs = ttk.Scrollbar(wrap, orient="horizontal", command=self.version_history_tree.xview)
        self.version_history_tree.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        self.version_history_tree.grid(row=0, column=0, sticky="nsew")
        vs.grid(row=0, column=1, sticky="ns")
        hs.grid(row=1, column=0, sticky="ew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        ttk.Label(
            history,
            text="version_seq is audit/import order. Version display/order uses version_sort_key. Late historical imports are spliced into the semantic chain without downgrading Current.",
            wraplength=1200,
            justify=LEFT,
            bootstyle="secondary",
            style="Small.TLabel",
        ).pack(anchor="w", pady=(8, 0))

    @staticmethod
    def _build_diff_legend(parent: ttk.Frame) -> None:
        legend = ttk.Frame(parent)
        legend.pack(fill=X, pady=(0, 8))
        ttk.Label(
            legend,
            text="Legend",
            font=("Microsoft YaHei UI", 9, "bold"),
            bootstyle="secondary",
        ).pack(side=LEFT, padx=(0, 8))
        items = (
            ("ADDED", DIFF_PALETTE["added"]),
            ("REMOVED", DIFF_PALETTE["removed"]),
            ("MODIFIED(old)", DIFF_PALETTE["modified_old"]),
            ("MODIFIED(new)", DIFF_PALETTE["modified_new"]),
            ("MOVED", DIFF_PALETTE["moved"]),
        )
        for label, palette in items:
            chip = tk.Label(
                legend,
                text=f"  {label}  ",
                bg=palette["bg"],
                fg=palette["fg"],
                font=("Microsoft YaHei UI", 8, "bold"),
                bd=1,
                relief="solid",
                padx=2,
                pady=1,
            )
            chip.pack(side=LEFT, padx=(0, 6))
        ttk.Label(
            legend,
            text="Low-saturation colors for comfortable long reading.",
            bootstyle="secondary",
            style="Small.TLabel",
        ).pack(side=LEFT, padx=(8, 0))

    @staticmethod
    def _configure_change_tree_tags(tree: ttk.Treeview) -> None:
        tags = {
            "change-added": DIFF_PALETTE["added"],
            "change-removed": DIFF_PALETTE["removed"],
            "change-modified": DIFF_PALETTE["modified_new"],
            "change-moved": DIFF_PALETTE["moved"],
        }
        for tag, palette in tags.items():
            tree.tag_configure(tag, background=palette["bg"], foreground=palette["fg"])

    @staticmethod
    def _configure_diff_text_widget(widget: tk.Text) -> None:
        widget.configure(bg="#FBFBFC", fg="#1F2933", insertbackground="#1F2933")
        widget.tag_configure("muted-placeholder", foreground=DIFF_PALETTE["muted"]["fg"], background=DIFF_PALETTE["muted"]["bg"])
        widget.tag_configure("added", background=DIFF_PALETTE["added"]["bg"], foreground=DIFF_PALETTE["added"]["fg"])
        widget.tag_configure("removed", background=DIFF_PALETTE["removed"]["bg"], foreground=DIFF_PALETTE["removed"]["fg"], overstrike=True)
        widget.tag_configure("modified-old", background=DIFF_PALETTE["modified_old"]["bg"], foreground=DIFF_PALETTE["modified_old"]["fg"])
        widget.tag_configure("modified-new", background=DIFF_PALETTE["modified_new"]["bg"], foreground=DIFF_PALETTE["modified_new"]["fg"])
        widget.tag_configure("moved", background=DIFF_PALETTE["moved"]["bg"], foreground=DIFF_PALETTE["moved"]["fg"])

    @staticmethod
    def _tokenize_diff_text(value: str) -> list[str]:
        return re.findall(r"\s+|\S+", value or "")

    def _insert_diff_tokens(self, widget: tk.Text, tokens: list[str], tag: str | None = None) -> None:
        for token in tokens:
            widget.insert(END, token, (() if tag is None else (tag,)))

    def _render_text_diff(
        self,
        old_widget: tk.Text,
        new_widget: tk.Text,
        old_text: str,
        new_text: str,
        *,
        change_type: str = "MODIFIED",
    ) -> None:
        old_value = str(old_text or "")
        new_value = str(new_text or "")
        old_widget.configure(state=NORMAL)
        new_widget.configure(state=NORMAL)
        old_widget.delete("1.0", END)
        new_widget.delete("1.0", END)

        normalized_change = (change_type or "").upper()
        if not old_value and not new_value:
            old_widget.insert("1.0", "(not present)", ("muted-placeholder",))
            new_widget.insert("1.0", "(not present)", ("muted-placeholder",))
        elif normalized_change == "ADDED":
            old_widget.insert("1.0", "(not present in old version)", ("muted-placeholder",))
            new_widget.insert("1.0", new_value, ("added",))
        elif normalized_change == "REMOVED":
            old_widget.insert("1.0", old_value, ("removed",))
            new_widget.insert("1.0", "(removed in new version)", ("muted-placeholder",))
        elif normalized_change == "MOVED":
            old_widget.insert("1.0", old_value, ("moved",))
            new_widget.insert("1.0", new_value, ("moved",))
        else:
            mode = self.version_diff_granularity_var.get().strip().upper() or "WORD"
            if mode == "LINE":
                old_lines = old_value.splitlines(keepends=True)
                new_lines = new_value.splitlines(keepends=True)
                matcher = difflib.SequenceMatcher(None, old_lines, new_lines)
                for opcode, a0, a1, b0, b1 in matcher.get_opcodes():
                    old_chunk = old_lines[a0:a1]
                    new_chunk = new_lines[b0:b1]
                    if opcode == "equal":
                        self._insert_diff_tokens(old_widget, old_chunk)
                        self._insert_diff_tokens(new_widget, new_chunk)
                    elif opcode == "delete":
                        self._insert_diff_tokens(old_widget, old_chunk, "removed")
                    elif opcode == "insert":
                        self._insert_diff_tokens(new_widget, new_chunk, "added")
                    elif opcode == "replace":
                        self._insert_diff_tokens(old_widget, old_chunk, "modified-old")
                        self._insert_diff_tokens(new_widget, new_chunk, "modified-new")
            else:
                old_tokens = self._tokenize_diff_text(old_value)
                new_tokens = self._tokenize_diff_text(new_value)
                matcher = difflib.SequenceMatcher(None, old_tokens, new_tokens)
                for opcode, a0, a1, b0, b1 in matcher.get_opcodes():
                    old_chunk = old_tokens[a0:a1]
                    new_chunk = new_tokens[b0:b1]
                    if opcode == "equal":
                        self._insert_diff_tokens(old_widget, old_chunk)
                        self._insert_diff_tokens(new_widget, new_chunk)
                    elif opcode == "delete":
                        self._insert_diff_tokens(old_widget, old_chunk, "removed")
                    elif opcode == "insert":
                        self._insert_diff_tokens(new_widget, new_chunk, "added")
                    elif opcode == "replace":
                        self._insert_diff_tokens(old_widget, old_chunk, "modified-old")
                        self._insert_diff_tokens(new_widget, new_chunk, "modified-new")

        old_widget.configure(state=DISABLED)
        new_widget.configure(state=DISABLED)

    def _register_diff_mode_button(self, button: tk.Button, mode: str) -> None:
        self._diff_mode_buttons.append((button, mode))
        self._refresh_diff_mode_buttons()

    def _refresh_diff_mode_buttons(self) -> None:
        active = self.version_diff_granularity_var.get().strip().upper() or "WORD"
        for button, mode in getattr(self, "_diff_mode_buttons", []):
            if not button.winfo_exists():
                continue
            selected = mode == active
            button.configure(
                relief=(tk.SUNKEN if selected else tk.FLAT),
                bg=("#EAF2FF" if selected else "#F7F8FA"),
                fg=("#1F4E8C" if selected else "#4B5563"),
                activebackground="#EAF2FF",
                activeforeground="#1F4E8C",
            )

    def _set_version_diff_granularity(self, mode: str) -> None:
        mode = str(mode or "WORD").strip().upper()
        if mode not in {"WORD", "LINE"}:
            mode = "WORD"
        self.version_diff_granularity_var.set(mode)
        self._refresh_diff_mode_buttons()
        self._rerender_selected_version_diff()
        self._rerender_selected_change_center_diff()

    def _build_diff_mode_switch(self, parent: ttk.Frame) -> None:
        holder = ttk.Frame(parent)
        holder.pack(side=RIGHT)
        ttk.Label(holder, text="Highlight", bootstyle="secondary", style="Small.TLabel").pack(side=LEFT, padx=(0, 5))
        for mode, label in (("WORD", "Aa  Word"), ("LINE", "≡  Line")):
            button = tk.Button(
                holder,
                text=label,
                width=8,
                font=("Microsoft YaHei UI", 8, "bold"),
                bd=1,
                padx=4,
                pady=2,
                cursor="hand2",
                command=lambda selected=mode: self._set_version_diff_granularity(selected),
            )
            button.pack(side=LEFT, padx=(0, 4))
            self._register_diff_mode_button(button, mode)

    def _make_change_filter_button(
        self,
        parent: ttk.Frame,
        *,
        label: str,
        icon: str,
        value: str,
        palette_key: str | None,
        command: Any,
        registry: list[tuple[tk.Button, str]],
    ) -> None:
        palette = DIFF_PALETTE.get(palette_key or "muted", DIFF_PALETTE["muted"])
        button = tk.Button(
            parent,
            text=f"{icon}\n{label}",
            width=8,
            height=2,
            font=("Microsoft YaHei UI", 8, "bold"),
            bd=1,
            relief=tk.FLAT,
            bg="#F7F8FA",
            fg="#4B5563",
            activebackground=palette["bg"],
            activeforeground=palette["fg"],
            cursor="hand2",
            command=lambda selected=value: command(selected),
        )
        button.pack(fill=X, pady=(0, 5))
        registry.append((button, value))

    @staticmethod
    def _refresh_change_filter_buttons(
        registry: list[tuple[tk.Button, str]],
        active: str,
    ) -> None:
        palette_by_value = {
            "ADDED": DIFF_PALETTE["added"],
            "REMOVED": DIFF_PALETTE["removed"],
            "MODIFIED": DIFF_PALETTE["modified_new"],
            "MOVED": DIFF_PALETTE["moved"],
            "ALL": DIFF_PALETTE["muted"],
        }
        for button, value in registry:
            if not button.winfo_exists():
                continue
            selected = value == active
            palette = palette_by_value.get(value, DIFF_PALETTE["muted"])
            button.configure(
                relief=(tk.SUNKEN if selected else tk.FLAT),
                bg=(palette["bg"] if selected else "#F7F8FA"),
                fg=(palette["fg"] if selected else "#4B5563"),
            )

    def _build_version_change_filter_rail(self, parent: ttk.Frame) -> None:
        rail = ttk.Frame(parent)
        rail.pack(side=LEFT, fill=Y, padx=(0, 8))
        ttk.Label(rail, text="Show", font=("Microsoft YaHei UI", 8, "bold"), bootstyle="secondary").pack(pady=(1, 6))
        items = (
            ("All", "◎", "ALL", None),
            ("Added", "+", "ADDED", "added"),
            ("Removed", "−", "REMOVED", "removed"),
            ("Modified", "✎", "MODIFIED", "modified_new"),
            ("Moved", "↔", "MOVED", "moved"),
        )
        for label, icon, value, palette_key in items:
            self._make_change_filter_button(
                rail,
                label=label,
                icon=icon,
                value=value,
                palette_key=palette_key,
                command=self._set_version_change_filter,
                registry=self._version_filter_buttons,
            )
        ttk.Label(rail, textvariable=self.version_change_visible_var, bootstyle="secondary", style="Small.TLabel").pack(pady=(4, 0))
        self._refresh_change_filter_buttons(self._version_filter_buttons, self.version_change_filter_var.get())

    def _build_change_center_filter_rail(self, parent: ttk.Frame) -> None:
        rail = ttk.Frame(parent)
        rail.pack(side=LEFT, fill=Y, padx=(0, 8))
        ttk.Label(rail, text="Show", font=("Microsoft YaHei UI", 8, "bold"), bootstyle="secondary").pack(pady=(1, 6))
        items = (
            ("All", "◎", "ALL", None),
            ("Added", "+", "ADDED", "added"),
            ("Removed", "−", "REMOVED", "removed"),
            ("Modified", "✎", "MODIFIED", "modified_new"),
            ("Moved", "↔", "MOVED", "moved"),
        )
        for label, icon, value, palette_key in items:
            self._make_change_filter_button(
                rail,
                label=label,
                icon=icon,
                value=value,
                palette_key=palette_key,
                command=self._set_change_center_quick_filter,
                registry=self._change_center_filter_buttons,
            )
        self._refresh_change_filter_buttons(self._change_center_filter_buttons, self.change_type_filter_var.get())

    def _set_version_change_filter(self, value: str) -> None:
        value = str(value or "ALL").strip().upper()
        if value not in {"ALL", "ADDED", "REMOVED", "MODIFIED", "MOVED"}:
            value = "ALL"
        self.version_change_filter_var.set(value)
        self._refresh_change_filter_buttons(self._version_filter_buttons, value)
        self._apply_version_change_filter()

    def _set_change_center_quick_filter(self, value: str) -> None:
        value = str(value or "ALL").strip().upper()
        if value not in {"ALL", "ADDED", "REMOVED", "MODIFIED", "MOVED"}:
            value = "ALL"
        self.change_type_filter_var.set(value)
        self._refresh_change_filter_buttons(self._change_center_filter_buttons, value)
        self._apply_change_detail_filters()

    def _apply_version_change_filter(self) -> None:
        if not hasattr(self, "version_change_tree"):
            return
        for iid in self.version_change_tree.get_children():
            self.version_change_tree.delete(iid)
        self._version_change_by_iid.clear()
        selected_filter = self.version_change_filter_var.get().strip().upper() or "ALL"
        visible = 0
        for row in self._version_compare_changes:
            change_type = str(row.get("change_type") or "").strip().upper()
            if selected_filter != "ALL" and change_type != selected_filter:
                continue
            iid = f"change-{row['id']}"
            self._version_change_by_iid[iid] = row
            similarity = row.get("similarity_score")
            similarity_text = f"{float(similarity) * 100:.0f}%" if similarity is not None else "-"
            change_tag = f"change-{change_type.lower()}"
            self.version_change_tree.insert(
                "",
                END,
                iid=iid,
                values=(
                    change_type or "-",
                    row.get("chapter_title") or row.get("chapter_id") or "-",
                    row.get("section_title") or row.get("section_id") or row.get("entity_key") or "-",
                    similarity_text,
                ),
                tags=(change_tag,),
            )
            visible += 1
        self.version_change_visible_var.set(f"{visible} shown")
        self._set_readonly_text(self.version_old_text, "")
        self._set_readonly_text(self.version_new_text, "")

    def _rerender_selected_version_diff(self) -> None:
        if not hasattr(self, "version_change_tree"):
            return
        selection = self.version_change_tree.selection()
        if not selection:
            return
        row = self._version_change_by_iid.get(selection[0])
        if not row:
            return
        self._render_text_diff(
            self.version_old_text,
            self.version_new_text,
            str(row.get("old_text") or ""),
            str(row.get("new_text") or ""),
            change_type=str(row.get("change_type") or "MODIFIED"),
        )

    def _rerender_selected_change_center_diff(self) -> None:
        if not hasattr(self, "change_center_tree"):
            return
        selection = self.change_center_tree.selection()
        if not selection:
            return
        row = self._change_detail_by_iid.get(selection[0])
        if not row:
            return
        self._render_text_diff(
            self.change_center_old_text,
            self.change_center_new_text,
            str(row.get("old_text") or ""),
            str(row.get("new_text") or ""),
            change_type=str(row.get("change_type") or "MODIFIED"),
        )

    def _build_specification_compare_page(self, parent: ttk.Frame) -> None:
        header = self._build_spec_page_header(
            parent,
            "Version Compare",
            "Deterministic section-level diff. From is always older than To; illegal directions are removed from the selectors.",
        )
        specbox = ttk.Frame(header)
        specbox.pack(side=RIGHT)
        ttk.Label(specbox, text="Specification").pack(side=LEFT, padx=(0, 6))
        self.spec_compare_selector = self._build_spec_selector(specbox)
        self.spec_compare_selector.pack(side=LEFT)

        compare = ttk.LabelFrame(parent, text="Compare Versions", padding=12, bootstyle="secondary")
        compare.pack(fill=BOTH, expand=True)
        selectors = ttk.Frame(compare)
        selectors.pack(fill=X)
        selectors.columnconfigure(1, weight=1)
        selectors.columnconfigure(3, weight=1)
        ttk.Label(selectors, text="From", font=("Microsoft YaHei UI", 9, "bold")).grid(row=0, column=0, sticky="w", padx=(0, 6))
        self.version_compare_from_combo = ttk.Combobox(selectors, textvariable=self.version_compare_from_var, state="readonly", width=18)
        self.version_compare_from_combo.grid(row=0, column=1, sticky="ew", padx=(0, 12))
        self.version_compare_from_combo.bind("<<ComboboxSelected>>", self._on_version_compare_from_selected)
        ttk.Label(selectors, text="To", font=("Microsoft YaHei UI", 9, "bold")).grid(row=0, column=2, sticky="w", padx=(0, 6))
        self.version_compare_to_combo = ttk.Combobox(selectors, textvariable=self.version_compare_to_var, state="readonly", width=18)
        self.version_compare_to_combo.grid(row=0, column=3, sticky="ew", padx=(0, 12))
        self.version_compare_to_combo.bind("<<ComboboxSelected>>", self._on_version_compare_to_selected)
        self.version_compare_button = ttk.Button(selectors, text="Compare", command=self.start_version_compare, bootstyle="success")
        self.version_compare_button.grid(row=0, column=4, sticky="e")

        summary = ttk.Frame(compare)
        summary.pack(fill=X, pady=(10, 6))
        for variable in (self.version_added_var, self.version_modified_var, self.version_removed_var, self.version_moved_var):
            ttk.Label(summary, textvariable=variable, font=("Microsoft YaHei UI", 10, "bold"), bootstyle="primary").pack(side=LEFT, padx=(0, 18))
        ttk.Button(summary, text="Open Change Center", command=lambda: self.navigate("spec_changes"), bootstyle="secondary-outline").pack(side=RIGHT)
        ttk.Label(compare, textvariable=self.version_compare_summary_var, wraplength=1200, justify=LEFT, bootstyle="secondary").pack(fill=X, pady=(0, 4))
        diff_controls = ttk.Frame(compare)
        diff_controls.pack(fill=X, pady=(0, 6))
        legend_host = ttk.Frame(diff_controls)
        legend_host.pack(side=LEFT, fill=X, expand=True)
        self._build_diff_legend(legend_host)
        self._build_diff_mode_switch(diff_controls)

        split = ttk.Panedwindow(compare, orient="vertical")
        split.pack(fill=BOTH, expand=True)
        list_frame = ttk.Frame(split)
        detail_frame = ttk.Panedwindow(split, orient="horizontal")
        split.add(list_frame, weight=3)
        split.add(detail_frame, weight=2)
        self._build_version_change_filter_rail(list_frame)
        self.version_change_tree = ttk.Treeview(
            list_frame,
            columns=("type", "chapter", "section", "similarity"), show="headings", selectmode="browse", height=13,
        )
        for column, title, width, anchor in (
            ("type", "Change", 90, "center"), ("chapter", "Chapter", 180, "w"),
            ("section", "Section", 420, "w"), ("similarity", "Match", 75, "center"),
        ):
            self.version_change_tree.heading(column, text=title)
            self.version_change_tree.column(column, width=width, minwidth=60, anchor=anchor, stretch=(column in {"chapter", "section"}))
        scroll = ttk.Scrollbar(list_frame, orient=VERTICAL, command=self.version_change_tree.yview)
        self.version_change_tree.configure(yscrollcommand=scroll.set)
        self.version_change_tree.pack(side=LEFT, fill=BOTH, expand=True)
        scroll.pack(side=RIGHT, fill=Y)
        self._configure_change_tree_tags(self.version_change_tree)
        self.version_change_tree.bind("<<TreeviewSelect>>", self._on_version_change_selected)
        old_frame = ttk.LabelFrame(detail_frame, text="Old Version", padding=6, bootstyle="secondary")
        new_frame = ttk.LabelFrame(detail_frame, text="New Version", padding=6, bootstyle="secondary")
        detail_frame.add(old_frame, weight=1)
        detail_frame.add(new_frame, weight=1)
        self.version_old_text = tk.Text(old_frame, wrap=WORD, height=12, font=("Consolas", 9), state=DISABLED)
        self.version_new_text = tk.Text(new_frame, wrap=WORD, height=12, font=("Consolas", 9), state=DISABLED)
        self._configure_diff_text_widget(self.version_old_text)
        self._configure_diff_text_widget(self.version_new_text)
        self.version_old_text.pack(fill=BOTH, expand=True)
        self.version_new_text.pack(fill=BOTH, expand=True)

        policy = ttk.LabelFrame(parent, text="RAG Query Policy", padding=8, bootstyle="secondary")
        policy.pack(fill=X, pady=(8, 0))
        ttk.Label(policy, text="Default retrieval uses rag_current_chunks (Current only). Historical versions are retained for explicit lookup, comparison and audit.", bootstyle="secondary", style="Small.TLabel").pack(anchor="w")

    def _build_specification_changes_page(self, parent: ttk.Frame) -> None:
        header = self._build_spec_page_header(
            parent,
            "Change Center",
            "Adjacent-version change timeline produced automatically during ingestion, with auditable ADDED / MODIFIED / REMOVED / MOVED facts.",
        )
        specbox = ttk.Frame(header)
        specbox.pack(side=RIGHT)
        ttk.Label(specbox, text="Specification").pack(side=LEFT, padx=(0, 6))
        self.spec_changes_selector = self._build_spec_selector(specbox, on_change=self._on_change_spec_selector_selected)
        self.spec_changes_selector.pack(side=LEFT, padx=(0, 8))
        ttk.Button(specbox, text="Refresh", command=self.refresh_change_timeline, bootstyle="secondary-outline").pack(side=LEFT)

        split = ttk.Panedwindow(parent, orient="horizontal")
        split.pack(fill=BOTH, expand=True)
        timeline_panel = ttk.LabelFrame(split, text="Version Change Timeline", padding=8, bootstyle="secondary")
        detail_panel = ttk.LabelFrame(split, text="Change Details", padding=8, bootstyle="secondary")
        split.add(timeline_panel, weight=2)
        split.add(detail_panel, weight=5)

        timeline_body = ttk.Frame(timeline_panel)
        timeline_body.pack(fill=BOTH, expand=True)
        self.change_timeline_tree = ttk.Treeview(
            timeline_body,
            columns=("transition", "added", "modified", "removed", "moved", "total", "created"),
            show="headings", selectmode="browse", height=25,
        )
        for column, title, width, anchor in (
            ("transition", "Version Transition", 150, "w"), ("added", "+", 45, "center"),
            ("modified", "~", 45, "center"), ("removed", "-", 45, "center"),
            ("moved", "↔", 45, "center"), ("total", "Total", 55, "center"),
            ("created", "Generated", 125, "center"),
        ):
            self.change_timeline_tree.heading(column, text=title)
            self.change_timeline_tree.column(column, width=width, minwidth=40, anchor=anchor, stretch=(column == "transition"))
        tl_scroll = ttk.Scrollbar(timeline_body, orient=VERTICAL, command=self.change_timeline_tree.yview)
        self.change_timeline_tree.configure(yscrollcommand=tl_scroll.set)
        self.change_timeline_tree.pack(side=LEFT, fill=BOTH, expand=True)
        tl_scroll.pack(side=RIGHT, fill=Y)
        self.change_timeline_tree.bind("<<TreeviewSelect>>", self._on_change_timeline_selected)
        ttk.Label(timeline_panel, textvariable=self.change_center_status_var, wraplength=410, justify=LEFT, bootstyle="secondary", style="Small.TLabel").pack(fill=X, pady=(8, 0))

        toolbar = ttk.Frame(detail_panel)
        toolbar.pack(fill=X, pady=(0, 6))
        self.change_type_combo = ttk.Combobox(toolbar, textvariable=self.change_type_filter_var, values=("ALL", "ADDED", "MODIFIED", "REMOVED", "MOVED"), state="readonly", width=12)
        self.change_type_combo.bind("<<ComboboxSelected>>", lambda _e: self._apply_change_detail_filters())
        ttk.Label(toolbar, text="Search").pack(side=LEFT, padx=(0, 5))
        change_search = ttk.Entry(toolbar, textvariable=self.change_search_var, width=30)
        change_search.pack(side=LEFT, fill=X, expand=True)
        change_search.bind("<KeyRelease>", lambda _e: self._apply_change_detail_filters())
        self._build_diff_mode_switch(toolbar)
        ttk.Label(detail_panel, textvariable=self.change_center_summary_var, font=("Microsoft YaHei UI", 10, "bold"), wraplength=900, justify=LEFT).pack(fill=X, pady=(0, 4))
        self._build_diff_legend(detail_panel)

        details_split = ttk.Panedwindow(detail_panel, orient="vertical")
        details_split.pack(fill=BOTH, expand=True)
        changes_frame = ttk.Frame(details_split)
        text_split = ttk.Panedwindow(details_split, orient="horizontal")
        details_split.add(changes_frame, weight=3)
        details_split.add(text_split, weight=2)
        self._build_change_center_filter_rail(changes_frame)
        self.change_center_tree = ttk.Treeview(
            changes_frame,
            columns=("type", "chapter", "section", "match"), show="headings", selectmode="browse", height=13,
        )
        for column, title, width, anchor in (
            ("type", "Change", 90, "center"), ("chapter", "Chapter", 170, "w"),
            ("section", "Section", 360, "w"), ("match", "Match", 70, "center"),
        ):
            self.change_center_tree.heading(column, text=title)
            self.change_center_tree.column(column, width=width, minwidth=55, anchor=anchor, stretch=(column in {"chapter", "section"}))
        dc_scroll = ttk.Scrollbar(changes_frame, orient=VERTICAL, command=self.change_center_tree.yview)
        self.change_center_tree.configure(yscrollcommand=dc_scroll.set)
        self.change_center_tree.pack(side=LEFT, fill=BOTH, expand=True)
        dc_scroll.pack(side=RIGHT, fill=Y)
        self._configure_change_tree_tags(self.change_center_tree)
        self.change_center_tree.bind("<<TreeviewSelect>>", self._on_change_center_detail_selected)
        old_frame = ttk.LabelFrame(text_split, text="Before", padding=6, bootstyle="secondary")
        new_frame = ttk.LabelFrame(text_split, text="After", padding=6, bootstyle="secondary")
        text_split.add(old_frame, weight=1)
        text_split.add(new_frame, weight=1)
        self.change_center_old_text = tk.Text(old_frame, wrap=WORD, height=12, font=("Consolas", 9), state=DISABLED)
        self.change_center_new_text = tk.Text(new_frame, wrap=WORD, height=12, font=("Consolas", 9), state=DISABLED)
        self._configure_diff_text_widget(self.change_center_old_text)
        self._configure_diff_text_widget(self.change_center_new_text)
        self.change_center_old_text.pack(fill=BOTH, expand=True)
        self.change_center_new_text.pack(fill=BOTH, expand=True)

    def _build_engineering_relations_page(self, parent: ttk.Frame) -> None:
        header = self._build_spec_page_header(
            parent,
            "Engineering Relation Registry",
            "Deterministic, version-aware engineering facts extracted during ingestion before embedding/Qdrant indexing. Every rendered edge is backed by source evidence.",
        )
        actions = ttk.Frame(header)
        actions.pack(side=RIGHT)
        self.relation_current_only_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            actions,
            text="Current versions only",
            variable=self.relation_current_only_var,
            command=self.refresh_engineering_relations,
            bootstyle="round-toggle",
        ).pack(side=LEFT, padx=(0, 10))
        ttk.Button(
            actions,
            text="Reconcile Unresolved",
            command=self.reconcile_engineering_relations,
            bootstyle="secondary-outline",
        ).pack(side=LEFT, padx=(0, 8))
        ttk.Button(
            actions,
            text="Refresh",
            command=self.refresh_engineering_relations,
            bootstyle="primary",
        ).pack(side=LEFT)

        summary = ttk.Frame(parent)
        summary.pack(fill=X, pady=(0, 10))
        self.relation_summary_total_var = tk.StringVar(value="Relations 0")
        self.relation_summary_confirmed_var = tk.StringVar(value="Confirmed 0")
        self.relation_summary_unresolved_var = tk.StringVar(value="Unresolved 0")
        self.relation_summary_evidence_var = tk.StringVar(value="Evidence 0")
        for idx, var in enumerate((
            self.relation_summary_total_var,
            self.relation_summary_confirmed_var,
            self.relation_summary_unresolved_var,
            self.relation_summary_evidence_var,
        )):
            summary.columnconfigure(idx, weight=1, uniform="relation-summary")
            card = ttk.LabelFrame(summary, text="", padding=(10, 8), bootstyle="secondary")
            card.grid(row=0, column=idx, sticky="ew", padx=(0 if idx == 0 else 5, 0))
            ttk.Label(card, textvariable=var, font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w")

        filters = ttk.Frame(parent)
        filters.pack(fill=X, pady=(0, 8))
        self.relation_view_filter_var = tk.StringVar(value="SPEC REFERENCES")
        self.relation_type_filter_var = tk.StringVar(value="ALL")
        self.relation_semantic_filter_var = tk.StringVar(value="ALL")
        self.relation_status_filter_var = tk.StringVar(value="ALL")
        self.relation_search_var = tk.StringVar(value="")
        self.relation_registry_status_var = tk.StringVar(value="Ready")

        filter_row1 = ttk.Frame(filters)
        filter_row1.pack(fill=X)
        ttk.Label(filter_row1, text="View").pack(side=LEFT, padx=(0, 5))
        view_combo = ttk.Combobox(
            filter_row1,
            textvariable=self.relation_view_filter_var,
            values=("SPEC REFERENCES", "REFERENCE AUDIT", "ENGINEERING FACTS", "ALL"),
            state="readonly",
            width=19,
        )
        view_combo.pack(side=LEFT, padx=(0, 10))
        view_combo.bind("<<ComboboxSelected>>", self._on_relation_view_filter_changed)
        ttk.Label(filter_row1, text="Relation").pack(side=LEFT, padx=(0, 5))
        self.relation_type_combo = ttk.Combobox(
            filter_row1,
            textvariable=self.relation_type_filter_var,
            values=("ALL",),
            state="readonly",
            width=20,
        )
        self.relation_type_combo.pack(side=LEFT, padx=(0, 10))
        self.relation_type_combo.bind("<<ComboboxSelected>>", lambda _e: self._apply_relation_filters())
        ttk.Label(filter_row1, text="Meaning").pack(side=LEFT, padx=(0, 5))
        self.relation_semantic_combo = ttk.Combobox(
            filter_row1,
            textvariable=self.relation_semantic_filter_var,
            values=("ALL",),
            state="readonly",
            width=22,
        )
        self.relation_semantic_combo.pack(side=LEFT, padx=(0, 10))
        self.relation_semantic_combo.bind("<<ComboboxSelected>>", lambda _e: self._apply_relation_filters())
        ttk.Label(filter_row1, text="Status").pack(side=LEFT, padx=(0, 5))
        status_combo = ttk.Combobox(
            filter_row1,
            textvariable=self.relation_status_filter_var,
            values=("ALL", "CONFIRMED", "UNRESOLVED", "AMBIGUOUS"),
            state="readonly",
            width=14,
        )
        status_combo.pack(side=LEFT)
        status_combo.bind("<<ComboboxSelected>>", lambda _e: self._apply_relation_filters())

        filter_row2 = ttk.Frame(filters)
        filter_row2.pack(fill=X, pady=(6, 0))
        ttk.Label(filter_row2, text="Search").pack(side=LEFT, padx=(0, 5))
        relation_search = ttk.Entry(filter_row2, textvariable=self.relation_search_var, width=44)
        relation_search.pack(side=LEFT, fill=X, expand=True, padx=(0, 10))
        relation_search.bind("<KeyRelease>", lambda _e: self._apply_relation_filters())
        ttk.Label(filter_row2, textvariable=self.relation_registry_status_var, bootstyle="secondary").pack(side=RIGHT)

        self.relation_view_notebook = ttk.Notebook(parent)
        self.relation_view_notebook.pack(fill=BOTH, expand=True)
        relation_tab = ttk.Frame(self.relation_view_notebook, padding=(0, 6, 0, 0))
        graph_tab = ttk.Frame(self.relation_view_notebook, padding=(0, 6, 0, 0))
        manual_tab = ttk.Frame(self.relation_view_notebook, padding=(0, 6, 0, 0))
        self.relation_view_notebook.add(relation_tab, text="Relations & Evidence")
        self.relation_view_notebook.add(graph_tab, text="Relationship Graph")
        self.relation_view_notebook.add(manual_tab, text="Manual Relation Editor")

        split = ttk.Panedwindow(relation_tab, orient="horizontal")
        split.pack(fill=BOTH, expand=True)
        registry_panel = ttk.LabelFrame(split, text="Authoritative Relation Facts", padding=8, bootstyle="secondary")
        evidence_panel = ttk.LabelFrame(split, text="Relation Evidence Inspector", padding=8, bootstyle="secondary")
        split.add(registry_panel, weight=5)
        split.add(evidence_panel, weight=3)

        registry_body = ttk.Frame(registry_panel)
        registry_body.pack(fill=BOTH, expand=True)
        self.relation_registry_tree = ttk.Treeview(
            registry_body,
            columns=("source", "version", "relation", "meaning", "target", "status", "confidence", "evidence"),
            show="headings",
            selectmode="browse",
            height=26,
        )
        for column, title, width, anchor in (
            ("source", "Source Specification", 235, "w"),
            ("version", "Version", 78, "center"),
            ("relation", "Relation", 110, "w"),
            ("meaning", "Meaning", 150, "w"),
            ("target", "Target", 250, "w"),
            ("status", "Status", 92, "center"),
            ("confidence", "Confidence", 82, "center"),
            ("evidence", "Evidence / Chunks", 105, "center"),
        ):
            self.relation_registry_tree.heading(column, text=title)
            self.relation_registry_tree.column(
                column, width=width, minwidth=55, anchor=anchor,
                stretch=(column in {"source", "target"}),
            )
        registry_vscroll = ttk.Scrollbar(registry_body, orient=VERTICAL, command=self.relation_registry_tree.yview)
        registry_hscroll = ttk.Scrollbar(registry_body, orient="horizontal", command=self.relation_registry_tree.xview)
        self.relation_registry_tree.configure(
            yscrollcommand=registry_vscroll.set,
            xscrollcommand=registry_hscroll.set,
        )
        self.relation_registry_tree.grid(row=0, column=0, sticky="nsew")
        registry_vscroll.grid(row=0, column=1, sticky="ns")
        registry_hscroll.grid(row=1, column=0, sticky="ew")
        registry_body.rowconfigure(0, weight=1)
        registry_body.columnconfigure(0, weight=1)
        self.relation_registry_tree.bind("<<TreeviewSelect>>", self._on_relation_registry_selected)

        self.relation_evidence_header_var = tk.StringVar(value="Select a relation to inspect source evidence.")
        self.relation_evidence_location_var = tk.StringVar(value="")
        ttk.Label(
            evidence_panel, textvariable=self.relation_evidence_header_var,
            font=("Microsoft YaHei UI", 10, "bold"), wraplength=520, justify=LEFT,
        ).pack(fill=X, pady=(0, 5))
        ttk.Label(
            evidence_panel, textvariable=self.relation_evidence_location_var,
            bootstyle="secondary", wraplength=520, justify=LEFT, style="Small.TLabel",
        ).pack(fill=X, pady=(0, 8))

        evidence_list = ttk.Frame(evidence_panel)
        evidence_list.pack(fill=BOTH, expand=True)
        self.relation_evidence_tree = ttk.Treeview(
            evidence_list,
            columns=("location", "document", "mapping"),
            show="tree headings",
            selectmode="browse",
            height=10,
        )
        self.relation_evidence_tree.heading("#0", text="Chunk / Evidence")
        self.relation_evidence_tree.heading("location", text="Location")
        self.relation_evidence_tree.heading("document", text="Document")
        self.relation_evidence_tree.heading("mapping", text="Mapping")
        self.relation_evidence_tree.column("#0", width=175, minwidth=110, anchor="w", stretch=True)
        self.relation_evidence_tree.column("location", width=180, minwidth=90, anchor="w", stretch=True)
        self.relation_evidence_tree.column("document", width=205, minwidth=100, anchor="w", stretch=True)
        self.relation_evidence_tree.column("mapping", width=95, minwidth=75, anchor="center", stretch=False)
        evidence_vscroll = ttk.Scrollbar(evidence_list, orient=VERTICAL, command=self.relation_evidence_tree.yview)
        evidence_hscroll = ttk.Scrollbar(evidence_list, orient="horizontal", command=self.relation_evidence_tree.xview)
        self.relation_evidence_tree.configure(
            yscrollcommand=evidence_vscroll.set,
            xscrollcommand=evidence_hscroll.set,
        )
        self.relation_evidence_tree.grid(row=0, column=0, sticky="nsew")
        evidence_vscroll.grid(row=0, column=1, sticky="ns")
        evidence_hscroll.grid(row=1, column=0, sticky="ew")
        evidence_list.rowconfigure(0, weight=1)
        evidence_list.columnconfigure(0, weight=1)
        self.relation_evidence_tree.bind("<<TreeviewSelect>>", self._on_relation_evidence_selected)

        text_notebook = ttk.Notebook(evidence_panel)
        text_notebook.pack(fill=BOTH, expand=True, pady=(8, 0))
        evidence_text_tab = ttk.Frame(text_notebook, padding=4)
        chunk_text_tab = ttk.Frame(text_notebook, padding=4)
        text_notebook.add(evidence_text_tab, text="Evidence Text")
        text_notebook.add(chunk_text_tab, text="Canonical Chunk")

        self.relation_evidence_text = tk.Text(
            evidence_text_tab, wrap=WORD, height=9, font=("Consolas", 9), state=DISABLED
        )
        evidence_text_scroll = ttk.Scrollbar(
            evidence_text_tab, orient=VERTICAL, command=self.relation_evidence_text.yview
        )
        self.relation_evidence_text.configure(yscrollcommand=evidence_text_scroll.set)
        self.relation_evidence_text.pack(side=LEFT, fill=BOTH, expand=True)
        evidence_text_scroll.pack(side=RIGHT, fill=Y)

        self.relation_chunk_text = tk.Text(
            chunk_text_tab, wrap=WORD, height=9, font=("Consolas", 9), state=DISABLED
        )
        chunk_text_scroll = ttk.Scrollbar(
            chunk_text_tab, orient=VERTICAL, command=self.relation_chunk_text.yview
        )
        self.relation_chunk_text.configure(yscrollcommand=chunk_text_scroll.set)
        self.relation_chunk_text.pack(side=LEFT, fill=BOTH, expand=True)
        chunk_text_scroll.pack(side=RIGHT, fill=Y)

        manual_split = ttk.Panedwindow(manual_tab, orient="horizontal")
        manual_split.pack(fill=BOTH, expand=True)
        manual_form_panel = ttk.LabelFrame(
            manual_split,
            text="Create / Edit Manual Specification Relation",
            padding=10,
            bootstyle="secondary",
        )
        manual_list_panel = ttk.LabelFrame(
            manual_split,
            text="Manual Relation Audit Registry",
            padding=8,
            bootstyle="secondary",
        )
        manual_split.add(manual_form_panel, weight=4)
        manual_split.add(manual_list_panel, weight=5)

        self.manual_relation_source_var = tk.StringVar(value="")
        self.manual_relation_target_mode_var = tk.StringVar(value="EXISTING")
        self.manual_relation_target_var = tk.StringVar(value="")
        self.manual_relation_external_code_var = tk.StringVar(value="")
        self.manual_relation_title_hint_var = tk.StringVar(value="")
        self.manual_relation_semantic_var = tk.StringVar(value="REFERENCES_SPEC")
        self.manual_relation_status_var = tk.StringVar(value="AUTO")
        self.manual_relation_locator_type_var = tk.StringVar(value="NONE")
        self.manual_relation_locator_value_var = tk.StringVar(value="")
        self.manual_relation_evidence_kind_var = tk.StringVar(value="SOURCE_QUOTE")
        self.manual_relation_note_var = tk.StringVar(value="")
        self.manual_relation_editor_status_var = tk.StringVar(
            value="Manual relations are stored separately from extractor output and remain auditable."
        )

        form = ttk.Frame(manual_form_panel)
        form.pack(fill=BOTH, expand=True)
        form.columnconfigure(1, weight=1)
        form.columnconfigure(3, weight=1)

        ttk.Label(form, text="Source specification").grid(row=0, column=0, sticky="w", padx=(0, 6), pady=4)
        self.manual_relation_source_combo = ttk.Combobox(
            form,
            textvariable=self.manual_relation_source_var,
            values=(),
            state="readonly",
            width=46,
        )
        self.manual_relation_source_combo.grid(row=0, column=1, columnspan=3, sticky="ew", pady=4)

        ttk.Label(form, text="Target mode").grid(row=1, column=0, sticky="w", padx=(0, 6), pady=4)
        self.manual_relation_target_mode_combo = ttk.Combobox(
            form,
            textvariable=self.manual_relation_target_mode_var,
            values=("EXISTING", "EXTERNAL SPEC NO."),
            state="readonly",
            width=20,
        )
        self.manual_relation_target_mode_combo.grid(row=1, column=1, sticky="ew", pady=4, padx=(0, 10))
        self.manual_relation_target_mode_combo.bind(
            "<<ComboboxSelected>>",
            lambda _e: self._on_manual_relation_target_mode_changed(),
        )
        ttk.Label(form, text="Status").grid(row=1, column=2, sticky="w", padx=(0, 6), pady=4)
        self.manual_relation_status_combo = ttk.Combobox(
            form,
            textvariable=self.manual_relation_status_var,
            values=("AUTO", "CONFIRMED", "UNRESOLVED", "AMBIGUOUS"),
            state="readonly",
            width=18,
        )
        self.manual_relation_status_combo.grid(row=1, column=3, sticky="ew", pady=4)

        ttk.Label(form, text="Existing target").grid(row=2, column=0, sticky="w", padx=(0, 6), pady=4)
        self.manual_relation_target_combo = ttk.Combobox(
            form,
            textvariable=self.manual_relation_target_var,
            values=(),
            state="readonly",
            width=46,
        )
        self.manual_relation_target_combo.grid(row=2, column=1, columnspan=3, sticky="ew", pady=4)

        ttk.Label(form, text="External Spec No.").grid(row=3, column=0, sticky="w", padx=(0, 6), pady=4)
        self.manual_relation_external_code_entry = ttk.Entry(
            form,
            textvariable=self.manual_relation_external_code_var,
        )
        self.manual_relation_external_code_entry.grid(row=3, column=1, sticky="ew", pady=4, padx=(0, 10))
        ttk.Label(form, text="Title hint").grid(row=3, column=2, sticky="w", padx=(0, 6), pady=4)
        self.manual_relation_title_hint_entry = ttk.Entry(
            form,
            textvariable=self.manual_relation_title_hint_var,
        )
        self.manual_relation_title_hint_entry.grid(row=3, column=3, sticky="ew", pady=4)

        ttk.Label(form, text="Meaning").grid(row=4, column=0, sticky="w", padx=(0, 6), pady=4)
        self.manual_relation_semantic_combo = ttk.Combobox(
            form,
            textvariable=self.manual_relation_semantic_var,
            values=(
                "REFERENCES_SPEC",
                "COMPLIES_WITH",
                "RELEVANT_SPEC",
                "REFERENCES_SECTION",
                "DETAILS_DEFINED_IN",
                "CORRESPONDS_TO_SECTION",
            ),
            state="readonly",
            width=28,
        )
        self.manual_relation_semantic_combo.grid(row=4, column=1, sticky="ew", pady=4, padx=(0, 10))
        ttk.Label(form, text="Locator type").grid(row=4, column=2, sticky="w", padx=(0, 6), pady=4)
        self.manual_relation_locator_type_combo = ttk.Combobox(
            form,
            textvariable=self.manual_relation_locator_type_var,
            values=("NONE", "SECTION", "USE_CASE", "REQUIREMENT", "CHAPTER", "PAGE", "CUSTOM"),
            state="readonly",
            width=18,
        )
        self.manual_relation_locator_type_combo.grid(row=4, column=3, sticky="ew", pady=4)

        ttk.Label(form, text="Target locator").grid(row=5, column=0, sticky="w", padx=(0, 6), pady=4)
        self.manual_relation_locator_value_entry = ttk.Entry(
            form,
            textvariable=self.manual_relation_locator_value_var,
        )
        self.manual_relation_locator_value_entry.grid(row=5, column=1, columnspan=3, sticky="ew", pady=4)

        ttk.Label(form, text="Evidence type").grid(row=6, column=0, sticky="w", padx=(0, 6), pady=4)
        self.manual_relation_evidence_kind_combo = ttk.Combobox(
            form,
            textvariable=self.manual_relation_evidence_kind_var,
            values=("SOURCE_QUOTE", "REFERENCE_TABLE", "ENGINEERING_DECISION", "OTHER"),
            state="readonly",
            width=24,
        )
        self.manual_relation_evidence_kind_combo.grid(row=6, column=1, sticky="ew", pady=4, padx=(0, 10))
        ttk.Label(form, text="Note").grid(row=6, column=2, sticky="w", padx=(0, 6), pady=4)
        ttk.Entry(form, textvariable=self.manual_relation_note_var).grid(row=6, column=3, sticky="ew", pady=4)

        ttk.Label(form, text="Evidence / engineering decision").grid(
            row=7, column=0, columnspan=4, sticky="w", pady=(8, 4)
        )
        evidence_host = ttk.Frame(form)
        evidence_host.grid(row=8, column=0, columnspan=4, sticky="nsew")
        form.rowconfigure(8, weight=1)
        self.manual_relation_evidence_text = tk.Text(
            evidence_host,
            wrap=WORD,
            height=9,
            font=("Consolas", 9),
        )
        manual_evidence_scroll = ttk.Scrollbar(
            evidence_host,
            orient=VERTICAL,
            command=self.manual_relation_evidence_text.yview,
        )
        self.manual_relation_evidence_text.configure(yscrollcommand=manual_evidence_scroll.set)
        self.manual_relation_evidence_text.pack(side=LEFT, fill=BOTH, expand=True)
        manual_evidence_scroll.pack(side=RIGHT, fill=Y)

        ttk.Label(
            form,
            text=(
                "Rule: use SOURCE_QUOTE / REFERENCE_TABLE when the source document really contains the relation; "
                "use ENGINEERING_DECISION only for a deliberate manual engineering link. Manual links never overwrite extractor facts."
            ),
            bootstyle="secondary",
            style="Small.TLabel",
            wraplength=760,
            justify=LEFT,
        ).grid(row=9, column=0, columnspan=4, sticky="ew", pady=(8, 4))

        manual_actions = ttk.Frame(form)
        manual_actions.grid(row=10, column=0, columnspan=4, sticky="ew", pady=(6, 0))
        ttk.Button(
            manual_actions,
            text="Add Relation",
            command=lambda: self._save_manual_relation_from_editor(create=True),
            bootstyle="primary",
        ).pack(side=LEFT, padx=(0, 6))
        ttk.Button(
            manual_actions,
            text="Update Selected",
            command=lambda: self._save_manual_relation_from_editor(create=False),
            bootstyle="secondary-outline",
        ).pack(side=LEFT, padx=(0, 6))
        ttk.Button(
            manual_actions,
            text="Delete Selected",
            command=self._delete_manual_relation_from_editor,
            bootstyle="danger-outline",
        ).pack(side=LEFT, padx=(0, 6))
        ttk.Button(
            manual_actions,
            text="Clear",
            command=self._clear_manual_relation_editor,
            bootstyle="secondary-outline",
        ).pack(side=LEFT)
        ttk.Label(
            manual_actions,
            textvariable=self.manual_relation_editor_status_var,
            bootstyle="secondary",
            style="Small.TLabel",
        ).pack(side=RIGHT, padx=(10, 0))

        manual_list_host = ttk.Frame(manual_list_panel)
        manual_list_host.pack(fill=BOTH, expand=True)
        manual_list_host.rowconfigure(0, weight=1)
        manual_list_host.columnconfigure(0, weight=1)
        self.manual_relation_tree = ttk.Treeview(
            manual_list_host,
            columns=("source", "meaning", "target", "status", "evidence", "updated"),
            show="headings",
            selectmode="browse",
            height=24,
        )
        for column, title, width, anchor in (
            ("source", "Source", 180, "w"),
            ("meaning", "Meaning", 145, "w"),
            ("target", "Target", 190, "w"),
            ("status", "Status", 90, "center"),
            ("evidence", "Evidence", 120, "center"),
            ("updated", "Updated", 135, "center"),
        ):
            self.manual_relation_tree.heading(column, text=title)
            self.manual_relation_tree.column(
                column,
                width=width,
                minwidth=70,
                anchor=anchor,
                stretch=(column in {"source", "target"}),
            )
        manual_tree_vscroll = ttk.Scrollbar(
            manual_list_host,
            orient=VERTICAL,
            command=self.manual_relation_tree.yview,
        )
        manual_tree_hscroll = ttk.Scrollbar(
            manual_list_host,
            orient="horizontal",
            command=self.manual_relation_tree.xview,
        )
        self.manual_relation_tree.configure(
            yscrollcommand=manual_tree_vscroll.set,
            xscrollcommand=manual_tree_hscroll.set,
        )
        self.manual_relation_tree.grid(row=0, column=0, sticky="nsew")
        manual_tree_vscroll.grid(row=0, column=1, sticky="ns")
        manual_tree_hscroll.grid(row=1, column=0, sticky="ew")
        self.manual_relation_tree.bind("<<TreeviewSelect>>", self._on_manual_relation_tree_selected)

        graph_toolbar = ttk.Frame(graph_tab)
        graph_toolbar.pack(fill=X, pady=(0, 8))
        self.relation_graph_root_var = tk.StringVar(value="Auto (recommended)")
        self.relation_graph_depth_var = tk.StringVar(value="1")
        self.relation_graph_direction_var = tk.StringVar(value="BOTH")
        self.relation_graph_include_unresolved_var = tk.BooleanVar(value=True)
        self.relation_graph_include_title_candidates_var = tk.BooleanVar(value=False)
        self.relation_graph_status_var = tk.StringVar(value="Load relations to render the graph.")
        self.relation_graph_zoom_var = tk.StringVar(value="100%")

        ttk.Label(graph_toolbar, text="Root").pack(side=LEFT, padx=(0, 5))
        self.relation_graph_root_combo = ttk.Combobox(
            graph_toolbar,
            textvariable=self.relation_graph_root_var,
            values=("Auto (recommended)",),
            state="readonly",
            width=38,
        )
        self.relation_graph_root_combo.pack(side=LEFT, padx=(0, 10))
        self.relation_graph_root_combo.bind("<<ComboboxSelected>>", lambda _e: self._render_relation_graph())
        ttk.Label(graph_toolbar, text="Depth").pack(side=LEFT, padx=(0, 5))
        depth_combo = ttk.Combobox(
            graph_toolbar,
            textvariable=self.relation_graph_depth_var,
            values=("1", "2", "3"),
            state="readonly",
            width=5,
        )
        depth_combo.pack(side=LEFT, padx=(0, 10))
        depth_combo.bind("<<ComboboxSelected>>", lambda _e: self._render_relation_graph())
        ttk.Label(graph_toolbar, text="Direction").pack(side=LEFT, padx=(0, 5))
        direction_combo = ttk.Combobox(
            graph_toolbar,
            textvariable=self.relation_graph_direction_var,
            values=("BOTH", "OUTGOING", "INCOMING"),
            state="readonly",
            width=11,
        )
        direction_combo.pack(side=LEFT, padx=(0, 10))
        direction_combo.bind("<<ComboboxSelected>>", lambda _e: self._render_relation_graph())
        ttk.Checkbutton(
            graph_toolbar,
            text="Include unresolved",
            variable=self.relation_graph_include_unresolved_var,
            command=self._render_relation_graph,
            bootstyle="round-toggle",
        ).pack(side=LEFT, padx=(0, 10))
        ttk.Checkbutton(
            graph_toolbar,
            text="Audit candidates",
            variable=self.relation_graph_include_title_candidates_var,
            command=self._render_relation_graph,
            bootstyle="round-toggle",
        ).pack(side=LEFT, padx=(0, 10))
        ttk.Separator(graph_toolbar, orient="vertical").pack(side=LEFT, fill=Y, padx=(2, 8))
        ttk.Label(graph_toolbar, text="Zoom").pack(side=LEFT, padx=(0, 5))
        ttk.Button(
            graph_toolbar, text="−", width=3, command=lambda: self._change_relation_graph_zoom(-0.10),
            bootstyle="secondary-outline",
        ).pack(side=LEFT)
        ttk.Label(
            graph_toolbar, textvariable=self.relation_graph_zoom_var, width=6, anchor="center"
        ).pack(side=LEFT, padx=4)
        ttk.Button(
            graph_toolbar, text="+", width=3, command=lambda: self._change_relation_graph_zoom(0.10),
            bootstyle="secondary-outline",
        ).pack(side=LEFT)
        ttk.Button(
            graph_toolbar, text="100%", command=self._reset_relation_graph_zoom,
            bootstyle="secondary-outline",
        ).pack(side=LEFT, padx=(6, 4))
        ttk.Button(
            graph_toolbar, text="Fit", command=self._fit_relation_graph,
            bootstyle="secondary-outline",
        ).pack(side=LEFT)
        ttk.Button(
            graph_toolbar, text="Reset Layout", command=self._reset_relation_graph_layout,
            bootstyle="secondary-outline",
        ).pack(side=LEFT, padx=(4, 0))

        graph_host = ttk.Frame(graph_tab)
        graph_host.pack(fill=BOTH, expand=True)
        graph_host.rowconfigure(0, weight=1)
        graph_host.columnconfigure(0, weight=1)
        graph_background = ttk.Style().lookup("TFrame", "background") or "white"
        self.relation_graph_canvas = tk.Canvas(
            graph_host,
            highlightthickness=1,
            highlightbackground="#9aa0a6",
            background=graph_background,
            height=620,
        )
        graph_vscroll = ttk.Scrollbar(graph_host, orient=VERTICAL, command=self.relation_graph_canvas.yview)
        graph_hscroll = ttk.Scrollbar(graph_host, orient="horizontal", command=self.relation_graph_canvas.xview)
        self.relation_graph_canvas.configure(
            yscrollcommand=graph_vscroll.set,
            xscrollcommand=graph_hscroll.set,
        )
        self.relation_graph_canvas.grid(row=0, column=0, sticky="nsew")
        graph_vscroll.grid(row=0, column=1, sticky="ns")
        graph_hscroll.grid(row=1, column=0, sticky="ew")
        # Graph navigation: Ctrl+mouse-wheel zooms, normal wheel scrolls,
        # Shift+wheel scrolls horizontally, and middle-button drag pans.
        self.relation_graph_canvas.bind("<Control-MouseWheel>", self._on_relation_graph_zoom_wheel)
        self.relation_graph_canvas.bind("<MouseWheel>", self._on_relation_graph_mousewheel)
        self.relation_graph_canvas.bind("<Shift-MouseWheel>", self._on_relation_graph_shift_mousewheel)
        self.relation_graph_canvas.bind("<ButtonPress-2>", self._on_relation_graph_pan_start)
        self.relation_graph_canvas.bind("<B2-Motion>", self._on_relation_graph_pan_drag)
        self.relation_graph_canvas.bind("<Button-2>", lambda _e: self.relation_graph_canvas.configure(cursor=""))
        ttk.Label(
            graph_tab,
            textvariable=self.relation_graph_status_var,
            bootstyle="secondary",
            style="Small.TLabel",
        ).pack(fill=X, pady=(6, 0))

    def _refresh_relation_graph_roots(self) -> None:
        if not hasattr(self, "relation_graph_root_combo"):
            return
        current = self.relation_graph_root_var.get().strip() or "Auto (recommended)"
        # The primary dependency graph is intentionally single-root. Rendering
        # the whole repository by default produced an unreadable spider web and
        # mixed supporting/title-only documents with real Toyota Spec Nos.
        options = [
            ("Auto (recommended)", EngineeringRelationGraphService.AUTO_ROOT_ID),
        ]
        for node_id, label in EngineeringRelationGraphService.root_options(self._visible_relation_rows):
            options.append((label, node_id))
        self._relation_graph_root_by_label = dict(options)
        labels = tuple(label for label, _node_id in options)
        self.relation_graph_root_combo.configure(values=labels)
        if current not in self._relation_graph_root_by_label:
            self.relation_graph_root_var.set("Auto (recommended)")

    def _preferred_relation_graph_root_id(self) -> str | None:
        selected_id = getattr(self, "_selected_specification_id", None)
        if selected_id is not None:
            for row in self._visible_relation_rows:
                if not EngineeringRelationGraphService.is_primary_reference_row(row):
                    continue
                if int(row.get("source_specification_id") or -1) == int(selected_id):
                    key = str(row.get("source_specification_key") or row.get("source_specification_id"))
                    return f"SPEC::{key}"
                if int(row.get("target_specification_id") or -1) == int(selected_id):
                    key = str(row.get("target_specification_key") or row.get("target_specification_id"))
                    return f"SPEC::{key}"

        recommended = EngineeringRelationGraphService.recommended_root(self._visible_relation_rows)
        if recommended:
            return recommended

        # Advanced Engineering Facts views may contain no REFERENCES rows.
        # Still keep a bounded single-root graph by choosing the most frequent
        # source specification rather than falling back to an all-repository map.
        counts: dict[str, int] = {}
        for row in self._visible_relation_rows:
            key = str(row.get("source_specification_key") or row.get("source_specification_id") or "")
            if not key:
                continue
            node_id = f"SPEC::{key}"
            counts[node_id] = counts.get(node_id, 0) + 1
        if not counts:
            return None
        return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]

    def _relation_graph_view_center_base(self) -> tuple[float, float] | None:
        if not hasattr(self, "relation_graph_canvas"):
            return None
        canvas = self.relation_graph_canvas
        canvas.update_idletasks()
        width = max(canvas.winfo_width(), 1)
        height = max(canvas.winfo_height(), 1)
        zoom = max(float(self._relation_graph_zoom or 1.0), 0.01)
        return (
            canvas.canvasx(width / 2) / zoom,
            canvas.canvasy(height / 2) / zoom,
        )

    def _center_relation_graph_on_base_point(self, base_point: tuple[float, float] | None) -> None:
        if base_point is None or not hasattr(self, "relation_graph_canvas"):
            return
        canvas = self.relation_graph_canvas
        canvas.update_idletasks()
        bbox = canvas.bbox("all")
        if not bbox:
            return
        x1, y1, x2, y2 = bbox
        pad = 60 * self._relation_graph_zoom
        sx1, sy1, sx2, sy2 = x1 - pad, y1 - pad, x2 + pad, y2 + pad
        canvas.configure(scrollregion=(sx1, sy1, sx2, sy2))
        width = max(canvas.winfo_width(), 1)
        height = max(canvas.winfo_height(), 1)
        target_x = base_point[0] * self._relation_graph_zoom
        target_y = base_point[1] * self._relation_graph_zoom
        total_w = max(sx2 - sx1, width)
        total_h = max(sy2 - sy1, height)
        x_fraction = (target_x - width / 2 - sx1) / max(total_w - width, 1)
        y_fraction = (target_y - height / 2 - sy1) / max(total_h - height, 1)
        canvas.xview_moveto(max(0.0, min(1.0, x_fraction)))
        canvas.yview_moveto(max(0.0, min(1.0, y_fraction)))

    def _change_relation_graph_zoom(self, delta: float) -> None:
        center = self._relation_graph_view_center_base()
        new_zoom = max(
            self._relation_graph_zoom_min,
            min(self._relation_graph_zoom_max, self._relation_graph_zoom + float(delta)),
        )
        if abs(new_zoom - self._relation_graph_zoom) < 1e-9:
            return
        self._relation_graph_zoom = new_zoom
        self.relation_graph_zoom_var.set(f"{round(new_zoom * 100):d}%")
        self._render_relation_graph(preserve_base_center=center)

    def _reset_relation_graph_zoom(self) -> None:
        center = self._relation_graph_view_center_base()
        self._relation_graph_zoom = 1.0
        self.relation_graph_zoom_var.set("100%")
        self._render_relation_graph(preserve_base_center=center)

    def _relation_graph_symmetric_bbox(
        self,
        bbox: tuple[int, int, int, int] | tuple[float, float, float, float],
    ) -> tuple[float, float, float, float]:
        """Expand a graph bbox symmetrically around the logical root.

        Status-radial graphs can legitimately have most neighbors in one sector
        (for example all targets are UNRESOLVED on the right).  A raw bbox would
        visually pin the root to the left after Fit.  Symmetric extents keep the
        current specification at the viewport center while preserving all nodes.
        """
        x1, y1, x2, y2 = [float(v) for v in bbox]
        root = self._relation_graph_last_root_base_point
        if root is None:
            return x1, y1, x2, y2
        zoom = max(float(self._relation_graph_zoom or 1.0), 0.01)
        root_x = root[0] * zoom
        root_y = root[1] * zoom
        half_w = max(root_x - x1, x2 - root_x, 1.0)
        half_h = max(root_y - y1, y2 - root_y, 1.0)
        return (
            root_x - half_w,
            root_y - half_h,
            root_x + half_w,
            root_y + half_h,
        )

    def _on_relation_graph_node_drag_start(self, event: Any, node_id: str) -> str:
        if not hasattr(self, "relation_graph_canvas"):
            return "break"
        canvas = self.relation_graph_canvas
        x = float(canvas.canvasx(event.x))
        y = float(canvas.canvasy(event.y))
        base_x, base_y = self._relation_graph_last_positions.get(node_id, (x / max(self._relation_graph_zoom, 0.01), y / max(self._relation_graph_zoom, 0.01)))
        self._relation_graph_drag_state = (node_id, x, y, base_x, base_y)
        canvas.configure(cursor="fleur")
        self._hide_relation_graph_tooltip()
        return "break"

    def _on_relation_graph_node_drag(self, event: Any, node_id: str) -> str:
        if not hasattr(self, "relation_graph_canvas") or not self._relation_graph_drag_state:
            return "break"
        state_node, start_x, start_y, base_x, base_y = self._relation_graph_drag_state
        if state_node != node_id:
            return "break"
        canvas = self.relation_graph_canvas
        current_x = float(canvas.canvasx(event.x))
        current_y = float(canvas.canvasy(event.y))
        zoom = max(float(self._relation_graph_zoom or 1.0), 0.01)
        self._relation_graph_manual_positions[node_id] = (base_x + (current_x - start_x) / zoom, base_y + (current_y - start_y) / zoom)
        tag = self._relation_graph_node_tag_by_id.get(node_id)
        if tag:
            # Visual node follows the pointer immediately; edges are rebuilt on release.
            rendered_x, rendered_y = self._relation_graph_last_positions.get(node_id, (base_x, base_y))
            target_x, target_y = self._relation_graph_manual_positions[node_id]
            canvas.move(tag, (target_x - rendered_x) * zoom, (target_y - rendered_y) * zoom)
            self._relation_graph_last_positions[node_id] = (target_x, target_y)
        return "break"

    def _on_relation_graph_node_drag_end(self, _event: Any, node_id: str) -> str:
        if not hasattr(self, "relation_graph_canvas"):
            return "break"
        if self._relation_graph_drag_state and self._relation_graph_drag_state[0] == node_id:
            self._relation_graph_drag_state = None
            self.relation_graph_canvas.configure(cursor="")
            self._render_relation_graph(preserve_base_center=self._relation_graph_view_center_base())
        return "break"

    def _reset_relation_graph_layout(self) -> None:
        """Clear manual drag positions and rebuild the deterministic graph layout."""
        self._relation_graph_manual_positions.clear()
        self._render_relation_graph()
        try:
            self.root.after_idle(self._fit_relation_graph)
        except Exception:
            pass

    def _fit_relation_graph(self) -> None:
        if not hasattr(self, "relation_graph_canvas"):
            return
        canvas = self.relation_graph_canvas
        # Measure once at 100%, then choose a scale that fits both dimensions.
        self._relation_graph_zoom = 1.0
        self.relation_graph_zoom_var.set("100%")
        self._render_relation_graph()
        canvas.update_idletasks()
        bbox = canvas.bbox("all")
        if not bbox:
            return
        x1, y1, x2, y2 = self._relation_graph_symmetric_bbox(bbox)
        graph_w = max(x2 - x1, 1)
        graph_h = max(y2 - y1, 1)
        viewport_w = max(canvas.winfo_width() - 36, 160)
        viewport_h = max(canvas.winfo_height() - 36, 120)
        fitted = min(viewport_w / graph_w, viewport_h / graph_h)
        fitted = max(self._relation_graph_zoom_min, min(self._relation_graph_zoom_max, fitted))
        self._relation_graph_zoom = fitted
        self.relation_graph_zoom_var.set(f"{round(fitted * 100):d}%")
        self._render_relation_graph()
        canvas.update_idletasks()
        if self._relation_graph_last_root_base_point is not None:
            self._center_relation_graph_on_base_point(
                self._relation_graph_last_root_base_point
            )

    def _on_relation_graph_zoom_wheel(self, event: Any) -> str:
        delta = 0.10 if getattr(event, "delta", 0) > 0 else -0.10
        self._change_relation_graph_zoom(delta)
        return "break"

    def _on_relation_graph_mousewheel(self, event: Any) -> str:
        if not hasattr(self, "relation_graph_canvas"):
            return "break"
        steps = -1 if getattr(event, "delta", 0) > 0 else 1
        self.relation_graph_canvas.yview_scroll(steps * 3, "units")
        return "break"

    def _on_relation_graph_shift_mousewheel(self, event: Any) -> str:
        if not hasattr(self, "relation_graph_canvas"):
            return "break"
        steps = -1 if getattr(event, "delta", 0) > 0 else 1
        self.relation_graph_canvas.xview_scroll(steps * 3, "units")
        return "break"

    def _on_relation_graph_pan_start(self, event: Any) -> str:
        self.relation_graph_canvas.scan_mark(event.x, event.y)
        self.relation_graph_canvas.configure(cursor="fleur")
        return "break"

    def _on_relation_graph_pan_drag(self, event: Any) -> str:
        self.relation_graph_canvas.scan_dragto(event.x, event.y, gain=1)
        return "break"

    def _show_relation_graph_tooltip(self, event: Any, text: str) -> None:
        text = str(text or "").strip()
        if not text:
            return
        if self._relation_graph_tooltip is None or not self._relation_graph_tooltip.winfo_exists():
            tooltip = tk.Toplevel(self.root)
            tooltip.wm_overrideredirect(True)
            tooltip.attributes("-topmost", True)
            label = ttk.Label(
                tooltip,
                text=text,
                justify=LEFT,
                anchor="w",
                padding=(8, 6),
                bootstyle="light",
                style="Small.TLabel",
            )
            label.pack(fill=BOTH, expand=True)
            self._relation_graph_tooltip = tooltip
            self._relation_graph_tooltip_label = label
        elif self._relation_graph_tooltip_label is not None:
            self._relation_graph_tooltip_label.configure(text=text)
        self._move_relation_graph_tooltip(event)

    def _move_relation_graph_tooltip(self, event: Any) -> None:
        tooltip = self._relation_graph_tooltip
        if tooltip is None or not tooltip.winfo_exists():
            return
        x = event.widget.winfo_rootx() + int(getattr(event, "x", 0)) + 16
        y = event.widget.winfo_rooty() + int(getattr(event, "y", 0)) + 16
        tooltip.geometry(f"+{x}+{y}")

    def _hide_relation_graph_tooltip(self, _event: Any | None = None) -> None:
        tooltip = self._relation_graph_tooltip
        if tooltip is not None and tooltip.winfo_exists():
            tooltip.destroy()
        self._relation_graph_tooltip = None
        self._relation_graph_tooltip_label = None

    def _relation_graph_node_tooltip(self, node: RelationGraphNode, *, is_root: bool) -> str:
        parts = [node.label]
        if node.subtitle:
            parts.append(node.subtitle)
        status = str(node.status or "").upper() or "UNKNOWN"
        if is_root:
            parts.append("Role: current graph root")
        parts.append(f"Status: {status}")
        parts.append(f"Kind: {node.kind}")
        parts.append("Double-click: make this node the graph root + fit")
        return "\n".join(parts)

    def _relation_graph_edge_tooltip(
        self,
        edge: RelationGraphEdge,
        node_map: dict[str, RelationGraphNode],
    ) -> str:
        source = node_map.get(edge.source_id)
        target = node_map.get(edge.target_id)
        source_label = source.label if source is not None else edge.source_id
        target_label = target.label if target is not None else edge.target_id
        semantic = edge.semantic_type.replace("_", " ").title()
        confidence = "-" if edge.confidence is None else f"{edge.confidence * 100:.1f}%"
        origin = "MANUAL" if int(edge.relation_id) < 0 else "EXTRACTED"
        return "\n".join((
            f"{source_label} → {target_label}",
            f"Origin: {origin}",
            f"Relation: {edge.relation_type}",
            f"Meaning: {semantic}",
            f"Status: {edge.status}",
            f"Confidence: {confidence}",
            f"Evidence rows: {edge.evidence_count}",
            f"Mapped chunks: {edge.evidence_chunk_count}",
            "Click: open this relation in the registry",
        ))

    def _relation_graph_edge_dash(self, edge: RelationGraphEdge, zoom: float) -> tuple[int, ...] | None:
        if edge.status == "AMBIGUOUS":
            return (max(2, round(2 * zoom)), max(2, round(4 * zoom)))
        if edge.status != "CONFIRMED":
            return (max(2, round(5 * zoom)), max(2, round(3 * zoom)))
        semantic = str(edge.semantic_type or "").upper()
        if semantic == "COMPLIES_WITH":
            return (max(2, round(10 * zoom)), max(2, round(4 * zoom)))
        if semantic == "RELEVANT_SPEC":
            return (max(2, round(3 * zoom)), max(2, round(3 * zoom)))
        if semantic in {"TITLE_ONLY_CANDIDATE", "REFERENCES_SECTION"}:
            return (max(2, round(2 * zoom)), max(2, round(5 * zoom)))
        return None

    def _on_relation_graph_node_double_clicked(self, node_id: str) -> str:
        for label, mapped_id in self._relation_graph_root_by_label.items():
            if mapped_id != node_id:
                continue
            self.relation_graph_root_var.set(label)
            self._render_relation_graph()
            self.root.after_idle(self._fit_relation_graph)
            return "break"
        # Unresolved/external targets cannot become authoritative roots until
        # the referenced specification has been imported/reconciled.
        self.relation_graph_status_var.set(
            "This target is unresolved and cannot be used as a root yet. Import/reconcile the target specification first."
        )
        return "break"

    def _relation_graph_current_root_id(self) -> str | None:
        root_label = self.relation_graph_root_var.get().strip() or "Auto (recommended)"
        root_id = self._relation_graph_root_by_label.get(root_label)
        if root_id == EngineeringRelationGraphService.AUTO_ROOT_ID:
            root_id = self._preferred_relation_graph_root_id()
        return root_id

    def _render_relation_graph(
        self,
        *,
        preserve_base_center: tuple[float, float] | None = None,
    ) -> None:
        if not hasattr(self, "relation_graph_canvas"):
            return
        canvas = self.relation_graph_canvas
        canvas.delete("all")
        root_label = self.relation_graph_root_var.get().strip() or "Auto (recommended)"
        root_id = self._relation_graph_root_by_label.get(root_label)
        if root_id == EngineeringRelationGraphService.AUTO_ROOT_ID:
            root_id = self._preferred_relation_graph_root_id()
        try:
            depth = int(self.relation_graph_depth_var.get() or 1)
        except (TypeError, ValueError):
            depth = 1
        direction = self.relation_graph_direction_var.get().strip().upper() or "BOTH"
        include_unresolved = bool(self.relation_graph_include_unresolved_var.get())
        include_title_candidates = bool(self.relation_graph_include_title_candidates_var.get())
        relation_filter = self.relation_type_filter_var.get().strip().upper() or "ALL"
        view_filter = self.relation_view_filter_var.get().strip().upper() or "SPEC REFERENCES"
        references_only = view_filter in {"SPEC REFERENCES", "REFERENCE AUDIT"} or relation_filter == "REFERENCES"
        graph = EngineeringRelationGraphService.build(
            self._visible_relation_rows,
            root_id=root_id,
            depth=depth,
            direction=direction,
            include_unresolved=include_unresolved,
            include_title_candidates=include_title_candidates,
            references_only=references_only,
        )
        if not graph.nodes:
            canvas.create_text(
                30, 30, anchor="nw",
                text=(
                    "No relationship edges match the current filters."
                    if include_unresolved
                    else "No confirmed edges match. Enable Include unresolved to show missing target specifications."
                ),
                font=("Microsoft YaHei UI", 11, "bold"),
            )
            canvas.configure(scrollregion=(0, 0, 900, 600))
            self.relation_graph_status_var.set("0 nodes · 0 edges")
            return

        positions = self._layout_relation_graph(graph, root_id=root_id, direction=direction)
        for node_id, manual_position in self._relation_graph_manual_positions.items():
            if node_id in positions:
                positions[node_id] = manual_position
        self._relation_graph_last_positions = dict(positions)
        self._relation_graph_last_root_base_point = positions.get(root_id) if root_id else None
        node_map = {node.node_id: node for node in graph.nodes}
        style = ttk.Style()
        edge_color = style.lookup("TLabel", "foreground") or "#4b5563"
        node_fill = style.lookup("TEntry", "fieldbackground") or style.lookup("TFrame", "background") or "#ffffff"
        text_color = style.lookup("TLabel", "foreground") or "#111827"
        confirmed_outline = "#4f6f8f"
        unresolved_outline = "#b7791f"
        entity_outline = "#6b7280"
        zoom = max(self._relation_graph_zoom_min, min(self._relation_graph_zoom_max, self._relation_graph_zoom))
        self._relation_graph_zoom = zoom
        self.relation_graph_zoom_var.set(f"{round(zoom * 100):d}%")
        node_w = 248 * zoom
        node_h = 88 * zoom
        line_width = max(1, round(2 * zoom))
        title_font = ("Microsoft YaHei UI", max(7, round(10 * zoom)), "bold")
        subtitle_font = ("Microsoft YaHei UI", max(6, round(8 * zoom)))
        edge_font = ("Microsoft YaHei UI", max(6, round(8 * zoom)), "bold")

        scaled_positions = {node_id: (x * zoom, y * zoom) for node_id, (x, y) in positions.items()}
        edge_pairs = {(edge.source_id, edge.target_id) for edge in graph.edges}
        self._hide_relation_graph_tooltip()
        self._relation_graph_edge_items.clear()
        for edge in graph.edges:
            if edge.source_id not in scaled_positions or edge.target_id not in scaled_positions:
                continue
            sx, sy = scaled_positions[edge.source_id]
            tx, ty = scaled_positions[edge.target_id]
            reverse = (edge.target_id, edge.source_id) in edge_pairs
            offset = 18.0 * zoom if reverse and edge.source_id < edge.target_id else (-18.0 * zoom if reverse else 0.0)
            dx, dy = tx - sx, ty - sy
            length = max((dx * dx + dy * dy) ** 0.5, 1.0)
            px, py = -dy / length * offset, dx / length * offset
            ux, uy = dx / length, dy / length

            def border_distance(vx: float, vy: float) -> float:
                candidates = []
                if abs(vx) > 1e-9:
                    candidates.append((node_w / 2) / abs(vx))
                if abs(vy) > 1e-9:
                    candidates.append((node_h / 2) / abs(vy))
                return min(candidates) if candidates else 0.0

            source_border = border_distance(ux, uy)
            target_border = border_distance(ux, uy)
            start_x, start_y = sx + ux * source_border + px, sy + uy * source_border + py
            end_x, end_y = tx - ux * target_border + px, ty - uy * target_border + py
            line = canvas.create_line(
                start_x, start_y, end_x, end_y,
                arrow=tk.LAST,
                width=line_width,
                fill=edge_color,
                dash=self._relation_graph_edge_dash(edge, zoom),
                smooth=bool(reverse),
                tags=(f"relation-edge-{edge.relation_id}", "relation-edge"),
            )
            self._relation_graph_edge_items[line] = edge.relation_id
            edge_tag = f"relation-edge-{edge.relation_id}"
            canvas.tag_bind(edge_tag, "<Button-1>", lambda _event, relation_id=edge.relation_id: self._select_relation_from_graph(relation_id))
            edge_tip = self._relation_graph_edge_tooltip(edge, node_map)
            canvas.tag_bind(edge_tag, "<Enter>", lambda event, text=edge_tip: self._show_relation_graph_tooltip(event, text))
            canvas.tag_bind(edge_tag, "<Motion>", self._move_relation_graph_tooltip)
            canvas.tag_bind(edge_tag, "<Leave>", self._hide_relation_graph_tooltip)
            label_map = {
                "REFERENCES_SPEC": "REFERENCES",
                "COMPLIES_WITH": "COMPLIES",
                "RELEVANT_SPEC": "RELEVANT",
                "REFERENCES_SECTION": "REF SECTION",
                "DETAILS_DEFINED_IN": "DETAILS IN",
            }
            label = label_map.get(edge.semantic_type, edge.semantic_type)
            mx, my = (sx + tx) / 2 + px, (sy + ty) / 2 + py - 12 * zoom
            if zoom >= 0.65 and edge.source_id == root_id:
                canvas.create_text(mx, my, text=label, fill=edge_color, font=edge_font, tags=(edge_tag, "relation-edge-label"))

        # Quadrant guide for the status-radial dependency view.
        if root_id and root_id in scaled_positions:
            root_x, root_y = scaled_positions[root_id]
            guide_x = node_w / 2 + 72 * zoom
            guide_y = node_h / 2 + 72 * zoom
            guide_font = ("Microsoft YaHei UI", max(7, round(9 * zoom)), "bold")
            guide_fill = style.lookup("TLabel", "foreground") or "#4b5563"
            for dx, dy, text_label in (
                (0, -guide_y, "CONFIRMED"),
                (guide_x, 0, "UNRESOLVED"),
                (0, guide_y, "AMBIGUOUS"),
                (-guide_x, 0, "AUDIT"),
            ):
                canvas.create_text(
                    root_x + dx,
                    root_y + dy,
                    text=text_label,
                    fill=guide_fill,
                    font=guide_font,
                )

        self._relation_graph_node_items.clear()
        self._relation_graph_node_tag_by_id.clear()
        for index, node in enumerate(graph.nodes):
            x, y = scaled_positions[node.node_id]
            x1, y1, x2, y2 = x - node_w / 2, y - node_h / 2, x + node_w / 2, y + node_h / 2
            is_root_node = node.node_id == root_id
            if node.kind == "SUPPORTING":
                outline, dash = entity_outline, (max(2, round(2 * zoom)), max(2, round(5 * zoom)))
            elif node.status == "AMBIGUOUS":
                outline, dash = "#8b5cf6", (max(2, round(2 * zoom)), max(2, round(4 * zoom)))
            elif node.kind == "EXTERNAL" or node.status != "CONFIRMED":
                outline, dash = unresolved_outline, (max(2, round(5 * zoom)), max(2, round(3 * zoom)))
            elif node.kind == "ENTITY":
                outline, dash = entity_outline, None
            else:
                outline, dash = confirmed_outline, None
            tag = f"relation-node-{index}"
            self._relation_graph_node_tag_by_id[node.node_id] = tag
            rect = canvas.create_rectangle(x1, y1, x2, y2, fill=node_fill, outline=("#1f4e79" if is_root_node else outline), width=(line_width + (1 if is_root_node else 0)), dash=dash, tags=(tag, "relation-node"))
            self._relation_graph_node_items[rect] = node.node_id
            canvas.create_text(x, y - 11 * zoom, text=node.label, width=node_w - 20 * zoom, justify="center", fill=text_color, font=title_font, tags=(tag, "relation-node"))
            canvas.create_text(x, y + 21 * zoom, text=node.subtitle, width=node_w - 20 * zoom, justify="center", fill=text_color, font=subtitle_font, tags=(tag, "relation-node"))
            canvas.tag_bind(tag, "<ButtonPress-1>", lambda event, selected=node.node_id: self._on_relation_graph_node_drag_start(event, selected))
            canvas.tag_bind(tag, "<B1-Motion>", lambda event, selected=node.node_id: self._on_relation_graph_node_drag(event, selected))
            canvas.tag_bind(tag, "<ButtonRelease-1>", lambda event, selected=node.node_id: self._on_relation_graph_node_drag_end(event, selected))
            canvas.tag_bind(tag, "<Double-Button-1>", lambda _event, selected=node.node_id: self._on_relation_graph_node_double_clicked(selected))
            node_tip = self._relation_graph_node_tooltip(node, is_root=is_root_node)
            canvas.tag_bind(tag, "<Enter>", lambda event, text=node_tip: self._show_relation_graph_tooltip(event, text))
            canvas.tag_bind(tag, "<Motion>", self._move_relation_graph_tooltip)
            canvas.tag_bind(tag, "<Leave>", self._hide_relation_graph_tooltip)

        bbox = canvas.bbox("all")
        if bbox:
            x1, y1, x2, y2 = self._relation_graph_symmetric_bbox(bbox)
            pad = 72 * zoom
            x1, y1, x2, y2 = x1 - pad, y1 - pad, x2 + pad, y2 + pad
            canvas.update_idletasks()
            viewport_w = max(float(canvas.winfo_width()), 1.0)
            viewport_h = max(float(canvas.winfo_height()), 1.0)
            cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
            half_w = max((x2 - x1) / 2, viewport_w / 2)
            half_h = max((y2 - y1) / 2, viewport_h / 2)
            canvas.configure(
                scrollregion=(
                    cx - half_w,
                    cy - half_h,
                    cx + half_w,
                    cy + half_h,
                )
            )
        else:
            canvas.configure(scrollregion=(0, 0, 900, 600))
        if preserve_base_center is not None:
            self._center_relation_graph_on_base_point(preserve_base_center)
        elif self._relation_graph_last_root_base_point is not None:
            self._center_relation_graph_on_base_point(
                self._relation_graph_last_root_base_point
            )
        suffix = " · truncated by graph safety limits" if graph.truncated else ""
        hidden = (
            f" · {graph.hidden_title_candidates} non-primary candidate(s) hidden"
            if graph.hidden_title_candidates and not include_title_candidates else ""
        )
        self.relation_graph_status_var.set(
            f"{len(graph.nodes)} nodes · {len(graph.edges)} evidence-backed edges · depth {depth} · zoom {round(zoom * 100)}% · drag node=manual layout · double-click node=re-root · click edge=inspect{hidden}{suffix}"
        )

    def _layout_relation_graph(
        self,
        graph: RelationGraph,
        *,
        root_id: str | None,
        direction: str,
    ) -> dict[str, tuple[float, float]]:
        """Collision-free four-direction status-radial layout.

        The root is the logical center.  Each state owns one direction:
        CONFIRMED=up, UNRESOLVED=right, AMBIGUOUS=down, AUDIT=left.

        A polar fan looks attractive for a few nodes, but Toyota reference
        tables often produce 10-30 unresolved targets.  At Fit zoom those arcs
        collapse and overlap.  This layout keeps the radial semantics while
        packing each sector into an orthogonal grid with deterministic spacing.
        """
        from collections import defaultdict, deque

        nodes = {node.node_id: node for node in graph.nodes}
        if root_id and root_id in nodes:
            outgoing: dict[str, set[str]] = defaultdict(set)
            incoming: dict[str, set[str]] = defaultdict(set)
            incident_edges: dict[str, list[RelationGraphEdge]] = defaultdict(list)
            for edge in graph.edges:
                outgoing[edge.source_id].add(edge.target_id)
                incoming[edge.target_id].add(edge.source_id)
                incident_edges[edge.source_id].append(edge)
                incident_edges[edge.target_id].append(edge)

            levels: dict[str, int] = {root_id: 0}
            queue: deque[tuple[str, int]] = deque([(root_id, 0)])
            while queue:
                current, level = queue.popleft()
                if level >= 3:
                    continue
                neighbors: set[str] = set()
                if direction in {"BOTH", "OUTGOING"}:
                    neighbors.update(outgoing.get(current, set()))
                if direction in {"BOTH", "INCOMING"}:
                    neighbors.update(incoming.get(current, set()))
                for neighbor in sorted(neighbors):
                    if neighbor not in nodes:
                        continue
                    next_level = level + 1
                    previous = levels.get(neighbor)
                    if previous is None or next_level < previous:
                        levels[neighbor] = next_level
                        queue.append((neighbor, next_level))

            max_level = max(levels.values(), default=0)
            for node_id in nodes:
                levels.setdefault(node_id, max_level + 1)

            def sector_for_node(node_id: str) -> str:
                if node_id == root_id:
                    return "ROOT"
                node = nodes[node_id]
                subtitle = str(node.subtitle or "").upper()
                if (
                    node.kind == "SUPPORTING"
                    or "AUDIT" in subtitle
                    or "TITLE-ONLY" in subtitle
                ):
                    return "AUDIT"
                if any(
                    edge.semantic_type == "TITLE_ONLY_CANDIDATE"
                    for edge in incident_edges.get(node_id, [])
                ):
                    return "AUDIT"
                status = str(node.status or "").upper()
                if status == "CONFIRMED":
                    return "CONFIRMED"
                if status == "AMBIGUOUS":
                    return "AMBIGUOUS"
                if status == "UNRESOLVED" or node.kind == "EXTERNAL":
                    return "UNRESOLVED"
                return "AUDIT"

            grouped: dict[tuple[str, int], list[str]] = defaultdict(list)
            for node_id in nodes:
                if node_id == root_id:
                    continue
                grouped[(sector_for_node(node_id), levels[node_id])].append(node_id)
            for ids in grouped.values():
                ids.sort(
                    key=lambda node_id: (
                        nodes[node_id].label.casefold(),
                        node_id,
                    )
                )

            center_x = 1100.0
            center_y = 760.0
            positions: dict[str, tuple[float, float]] = {
                root_id: (center_x, center_y)
            }

            node_w = 248.0
            node_h = 88.0
            horizontal_gap = 54.0
            vertical_gap = 40.0
            col_pitch = node_w + horizontal_gap
            row_pitch = node_h + vertical_gap
            root_clear_x = node_w / 2 + 150.0
            root_clear_y = node_h / 2 + 150.0
            level_gap = 120.0
            max_rows_side = 4
            max_cols_vertical = 4

            # Right / left sectors: vertical rows, expanding by columns.
            for sector, sign in (("UNRESOLVED", +1), ("AUDIT", -1)):
                cursor = root_clear_x
                levels_in_sector = sorted(
                    level for bucket, level in grouped if bucket == sector
                )
                for level in levels_in_sector:
                    ids = grouped[(sector, level)]
                    columns = max(
                        1,
                        (len(ids) + max_rows_side - 1) // max_rows_side,
                    )
                    for idx, node_id in enumerate(ids):
                        col = idx // max_rows_side
                        row = idx % max_rows_side
                        rows_this_col = min(
                            max_rows_side,
                            len(ids) - col * max_rows_side,
                        )
                        y = (
                            center_y
                            + (row - (rows_this_col - 1) / 2) * row_pitch
                        )
                        x = center_x + sign * (cursor + col * col_pitch)
                        positions[node_id] = (x, y)
                    cursor += columns * col_pitch + level_gap

            # Up / down sectors: horizontal columns, expanding by rows.
            for sector, sign in (("CONFIRMED", -1), ("AMBIGUOUS", +1)):
                cursor = root_clear_y
                levels_in_sector = sorted(
                    level for bucket, level in grouped if bucket == sector
                )
                for level in levels_in_sector:
                    ids = grouped[(sector, level)]
                    rows = max(
                        1,
                        (len(ids) + max_cols_vertical - 1)
                        // max_cols_vertical,
                    )
                    for idx, node_id in enumerate(ids):
                        row = idx // max_cols_vertical
                        col = idx % max_cols_vertical
                        cols_this_row = min(
                            max_cols_vertical,
                            len(ids) - row * max_cols_vertical,
                        )
                        x = (
                            center_x
                            + (col - (cols_this_row - 1) / 2) * col_pitch
                        )
                        y = center_y + sign * (cursor + row * row_pitch)
                        positions[node_id] = (x, y)
                    cursor += rows * row_pitch + level_gap

            return positions

        ordered = sorted(
            nodes.values(),
            key=lambda node: (
                node.kind != "SPECIFICATION",
                node.label.casefold(),
                node.node_id,
            ),
        )
        columns = 4 if len(ordered) > 8 else max(2, min(3, len(ordered)))
        positions: dict[str, tuple[float, float]] = {}
        for idx, node in enumerate(ordered):
            row, col = divmod(idx, columns)
            positions[node.node_id] = (180 + col * 300, 120 + row * 132)
        return positions

    def _on_relation_graph_node_clicked(self, node_id: str) -> None:
        for label, mapped_id in self._relation_graph_root_by_label.items():
            if mapped_id == node_id:
                self.relation_graph_root_var.set(label)
                self._render_relation_graph()
                return

    def _select_relation_from_graph(self, relation_id: int) -> None:
        iid = f"relation-{int(relation_id)}"
        if iid not in self._relation_by_iid:
            return
        self.relation_view_notebook.select(0)
        self.relation_registry_tree.selection_set(iid)
        self.relation_registry_tree.focus(iid)
        self.relation_registry_tree.see(iid)
        self._on_relation_registry_selected()

    @staticmethod
    def _manual_relation_spec_label(row: dict[str, Any]) -> str:
        code = str(row.get("spec_code") or "").strip()
        name = str(row.get("canonical_name") or row.get("specification_key") or "-").strip()
        version = str(row.get("current_version") or "-").strip()
        lead = code or str(row.get("specification_key") or "-")
        return f"{lead} — {name} · {version}"

    def _refresh_manual_relation_editor(self, *, silent: bool = False) -> None:
        if not hasattr(self, "manual_relation_tree"):
            return
        resolved = self._get_version_history_connection()
        if resolved is None:
            if not silent:
                self.manual_relation_editor_status_var.set("PostgreSQL connection is not ready.")
            return
        database_connection, _ = resolved
        try:
            service = EngineeringRelationAuditService(database_connection)
            schema_installed = service.manual_schema_installed()
            specs = service.list_specifications_for_manual_editor()
            rows = service.list_manual_relations()
        except Exception as exc:
            if not silent:
                self.manual_relation_editor_status_var.set(str(exc))
            return

        previous_source = self.manual_relation_source_var.get()
        previous_target = self.manual_relation_target_var.get()
        self._manual_relation_spec_by_label.clear()
        self._manual_relation_spec_label_by_id.clear()
        labels: list[str] = []
        for row in specs:
            label = self._manual_relation_spec_label(row)
            labels.append(label)
            self._manual_relation_spec_by_label[label] = row
            self._manual_relation_spec_label_by_id[int(row["specification_id"])] = label
        self.manual_relation_source_combo.configure(values=tuple(labels))
        self.manual_relation_target_combo.configure(values=tuple(labels))
        if previous_source in self._manual_relation_spec_by_label:
            self.manual_relation_source_var.set(previous_source)
        elif labels:
            preferred = self._manual_relation_source_label_for_graph_root()
            self.manual_relation_source_var.set(preferred or labels[0])
        if previous_target in self._manual_relation_spec_by_label:
            self.manual_relation_target_var.set(previous_target)
        elif labels:
            self.manual_relation_target_var.set(labels[0])

        self._manual_relation_rows_by_iid.clear()
        for iid in self.manual_relation_tree.get_children():
            self.manual_relation_tree.delete(iid)
        for row in rows:
            manual_id = int(row["manual_id"])
            iid = f"manual-relation-{manual_id}"
            self._manual_relation_rows_by_iid[iid] = row
            source = self._manual_relation_source_target_display(
                row.get("source_spec_code"),
                row.get("source_specification_name"),
                None,
            )
            target = self._manual_relation_source_target_display(
                row.get("target_spec_code"),
                row.get("target_specification_name"),
                row.get("target_external_key"),
            )
            updated = row.get("updated_at")
            updated_text = str(updated)[:19] if updated is not None else "-"
            self.manual_relation_tree.insert(
                "",
                END,
                iid=iid,
                values=(
                    source,
                    row.get("semantic_type") or "-",
                    target,
                    row.get("status") or "-",
                    row.get("evidence_kind") or "-",
                    updated_text,
                ),
            )
        self._on_manual_relation_target_mode_changed()
        if schema_installed:
            self.manual_relation_editor_status_var.set(
                f"{len(rows)} active manual relation(s) · {len(specs)} specification(s) available"
            )
        else:
            self.manual_relation_editor_status_var.set(
                "Manual editor SQL is not installed. Run database/upgrade_manual_spec_relations_v6_5.sql."
            )

    def _manual_relation_source_label_for_graph_root(self) -> str | None:
        root_id = self._relation_graph_current_root_id() if hasattr(self, "relation_graph_root_var") else None
        if not root_id or not root_id.startswith("SPEC::"):
            return None
        key = root_id[len("SPEC::"):]
        for label, row in self._manual_relation_spec_by_label.items():
            if str(row.get("specification_key") or "") == key:
                return label
        return None

    @staticmethod
    def _manual_relation_source_target_display(code: Any, name: Any, external: Any) -> str:
        code_text = str(code or "").strip()
        name_text = str(name or "").strip()
        external_text = str(external or "").strip()
        if code_text and name_text:
            return f"{code_text} · {name_text}"
        if code_text or name_text:
            return code_text or name_text
        return external_text or "-"

    def _on_manual_relation_target_mode_changed(self) -> None:
        if not hasattr(self, "manual_relation_target_combo"):
            return
        external = self.manual_relation_target_mode_var.get().strip().upper().startswith("EXTERNAL")
        self.manual_relation_target_combo.configure(state=("disabled" if external else "readonly"))
        self.manual_relation_external_code_entry.configure(state=("normal" if external else "disabled"))
        self.manual_relation_title_hint_entry.configure(state=("normal" if external else "disabled"))
        if external and self.manual_relation_status_var.get() == "CONFIRMED":
            self.manual_relation_status_var.set("AUTO")
        if not external and self.manual_relation_status_var.get() == "UNRESOLVED":
            self.manual_relation_status_var.set("AUTO")

    def _clear_manual_relation_editor(self) -> None:
        self._selected_manual_relation_id = None
        if hasattr(self, "manual_relation_tree"):
            self.manual_relation_tree.selection_remove(*self.manual_relation_tree.selection())
        preferred = self._manual_relation_source_label_for_graph_root()
        if preferred:
            self.manual_relation_source_var.set(preferred)
        self.manual_relation_target_mode_var.set("EXISTING")
        labels = tuple(self.manual_relation_target_combo.cget("values")) if hasattr(self, "manual_relation_target_combo") else ()
        if labels:
            self.manual_relation_target_var.set(str(labels[0]))
        self.manual_relation_external_code_var.set("")
        self.manual_relation_title_hint_var.set("")
        self.manual_relation_semantic_var.set("REFERENCES_SPEC")
        self.manual_relation_status_var.set("AUTO")
        self.manual_relation_locator_type_var.set("NONE")
        self.manual_relation_locator_value_var.set("")
        self.manual_relation_evidence_kind_var.set("SOURCE_QUOTE")
        self.manual_relation_note_var.set("")
        if hasattr(self, "manual_relation_evidence_text"):
            self.manual_relation_evidence_text.delete("1.0", END)
        self._on_manual_relation_target_mode_changed()
        self.manual_relation_editor_status_var.set("Ready to add a manual relation.")

    def _on_manual_relation_tree_selected(self, _event: Any | None = None) -> None:
        selection = self.manual_relation_tree.selection()
        if not selection:
            return
        row = self._manual_relation_rows_by_iid.get(selection[0])
        if row is None:
            return
        self._selected_manual_relation_id = int(row["manual_id"])
        source_label = self._manual_relation_spec_label_by_id.get(int(row["source_specification_id"]))
        if source_label:
            self.manual_relation_source_var.set(source_label)
        target_id = row.get("target_specification_id")
        if target_id is not None:
            self.manual_relation_target_mode_var.set("EXISTING")
            target_label = self._manual_relation_spec_label_by_id.get(int(target_id))
            if target_label:
                self.manual_relation_target_var.set(target_label)
            self.manual_relation_external_code_var.set("")
            self.manual_relation_title_hint_var.set("")
        else:
            self.manual_relation_target_mode_var.set("EXTERNAL SPEC NO.")
            external = str(row.get("target_external_key") or "")
            self.manual_relation_external_code_var.set(external[len("SPEC:"):] if external.startswith("SPEC:") else external)
            metadata = dict(row.get("metadata") or {})
            self.manual_relation_title_hint_var.set(str(metadata.get("target_title_hint") or ""))
        self.manual_relation_semantic_var.set(str(row.get("semantic_type") or "REFERENCES_SPEC"))
        self.manual_relation_status_var.set(str(row.get("status") or "AUTO"))
        self.manual_relation_locator_type_var.set(str(row.get("target_locator_type") or "NONE"))
        self.manual_relation_locator_value_var.set(str(row.get("target_locator_value") or ""))
        self.manual_relation_evidence_kind_var.set(str(row.get("evidence_kind") or "SOURCE_QUOTE"))
        self.manual_relation_note_var.set(str(row.get("note") or ""))
        self.manual_relation_evidence_text.delete("1.0", END)
        self.manual_relation_evidence_text.insert("1.0", str(row.get("evidence_text") or ""))
        self._on_manual_relation_target_mode_changed()
        self.manual_relation_editor_status_var.set(f"Editing manual relation #{self._selected_manual_relation_id}")

    def _manual_relation_editor_payload(self) -> dict[str, Any]:
        source_label = self.manual_relation_source_var.get().strip()
        source = self._manual_relation_spec_by_label.get(source_label)
        if source is None:
            raise ValueError("Select a source specification.")
        target_mode_external = self.manual_relation_target_mode_var.get().strip().upper().startswith("EXTERNAL")
        target_id: int | None = None
        external_code: str | None = None
        if target_mode_external:
            external_code = self.manual_relation_external_code_var.get().strip()
            if not external_code:
                raise ValueError("Enter the external Toyota Specification No.")
        else:
            target = self._manual_relation_spec_by_label.get(self.manual_relation_target_var.get().strip())
            if target is None:
                raise ValueError("Select an existing target specification.")
            target_id = int(target["specification_id"])
        evidence_text = self.manual_relation_evidence_text.get("1.0", END).strip()
        if not evidence_text:
            raise ValueError("Evidence / engineering decision text is required.")
        return {
            "source_specification_id": int(source["specification_id"]),
            "target_specification_id": target_id,
            "target_external_code": external_code,
            "target_title_hint": self.manual_relation_title_hint_var.get().strip() or None,
            "semantic_type": self.manual_relation_semantic_var.get().strip(),
            "status": self.manual_relation_status_var.get().strip(),
            "evidence_kind": self.manual_relation_evidence_kind_var.get().strip(),
            "evidence_text": evidence_text,
            "target_locator_type": self.manual_relation_locator_type_var.get().strip(),
            "target_locator_value": self.manual_relation_locator_value_var.get().strip() or None,
            "note": self.manual_relation_note_var.get().strip() or None,
        }

    def _save_manual_relation_from_editor(self, *, create: bool) -> None:
        if not create and self._selected_manual_relation_id is None:
            messagebox.showwarning("Manual Relation", "Select a manual relation to update.", parent=self.root)
            return
        resolved = self._get_version_history_connection()
        if resolved is None:
            return
        database_connection, _ = resolved
        try:
            payload = self._manual_relation_editor_payload()
            service = EngineeringRelationAuditService(database_connection)
            if create:
                manual_id = service.create_manual_relation(**payload)
                action = "created"
            else:
                manual_id = service.update_manual_relation(
                    manual_id=int(self._selected_manual_relation_id),
                    **payload,
                )
                action = "updated"
        except Exception as exc:
            messagebox.showerror("Manual Relation", str(exc), parent=self.root)
            self.manual_relation_editor_status_var.set(str(exc))
            return
        self._selected_manual_relation_id = int(manual_id)
        self.manual_relation_editor_status_var.set(f"Manual relation #{manual_id} {action}.")
        self._refresh_manual_relation_editor(silent=False)
        self.refresh_engineering_relations()

    def _delete_manual_relation_from_editor(self) -> None:
        if self._selected_manual_relation_id is None:
            messagebox.showwarning("Manual Relation", "Select a manual relation to delete.", parent=self.root)
            return
        manual_id = int(self._selected_manual_relation_id)
        if not messagebox.askyesno(
            "Delete Manual Relation",
            f"Deactivate manual relation #{manual_id}?\n\nThe audit history will be retained.",
            parent=self.root,
        ):
            return
        resolved = self._get_version_history_connection()
        if resolved is None:
            return
        database_connection, _ = resolved
        try:
            EngineeringRelationAuditService(database_connection).delete_manual_relation(manual_id)
        except Exception as exc:
            messagebox.showerror("Manual Relation", str(exc), parent=self.root)
            return
        self._clear_manual_relation_editor()
        self._refresh_manual_relation_editor(silent=False)
        self.refresh_engineering_relations()

    def refresh_engineering_relations(self) -> None:
        if not hasattr(self, "relation_registry_tree"):
            return
        if self.relation_audit_thread is not None and self.relation_audit_thread.is_alive():
            return
        resolved = self._get_version_history_connection()
        if resolved is None:
            self.relation_registry_status_var.set("PostgreSQL connection is not ready.")
            return
        database_connection, _ = resolved
        generation = self._spec_context_generation
        current_only = bool(self.relation_current_only_var.get())
        self.relation_registry_status_var.set("Loading engineering relations...")
        self.relation_audit_thread = threading.Thread(
            target=self._engineering_relations_worker,
            args=(database_connection, current_only, generation),
            daemon=True,
        )
        self.relation_audit_thread.start()

    def _engineering_relations_worker(
        self,
        database_connection: DatabaseConnection,
        current_only: bool,
        generation: int,
    ) -> None:
        try:
            service = EngineeringRelationAuditService(database_connection)
            rows = service.list_relations(current_source_only=current_only)
            summary = service.summary(current_source_only=current_only)
            self._emit(
                "engineering_relations_result",
                {"ok": True, "rows": rows, "summary": summary, "generation": generation},
            )
        except Exception as exc:
            self._emit(
                "engineering_relations_result",
                {"ok": False, "error": str(exc), "generation": generation},
            )

    def _handle_engineering_relations_result(self, payload: dict[str, Any]) -> None:
        if int(payload.get("generation") or -1) != self._spec_context_generation:
            return
        if not payload.get("ok"):
            self.relation_registry_status_var.set("Failed to load engineering relations.")
            messagebox.showerror(
                "Engineering Relations",
                str(payload.get("error") or "Unknown error"),
                parent=self.root,
            )
            return
        self._relation_rows = list(payload.get("rows") or [])
        summary = dict(payload.get("summary") or {})
        self.relation_summary_total_var.set(f"Relations {int(summary.get('relation_count') or 0)}")
        self.relation_summary_confirmed_var.set(f"Confirmed {int(summary.get('confirmed_count') or 0)}")
        self.relation_summary_unresolved_var.set(f"Unresolved {int(summary.get('unresolved_count') or 0)}")
        self.relation_summary_evidence_var.set(f"Evidence {int(summary.get('evidence_count') or 0)}")
        relation_types = sorted({str(row.get("relation_type") or "") for row in self._relation_rows if row.get("relation_type")})
        semantic_types = sorted({self._relation_semantic_type(row) for row in self._relation_rows})
        self.relation_type_combo.configure(values=("ALL", *relation_types))
        self.relation_semantic_combo.configure(values=("ALL", *semantic_types))
        if self.relation_type_filter_var.get() not in {"ALL", *relation_types}:
            self.relation_type_filter_var.set("ALL")
        if self.relation_semantic_filter_var.get() not in {"ALL", *semantic_types}:
            self.relation_semantic_filter_var.set("ALL")
        self._apply_relation_filters()
        self.relation_registry_status_var.set(
            f"{len(self._visible_relation_rows)} shown / {len(self._relation_rows)} loaded"
        )
        self._refresh_manual_relation_editor(silent=True)

    def _on_relation_view_filter_changed(self, _event: Any | None = None) -> None:
        self.relation_type_filter_var.set("ALL")
        self.relation_semantic_filter_var.set("ALL")
        self._apply_relation_filters()

    @staticmethod
    def _relation_metadata(row: dict[str, Any]) -> dict[str, Any]:
        value = row.get("metadata")
        if isinstance(value, dict):
            return dict(value)
        return {}

    @classmethod
    def _relation_semantic_type(cls, row: dict[str, Any]) -> str:
        return EngineeringRelationGraphService.semantic_type(row)

    @classmethod
    def _relation_source_display(cls, row: dict[str, Any]) -> str:
        code = str(row.get("source_spec_code") or "").strip()
        name = str(row.get("source_specification_name") or row.get("source_specification_key") or "-")
        return f"{code} · {name}" if code and code != name else name

    @classmethod
    def _relation_target_display(cls, row: dict[str, Any]) -> str:
        code = str(row.get("target_spec_code") or "").strip()
        name = str(row.get("target_specification_name") or "").strip()
        if code or name:
            if code and name and code != name:
                return f"{code} · {name}"
            return code or name
        external = str(row.get("target_external_key") or "").strip()
        metadata = cls._relation_metadata(row)
        if external.startswith("SPEC:"):
            spec_code = external[len("SPEC:"):]
            hint = str(metadata.get("target_title_hint") or "").strip()
            return f"{spec_code} · {hint}" if hint else spec_code
        if external.startswith("SPEC_TITLE:"):
            matched = str(metadata.get("matched_reference") or "").strip()
            return f"Title candidate: {matched or external[len('SPEC_TITLE:'):]}"
        return external or str(row.get("target_entity_name") or "-")

    def _apply_relation_filters(self) -> None:
        if not hasattr(self, "relation_registry_tree"):
            return
        for iid in self.relation_registry_tree.get_children():
            self.relation_registry_tree.delete(iid)
        self._relation_by_iid.clear()
        view_filter = self.relation_view_filter_var.get().strip().upper() or "ALL"
        relation_filter = self.relation_type_filter_var.get().strip().upper() or "ALL"
        semantic_filter = self.relation_semantic_filter_var.get().strip().upper() or "ALL"
        status_filter = self.relation_status_filter_var.get().strip().upper() or "ALL"
        query = self.relation_search_var.get().strip().casefold()
        visible = 0
        visible_rows: list[dict[str, Any]] = []
        for row in self._relation_rows:
            relation_type = str(row.get("relation_type") or "")
            semantic_type = self._relation_semantic_type(row)
            status = str(row.get("status") or "")
            target = self._relation_target_display(row)
            haystack = " ".join(
                str(row.get(key) or "")
                for key in (
                    "source_specification_name", "source_specification_key",
                    "source_version", "relation_type", "target_specification_name",
                    "target_entity_type", "target_entity_name", "target_external_key",
                    "source_spec_code", "target_spec_code", "extraction_method", "status",
                )
            ).casefold()
            haystack += " " + semantic_type.casefold() + " " + str(self._relation_metadata(row)).casefold()
            if view_filter == "SPEC REFERENCES":
                if relation_type.upper() != "REFERENCES":
                    continue
                if not EngineeringRelationGraphService.is_primary_reference_row(row):
                    continue
            if view_filter == "REFERENCE AUDIT" and relation_type.upper() != "REFERENCES":
                continue
            if view_filter == "ENGINEERING FACTS" and relation_type.upper() == "REFERENCES":
                continue
            if relation_filter != "ALL" and relation_type.upper() != relation_filter:
                continue
            if semantic_filter != "ALL" and semantic_type.upper() != semantic_filter:
                continue
            if status_filter != "ALL" and status.upper() != status_filter:
                continue
            if query and query not in haystack:
                continue
            relation_id = int(row["relation_id"])
            iid = f"relation-{relation_id}"
            self._relation_by_iid[iid] = row
            confidence = row.get("confidence")
            confidence_text = f"{float(confidence) * 100:.1f}%" if confidence is not None else "-"
            self.relation_registry_tree.insert(
                "", END, iid=iid, values=(
                    self._relation_source_display(row),
                    row.get("source_version") or "-",
                    relation_type or "-",
                    semantic_type or "-",
                    target,
                    status or "-",
                    confidence_text,
                    f"{int(row.get('evidence_count') or 0)} / {int(row.get('evidence_chunk_count') or 0)}",
                )
            )
            visible_rows.append(row)
            visible += 1
        self._visible_relation_rows = visible_rows
        # Summary cards follow the active view/filter instead of mixing hidden
        # Engineering Facts and audit candidates into the SPEC REFERENCES count.
        confirmed_visible = sum(
            1 for row in visible_rows
            if str(row.get("status") or "").strip().upper() == "CONFIRMED"
        )
        unresolved_visible = len(visible_rows) - confirmed_visible
        evidence_visible = sum(int(row.get("evidence_count") or 0) for row in visible_rows)
        self.relation_summary_total_var.set(f"Relations {len(visible_rows)}")
        self.relation_summary_confirmed_var.set(f"Confirmed {confirmed_visible}")
        self.relation_summary_unresolved_var.set(f"Unresolved {unresolved_visible}")
        self.relation_summary_evidence_var.set(f"Evidence {evidence_visible}")
        self.relation_registry_status_var.set(f"{visible} shown / {len(self._relation_rows)} loaded")
        self._clear_relation_evidence()
        self._refresh_relation_graph_roots()
        self._render_relation_graph()

    def _on_relation_registry_selected(self, _event: Any | None = None) -> None:
        selection = self.relation_registry_tree.selection()
        if not selection:
            return
        row = self._relation_by_iid.get(selection[0])
        if row is None:
            return
        relation_id = int(row["relation_id"])
        self._selected_relation_id = relation_id
        target = self._relation_target_display(row)
        semantic = self._relation_semantic_type(row)
        self.relation_evidence_header_var.set(
            f"{self._relation_source_display(row)} {row.get('source_version') or ''}  →  "
            f"{semantic}  →  {target}"
        )
        self.relation_evidence_location_var.set(
            f"Method: {row.get('extraction_method') or '-'} · Status: {row.get('status') or '-'} · "
            f"Evidence: {row.get('evidence_count') or 0}"
        )
        resolved = self._get_version_history_connection()
        if resolved is None:
            return
        database_connection, _ = resolved
        generation = self._spec_context_generation
        self.relation_audit_thread = threading.Thread(
            target=self._relation_evidence_worker,
            args=(database_connection, relation_id, generation),
            daemon=True,
        )
        self.relation_audit_thread.start()

    def _relation_evidence_worker(
        self, database_connection: DatabaseConnection, relation_id: int, generation: int
    ) -> None:
        try:
            rows = EngineeringRelationAuditService(database_connection).list_evidence(relation_id)
            self._emit(
                "relation_evidence_result",
                {"ok": True, "relation_id": relation_id, "rows": rows, "generation": generation},
            )
        except Exception as exc:
            self._emit(
                "relation_evidence_result",
                {"ok": False, "relation_id": relation_id, "error": str(exc), "generation": generation},
            )

    def _handle_relation_evidence_result(self, payload: dict[str, Any]) -> None:
        if int(payload.get("generation") or -1) != self._spec_context_generation:
            return
        if int(payload.get("relation_id") or 0) != int(self._selected_relation_id or 0):
            return
        if not payload.get("ok"):
            messagebox.showerror(
                "Relation Evidence", str(payload.get("error") or "Unknown error"), parent=self.root
            )
            return
        self._relation_evidence_by_iid.clear()
        self._relation_evidence_group_by_iid.clear()
        for iid in self.relation_evidence_tree.get_children():
            self.relation_evidence_tree.delete(iid)
        rows = list(payload.get("rows") or [])

        grouped: dict[str, list[dict[str, Any]]] = {}
        order: list[str] = []
        for row in rows:
            content_id = row.get("content_id")
            if content_id is None:
                group_key = f"unmapped-{row.get('evidence_id')}"
            else:
                group_key = f"content-{content_id}"
            if group_key not in grouped:
                grouped[group_key] = []
                order.append(group_key)
            grouped[group_key].append(row)

        for group_key in order:
            group_rows = grouped[group_key]
            first_row = group_rows[0]
            content_id = first_row.get("content_id")
            parent_iid = f"chunk-{group_key}"
            statuses = {str(row.get("mapping_status") or "UNAVAILABLE") for row in group_rows}
            mapping_status = next(iter(statuses)) if len(statuses) == 1 else "MIXED"
            location_parts = self._relation_evidence_location_parts(first_row)
            chunk_index = first_row.get("mapped_chunk_index")
            chunk_label = f"Chunk #{content_id}" if content_id is not None else "Unmapped evidence"
            if chunk_index is not None:
                chunk_label += f" · idx {chunk_index}"
            chunk_label += f" · {len(group_rows)} evidence"
            self._relation_evidence_group_by_iid[parent_iid] = {
                "kind": "chunk",
                "content_id": content_id,
                "rows": group_rows,
                "mapping_status": mapping_status,
                "mapped_chunk_content": first_row.get("mapped_chunk_content") or "",
            }
            self.relation_evidence_tree.insert(
                "",
                END,
                iid=parent_iid,
                text=chunk_label,
                values=(
                    " · ".join(location_parts) or "Canonical source chunk",
                    first_row.get("document_id") or "-",
                    mapping_status,
                ),
                open=(len(group_rows) <= 3),
            )
            for row in group_rows:
                evidence_id = int(row["evidence_id"])
                iid = f"evidence-{evidence_id}"
                self._relation_evidence_by_iid[iid] = row
                evidence_location = self._relation_evidence_location_parts(row)
                offset_label = ""
                if row.get("start_offset") is not None or row.get("end_offset") is not None:
                    offset_label = f"chars {row.get('start_offset') if row.get('start_offset') is not None else '?'}-{row.get('end_offset') if row.get('end_offset') is not None else '?'}"
                    evidence_location.append(offset_label)
                self.relation_evidence_tree.insert(
                    parent_iid,
                    END,
                    iid=iid,
                    text=f"Evidence #{evidence_id}",
                    values=(
                        " · ".join(evidence_location) or "Evidence occurrence",
                        row.get("document_id") or "-",
                        row.get("mapping_status") or "UNAVAILABLE",
                    ),
                )

        self._set_readonly_text(self.relation_evidence_text, "")
        self._set_readonly_text(self.relation_chunk_text, "")
        if order:
            first_parent = f"chunk-{order[0]}"
            self.relation_evidence_tree.selection_set(first_parent)
            self.relation_evidence_tree.see(first_parent)
            self._on_relation_evidence_selected()

    @staticmethod
    def _relation_evidence_location_parts(row: dict[str, Any]) -> list[str]:
        parts: list[str] = []
        if row.get("sheet_name"):
            parts.append(str(row.get("sheet_name")))
        if row.get("cell_range"):
            parts.append(str(row.get("cell_range")))
        elif row.get("page_number"):
            parts.append(f"page {row.get('page_number')}")
        if row.get("section_title"):
            parts.append(str(row.get("section_title")))
        return parts

    def _on_relation_evidence_selected(self, _event: Any | None = None) -> None:
        selection = self.relation_evidence_tree.selection()
        if not selection:
            return
        iid = selection[0]
        group = self._relation_evidence_group_by_iid.get(iid)
        if group is not None:
            rows = list(group.get("rows") or [])
            snippets = []
            for row in rows:
                evidence_id = row.get("evidence_id")
                start = row.get("start_offset")
                end = row.get("end_offset")
                offset = f" [{start}-{end}]" if start is not None or end is not None else ""
                snippets.append(f"Evidence #{evidence_id}{offset}\n{row.get('evidence_text') or ''}")
            self.relation_evidence_location_var.set(
                f"Chunk content_id={group.get('content_id')} · {len(rows)} evidence occurrence(s) · "
                f"Mapping: {group.get('mapping_status') or 'UNAVAILABLE'}"
            )
            self._set_readonly_text(self.relation_evidence_text, "\n\n".join(snippets))
            self._set_readonly_text(self.relation_chunk_text, str(group.get("mapped_chunk_content") or ""))
            return

        row = self._relation_evidence_by_iid.get(iid)
        if row is None:
            return
        location = []
        if row.get("sheet_name"):
            location.append(f"Sheet: {row.get('sheet_name')}")
        if row.get("cell_range"):
            location.append(f"Range: {row.get('cell_range')}")
        if row.get("page_number"):
            location.append(f"Page: {row.get('page_number')}")
        if row.get("chapter_title"):
            location.append(f"Chapter: {row.get('chapter_title')}")
        if row.get("section_title"):
            location.append(f"Section: {row.get('section_title')}")
        if row.get("start_offset") is not None or row.get("end_offset") is not None:
            location.append(f"Chars: {row.get('start_offset')}–{row.get('end_offset')}")
        location.append(f"Mapping: {row.get('mapping_status') or 'UNAVAILABLE'}")
        self.relation_evidence_location_var.set(" · ".join(location))
        self._set_readonly_text(self.relation_evidence_text, str(row.get("evidence_text") or ""))
        self._set_readonly_text(self.relation_chunk_text, str(row.get("mapped_chunk_content") or ""))

    def _clear_relation_evidence(self) -> None:
        self._selected_relation_id = None
        if hasattr(self, "relation_evidence_tree"):
            for iid in self.relation_evidence_tree.get_children():
                self.relation_evidence_tree.delete(iid)
        self._relation_evidence_by_iid.clear()
        self._relation_evidence_group_by_iid.clear()
        if hasattr(self, "relation_evidence_header_var"):
            self.relation_evidence_header_var.set("Select a relation to inspect source evidence.")
            self.relation_evidence_location_var.set("")
            self._set_readonly_text(self.relation_evidence_text, "")
            if hasattr(self, "relation_chunk_text"):
                self._set_readonly_text(self.relation_chunk_text, "")

    def reconcile_engineering_relations(self) -> None:
        if self.relation_audit_thread is not None and self.relation_audit_thread.is_alive():
            return
        resolved = self._get_version_history_connection()
        if resolved is None:
            return
        database_connection, _ = resolved
        try:
            project = ProjectRegistry.resolve(self.project_var.get())
        except ProjectRegistryError:
            return
        generation = self._spec_context_generation
        self.relation_registry_status_var.set("Reconciling unresolved specification references...")
        self.relation_audit_thread = threading.Thread(
            target=self._relation_reconcile_worker,
            args=(database_connection, project.code, generation),
            daemon=True,
        )
        self.relation_audit_thread.start()

    def _relation_reconcile_worker(
        self, database_connection: DatabaseConnection, project_code: str, generation: int
    ) -> None:
        try:
            count = EngineeringRelationAuditService(database_connection).reconcile_unresolved(
                project_code=project_code
            )
            self._emit(
                "relation_reconcile_result",
                {"ok": True, "count": count, "generation": generation},
            )
        except Exception as exc:
            self._emit(
                "relation_reconcile_result",
                {"ok": False, "error": str(exc), "generation": generation},
            )

    def _handle_relation_reconcile_result(self, payload: dict[str, Any]) -> None:
        if int(payload.get("generation") or -1) != self._spec_context_generation:
            return
        if not payload.get("ok"):
            self.relation_registry_status_var.set("Reconciliation failed.")
            messagebox.showerror(
                "Engineering Relations", str(payload.get("error") or "Unknown error"), parent=self.root
            )
            return
        count = int(payload.get("count") or 0)
        self.relation_registry_status_var.set(f"Reconciled {count} unresolved relation(s).")
        self.root.after_idle(self.refresh_engineering_relations)

    def _format_spec_selector_label(self, row: dict[str, Any]) -> str:
        spec_id = int(row["specification_id"])
        name = str(row.get("canonical_name") or row.get("specification_key") or f"Specification {spec_id}")
        current = str(row.get("current_version") or "-")
        return f"{name}  ·  {current}  [#{spec_id}]"

    def _update_spec_selectors(self) -> None:
        values = [self._spec_id_to_display[spec_id] for spec_id in sorted(self._spec_id_to_display, key=lambda item: self._spec_id_to_display[item].casefold())]
        for attr in ("spec_versions_selector", "spec_compare_selector", "spec_changes_selector"):
            widget = getattr(self, attr, None)
            if widget is not None:
                widget.configure(values=values)
        if self._selected_specification_id in self._spec_id_to_display:
            self.spec_active_selector_var.set(self._spec_id_to_display[self._selected_specification_id])
        elif values:
            self.spec_active_selector_var.set(values[0])

    def _on_spec_selector_selected(self, _event: Any | None = None) -> None:
        spec_id = self._spec_display_to_id.get(self.spec_active_selector_var.get())
        if spec_id is not None:
            self._select_specification_by_id(spec_id)

    def _on_change_spec_selector_selected(self, _event: Any | None = None) -> None:
        self._on_spec_selector_selected(_event)
        self.root.after_idle(self.refresh_change_timeline)

    def _select_specification_by_id(self, specification_id: int) -> None:
        row = next((item for item in self._version_catalog_rows if int(item.get("specification_id") or 0) == int(specification_id)), None)
        if row is None:
            return
        self._selected_specification_id = int(specification_id)
        display = self._spec_id_to_display.get(int(specification_id), self._format_spec_selector_label(row))
        self.spec_active_selector_var.set(display)
        self._update_spec_detail(row)
        iid = f"spec-{specification_id}"
        if hasattr(self, "version_master_tree") and self.version_master_tree.exists(iid):
            self._suppress_spec_tree_event = True
            try:
                self.version_master_tree.selection_set(iid)
                self.version_master_tree.see(iid)
            finally:
                self._suppress_spec_tree_event = False
        self._load_selected_specification_versions(row)

    def _update_spec_detail(self, row: dict[str, Any]) -> None:
        name = str(row.get("canonical_name") or row.get("specification_key") or "-")
        self.spec_detail_name_var.set(name)
        self.spec_detail_key_var.set(str(row.get("specification_key") or "-"))
        self.spec_detail_current_var.set(str(row.get("current_version") or "-"))
        self.spec_detail_versions_var.set(str(row.get("version_count") or 0))
        self.spec_detail_history_var.set(str(row.get("history_count") or 0))
        self.spec_detail_type_var.set(str(row.get("spec_subtype") or row.get("spec_type") or "-"))
        self.spec_detail_region_var.set(str(row.get("region_scope") or "-"))
        self.spec_detail_status_var.set(str(row.get("specification_status") or "ACTIVE"))
        updated = row.get("updated_at")
        self.spec_detail_updated_var.set(updated.strftime("%Y-%m-%d %H:%M") if hasattr(updated, "strftime") else str(updated or "-")[:16])
        self.version_selected_master_var.set(f"{name}  |  Current: {row.get('current_version') or '-'}  |  Versions: {row.get('version_count') or 0}")

    def _open_selected_spec_page(self, page_key: str) -> None:
        if self._selected_specification_id is None:
            messagebox.showwarning("Specification", "Select a specification first.", parent=self.root)
            return
        self.navigate(page_key)
        if page_key == "spec_changes":
            self.root.after_idle(self.refresh_change_timeline)

    def _refresh_selected_specification(self) -> None:
        if self._selected_specification_id is None:
            self.refresh_version_catalog()
            return
        row = next((item for item in self._version_catalog_rows if int(item.get("specification_id") or 0) == self._selected_specification_id), None)
        if row is not None:
            self._load_selected_specification_versions(row)

    def _load_selected_specification_versions(self, row: dict[str, Any]) -> None:
        specification_id = int(row["specification_id"])
        resolved = self._get_version_history_connection()
        if resolved is None:
            return
        database_connection, _ = resolved
        self.version_status_var.set("Loading version history...")
        generation = self._spec_context_generation
        self.version_history_thread = threading.Thread(
            target=self._version_rows_worker,
            args=(database_connection, specification_id, generation),
            daemon=True,
        )
        self.version_history_thread.start()

    def _reset_spec_catalog_filters(self) -> None:
        self.spec_catalog_search_var.set("")
        self.spec_catalog_type_var.set("All")
        self.spec_catalog_region_var.set("All")
        self.spec_catalog_status_var.set("All")
        self._apply_spec_catalog_filters()

    def _apply_spec_catalog_filters(self) -> None:
        if not hasattr(self, "version_master_tree"):
            return
        query = self.spec_catalog_search_var.get().strip().casefold()
        type_filter = self.spec_catalog_type_var.get().strip()
        region_filter = self.spec_catalog_region_var.get().strip()
        status_filter = self.spec_catalog_status_var.get().strip()
        current_selection = self._selected_specification_id
        for iid in self.version_master_tree.get_children():
            self.version_master_tree.delete(iid)
        visible = 0
        for row in self._version_catalog_rows:
            spec_type = str(row.get("spec_subtype") or row.get("spec_type") or "-")
            region = str(row.get("region_scope") or "-")
            status = str(row.get("specification_status") or "ACTIVE")
            haystack = " ".join(str(row.get(key) or "") for key in ("canonical_name", "specification_key", "spec_code", "spec_type", "spec_subtype", "region_scope")).casefold()
            if query and query not in haystack:
                continue
            if type_filter not in {"", "All"} and spec_type != type_filter:
                continue
            if region_filter not in {"", "All"} and region != region_filter:
                continue
            if status_filter not in {"", "All"} and status != status_filter:
                continue
            spec_id = int(row["specification_id"])
            updated = row.get("updated_at")
            updated_text = updated.strftime("%Y-%m-%d %H:%M") if hasattr(updated, "strftime") else str(updated or "")[:16]
            self._version_catalog_by_iid[f"spec-{spec_id}"] = row
            self.version_master_tree.insert(
                "", END, iid=f"spec-{spec_id}", values=(
                    row.get("canonical_name") or row.get("specification_key"),
                    row.get("current_version") or "-",
                    row.get("version_count") or 0,
                    spec_type,
                    region,
                    status,
                    updated_text,
                )
            )
            visible += 1
        self.spec_catalog_visible_count_var.set(f"{visible} shown / {len(self._version_catalog_rows)} total")
        if current_selection is not None:
            iid = f"spec-{current_selection}"
            if self.version_master_tree.exists(iid):
                self.version_master_tree.selection_set(iid)
                self.version_master_tree.see(iid)

    def refresh_change_timeline(self) -> None:
        if self._selected_specification_id is None:
            self.change_center_status_var.set("Select a specification first.")
            return
        if self.version_history_thread is not None and self.version_history_thread.is_alive():
            self.root.after(150, self.refresh_change_timeline)
            return
        resolved = self._get_version_history_connection()
        if resolved is None:
            return
        database_connection, _ = resolved
        specification_id = int(self._selected_specification_id)
        self.change_center_status_var.set("Loading adjacent version changes...")
        generation = self._spec_context_generation
        self.version_history_thread = threading.Thread(
            target=self._change_timeline_worker,
            args=(database_connection, specification_id, generation),
            daemon=True,
        )
        self.version_history_thread.start()

    def _change_timeline_worker(self, database_connection: DatabaseConnection, specification_id: int, generation: int) -> None:
        try:
            rows = SpecificationHistoryService(database_connection).list_change_timeline(specification_id)
            self._emit("change_timeline_result", {"ok": True, "specification_id": specification_id, "rows": rows, "generation": generation})
        except Exception as exc:
            self._emit("change_timeline_result", {"ok": False, "specification_id": specification_id, "error": str(exc), "generation": generation})

    def _handle_change_timeline_result(self, payload: dict[str, Any]) -> None:
        if int(payload.get("generation") or -1) != self._spec_context_generation:
            return
        if int(payload.get("specification_id") or 0) != int(self._selected_specification_id or 0):
            return
        if not payload.get("ok"):
            self.change_center_status_var.set("Failed to load change timeline.")
            messagebox.showerror("Change Center", str(payload.get("error") or "Unknown error"), parent=self.root)
            return
        self._change_timeline_by_iid.clear()
        self._selected_change_set_id = None
        for iid in self.change_timeline_tree.get_children():
            self.change_timeline_tree.delete(iid)
        rows = list(payload.get("rows") or [])
        for row in rows:
            change_set_id = int(row["change_set_id"])
            iid = f"changeset-{change_set_id}"
            self._change_timeline_by_iid[iid] = row
            total = sum(int(row.get(key) or 0) for key in ("added_count", "modified_count", "removed_count", "moved_count"))
            created = row.get("created_at")
            created_text = created.strftime("%Y-%m-%d %H:%M") if hasattr(created, "strftime") else str(created or "")[:16]
            self.change_timeline_tree.insert("", END, iid=iid, values=(
                f"{row.get('from_version_label')} → {row.get('to_version_label')}",
                row.get("added_count") or 0,
                row.get("modified_count") or 0,
                row.get("removed_count") or 0,
                row.get("moved_count") or 0,
                total,
                created_text,
            ))
        self.change_center_status_var.set(f"Loaded {len(rows)} adjacent version change set(s).")
        self.change_center_summary_var.set("Select a version transition to inspect detailed changes.")
        self._change_detail_rows.clear()
        self._apply_change_detail_filters()
        if rows:
            first = self.change_timeline_tree.get_children()[0]
            self.change_timeline_tree.selection_set(first)
            self.change_timeline_tree.see(first)
            self._on_change_timeline_selected()

    def _on_change_timeline_selected(self, _event: Any | None = None) -> None:
        selection = self.change_timeline_tree.selection()
        if not selection:
            return
        row = self._change_timeline_by_iid.get(selection[0])
        if row is None:
            return
        change_set_id = int(row["change_set_id"])
        self._selected_change_set_id = change_set_id
        total = sum(int(row.get(key) or 0) for key in ("added_count", "modified_count", "removed_count", "moved_count"))
        self.change_center_summary_var.set(
            f"{row.get('from_version_label')} → {row.get('to_version_label')}  |  "
            f"Added {row.get('added_count') or 0} · Modified {row.get('modified_count') or 0} · "
            f"Removed {row.get('removed_count') or 0} · Moved {row.get('moved_count') or 0} · Total {total}"
        )
        resolved = self._get_version_history_connection()
        if resolved is None:
            return
        database_connection, _ = resolved
        generation = self._spec_context_generation
        self.version_history_thread = threading.Thread(
            target=self._change_detail_worker,
            args=(database_connection, change_set_id, generation),
            daemon=True,
        )
        self.version_history_thread.start()

    def _change_detail_worker(self, database_connection: DatabaseConnection, change_set_id: int, generation: int) -> None:
        try:
            rows = SpecificationHistoryService(database_connection).list_change_details(change_set_id)
            self._emit("change_detail_result", {"ok": True, "change_set_id": change_set_id, "rows": rows, "generation": generation})
        except Exception as exc:
            self._emit("change_detail_result", {"ok": False, "change_set_id": change_set_id, "error": str(exc), "generation": generation})

    def _handle_change_detail_result(self, payload: dict[str, Any]) -> None:
        if int(payload.get("generation") or -1) != self._spec_context_generation:
            return
        if int(payload.get("change_set_id") or 0) != int(self._selected_change_set_id or 0):
            return
        if not payload.get("ok"):
            messagebox.showerror("Change Center", str(payload.get("error") or "Unknown error"), parent=self.root)
            return
        self._change_detail_rows = list(payload.get("rows") or [])
        self._apply_change_detail_filters()

    def _apply_change_detail_filters(self) -> None:
        if not hasattr(self, "change_center_tree"):
            return
        for iid in self.change_center_tree.get_children():
            self.change_center_tree.delete(iid)
        self._change_detail_by_iid.clear()
        kind = self.change_type_filter_var.get().strip().upper() or "ALL"
        self._refresh_change_filter_buttons(self._change_center_filter_buttons, kind)
        query = self.change_search_var.get().strip().casefold()
        for row in self._change_detail_rows:
            change_type = str(row.get("change_type") or "")
            if kind != "ALL" and change_type != kind:
                continue
            haystack = " ".join(str(row.get(key) or "") for key in ("chapter_title", "section_title", "entity_key", "old_text", "new_text")).casefold()
            if query and query not in haystack:
                continue
            iid = f"change-{row['id']}"
            self._change_detail_by_iid[iid] = row
            similarity = row.get("similarity_score")
            similarity_text = f"{float(similarity) * 100:.0f}%" if similarity is not None else "-"
            change_tag = f"change-{str(change_type or "").strip().lower()}"
            self.change_center_tree.insert("", END, iid=iid, values=(
                change_type or "-",
                row.get("chapter_title") or row.get("chapter_id") or "-",
                row.get("section_title") or row.get("section_id") or row.get("entity_key") or "-",
                similarity_text,
            ), tags=(change_tag,))
        self._set_readonly_text(self.change_center_old_text, "")
        self._set_readonly_text(self.change_center_new_text, "")

    def _on_change_center_detail_selected(self, _event: Any | None = None) -> None:
        selection = self.change_center_tree.selection()
        if not selection:
            return
        row = self._change_detail_by_iid.get(selection[0])
        if row is None:
            return
        self._render_text_diff(
            self.change_center_old_text,
            self.change_center_new_text,
            str(row.get("old_text") or ""),
            str(row.get("new_text") or ""),
            change_type=str(row.get("change_type") or "MODIFIED"),
        )

    # ========================================================
    # Specification Version History / Compare Page
    # ========================================================

    def _build_version_history_page(self, parent: ttk.Frame) -> None:
        """Enterprise version catalog + deterministic cross-version diff UI."""
        workspace = ttk.Frame(parent)
        workspace.pack(fill=BOTH, expand=True)
        workspace.columnconfigure(0, weight=4, uniform="version-columns")
        workspace.columnconfigure(1, weight=5, uniform="version-columns")
        workspace.columnconfigure(2, weight=7, uniform="version-columns")
        workspace.rowconfigure(0, weight=1)

        left = ttk.Frame(workspace)
        middle = ttk.Frame(workspace)
        right = ttk.Frame(workspace)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 7))
        middle.grid(row=0, column=1, sticky="nsew", padx=7)
        right.grid(row=0, column=2, sticky="nsew", padx=(7, 0))

        target = ttk.LabelFrame(
            left,
            text="1. Specification Catalog",
            padding=12,
            style="Card.TLabelframe",
            bootstyle="secondary",
        )
        target.pack(fill=BOTH, expand=True)

        target_info = ttk.Frame(target)
        target_info.pack(fill=X, pady=(0, 8))
        target_info.columnconfigure(1, weight=1)
        for row_index, (label, variable) in enumerate((
            ("Scope", self.version_target_scope_var),
            ("Database", self.version_target_database_var),
            ("Host", self.version_target_host_var),
        )):
            ttk.Label(target_info, text=f"{label}:", font=("Microsoft YaHei UI", 9, "bold")).grid(
                row=row_index, column=0, sticky="w", padx=(0, 8), pady=1
            )
            ttk.Label(target_info, textvariable=variable, bootstyle="primary", style="Small.TLabel").grid(
                row=row_index, column=1, sticky="w", pady=1
            )

        toolbar = ttk.Frame(target)
        toolbar.pack(fill=X, pady=(0, 8))
        self.version_refresh_button = ttk.Button(
            toolbar,
            text="↻  Refresh",
            command=self.refresh_version_catalog,
            bootstyle="primary",
        )
        self.version_refresh_button.pack(side=LEFT)
        ttk.Label(
            toolbar,
            textvariable=self.version_master_count_var,
            bootstyle="secondary",
            style="Small.TLabel",
        ).pack(side=RIGHT)

        catalog_container = ttk.Frame(target)
        catalog_container.pack(fill=BOTH, expand=True)
        self.version_master_tree = ttk.Treeview(
            catalog_container,
            columns=("name", "current", "versions", "type"),
            show="headings",
            selectmode="browse",
            height=24,
        )
        for column, title, width, anchor in (
            ("name", "Specification", 210, "w"),
            ("current", "Current", 75, "center"),
            ("versions", "Versions", 65, "center"),
            ("type", "Type", 80, "center"),
        ):
            self.version_master_tree.heading(column, text=title)
            self.version_master_tree.column(column, width=width, minwidth=50, anchor=anchor, stretch=(column == "name"))
        catalog_scroll = ttk.Scrollbar(catalog_container, orient=VERTICAL, command=self.version_master_tree.yview)
        self.version_master_tree.configure(yscrollcommand=catalog_scroll.set)
        self.version_master_tree.pack(side=LEFT, fill=BOTH, expand=True)
        catalog_scroll.pack(side=RIGHT, fill=Y)
        self.version_master_tree.bind("<<TreeviewSelect>>", self._on_version_master_selected)

        ttk.Label(
            target,
            textvariable=self.version_status_var,
            wraplength=360,
            justify=LEFT,
            bootstyle="secondary",
            style="Small.TLabel",
        ).pack(fill=X, pady=(8, 0))

        history = ttk.LabelFrame(
            middle,
            text="2. Version History",
            padding=12,
            style="Card.TLabelframe",
            bootstyle="secondary",
        )
        history.pack(fill=BOTH, expand=True)
        ttk.Label(
            history,
            textvariable=self.version_selected_master_var,
            font=("Microsoft YaHei UI", 10, "bold"),
            bootstyle="dark",
            wraplength=430,
        ).pack(anchor="w", pady=(0, 8))

        history_container = ttk.Frame(history)
        history_container.pack(fill=BOTH, expand=True)
        self.version_history_tree = ttk.Treeview(
            history_container,
            columns=("version", "role", "previous", "changes", "imported"),
            show="headings",
            selectmode="browse",
            height=24,
        )
        for column, title, width, anchor in (
            ("version", "Version", 80, "center"),
            ("role", "Role", 75, "center"),
            ("previous", "Previous", 80, "center"),
            ("changes", "Δ Changes", 85, "center"),
            ("imported", "Imported", 120, "center"),
        ):
            self.version_history_tree.heading(column, text=title)
            self.version_history_tree.column(column, width=width, minwidth=60, anchor=anchor, stretch=(column == "imported"))
        history_scroll = ttk.Scrollbar(history_container, orient=VERTICAL, command=self.version_history_tree.yview)
        self.version_history_tree.configure(yscrollcommand=history_scroll.set)
        self.version_history_tree.pack(side=LEFT, fill=BOTH, expand=True)
        history_scroll.pack(side=RIGHT, fill=Y)

        ttk.Label(
            history,
            text=(
                "Business versions are sorted semantically, not by import sequence. "
                "Late imports are spliced into the history chain automatically."
            ),
            wraplength=440,
            justify=LEFT,
            bootstyle="secondary",
            style="Small.TLabel",
        ).pack(anchor="w", pady=(8, 0))

        compare = ttk.LabelFrame(
            right,
            text="3. Compare Versions",
            padding=12,
            style="Card.TLabelframe",
            bootstyle="secondary",
        )
        compare.pack(fill=BOTH, expand=True)

        selectors = ttk.Frame(compare)
        selectors.pack(fill=X)
        selectors.columnconfigure(1, weight=1)
        selectors.columnconfigure(3, weight=1)
        ttk.Label(selectors, text="From", font=("Microsoft YaHei UI", 9, "bold")).grid(row=0, column=0, sticky="w", padx=(0, 6))
        self.version_compare_from_combo = ttk.Combobox(
            selectors,
            textvariable=self.version_compare_from_var,
            state="readonly",
            width=15,
        )
        self.version_compare_from_combo.grid(row=0, column=1, sticky="ew", padx=(0, 10))
        self.version_compare_from_combo.bind(
            "<<ComboboxSelected>>",
            self._on_version_compare_from_selected,
        )
        ttk.Label(selectors, text="To", font=("Microsoft YaHei UI", 9, "bold")).grid(row=0, column=2, sticky="w", padx=(0, 6))
        self.version_compare_to_combo = ttk.Combobox(
            selectors,
            textvariable=self.version_compare_to_var,
            state="readonly",
            width=15,
        )
        self.version_compare_to_combo.grid(row=0, column=3, sticky="ew", padx=(0, 10))
        self.version_compare_to_combo.bind(
            "<<ComboboxSelected>>",
            self._on_version_compare_to_selected,
        )
        self.version_compare_button = ttk.Button(
            selectors,
            text="Compare",
            command=self.start_version_compare,
            bootstyle="success",
        )
        self.version_compare_button.grid(row=0, column=4, sticky="e")

        summary = ttk.Frame(compare)
        summary.pack(fill=X, pady=(10, 8))
        for variable in (
            self.version_added_var,
            self.version_modified_var,
            self.version_removed_var,
            self.version_moved_var,
        ):
            ttk.Label(
                summary,
                textvariable=variable,
                font=("Microsoft YaHei UI", 9, "bold"),
                bootstyle="primary",
            ).pack(side=LEFT, padx=(0, 14))

        ttk.Label(
            compare,
            textvariable=self.version_compare_summary_var,
            wraplength=620,
            justify=LEFT,
            bootstyle="secondary",
            style="Small.TLabel",
        ).pack(fill=X, pady=(0, 8))

        changes_container = ttk.Frame(compare)
        changes_container.pack(fill=BOTH, expand=True)
        changes_container.rowconfigure(0, weight=3)
        changes_container.rowconfigure(1, weight=2)
        changes_container.columnconfigure(0, weight=1)

        changes_top = ttk.Frame(changes_container)
        changes_top.grid(row=0, column=0, sticky="nsew")
        self.version_change_tree = ttk.Treeview(
            changes_top,
            columns=("type", "chapter", "section", "similarity"),
            show="headings",
            selectmode="browse",
            height=11,
        )
        for column, title, width, anchor in (
            ("type", "Change", 80, "center"),
            ("chapter", "Chapter", 120, "w"),
            ("section", "Section", 230, "w"),
            ("similarity", "Match", 70, "center"),
        ):
            self.version_change_tree.heading(column, text=title)
            self.version_change_tree.column(column, width=width, minwidth=55, anchor=anchor, stretch=(column in {"chapter", "section"}))
        changes_scroll = ttk.Scrollbar(changes_top, orient=VERTICAL, command=self.version_change_tree.yview)
        self.version_change_tree.configure(yscrollcommand=changes_scroll.set)
        self.version_change_tree.pack(side=LEFT, fill=BOTH, expand=True)
        changes_scroll.pack(side=RIGHT, fill=Y)
        self.version_change_tree.bind("<<TreeviewSelect>>", self._on_version_change_selected)

        detail = ttk.Panedwindow(changes_container, orient="horizontal")
        detail.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        old_frame = ttk.LabelFrame(detail, text="Old Version", padding=6, bootstyle="secondary")
        new_frame = ttk.LabelFrame(detail, text="New Version", padding=6, bootstyle="secondary")
        detail.add(old_frame, weight=1)
        detail.add(new_frame, weight=1)
        self.version_old_text = tk.Text(old_frame, wrap=WORD, height=10, font=("Consolas", 9), state=DISABLED)
        self.version_new_text = tk.Text(new_frame, wrap=WORD, height=10, font=("Consolas", 9), state=DISABLED)
        self._configure_diff_text_widget(self.version_old_text)
        self._configure_diff_text_widget(self.version_new_text)
        self.version_old_text.pack(fill=BOTH, expand=True)
        self.version_new_text.pack(fill=BOTH, expand=True)

        footer = ttk.LabelFrame(
            right,
            text="RAG Query Policy",
            padding=10,
            style="Card.TLabelframe",
            bootstyle="secondary",
        )
        footer.pack(fill=X, pady=(10, 0))
        ttk.Label(
            footer,
            text=(
                "Default retrieval uses rag_current_chunks (Current only). "
                "History is retained for explicit version lookup and comparison."
            ),
            wraplength=620,
            justify=LEFT,
            bootstyle="secondary",
            style="Small.TLabel",
        ).pack(anchor="w")

        self._refresh_version_target_info()

    def _refresh_version_target_info(self) -> None:
        if not hasattr(self, "version_target_scope_var"):
            return
        try:
            project = ProjectRegistry.resolve(self.project_var.get())
        except ProjectRegistryError:
            self.version_target_scope_var.set("Not selected")
            self.version_target_database_var.set("Not configured")
        else:
            self.version_target_scope_var.set(project.display_name)
            self.version_target_database_var.set(self.postgres_database_var.get().strip() or project.database_default)
        host = self.postgres_host_var.get().strip() if hasattr(self, "postgres_host_var") else ""
        port = self.postgres_port_var.get().strip() if hasattr(self, "postgres_port_var") else ""
        self.version_target_host_var.set(f"{host}:{port}" if host and port else "Not configured")

    def _get_version_history_connection(self) -> tuple[DatabaseConnection, int] | None:
        if not self._ensure_postgres_password():
            return None
        settings = self._collect_postgres_settings()
        if settings is None:
            return None
        try:
            project = ProjectRegistry.resolve(self.project_var.get())
        except ProjectRegistryError:
            return None
        config = self._build_json_import_database_config(settings=settings)
        return DatabaseConnection(config), hash((project.code, settings.database))

    def refresh_version_catalog(self) -> None:
        if self.version_history_thread is not None and self.version_history_thread.is_alive():
            return
        resolved = self._get_version_history_connection()
        if resolved is None:
            return
        database_connection, _ = resolved
        self.version_status_var.set("Loading specification catalog...")
        self.version_refresh_button.configure(state=DISABLED)
        generation = self._spec_context_generation
        self.version_history_thread = threading.Thread(
            target=self._version_catalog_worker,
            args=(database_connection, generation),
            daemon=True,
        )
        self.version_history_thread.start()

    def _version_catalog_worker(self, database_connection: DatabaseConnection, generation: int) -> None:
        try:
            rows = SpecificationHistoryService(database_connection).list_specifications()
            self._emit("version_catalog_result", {"ok": True, "rows": rows, "generation": generation})
        except Exception as exc:
            self._emit("version_catalog_result", {"ok": False, "error": str(exc), "generation": generation})

    def _handle_version_catalog_result(self, payload: dict[str, Any]) -> None:
        self.version_refresh_button.configure(state=NORMAL)
        if int(payload.get("generation") or -1) != self._spec_context_generation:
            return
        if not payload.get("ok"):
            self.version_status_var.set("Failed to load specification catalog.")
            messagebox.showerror("Specification Catalog", str(payload.get("error") or "Unknown error"), parent=self.root)
            return

        self._version_catalog_by_iid.clear()
        self._version_catalog_rows = list(payload.get("rows") or [])
        self._spec_display_to_id.clear()
        self._spec_id_to_display.clear()
        for row in self._version_catalog_rows:
            spec_id = int(row["specification_id"])
            display = self._format_spec_selector_label(row)
            self._spec_display_to_id[display] = spec_id
            self._spec_id_to_display[spec_id] = display

        types = sorted({str(row.get("spec_subtype") or row.get("spec_type") or "-") for row in self._version_catalog_rows})
        regions = sorted({str(row.get("region_scope") or "-") for row in self._version_catalog_rows})
        statuses = sorted({str(row.get("specification_status") or "ACTIVE") for row in self._version_catalog_rows})
        self.spec_catalog_type_combo.configure(values=("All", *types))
        self.spec_catalog_region_combo.configure(values=("All", *regions))
        self.spec_catalog_status_combo.configure(values=("All", *statuses))
        if self.spec_catalog_type_var.get() not in ("All", *types):
            self.spec_catalog_type_var.set("All")
        if self.spec_catalog_region_var.get() not in ("All", *regions):
            self.spec_catalog_region_var.set("All")
        if self.spec_catalog_status_var.get() not in ("All", *statuses):
            self.spec_catalog_status_var.set("All")

        self._apply_spec_catalog_filters()
        self._update_spec_selectors()
        self.version_master_count_var.set(f"{len(self._version_catalog_rows)} specifications")
        self.version_status_var.set("Catalog loaded. Select a specification to inspect its lifecycle.")

        pending_row = None
        if self._pending_open_spec_key:
            pending_row = next(
                (item for item in self._version_catalog_rows if str(item.get("specification_key") or "") == self._pending_open_spec_key),
                None,
            )
        target_id = int(pending_row["specification_id"]) if pending_row is not None else self._selected_specification_id
        if target_id not in self._spec_id_to_display:
            target_id = int(self._version_catalog_rows[0]["specification_id"]) if self._version_catalog_rows else None
        if target_id is not None:
            self._select_specification_by_id(target_id)
            if pending_row is not None and self._pending_open_spec_page:
                page = self._pending_open_spec_page
                self._pending_open_spec_key = None
                self._pending_open_spec_page = None
                self.navigate(page)
                if page == "spec_changes":
                    self.root.after_idle(self.refresh_change_timeline)
        else:
            self.spec_detail_name_var.set("No specifications in the selected scope")

    def _on_version_master_selected(self, _event: Any | None = None) -> None:
        if self._suppress_spec_tree_event:
            return
        selection = self.version_master_tree.selection()
        if not selection:
            return
        row = self._version_catalog_by_iid.get(selection[0])
        if not row:
            return
        specification_id = int(row["specification_id"])
        self._selected_specification_id = specification_id
        self.spec_active_selector_var.set(self._spec_id_to_display.get(specification_id, self._format_spec_selector_label(row)))
        self._update_spec_detail(row)
        self._load_selected_specification_versions(row)

    def _version_rows_worker(self, database_connection: DatabaseConnection, specification_id: int, generation: int) -> None:
        try:
            rows = SpecificationHistoryService(database_connection).list_versions(specification_id)
            self._emit("version_rows_result", {"ok": True, "specification_id": specification_id, "rows": rows, "generation": generation})
        except Exception as exc:
            self._emit("version_rows_result", {"ok": False, "specification_id": specification_id, "error": str(exc), "generation": generation})

    def _handle_version_rows_result(self, payload: dict[str, Any]) -> None:
        if int(payload.get("generation") or -1) != self._spec_context_generation:
            return
        if int(payload.get("specification_id") or 0) != int(self._selected_specification_id or 0):
            return
        if not payload.get("ok"):
            self.version_status_var.set("Failed to load version history.")
            messagebox.showerror("Version History", str(payload.get("error") or "Unknown error"), parent=self.root)
            return

        for iid in self.version_history_tree.get_children():
            self.version_history_tree.delete(iid)
        self._version_rows_by_label.clear()
        rows = list(payload.get("rows") or [])
        for row in rows:
            label = str(row.get("version_label") or "")
            self._version_rows_by_label[label] = row
            changes = int(row.get("added_count") or 0) + int(row.get("modified_count") or 0) + int(row.get("removed_count") or 0) + int(row.get("moved_count") or 0)
            created_at = row.get("created_at")
            imported = created_at.strftime("%Y-%m-%d %H:%M") if hasattr(created_at, "strftime") else str(created_at or "")[:16]
            self.version_history_tree.insert(
                "",
                END,
                iid=f"ver-{row['specification_version_id']}",
                values=(
                    label,
                    row.get("version_role") or "HISTORY",
                    row.get("previous_version_label") or "-",
                    row.get("added_count") or 0,
                    row.get("modified_count") or 0,
                    row.get("removed_count") or 0,
                    row.get("moved_count") or 0,
                    changes,
                    imported,
                ),
            )

        # list_versions() is already ordered by semantic business version:
        # newest -> oldest.  Keep that order as the canonical UI ordering.
        labels = [str(row.get("version_label") or "") for row in rows if str(row.get("version_label") or "")]
        self._version_labels_desc = labels
        current = next((row for row in rows if row.get("is_current")), None)
        if current:
            to_label = str(current.get("version_label") or "")
            from_label = str(current.get("previous_version_label") or "")
            if from_label not in self._version_rows_by_label and len(labels) > 1:
                # labels[0] is newest/current in the normal case; labels[1] is
                # the nearest older business version.
                from_label = labels[1]
            self.version_compare_to_var.set(to_label if to_label in self._version_rows_by_label else "")
            self.version_compare_from_var.set(from_label if from_label in self._version_rows_by_label else "")
        elif len(labels) > 1:
            self.version_compare_to_var.set(labels[0])
            self.version_compare_from_var.set(labels[1])
        else:
            self.version_compare_to_var.set("")
            self.version_compare_from_var.set("")

        self._apply_version_compare_constraints()
        self._clear_version_change_display()
        self.version_status_var.set(f"Loaded {len(rows)} versions. Current is queried by default through rag_current_chunks.")
        if self._current_page_key == "spec_changes":
            self.root.after_idle(self.refresh_change_timeline)

    def _apply_version_compare_constraints(self, *, changed: str | None = None) -> None:
        """Apply forward-only constraints directly to both compare dropdowns."""
        labels = list(self._version_labels_desc)
        if len(labels) < 2:
            self.version_compare_from_var.set("")
            self.version_compare_to_var.set("")
            self.version_compare_from_combo.configure(values=())
            self.version_compare_to_combo.configure(values=())
            self.version_compare_button.configure(state=DISABLED)
            return

        index = {label: position for position, label in enumerate(labels)}
        from_label = self.version_compare_from_var.get().strip()
        to_label = self.version_compare_to_var.get().strip()

        # If a user changes one selector and the opposite selection is no longer
        # legal, move the opposite side to the nearest legal business version.
        if changed == "from" and from_label in index:
            legal_to = labels[: index[from_label]]
            if to_label not in legal_to:
                to_label = legal_to[-1] if legal_to else ""
                self.version_compare_to_var.set(to_label)
        elif changed == "to" and to_label in index:
            legal_from = labels[index[to_label] + 1 :]
            if from_label not in legal_from:
                from_label = legal_from[0] if legal_from else ""
                self.version_compare_from_var.set(from_label)

        # Repair an invalid/programmatic state as defense in depth.  The normal
        # user path cannot reach this state because the popup values are filtered.
        from_label = self.version_compare_from_var.get().strip()
        to_label = self.version_compare_to_var.get().strip()
        if (
            from_label not in index
            or to_label not in index
            or index[from_label] <= index[to_label]
        ):
            self.version_compare_to_var.set(labels[0])
            self.version_compare_from_var.set(labels[1])
            from_label = labels[1]
            to_label = labels[0]

        from_values, to_values = legal_version_compare_values(
            labels,
            from_label=from_label,
            to_label=to_label,
        )
        self.version_compare_from_combo.configure(values=from_values)
        self.version_compare_to_combo.configure(values=to_values)
        self.version_compare_button.configure(state=NORMAL)

    def _on_version_compare_from_selected(self, _event: Any | None = None) -> None:
        self._apply_version_compare_constraints(changed="from")
        self._clear_version_change_display()

    def _on_version_compare_to_selected(self, _event: Any | None = None) -> None:
        self._apply_version_compare_constraints(changed="to")
        self._clear_version_change_display()

    def start_version_compare(self) -> None:
        if self.version_history_thread is not None and self.version_history_thread.is_alive():
            return
        specification_id = self._selected_specification_id
        from_label = self.version_compare_from_var.get().strip()
        to_label = self.version_compare_to_var.get().strip()
        if specification_id is None or not from_label or not to_label:
            messagebox.showwarning("Version Compare", "Select a specification and two versions first.", parent=self.root)
            return
        if from_label == to_label:
            messagebox.showwarning("Version Compare", "From must be older than To.", parent=self.root)
            self._apply_version_compare_constraints()
            return
        from_row = self._version_rows_by_label.get(from_label)
        to_row = self._version_rows_by_label.get(to_label)
        if not from_row or not to_row:
            messagebox.showwarning("Version Compare", "Refresh the version history and select valid versions.", parent=self.root)
            return

        # Defense in depth: the dropdowns already remove illegal values, but the
        # compare command also rejects reversed/equal directions so a stale or
        # programmatically assigned value can never request a backward diff.
        if not is_forward_version_compare(
            self._version_labels_desc,
            from_label=from_label,
            to_label=to_label,
        ):
            messagebox.showwarning(
                "Version Compare",
                "Invalid version direction. From must be an older version and To must be a newer version.",
                parent=self.root,
            )
            self._apply_version_compare_constraints()
            return
        resolved = self._get_version_history_connection()
        if resolved is None:
            return
        database_connection, _ = resolved
        self.version_compare_button.configure(state=DISABLED)
        self.version_compare_summary_var.set(f"Comparing {from_label} → {to_label}...")
        generation = self._spec_context_generation
        self.version_history_thread = threading.Thread(
            target=self._version_compare_worker,
            args=(
                database_connection,
                specification_id,
                int(from_row["specification_version_id"]),
                int(to_row["specification_version_id"]),
                generation,
            ),
            daemon=True,
        )
        self.version_history_thread.start()

    def _version_compare_worker(
        self,
        database_connection: DatabaseConnection,
        specification_id: int,
        from_version_id: int,
        to_version_id: int,
        generation: int,
    ) -> None:
        try:
            summary, changes = SpecificationHistoryService(database_connection).compare_versions(
                specification_id=specification_id,
                from_version_id=from_version_id,
                to_version_id=to_version_id,
            )
            self._emit("version_compare_result", {"ok": True, "summary": summary, "changes": changes, "generation": generation})
        except Exception as exc:
            self._emit("version_compare_result", {"ok": False, "error": str(exc), "generation": generation})

    def _handle_version_compare_result(self, payload: dict[str, Any]) -> None:
        self.version_compare_button.configure(state=NORMAL)
        if int(payload.get("generation") or -1) != self._spec_context_generation:
            return
        if not payload.get("ok"):
            self.version_compare_summary_var.set("Version comparison failed.")
            messagebox.showerror("Version Compare", str(payload.get("error") or "Unknown error"), parent=self.root)
            return
        summary = dict(payload.get("summary") or {})
        changes = list(payload.get("changes") or [])
        self.version_added_var.set(f"Added {summary.get('added_count', 0)}")
        self.version_modified_var.set(f"Modified {summary.get('modified_count', 0)}")
        self.version_removed_var.set(f"Removed {summary.get('removed_count', 0)}")
        self.version_moved_var.set(f"Moved {summary.get('moved_count', 0)}")
        self.version_compare_summary_var.set(str(summary.get("summary_text") or "Comparison ready."))

        self._version_compare_changes = changes
        self._apply_version_change_filter()

    def _on_version_change_selected(self, _event: Any | None = None) -> None:
        selection = self.version_change_tree.selection()
        if not selection:
            return
        row = self._version_change_by_iid.get(selection[0])
        if not row:
            return
        self._render_text_diff(
            self.version_old_text,
            self.version_new_text,
            str(row.get("old_text") or ""),
            str(row.get("new_text") or ""),
            change_type=str(row.get("change_type") or "MODIFIED"),
        )

    @staticmethod
    def _set_readonly_text(widget: tk.Text, value: str) -> None:
        widget.configure(state=NORMAL)
        widget.delete("1.0", END)
        if value:
            widget.insert("1.0", value)
        widget.configure(state=DISABLED)

    def _clear_version_change_display(self) -> None:
        self.version_added_var.set("Added 0")
        self.version_modified_var.set("Modified 0")
        self.version_removed_var.set("Removed 0")
        self.version_moved_var.set("Moved 0")
        self.version_compare_summary_var.set("Select two versions to compare.")
        if hasattr(self, "version_change_tree"):
            for iid in self.version_change_tree.get_children():
                self.version_change_tree.delete(iid)
        self._version_change_by_iid.clear()
        self._version_compare_changes = []
        self.version_change_filter_var.set("ALL")
        self.version_change_visible_var.set("0 shown")
        self._refresh_change_filter_buttons(self._version_filter_buttons, "ALL")
        if hasattr(self, "version_old_text"):
            self._set_readonly_text(self.version_old_text, "")
            self._set_readonly_text(self.version_new_text, "")

    # ========================================================
    # JSON -> PostgreSQL Page
    # ========================================================

    def _build_json_import_page(
        self,
        parent: ttk.Frame,
    ) -> None:
        """
        Build the JSON -> PostgreSQL import page.

        This page imports JSON files generated by this application.
        It does not re-run PDF / DOCX / PPTX / PPT / XLSX / TXT / PNG / JPG parsing.
        """

        workspace = ttk.Frame(
            parent
        )

        workspace.pack(
            fill=BOTH,
            expand=True,
        )

        workspace.columnconfigure(
            0,
            weight=5,
            uniform="json-columns",
        )

        workspace.columnconfigure(
            1,
            weight=4,
            uniform="json-columns",
        )

        workspace.columnconfigure(
            2,
            weight=5,
            uniform="json-columns",
        )

        workspace.rowconfigure(
            0,
            weight=1,
        )

        left_panel = ttk.Frame(
            workspace
        )

        left_panel.grid(
            row=0,
            column=0,
            sticky="nsew",
            padx=(
                0,
                8,
            ),
        )

        center_panel = ttk.Frame(
            workspace
        )

        center_panel.grid(
            row=0,
            column=1,
            sticky="nsew",
            padx=8,
        )

        right_panel = ttk.Frame(
            workspace
        )

        right_panel.grid(
            row=0,
            column=2,
            sticky="nsew",
            padx=(
                8,
                0,
            ),
        )

        self._build_json_file_section(
            left_panel
        )

        self._build_json_import_info_section(
            center_panel
        )

        self._build_json_import_action_section(
            center_panel
        )

        self._build_json_import_progress_section(
            right_panel
        )

        self._build_json_import_log_section(
            right_panel
        )

    def _build_json_file_section(
        self,
        parent: ttk.Frame,
    ) -> None:

        frame = ttk.LabelFrame(
            parent,
            text="1. JSON Files",
            padding=14,
            style="Card.TLabelframe",
            bootstyle="secondary",
        )

        frame.pack(
            fill=BOTH,
            expand=True,
        )

        toolbar = ttk.Frame(
            frame
        )

        toolbar.pack(
            fill=X,
            pady=(
                0,
                10,
            ),
        )

        self.add_json_files_button = (
            ttk.Button(
                toolbar,
                text="＋  Add JSON",
                command=(
                    self.select_json_files
                ),
                bootstyle="primary",
            )
        )

        self.add_json_files_button.pack(
            side=LEFT
        )

        self.add_json_folder_button = ttk.Button(
            toolbar,
            text="＋  Add JSON Folder",
            command=self.select_json_folder,
            bootstyle="primary-outline",
        )
        self.add_json_folder_button.pack(side=LEFT, padx=(8, 0))

        self.remove_json_files_button = (
            ttk.Button(
                toolbar,
                text="Remove",
                command=(
                    self.remove_selected_json_files
                ),
                bootstyle="secondary-outline",
            )
        )

        self.remove_json_files_button.pack(
            side=LEFT,
            padx=(
                8,
                0,
            ),
        )

        self.clear_json_files_button = (
            ttk.Button(
                toolbar,
                text="Clear",
                command=(
                    self.clear_json_files
                ),
                bootstyle="secondary-outline",
            )
        )

        self.clear_json_files_button.pack(
            side=LEFT,
            padx=(
                8,
                0,
            ),
        )

        list_container = ttk.Frame(
            frame
        )

        list_container.pack(
            fill=BOTH,
            expand=True,
        )

        self.json_file_listbox = tk.Listbox(
            list_container,
            height=24,
            selectmode=EXTENDED,
            activestyle="none",
            font=(
                "Microsoft YaHei UI",
                10,
            ),
            borderwidth=1,
            relief="solid",
            highlightthickness=0,
            selectborderwidth=0,
        )

        scrollbar = ttk.Scrollbar(
            list_container,
            orient=VERTICAL,
            command=(
                self.json_file_listbox
                .yview
            ),
            bootstyle="round",
        )

        self.json_file_listbox.configure(
            yscrollcommand=(
                scrollbar.set
            )
        )

        self.json_file_listbox.pack(
            side=LEFT,
            fill=BOTH,
            expand=True,
        )

        scrollbar.pack(
            side=RIGHT,
            fill=Y,
        )

        footer = ttk.Frame(
            frame
        )

        footer.pack(
            fill=X,
            pady=(
                10,
                0,
            ),
        )

        ttk.Label(
            footer,
            text=(
                "Only JSON generated by "
                "Document Intelligence Console Enterprise"
            ),
            bootstyle="secondary",
            style="Small.TLabel",
        ).pack(
            side=LEFT
        )

        ttk.Label(
            footer,
            textvariable=(
                self.json_file_count_var
            ),
            bootstyle="primary",
            font=(
                "Microsoft YaHei UI",
                9,
                "bold",
            ),
        ).pack(
            side=RIGHT
        )

    def _build_json_import_info_section(
        self,
        parent: ttk.Frame,
    ) -> None:

        target_frame = ttk.LabelFrame(
            parent,
            text="2. Import Target",
            padding=14,
            style="Card.TLabelframe",
            bootstyle="secondary",
        )

        target_frame.pack(
            fill=X,
            pady=(
                0,
                12,
            ),
        )

        ttk.Label(
            target_frame,
            text="PostgreSQL",
            font=(
                "Microsoft YaHei UI",
                11,
                "bold",
            ),
            bootstyle="dark",
        ).pack(
            anchor="w"
        )

        target_grid = ttk.Frame(target_frame)
        target_grid.pack(fill=X, pady=(8, 0))
        target_grid.columnconfigure(1, weight=1)

        for row_index, (label_text, variable) in enumerate((
            ("Scope", self.json_import_target_scope_var),
            ("Database", self.json_import_target_database_var),
            ("Host", self.json_import_target_host_var),
        )):
            ttk.Label(
                target_grid,
                text=f"{label_text}:",
                font=("Microsoft YaHei UI", 9, "bold"),
            ).grid(
                row=row_index,
                column=0,
                sticky="w",
                padx=(0, 10),
                pady=2,
            )
            ttk.Label(
                target_grid,
                textvariable=variable,
                bootstyle="primary",
                style="Small.TLabel",
            ).grid(
                row=row_index,
                column=1,
                sticky="w",
                pady=2,
            )

        ttk.Label(
            target_frame,
            text=(
                "Target is controlled only by PostgreSQL Settings. "
                "JSON project_code is validated before any file is imported."
            ),
            wraplength=330,
            justify=LEFT,
            bootstyle="secondary",
            style="Small.TLabel",
        ).pack(
            anchor="w",
            pady=(8, 0),
        )

        self._refresh_json_import_target_info()

        behavior_frame = ttk.LabelFrame(
            parent,
            text="3. Import Behavior",
            padding=14,
            style="Card.TLabelframe",
            bootstyle="secondary",
        )

        behavior_frame.pack(
            fill=X,
            pady=(
                0,
                12,
            ),
        )

        ttk.Label(
            behavior_frame,
            text="What happens during import",
            font=(
                "Microsoft YaHei UI",
                9,
                "bold",
            ),
        ).pack(
            anchor="w",
            pady=(
                0,
                8,
            ),
        )

        for text in (
            "• Validate the entire selected batch before any write",
            "• Require JSON scope to match PostgreSQL Settings",
            "• Rebuild the Unified Document Model",
            "• Use the current Settings database as the only target",
            "• Update the same document_id idempotently",
            "• Contents are stored with embedding_status=PENDING",
        ):
            ttk.Label(
                behavior_frame,
                text=text,
                wraplength=330,
                justify=LEFT,
                bootstyle="secondary",
                style="Small.TLabel",
            ).pack(
                anchor="w",
                pady=(
                    2,
                    0,
                ),
            )

    def _build_json_import_action_section(
        self,
        parent: ttk.Frame,
    ) -> None:

        frame = ttk.LabelFrame(
            parent,
            text="4. Actions",
            padding=14,
            style="Card.TLabelframe",
            bootstyle="secondary",
        )

        frame.pack(
            fill=X,
        )

        self.import_json_button = (
            ttk.Button(
                frame,
                text="⇩  Import to PostgreSQL",
                command=(
                    self.start_json_import
                ),
                bootstyle="success",
            )
        )

        self.import_json_button.pack(
            fill=X,
            ipady=5,
        )

        self.cancel_json_import_button = (
            ttk.Button(
                frame,
                text="Cancel",
                command=(
                    self.cancel_json_import
                ),
                state=DISABLED,
                bootstyle="danger-outline",
            )
        )

        self.cancel_json_import_button.pack(
            fill=X,
            pady=(
                10,
                0,
            ),
        )

        ttk.Label(
            frame,
            text=(
                "Import does not parse the original Office/PDF "
                "file again."
            ),
            wraplength=330,
            justify=LEFT,
            bootstyle="secondary",
            style="Small.TLabel",
        ).pack(
            anchor="w",
            pady=(
                12,
                0,
            ),
        )

    def _build_json_import_progress_section(
        self,
        parent: ttk.Frame,
    ) -> None:

        frame = ttk.LabelFrame(
            parent,
            text="5. Import Status",
            padding=14,
            style="Card.TLabelframe",
            bootstyle="secondary",
        )

        frame.pack(
            fill=X,
            pady=(
                0,
                12,
            ),
        )

        header = ttk.Frame(
            frame
        )

        header.pack(
            fill=X
        )

        ttk.Label(
            header,
            textvariable=(
                self.json_import_status_var
            ),
            style="StatusBold.TLabel",
            bootstyle="dark",
        ).pack(
            side=LEFT
        )

        ttk.Label(
            header,
            textvariable=(
                self.json_import_progress_text_var
            ),
            bootstyle="success",
            font=(
                "Microsoft YaHei UI",
                10,
                "bold",
            ),
        ).pack(
            side=RIGHT
        )

        self.json_import_progress_bar = (
            ttk.Progressbar(
                frame,
                variable=(
                    self.json_import_progress_var
                ),
                maximum=100,
                bootstyle="success-striped",
            )
        )

        self.json_import_progress_bar.pack(
            fill=X,
            pady=(
                12,
                10,
            ),
        )

        ttk.Label(
            frame,
            textvariable=(
                self.json_import_current_file_var
            ),
            wraplength=400,
            justify=LEFT,
            bootstyle="secondary",
        ).pack(
            anchor="w",
            pady=(
                0,
                8,
            ),
        )

        ttk.Separator(
            frame,
            orient="horizontal",
            bootstyle="secondary",
        ).pack(
            fill=X,
            pady=(
                2,
                8,
            ),
        )

        ttk.Label(
            frame,
            textvariable=(
                self.json_import_summary_var
            ),
            bootstyle="secondary",
            style="Small.TLabel",
        ).pack(
            anchor="w"
        )

    def _build_json_import_log_section(
        self,
        parent: ttk.Frame,
    ) -> None:

        frame = ttk.LabelFrame(
            parent,
            text="Import Log",
            padding=14,
            style="Card.TLabelframe",
            bootstyle="secondary",
        )

        frame.pack(
            fill=BOTH,
            expand=True,
        )

        text_container = ttk.Frame(
            frame
        )

        text_container.pack(
            fill=BOTH,
            expand=True,
        )

        self.json_import_log_text = tk.Text(
            text_container,
            state=DISABLED,
            wrap=WORD,
            height=20,
            font=(
                "Cascadia Mono",
                9,
            ),
            borderwidth=1,
            relief="solid",
            highlightthickness=0,
        )

        scrollbar = ttk.Scrollbar(
            text_container,
            orient=VERTICAL,
            command=(
                self.json_import_log_text
                .yview
            ),
            bootstyle="round",
        )

        self.json_import_log_text.configure(
            yscrollcommand=(
                scrollbar.set
            )
        )

        self.json_import_log_text.pack(
            side=LEFT,
            fill=BOTH,
            expand=True,
        )

        scrollbar.pack(
            side=RIGHT,
            fill=Y,
        )

    # ========================================================
    # JSON File Selection
    # ========================================================

    def select_json_files(
        self,
    ) -> None:

        if self.is_processing:
            return

        raw_files = (
            filedialog.askopenfilenames(
                title=(
                    "Select Document Ingestion JSON"
                ),
                filetypes=JSON_FILE_TYPES,
            )
        )

        if not raw_files:
            return

        existing_paths = {
            str(
                path.resolve()
            ).lower()
            for path
            in self.selected_json_files
        }

        added = 0

        for raw_file in raw_files:
            path = Path(
                raw_file
            ).resolve()

            if path.suffix.lower() != ".json":
                continue

            normalized = str(
                path
            ).lower()

            if normalized in existing_paths:
                continue

            self.selected_json_files.append(
                path
            )

            self.json_file_listbox.insert(
                END,
                str(
                    path
                ),
            )

            existing_paths.add(
                normalized
            )

            added += 1

        self._update_json_file_count()

        if added == 0:
            LOGGER.info(
                "No new JSON files added."
            )

    def select_json_folder(self) -> None:
        """Recursively add all JSON files from a folder to the SQL import batch."""
        if self.is_processing:
            return
        selected = filedialog.askdirectory(title="Select JSON Folder")
        if not selected:
            return
        root = Path(selected).expanduser().resolve()
        existing_paths = {str(path.resolve()).lower() for path in self.selected_json_files}
        candidates = sorted(
            (
                path.resolve()
                for path in root.rglob("*.json")
                if path.is_file() and not path.name.startswith(".")
            ),
            key=lambda path: str(path).casefold(),
        )
        added = 0
        for path in candidates:
            normalized = str(path).lower()
            if normalized in existing_paths:
                continue
            self.selected_json_files.append(path)
            self.json_file_listbox.insert(END, str(path))
            existing_paths.add(normalized)
            added += 1
        self._update_json_file_count()
        self._append_json_import_log(
            f"FOLDER  | {root} | json_found={len(candidates)} | added={added} | recursive=yes"
        )
        if not candidates:
            messagebox.showinfo("JSON Folder Import", "No JSON files were found in the selected folder.")

    def remove_selected_json_files(
        self,
    ) -> None:

        if self.is_processing:
            return

        indexes = list(
            self.json_file_listbox
            .curselection()
        )

        for index in reversed(
            indexes
        ):
            self.json_file_listbox.delete(
                index
            )

            del self.selected_json_files[
                index
            ]

        self._update_json_file_count()

    def clear_json_files(
        self,
    ) -> None:

        if self.is_processing:
            return

        self.selected_json_files.clear()

        self.json_file_listbox.delete(
            0,
            END,
        )

        self._update_json_file_count()

    def _update_json_file_count(
        self,
    ) -> None:

        count = len(
            self.selected_json_files
        )

        text = (
            f"{count} JSON file selected"
            if count == 1
            else (
                f"{count} JSON files selected"
            )
        )

        self.json_file_count_var.set(
            text
        )

    # ========================================================
    # JSON -> Document
    # ========================================================

    @staticmethod
    def _load_document_from_json(
        json_path: Path,
    ) -> Document:
        """
        Rebuild a Document from JSON generated by JsonBuilder.

        JsonBuilder stores:
            document
            metadata
            chapters
            sections
            contents

        pages / raw blocks are not required by PostgresStorage,
        therefore they are intentionally reconstructed as empty lists.
        """

        try:
            with json_path.open(
                "r",
                encoding="utf-8",
            ) as file:
                payload = json.load(
                    file
                )

        except json.JSONDecodeError as exc:
            raise ValueError(
                "Invalid JSON syntax: "
                f"{exc}"
            ) from exc

        except OSError as exc:
            raise ValueError(
                "Unable to read JSON file: "
                f"{exc}"
            ) from exc

        if not isinstance(
            payload,
            dict,
        ):
            raise ValueError(
                "JSON root must be an object."
            )

        document_data = payload.get(
            "document"
        )

        if not isinstance(
            document_data,
            dict,
        ):
            raise ValueError(
                "Missing or invalid 'document' object."
            )

        file_name = document_data.get(
            "file_name"
        )

        file_type = document_data.get(
            "file_type"
        )

        document_id = document_data.get(
            "document_id"
        )

        if (
            not isinstance(
                file_name,
                str,
            )
            or not file_name.strip()
        ):
            raise ValueError(
                "document.file_name is missing."
            )

        if (
            not isinstance(
                file_type,
                str,
            )
            or not file_type.strip()
        ):
            raise ValueError(
                "document.file_type is missing."
            )

        metadata = payload.get(
            "metadata",
            {},
        )

        if metadata is None:
            metadata = {}

        if not isinstance(
            metadata,
            dict,
        ):
            raise ValueError(
                "'metadata' must be an object."
            )

        # Work on a new dictionary so the parsed JSON is never mutated.
        metadata = dict(
            metadata
        )

        if (
            isinstance(
                document_id,
                str,
            )
            and document_id.strip()
        ):
            metadata[
                "document_id"
            ] = document_id.strip()

        for field_name in (
            "project_code",
            "project_name",
            "project_assignment_source",
            "series",
            "region_scope",
            "spec_type",
            "spec_subtype",
            "specification_code",
            "specification_key",
            "specification_name",
            "specification_version",
            "specification_version_status",
        ):
            value = document_data.get(field_name)
            if value is not None:
                metadata[field_name] = value

        chapters_data = payload.get(
            "chapters",
            [],
        )

        sections_data = payload.get(
            "sections",
            [],
        )

        contents_data = payload.get(
            "contents",
            [],
        )

        for key, value in (
            (
                "chapters",
                chapters_data,
            ),
            (
                "sections",
                sections_data,
            ),
            (
                "contents",
                contents_data,
            ),
        ):
            if not isinstance(
                value,
                list,
            ):
                raise ValueError(
                    f"'{key}' must be an array."
                )

        chapters = [
            Chapter.model_validate(
                record
            )
            for record
            in chapters_data
        ]

        sections = [
            Section.model_validate(
                record
            )
            for record
            in sections_data
        ]

        contents = [
            Content.model_validate(
                record
            )
            for record
            in contents_data
        ]

        if not contents:
            raise ValueError(
                "JSON contains no contents."
            )

        return Document(
            file_name=(
                file_name.strip()
            ),
            file_type=(
                file_type.strip().lower()
            ),
            pages=[],
            blocks=[],
            chapters=chapters,
            sections=sections,
            contents=contents,
            metadata=metadata,
        )

    # ========================================================
    # JSON Import Target / Preflight
    # ========================================================

    def _refresh_json_import_target_info(self) -> None:
        """Refresh the read-only target shown on JSON -> PostgreSQL."""
        if not hasattr(self, "json_import_target_scope_var"):
            return

        try:
            project = ProjectRegistry.resolve(self.project_var.get())
        except (ProjectRegistryError, AttributeError):
            self.json_import_target_scope_var.set("Not selected")
            self.json_import_target_database_var.set("Not configured")
        else:
            self.json_import_target_scope_var.set(project.display_name)
            database_name = self.postgres_database_var.get().strip()
            self.json_import_target_database_var.set(
                database_name or "Not configured"
            )

        host = self.postgres_host_var.get().strip() if hasattr(
            self, "postgres_host_var"
        ) else ""
        port = self.postgres_port_var.get().strip() if hasattr(
            self, "postgres_port_var"
        ) else ""
        if host and port:
            self.json_import_target_host_var.set(f"{host}:{port}")
        elif host:
            self.json_import_target_host_var.set(host)
        else:
            self.json_import_target_host_var.set("Not configured")

    @staticmethod
    def _extract_json_project_code(json_path: Path) -> str | None:
        """Read only project assignment metadata for JSON batch preflight."""
        try:
            with json_path.open("r", encoding="utf-8") as file:
                payload = json.load(file)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Invalid JSON syntax: {exc.msg} "
                f"(line {exc.lineno}, column {exc.colno})."
            ) from exc
        except OSError as exc:
            raise ValueError(f"Unable to read JSON file: {exc}") from exc

        if not isinstance(payload, dict):
            raise ValueError("JSON root must be an object.")

        document_data = payload.get("document")
        if isinstance(document_data, dict):
            value = document_data.get("project_code")
            if isinstance(value, str) and value.strip():
                return value.strip()

        metadata = payload.get("metadata")
        if isinstance(metadata, dict):
            value = metadata.get("project_code")
            if isinstance(value, str) and value.strip():
                return value.strip()

        return None

    def _validate_json_import_scope_preflight(
        self,
        *,
        files: list[Path],
        target_project_code: str,
    ) -> list[str]:
        """Validate the whole selected batch before asking PostgreSQL to write."""
        target = ProjectRegistry.resolve(target_project_code)
        problems: list[str] = []

        for json_path in files:
            try:
                raw_project_code = self._extract_json_project_code(json_path)
                if raw_project_code is None:
                    if target.code == "COMMON":
                        # Legacy generic JSON from pre-project versions is allowed
                        # only in Common mode.  The worker assigns COMMON before
                        # the storage layer sees the document.
                        continue
                    raise ValueError(
                        "project_code is missing; project-aware JSON is required "
                        f"for target {target.display_name}."
                    )

                actual = ProjectRegistry.resolve(raw_project_code)
                if actual.code != target.code:
                    raise ValueError(
                        f"JSON scope is {actual.display_name}, but current target "
                        f"is {target.display_name}."
                    )
            except Exception as exc:
                problems.append(f"{json_path.name}: {exc}")

        return problems

    @staticmethod
    def _apply_common_legacy_assignment(document: Document) -> None:
        """Assign legacy project-less JSON to Common, and only to Common."""
        metadata = document.metadata
        if metadata.get("project_code"):
            return
        metadata["project_code"] = "COMMON"
        metadata.setdefault("project_name", "Common")
        metadata.setdefault(
            "project_assignment_source",
            "JSON_IMPORT_COMMON_FALLBACK",
        )

    def _build_json_import_database_config(
        self,
        *,
        settings: PostgreSQLSettings,
    ) -> DatabaseConfig:
        """Build an explicit connection config from the current Settings fields."""
        password_env = "POSTGRES_PASSWORD"
        return DatabaseConfig(
            enabled=True,
            host=settings.host,
            port=settings.port,
            database=settings.database,
            project_databases=dict(self._project_database_names),
            user=settings.user,
            password=os.getenv(password_env),
            password_env=password_env,
            connect_timeout=settings.connect_timeout,
        )

    # ========================================================
    # JSON Import Start / Worker
    # ========================================================

    def start_json_import(
        self,
    ) -> None:

        if self.is_processing:
            return

        if not self.selected_json_files:
            messagebox.showwarning(
                "No JSON Files",
                "Please select at least one JSON file.",
            )
            return

        missing_files = [
            path for path in self.selected_json_files if not path.is_file()
        ]
        if missing_files:
            messagebox.showerror(
                "Missing JSON Files",
                "Some selected JSON files no longer exist:\n\n"
                + "\n".join(str(path) for path in missing_files[:10]),
            )
            return

        try:
            target_project = ProjectRegistry.resolve(self.project_var.get())
        except ProjectRegistryError:
            messagebox.showwarning(
                "JSON Import Target",
                "Select 21MM, 24MM, or Common in PostgreSQL Settings first.",
                parent=self.root,
            )
            return

        settings = self._collect_postgres_settings()
        if settings is None:
            return

        files = list(self.selected_json_files)
        preflight_problems = self._validate_json_import_scope_preflight(
            files=files,
            target_project_code=target_project.code,
        )
        if preflight_problems:
            lines = [
                "JSON batch validation failed.",
                "",
                f"Target scope: {target_project.display_name}",
                f"Target database: {settings.database}",
                "",
                "No files were imported.",
                "",
                "Problems:",
            ]
            lines.extend(f"- {problem}" for problem in preflight_problems[:10])
            if len(preflight_problems) > 10:
                lines.append(f"- ... and {len(preflight_problems) - 10} more")
            messagebox.showerror(
                "JSON Import Preflight Failed",
                "\n".join(lines),
                parent=self.root,
            )
            return

        if not self._ensure_postgres_password():
            return

        database_config = self._build_json_import_database_config(
            settings=settings
        )

        self.is_processing = True
        self.cancel_requested.clear()
        self.json_import_failures.clear()
        self._set_controls_enabled(False)
        self.cancel_json_import_button.configure(state=NORMAL)
        self.json_import_progress_var.set(0)
        self.json_import_progress_text_var.set("0%")
        self.json_import_status_var.set("Importing")
        self.json_import_current_file_var.set("Validating JSON batch...")
        self.json_import_summary_var.set(
            f"Success: 0    Warnings: 0    Failed: 0    Total: {len(files)}"
        )
        self._clear_json_import_log()

        LOGGER.info(
            "JSON import started | total=%s | target_scope=%s | "
            "target_database=%s",
            len(files),
            target_project.code,
            settings.database,
        )

        self.json_import_thread = threading.Thread(
            target=self._import_json_files,
            kwargs={
                "files": files,
                "target_project_code": target_project.code,
                "database_config": database_config,
            },
            daemon=True,
            name="json-postgres-import-worker",
        )
        self.json_import_thread.start()

    def _import_json_files(
        self,
        *,
        files: list[Path],
        target_project_code: str,
        database_config: DatabaseConfig,
    ) -> None:
        """Two-phase JSON import: validate the entire batch, then write it."""
        target_project = ProjectRegistry.resolve(target_project_code)
        total = len(files)
        cancelled = False
        validated: list[tuple[Path, Document]] = []
        validation_failures: list[FileFailure] = []

        # Phase 1: full JSON/model/scope validation.  No PostgreSQL writes occur
        # until every selected file passes this phase.
        for index, json_path in enumerate(files, start=1):
            if self.cancel_requested.is_set():
                cancelled = True
                break

            self._emit(
                "json_import_current_file",
                f"Validating {index}/{total} {json_path.name}",
            )
            self._emit("json_import_log", f"CHECK   | {json_path.name}")

            try:
                document = self._load_document_from_json(json_path)
                project_code = document.metadata.get("project_code")

                if not project_code:
                    if target_project.code != "COMMON":
                        raise ValueError(
                            "JSON does not contain document.project_code. "
                            "Project-aware JSON is required for 21MM / 24MM."
                        )
                    self._apply_common_legacy_assignment(document)
                    project_code = "COMMON"
                    self._emit(
                        "json_import_log",
                        f"LEGACY  | {json_path.name} | assigned to Common",
                    )

                actual_project = ProjectRegistry.resolve(str(project_code))
                if actual_project.code != target_project.code:
                    raise ValueError(
                        f"JSON scope is {actual_project.display_name}, but "
                        f"current target is {target_project.display_name}."
                    )

                validated.append((json_path, document))

            except Exception as exc:
                failure = FileFailure(
                    file_path=json_path,
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )
                validation_failures.append(failure)
                self._emit(
                    "json_import_log",
                    f"FAILED  | {json_path.name} | "
                    f"{failure.error_type}: {failure.error_message}",
                )

        if cancelled:
            self._emit(
                "json_import_finished",
                BatchSummary(success=0, failed=0, total=total, cancelled=True),
            )
            return

        if validation_failures:
            self.json_import_failures.extend(validation_failures)
            self._emit(
                "json_import_log",
                "ABORT   | Batch validation failed; no PostgreSQL writes were made.",
            )
            self._emit(
                "json_import_finished",
                BatchSummary(success=0, failed=total, total=total, cancelled=False),
            )
            return

        # Phase 2: all files have a compatible target identity.  Use one
        # explicit DatabaseConnection built from the current Settings fields,
        # rather than routing independently from each JSON document.
        success_count = 0
        failed_count = 0
        warning_count = 0

        for index, (json_path, document) in enumerate(validated, start=1):
            if self.cancel_requested.is_set():
                cancelled = True
                break

            self._emit(
                "json_import_current_file",
                f"{index}/{total} {json_path.name}",
            )
            self._emit("json_import_log", f"START   | {json_path.name}")

            try:
                storage = PostgresStorage(
                    database_connection=DatabaseConnection(
                        config=database_config
                    ),
                    project_code=target_project.code,
                )
                storage.save(document)
                success_count += 1
                json_metadata = dict(getattr(document, "metadata", None) or {})
                json_warning_codes = [str(code) for code in (json_metadata.get("import_warning_codes") or []) if str(code).strip()]
                if json_warning_codes or str(json_metadata.get("import_status") or "").upper() in {"IMPORTED_WITH_WARNING", "IMPORTED_SANITIZED"}:
                    warning_count += 1

                success_message = (
                    "SUCCESS | "
                    f"{json_path.name} | "
                    f"scope={target_project.display_name} | "
                    f"database={database_config.database} | "
                    f"chapters={len(document.chapters)} | "
                    f"sections={len(document.sections)} | "
                    f"contents={len(document.contents)}"
                )
                self._emit("json_import_log", success_message)
                LOGGER.info(
                    "JSON import succeeded | file=%s | target_scope=%s | "
                    "target_database=%s | chapters=%s | sections=%s | contents=%s",
                    json_path,
                    target_project.code,
                    database_config.database,
                    len(document.chapters),
                    len(document.sections),
                    len(document.contents),
                )

            except Exception as exc:
                failed_count += 1
                failure = FileFailure(
                    file_path=json_path,
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )
                self.json_import_failures.append(failure)
                self._emit(
                    "json_import_log",
                    f"FAILED  | {json_path.name} | "
                    f"{failure.error_type}: {failure.error_message}",
                )
                LOGGER.error(
                    "JSON import failed | file=%s | error_type=%s | error=%s\n%s",
                    json_path,
                    failure.error_type,
                    failure.error_message,
                    traceback.format_exc(),
                )

            percentage = int(index / total * 100)
            self._emit("json_import_progress", percentage)
            self._emit(
                "json_import_summary",
                BatchSummary(
                    success=success_count,
                    failed=failed_count,
                    total=total,
                    warnings=warning_count,
                    cancelled=False,
                ),
            )

        self._emit(
            "json_import_finished",
            BatchSummary(
                success=success_count,
                failed=failed_count,
                total=total,
                warnings=warning_count,
                cancelled=cancelled,
            ),
        )

    def cancel_json_import(
        self,
    ) -> None:

        if not self.is_processing:
            return

        if self.cancel_requested.is_set():
            return

        confirmed = messagebox.askyesno(
            "Cancel JSON Import",
            (
                "Stop after the current "
                "JSON file finishes?"
            ),
        )

        if not confirmed:
            return

        self.cancel_requested.set()

        self.cancel_json_import_button.configure(
            state=DISABLED
        )

        self.json_import_status_var.set(
            "Cancelling..."
        )

        self._append_json_import_log(
            (
                "CANCEL  | "
                "Cancellation requested."
            )
        )

    # ========================================================
    # JSON Import Log / Summary / Finish
    # ========================================================

    def _append_json_import_log(
        self,
        message: str,
    ) -> None:

        self.json_import_log_text.configure(
            state=NORMAL
        )

        self.json_import_log_text.insert(
            END,
            message + "\n",
        )

        self.json_import_log_text.see(
            END
        )

        self.json_import_log_text.configure(
            state=DISABLED
        )

    def _clear_json_import_log(
        self,
    ) -> None:

        self.json_import_log_text.configure(
            state=NORMAL
        )

        self.json_import_log_text.delete(
            "1.0",
            END,
        )

        self.json_import_log_text.configure(
            state=DISABLED
        )

    def _update_json_import_summary(
        self,
        summary: BatchSummary,
    ) -> None:

        self.json_import_summary_var.set(
            (
                f"Success: "
                f"{summary.success}    "
                f"Warnings: "
                f"{summary.warnings}    "
                f"Failed: "
                f"{summary.failed}    "
                f"Total: "
                f"{summary.total}"
            )
        )

    def _handle_json_import_finished(
        self,
        summary: BatchSummary,
    ) -> None:

        self.is_processing = False
        self.cancel_requested.clear()

        self._set_controls_enabled(
            True
        )

        self.cancel_json_import_button.configure(
            state=DISABLED
        )

        self._update_json_import_summary(
            summary
        )

        if summary.cancelled:

            self.json_import_status_var.set(
                "Cancelled"
            )

            self.json_import_current_file_var.set(
                "JSON import was cancelled."
            )

            self._append_json_import_log(
                (
                    "BATCH   | "
                    "JSON import cancelled."
                )
            )

            messagebox.showwarning(
                "JSON Import Cancelled",
                (
                    "JSON import was cancelled."
                ),
            )

            return

        self.json_import_progress_var.set(
            100
        )

        self.json_import_progress_text_var.set(
            "100%"
        )

        if summary.failed == 0:

            self.json_import_status_var.set(
                "Completed with Warnings" if summary.warnings else "Completed"
            )

            self.json_import_current_file_var.set(
                (
                    "JSON files were imported to PostgreSQL."
                )
            )

            self._append_json_import_log(
                (
                    "BATCH   | "
                    f"success={summary.success} | "
                    f"warnings={summary.warnings} | "
                    "failed=0 | "
                    f"total={summary.total}"
                )
            )

            message = (
                "JSON import completed.\n\n"
                f"Success: {summary.success}\n"
                f"Warnings: {summary.warnings}\n"
                "Failed: 0\n"
                f"Total: {summary.total}"
            )
            if summary.warnings:
                messagebox.showwarning("JSON Import Completed with Warnings", message)
            else:
                messagebox.showinfo("JSON Import Completed", message)

            return

        self.json_import_status_var.set(
            "Completed with Errors"
        )

        self.json_import_current_file_var.set(
            (
                "JSON import completed "
                "with errors."
            )
        )

        lines = [
            "JSON import completed with errors.",
            "",
            f"Success: {summary.success}",
            f"Warnings: {summary.warnings}",
            f"Failed: {summary.failed}",
            f"Total: {summary.total}",
            "",
            "Failed JSON files:",
        ]

        for index, failure in enumerate(
            self.json_import_failures[:5],
            start=1,
        ):
            lines.extend(
                [
                    "",
                    (
                        f"{index}. "
                        f"{failure.file_path.name}"
                    ),
                    (
                        "   "
                        f"{failure.error_type}: "
                        f"{failure.error_message}"
                    ),
                ]
            )

        lines.extend(
            [
                "",
                "Full exception details:",
                str(
                    LOG_DIR
                    / "gui.log"
                ),
            ]
        )

        messagebox.showwarning(
            "JSON Import Completed with Errors",
            "\n".join(
                lines
            ),
        )

    def _is_importable_path(self, path: Path) -> bool:
        name = path.name
        if not path.is_file():
            return False
        if name.startswith("~$") or name.startswith("."):
            return False
        try:
            project = ProjectRegistry.resolve(self.project_var.get())
        except ProjectRegistryError:
            project = None
        if project is not None and project.code == "QABUG":
            return path.suffix.lower() in {".xlsx", ".xlsm", ".csv", ".tsv", ".txt"}
        return path.suffix.lower() in SUPPORTED_SUFFIXES

    def _add_document_paths(self, raw_paths: list[str | Path]) -> tuple[int, list[str]]:
        existing_paths = {str(path.resolve()).lower() for path in self.selected_files}
        added = 0
        unsupported: list[str] = []
        for raw_path in raw_paths:
            path = Path(raw_path).expanduser().resolve()
            if not self._is_importable_path(path):
                if path.is_file() and not path.name.startswith("~$"):
                    unsupported.append(path.name)
                continue
            normalized = str(path).lower()
            if normalized in existing_paths:
                continue
            self.selected_files.append(path)
            self.file_listbox.insert(END, str(path))
            existing_paths.add(normalized)
            added += 1
        self._update_file_count()
        return added, unsupported

    def select_files(self) -> None:
        if self.is_processing:
            return
        try:
            project = ProjectRegistry.resolve(self.project_var.get())
        except ProjectRegistryError:
            project = None
        if project is not None and project.code == "QABUG":
            title = "Select QA/BUG Lists"
            filetypes = [
                ("QA/BUG Lists", "*.xlsx *.xlsm *.csv *.tsv *.txt"),
                ("Excel", "*.xlsx *.xlsm"),
                ("Text / CSV", "*.txt *.tsv *.csv"),
                ("All Files", "*.*"),
            ]
        else:
            title = "Select Documents"
            filetypes = SUPPORTED_FILE_TYPES
        raw_files = filedialog.askopenfilenames(
            title=title,
            filetypes=filetypes,
        )
        if not raw_files:
            return
        added, unsupported = self._add_document_paths(list(raw_files))
        if unsupported:
            messagebox.showwarning(
                "Unsupported Files",
                "The following files were ignored:\n\n" + "\n".join(unsupported[:10]),
            )
        if added == 0:
            LOGGER.info("No new input files added.")

    def select_folder_files(self) -> None:
        """Recursively add every supported document under one folder."""
        if self.is_processing:
            return
        selected = filedialog.askdirectory(title="Select Document Folder")
        if not selected:
            return
        root = Path(selected).expanduser().resolve()
        candidates = sorted(
            (path for path in root.rglob("*") if self._is_importable_path(path)),
            key=lambda path: str(path).casefold(),
        )
        added, _unsupported = self._add_document_paths(candidates)
        self._append_log(
            f"FOLDER  | {root} | found={len(candidates)} | added={added} | recursive=yes"
        )
        if not candidates:
            messagebox.showinfo(
                "Folder Import",
                "No supported document files were found in the selected folder.",
            )
        elif added == 0:
            messagebox.showinfo(
                "Folder Import",
                f"Found {len(candidates)} supported files, but all are already in the batch.",
            )

    def remove_selected_files(
        self,
    ) -> None:

        if self.is_processing:
            return

        indexes = list(
            self.file_listbox
            .curselection()
        )

        for index in reversed(
            indexes
        ):

            self.file_listbox.delete(
                index
            )

            del self.selected_files[
                index
            ]

        self._update_file_count()

    def clear_files(
        self,
    ) -> None:

        if self.is_processing:
            return

        self.selected_files.clear()

        self.file_listbox.delete(
            0,
            END,
        )

        self._update_file_count()

    def _update_file_count(
        self,
    ) -> None:

        count = len(
            self.selected_files
        )

        text = (
            f"{count} file selected"
            if count == 1
            else (
                f"{count} files selected"
            )
        )

        self.file_count_var.set(
            text
        )

    # ========================================================
    # Output Directory
    # ========================================================

    def select_output_directory(
        self,
    ) -> None:

        if self.is_processing:
            return

        current_value = (
            self.output_directory_var
            .get()
            .strip()
        )

        initial_dir = None

        if current_value:

            current_path = Path(
                current_value
            )

            if current_path.exists():
                initial_dir = str(
                    current_path
                )

        selected = (
            filedialog
            .askdirectory(
                title=(
                    "Select Output Directory"
                ),
                initialdir=(
                    initial_dir
                ),
            )
        )

        if not selected:
            return

        self.output_directory_var.set(
            selected
        )

    # ========================================================
    # PostgreSQL Credentials
    # ========================================================

    def _ensure_postgres_password(self) -> bool:
        """
        Ensure POSTGRES_PASSWORD is available for this process.

        If the environment variable already exists, no dialog is shown.
        Otherwise a masked GUI dialog requests the password. The password
        is never written to the application log or configuration file.
        """

        existing_password = os.getenv(
            "POSTGRES_PASSWORD"
        )

        if existing_password:
            return True

        result = self._show_postgres_password_dialog()

        if result is None:
            return False

        password, remember = result

        # Make the credential immediately available to the current EXE /
        # Python process. This is required even when it is persisted below.
        os.environ["POSTGRES_PASSWORD"] = password

        if remember:
            try:
                self._persist_postgres_password(
                    password
                )
            except OSError as exc:
                LOGGER.warning(
                    "Unable to persist PostgreSQL password: %s",
                    exc,
                )

                messagebox.showwarning(
                    "PostgreSQL Password",
                    (
                        "The password will be used for this session, "
                        "but Windows could not save it for future runs.\n\n"
                        f"Reason: {exc}"
                    ),
                    parent=self.root,
                )

        if hasattr(
            self,
            "postgres_password_status_label",
        ):
            self._refresh_postgres_password_status()

        return True

    def _show_postgres_password_dialog(
        self,
    ) -> tuple[str, bool] | None:
        """
        Display a modal masked PostgreSQL password dialog.

        Returns:
            (password, remember) when confirmed.
            None when cancelled.
        """

        dialog = tk.Toplevel(
            self.root
        )

        dialog.title(
            "PostgreSQL Password"
        )

        dialog.resizable(
            False,
            False,
        )

        dialog.transient(
            self.root
        )

        password_var = tk.StringVar()
        remember_var = tk.BooleanVar(
            value=False
        )

        result: dict[
            str,
            tuple[str, bool] | None,
        ] = {
            "value": None
        }

        frame = ttk.Frame(
            dialog,
            padding=20,
        )

        frame.pack(
            fill=BOTH,
            expand=True,
        )

        ttk.Label(
            frame,
            text=(
                "PostgreSQL output is enabled.\n"
                "Enter the PostgreSQL password."
            ),
        ).pack(
            anchor="w",
            pady=(0, 12),
        )

        ttk.Label(
            frame,
            text="Password",
        ).pack(
            anchor="w"
        )

        password_entry = ttk.Entry(
            frame,
            textvariable=password_var,
            show="*",
            width=42,
        )

        password_entry.pack(
            fill=X,
            pady=(4, 12),
        )

        ttk.Checkbutton(
            frame,
            text="Remember on this computer",
            variable=remember_var,
        ).pack(
            anchor="w"
        )

        button_frame = ttk.Frame(
            frame
        )

        button_frame.pack(
            fill=X,
            pady=(18, 0),
        )

        def cancel() -> None:
            result["value"] = None
            dialog.destroy()

        def confirm() -> None:
            password = password_var.get()

            if not password:
                messagebox.showwarning(
                    "PostgreSQL Password",
                    "Please enter the PostgreSQL password.",
                    parent=dialog,
                )
                password_entry.focus_set()
                return

            result["value"] = (
                password,
                bool(remember_var.get()),
            )

            dialog.destroy()

        ttk.Button(
            button_frame,
            text="Cancel",
            command=cancel,
        ).pack(
            side=RIGHT,
        )

        ttk.Button(
            button_frame,
            text="OK",
            command=confirm,
        ).pack(
            side=RIGHT,
            padx=(0, 10),
        )

        dialog.protocol(
            "WM_DELETE_WINDOW",
            cancel,
        )

        dialog.bind(
            "<Return>",
            lambda _event: confirm(),
        )

        dialog.bind(
            "<Escape>",
            lambda _event: cancel(),
        )

        dialog.update_idletasks()

        x = (
            self.root.winfo_rootx()
            + max(
                0,
                (
                    self.root.winfo_width()
                    - dialog.winfo_width()
                ) // 2,
            )
        )

        y = (
            self.root.winfo_rooty()
            + max(
                0,
                (
                    self.root.winfo_height()
                    - dialog.winfo_height()
                ) // 2,
            )
        )

        dialog.geometry(
            f"+{x}+{y}"
        )

        dialog.grab_set()
        password_entry.focus_set()
        self.root.wait_window(
            dialog
        )

        return result["value"]

    @staticmethod
    def _persist_postgres_password(
        password: str,
    ) -> None:
        """
        Persist POSTGRES_PASSWORD in the current Windows user's environment.

        The value is stored under HKCU\\Environment, so administrator
        privileges are not required.
        """

        if os.name != "nt":
            raise OSError(
                "Remember password is supported only on Windows."
            )

        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            "Environment",
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            winreg.SetValueEx(
                key,
                "POSTGRES_PASSWORD",
                0,
                winreg.REG_SZ,
                password,
            )

        # Notify the Windows shell that the user environment changed so
        # subsequently launched processes can receive the new value.
        try:
            import ctypes

            HWND_BROADCAST = 0xFFFF
            WM_SETTINGCHANGE = 0x001A
            SMTO_ABORTIFHUNG = 0x0002

            ctypes.windll.user32.SendMessageTimeoutW(
                HWND_BROADCAST,
                WM_SETTINGCHANGE,
                0,
                "Environment",
                SMTO_ABORTIFHUNG,
                5000,
                None,
            )
        except Exception:
            # Persistence already succeeded; notification failure is not
            # fatal for the current process.
            pass

    # ========================================================
    # Validation
    # ========================================================

    def _validate_before_start(
        self,
    ) -> ProcessingOptions | None:

        if not self.selected_files:

            messagebox.showwarning(
                "No Input Files",
                (
                    "Please select at least "
                    "one document."
                ),
            )

            return None

        missing_files = [
            path
            for path
            in self.selected_files
            if not path.is_file()
        ]

        if missing_files:

            messagebox.showerror(
                "Missing Input Files",
                (
                    "Some selected files "
                    "no longer exist:\n\n"
                    + "\n".join(
                        str(
                            path
                        )
                        for path
                        in missing_files[:10]
                    )
                ),
            )

            return None

        try:
            selected_project = ProjectRegistry.resolve(
                self.project_var.get()
            )
        except ProjectRegistryError:
            messagebox.showwarning(
                "Project Required",
                "Please select 21MM, 24MM, Common, or QA/BUG before processing.",
            )
            if hasattr(self, "project_combobox"):
                self.project_combobox.focus_set()
            return None

        save_json = bool(
            self.save_json_var.get()
        )

        save_database = bool(
            self.save_database_var.get()
        )

        if (
            not save_json
            and not save_database
        ):

            messagebox.showwarning(
                "No Output Selected",
                (
                    "Enable at least one "
                    "processing output."
                ),
            )

            return None

        output_text = (
            self.output_directory_var
            .get()
            .strip()
        )

        if not output_text:

            messagebox.showwarning(
                "Output Directory",
                (
                    "Please select an "
                    "output directory."
                ),
            )

            return None

        output_directory = Path(
            output_text
        ).resolve()

        try:

            output_directory.mkdir(
                parents=True,
                exist_ok=True,
            )

            # JSON-only users may not have PostgreSQL at all. Keep project
            # outputs physically separated so 21MM / 24MM / Common files
            # cannot be mixed in a shared output directory.
            if save_json and not save_database:
                ProjectOutputRouter.ensure_json_output_directory(
                    output_directory=output_directory,
                    project_code=selected_project.code,
                    save_database=False,
                )

        except OSError as exc:

            messagebox.showerror(
                "Output Directory Error",
                (
                    "Unable to access or "
                    "create output directory.\n\n"
                    f"{exc}"
                ),
            )

            return None

        database_config: DatabaseConfig | None = None

        if save_database:

            if not self._ensure_postgres_password():
                return None

            settings = self._collect_postgres_settings()
            if settings is None:
                return None

            # Document Conversion must use the exact PostgreSQL target shown
            # in Settings.  Do not re-resolve the database from a stale or
            # embedded config.yaml inside the worker pipeline.
            database_config = self._build_json_import_database_config(
                settings=settings
            )

        return ProcessingOptions(
            output_directory=(
                output_directory
            ),
            save_json=(
                save_json
            ),
            save_database=(
                save_database
            ),
            project_code=selected_project.code,
            database_config=database_config,
        )

    # ========================================================
    # Start Processing
    # ========================================================

    def start_processing(
        self,
    ) -> None:

        if self.is_processing:
            return

        options = (
            self._validate_before_start()
        )

        if options is None:
            return

        files = list(
            self.selected_files
        )

        qabug_runtime = None
        if options.project_code == "QABUG" and options.save_database:
            try:
                qabug_runtime = self._qabug_runtime_profiles()
            except Exception as exc:
                messagebox.showerror("QA/BUG 导入", str(exc), parent=self.root)
                return

        self.cancel_requested.clear()

        self.failures.clear()

        self._prepare_processing_ui(
            total=len(
                files
            )
        )

        LOGGER.info(
            (
                "Batch started | "
                "total=%s | "
                "json=%s | "
                "postgresql=%s | "
                "project=%s | "
                "output=%s"
            ),
            len(files),
            options.save_json,
            options.save_database,
            options.project_code,
            options.output_directory,
        )

        worker = self._process_qabug_files if options.project_code == "QABUG" else self._process_files
        worker_name = "qabug-unified-ingestion-worker" if options.project_code == "QABUG" else "document-ingestion-worker"
        worker_kwargs = {"files": files, "options": options}
        if options.project_code == "QABUG":
            worker_kwargs["runtime"] = qabug_runtime
        self.processing_thread = threading.Thread(
            target=worker,
            kwargs=worker_kwargs,
            daemon=True,
            name=worker_name,
        )

        self.processing_thread.start()

    def _prepare_processing_ui(
        self,
        *,
        total: int,
    ) -> None:

        self.is_processing = True

        self._set_controls_enabled(
            False
        )

        self.cancel_button.configure(
            state=NORMAL
        )

        self.progress_var.set(
            0
        )

        self.progress_text_var.set(
            "0%"
        )

        self.status_var.set(
            "Processing"
        )

        self.current_file_var.set(
            "Preparing batch..."
        )

        self.summary_var.set(
            (
                "Success: 0    "
                "Warnings: 0    "
                "Failed: 0    "
                f"Total: {total}"
            )
        )

        self._clear_log()
        self._last_ingest_specification_key = None
        self._last_ingest_specification_name = None
        self._last_ingest_version = None
        self.last_ingest_result_var.set("Waiting for a versioned specification result…")
        self.open_last_spec_button.configure(state=DISABLED)
        self.open_last_changes_button.configure(state=DISABLED)

    def _refresh_unified_import_scope_ui(self, project_code: str) -> None:
        """Keep the shared import page explicit about its active ingestion contract."""
        is_qabug = str(project_code).upper() == "QABUG"
        if hasattr(self, "convert_button"):
            self.convert_button.configure(text="▶  导入 QA/BUG" if is_qabug else "▶  转换")
        if hasattr(self, "json_checkbox"):
            self.json_checkbox.configure(text="生成企业 RAG JSONL" if is_qabug else "生成 JSON")
        if hasattr(self, "database_checkbox"):
            self.database_checkbox.configure(text="保存到 PostgreSQL + Qdrant" if is_qabug else "保存到 PostgreSQL")
        if hasattr(self, "supported_types_label"):
            self.supported_types_label.configure(
                text=(
                    "支持 QA/BUG List：XLSX / XLSM / CSV / TSV / TXT"
                    if is_qabug
                    else "支持：PDF / DOCX / PPTX / PPT / XLSX / TXT / PNG / JPG"
                )
            )
        if hasattr(self, "output_policy_label"):
            self.output_policy_label.configure(
                text=(
                    "QA/BUG：可生成企业 RAG JSONL，并可直接写入 rag_qabug + rag_qabug_current_chunks。"
                    if is_qabug
                    else "仅在启用数据库输出时使用 PostgreSQL 密码；仅生成 JSON 时按处理范围分目录保存。"
                )
            )
        if hasattr(self, "last_ingest_frame"):
            if is_qabug:
                self.last_ingest_frame.pack_forget()
            elif not self.last_ingest_frame.winfo_manager():
                self.last_ingest_frame.pack(fill=X, pady=(10, 0))

    def _process_qabug_files(
        self,
        *,
        files: list[Path],
        options: ProcessingOptions,
        runtime: tuple[PostgresProfile, QdrantService, str] | None = None,
    ) -> None:
        """Unified QA/BUG ingestion used by the same page as 21MM/24MM/Common.

        The source file is normalized to the canonical enterprise JSON contract.
        PostgreSQL remains authoritative; Qdrant is synchronized only after all
        selected files have completed their SQL upserts.
        """
        success_count = 0
        failed_count = 0
        warning_count = 0
        cancelled = False
        total = len(files)

        pg = None
        qdrant = None
        password = ""
        if options.save_database:
            try:
                if runtime is None:
                    raise RuntimeError("QA/BUG runtime profile was not captured on the UI thread.")
                pg, qdrant, password = runtime
                QABugSchemaManager(
                    host=pg.host, port=pg.port, database=pg.database, user=pg.user,
                    password=password, connect_timeout=pg.connect_timeout,
                ).ensure_schema(create_database=True)
                QABugQdrantManager(qdrant_service=qdrant).ensure_contract()
            except Exception as exc:
                self._emit("log", f"FAILED  | QA/BUG environment | {type(exc).__name__}: {exc}")
                self._emit("finished", BatchSummary(success=0, failed=total, total=total, warnings=0, cancelled=False))
                return

        pipeline = QABugImportPipeline()
        for index, file_path in enumerate(files, start=1):
            if self.cancel_requested.is_set():
                cancelled = True
                break
            self._emit("current_file", f"{index}/{total} {file_path.name}")
            self._emit("log", f"START   | {file_path.name} | domain=QA/BUG")
            try:
                record_type, confidence, scores = pipeline.detect_record_type(file_path)
                if confidence < 0.60:
                    warning_count += 1
                    self._emit(
                        "log",
                        f"WARNING | {file_path.name} | low type confidence={confidence:.2f} "
                        f"QA={scores.get('QA', 0)} BUG={scores.get('BUG', 0)} | selected={record_type}",
                    )

                jsonl_count = 0
                if options.save_json:
                    output_dir = ProjectOutputRouter.ensure_json_output_directory(
                        output_directory=options.output_directory,
                        project_code="QABUG",
                        save_database=options.save_database,
                    )
                    output_path = output_dir / f"{file_path.stem}_{record_type.lower()}_enterprise_rag.jsonl"
                    json_report = pipeline.export_jsonl(
                        file_path, output_path, record_type=record_type,
                        progress=lambda n, key, name=file_path.name: self._emit(
                            "log", f"QABUG JSONL | {name} | {n} records | {key}"
                        ),
                    )
                    jsonl_count = json_report.jsonl_written

                records = 0
                chunks = 0
                quarantined = 0
                if options.save_database and pg is not None:
                    sql_report = pipeline.import_postgres(
                        file_path, record_type=record_type, profile=pg, password=password,
                        progress=lambda n, key, name=file_path.name: self._emit(
                            "log", f"QABUG SQL | {name} | {n} records | {key}"
                        ),
                    )
                    records = sql_report.records_upserted
                    chunks = sql_report.chunks_upserted
                    quarantined = getattr(sql_report, "rows_quarantined", 0)
                    if sql_report.rows_failed:
                        warning_count += 1
                        self._emit("log", f"WARNING | {file_path.name} | row failures={sql_report.rows_failed}")

                success_count += 1
                self._emit(
                    "log",
                    f"SUCCESS | {file_path.name} | type={record_type} | jsonl={jsonl_count} | "
                    f"records={records} | chunks={chunks} | quarantine={quarantined}",
                )
            except Exception as exc:
                failed_count += 1
                failure = FileFailure(file_path=file_path, error_type=type(exc).__name__, error_message=str(exc))
                self.failures.append(failure)
                self._emit("log", f"FAILED  | {file_path.name} | {type(exc).__name__}: {exc}")
                LOGGER.error("QA/BUG unified import failed | file=%s\n%s", file_path, traceback.format_exc())

            percentage = int(index / total * 85) if options.save_database else int(index / total * 100)
            self._emit("progress", percentage)
            self._emit("summary", BatchSummary(success=success_count, failed=failed_count, total=total, warnings=warning_count))

        if options.save_database and not cancelled and pg is not None and qdrant is not None:
            try:
                self._emit("current_file", "Synchronizing QA/BUG vectors to Qdrant...")
                vector_report = QABugQdrantManager(qdrant_service=qdrant).sync_from_postgres(
                    postgres_profile=pg, password=password,
                    progress=lambda n, total_points, key: self._emit(
                        "log", f"QABUG QDRANT | {n}/{total_points} chunks | {key}"
                    ),
                )
                quality = QABugDataQualityValidator(
                    postgres_profile=pg, password=password, qdrant_service=qdrant,
                ).validate()
                self._emit(
                    "log",
                    f"QABUG VECTOR | upserted={vector_report.points_upserted} | "
                    f"stale_deleted={vector_report.stale_points_deleted} | DQ={quality.status}",
                )
                self._emit(
                    "qabug_vector_ready",
                    {
                        "collection": vector_report.collection,
                        "upserted": vector_report.points_upserted,
                        "qdrant_points": quality.qdrant_points,
                        "quality": quality.status,
                    },
                )
                if quality.status != "PASS":
                    warning_count += 1
                self._emit("progress", 100)
            except Exception as exc:
                warning_count += 1
                self._emit("log", f"WARNING | QA/BUG Qdrant sync incomplete | {type(exc).__name__}: {exc}")

        self._emit(
            "finished",
            BatchSummary(
                success=success_count, failed=failed_count, total=total,
                warnings=warning_count, cancelled=cancelled,
            ),
        )

    # ========================================================
    # Worker Thread
    # ========================================================

    def _process_files(
        self,
        *,
        files: list[Path],
        options: ProcessingOptions,
    ) -> None:

        success_count = 0
        failed_count = 0
        warning_count = 0

        runtime_override_active = False
        if options.save_database:
            if options.database_config is None:
                raise RuntimeError(
                    "PostgreSQL output is enabled but no runtime database "
                    "configuration was captured from Settings."
                )
            ProjectDatabaseRouter.set_runtime_override(
                options.project_code,
                options.database_config,
            )
            runtime_override_active = True
            LOGGER.info(
                "Document conversion PostgreSQL target locked | "
                "project=%s | database=%s | host=%s | port=%s",
                options.project_code,
                options.database_config.database,
                options.database_config.host,
                options.database_config.port,
            )

        total = len(
            files
        )

        cancelled = False

        for index, file_path in enumerate(
            files,
            start=1,
        ):

            if self.cancel_requested.is_set():

                cancelled = True

                LOGGER.warning(
                    "Batch cancellation requested."
                )

                break

            self._emit(
                "current_file",
                (
                    f"{index}/{total} "
                    f"{file_path.name}"
                ),
            )

            self._emit(
                "log",
                (
                    f"START   | "
                    f"{file_path.name}"
                ),
            )

            LOGGER.info(
                "Processing started | "
                "file=%s",
                file_path,
            )

            try:

                pipeline = (
                    PipelineFactory.create(
                        file_path,
                        save_json=(
                            options.save_json
                        ),
                        save_database=(
                            options
                            .save_database
                        ),
                        project_code=options.project_code,
                    )
                )

                output_path = ProjectOutputRouter.resolve_json_output_path(
                    output_directory=options.output_directory,
                    project_code=options.project_code,
                    file_stem=file_path.stem,
                    save_database=options.save_database,
                )

                document = (
                    pipeline.run(
                        file_path=(
                            file_path
                        ),
                        output=(
                            output_path
                        ),
                    )
                )

                success_count += 1

                pages = len(
                    getattr(
                        document,
                        "pages",
                        [],
                    )
                )

                chapters = len(
                    getattr(
                        document,
                        "chapters",
                        [],
                    )
                )

                sections = len(
                    getattr(
                        document,
                        "sections",
                        [],
                    )
                )

                contents = len(
                    getattr(
                        document,
                        "contents",
                        [],
                    )
                )

                change_summary = str(
                    (getattr(document, "metadata", None) or {}).get(
                        "specification_change_summary"
                    )
                    or ""
                ).strip()
                relation_summary = str(
                    (getattr(document, "metadata", None) or {}).get(
                        "engineering_relation_summary"
                    )
                    or ""
                ).strip()
                document_metadata = dict(getattr(document, "metadata", None) or {})
                document_role = str(document_metadata.get("document_role") or "PRIMARY").upper()
                supporting_type = str(document_metadata.get("supporting_document_type") or "").strip()
                import_status = str(document_metadata.get("import_status") or "IMPORTED").upper()
                import_warning_codes = [
                    str(code) for code in (document_metadata.get("import_warning_codes") or []) if str(code).strip()
                ]
                has_warning = bool(import_warning_codes) or import_status in {
                    "IMPORTED_WITH_WARNING", "IMPORTED_SANITIZED"
                }
                if has_warning:
                    warning_count += 1

                success_message = (
                    "SUCCESS | "
                    f"{file_path.name} | "
                    f"project={options.project_code} | "
                    f"pages={pages} | "
                    f"chapters={chapters} | "
                    f"sections={sections} | "
                    f"contents={contents}"
                )
                if change_summary:
                    success_message += f" | changes={change_summary}"
                if relation_summary:
                    success_message += f" | relations={relation_summary}"
                if document_role == "SUPPORTING":
                    success_message += (
                        f" | role=SUPPORTING"
                        f" | type={supporting_type or 'OTHER'}"
                        " | qdrant=EXCLUDED"
                    )
                elif document_role == "METADATA_ONLY":
                    success_message += " | role=METADATA_ONLY | qdrant=EXCLUDED"
                elif document_role == "TRANSLATION":
                    success_message += (
                        f" | role=TRANSLATION"
                        f" | language={document_metadata.get('translation_language') or 'UNSPECIFIED'}"
                        " | qdrant=EXCLUDED"
                    )
                if import_warning_codes:
                    success_message += " | warnings=" + ",".join(import_warning_codes)

                self._emit(
                    "log",
                    success_message,
                )

                metadata = dict(getattr(document, "metadata", None) or {})
                specification_key = str(metadata.get("specification_key") or "").strip()
                if specification_key:
                    self._emit(
                        "ingest_spec_result",
                        {
                            "specification_key": specification_key,
                            "specification_name": str(metadata.get("specification_name") or specification_key),
                            "version": str(metadata.get("specification_version") or metadata.get("version") or "-") ,
                            "change_summary": change_summary,
                            "relation_summary": relation_summary,
                            "file_name": file_path.name,
                        },
                    )

                LOGGER.info(
                    success_message
                )

            except Exception as exc:

                failed_count += 1

                error_message = str(
                    exc
                )
                is_version_conflict = "Specification version conflict:" in error_message
                error_type = "VERSION_CONFLICT" if is_version_conflict else type(exc).__name__

                failure = FileFailure(
                    file_path=(
                        file_path
                    ),
                    error_type=(
                        error_type
                    ),
                    error_message=(
                        error_message
                    ),
                )

                self.failures.append(
                    failure
                )

                gui_message = (
                    ("CONFLICT | " if is_version_conflict else "FAILED  | ")
                    + f"{file_path.name} | "
                    + f"{error_type}: {error_message}"
                )

                self._emit(
                    "log",
                    gui_message,
                )

                LOGGER.error(
                    gui_message
                )

                LOGGER.error(
                    "Traceback | "
                    "file=%s\n%s",
                    file_path,
                    traceback.format_exc(),
                )

            percentage = int(
                index
                / total
                * 100
            )

            self._emit(
                "progress",
                percentage,
            )

            self._emit(
                "summary",
                BatchSummary(
                    success=(
                        success_count
                    ),
                    failed=(
                        failed_count
                    ),
                    total=total,
                    warnings=warning_count,
                    cancelled=False,
                ),
            )

        if runtime_override_active:
            ProjectDatabaseRouter.clear_runtime_override(
                options.project_code
            )

        self._emit(
            "finished",
            BatchSummary(
                success=(
                    success_count
                ),
                failed=(
                    failed_count
                ),
                total=total,
                warnings=warning_count,
                cancelled=(
                    cancelled
                ),
            ),
        )

    # ========================================================
    # Cancel
    # ========================================================

    def cancel_processing(
        self,
    ) -> None:

        if not self.is_processing:
            return

        if self.cancel_requested.is_set():
            return

        confirmed = (
            messagebox.askyesno(
                "Cancel Processing",
                (
                    "Stop after the current "
                    "document finishes?"
                ),
            )
        )

        if not confirmed:
            return

        self.cancel_requested.set()

        self.cancel_button.configure(
            state=DISABLED
        )

        self.status_var.set(
            "Cancelling..."
        )

        self._append_log(
            (
                "CANCEL  | "
                "Cancellation requested."
            )
        )

    # ========================================================
    # Event Queue
    # ========================================================

    def _emit(
        self,
        event_type: str,
        payload: Any,
    ) -> None:

        self.event_queue.put(
            (
                event_type,
                payload,
            )
        )

    def _poll_events(
        self,
    ) -> None:

        try:

            while True:

                (
                    event_type,
                    payload,
                ) = (
                    self.event_queue
                    .get_nowait()
                )

                self._handle_event(
                    event_type,
                    payload,
                )

        except queue.Empty:
            pass

        self.root.after(
            100,
            self._poll_events,
        )

    def _handle_event(
        self,
        event_type: str,
        payload: Any,
    ) -> None:

        if event_type == "log":

            self._append_log(
                str(
                    payload
                )
            )

        elif (
            event_type
            == "current_file"
        ):

            self.current_file_var.set(
                str(
                    payload
                )
            )

        elif (
            event_type
            == "progress"
        ):

            value = float(
                payload
            )

            self.progress_var.set(
                value
            )

            self.progress_text_var.set(
                f"{int(value)}%"
            )

        elif (
            event_type
            == "summary"
        ):

            self._update_summary(
                payload
            )

        elif (
            event_type
            == "finished"
        ):

            self._handle_finished(
                payload
            )

        elif event_type == "qabug_vector_ready":
            # Keep the administration views in sync with the just-finished
            # unified QA/BUG import. This runs on the Tk thread.
            try:
                if hasattr(self, "enterprise_module"):
                    self.enterprise_module.refresh_qdrant_sync_context()
                    self.enterprise_module.refresh_qdrant()
            except Exception:
                LOGGER.exception("Unable to refresh QA/BUG Qdrant administration view after import")

        elif (
            event_type
            == "json_import_log"
        ):

            self._append_json_import_log(
                str(
                    payload
                )
            )

        elif (
            event_type
            == "json_import_current_file"
        ):

            self.json_import_current_file_var.set(
                str(
                    payload
                )
            )

        elif (
            event_type
            == "json_import_progress"
        ):

            value = float(
                payload
            )

            self.json_import_progress_var.set(
                value
            )

            self.json_import_progress_text_var.set(
                f"{int(value)}%"
            )

        elif (
            event_type
            == "json_import_summary"
        ):

            self._update_json_import_summary(
                payload
            )

        elif (
            event_type
            == "json_import_finished"
        ):

            self._handle_json_import_finished(
                payload
            )

        elif (
            event_type
            == "postgres_test_result"
        ):

            self._handle_postgres_test_result(
                payload
            )

        elif (
            event_type
            == "postgres_schema_result"
        ):

            self._handle_postgres_schema_result(
                payload
            )

        elif event_type == "qabug_setup_result":
            self._handle_qabug_setup_result(payload)

        elif event_type == "qabug_import_result":
            self._handle_qabug_import_result(payload)

        elif event_type == "qabug_quality_result":
            self._handle_qabug_quality_result(payload)

        elif event_type == "version_catalog_result":
            self._handle_version_catalog_result(payload)

        elif event_type == "version_rows_result":
            self._handle_version_rows_result(payload)

        elif event_type == "version_compare_result":
            self._handle_version_compare_result(payload)

        elif event_type == "change_timeline_result":
            self._handle_change_timeline_result(payload)

        elif event_type == "change_detail_result":
            self._handle_change_detail_result(payload)

        elif event_type == "ingest_spec_result":
            self._handle_ingest_spec_result(payload)

        elif event_type == "engineering_relations_result":
            self._handle_engineering_relations_result(payload)

        elif event_type == "relation_evidence_result":
            self._handle_relation_evidence_result(payload)

        elif event_type == "relation_reconcile_result":
            self._handle_relation_reconcile_result(payload)

    def _handle_ingest_spec_result(self, payload: dict[str, Any]) -> None:
        key = str(payload.get("specification_key") or "").strip()
        if not key:
            return
        self._last_ingest_specification_key = key
        self._last_ingest_specification_name = str(payload.get("specification_name") or key)
        self._last_ingest_version = str(payload.get("version") or "-")
        change_summary = str(payload.get("change_summary") or "").strip()
        relation_summary = str(payload.get("relation_summary") or "").strip()
        text = f"{self._last_ingest_specification_name}  |  {self._last_ingest_version}"
        if change_summary:
            text += f"  |  {change_summary}"
        if relation_summary:
            text += f"  |  relations: {relation_summary}"
        self.last_ingest_result_var.set(text)
        if not self.is_processing:
            self.open_last_spec_button.configure(state=NORMAL)
            self.open_last_changes_button.configure(state=NORMAL)
        # Force a fresh catalog the next time the operator opens lifecycle data.
        self._version_catalog_rows.clear()

    def _open_last_ingested_specification(self, page_key: str) -> None:
        key = self._last_ingest_specification_key
        if not key:
            messagebox.showinfo("Specification", "No versioned specification has been imported in this session.", parent=self.root)
            return
        row = next((item for item in self._version_catalog_rows if str(item.get("specification_key") or "") == key), None)
        if row is not None:
            self._select_specification_by_id(int(row["specification_id"]))
            self.navigate(page_key)
            if page_key == "spec_changes":
                self.root.after_idle(self.refresh_change_timeline)
            return
        self._pending_open_spec_key = key
        self._pending_open_spec_page = page_key
        self.navigate("spec_catalog")
        self.refresh_version_catalog()

    # ========================================================
    # Summary
    # ========================================================

    def _update_summary(
        self,
        summary: BatchSummary,
    ) -> None:

        self.summary_var.set(
            (
                f"Success: "
                f"{summary.success}    "
                f"Failed: "
                f"{summary.failed}    "
                f"Total: "
                f"{summary.total}"
            )
        )

    # ========================================================
    # Finish
    # ========================================================

    def _handle_finished(
        self,
        summary: BatchSummary,
    ) -> None:

        self.is_processing = False

        self.cancel_requested.clear()

        self._set_controls_enabled(
            True
        )

        self.cancel_button.configure(
            state=DISABLED
        )
        if self._last_ingest_specification_key:
            self.open_last_spec_button.configure(state=NORMAL)
            self.open_last_changes_button.configure(state=NORMAL)
        else:
            self.open_last_spec_button.configure(state=DISABLED)
            self.open_last_changes_button.configure(state=DISABLED)
            if self.last_ingest_result_var.get().startswith("Waiting"):
                self.last_ingest_result_var.set("No versioned specification was committed in this batch.")

        self._update_summary(
            summary
        )

        if summary.cancelled:

            self.status_var.set(
                "Cancelled"
            )

            self.current_file_var.set(
                (
                    "Batch processing "
                    "was cancelled."
                )
            )

            self._append_log(
                (
                    "BATCH   | "
                    "Processing cancelled."
                )
            )

            LOGGER.warning(
                (
                    "Batch cancelled | "
                    "success=%s | "
                    "failed=%s | "
                    "total=%s"
                ),
                summary.success,
                summary.failed,
                summary.total,
            )

            messagebox.showwarning(
                "Processing Cancelled",
                (
                    "Batch processing "
                    "was cancelled."
                ),
            )

            return

        self.progress_var.set(
            100
        )

        self.progress_text_var.set(
            "100%"
        )

        self.status_var.set(
            "Completed"
        )

        self.current_file_var.set(
            (
                "Batch processing "
                "completed."
            )
        )

        self._append_log(
            (
                "BATCH   | "
                f"success="
                f"{summary.success} | "
                f"failed="
                f"{summary.failed} | "
                f"warnings="
                f"{summary.warnings} | "
                f"total="
                f"{summary.total}"
            )
        )

        LOGGER.info(
            (
                "Batch completed | "
                "success=%s | "
                "failed=%s | "
                "warnings=%s | "
                "total=%s"
            ),
            summary.success,
            summary.failed,
            summary.warnings,
            summary.total,
        )

        if summary.failed == 0:
            message = (
                "Batch processing completed.\n\n"
                f"Imported: {summary.success}\n"
                f"Warnings: {summary.warnings}\n"
                f"Failed: {summary.failed}\n"
                f"Total: {summary.total}"
            )
            if summary.warnings:
                messagebox.showwarning(
                    "Completed with Warnings",
                    message + "\n\nWarning-class documents were preserved for audit and excluded from Qdrant when required.",
                )
            else:
                messagebox.showinfo("Completed", message)
            return

        self._show_failure_dialog(
            summary
        )

    # ========================================================
    # Failure Dialog
    # ========================================================

    def _show_failure_dialog(
        self,
        summary: BatchSummary,
    ) -> None:

        conflict_failures = [failure for failure in self.failures if failure.error_type == "VERSION_CONFLICT"]
        lines = [
            (
                "Batch completed with version conflicts / errors."
                if conflict_failures
                else "Batch completed with errors."
            ),
            "",
            (
                f"Success: "
                f"{summary.success}"
            ),
            (
                f"Failed: "
                f"{summary.failed}"
            ),
            (
                f"Warnings: "
                f"{summary.warnings}"
            ),
            (
                f"Total: "
                f"{summary.total}"
            ),
            "",
            "Failed documents:",
        ]

        for index, failure in enumerate(
            self.failures[:5],
            start=1,
        ):

            lines.extend(
                [
                    "",
                    (
                        f"{index}. "
                        f"{failure.file_path.name}"
                    ),
                    (
                        f"   "
                        f"{failure.error_type}: "
                        f"{failure.error_message}"
                    ),
                ]
            )

        if len(
            self.failures
        ) > 5:

            lines.extend(
                [
                    "",
                    (
                        "... additional failures "
                        "are available in "
                        "logs/gui.log"
                    ),
                ]
            )

        if conflict_failures:
            lines.extend(
                [
                    "",
                    "Version conflict policy:",
                    "  Primary specification versions are immutable and are never silently overwritten.",
                    "  CoverSheet / CheckSheet / TestSheet / 表紙 / 添付資料 are stored as SUPPORTING and excluded from Current Qdrant.",
                    "  Explicit language variants (for example *_en) are stored separately as TRANSLATION and never overwrite the immutable primary version.",
                    "  If a real primary specification changed, assign a new version label and import again.",
                    "  Use Specification > Compare / Changes to inspect existing version history.",
                ]
            )

        lines.extend(
            [
                "",
                (
                    "Full exception details:"
                ),
                str(
                    LOG_DIR
                    / "gui.log"
                ),
            ]
        )

        messagebox.showwarning(
            "Completed with Errors",
            "\n".join(
                lines
            ),
        )

    # ========================================================
    # Controls
    # ========================================================

    def _set_controls_enabled(
        self,
        enabled: bool,
    ) -> None:

        state = (
            NORMAL
            if enabled
            else DISABLED
        )

        widgets = (
            self.add_files_button,
            self.add_folder_button,
            self.remove_files_button,
            self.clear_files_button,
            self.output_button,
            self.output_entry,
            self.json_checkbox,
            self.database_checkbox,
            self.convert_button,
            self.add_json_files_button,
            self.add_json_folder_button,
            self.remove_json_files_button,
            self.clear_json_files_button,
            self.import_json_button,
            self.postgres_test_button,
            self.postgres_save_button,
            self.postgres_defaults_button,
            self.postgres_clear_password_button,
            self.postgres_remember_checkbox,
            *self.postgres_setting_entries,
        )

        for widget in widgets:

            widget.configure(
                state=state
            )

        if hasattr(self, "project_combobox"):
            self.project_combobox.configure(
                state="readonly" if enabled else DISABLED
            )
        if hasattr(self, "postgres_project_combobox"):
            self.postgres_project_combobox.configure(
                state="readonly" if enabled else DISABLED
            )
        if hasattr(self, "header_project_combobox"):
            self.header_project_combobox.configure(
                state="readonly" if enabled else DISABLED
            )

        if hasattr(
            self,
            "postgres_create_tables_button",
        ):
            self.postgres_create_tables_button.configure(
                state=(
                    NORMAL
                    if (
                        enabled
                        and self.postgres_connection_verified
                    )
                    else DISABLED
                )
            )
        if hasattr(self, "qabug_setup_button"):
            self.qabug_setup_button.configure(
                state=NORMAL if enabled and self.postgres_connection_verified else DISABLED
            )
        # QA/BUG import/quality are controlled by the last successful setup/validation result.
        if not enabled:
            for name in ("qabug_import_qa_button", "qabug_import_bug_button", "qabug_quality_button"):
                if hasattr(self, name):
                    getattr(self, name).configure(state=DISABLED)

    # ========================================================
    # GUI Log
    # ========================================================

    def _append_log(
        self,
        message: str,
    ) -> None:

        self.log_text.configure(
            state=NORMAL
        )

        self.log_text.insert(
            END,
            (
                message
                + "\n"
            ),
        )

        self.log_text.see(
            END
        )

        self.log_text.configure(
            state=DISABLED
        )

    def _clear_log(
        self,
    ) -> None:

        self.log_text.configure(
            state=NORMAL
        )

        self.log_text.delete(
            "1.0",
            END,
        )

        self.log_text.configure(
            state=DISABLED
        )

    # ========================================================
    # Close
    # ========================================================

    def _on_close(
        self,
    ) -> None:

        if self.is_processing:

            confirmed = (
                messagebox.askyesno(
                    "Exit Application",
                    (
                        "Document processing "
                        "is still running.\n\n"
                        "Exit anyway?"
                    ),
                )
            )

            if not confirmed:
                return

            self.cancel_requested.set()

        if hasattr(self, "ai_module"):
            try:
                self.ai_module.shutdown()
            except Exception:
                LOGGER.exception("Failed to shut down AI workspace cleanly.")

        if hasattr(self, "enterprise_module"):
            try:
                self.enterprise_module.shutdown()
            except Exception:
                LOGGER.exception("Failed to shut down enterprise module cleanly.")

        LOGGER.info(
            "GUI application closed."
        )

        self.root.destroy()


# ============================================================
# Application Entry
# ============================================================


def main() -> None:

    root = ttk.Window(
        themename="flatly"
    )

    DocumentIngestionGUI(
        root
    )

    root.mainloop()


if __name__ == "__main__":
    main()
