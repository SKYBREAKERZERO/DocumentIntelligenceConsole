from __future__ import annotations

import math
import tkinter as tk
from tkinter import BOTH, END, LEFT, RIGHT, X, Y, messagebox
from tkinter import ttk as tkttk
from typing import Any, Callable

import ttkbootstrap as ttk

from app.enterprise.postgres_service import QueryResult


class RecordEditorDialog(tk.Toplevel):
    """Generic row editor with nullable controls and PK-safe edit semantics."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        title: str,
        columns: list[dict[str, Any]],
        initial: dict[str, Any] | None = None,
        readonly_columns: set[str] | None = None,
    ) -> None:
        super().__init__(parent)
        self.title(title)
        self.transient(parent)
        self.grab_set()
        self.geometry("780x650")
        self.minsize(620, 480)
        self.result: dict[str, Any] | None = None
        self.columns = columns
        self.initial = initial or {}
        self.readonly_columns = readonly_columns or set()
        self.value_widgets: dict[str, tk.Widget] = {}
        self.null_vars: dict[str, tk.BooleanVar] = {}

        outer = ttk.Frame(self, padding=12)
        outer.pack(fill=BOTH, expand=True)

        canvas = tk.Canvas(outer, highlightthickness=0)
        ybar = tkttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        body = ttk.Frame(canvas)
        window_id = canvas.create_window((0, 0), window=body, anchor="nw")
        canvas.configure(yscrollcommand=ybar.set)
        canvas.pack(side=LEFT, fill=BOTH, expand=True)
        ybar.pack(side=RIGHT, fill=Y)

        def resize_body(_event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfigure(window_id, width=canvas.winfo_width())

        body.bind("<Configure>", resize_body)
        canvas.bind("<Configure>", resize_body)

        for row_index, col in enumerate(columns):
            name = col["column_name"]
            data_type = str(col.get("data_type") or "")
            nullable = col.get("is_nullable") == "YES"
            is_identity = col.get("is_identity") == "YES"
            generated = col.get("is_generated") not in {None, "NEVER", ""}
            readonly = name in self.readonly_columns or generated or (
                initial is None and is_identity
            )

            label_text = name
            if col.get("is_primary_key"):
                label_text += "  [PK]"
            label = ttk.Label(body, text=label_text)
            label.grid(row=row_index, column=0, sticky="nw", padx=(0, 8), pady=5)

            value = self.initial.get(name)
            long_type = data_type in {"text", "json", "jsonb", "xml", "bytea"}
            if long_type:
                widget: tk.Widget = tk.Text(body, height=4, wrap="word", font=("Cascadia Mono", 9))
                widget.insert("1.0", "" if value is None else str(value))
                widget.grid(row=row_index, column=1, sticky="nsew", pady=5)
            elif data_type == "boolean":
                widget = ttk.Combobox(body, values=["true", "false"], state="readonly")
                widget.set("" if value is None else str(value).lower())
                widget.grid(row=row_index, column=1, sticky="ew", pady=5)
            else:
                widget = ttk.Entry(body)
                if value is not None:
                    widget.insert(0, str(value))
                widget.grid(row=row_index, column=1, sticky="ew", pady=5)

            if readonly:
                try:
                    widget.configure(state="disabled")
                except tk.TclError:
                    pass

            self.value_widgets[name] = widget
            null_var = tk.BooleanVar(value=value is None and initial is not None)
            self.null_vars[name] = null_var
            null_check = ttk.Checkbutton(body, text="NULL", variable=null_var)
            null_check.grid(row=row_index, column=2, sticky="nw", padx=(8, 0), pady=5)
            if not nullable or readonly:
                null_check.configure(state="disabled")

        body.columnconfigure(1, weight=1)

        actions = ttk.Frame(outer)
        actions.pack(fill=X, pady=(10, 0))
        ttk.Button(actions, text="Cancel", command=self.destroy).pack(side=RIGHT)
        ttk.Button(
            actions,
            text="Save",
            command=self._save,
            bootstyle="success",
        ).pack(side=RIGHT, padx=(0, 8))

        self.bind("<Escape>", lambda _e: self.destroy())
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.wait_visibility()
        self.focus_set()

    def _read_widget(self, widget: tk.Widget) -> str:
        if isinstance(widget, tk.Text):
            return widget.get("1.0", END).rstrip("\n")
        return str(widget.get())

    def _save(self) -> None:
        result: dict[str, Any] = {}
        for col in self.columns:
            name = col["column_name"]
            widget = self.value_widgets[name]
            state = str(widget.cget("state")) if "state" in widget.keys() else "normal"
            if state == "disabled" and self.initial is None:
                continue
            if self.null_vars[name].get():
                result[name] = None
            else:
                value = self._read_widget(widget)
                # Empty text remains an empty string. NULL is explicit through the checkbox.
                result[name] = value
        self.result = result
        self.destroy()


class ChartDialog(tk.Toplevel):
    """Dependency-free bar/line/pie result visualization."""

    def __init__(self, parent: tk.Misc, result: QueryResult) -> None:
        super().__init__(parent)
        self.title("Query Result Chart")
        self.geometry("1050x720")
        self.minsize(760, 520)
        self.result = result

        outer = ttk.Frame(self, padding=10)
        outer.pack(fill=BOTH, expand=True)

        controls = ttk.Frame(outer)
        controls.pack(fill=X, pady=(0, 8))

        self.type_var = tk.StringVar(value="Bar")
        self.x_var = tk.StringVar(value=result.columns[0] if result.columns else "")
        self.y_var = tk.StringVar(value=result.columns[1] if len(result.columns) > 1 else "")

        ttk.Label(controls, text="Chart").pack(side=LEFT)
        ttk.Combobox(
            controls,
            textvariable=self.type_var,
            values=["Bar", "Line", "Pie"],
            state="readonly",
            width=10,
        ).pack(side=LEFT, padx=(6, 16))
        ttk.Label(controls, text="Category / X").pack(side=LEFT)
        ttk.Combobox(
            controls,
            textvariable=self.x_var,
            values=result.columns,
            state="readonly",
            width=24,
        ).pack(side=LEFT, padx=(6, 16))
        ttk.Label(controls, text="Value / Y").pack(side=LEFT)
        ttk.Combobox(
            controls,
            textvariable=self.y_var,
            values=result.columns,
            state="readonly",
            width=24,
        ).pack(side=LEFT, padx=(6, 10))
        ttk.Button(controls, text="Render", command=self.render, bootstyle="primary").pack(side=LEFT)

        self.canvas = tk.Canvas(outer, background="white", highlightthickness=1, highlightbackground="#cccccc")
        self.canvas.pack(fill=BOTH, expand=True)
        self.canvas.bind("<Configure>", lambda _e: self.render())
        self.after(60, self.render)

    @staticmethod
    def _as_number(value: Any) -> float | None:
        try:
            if value is None or value == "":
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    def _series(self) -> list[tuple[str, float]]:
        x = self.x_var.get()
        y = self.y_var.get()
        rows: list[tuple[str, float]] = []
        for row in self.result.rows[:80]:
            number = self._as_number(row.get(y))
            if number is None:
                continue
            rows.append((str(row.get(x, "")), number))
        return rows

    def render(self) -> None:
        self.canvas.delete("all")
        data = self._series()
        if not data:
            self.canvas.create_text(
                max(self.canvas.winfo_width() // 2, 200),
                max(self.canvas.winfo_height() // 2, 150),
                text="Choose a numeric Value / Y column.",
                fill="#555555",
            )
            return
        ctype = self.type_var.get()
        if ctype == "Pie":
            self._draw_pie(data)
        elif ctype == "Line":
            self._draw_line(data)
        else:
            self._draw_bar(data)

    def _bounds(self):
        w = max(self.canvas.winfo_width(), 600)
        h = max(self.canvas.winfo_height(), 420)
        return w, h, 70, 35, 45, 90

    def _draw_axes(self, max_value: float, min_value: float = 0.0):
        w, h, left, top, bottom, right = self._bounds()
        x0, y0 = left, h - bottom
        x1, y1 = w - right, top
        self.canvas.create_line(x0, y0, x1, y0, fill="#444")
        self.canvas.create_line(x0, y0, x0, y1, fill="#444")
        span = max(max_value - min_value, 1e-9)
        for i in range(6):
            frac = i / 5
            y = y0 - frac * (y0 - y1)
            value = min_value + frac * span
            self.canvas.create_line(x0 - 4, y, x1, y, fill="#e7e7e7")
            self.canvas.create_text(x0 - 8, y, text=f"{value:.3g}", anchor="e", fill="#555")
        return w, h, x0, y0, x1, y1, span

    def _draw_bar(self, data: list[tuple[str, float]]) -> None:
        values = [v for _, v in data]
        min_value = min(0.0, min(values))
        max_value = max(0.0, max(values))
        w, h, x0, y0, x1, y1, span = self._draw_axes(max_value, min_value)
        plot_w = x1 - x0
        n = len(data)
        step = plot_w / max(n, 1)
        zero_y = y0 - ((0.0 - min_value) / span) * (y0 - y1)
        for i, (label, value) in enumerate(data):
            cx = x0 + step * (i + 0.5)
            y = y0 - ((value - min_value) / span) * (y0 - y1)
            bw = max(3, step * 0.68)
            self.canvas.create_rectangle(cx - bw / 2, min(y, zero_y), cx + bw / 2, max(y, zero_y), fill="#4878a8", outline="")
            if n <= 24:
                self.canvas.create_text(cx, y0 + 8, text=label[:18], anchor="n", angle=45, fill="#444")

    def _draw_line(self, data: list[tuple[str, float]]) -> None:
        values = [v for _, v in data]
        min_value = min(values)
        max_value = max(values)
        if min_value == max_value:
            min_value -= 1
            max_value += 1
        w, h, x0, y0, x1, y1, span = self._draw_axes(max_value, min_value)
        plot_w = x1 - x0
        n = len(data)
        points = []
        for i, (label, value) in enumerate(data):
            x = x0 + (plot_w * i / max(n - 1, 1))
            y = y0 - ((value - min_value) / span) * (y0 - y1)
            points.extend([x, y])
            self.canvas.create_oval(x - 3, y - 3, x + 3, y + 3, fill="#4878a8", outline="")
            if n <= 18:
                self.canvas.create_text(x, y0 + 8, text=label[:16], anchor="n", angle=45, fill="#444")
        if len(points) >= 4:
            self.canvas.create_line(*points, fill="#4878a8", width=2)

    def _draw_pie(self, data: list[tuple[str, float]]) -> None:
        positive = [(k, v) for k, v in data if v > 0]
        total = sum(v for _, v in positive)
        if total <= 0:
            self.canvas.create_text(300, 220, text="Pie chart requires positive values.")
            return
        w = max(self.canvas.winfo_width(), 600)
        h = max(self.canvas.winfo_height(), 420)
        size = min(w * 0.55, h * 0.75)
        x0, y0 = 55, 45
        colors = ["#4878a8", "#f28e2b", "#59a14f", "#e15759", "#76b7b2", "#edc948", "#b07aa1", "#ff9da7"]
        start = 0.0
        for i, (label, value) in enumerate(positive[:12]):
            extent = value / total * 360.0
            self.canvas.create_arc(x0, y0, x0 + size, y0 + size, start=start, extent=extent, fill=colors[i % len(colors)], outline="white")
            ly = y0 + i * 24
            self.canvas.create_rectangle(x0 + size + 35, ly, x0 + size + 51, ly + 16, fill=colors[i % len(colors)], outline="")
            self.canvas.create_text(x0 + size + 58, ly + 8, anchor="w", text=f"{label[:30]}  {value:g}")
            start += extent


class ExplainPlanDialog(tk.Toplevel):
    """Graphical PostgreSQL EXPLAIN plan browser."""

    def __init__(self, parent: tk.Misc, plan: dict[str, Any]) -> None:
        super().__init__(parent)
        self.title("PostgreSQL Execution Plan")
        self.geometry("1200x760")
        self.minsize(780, 520)
        self.canvas = tk.Canvas(self, background="white")
        ybar = tkttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        xbar = tkttk.Scrollbar(self, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        ybar.grid(row=0, column=1, sticky="ns")
        xbar.grid(row=1, column=0, sticky="ew")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        root_plan = plan.get("Plan") or plan
        self._draw(root_plan)

    def _draw(self, root: dict[str, Any]) -> None:
        levels: dict[int, list[dict[str, Any]]] = {}
        edges: list[tuple[dict[str, Any], dict[str, Any]]] = []

        def visit(node: dict[str, Any], depth: int) -> None:
            levels.setdefault(depth, []).append(node)
            for child in node.get("Plans") or []:
                edges.append((node, child))
                visit(child, depth + 1)

        visit(root, 0)
        positions: dict[int, tuple[float, float]] = {}
        box_w, box_h = 260, 104
        for depth in sorted(levels):
            nodes = levels[depth]
            for i, node in enumerate(nodes):
                x = 45 + i * (box_w + 70)
                y = 40 + depth * (box_h + 80)
                positions[id(node)] = (x, y)
        for parent, child in edges:
            px, py = positions[id(parent)]
            cx, cy = positions[id(child)]
            self.canvas.create_line(px + box_w / 2, py + box_h, cx + box_w / 2, cy, arrow=tk.LAST, fill="#888", width=2)
        for depth in sorted(levels):
            for node in levels[depth]:
                x, y = positions[id(node)]
                self.canvas.create_rectangle(x, y, x + box_w, y + box_h, fill="#f7f8fa", outline="#89939f", width=1)
                node_type = str(node.get("Node Type", "Plan"))
                rel = str(node.get("Relation Name") or node.get("Index Name") or "")
                cost = f"Cost {node.get('Startup Cost', '?')}..{node.get('Total Cost', '?')}"
                rows = f"Rows {node.get('Plan Rows', '?')}"
                actual = ""
                if "Actual Total Time" in node:
                    actual = f"Actual {node.get('Actual Total Time')} ms / {node.get('Actual Rows')} rows"
                self.canvas.create_text(x + 10, y + 12, anchor="nw", text=node_type, font=("Segoe UI", 10, "bold"), fill="#243746")
                self.canvas.create_text(x + 10, y + 36, anchor="nw", text=rel[:36], fill="#555")
                self.canvas.create_text(x + 10, y + 58, anchor="nw", text=f"{cost} · {rows}", fill="#555")
                if actual:
                    self.canvas.create_text(x + 10, y + 80, anchor="nw", text=actual[:44], fill="#8a4b08")
        bbox = self.canvas.bbox("all")
        if bbox:
            self.canvas.configure(scrollregion=(bbox[0] - 20, bbox[1] - 20, bbox[2] + 40, bbox[3] + 40))


class ERDiagramFrame(ttk.Frame):
    """Interactive ER diagram with auto layout, drag, zoom, and pan."""

    def __init__(self, parent: tk.Misc, on_open_table: Callable[[str, str], None] | None = None) -> None:
        super().__init__(parent)
        self.on_open_table = on_open_table
        self.model: dict[str, Any] = {"tables": [], "foreign_keys": []}
        self.positions: dict[tuple[str, str], tuple[float, float]] = {}
        self.zoom_factor = 1.0
        self.drag_table: tuple[str, str] | None = None
        self.drag_offset = (0.0, 0.0)

        toolbar = ttk.Frame(self)
        toolbar.pack(fill=X, pady=(0, 5))
        ttk.Button(toolbar, text="Auto Layout", command=self.auto_layout).pack(side=LEFT)
        ttk.Button(toolbar, text="Zoom +", command=lambda: self.zoom(1.15)).pack(side=LEFT, padx=(6, 0))
        ttk.Button(toolbar, text="Zoom -", command=lambda: self.zoom(1 / 1.15)).pack(side=LEFT, padx=(6, 0))
        ttk.Button(toolbar, text="Reset", command=self.reset_view).pack(side=LEFT, padx=(6, 0))
        ttk.Label(toolbar, text="Drag tables · mouse wheel zoom · Shift+drag pan", bootstyle="secondary").pack(side=RIGHT)

        frame = ttk.Frame(self)
        frame.pack(fill=BOTH, expand=True)
        self.canvas = tk.Canvas(frame, background="#fbfbfc", highlightthickness=1, highlightbackground="#d8dde3")
        ybar = tkttk.Scrollbar(frame, orient="vertical", command=self.canvas.yview)
        xbar = tkttk.Scrollbar(frame, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        ybar.grid(row=0, column=1, sticky="ns")
        xbar.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<Double-Button-1>", self._double_click)
        self.canvas.bind("<MouseWheel>", self._mousewheel)
        self.canvas.bind("<Shift-ButtonPress-1>", self._pan_start)
        self.canvas.bind("<Shift-B1-Motion>", self._pan_move)

    def set_model(self, model: dict[str, Any]) -> None:
        self.model = model
        self.auto_layout()

    def auto_layout(self) -> None:
        tables = self.model.get("tables") or []
        cols = max(1, math.ceil(math.sqrt(max(len(tables), 1))))
        self.positions.clear()
        for i, table in enumerate(tables):
            row, col = divmod(i, cols)
            key = (table["schema"], table["table"])
            self.positions[key] = (55 + col * 330, 55 + row * 300)
        self.redraw()

    def reset_view(self) -> None:
        self.zoom_factor = 1.0
        self.canvas.xview_moveto(0)
        self.canvas.yview_moveto(0)
        self.redraw()

    def zoom(self, factor: float) -> None:
        self.zoom_factor = min(2.2, max(0.45, self.zoom_factor * factor))
        self.redraw()

    def _mousewheel(self, event) -> None:
        self.zoom(1.12 if event.delta > 0 else 1 / 1.12)

    def _pan_start(self, event) -> None:
        self.canvas.scan_mark(event.x, event.y)

    def _pan_move(self, event) -> None:
        self.canvas.scan_dragto(event.x, event.y, gain=1)

    def _table_dimensions(self, table: dict[str, Any]) -> tuple[float, float]:
        visible = min(len(table.get("columns") or []), 12)
        return 285.0, 52.0 + visible * 21.0 + (22.0 if len(table.get("columns") or []) > 12 else 0.0)

    def redraw(self) -> None:
        self.canvas.delete("all")
        s = self.zoom_factor
        table_map = {(t["schema"], t["table"]): t for t in self.model.get("tables") or []}

        # FKs first so table cards are above the lines.
        for fk in self.model.get("foreign_keys") or []:
            src = (fk["source_schema"], fk["source_table"])
            dst = (fk["target_schema"], fk["target_table"])
            if src not in self.positions or dst not in self.positions:
                continue
            sx, sy = self.positions[src]
            dx, dy = self.positions[dst]
            sw, sh = self._table_dimensions(table_map[src])
            dw, dh = self._table_dimensions(table_map[dst])
            x1 = (sx + sw / 2) * s
            y1 = (sy + sh / 2) * s
            x2 = (dx + dw / 2) * s
            y2 = (dy + dh / 2) * s
            self.canvas.create_line(x1, y1, x2, y2, arrow=tk.LAST, fill="#98a1aa", width=max(1, int(2 * s)), tags=("fk",))

        for key, table in table_map.items():
            x, y = self.positions.get(key, (40, 40))
            w, h = self._table_dimensions(table)
            x, y, w, h = x * s, y * s, w * s, h * s
            tag = f"table::{key[0]}::{key[1]}"
            self.canvas.create_rectangle(x, y, x + w, y + h, fill="white", outline="#7d8894", width=max(1, int(1.5 * s)), tags=(tag, "table_card"))
            self.canvas.create_rectangle(x, y, x + w, y + 34 * s, fill="#e9eef4", outline="#7d8894", tags=(tag, "table_card"))
            self.canvas.create_text(x + 10 * s, y + 17 * s, anchor="w", text=f"{key[0]}.{key[1]}", font=("Segoe UI", max(8, int(10 * s)), "bold"), tags=(tag, "table_card"))
            columns = table.get("columns") or []
            for i, col in enumerate(columns[:12]):
                marker = "PK" if col.get("pk") else "  "
                nullable = "" if not col.get("nullable") else "?"
                text = f"{marker}  {col['name']}  {col['type']}{nullable}"
                self.canvas.create_text(x + 10 * s, y + (47 + i * 21) * s, anchor="w", text=text[:48], font=("Cascadia Mono", max(7, int(8.6 * s))), fill="#334", tags=(tag, "table_card"))
            if len(columns) > 12:
                self.canvas.create_text(x + 10 * s, y + (47 + 12 * 21) * s, anchor="w", text=f"... +{len(columns) - 12} columns", fill="#777", tags=(tag, "table_card"))

        bbox = self.canvas.bbox("all")
        if bbox:
            self.canvas.configure(scrollregion=(bbox[0] - 80, bbox[1] - 80, bbox[2] + 180, bbox[3] + 180))

    def _table_at(self, x: float, y: float) -> tuple[str, str] | None:
        item = self.canvas.find_closest(self.canvas.canvasx(x), self.canvas.canvasy(y))
        if not item:
            return None
        for tag in self.canvas.gettags(item[0]):
            if tag.startswith("table::"):
                _, schema, table = tag.split("::", 2)
                return schema, table
        return None

    def _press(self, event) -> None:
        if event.state & 0x0001:  # Shift, handled by pan binding.
            return
        key = self._table_at(event.x, event.y)
        if key is None:
            return
        self.drag_table = key
        x, y = self.positions[key]
        self.drag_offset = (self.canvas.canvasx(event.x) / self.zoom_factor - x, self.canvas.canvasy(event.y) / self.zoom_factor - y)

    def _drag(self, event) -> None:
        if self.drag_table is None:
            return
        ox, oy = self.drag_offset
        self.positions[self.drag_table] = (
            self.canvas.canvasx(event.x) / self.zoom_factor - ox,
            self.canvas.canvasy(event.y) / self.zoom_factor - oy,
        )
        self.redraw()

    def _release(self, _event) -> None:
        self.drag_table = None

    def _double_click(self, event) -> None:
        key = self._table_at(event.x, event.y)
        if key and self.on_open_table:
            self.on_open_table(*key)
