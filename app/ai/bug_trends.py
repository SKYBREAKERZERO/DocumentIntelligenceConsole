"""Read-only BUG trend report with a fixed, reproducible counting policy."""
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from html import escape
import re
import time
import uuid

from app.ai.answer_policy import language
from app.ai.retrieval import EvidenceItem, RetrievalResult
from app.ai.retrieval_deadline import retrieval_budget

MODEL_ID = "document-intelligence-bug-trends"
JST = timezone(timedelta(hours=9))
DATE_TOKEN = r"\d{4}(?:-\d{2}(?:-\d{2})?)?"
DATE_RANGE = re.compile(rf"({DATE_TOKEN})(?:\s*(?:~|～|至|到|–|—|/|to|から|--)\s*({DATE_TOKEN}))?", re.I)
TICKET_REQUEST = re.compile(r"票据明细|相关票据|对应票据|列.{0,6}(?:票号|票据|工单)|チケット.{0,6}(?:一覧|表示)|(?:list|show)\s+(?:matching\s+)?(?:tickets|issues)", re.I)
PREVIOUS_SCOPE = re.compile(r"刚才|上述|上面|这个(?:范围|结果)|先ほど|この範囲|\b(?:previous|same period|above)\b", re.I)
FOCUS = {
    "type": r"按(?:问题)?类型|类型分布|哪些?类型|什么类型|类型.{0,5}最多|種別|by (?:issue )?type",
    "module": r"按模块|模块分布|哪些?模块|模块.{0,5}最多|モジュール別|by (?:module|component)",
    "version": r"按(?:软件)?版本|版本分布|哪些?版本|版本.{0,5}最多|バージョン別|by (?:software )?version",
}
TIME_FOCUS = {
    "day": r"按日|每日|日別|\bby day\b|\bdaily\b",
    "month": r"按月|每月|月別|\bby month\b|\bmonthly\b",
    "year": r"按年|每年|年別|\bby year\b|\byearly\b",
}
GRAIN = {
    "zh": {"day": "按日新增 BUG", "month": "按月新增 BUG", "year": "按年新增 BUG"},
    "ja": {"day": "日別の新規BUG", "month": "月別の新規BUG", "year": "年別の新規BUG"},
    "en": {"day": "New bugs by day", "month": "New bugs by month", "year": "New bugs by year"},
}
FIELDS = {
    "period": ("时间范围", "対象期間", "Reporting period", "统计月份", "集計対象月", "Reporting months"),
    "module": ("模块", "モジュール", "Module"),
    "version": ("软件版本", "ソフトウェアバージョン", "Software version"),
    "type": ("问题类型", "課題種別", "Issue type"),
}
LABELS = {
    "zh": {
        "invalid": "请用卡片模板填写时间范围，支持年份、月份或日期，例如 2025 至 2026、2026-01 至 2026-06、2026-06-01 至 2026-06-15。模块、软件版本和问题类型可留空。",
        "unavailable": "BUG 台账暂不可用，本次未取得统计结果。",
        "summary": "{start} 至 {end} 共记录 **{total} 条新建 BUG**。",
        "delta": "最近两个可比较时段：{prior_month} 为 {prior} 条，{last_month} 为 {last} 条，变动 {change:+d} 条{rate}。",
        "zero_base": "（上月为 0，百分比不适用）",
        "partial": "最后一个时段尚未结束，不参与环比。",
        "coverage": "筛选后的台账最近有创建记录的日期是 {latest}。之后的零记录时段不参与环比，以免把数据缺口读成下降。",
        "show": "查看统计明细", "hide": "收起", "period": "统计口径", "months": "按月新增 BUG",
        "module": "模块分布", "version": "软件版本分布", "key": "分类", "month": "月份", "count": "BUG 数量",
        "missing": "未登记", "all": "不限", "other": "其余分类", "no_rows": "所选月份和筛选条件下未查到带创建日期的记录。",
        "filters": "模块 {module}，软件版本 {version}，问题类型 {type}。",
        "type": "问题类型分布", "type_gap": "期内缺问题类型 {count} 条。",
        "tickets": "匹配票据", "ticket_policy": "最多列出最近 8 条匹配票据，统计结果按全部符合条件的记录计算。",
        "ticket": "票号", "created": "创建日期", "file": "导入来源", "period_key": "时段",
        "top_group": "{dimension}中，**{group}** 记录较多，共 {count} 条。{ref}",
        "method": "来源为本机 QA／BUG 台账。按原始创建日期（日本时间）统计当前可查询的 BUG 记录，包含零记录月份。",
        "gaps": "筛选范围内缺创建日期的记录 {undated} 条，无法归入月份。期内缺模块 {module} 条，缺软件版本 {version} 条。",
        "limits": "数量变化用于定位排查范围，无法单独判断缺陷率、根因或版本质量。",
        "change_heading": "相邻时段变化明细",
        "change_method": "同一批 BUG 按不同维度分组，各表的变化量不能相加。每表列出件数增减最明显的各两类。",
        "change": "变动",
        "source_id": "台账查询 {id} · 统计时间 {time}（日本时间）",
        "source_ref": "〔1〕",
        "latest_observed": "筛选后可见的最新创建日期：{latest}。该日期不代表所有期间的数据都已导入。",
        "priority": "同向变动最大的分类为 {dimension} **{group}**（{prior} → {last}，{change:+d} 条）。可先核查该分类的新增记录与导入覆盖。{ref}",
        "unknown_group": "{dimension}存在未登记分组的明显变动，不能进一步归到具体分类。",
    },
    "ja": {
        "invalid": "カードのテンプレートに対象期間を入力してください。年、月、日付を指定できます。例は 2026-01 ～ 2026-06 です。",
        "unavailable": "BUG台帳に接続できず、集計結果を取得できませんでした。",
        "summary": "{start} ～ {end} に登録された**新規BUGは{total}件**です。",
        "delta": "直近の比較可能な2期間は、{prior_month} が {prior}件、{last_month} が {last}件で、差は {change:+d}件{rate}です。",
        "zero_base": "（前月0件のため割合なし）",
        "partial": "最終期間は集計途中のため比較から除外します。",
        "coverage": "条件に一致する最新の作成日は {latest} です。それ以降の0件の期間は比較から除外します。",
        "show": "集計の詳細を表示", "hide": "閉じる", "period": "集計条件", "months": "月別の新規BUG",
        "module": "モジュール別", "version": "ソフトウェアバージョン別", "key": "分類", "month": "年月", "count": "件数",
        "missing": "未登録", "all": "指定なし", "other": "その他", "no_rows": "指定期間と条件に一致する作成日付きの記録はありません。",
        "filters": "モジュール {module}、ソフトウェアバージョン {version}、課題種別 {type}。",
        "type": "課題種別別", "type_gap": "期間内の課題種別未登録は{count}件です。",
        "tickets": "一致したチケット", "ticket_policy": "最新の一致チケットを最大8件表示します。集計は条件に一致する全件が対象です。",
        "ticket": "チケット番号", "created": "作成日", "file": "取り込み元", "period_key": "期間",
        "top_group": "{dimension}では **{group}** が上位で、{count}件です。{ref}",
        "method": "出典はローカルのQA／BUG台帳です。現在検索対象のBUGを元の作成日（日本時間）で集計し、0件の月も表示します。",
        "gaps": "条件内で作成日がなく月に割り当てられない記録は{undated}件です。期間内のモジュール未登録は{module}件、バージョン未登録は{version}件です。",
        "limits": "件数の変化だけで不具合率、原因、バージョン品質は判断できません。",
        "change_heading": "期間比較の内訳",
        "change_method": "各表は同じBUGを異なる軸で集計しています。増減は合算できません。各表に増減の上位2分類を示します。",
        "change": "増減",
        "source_id": "台帳照会 {id} · 集計時刻 {time}（日本時間）",
        "source_ref": "〔1〕",
        "latest_observed": "条件に一致する最新の作成日：{latest}。全期間の取り込み完了を示すものではありません。",
        "priority": "総件数と同方向の変化が最大だった分類は、{dimension}の **{group}**（{prior} → {last}、{change:+d}件）です。該当する新規記録と取り込み範囲を確認できます。{ref}",
        "unknown_group": "{dimension}の未登録グループにも変動があり、具体的な分類は特定できません。",
    },
    "en": {
        "invalid": "Use the card template and enter a year, month, date, or range, for example 2026-01 to 2026-06. Module, software version, and issue type are optional.",
        "unavailable": "The BUG register is unavailable. Statistics could not be retrieved.",
        "summary": "**{total} new BUG records** from {start} through {end}.",
        "delta": "Latest comparable periods: {prior_month} had {prior}, and {last_month} had {last}, a change of {change:+d}{rate}.",
        "zero_base": " (percentage unavailable because the baseline was zero)",
        "partial": "The final period is still in progress and is excluded from comparisons.",
        "coverage": "The latest creation date matching the filters is {latest}. Later zero-record periods are excluded from comparisons because coverage is uncertain.",
        "show": "Show report details", "hide": "Collapse", "period": "Counting policy", "months": "New bugs by month",
        "module": "By module", "version": "By software version", "key": "Group", "month": "Month", "count": "BUG count",
        "missing": "Unrecorded", "all": "Any", "other": "Other groups", "no_rows": "No dated records matched the selected months and filters.",
        "filters": "Module {module}, software version {version}, issue type {type}.",
        "type": "By issue type", "type_gap": "{count} dated records lack an issue type.",
        "tickets": "Matching tickets", "ticket_policy": "Shows up to the latest eight matching tickets. Statistics include all matching records.",
        "ticket": "Ticket ID", "created": "Created date", "file": "Import source", "period_key": "Period",
        "top_group": "{dimension}: **{group}** was among the largest groups, with {count} records. {ref}",
        "method": "Source: local QA/BUG register. Current searchable BUG records are grouped by original creation date in Japan time. Months with zero records are shown.",
        "gaps": "{undated} filtered records lack a creation date and cannot be assigned to a month. In the period, {module} lack a module and {version} lack a software version.",
        "limits": "Changes in record counts alone do not establish defect rates, root causes, or release quality.",
        "change_heading": "Changes between adjacent periods",
        "change_method": "The tables group the same bugs across different dimensions. Each shows up to two leading increases and decreases.",
        "change": "Change",
        "source_id": "Register query {id} · Run at {time} Japan time",
        "source_ref": "〔1〕",
        "latest_observed": "Latest observed creation date for these filters: {latest}. This date does not establish complete import coverage.",
        "priority": "The largest shift in the same direction as the total was {dimension} **{group}** ({prior} → {last}; {change:+d}). Check the new records and import coverage for that group first. {ref}",
        "unknown_group": "A material change in the unrecorded {dimension} group cannot be assigned to a specific category.",
    },
}


def _shift_month(day, offset):
    year, month = divmod(day.year * 12 + day.month - 1 + offset, 12)
    return date(year, month + 1, 1)


def _shift_period(day, grain, offset):
    if grain == "day":
        return day + timedelta(days=offset)
    return _shift_month(day, offset * (12 if grain == "year" else 1))


def _period_start(day, grain):
    return day if grain == "day" else day.replace(day=1, month=1 if grain == "year" else day.month)


def _period_key(day, grain):
    return day.isoformat()[:{"day": 10, "month": 7, "year": 4}[grain]]


def _normalize_dates(text):
    text = re.sub(
        r"(\d{4})\s*年\s*(\d{1,2})\s*月?\s*(至|到|～|~|から)\s*(\d{1,2})\s*月(?!\s*\d{1,2}\s*日)",
        lambda m: f"{m[1]}-{int(m[2]):02d}{m[3]}{m[1]}-{int(m[4]):02d}", text)
    def normalize(match):
        year, month, day = match.groups()
        return year + (f"-{int(month):02d}" if month else "") + (f"-{int(day):02d}" if day else "")
    return re.sub(r"(\d{4})\s*年(?:\s*(\d{1,2})\s*月(?:\s*(\d{1,2})\s*日)?)?", normalize, text)


@dataclass(frozen=True)
class TrendRequest:
    first: date
    last: date
    module: str = ""
    version: str = ""
    issue_type: str = ""
    grain: str = "month"
    focus: tuple[str, ...] = ()
    show_tickets: bool = False

    @classmethod
    def parse(cls, question, today):
        fields = {}
        focus_text = question
        for field, names in FIELDS.items():
            pattern = r"^[ \t]*(?:" + "|".join(map(re.escape, names)) + r")[ \t]*[:：][ \t]*([^\r\n]*)"
            match = re.search(pattern, question, re.I | re.M)
            if match:
                fields[field] = match.group(1).strip()
                focus_text = re.sub(pattern, "", focus_text, flags=re.I | re.M)
        grain = "month"
        first_this_month = today.replace(day=1)
        if "period" in fields and not fields["period"]:
            first, last = _shift_month(first_this_month, -6), _shift_month(first_this_month, -1)
        else:
            normalized = _normalize_dates(fields.get("period", question).strip())
            match = DATE_RANGE.fullmatch(normalized)
            if not match and "period" not in fields and (
                    TICKET_REQUEST.search(question) or any(re.search(pattern, question, re.I) for pattern in FOCUS.values())
                    or any(re.search(pattern, question, re.I) for pattern in TIME_FOCUS.values())
                    or re.search(r"趋势|统计|新增|件数|trend|count", question, re.I)):
                matches = list(re.finditer(rf"(?<![\d-]){DATE_RANGE.pattern}(?![\d-])", normalized, re.I))
                match = matches[0] if len(matches) == 1 else None
            if not match:
                raise ValueError("invalid reporting period")
            left, right = match[1], match[2] or match[1]
            if len(left) != len(right):
                raise ValueError("mixed date precision")
            grain = {4: "year", 7: "month", 10: "day"}[len(left)]
            suffix = {"year": "-01-01", "month": "-01", "day": ""}[grain]
            first, last = date.fromisoformat(left + suffix), date.fromisoformat(right + suffix)
        levels = tuple(TIME_FOCUS)
        if not 2000 <= first.year or not first <= last <= _period_start(today, grain):
            raise ValueError("invalid range")
        requested = [name for name, pattern in TIME_FOCUS.items() if re.search(pattern, focus_text, re.I)]
        if len(requested) == 1 and levels.index(requested[0]) < levels.index(grain):
            last_day = min(today, _shift_period(last, grain, 1) - timedelta(days=1))
            grain = requested[0]
            last = _period_start(last_day, grain)
        limit = {"day": 92, "month": 36, "year": 12}[grain]
        if _shift_period(first, grain, limit) <= last:
            raise ValueError("too many periods")
        module, version, issue_type = (fields.get(name, "") for name in ("module", "version", "type"))
        if max(map(len, (module, version, issue_type))) > 120:
            raise ValueError("filter too long")
        focus = tuple(name for name, pattern in FOCUS.items() if re.search(pattern, focus_text, re.I))
        return cls(first, last, module, version, issue_type, grain, focus, bool(TICKET_REQUEST.search(focus_text)))

    def periods(self):
        day = self.first
        while day <= self.last:
            yield _period_key(day, self.grain)
            day = _shift_period(day, self.grain, 1)


def read_trends(retriever, request, now):
    start = datetime.combine(request.first, datetime.min.time(), JST)
    end = min(datetime.combine(_shift_period(request.last, request.grain, 1), datetime.min.time(), JST), now)
    where, params = retriever._qabug_where({"record_type": "BUG"})
    filters = f"""{where}
          AND (%s = '' OR LOWER(BTRIM(r.component)) = LOWER(%s))
          AND (%s = '' OR LOWER(BTRIM(r.software_version)) = LOWER(%s))
          AND (%s = '' OR LOWER(BTRIM(r.issue_type)) = LOWER(%s))"""
    filter_values = (*params, request.module, request.module, request.version, request.version,
                     request.issue_type, request.issue_type)
    last_complete = _shift_period(_period_start(now.date(), request.grain), request.grain, -1)
    # Columns are fixed; every field supplied by the user is a bound value.
    with retrieval_budget(12), retriever._connect("QABUG") as conn, conn.cursor() as cursor:
        if request.show_tickets:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        cursor.execute(f"""
            WITH settings AS (
                SELECT %s::text AS grain, %s::interval AS step
            ), matched AS (
                SELECT r.created_at_source, r.component, r.software_version, r.issue_type
                FROM public.qabug_records r
                WHERE {filters}
                  AND (r.created_at_source IS NULL OR r.created_at_source < %s)
            ), coverage AS (
                SELECT MAX(created_at_source AT TIME ZONE 'Asia/Tokyo')::date AS latest_created FROM matched
            ), comparison AS (
                SELECT LEAST(%s::date, %s::date,
                             date_trunc(s.grain, latest_created::timestamp)::date) AS last_bucket, s.step
                FROM coverage CROSS JOIN settings s WHERE latest_created IS NOT NULL
            ), selected AS (
                SELECT date_trunc(s.grain, r.created_at_source AT TIME ZONE 'Asia/Tokyo')::date AS bucket,
                       NULLIF(BTRIM(r.component), '') AS component,
                       NULLIF(BTRIM(r.software_version), '') AS version,
                       NULLIF(BTRIM(r.issue_type), '') AS issue_type
                FROM matched r CROSS JOIN settings s
                WHERE r.created_at_source IS NULL OR (r.created_at_source >= %s AND r.created_at_source < %s)
            ), grouped AS (
                SELECT CASE GROUPING(bucket, component, version, issue_type)
                         WHEN 7 THEN 'period' WHEN 11 THEN 'module'
                         WHEN 13 THEN 'version' WHEN 14 THEN 'type' ELSE 'total' END AS kind,
                       bucket, COALESCE(component, version, issue_type) AS key,
                       COUNT(bucket) AS count,
                       COUNT(*) FILTER (WHERE bucket IS NULL) AS undated,
                       COUNT(bucket) FILTER (WHERE component IS NULL) AS missing_module,
                       COUNT(bucket) FILTER (WHERE version IS NULL) AS missing_version,
                       COUNT(bucket) FILTER (WHERE issue_type IS NULL) AS missing_type,
                       COUNT(*) FILTER (WHERE bucket = c.last_bucket) AS latest_count,
                       COUNT(*) FILTER (WHERE bucket = c.last_bucket - c.step) AS prior_count
                FROM selected LEFT JOIN comparison c ON true
                GROUP BY GROUPING SETS ((bucket), (component), (version), (issue_type), ())
            ), ranked AS (
                SELECT *, ROW_NUMBER() OVER (PARTITION BY kind ORDER BY count DESC, key) AS rank,
                       ROW_NUMBER() OVER (PARTITION BY kind ORDER BY latest_count - prior_count DESC, key) AS growth,
                       ROW_NUMBER() OVER (PARTITION BY kind ORDER BY latest_count - prior_count, key) AS decline
                FROM grouped
                WHERE kind = 'total' OR count > 0
            )
            SELECT kind, bucket, key, count, undated, missing_module, missing_version, missing_type, rank,
                   latest_count, prior_count, growth, decline,
                   CASE WHEN kind = 'total' THEN (SELECT latest_created FROM coverage) END AS latest_created
            FROM ranked
            WHERE kind IN ('period', 'total') OR rank <= 8
              OR (growth <= 2 AND latest_count > prior_count)
              OR (decline <= 2 AND latest_count < prior_count)
            ORDER BY kind, rank
        """, (request.grain, f"1 {request.grain}", *filter_values, now,
              request.last, last_complete, start, end))
        rows = cursor.fetchall()
        tickets = []
        if request.show_tickets:
            cursor.execute(f"""
                SELECT r.issue_key, (r.created_at_source AT TIME ZONE 'Asia/Tokyo')::date AS created,
                       r.issue_type, sf.source_file_name
                FROM public.qabug_records r
                LEFT JOIN public.qabug_source_files sf ON sf.id = r.source_file_id
                WHERE {filters} AND r.created_at_source >= %s AND r.created_at_source < %s
                ORDER BY r.created_at_source DESC, r.issue_key
                LIMIT 8
            """, (*filter_values, start, end))
            tickets = cursor.fetchall()
    total = next(row for row in rows if row["kind"] == "total")
    counts = {_period_key(row["bucket"], request.grain): row["count"]
              for row in rows if row["kind"] == "period"}
    dimensions = {name: [row for row in rows if row["kind"] == name]
                  for name in ("module", "version", "type")}
    changes = {}
    for name, groups in dimensions.items():
        changes[name] = [(row["key"], row["prior_count"], row["latest_count"]) for row in groups
                         if (row["growth"] <= 2 and row["latest_count"] > row["prior_count"])
                         or (row["decline"] <= 2 and row["latest_count"] < row["prior_count"])]
        changes[name].sort(key=lambda row: -abs(row[2] - row[1]))
    return {
        "periods": [(period, counts.get(period, 0)) for period in request.periods()],
        **{name: [(row["key"], row["count"]) for row in groups if row["rank"] <= 8]
           for name, groups in dimensions.items()},
        "changes": changes,
        "total": total["count"], "undated": total["undated"],
        "missing_module": total["missing_module"], "missing_version": total["missing_version"],
        "latest_created": total["latest_created"],
        "missing_type": total["missing_type"], "tickets": tickets,
    }


def _cell(value):
    # Keep register-supplied group names inert in Markdown.
    return re.sub(r"[\\`*_\[\]<>!|]", r"\\\g<0>", " ".join(str(value).split()))[:160]


def render_trends(request, report, lang, now, *, request_id=""):
    labels = LABELS[lang]
    periods = report["periods"]
    dimensions = request.focus or ("module", "version", "type")
    ref = labels["source_ref"]
    summary = labels["summary"].format(start=periods[0][0], end=periods[-1][0], total=report["total"])
    summary += f" {ref}"
    current = _period_key(now.date(), request.grain)
    latest = report["latest_created"]
    last_dated_period = _period_key(latest, request.grain) if latest else ""
    complete = [(period, count) for period, count in periods if period < current and period <= last_dated_period]
    comparison_details = []
    if len(complete) >= 2:
        (prior_month, prior), (last_month, last) = complete[-2:]
        change = last - prior
        rate = f" ({(last - prior) / prior * 100:+.1f}%)" if prior else labels["zero_base"]
        summary += "\n\n" + labels["delta"].format(
            prior_month=prior_month, last_month=last_month, prior=prior, last=last, change=change, rate=rate) + f" {ref}"
        leading = []
        for dimension in dimensions:
            groups = report["changes"].get(dimension, [])
            if not groups:
                continue
            heading = labels[dimension]
            table_rows = [f"#### {heading}", "",
                          f"| {labels['key']} | {prior_month} | {last_month} | {labels['change']} |",
                          "| --- | ---: | ---: | ---: |"]
            for group, before, after in groups:
                name = _cell(group) if group is not None else labels["missing"]
                table_rows.append(f"| {name} | {before} | {after} | {after - before:+d} |")
                if change * (after - before) > 0:
                    leading.append((abs(after - before), heading, group, before, after))
            comparison_details.append("\n".join(table_rows))
        if leading:
            _, dimension, group, before, after = max(leading, key=lambda entry: entry[0])
            if group is None:
                summary += "\n\n" + labels["unknown_group"].format(dimension=dimension) + f" {ref}"
            else:
                summary += "\n\n" + labels["priority"].format(
                    dimension=dimension, group=escape(_cell(group), quote=False),
                    prior=before, last=after, change=after - before, ref=ref)
    if periods[-1][0] == current:
        summary += "\n\n" + labels["partial"]
    if latest and periods[-1][0] > last_dated_period:
        summary += "\n\n" + labels["coverage"].format(latest=latest.isoformat())
    if not report["total"]:
        summary += "\n\n" + labels["no_rows"]
    elif request.focus:
        for dimension in request.focus:
            top = next(((group, count) for group, count in report[dimension] if group is not None), None)
            if top:
                group, count = top
                summary += "\n\n" + labels["top_group"].format(
                    dimension=labels[dimension], group=escape(_cell(group), quote=False), count=count, ref=ref)

    def table(heading, column, rows):
        lines = [f"### {heading}", "", f"| {column} | {labels['count']} |", "| --- | ---: |"]
        lines.extend(f"| {_cell(name) if name is not None else labels['missing']} | {count} |" for name, count in rows)
        return "\n".join(lines)

    details = [
        f"### {labels['period']}",
        f"{ref} " + labels["source_id"].format(id=request_id, time=now.strftime("%Y-%m-%d %H:%M:%S")),
        labels["filters"].format(module=_cell(request.module) or labels["all"],
                                 version=_cell(request.version) or labels["all"],
                                 type=_cell(request.issue_type) or labels["all"]),
        labels["method"],
        labels["gaps"].format(undated=report["undated"], module=report["missing_module"], version=report["missing_version"]),
        labels["type_gap"].format(count=report["missing_type"]),
        table(GRAIN[lang][request.grain], labels["month"] if request.grain == "month" else labels["period_key"], periods),
    ]
    if latest:
        details.insert(-1, labels["latest_observed"].format(latest=latest.isoformat()))
    if comparison_details:
        details.extend([f"### {labels['change_heading']}", labels["change_method"], *comparison_details])
    for dimension in dimensions:
        rows = report[dimension]
        if rows:
            remainder = report["total"] - sum(count for _, count in rows)
            if remainder:
                rows = [*rows, (labels["other"], remainder)]
            details.append(table(labels[dimension], labels["key"], rows))
    if request.show_tickets:
        ticket_rows = [f"### {labels['tickets']}", "", labels["ticket_policy"], "",
                       f"| {labels['ticket']} | {labels['created']} | {labels['type']} | {labels['file']} |",
                       "| --- | --- | --- | --- |"]
        for row in report["tickets"]:
            file_name = re.split(r"[/\\]", row["source_file_name"] or "")[-1]
            values = (row["issue_key"], row["created"], row["issue_type"], file_name)
            ticket_rows.append("| " + " | ".join(_cell(value) if value else labels["missing"] for value in values) + " |")
        details.append("\n".join(ticket_rows) if report["tickets"] else
                       f"### {labels['tickets']}\n\n{labels['no_rows']}")
    details.append(labels["limits"])
    return (f'{summary}\n\n<details type="spec_evidence" collapse="{labels["hide"]}">\n'
            f'<summary>{labels["show"]}</summary>\n\n'
            + escape("\n\n".join(details), quote=False) + "\n</details>")


class BugTrendService:
    def __init__(self, retriever):
        self.retriever = retriever

    def answer(self, messages, **kwargs):
        results = []
        started = time.monotonic()
        output = "".join(self.answer_stream(messages, on_retrieval=results.append, **kwargs))
        return output, results[0], int((time.monotonic() - started) * 1000)

    def answer_stream(self, messages, *, model=None, scope="AUTO", current_scope="", on_retrieval=None):
        user_turns = [item["content"] for item in messages if item["role"] == "user"]
        question = user_turns[-1] if user_turns else ""
        lang = language(question)
        labels = LABELS[lang]
        now = datetime.now(JST)
        request_id = "bug-trend-" + uuid.uuid4().hex[:12]
        evidence, warnings = (), ()
        try:
            request = TrendRequest.parse(question, now.date())
        except (ValueError, OverflowError):
            request = None
            if PREVIOUS_SCOPE.search(question) and TICKET_REQUEST.search(question) and not re.search(r"\d", question):
                for prior in reversed(user_turns[:-1]):
                    try:
                        request = TrendRequest.parse(prior + "\n" + question, now.date())
                        break
                    except ValueError:
                        continue
        if request is None:
            output = labels["invalid"]
        else:
            try:
                report = read_trends(self.retriever, request, now)
            except Exception:
                output = labels["unavailable"]
                warnings = ("BUG register aggregation unavailable",)
            else:
                output = render_trends(request, report, lang, now, request_id=request_id)
                evidence = (EvidenceItem(evidence_id="1", scope="QABUG", source_type="ISSUE_STATS",
                                         content="QA/BUG register · original creation date"),)
        if on_retrieval:
            on_retrieval(RetrievalResult(evidence=evidence, resolved_scopes=("QABUG",), route="BUG_TRENDS", request_id=request_id,
                                         warnings=warnings, tools=("issue_statistics",) if evidence else ()))
        yield output
