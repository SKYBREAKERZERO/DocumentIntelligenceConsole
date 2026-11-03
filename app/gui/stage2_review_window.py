"""Local Stage 2 human-review console. NEVER expose this as a web/API endpoint.

An offline graph snapshot is NOT an authorization. The GUI separately connects
with a dedicated PostgreSQL reviewer LOGIN and uses the v1.33 DB-side authority.
This program neither records credentials nor silently approves relations.
"""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk
from typing import Any

from app.engineering_intelligence.content_relation_review import (
    ContentRelationReviewCase,
    list_relation_queue,
    load_review_case,
    review_content_relation_secured,
)


class Stage2ReviewUIError(ValueError):
    pass


def confirm_review_inputs(
    case: ContentRelationReviewCase | None, *, candidate: str, source_hash12: str,
    target_hash12: str, outcome: str, reason: str,
) -> None:
    """Human must transcribe candidate ID + both endpoint digest prefixes."""
    if case is None or candidate.strip() != str(case.candidate_id):
        raise Stage2ReviewUIError("必须输入当前预览候选 ID")
    if (source_hash12.strip() != case.source.text_hash[:12]
            or target_hash12.strip() != case.target.text_hash[:12]):
        raise Stage2ReviewUIError("必须抄录双端 SHA-256 前 12 位以确认已检查原文")
    transitions = {
        "REVIEW_REQUIRED": {"CONFIRMED", "REJECTED"},
        "CONFIRMED": {"REVIEW_REQUIRED"},
        "REJECTED": {"REVIEW_REQUIRED"},
    }
    if outcome not in transitions.get(case.status, set()):
        raise Stage2ReviewUIError("不允许的状态转换")
    if not 12 <= len(reason.strip()) <= 2000:
        raise Stage2ReviewUIError("审核理由必须为 12–2000 字符")


def run_db_task(settings: dict[str, Any], operation: str, *, candidate_id: int = 0,
                expected: str = "", outcome: str = "", reason: str = "",
                source_hash: str = "", target_hash: str = "") -> Any:
    """One PostgreSQL transaction per operator action, with project identity gate."""
    import psycopg  # Delayed import: window opens even if optional driver is absent.

    with psycopg.connect(host=settings["host"], port=settings["port"],
                         dbname=settings["database"], user=settings["user"],
                         password=settings["password"], connect_timeout=10,
                         application_name="dic-stage2-review-gui") as conn:
        with conn.cursor() as cur:
            if operation != "apply":
                cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            # Bound waits on a competing version head/review transition; never
            # retry an uncertain COMMIT after a network/statement timeout.
            cur.execute("SET LOCAL lock_timeout = '5s'")
            cur.execute("SET LOCAL statement_timeout = '60s'")
            cur.execute("SELECT project_code FROM public.project_info ORDER BY project_code LIMIT 2")
            rows = cur.fetchall()
            if len(rows) != 1 or str(rows[0][0]) != settings["project"]:
                raise Stage2ReviewUIError("数据库项目身份与操作范围不一致")
            if operation == "list":
                data = list_relation_queue(cur, project=settings["project"], limit=50)
            elif operation == "preview":
                data = load_review_case(cur, candidate_id=candidate_id, project=settings["project"])
            elif operation == "apply":
                data = review_content_relation_secured(
                    cur, candidate_id=candidate_id, project=settings["project"],
                    expected_status=expected, new_status=outcome, reason=reason,
                    source_hash=source_hash, target_hash=target_hash,
                )
            else:
                raise Stage2ReviewUIError("不支持的数据库操作")
        if operation != "apply":
            conn.rollback()
        # For apply, psycopg's context manager commits only on success.
    return data


class Stage2ReviewWindow:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Stage 2 · Content Relation Graph 人工审核（受控 PostgreSQL）")
        root.geometry("1320x880")
        self.project = tk.StringVar(value="21MM")
        self.database = tk.StringVar(value="rag_21mm")
        self.host = tk.StringVar(value="127.0.0.1")
        self.port = tk.StringVar(value="5432")
        self.user = tk.StringVar(value="stage2_reviewer")
        self.password = tk.StringVar()
        self.candidate_id = tk.StringVar()
        self.ack_id = tk.StringVar()
        self.ack_source = tk.StringVar()
        self.ack_target = tk.StringVar()
        self.outcome = tk.StringVar(value="CONFIRMED")
        self.status = tk.StringVar(value="只读模式：加载待审核候选并查看完整双端证据。")
        self.selected: ContentRelationReviewCase | None = None
        self._queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._busy = False
        self._build()
        self.root.after(125, self._drain)

    def _build(self):
        frame = ttk.LabelFrame(self.root, text="工程库（独立 reviewer LOGIN；密码不保存）")
        frame.pack(fill="x", padx=12, pady=7)
        for pos, (name, var, width) in enumerate((
            ("项目", self.project, 9), ("数据库", self.database, 13),
            ("主机", self.host, 13), ("端口", self.port, 7),
            ("DB 登录", self.user, 16), ("密码", self.password, 17),
        )):
            ttk.Label(frame, text=name).grid(row=0, column=pos * 2, padx=3, pady=10)
            opts = {"width": width}
            if name == "密码":
                opts["show"] = "*"
            ttk.Entry(frame, textvariable=var, **opts).grid(row=0, column=pos * 2 + 1, padx=2)
        toolbar = ttk.Frame(self.root)
        toolbar.pack(fill="x", padx=12, pady=5)
        ttk.Button(toolbar, text="加载待审核队列（只读）", command=lambda: self._submit("list")).pack(side="left")
        ttk.Label(toolbar, text="候选 ID：").pack(side="left", padx=(18, 2))
        ttk.Entry(toolbar, textvariable=self.candidate_id, width=12).pack(side="left")
        ttk.Button(toolbar, text="验证双端原文（只读）", command=lambda: self._submit("preview")).pack(side="left", padx=8)
        ttk.Label(toolbar, text="无法凭离线 HTML 审核；状态和证据每次重新查询数据库。", foreground="#666").pack(side="left", padx=10)
        split = ttk.PanedWindow(self.root, orient="horizontal")
        split.pack(fill="both", expand=True, padx=12)
        left = ttk.Frame(split)
        right = ttk.Frame(split)
        split.add(left, weight=1)
        split.add(right, weight=4)
        self.tree = ttk.Treeview(left, columns=("id", "state", "source", "target"), show="headings", height=22)
        for key, caption, size in (("id", "ID", 60), ("state", "审核状态", 122), ("source", "源", 80), ("target", "目标", 80)):
            self.tree.heading(key, text=caption)
            self.tree.column(key, width=size, minwidth=55)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        scrollbar.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        ttk.Label(right, text="SOURCE → TARGET：未经删节的原文、精确偏移和 SHA-256（敏感信息）").pack(anchor="w")
        self.evidence = scrolledtext.ScrolledText(right, wrap="word", font=("Consolas", 10), state="disabled")
        self.evidence.pack(fill="both", expand=True)
        approval = ttk.LabelFrame(self.root, text="受控写入：输入候选 ID 与双端 SHA-256 前 12 位确认已检查证据")
        approval.pack(fill="x", padx=12, pady=8)
        for ix, (title, var, width) in enumerate((("候选 ID", self.ack_id, 12),
                                                   ("源 SHA-256 前12位", self.ack_source, 16),
                                                   ("目标 SHA-256 前12位", self.ack_target, 16))):
            ttk.Label(approval, text=title).grid(row=0, column=ix*2, padx=4, pady=7)
            ttk.Entry(approval, textvariable=var, width=width).grid(row=0, column=ix*2+1, padx=4)
        ttk.Label(approval, text="新状态").grid(row=0, column=6)
        ttk.Combobox(approval, values=("CONFIRMED", "REJECTED", "REVIEW_REQUIRED"),
                     textvariable=self.outcome, state="readonly", width=19).grid(row=0, column=7)
        ttk.Label(approval, text="审核理由（必填）").grid(row=1, column=0, columnspan=2, sticky="w", padx=5)
        self.reason = tk.Text(approval, height=2, width=96)
        self.reason.grid(row=1, column=2, columnspan=6, sticky="ew", padx=5, pady=8)
        ttk.Button(approval, text="提交人工审核（需二次确认）", command=lambda: self._submit("apply")).grid(row=2, column=0, columnspan=8, pady=7)
        ttk.Label(self.root, textvariable=self.status).pack(fill="x", padx=12, pady=6)

    def _on_select(self, _event=None):
        selected = self.tree.selection()
        if selected:
            self.candidate_id.set(selected[0])
        self._clear_preview()

    def _clear_preview(self):
        self.selected = None
        for var in (self.ack_id, self.ack_source, self.ack_target):
            var.set("")
        self.evidence.configure(state="normal")
        self.evidence.delete("1.0", "end")
        self.evidence.configure(state="disabled")

    def _config(self) -> dict[str, Any]:
        try:
            port = int(self.port.get())
        except ValueError as exc:
            raise Stage2ReviewUIError("无效的 PostgreSQL 端口") from exc
        if (not 1 <= port <= 65535 or not self.password.get() or not self.database.get().strip()
                or not self.project.get().strip() or self.project.get().upper() in {"QABUG", "RAG_QABUG"}
                or not self.user.get().strip() or self.user.get().lower() in {"postgres", "root"}):
            raise Stage2ReviewUIError("需使用工程库与独立 PostgreSQL reviewer 登录")
        return dict(host=self.host.get(), port=port, database=self.database.get().strip(),
                    project=self.project.get().strip(), user=self.user.get().strip(), password=self.password.get())

    def _submit(self, operation: str):
        if self._busy:
            return
        try:
            cfg = self._config()
            cid = int(self.candidate_id.get()) if operation != "list" else 0
            if operation != "list" and cid < 1:
                raise Stage2ReviewUIError("候选 ID 必须为正整数")
            kwargs: dict[str, Any] = {"candidate_id": cid}
            if operation == "apply":
                case = self.selected
                reason = self.reason.get("1.0", "end").strip()
                confirm_review_inputs(case, candidate=self.ack_id.get(),
                                      source_hash12=self.ack_source.get(),
                                      target_hash12=self.ack_target.get(),
                                      outcome=self.outcome.get(), reason=reason)
                if cid != case.candidate_id:
                    raise Stage2ReviewUIError("所选候选与预览不一致")
                if not messagebox.askyesno("最终确认", f"使用已认证的 DB 登录提交 #{cid}: {case.status} → {self.outcome.get()}？\n撤销也将永久留下审核记录。"):
                    return
                kwargs.update(expected=case.status, outcome=self.outcome.get(), reason=reason,
                              source_hash=case.source.text_hash, target_hash=case.target.text_hash)
            elif operation == "list":
                self._clear_preview()
            else:
                self._clear_preview()
        except (ValueError, Stage2ReviewUIError) as exc:
            messagebox.showerror("审核验证失败", str(exc))
            return
        self.password.set("")  # Never retain a secret in the Tk entry after submitting.
        self._busy = True
        self.status.set("正在访问 PostgreSQL；提交阶段网络异常可能导致结果未知，不允许自动重试。")

        def worker():
            try:
                answer = run_db_task(cfg, operation, **kwargs)
                self._queue.put((operation, answer))
            except Exception as exc:  # Sensitive exception may include server details: never print credentials.
                self._queue.put(("error", type(exc).__name__))
            finally:
                cfg["password"] = ""

        threading.Thread(target=worker, daemon=True).start()

    def _drain(self):
        while True:
            try:
                operation, value = self._queue.get_nowait()
            except queue.Empty:
                break
            self._busy = False
            if operation == "error":
                self._clear_preview()
                # After a network failure during COMMIT, the client cannot
                # infer whether PostgreSQL durably committed. Never describe
                # an ambiguous failure as "rolled back" or encourage a retry.
                self.status.set("数据库操作结果未知；若曾提交审核，必须只读重新查询状态及审计，禁止盲目重试。")
                messagebox.showerror("操作结果待核验", f"PostgreSQL 操作出现异常（{value}）。\n"
                                     "不能据此断言已回滚或未提交。请重新连接，先核对候选状态与最后一条审核审计，禁止直接重试。")
            elif operation == "list":
                for item in self.tree.get_children():
                    self.tree.delete(item)
                for row in value:
                    key = str(row["candidate_id"])
                    self.tree.insert("", "end", iid=key, values=(key, row["status"], row["source_code"], row["target_code"]))
                self.status.set(f"只读队列 {len(value)} 条；选中 ID 后读取双端原文。")
            elif operation == "preview":
                self.selected = value
                self.evidence.configure(state="normal")
                self.evidence.insert("1.0", self._describe(value))
                self.evidence.configure(state="disabled")
                self.status.set("证据预览已验证；审核前输入 ID 与两个 SHA-256 前缀。数据库提交时将二次复验。")
            else:
                self._clear_preview()
                self.reason.delete("1.0", "end")
                self.status.set("数据库受控审核事务已提交。请重新输入密码并加载列表复核审计状态。")
                messagebox.showinfo("完成", "审核写入已由数据库受控函数提交；请重新读取状态。")
        self.root.after(125, self._drain)

    @staticmethod
    def _describe(case: ContentRelationReviewCase) -> str:
        parts = [f"Candidate #{case.candidate_id}  状态: {case.status}  类型: {case.relation_type}",
                 f"关系原因: {case.rationale}", f"候选分数: {case.score}",
                 f"提交前最后审计 ID: {case.last_audit_id or 0} （请保留以供断线对账）",
                 "证据结论：章节引用 ≠ 已证实的功能依赖、冲突或变更影响。"]
        if case.reference_anchor is not None:
            anchor = case.reference_anchor
            parts.append(f"定位引用: {anchor.raw_text}  "
                         f"source-unit Unicode offsets=[{anchor.start_offset},{anchor.end_offset})")
        for side, ep in (("SOURCE", case.source), ("TARGET", case.target)):
            parts.append("\n" + "=" * 18 + " " + side + " " + "=" * 18)
            parts.append(f"{ep.specification_code}  {ep.version}  {ep.file_name}")
            parts.append(f"document_id={ep.document_id}  unit_key={ep.unit_key}")
            parts.append(f"content_id={ep.content_id}  section={ep.section_id}  page={ep.page_number}")
            parts.append(f"Unicode offsets=[{ep.start_offset},{ep.end_offset})  SHA-256={ep.text_hash}")
            parts.append("原文（完整）：\n" + ep.raw_text)
        return "\n".join(parts)


def main():
    root = tk.Tk()
    Stage2ReviewWindow(root)
    root.mainloop()


if __name__ == "__main__":
    main()
