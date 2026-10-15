"""Local developer-only Stage 1 review GUI (tkinter + PostgreSQL).

Separate process from main GUI; no HTTP endpoint or Open WebUI reviewer access.
Read-only by default. A reviewer must explicitly enter project/version,
credentials, backup confirmation and a reason before appending an event.
"""
from __future__ import annotations

import queue
import threading
import tkinter as tk
import uuid
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Any, Callable

from app.engineering_intelligence.review_events import record_review_event
from app.engineering_intelligence.review_queue import (
    ReviewDetail, list_review_queue, load_review_detail, validate_review_scope, check_database_project,
)


class Stage1ReviewWindow:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Stage 1 · 工程证据人工审核 / Gold v3 质量评估（本地管理员）")
        root.geometry("1310x840")
        self.project = tk.StringVar(value="21MM")
        self.version = tk.StringVar(value="")
        self.host = tk.StringVar(value="127.0.0.1")
        self.port = tk.StringVar(value="5432")
        self.user = tk.StringVar(value="stage1_reviewer")
        self.password = tk.StringVar(value="")
        self.reviewer = tk.StringVar(value="")
        self.reason = tk.StringVar(value="")
        self.state = tk.StringVar(value="NEEDS_CLARIFICATION")
        self.assertion = tk.StringVar(value="")
        self.target = tk.StringVar(value="")
        self.backup = tk.BooleanVar(value=False)
        self.kind = tk.StringVar(value="ALL")
        self.status_filter = tk.StringVar(value="ALL")
        self.status_message = tk.StringVar(value="默认只读。审核写入需要显式确认。")
        self.rows: list[Any] = []
        self.selected: ReviewDetail | None = None
        self._after: str | None = None
        self._native_view_key: str | None = None
        self.header_rows = tk.StringVar(value="")
        self._busy = False
        self._updates: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._build()
        self.state.trace_add("write", lambda *_: self._reset_assertion_if_not_confirmed())
        self.root.after(120, self._drain)
        root.protocol("WM_DELETE_WINDOW", self._close)

    def _build(self) -> None:
        top = ttk.LabelFrame(self.root, text="项目与 PostgreSQL（不会保存凭证）")
        top.pack(fill="x", padx=10, pady=8)
        fields = [("Project", self.project, 0, ["21MM", "24MM"]),
                  ("Version ID", self.version, 1, None),
                  ("Host", self.host, 2, None), ("Port", self.port, 3, None),
                  ("DB User", self.user, 4, None), ("DB Password", self.password, 5, None)]
        for label, var, pos, opts in fields:
            ttk.Label(top, text=label).grid(row=0, column=pos*2, padx=(8,2), pady=8)
            if opts:
                ttk.Combobox(top, textvariable=var, values=opts, state="readonly", width=7).grid(
                    row=0, column=pos*2+1, padx=(0,7))
            else:
                ttk.Entry(top, textvariable=var, width=13 if pos not in (2,5) else 18,
                          show="*" if pos==5 else "").grid(row=0, column=pos*2+1, padx=(0,7))
        ttk.Label(top, text="过滤").grid(row=1,column=0, padx=8, sticky="w")
        ttk.Combobox(top, textvariable=self.kind, values=["ALL","SCOPE","TABLE"],
                     state="readonly", width=10).grid(row=1,column=1,sticky="w")
        ttk.Label(top,text="审核状态").grid(row=1,column=2,sticky="e")
        ttk.Combobox(top,textvariable=self.status_filter,state="readonly",width=22,
            values=["ALL","PENDING","CONFIRMED","REJECTED","NEEDS_CLARIFICATION"]).grid(row=1,column=3,sticky="w")
        ttk.Button(top,text="加载候选 / 刷新",command=lambda:self._load(reset=True)).grid(row=1,column=4,columnspan=2,pady=5)
        ttk.Button(top,text="下一页",command=lambda:self._load(reset=False)).grid(row=1,column=6,columnspan=2)
        ttk.Label(top,text="只限本机管理员，不使用 WebUI Token。",
                  foreground="#555555").grid(row=1,column=8,columnspan=4,sticky="w")
        middle=ttk.Panedwindow(self.root,orient=tk.HORIZONTAL)
        middle.pack(fill="both",expand=True,padx=10,pady=5)
        left=ttk.Frame(middle)
        right=ttk.Frame(middle)
        middle.add(left,weight=1);middle.add(right,weight=3)
        self.tree=ttk.Treeview(left,columns=("kind","status","section","key"),show="headings",height=17)
        for name,title,width in (("kind","类型",70),("status","审核状态",126),
                                 ("section","章节",100),("key","Proposal Key",250)):
            self.tree.heading(name,text=title);self.tree.column(name,width=width,minwidth=70)
        self.tree.pack(side="left",fill="both",expand=True)
        sb=ttk.Scrollbar(left,orient="vertical",command=self.tree.yview)
        sb.pack(side="right",fill="y");self.tree.configure(yscrollcommand=sb.set)
        self.tree.bind("<<TreeviewSelect>>",self._selected)
        ttk.Label(right,text="证据原文与原生版面只读对照（本机显示）").pack(anchor="w")
        notebook=ttk.Notebook(right)
        notebook.pack(fill="both", expand=True)
        detail_tab=ttk.Frame(notebook)
        native_tab=ttk.Frame(notebook)
        gold_tab=ttk.Frame(notebook)
        notebook.add(detail_tab,text="提案 / 审核历史")
        notebook.add(native_tab,text="PDF/DOCX ↔ SQL 精确证据")
        notebook.add(gold_tab,text="Gold v3 质量报告")
        ttk.Button(gold_tab, text="打开本地人工复核 Gold v3（离线只读）",
                   command=self._evaluate_local_gold).pack(anchor="w", padx=5, pady=6)
        ttk.Label(gold_tab,
            text="仅统计已人工复核的留出集及各文档/复杂度子集；不证明人员身份，也不自动审批工程规则。").pack(anchor="w",padx=5)
        self.gold_report=scrolledtext.ScrolledText(gold_tab,wrap=tk.WORD,height=18,state="disabled")
        self.gold_report.pack(fill="both",expand=True,padx=5,pady=5)
        self.details=scrolledtext.ScrolledText(detail_tab,wrap=tk.WORD,height=16,state="disabled")
        self.details.pack(fill="both",expand=True)
        ttk.Button(native_tab,text="选择本地原始 PDF/DOCX 并验证 SHA + SQL 来源（只读）",
                   command=self._load_native_preview).pack(anchor="w",padx=4,pady=4)
        ttk.Label(native_tab,text="左：SQL 原文（对应片段高亮）   右：已证明唯一匹配的原生单元格；不代表表头已批准").pack(anchor="w")
        header_bar = ttk.Frame(native_tab)
        header_bar.pack(fill="x", padx=4)
        ttk.Label(header_bar, text="人工选择表头物理行 (如 0 或 0,1；仅 TABLE)：").pack(side="left")
        ttk.Entry(header_bar, textvariable=self.header_rows, width=18).pack(side="left")
        ttk.Label(header_bar, text="再次选择原文验证后显示逐列证据；不会自动审批").pack(side="left",padx=10)
        sides=ttk.Panedwindow(native_tab,orient=tk.HORIZONTAL)
        sides.pack(fill="both",expand=True)
        sql_box=ttk.Frame(sides)
        native_box=ttk.Frame(sides)
        sides.add(sql_box,weight=1)
        sides.add(native_box,weight=1)
        self.sql_preview=scrolledtext.ScrolledText(sql_box,wrap=tk.WORD,height=13,state="disabled")
        self.sql_preview.pack(fill="both",expand=True)
        self.sql_preview.tag_configure("matched",background="#ffe4a0")
        self.native_preview=scrolledtext.ScrolledText(native_box,wrap=tk.WORD,height=13,state="disabled")
        self.native_preview.pack(fill="both",expand=True)
        decision=ttk.LabelFrame(right,text="人工审核 · 明确提交才会写入 PostgreSQL")
        decision.pack(fill="x",pady=7)
        ttk.Label(decision,text="Reviewer ID").grid(row=0,column=0,sticky="w",padx=5,pady=4)
        ttk.Entry(decision,textvariable=self.reviewer,width=24).grid(row=0,column=1,sticky="w")
        ttk.Label(decision,text="状态").grid(row=0,column=2,padx=7)
        ttk.Combobox(decision,textvariable=self.state,state="readonly",width=22,
                     values=["NEEDS_CLARIFICATION","REJECTED","CONFIRMED"]).grid(row=0,column=3)
        ttk.Label(decision,text="断言").grid(row=1,column=0,padx=5,sticky="w")
        ttk.Combobox(decision,textvariable=self.assertion,width=30,
                     values=["","ACTION_ONLY","CONDITION_GOVERNS_ACTION",
                             "EXCEPTION_OVERRIDES_ACTION","HEADER_ROW_APPLIES"],
                     state="readonly").grid(row=1,column=1,sticky="w")
        ttk.Label(decision,text="表头 Unit Key").grid(row=1,column=2,padx=7)
        ttk.Entry(decision,textvariable=self.target,width=35).grid(row=1,column=3)
        ttk.Label(decision,text="审核理由").grid(row=2,column=0,padx=5,sticky="w")
        ttk.Entry(decision,textvariable=self.reason,width=87).grid(row=2,column=1,columnspan=3,sticky="ew",pady=4)
        ttk.Checkbutton(decision,text="我已完成数据库备份并核对当前原文",
                        variable=self.backup).grid(row=3,column=0,columnspan=3,sticky="w",padx=5,pady=7)
        self.submit=ttk.Button(decision,text="追加人工审核事件",command=self._submit)
        self.submit.grid(row=3,column=3,padx=5,pady=7,sticky="e")
        ttk.Label(self.root,textvariable=self.status_message,foreground="#304866").pack(
            side="bottom",fill="x",padx=12,pady=5)

    def _reset_assertion_if_not_confirmed(self)->None:
        if self.state.get() != "CONFIRMED":
            self.assertion.set("")
            self.target.set("")

    def _scope(self) -> tuple[str,int,dict[str,Any]]:
        version=int(self.version.get())
        project=self.project.get()
        validate_review_scope(project,version)
        port=int(self.port.get())
        if port<1 or port>65535 or not self.host.get().strip() or not self.user.get().strip():
            raise ValueError("数据库连接参数无效")
        password=self.password.get()
        if not password:
            raise ValueError("必须输入数据库密码")
        return project,version,dict(host=self.host.get().strip(),port=port,
            dbname="rag_"+project.lower(),user=self.user.get().strip(),password=password,
            connect_timeout=10,application_name="dic-stage1-v115-human-review")

    def _work(self, func:Callable[[],Any], callback:Callable[[Any],None]) -> None:
        if self._busy:return
        self._busy=True
        self.submit.configure(state="disabled")
        self.status_message.set("正在执行 PostgreSQL 查询；请稍候…")
        def worker():
            try:self._updates.put(("ok",(callback,func())))
            except Exception as exc:self._updates.put(("error",(type(exc).__name__,str(exc)[:280])))
        threading.Thread(target=worker,daemon=True).start()

    def _drain(self) -> None:
        try:
            while True:
                kind,value=self._updates.get_nowait()
                self._busy=False
                self.submit.configure(state="normal")
                if kind=="error":
                    self.status_message.set(f"查询失败：{value[0]}：{value[1]}")
                    messagebox.showerror("Stage 1 审核失败",f"{value[0]}: {value[1]}",parent=self.root)
                else:
                    callback,data=value
                    callback(data)
        except queue.Empty:pass
        if self.root.winfo_exists():self.root.after(120,self._drain)

    def _load(self,*,reset:bool)->None:
        try: project,version,connargs=self._scope()
        except (ValueError,TypeError) as exc:
            messagebox.showerror("参数错误",str(exc),parent=self.root);return
        after=None if reset else (self.rows[-1].key if self.rows else None)
        kind=None if self.kind.get()=="ALL" else self.kind.get()
        status_filter=None if self.status_filter.get()=="ALL" else self.status_filter.get()
        def run():
            import psycopg
            with psycopg.connect(**connargs) as con:
                with con.cursor() as cur:
                    cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                    return list_review_queue(cur,project=project,version_id=version,limit=50,
                                             after_key=after,kind=kind,status=status_filter)
        def done(rows):
            if reset:
                self.rows=[];self.selected=None
                self.tree.delete(*self.tree.get_children())
                self._display("请选择候选查看来源与审核记录")
            for r in rows:
                self.rows.append(r)
                self.tree.insert("", "end", values=(r.kind,r.status,r.section_id or "",r.key))
            self.status_message.set(f"已读取 {len(rows)} 条；当前窗口累计 {len(self.rows)} 条（只读）")
        self._work(run,done)

    def _selected(self,_event=None)->None:
        if self._busy:return
        keys=self.tree.selection()
        if not keys:return
        values=self.tree.item(keys[0],"values")
        if not values:return
        key=values[3]
        self.selected=None
        self._native_view_key=None
        self._reset_native_preview()
        self._display("正在加载证据，请等待确认本次加载完成…")
        try:project,version,connargs=self._scope()
        except (ValueError,TypeError) as exc:
            self.status_message.set(str(exc));return
        def run():
            import psycopg
            with psycopg.connect(**connargs) as con:
                with con.cursor() as cur:
                    cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                    return load_review_detail(cur,project=project,version_id=version,proposal_key=key)
        def done(detail):
            self.selected=detail
            import json
            text=(f"Proposal: {detail.item.key}\n证据摘要: {detail.item.digest}\n"
                  f"状态: {detail.item.status} | 最新事件: {detail.item.last_event_id}\n\n"
                  +json.dumps(detail.payload,ensure_ascii=False,indent=2)
                  +"\n\n===== 审核历史（最新在前）=====\n"
                  +json.dumps(detail.history,ensure_ascii=False,indent=2))
            self._display(text)
            self.status_message.set("已显示原文证据；需要核对实际式样书才能确认。")
        self._work(run,done)

    def _reset_native_preview(self) -> None:
        for widget in (self.sql_preview,self.native_preview):
            widget.configure(state="normal")
            widget.delete("1.0","end")
            widget.configure(state="disabled")

    def _load_native_preview(self) -> None:
        """Never conflate human approval with verbatim physical evidence."""
        detail=self.selected
        if detail is None:
            messagebox.showwarning("请选择候选","先从左侧选择并加载证据。",parent=self.root)
            return
        path=filedialog.askopenfilename(parent=self.root,title="选择此版本的可信原始文档",
            filetypes=[("Word / PDF", "*.docx *.pdf"), ("Word", "*.docx"), ("PDF", "*.pdf")])
        if not path:
            return
        try: project,version,connargs=self._scope()
        except (ValueError,TypeError) as exc:
            messagebox.showerror("数据库连接",str(exc),parent=self.root)
            return
        self._native_view_key=None
        self._reset_native_preview()
        expected_key=detail.item.key
        requested_headers = self.header_rows.get().strip()
        if requested_headers and detail.item.kind != "TABLE":
            messagebox.showwarning("表头证据", "只有 TABLE 审核提案可以选择表头物理行。", parent=self.root)
            return
        def run():
            import psycopg
            from app.engineering_intelligence.review_native_compare import (
                read_verified_native_context,compare_review_to_native)
            with psycopg.connect(**connargs) as con:
                with con.cursor() as cur:
                    cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                    # Both evidence sources are checked within one SQL snapshot.
                    fresh=load_review_detail(cur,project=project,version_id=version,proposal_key=expected_key)
                    if fresh.item.digest != detail.item.digest or fresh.item.last_event_id != detail.item.last_event_id:
                        raise ValueError("审核候选或历史状态已变化，请先刷新。")
                    report,contents,bindings=read_verified_native_context(
                        cur,project=project,version_id=version,source_path=path)
                    comparison = compare_review_to_native(fresh, report, contents, bindings)
                    header_info = None
                    if requested_headers:
                        if comparison.status != "EXACT_NATIVE_CELLS_CANDIDATE":
                            raise ValueError("物理表格与 SQL 审核提案未完整唯一匹配，禁止生成表头列映射")
                        from app.engineering_intelligence.table_header_review import (
                            preview_table_header_columns, redacted_header_review)
                        try:
                            header_rows = tuple(int(x) for x in requested_headers.split(","))
                        except ValueError as exc:
                            raise ValueError("表头行必须是逗号分隔的物理行整数") from exc
                        from app.engineering_intelligence.table_header_review import require_proposal_matches_header_table
                        table_names = {m.get("native_table") for m in comparison.matches
                                       if m.get("status") == "EXACT_NATIVE_SUBSPAN_CANDIDATE"}
                        if len(table_names) != 1:
                            raise ValueError("原生审核提案未唯一对应到一张物理表，不能选定表头")
                        table_id = table_names.pop()
                        require_proposal_matches_header_table(comparison, table_id)
                        header_info = redacted_header_review(preview_table_header_columns(
                            report, bindings, table_identity=table_id, header_rows=header_rows))
                    return comparison, header_info
        def done(value):
            comparison, header_info = value
            if not self.selected or self.selected.item.key != expected_key:
                return
            self._native_view_key=expected_key
            sql=self.sql_preview
            sql.configure(state="normal")
            sql.delete("1.0","end")
            sql.insert("1.0",comparison.sql_text)
            for link in comparison.matches:
                if (link['status']=="EXACT_NATIVE_SUBSPAN_CANDIDATE" and
                        comparison.sql_excerpt_start <= link['sql_start'] and
                        link['sql_end'] <= comparison.sql_excerpt_start + len(comparison.sql_text)):
                    left=link['sql_start']-comparison.sql_excerpt_start
                    right=link['sql_end']-comparison.sql_excerpt_start
                    sql.tag_add("matched",f"1.0+{left}c",f"1.0+{right}c")
            sql.configure(state="disabled")
            native=self.native_preview
            native.configure(state="normal")
            native.delete("1.0","end")
            import json
            summary=(f"核验结果：{comparison.status}\n"
                f"一致性：{comparison.exact_cells}/{comparison.reviewed_cells} 个候选片段\n"
                f"原文件 SHA-256: {comparison.source_sha256}\n"
                "注意：这是物理原文位置候选，人工审批仍须阅读正式式样书；未验证表头/合并/跨页语义。\n\n"
                + comparison.native_text + "\n\n匹配明细：\n"
                + json.dumps(comparison.matches,ensure_ascii=False,indent=2)
                + "\n\n结构警示：\n"+"; ".join(comparison.review_flags)
                + ("\n\n人工选择的表头逐列证据（仍需人工审核）：\n"
                   + json.dumps(header_info, ensure_ascii=False, indent=2) if header_info else ""))
            native.insert("1.0",summary)
            native.configure(state="disabled")
            self.status_message.set(f"本机来源对照完成：{comparison.status}。不自动确认工程规则。")
        self._work(run,done)

    def _evaluate_local_gold(self) -> None:
        """Offline report only. Never send gold source to the API, logs or DB."""
        path = filedialog.askopenfilename(parent=self.root,
            title="选择已人工双人复核的私有 Gold v3 JSON（本机）",
            filetypes=[("Gold JSON", "*.json")])
        if not path:
            return
        def run():
            from app.engineering_intelligence.stage1_evaluation_v3 import (
                evaluate_gold_v3_file, redacted_quality_report)
            return redacted_quality_report(evaluate_gold_v3_file(path))
        def done(result):
            import json
            self.gold_report.configure(state="normal")
            self.gold_report.delete("1.0", "end")
            # All returned fields are aggregated counts, source SHA and labels;
            # no original text, reviewer note, credential or full document path.
            self.gold_report.insert("1.0", json.dumps(result, ensure_ascii=False, indent=2))
            self.gold_report.configure(state="disabled")
            self.status_message.set(
                "Gold v3 离线测量完成；复核人身份未经系统验证，仍需真实样本与上线验收。")
        self._work(run, done)

    def _display(self,text:str)->None:
        self.details.configure(state="normal");self.details.delete("1.0","end")
        self.details.insert("1.0",text);self.details.configure(state="disabled")

    def _submit(self)->None:
        detail=self.selected
        if detail is None:
            messagebox.showwarning("未选择证据","先选择并查看候选证据。",parent=self.root);return
        if not self.backup.get():
            messagebox.showwarning("未确认备份","审核写入前必须确认备份。",parent=self.root);return
        try:project,version,connargs=self._scope()
        except (TypeError,ValueError) as exc:
            messagebox.showerror("参数错误",str(exc),parent=self.root);return
        reviewer=self.reviewer.get().strip();reason=self.reason.get().strip()
        status=self.state.get();assertion=self.assertion.get() or None;target=self.target.get().strip() or None
        if not reviewer or not reason:
            messagebox.showwarning("缺少审核信息","Reviewer ID 和审核理由必填",parent=self.root);return
        from app.engineering_intelligence.stage1_review import ReviewProposal,validate_review_decision
        proposal=ReviewProposal(detail.item.key,detail.item.kind,"",version,detail.item.digest,"",detail.payload)
        try:validate_review_decision(proposal,status=status,assertion=assertion,
                                      target_unit_key=target,reason=reason,reviewer=reviewer)
        except ValueError as exc:
            messagebox.showerror("审核内容不符合证据约束",str(exc),parent=self.root);return
        if not messagebox.askyesno("确认正式追加审核事件",
            f"项目 {project} / 版本 {version}\n状态 {status}\n证据 {detail.item.key[:16]}…\n"
            "这将向 PostgreSQL 追加不可覆盖的人工审核记录。继续？",parent=self.root):return
        def run():
            import psycopg
            with psycopg.connect(**connargs) as con:
                with con.cursor() as cur:
                    cur.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
                    check_database_project(cur,project)
                    event_id=record_review_event(cur,version_id=version,project=project,
                        proposal_key=detail.item.key,expected_evidence_digest=detail.item.digest,
                        expected_last_event_id=detail.item.last_event_id,
                        request_id=str(uuid.uuid4()),reviewer=reviewer,reason=reason,
                        status=status,assertion=assertion,target_unit_key=target)
                con.commit()
            return event_id
        def done(event_id):
            self.selected=None
            self.backup.set(False)
            self._display(f"审核事件 {event_id} 已提交。请重新刷新当前候选，检查最新审核历史。")
            self.status_message.set(f"审核事件 {event_id} 已提交；缓存已失效，必须重新加载。")
            self._load(reset=True)
        self._work(run,done)

    def _close(self)->None:
        self.password.set("")
        self.root.destroy()


def main()->None:
    root=tk.Tk()
    Stage1ReviewWindow(root)
    root.mainloop()


if __name__=="__main__":main()
