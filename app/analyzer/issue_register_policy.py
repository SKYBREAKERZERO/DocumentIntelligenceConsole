from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from app.model.document import Document


@dataclass(frozen=True, slots=True)
class IssueHeaderMatch:
    canonical_by_index: tuple[str, ...]
    recognized_fields: tuple[str, ...]
    confidence: float


class IssueRegisterPolicy:
    """Detect and normalize Jira/QA/BUG spreadsheet registers.

    Issue registers are engineering knowledge artefacts, but they are not
    SpecificationVersion masters.  Each spreadsheet data row is preserved as an
    independent RAG unit with canonical issue metadata so the AI layer can use
    exact SQL filters for keys/status/priority while retaining Qdrant semantic
    search for free-text problem descriptions.
    """

    ROLE = "ISSUE_REGISTER"
    ARTIFACT_TYPE = "ISSUE_REGISTER"
    QA_INDEX_POLICY = "INCLUDE"

    # Canonical issue fields.  Header aliases intentionally cover common Jira
    # exports plus Chinese/Japanese manually maintained QA sheets.
    _ALIASES: dict[str, tuple[str, ...]] = {
        "issue_key": (
            "issue key", "issuekey", "key", "jira key", "ticket key", "ticket id",
            "bug id", "bug no", "bug no.", "qa id", "defect id", "problem id",
            "課題キー", "課題 key", "チケット", "チケットid", "バグid", "バグ番号",
            "问题编号", "問題編號", "缺陷编号", "缺陷編號", "bug编号", "qa编号",
        ),
        "issue_type": (
            "issue type", "issuetype", "type", "ticket type", "defect type",
            "種別", "課題タイプ", "課題種別", "类型", "類型", "问题类型", "缺陷类型",
        ),
        "status": (
            "status", "state", "workflow status", "ステータス", "状態", "状态", "狀態",
        ),
        "priority": (
            "priority", "severity", "重要度", "優先度", "优先级", "優先級", "严重度", "嚴重度",
        ),
        "summary": (
            "summary", "title", "subject", "件名", "概要", "タイトル", "标题", "標題", "摘要", "问题概要",
        ),
        "description": (
            "description", "details", "detail", "problem", "symptom", "phenomenon",
            "説明", "詳細", "現象", "内容", "问题描述", "問題描述", "缺陷描述", "现象", "現象",
        ),
        "root_cause": (
            "root cause", "rootcause", "cause", "原因", "原因分析", "根因", "根本原因", "原因解析", "真因",
        ),
        "steps_to_reproduce": (
            "steps to reproduce", "reproduction steps", "reproduce steps", "repro steps", "repro",
            "再現手順", "再现步骤", "重现步骤", "復現步驟", "复现步骤",
        ),
        "expected_result": (
            "expected result", "expected", "期待結果", "期待値", "预期结果", "預期結果",
        ),
        "actual_result": (
            "actual result", "actual", "実際結果", "実績", "实际结果", "實際結果",
        ),
        "resolution": (
            "resolution", "fix", "solution", "countermeasure", "対応", "対策", "解決策", "解决方案", "修复方案", "處置",
        ),
        "component": (
            "component", "components", "module", "subsystem", "機能", "モジュール", "コンポーネント", "模块", "模塊", "组件", "組件",
        ),
        "assignee": (
            "assignee", "owner", "responsible", "担当", "担当者", "负责人", "負責人", "责任人", "責任人",
        ),
        "reporter": (
            "reporter", "reported by", "creator", "起票者", "報告者", "创建人", "建立者", "提出人",
        ),
        "fix_version": (
            "fix version", "fix versions", "fixversion", "target version", "修正version", "修正版", "修复版本", "修復版本", "目标版本",
        ),
        "affects_version": (
            "affects version", "affected version", "found version", "発生version", "対象version", "影响版本", "影響版本", "发现版本",
        ),
        "labels": (
            "labels", "label", "tags", "tag", "ラベル", "タグ", "标签", "標籤",
        ),
        "created": (
            "created", "created at", "create date", "登録日", "作成日", "创建时间", "建立時間", "创建日期",
        ),
        "updated": (
            "updated", "updated at", "update date", "更新日", "更新时间", "更新時間", "更新日期",
        ),
        "linked_issues": (
            "linked issues", "linked issue", "links", "related issues", "関連課題", "関連bug", "关联问题", "關聯問題", "关联bug",
        ),
        "test_case": (
            "test case", "testcase", "case id", "test id", "テストケース", "試験項目", "测试用例", "測試用例", "用例编号",
        ),
        "qa_result": (
            "qa result", "test result", "result", "判定", "試験結果", "测试结果", "測試結果", "qa结果",
        ),
        "environment": (
            "environment", "env", "platform", "環境", "环境", "環境情報", "测试环境", "測試環境",
        ),
    }


    FIELD_LABELS: dict[str, str] = {
        "issue_key": "Issue Key",
        "issue_type": "Issue Type",
        "status": "Status",
        "priority": "Priority",
        "summary": "Summary",
        "description": "Description",
        "root_cause": "Root Cause",
        "steps_to_reproduce": "Steps to Reproduce",
        "expected_result": "Expected Result",
        "actual_result": "Actual Result",
        "resolution": "Resolution",
        "component": "Component",
        "assignee": "Assignee",
        "reporter": "Reporter",
        "fix_version": "Fix Version",
        "affects_version": "Affects Version",
        "labels": "Labels",
        "created": "Created",
        "updated": "Updated",
        "linked_issues": "Linked Issues",
        "test_case": "Test Case",
        "qa_result": "QA Result",
        "environment": "Environment",
    }

    _FILE_HINT = re.compile(
        r"(?:jira|\bbug\b|\bqa\b|defect|issue|ticket|障害|不具合|バグ|課題|缺陷|问题单|問題單|测试问题|測試問題)",
        re.I,
    )

    @staticmethod
    def _norm(value: Any) -> str:
        text = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
        text = re.sub(r"[_\-/:：|（）()\[\]]+", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    @classmethod
    def canonical_header(cls, value: Any) -> str:
        normalized = cls._norm(value)
        if not normalized:
            return ""
        for canonical, aliases in cls._ALIASES.items():
            for alias in aliases:
                alias_norm = cls._norm(alias)
                if normalized == alias_norm:
                    return canonical
        # Slightly permissive suffix/prefix match for exports such as
        # "JIRA Issue Key" or "Priority / Severity" without matching arbitrary text.
        for canonical, aliases in cls._ALIASES.items():
            for alias in aliases:
                alias_norm = cls._norm(alias)
                if len(alias_norm) >= 5 and (
                    normalized.endswith(" " + alias_norm)
                    or normalized.startswith(alias_norm + " ")
                ):
                    return canonical
        return ""

    @classmethod
    def detect_headers(
        cls,
        header_cells: Sequence[Any],
        *,
        file_name: str = "",
        sheet_name: str = "",
    ) -> IssueHeaderMatch | None:
        canonical = tuple(cls.canonical_header(v) for v in header_cells)
        recognized = tuple(dict.fromkeys(v for v in canonical if v))
        if not recognized:
            return None
        core = {"issue_key", "status", "summary", "description", "issue_type", "priority", "resolution"}
        core_count = len(core.intersection(recognized))
        has_identity = "issue_key" in recognized
        has_status_or_text = bool({"status", "summary", "description"}.intersection(recognized))
        hinted = bool(cls._FILE_HINT.search(f"{file_name} {sheet_name}"))
        # Strong table shape: key + two useful fields; or a clearly named Jira/QA
        # workbook with at least three recognized issue columns.
        detected = (has_identity and has_status_or_text and len(recognized) >= 3) or (
            hinted and len(recognized) >= 3 and core_count >= 2
        )
        if not detected:
            return None
        confidence = min(1.0, 0.45 + len(recognized) * 0.06 + (0.18 if has_identity else 0.0) + (0.10 if hinted else 0.0))
        return IssueHeaderMatch(canonical, recognized, round(confidence, 3))

    @classmethod
    def row_metadata(
        cls,
        header_match: IssueHeaderMatch,
        row_cells: Sequence[Any],
    ) -> dict[str, str]:
        result: dict[str, str] = {}
        for idx, canonical in enumerate(header_match.canonical_by_index):
            if not canonical or idx >= len(row_cells):
                continue
            value = unicodedata.normalize("NFKC", str(row_cells[idx] or "")).strip()
            if not value:
                continue
            if canonical in result and result[canonical] != value:
                result[canonical] = f"{result[canonical]} | {value}"
            else:
                result[canonical] = value
        return result


    @classmethod
    def serialize_row(cls, metadata: dict[str, str], *, row_number: int | None = None) -> str:
        parts: list[str] = []
        for key in cls.FIELD_LABELS:
            value = str(metadata.get(key) or "").strip()
            if value:
                parts.append(f"{cls.FIELD_LABELS[key]}: {value}")
        prefix = f"[Issue row {row_number}] " if row_number is not None else "[Issue] "
        return prefix + " | ".join(parts) if parts else ""

    @classmethod
    def apply_document(cls, document: Document, *, fields: Iterable[str] = (), confidence: float = 1.0) -> None:
        metadata = document.metadata
        metadata.update(
            {
                "document_role": cls.ROLE,
                "artifact_type": cls.ARTIFACT_TYPE,
                "qa_index_policy": cls.QA_INDEX_POLICY,
                "issue_register_detected": True,
                "issue_register_fields": list(dict.fromkeys(str(x) for x in fields if str(x))),
                "issue_register_confidence": float(confidence),
                "specification_classifier_status": "ISSUE_REGISTER",
                "specification_version_status": "NOT_APPLICABLE",
                "specification_identity_source": "issue_register_policy",
            }
        )
        # Issue registers are not specification masters, even when a Jira export
        # filename contains a number that resembles a specification code.
        for key in (
            "specification_code",
            "specification_key",
            "specification_name",
            "specification_version",
            "specification_version_source",
            "specification_rule_id",
        ):
            metadata[key] = None
