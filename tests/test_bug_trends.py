from datetime import date, datetime
from unittest.mock import MagicMock

import pytest

from app.ai.bug_trends import BugTrendService, JST, TrendRequest, read_trends, render_trends
from app.ai.retrieval import EnterpriseRetriever


@pytest.mark.parametrize("question", [
    "统计月份：2026-01 至 2026-03\n模块：Audio\n软件版本：v2",
    "Reporting months: 2026-01 to 2026-03\nModule: Audio\nSoftware version: v2",
    "集計対象月：2026-01 ～ 2026-03\nモジュール：Audio\nソフトウェアバージョン：v2",
])
def test_card_reads_explicit_months_and_filters(question):
    result = TrendRequest.parse(question, date(2026, 3, 17))
    assert list(result.periods()) == ["2026-01", "2026-02", "2026-03"]
    assert (result.module, result.version) == ("Audio", "v2")


def test_blank_period_defaults_to_six_complete_months_across_year_boundary():
    result = TrendRequest.parse("统计月份：\n模块：\n软件版本：", date(2026, 3, 17))
    assert len(list(result.periods())) == 6
    assert (result.first, result.last) == (date(2025, 9, 1), date(2026, 2, 1))


def test_uses_question_to_select_dimension_and_time_grain():
    request = TrendRequest.parse("2025年按月统计，并按问题类型整理，列出相关票据", date(2026, 3, 17))
    assert (request.grain, request.focus, request.show_tickets) == ("month", ("type",), True)
    assert list(request.periods()) == [f"2025-{month:02d}" for month in range(1, 13)]
    summer = TrendRequest.parse("统计2025年6月到8月新增BUG，按类型整理", date(2026, 3, 17))
    assert list(summer.periods()) == ["2025-06", "2025-07", "2025-08"]


def test_year_and_day_ranges_are_inclusive_and_leap_day_is_preserved():
    year = TrendRequest.parse("时间范围：2024 至 2026\n问题类型：Defect", date(2026, 3, 17))
    assert (year.grain, year.issue_type, list(year.periods())) == ("year", "Defect", ["2024", "2025", "2026"])
    day = TrendRequest.parse("时间范围：2024-02-28 至 2024-03-01", date(2026, 3, 17))
    assert list(day.periods()) == ["2024-02-28", "2024-02-29", "2024-03-01"]


@pytest.mark.parametrize("period", ["2026-13", "2026-02 至 2026-01", "2999-04", "2020-01 至 2026-01"])
def test_invalid_period_does_not_query_register(period):
    service = BugTrendService(MagicMock())
    answer, result, _ = service.answer([{"role": "user", "content": f"统计月份：{period}"}])
    with pytest.raises(ValueError):
        TrendRequest.parse(f"统计月份：{period}", date(2026, 3, 17))
    assert "请用卡片模板" in answer and not result.evidence
    assert not service.retriever.mock_calls


def test_sql_uses_only_bug_creation_date_bound_filters_and_zero_fills():
    retriever = MagicMock()
    retriever._qabug_where.side_effect = EnterpriseRetriever._qabug_where
    cur = retriever._connect.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value = [
        {"kind": "period", "bucket": date(2026, 1, 1), "count": 2},
        {"kind": "period", "bucket": date(2026, 3, 1), "count": 1},
        {"kind": "module", "key": "Audio", "count": 3, "rank": 1,
         "latest_count": 0, "prior_count": 2, "growth": 1, "decline": 1},
        {"kind": "version", "key": None, "count": 3, "rank": 1,
         "latest_count": 0, "prior_count": 2, "growth": 1, "decline": 1},
        {"kind": "total", "key": None, "count": 3, "undated": 1, "missing_module": 0, "missing_type": 0, "missing_version": 3,
         "latest_created": date(2026, 3, 12)},
    ]
    req = TrendRequest(date(2026, 1, 1), date(2026, 3, 1), "Audio%")
    now = datetime(2026, 3, 17, 9, tzinfo=JST)
    data = read_trends(retriever, req, now)
    sql, params = cur.execute.call_args.args
    assert params[2] == "BUG" and params[3:5] == ("Audio%", "Audio%") and params[-1] == now
    assert "Audio%" not in sql and "updated_at_source" not in sql
    assert params[-2] == datetime(2026, 1, 1, tzinfo=JST)
    assert data["periods"] == [("2026-01", 2), ("2026-02", 0), ("2026-03", 1)]
    report = render_trends(req, data, "zh", now)
    assert "变动 -2 条" in report and "最后一个时段尚未结束" in report
    assert "| 2026-02 | 0 |" in report and "缺软件版本 3 条" in report
    assert "| Audio | 2 | 0 | -2 |" in report
    assert "同向变动最大的分类为 模块分布 **Audio**" in report


def test_comparison_can_find_group_outside_top_period_distribution():
    retriever = MagicMock()
    retriever._qabug_where.side_effect = EnterpriseRetriever._qabug_where
    cur = retriever._connect.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value = [
        {"kind": "period", "bucket": date(2026, 1, 1), "count": 4},
        {"kind": "period", "bucket": date(2026, 2, 1), "count": 14},
        {"kind": "module", "key": "New module", "count": 10, "rank": 9,
         "latest_count": 10, "prior_count": 0, "growth": 1, "decline": 9},
        {"kind": "total", "key": None, "count": 18, "undated": 0, "missing_module": 0, "missing_type": 0, "missing_version": 0,
         "latest_created": date(2026, 2, 28)},
    ]
    req = TrendRequest(date(2026, 1, 1), date(2026, 2, 1))
    now = datetime(2026, 3, 17, 9, tzinfo=JST)
    report = read_trends(retriever, req, now)
    assert not report["module"]
    answer = render_trends(req, report, "en", now, request_id="bug-trend-test")
    assert "| New module | 0 | 10 | +10 |" in answer
    assert "bug-trend-test" in answer and "〔1〕" in answer


def test_report_discloses_trailing_months_after_latest_dated_record():
    req = TrendRequest(date(2026, 1, 1), date(2026, 3, 1))
    data = {"periods": [("2026-01", 4), ("2026-02", 0), ("2026-03", 0)],
            "total": 4, "latest_created": date(2026, 1, 25), "module": [], "version": [], "type": [], "changes": {},
            "undated": 0, "missing_module": 0, "missing_type": 0, "missing_version": 0}
    output = render_trends(req, data, "zh", datetime(2026, 4, 1, tzinfo=JST))
    assert "2026-01-25" in output and "变动" not in output


def test_type_focus_and_tickets_expose_only_matching_selected_evidence():
    req = TrendRequest.parse(
        "时间范围：2026-01-02\n问题类型：Defect\n问题：按类型整理并列出相关票据", date(2026, 3, 17))
    retriever = MagicMock()
    retriever._qabug_where.side_effect = EnterpriseRetriever._qabug_where
    cur = retriever._connect.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
    cur.fetchall.side_effect = [
        [{"kind": "period", "bucket": req.first, "count": 1},
         {"kind": "type", "key": "Defect", "count": 1, "rank": 1,
          "latest_count": 1, "prior_count": 0, "growth": 1, "decline": 1},
         {"kind": "total", "count": 1, "undated": 0, "missing_module": 0, "missing_version": 0,
          "missing_type": 0, "latest_created": req.first}],
        [{"issue_key": "DEMO-1", "created": req.first, "issue_type": "Defect",
          "source_file_name": r"C:\private\demo.xlsx"}],
    ]
    now = datetime(2026, 3, 17, tzinfo=JST)
    report = read_trends(retriever, req, now)
    assert cur.execute.call_args.args[1][-4:] == (
        "Defect", "Defect", datetime(2026, 1, 2, tzinfo=JST), datetime(2026, 1, 3, tzinfo=JST))
    output = render_trends(req, report, "zh", now)
    assert "按日新增 BUG" in output and "问题类型分布" in output and "模块分布" not in output
    assert "DEMO-1" in output and "demo.xlsx" in output and "private" not in output


def test_database_failure_is_not_rendered_as_zero():
    retriever = MagicMock()
    retriever._qabug_where.side_effect = RuntimeError("private connection diagnostic")
    service = BugTrendService(retriever)
    answer, result, _ = service.answer([{"role": "user", "content": "统计月份：2020-03"}])
    assert "未取得统计结果" in answer and "private connection" not in answer
    assert result.warnings and not result.evidence
