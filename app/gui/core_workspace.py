"""Small operator home; reuse existing screens rather than duplicate editors."""
from __future__ import annotations

import json
from pathlib import Path
import tkinter as tk
from tkinter import messagebox
import ttkbootstrap as ttk

from app.enterprise.core_operations import recent_receipts
from app.skills.catalog import list_skills


CORE_GROUPS = [
    ("核心后台", [("core_home", "后台总览 / 一键入库"), ("settings", "连接配置 / 一键建库 / Wi-Fi"),
                 ("ai_settings", "模型与 Open WebUI 配置"), ("ai_status", "Backend / 模型状态")]),
    ("文档与数据", [("document_import", "文档 / QA / BUG 导入"), ("database", "SQL 数据库查看"),
                   ("qdrant", "Qdrant 库查看"), ("qdrant_sync", "增量同步 / 一致性检查")]),
    ("式样管理", [("spec_catalog", "式样书目录"), ("spec_versions", "历史版本"),
                 ("spec_compare", "历史版本对比"), ("spec_relations", "工程关系")]),
    ("Skills", [("skills", "Skills 能力与离线验证")]),
    ("高级工具", [("json_import", "已有 JSON 补导 SQL"), ("spec_changes", "变更中心"),
                  ("documents", "原文浏览"), ("ai_audit", "问答审计"),
                  ("ai_chat", "内嵌聊天诊断"), ("sql", "SQL 编辑器"),
                  ("visual", "可视化查询"), ("erd", "ER 图"), ("embedding", "Embedding 监控")]),
]


def build_home(host, parent, config_dir):
    ttk.Label(parent, text="企业知识后台", font=("Microsoft YaHei UI", 21, "bold")).pack(anchor="w", pady=(4, 8))
    ttk.Label(parent, text="管理员在这里配置、入库和检查数据；用户通过 Open WebUI 提问。", wraplength=800).pack(anchor="w")
    for title, text, actions in (
        ("1  配置与初始化", "首次使用先保存连接参数、测试连接，再初始化当前范围。Wi-Fi 仅共享 Open WebUI。", [
            ("配置 / 建库 / Wi-Fi", lambda: host.navigate("settings")),
            ("启动独立 Backend", host._ensure_local_backend_running),
            ("打开 Open WebUI", host._open_local_webui)]),
        ("2  文档 → JSON → SQL → Qdrant", "选择顶部处理范围并添加文件。一键入库先完成转换和 SQL，再增量同步向量并校验。", [
            ("添加文件", host.select_files), ("查看文件 / 输出位置", lambda: host.navigate("document_import")),
            ("一键入库并校验", host.start_core_ingestion)]),
        ("3  数据与工程管理", "保留 SQL 权威内容、当前版向量索引、历史版本和有来源证据的工程关系。", [
            ("SQL 数据库", lambda: host.navigate("database")), ("Qdrant 库", lambda: host.navigate("qdrant")),
            ("版本对比", lambda: host.navigate("spec_compare")), ("工程关系", lambda: host.navigate("spec_relations"))]),
    ):
        card = ttk.Labelframe(parent, text=title, padding=14)
        card.pack(fill="x", pady=(16, 0))
        ttk.Label(card, text=text, wraplength=850).pack(anchor="w", pady=(0, 9))
        row = ttk.Frame(card)
        row.pack(fill="x")
        for label, callback in actions:
            ttk.Button(row, text=label, command=callback, bootstyle="primary-outline").pack(side="left", padx=(0, 8))
    status = ttk.Labelframe(parent, text="当前导入状态", padding=12)
    status.pack(fill="x", pady=(16, 0))
    ttk.Label(status, textvariable=host.file_count_var).pack(anchor="w")
    ttk.Label(status, textvariable=host.current_file_var, wraplength=850).pack(anchor="w")
    ttk.Label(status, textvariable=host.status_var).pack(anchor="w")
    ttk.Button(status, text="停止（当前文档 / 批次安全点）", command=host.cancel_processing, bootstyle="warning-outline").pack(anchor="w", pady=6)
    history = ttk.Labelframe(parent, text="最近一键入库回执", padding=12)
    history.pack(fill="both", expand=True, pady=16)
    tree = ttk.Treeview(history, columns=("scope", "status", "stage", "retry"), show="headings", height=5)
    for column, label, width in (("scope", "范围", 70), ("status", "结果", 150), ("stage", "阶段", 260), ("retry", "后续操作", 250)):
        tree.heading(column, text=label)
        tree.column(column, width=width, stretch=True)
    tree.pack(fill="both", expand=True)
    def refresh():
        tree.delete(*tree.get_children())
        for row in recent_receipts(Path(config_dir)/"ingestion_runs"):
            tree.insert("", "end", values=(row["scope"], row["display_status"],
                        " / ".join(row["stages"].values()), row["retry"]))
    ttk.Button(history, text="刷新回执", command=refresh).pack(anchor="w", pady=6)
    ttk.Label(history, text="PARTIAL 表示 SQL 可能已提交，但向量或校验未完成；请到增量同步页恢复。RUNNING 回执不证明任务仍在运行。", wraplength=850).pack(anchor="w")
    refresh()


def build_skills(host, parent):
    ttk.Label(parent, text="Skills 能力中心", font=("Microsoft YaHei UI", 20, "bold")).pack(anchor="w", pady=8)
    ttk.Label(parent, text="七项受控能力继续接入原问答路由。下面只验证本地 JSON 快照，不访问数据库或执行任意代码。", wraplength=850).pack(anchor="w")
    records = list_skills()
    selected = tk.StringVar(value=records[0]["name"])
    ttk.Combobox(parent, textvariable=selected, values=[x["name"] for x in records], state="readonly", width=48).pack(anchor="w", pady=12)
    ttk.Label(parent, text="输入 JSON（可加载随包示例）").pack(anchor="w")
    payload = tk.Text(parent, height=10, wrap="word")
    payload.pack(fill="both", expand=True)
    payload.insert("1.0", "{}")
    result = tk.Text(parent, height=10, wrap="word", state="disabled")
    row = ttk.Frame(parent)
    row.pack(fill="x", pady=10)
    def example():
        path=Path(__file__).resolve().parents[2]/"examples"/"skills"/(selected.get()+".json")
        if not path.is_file():
            messagebox.showinfo("Skills", "此能力暂无同名示例，请参照 config/skills 下的输入契约。", parent=host.root)
            return
        payload.delete("1.0", "end")
        payload.insert("1.0", path.read_text(encoding="utf-8"))
    def execute():
        from app.skills.executor import execute_skill
        try:
            raw=payload.get("1.0", "end")
            if len(raw.encode("utf-8"))>2_000_000:
                raise ValueError("输入超过 2MB")
            output=execute_skill(selected.get(), json.loads(raw))
        except (ValueError, KeyError, TypeError) as exc:
            output={"status":"BLOCKED", "reason":str(exc)[:180]}
        result.configure(state="normal")
        result.delete("1.0", "end")
        result.insert("1.0", json.dumps(output, ensure_ascii=False, indent=2))
        result.configure(state="disabled")
    ttk.Button(row, text="加载示例", command=example).pack(side="left", padx=(0,8))
    ttk.Button(row, text="运行离线验证", command=execute).pack(side="left")
    ttk.Label(parent, text="执行结果 / 限制").pack(anchor="w")
    result.pack(fill="both", expand=True)
