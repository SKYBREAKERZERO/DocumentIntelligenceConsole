from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

QABUG_JSON_SCHEMA_VERSION = "1.0.0"
DEFAULT_EMBEDDING_MODEL = "BAAI/bge-m3"
DEFAULT_EMBEDDING_VERSION = "current-index-v1"
DEFAULT_VECTOR_DIMENSION = 1024
DEFAULT_QDRANT_COLLECTION = "rag_qabug_current_chunks"


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return unicodedata.normalize("NFKC", str(value)).replace("\x00", "").strip()


def _norm_header(value: Any) -> str:
    text = _text(value).lower()
    text = re.sub(r"^カスタム\s*フィールド\s*[（(](.*)[）)]$", r"\1", text)
    text = re.sub(r"[\s_\-/:：|（）()\[\]【】]+", "", text)
    return text


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def stable_point_id(chunk_key: str) -> int:
    """Return a deterministic positive signed-int64 Qdrant point id."""
    value = int.from_bytes(hashlib.sha256(chunk_key.encode("utf-8")).digest()[:8], "big")
    return value & 0x7FFF_FFFF_FFFF_FFFF


# Japanese Jira exports used by the user's QA and BUG registers plus common
# English/Chinese aliases. Unknown headers are NEVER discarded; they remain in
# raw_record/custom_fields.
HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "summary": ("要約", "summary", "title", "概要", "标题"),
    "issue_key": ("課題キー", "issue key", "issuekey", "jira key", "ticket key", "qa id", "bug id"),
    "issue_id": ("課題 id", "課題id", "issue id", "issueid"),
    "parent_issue_id": ("親 id", "親id", "parent id"),
    "issue_type": ("課題タイプ", "issue type", "issuetype", "種別"),
    "status": ("ステータス", "status", "状態"),
    "project_key": ("プロジェクト キー", "project key"),
    "project_name": ("プロジェクト名", "project name"),
    "project_type": ("プロジェクトタイプ", "project type"),
    "project_leader": ("プロジェクト リーダー", "project leader"),
    "priority": ("優先度", "priority", "severity", "重要度"),
    "resolution": ("解決状況", "resolution", "解決"),
    "assignee": ("担当者", "assignee", "owner", "responsible"),
    "reporter": ("報告者", "reporter", "reported by"),
    "creator": ("作成者", "creator", "created by"),
    "created_at": ("作成日", "created", "created at"),
    "updated_at": ("更新日", "updated", "updated at"),
    "last_viewed_at": ("最終閲覧日", "last viewed"),
    "resolved_at": ("解決日", "resolved", "resolved at"),
    "release_plan_version": ("リリース計画バージョン", "release plan version", "target version"),
    "component": ("コンポーネント", "component", "components"),
    "due_at": ("期限", "due", "due date"),
    "labels": ("ラベル", "labels", "tags"),
    "description": ("説明", "description", "details"),
    "environment": ("環境", "environment", "env"),
    "watchers": ("ウォッチャー", "watchers"),
    "worklog": ("作業ログ", "worklog", "work log"),
    "attachments": ("添付ファイル", "attachments", "attachment"),
    "comments": ("コメント", "comments", "comment"),
    "affected_version": ("不具合バージョン", "affects version", "affected version", "found version"),
    "software_version": ("ソフトバージョン", "software version"),
    "grade": ("グレード", "grade"),
    "sheet_name": ("シート名", "sheet name"),
    "test_spec_defect_detail": ("テスト仕様書不備詳細", "test specification defect detail"),
    "test_spec_name": ("テスト仕様書名", "test specification name"),
    "defect_content": ("不具合内容", "defect content"),
    "destination": ("仕向け", "destination", "market", "region"),
    "content_type": ("内容種別", "content type"),
    "precondition": ("前提条件", "precondition", "preconditions"),
    "reference_spec": ("参照仕様書", "reference specification", "reference spec"),
    "answer": ("回答", "answer"),
    "answer_type": ("回答区分", "answer type"),
    "answer_requested_at": ("回答希望日", "requested answer date"),
    "change_target": ("変更対象情報", "change target"),
    "actual_behavior": ("実動作", "actual behavior", "actual result"),
    "procedure_expected": ("手順/期待値", "手順期待値", "procedure/expected", "steps/expected"),
    "responsible_group": ("担当gr.", "担当gr", "responsible group"),
    "final_answer_group": ("最終回答gr.", "最終回答gr", "final answer group"),
    "final_answer": ("最終回答内容", "final answer"),
    "final_answerer": ("最終回答者", "final answerer"),
    "rationale": ("根拠", "rationale", "basis", "evidence rationale"),
    "item_cell": ("項目no./セル番地", "項目noセル番地", "item no/cell", "cell address"),
    "customer_report": ("顧客報告", "customer report"),
    "occurrence_environment_detail": ("発生環境_詳細", "発生環境詳細", "occurrence environment detail"),
    "occurrence_probability": ("発生確率", "occurrence probability"),
    "phenomenon": ("現象", "現象詳細", "phenomenon", "symptom"),
    "repro_steps": ("発生手順", "再現手順", "操作手順", "repro steps", "steps to reproduce"),
    "expected_result": ("期待結果", "期待動作", "expected result", "expected"),
    "actual_result": ("実際結果", "actual result"),
    "analysis_result": ("解析結果", "analysis result"),
    "analysis_comment": ("解析コメント", "analysis comment"),
    "analysis_owner": ("解析者", "analysis owner"),
    "root_cause": ("混入原因", "主要因分類", "root cause", "根本原因", "原因"),
    "cause_module": ("原因モジュール", "cause module"),
    "cause_company": ("原因会社", "cause company"),
    "countermeasure": ("対策内容", "countermeasure", "fix content"),
    "countermeasure_comment": ("対策コメント", "countermeasure comment"),
    "countermeasure_owner": ("対策者", "countermeasure owner"),
    "countermeasure_scope": ("対策適用範囲", "countermeasure scope"),
    "effect_result": ("効果確認結果", "effect confirmation result", "verification result"),
    "effect_version": ("効果確認バージョン", "effect confirmation version"),
    "effect_date": ("効果確認実施日", "effect confirmation date"),
    "recurrence_prevention_escape": ("再発防止策(流出)", "再発防止策（流出）", "escape recurrence prevention"),
    "recurrence_prevention_injection": ("再発防止策(混入)", "再発防止策（混入）", "injection recurrence prevention"),
    "impact_analysis": ("影響分析結果", "impact analysis result"),
    "impact": ("影響度", "impact", "severity"),
    "occurrence_frequency": ("発生頻度（21mm）", "発生頻度", "occurrence frequency"),
    "occurrence_date": ("発生日", "occurrence date"),
    "recovery_procedure": ("復帰手順", "recovery procedure", "workaround"),
    "problem_type": ("問題種類", "problem type"),
    "function_class": ("機能分類", "function classification"),
    "model": ("機種", "model"),
    "wall_id": ("壁id", "wall id"),
}

_NORMALIZED_ALIAS: dict[str, str] = {}
for _canonical, _aliases in HEADER_ALIASES.items():
    for _alias in (_canonical, *_aliases):
        _NORMALIZED_ALIAS.setdefault(_norm_header(_alias), _canonical)


def canonical_header(header: Any) -> str:
    return _NORMALIZED_ALIAS.get(_norm_header(header), "")


def _split_multi(value: str) -> list[str]:
    if not value:
        return []
    values = re.split(r"[\r\n]+|\s*\|\s*|\s*;\s*", value)
    return list(dict.fromkeys(v.strip() for v in values if v.strip()))


def normalize_status(value: str) -> str:
    raw = _text(value)
    if not raw:
        return "UNSPECIFIED"
    patterns = (
        ("OPEN", r"^(?:open|オープン|未対応|未処理)$"),
        ("IN_PROGRESS", r"(?:in[ -]?progress|進行中|対応中|処理中|検討中|解析中)"),
        ("RESOLVED", r"(?:resolved|解決済|修正済|対応済)$"),
        ("CLOSED", r"(?:closed|クローズ|完了)$"),
        ("REOPENED", r"(?:reopened|再オープン)"),
        ("CANCELLED", r"(?:cancel|中止|対策中止|won.?t fix|不実行)"),
    )
    for normalized, pattern in patterns:
        if re.search(pattern, raw, re.I):
            return normalized
    return re.sub(r"\s+", "_", raw.upper())[:64]


def normalize_priority(value: str) -> str:
    raw = _text(value)
    if not raw:
        return "UNSPECIFIED"
    mapping = {
        "最高": "CRITICAL", "緊急": "CRITICAL", "blocker": "CRITICAL", "critical": "CRITICAL",
        "高": "HIGH", "high": "HIGH", "中": "MEDIUM", "medium": "MEDIUM",
        "低": "LOW", "low": "LOW",
    }
    low = raw.lower()
    if re.fullmatch(r"p[0-4]", low):
        return low.upper()
    for key, normalized in mapping.items():
        if key.lower() in low:
            return normalized
    return re.sub(r"\s+", "_", raw.upper())[:64]


def normalize_resolution(value: str) -> str:
    raw = _text(value)
    if not raw:
        return "UNSPECIFIED"
    mapping = (
        ("FIXED", r"修正済|fixed|resolved"),
        ("DONE", r"完了|done|complete"),
        ("WONT_FIX", r"won.?t fix|対応しない|対策中止"),
        ("DUPLICATE", r"duplicate|重複|同一案件"),
        ("NOT_REPRODUCIBLE", r"未再現|再現しない|not reproduc"),
        ("ENVIRONMENT", r"環境要因"),
    )
    for normalized, pattern in mapping:
        if re.search(pattern, raw, re.I):
            return normalized
    return re.sub(r"\s+", "_", raw.upper())[:64]


_SECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("PHENOMENON", re.compile(r"(?:【|<|\[)?(?:現象(?:詳細)?|症状)(?:】|>|\])?\s*[:：]?", re.I)),
    ("REPRO_STEPS", re.compile(r"(?:【|<|\[)?(?:発生手順|再現手順|操作手順|手順)(?:】|>|\])?\s*[:：]?", re.I)),
    ("EXPECTED_RESULT", re.compile(r"(?:【|<|\[)?(?:期待結果|期待動作|期待値)(?:】|>|\])?\s*[:：]?", re.I)),
    ("PRECONDITION", re.compile(r"(?:【|<|\[)?(?:事前準備|前提条件)(?:】|>|\])?\s*[:：]?", re.I)),
    ("ENVIRONMENT", re.compile(r"(?:【|<|\[)?(?:環境|発生環境)(?:】|>|\])?\s*[:：]?", re.I)),
    ("RECOVERY", re.compile(r"(?:【|<|\[)?(?:復帰手順|回避策)(?:】|>|\])?\s*[:：]?", re.I)),
    ("LOG", re.compile(r"(?:【|<|\[)?(?:ログ|log)(?:】|>|\])?\s*[:：]?", re.I)),
)


def extract_description_sections(description: str) -> dict[str, str]:
    text = _text(description)
    if not text:
        return {}
    hits: list[tuple[int, int, str]] = []
    for name, pattern in _SECTION_PATTERNS:
        for match in pattern.finditer(text):
            hits.append((match.start(), match.end(), name))
    if not hits:
        return {"DESCRIPTION": text}
    hits.sort()
    sections: dict[str, str] = {}
    prefix = text[:hits[0][0]].strip()
    if prefix:
        sections["DESCRIPTION"] = prefix
    for idx, (start, end, name) in enumerate(hits):
        next_start = hits[idx + 1][0] if idx + 1 < len(hits) else len(text)
        body = text[end:next_start].strip(" \t\r\n:：?-—")
        if body:
            if name in sections:
                sections[name] += "\n" + body
            else:
                sections[name] = body
    return sections


def _parse_links(raw: Mapping[str, Any]) -> list[dict[str, str]]:
    relations: list[dict[str, str]] = []
    for header, value in raw.items():
        h = _text(header)
        if "課題外部リンク" not in h and "issue link" not in h.lower() and "linked issue" not in h.lower():
            continue
        relation_type = h
        match = re.search(r"[（(](.*?)[）)]", h)
        if match:
            relation_type = match.group(1)
        for token in re.findall(r"[A-Z][A-Z0-9]{1,20}-\d{1,10}", _text(value), re.I):
            relations.append({"relation_type": relation_type, "target_issue_key": token.upper(), "source_field": h})
    # Also capture issue keys appearing in comments/description as weak references.
    return relations


def _quality_flags(
    record_type: str,
    normalized: Mapping[str, str],
    *,
    expected_column_count: int | None = None,
    source_column_count: int | None = None,
) -> list[dict[str, str]]:
    flags: list[dict[str, str]] = []
    if (
        expected_column_count is not None
        and source_column_count is not None
        and int(expected_column_count) != int(source_column_count)
    ):
        flags.append(
            {
                "severity": "ERROR",
                "code": "SOURCE_COLUMN_COUNT_MISMATCH",
                "message": (
                    f"Source row has {int(source_column_count)} columns; "
                    f"header contract requires {int(expected_column_count)}. "
                    "The row is preserved for audit but quarantined from AI retrieval."
                ),
            }
        )
    if not normalized.get("issue_key"):
        flags.append({"severity": "ERROR", "code": "MISSING_ISSUE_KEY", "message": "Stable Jira/QA issue key is missing."})
    if not normalized.get("summary"):
        flags.append({"severity": "WARN", "code": "MISSING_SUMMARY", "message": "Summary/title is empty."})
    if not normalized.get("status"):
        flags.append({"severity": "WARN", "code": "MISSING_STATUS", "message": "Workflow status is empty."})
    if record_type == "QA" and not any(normalized.get(k) for k in ("answer", "final_answer", "rationale")):
        flags.append({"severity": "WARN", "code": "QA_ANSWER_MISSING", "message": "QA record has no explicit answer/final answer."})
    if record_type == "BUG" and not any(normalized.get(k) for k in ("root_cause", "analysis_result")):
        flags.append({"severity": "INFO", "code": "BUG_ROOT_CAUSE_UNRECORDED", "message": "No explicit root-cause/analysis field is populated."})
    return flags


def _is_queryable(flags: Sequence[Mapping[str, Any]]) -> bool:
    return not any(str(flag.get("severity") or "").upper() == "ERROR" for flag in flags)


@dataclass(frozen=True, slots=True)
class CanonicalChunk:
    point_id: int
    chunk_key: str
    section_type: str
    section_order: int
    content: str
    content_hash: str
    index_hash: str
    payload: dict[str, Any]


class QABugCanonicalizer:
    """Normalize Jira-style QA/BUG registers into canonical enterprise RAG JSON."""

    def __init__(
        self,
        *,
        embedding_model: str = DEFAULT_EMBEDDING_MODEL,
        embedding_version: str = DEFAULT_EMBEDDING_VERSION,
        vector_dimension: int = DEFAULT_VECTOR_DIMENSION,
        collection_name: str = DEFAULT_QDRANT_COLLECTION,
        chunk_chars: int = 1600,
        chunk_overlap: int = 160,
    ) -> None:
        self.embedding_model = embedding_model
        self.embedding_version = embedding_version
        self.vector_dimension = int(vector_dimension)
        self.collection_name = collection_name
        self.chunk_chars = max(600, int(chunk_chars))
        self.chunk_overlap = max(0, min(int(chunk_overlap), self.chunk_chars // 3))

    @staticmethod
    def map_headers(headers: Sequence[Any]) -> list[tuple[str, str]]:
        mapped: list[tuple[str, str]] = []
        seen_raw: dict[str, int] = {}
        for index, value in enumerate(headers):
            raw = _text(value) or f"__column_{index + 1}"
            count = seen_raw.get(raw, 0) + 1
            seen_raw[raw] = count
            unique_raw = raw if count == 1 else f"{raw}#{count}"
            mapped.append((unique_raw, canonical_header(raw)))
        return mapped

    @staticmethod
    def row_to_raw(mapped_headers: Sequence[tuple[str, str]], values: Sequence[Any]) -> dict[str, str]:
        raw: dict[str, str] = {}
        for idx, (raw_header, _canonical) in enumerate(mapped_headers):
            value = _text(values[idx] if idx < len(values) else None)
            if value:
                raw[raw_header] = value
        return raw

    @staticmethod
    def _canonical_values(mapped_headers: Sequence[tuple[str, str]], values: Sequence[Any]) -> dict[str, str]:
        result: dict[str, str] = {}
        for idx, (_raw, canonical) in enumerate(mapped_headers):
            if not canonical:
                continue
            value = _text(values[idx] if idx < len(values) else None)
            if not value:
                continue
            if canonical in result and value != result[canonical]:
                result[canonical] = result[canonical] + "\n" + value
            else:
                result[canonical] = value
        return result

    def canonicalize(
        self,
        *,
        record_type: str,
        mapped_headers: Sequence[tuple[str, str]],
        values: Sequence[Any],
        source_file: str,
        source_sheet: str = "",
        source_row: int | None = None,
        source_format: str = "",
    ) -> dict[str, Any] | None:
        record_type = str(record_type or "").strip().upper()
        if record_type not in {"QA", "BUG"}:
            raise ValueError("record_type must be QA or BUG")
        expected_column_count = len(mapped_headers)
        source_column_count = len(values)
        repair_info = dict(getattr(values, "repair_info", {}) or {})
        raw_record = self.row_to_raw(mapped_headers, values)
        # Preserve overflow cells from malformed tabular exports.  They are not
        # mapped to semantic fields, but they remain audit-visible and contribute
        # to source_hash.
        if source_column_count > expected_column_count:
            for overflow_index, overflow_value in enumerate(values[expected_column_count:], start=expected_column_count + 1):
                text = _text(overflow_value)
                if text:
                    raw_record[f"__overflow_column_{overflow_index}"] = text
        if not raw_record:
            return None
        n = self._canonical_values(mapped_headers, values)
        issue_key = _text(n.get("issue_key")).upper()
        summary = _text(n.get("summary"))
        description = _text(n.get("description"))
        if not issue_key and not summary and not description:
            return None

        status_raw = _text(n.get("status"))
        priority_raw = _text(n.get("priority"))
        resolution_raw = _text(n.get("resolution"))
        normalized = dict(n)
        normalized.update(
            {
                "issue_key": issue_key,
                "status_norm": normalize_status(status_raw),
                "priority_norm": normalize_priority(priority_raw),
                "resolution_norm": normalize_resolution(resolution_raw),
            }
        )

        # Derive common semantic sections while never overwriting explicit fields.
        derived = extract_description_sections(description)
        if record_type == "BUG":
            normalized.setdefault("phenomenon", derived.get("PHENOMENON", ""))
            normalized.setdefault("repro_steps", derived.get("REPRO_STEPS", ""))
            normalized.setdefault("expected_result", derived.get("EXPECTED_RESULT", ""))
            normalized.setdefault("precondition", derived.get("PRECONDITION", ""))
            normalized.setdefault("recovery_procedure", derived.get("RECOVERY", ""))
            if not normalized.get("environment"):
                normalized["environment"] = derived.get("ENVIRONMENT", "")
        elif record_type == "QA":
            normalized.setdefault("precondition", derived.get("PRECONDITION", ""))
            normalized.setdefault("procedure_expected", derived.get("REPRO_STEPS", "") or derived.get("EXPECTED_RESULT", ""))

        # Unknown/custom source fields are losslessly retained here.
        recognized_raw = {raw for raw, canonical in mapped_headers if canonical}
        custom_fields = {key: value for key, value in raw_record.items() if key.split("#", 1)[0] not in recognized_raw}
        # More useful: all explicit Jira custom fields are copied as custom_fields too,
        # including ones that also map to a canonical query field.
        for key, value in raw_record.items():
            if key.startswith("カスタム フィールド"):
                custom_fields[key] = value
                # Also expose the human-facing inner Jira custom-field name so
                # custom[字段名]=值 filters do not require the export wrapper.
                inner = re.match(r"^カスタム\s*フィールド\s*[（(](.*?)[）)](?:#\d+)?$", key)
                if inner and inner.group(1).strip():
                    alias = inner.group(1).strip()
                    if alias in custom_fields and custom_fields[alias] != value:
                        custom_fields[alias] = custom_fields[alias] + "\n" + value
                    else:
                        custom_fields[alias] = value

        record_key = f"{record_type}:{issue_key or _sha256(source_file + ':' + str(source_row) + ':' + summary)[:20]}"
        source_identity = f"{Path(source_file).name}|{source_sheet}|{source_row or 0}"
        source_hash = _sha256(json.dumps(raw_record, ensure_ascii=False, sort_keys=True))

        sections = self._build_sections(record_type, normalized)
        content_for_hash = "\n\n".join(f"[{s['section_type']}]\n{s['content']}" for s in sections)
        content_hash = _sha256(content_for_hash)
        index_hash = _sha256("|".join((record_key, content_hash, self.embedding_model, self.embedding_version, str(self.vector_dimension))))
        relations = _parse_links(raw_record)
        quality_flags = _quality_flags(
            record_type,
            normalized,
            expected_column_count=expected_column_count,
            source_column_count=source_column_count,
        )
        if repair_info:
            quality_flags.append({
                "severity": "INFO",
                "code": "SOURCE_FRAGMENT_REPAIRED",
                "message": "Malformed delimited source fragments were deterministically reconstructed to the exact header width.",
            })
        queryable = _is_queryable(quality_flags)

        document = {
            "schema_version": QABUG_JSON_SCHEMA_VERSION,
            "document_type": record_type,
            "identity": {
                "record_key": record_key,
                "issue_key": issue_key or None,
                "issue_id": n.get("issue_id") or None,
                "parent_issue_id": n.get("parent_issue_id") or None,
                "project_key": n.get("project_key") or None,
            },
            "source": {
                "file_name": Path(source_file).name,
                "source_format": source_format or Path(source_file).suffix.lstrip(".").upper(),
                "sheet_name": source_sheet or None,
                "row_number": source_row,
                "source_identity": source_identity,
                "expected_column_count": expected_column_count,
                "source_column_count": source_column_count,
                "column_count_match": source_column_count == expected_column_count,
                "repair_info": repair_info or None,
            },
            "structured_metadata": {
                "issue_type": n.get("issue_type") or None,
                "status": status_raw or None,
                "status_norm": normalized["status_norm"],
                "priority": priority_raw or None,
                "priority_norm": normalized["priority_norm"],
                "resolution": resolution_raw or None,
                "resolution_norm": normalized["resolution_norm"],
                "project_name": n.get("project_name") or None,
                "project_type": n.get("project_type") or None,
                "project_leader": n.get("project_leader") or None,
                "component": n.get("component") or None,
                "assignee": n.get("assignee") or None,
                "reporter": n.get("reporter") or None,
                "creator": n.get("creator") or None,
                "release_plan_version": n.get("release_plan_version") or None,
                "affected_version": n.get("affected_version") or None,
                "software_version": n.get("software_version") or None,
                "grade": n.get("grade") or None,
                "destination": n.get("destination") or None,
                "model": n.get("model") or None,
                "labels": _split_multi(n.get("labels", "")),
                "created_at": n.get("created_at") or None,
                "updated_at": n.get("updated_at") or None,
                "resolved_at": n.get("resolved_at") or None,
                "due_at": n.get("due_at") or None,
            },
            "normalized_fields": {k: v for k, v in normalized.items() if v},
            "content_sections": sections,
            "relations": relations,
            "provenance": {
                "source_fields": list(raw_record.keys()),
                "raw_record_preserved": True,
                "normalization": "deterministic-field-mapping-v1",
                "derived_sections": sorted(derived.keys()),
                "expected_column_count": expected_column_count,
                "source_column_count": source_column_count,
                "column_count_match": source_column_count == expected_column_count,
                "quarantine_policy": "exclude-from-ai-on-error-quality-flag",
                "source_repair": repair_info or None,
            },
            "hashes": {
                "source_hash": source_hash,
                "content_hash": content_hash,
                "index_hash": index_hash,
            },
            "embedding_metadata": {
                "model": self.embedding_model,
                "version": self.embedding_version,
                "dimension": self.vector_dimension,
                "distance": "Cosine",
                "collection": self.collection_name,
            },
            "quality_flags": quality_flags,
            "queryable": queryable,
            "acl": {"classification": "INTERNAL", "allowed_groups": [], "policy_version": "placeholder-v1"},
            "raw_record": raw_record,
            "custom_fields": custom_fields,
        }
        document["chunks"] = [asdict(chunk) for chunk in self.build_chunks(document)]
        return document

    @staticmethod
    def _build_sections(record_type: str, n: Mapping[str, str]) -> list[dict[str, Any]]:
        ordered_common = (
            ("SUMMARY", "summary"),
            ("DESCRIPTION", "description"),
            ("ENVIRONMENT", "environment"),
            ("WORKLOG", "worklog"),
            ("COMMENTS", "comments"),
        )
        ordered_qa = (
            ("QA_PRECONDITION", "precondition"),
            ("QA_PROCEDURE_EXPECTED", "procedure_expected"),
            ("QA_ACTUAL_BEHAVIOR", "actual_behavior"),
            ("QA_ANSWER", "answer"),
            ("QA_FINAL_ANSWER", "final_answer"),
            ("QA_RATIONALE", "rationale"),
            ("QA_REFERENCE_SPEC", "reference_spec"),
            ("QA_TEST_SPEC", "test_spec_name"),
            ("QA_TEST_SPEC_DEFECT", "test_spec_defect_detail"),
        )
        ordered_bug = (
            ("BUG_PHENOMENON", "phenomenon"),
            ("BUG_REPRO_STEPS", "repro_steps"),
            ("BUG_EXPECTED_RESULT", "expected_result"),
            ("BUG_ACTUAL_RESULT", "actual_result"),
            ("BUG_ANALYSIS_RESULT", "analysis_result"),
            ("BUG_ANALYSIS_COMMENT", "analysis_comment"),
            ("BUG_ROOT_CAUSE", "root_cause"),
            ("BUG_COUNTERMEASURE", "countermeasure"),
            ("BUG_COUNTERMEASURE_COMMENT", "countermeasure_comment"),
            ("BUG_EFFECT_RESULT", "effect_result"),
            ("BUG_IMPACT_ANALYSIS", "impact_analysis"),
            ("BUG_RECURRENCE_PREVENTION_ESCAPE", "recurrence_prevention_escape"),
            ("BUG_RECURRENCE_PREVENTION_INJECTION", "recurrence_prevention_injection"),
            ("BUG_RECOVERY", "recovery_procedure"),
        )
        fields = (*ordered_common, *(ordered_qa if record_type == "QA" else ordered_bug))
        result: list[dict[str, Any]] = []
        for section_type, key in fields:
            value = _text(n.get(key))
            if value:
                result.append({"section_type": section_type, "source_field": key, "content": value})
        return result

    def _split_content(self, content: str) -> Iterator[str]:
        value = _text(content)
        if len(value) <= self.chunk_chars:
            if value:
                yield value
            return
        start = 0
        while start < len(value):
            end = min(len(value), start + self.chunk_chars)
            if end < len(value):
                # Prefer a natural line/sentence boundary in the last 25%.
                floor = start + int(self.chunk_chars * 0.72)
                candidates = [value.rfind(token, floor, end) for token in ("\n", "。", ". ", "；", "; ")]
                boundary = max(candidates)
                if boundary > floor:
                    end = boundary + 1
            piece = value[start:end].strip()
            if piece:
                yield piece
            if end >= len(value):
                break
            start = max(start + 1, end - self.chunk_overlap)

    def build_chunks(self, document: Mapping[str, Any]) -> list[CanonicalChunk]:
        identity = document.get("identity") or {}
        structured = document.get("structured_metadata") or {}
        hashes = document.get("hashes") or {}
        source = document.get("source") or {}
        record_type = str(document.get("document_type") or "")
        record_key = str(identity.get("record_key") or "")
        issue_key = str(identity.get("issue_key") or "")
        result: list[CanonicalChunk] = []
        ordinal = 0
        for section in document.get("content_sections") or []:
            section_type = str(section.get("section_type") or "CONTENT")
            for part_index, content in enumerate(self._split_content(str(section.get("content") or ""))):
                ordinal += 1
                chunk_key = f"QABUG:{record_key}:{section_type}:{part_index}"
                chunk_hash = _sha256(content)
                index_hash = _sha256("|".join((chunk_key, chunk_hash, self.embedding_model, self.embedding_version)))
                payload = {
                    "artifact_type": "QABUG_REGISTER",
                    "record_type": record_type,
                    "record_key": record_key,
                    "issue_key": issue_key,
                    "issue_id": str(identity.get("issue_id") or ""),
                    "project_key": str(identity.get("project_key") or ""),
                    "issue_type": str(structured.get("issue_type") or ""),
                    "status": str(structured.get("status_norm") or "UNSPECIFIED"),
                    "status_raw": str(structured.get("status") or ""),
                    "priority": str(structured.get("priority_norm") or "UNSPECIFIED"),
                    "priority_raw": str(structured.get("priority") or ""),
                    "resolution": str(structured.get("resolution_norm") or "UNSPECIFIED"),
                    "component": str(structured.get("component") or ""),
                    "assignee": str(structured.get("assignee") or ""),
                    "reporter": str(structured.get("reporter") or ""),
                    "release_version": str(structured.get("release_plan_version") or ""),
                    "affected_version": str(structured.get("affected_version") or ""),
                    "software_version": str(structured.get("software_version") or ""),
                    "grade": str(structured.get("grade") or ""),
                    "destination": str(structured.get("destination") or ""),
                    "model": str(structured.get("model") or ""),
                    "section_type": section_type,
                    "source_file": str(source.get("file_name") or ""),
                    "source_sheet": str(source.get("sheet_name") or ""),
                    "source_row": int(source.get("row_number") or 0),
                    "content_hash": chunk_hash,
                    "record_content_hash": str(hashes.get("content_hash") or ""),
                    "index_hash": index_hash,
                    "embedding_model": self.embedding_model,
                    "embedding_version": self.embedding_version,
                    "index_scope": "CURRENT",
                    "source_view": "public.qabug_current_chunks",
                    # Keeping content in payload permits zero-SQL semantic evidence
                    # hydration, while SQL remains authoritative for structured facts.
                    "content": content,
                }
                result.append(CanonicalChunk(
                    point_id=stable_point_id(chunk_key),
                    chunk_key=chunk_key,
                    section_type=section_type,
                    section_order=ordinal,
                    content=content,
                    content_hash=chunk_hash,
                    index_hash=index_hash,
                    payload=payload,
                ))
        return result


# JSON Schema is deliberately dependency-free so it can be written into release
# artifacts and used by offline validators.
RAG_JSON_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://document-intelligence.local/schemas/qabug-rag-document-v1.json",
    "title": "Enterprise QA/BUG RAG Canonical Document",
    "description": "Lossless canonical interchange contract for QA/Jira and BUG/Jira records. PostgreSQL remains authoritative; Qdrant chunks are derived retrieval state.",
    "type": "object",
    "required": [
        "schema_version", "document_type", "identity", "source", "structured_metadata",
        "normalized_fields", "content_sections", "relations", "provenance", "hashes",
        "embedding_metadata", "quality_flags", "queryable", "acl", "raw_record", "custom_fields", "chunks",
    ],
    "$defs": {
        "nullableString": {"type": ["string", "null"]},
        "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "contentSection": {
            "type": "object",
            "required": ["section_type", "source_field", "content"],
            "properties": {
                "section_type": {"type": "string", "minLength": 1, "maxLength": 128},
                "source_field": {"type": "string", "minLength": 1, "maxLength": 256},
                "content": {"type": "string", "minLength": 1},
            },
            "additionalProperties": True,
        },
        "qualityFlag": {
            "type": "object",
            "required": ["severity", "code", "message"],
            "properties": {
                "severity": {"type": "string", "enum": ["INFO", "WARN", "WARNING", "ERROR", "CRITICAL"]},
                "code": {"type": "string", "minLength": 1, "maxLength": 128},
                "message": {"type": "string"},
            },
            "additionalProperties": True,
        },
        "chunk": {
            "type": "object",
            "required": ["point_id", "chunk_key", "section_type", "section_order", "content", "content_hash", "index_hash", "payload"],
            "properties": {
                "point_id": {"type": "integer", "minimum": 0, "maximum": 9223372036854775807},
                "chunk_key": {"type": "string", "minLength": 1},
                "section_type": {"type": "string", "minLength": 1, "maxLength": 128},
                "section_order": {"type": "integer", "minimum": 1},
                "content": {"type": "string", "minLength": 1},
                "content_hash": {"$ref": "#/$defs/sha256"},
                "index_hash": {"$ref": "#/$defs/sha256"},
                "payload": {"type": "object"},
            },
            "additionalProperties": True,
        },
    },
    "properties": {
        "schema_version": {"const": QABUG_JSON_SCHEMA_VERSION},
        "document_type": {"enum": ["QA", "BUG"]},
        "identity": {
            "type": "object",
            "required": ["record_key", "issue_key", "issue_id", "parent_issue_id", "project_key"],
            "properties": {
                "record_key": {"type": "string", "minLength": 1},
                "issue_key": {"$ref": "#/$defs/nullableString"},
                "issue_id": {"$ref": "#/$defs/nullableString"},
                "parent_issue_id": {"$ref": "#/$defs/nullableString"},
                "project_key": {"$ref": "#/$defs/nullableString"},
            },
            "additionalProperties": True,
        },
        "source": {
            "type": "object",
            "required": ["file_name", "source_format", "source_identity", "expected_column_count", "source_column_count", "column_count_match"],
            "properties": {
                "file_name": {"type": "string", "minLength": 1},
                "source_format": {"type": "string", "minLength": 1},
                "sheet_name": {"$ref": "#/$defs/nullableString"},
                "row_number": {"type": ["integer", "null"], "minimum": 1},
                "source_identity": {"type": "string", "minLength": 1},
                "expected_column_count": {"type": "integer", "minimum": 1},
                "source_column_count": {"type": "integer", "minimum": 0},
                "column_count_match": {"type": "boolean"},
                "repair_info": {"type": ["object", "null"]},
            },
            "additionalProperties": True,
        },
        "structured_metadata": {
            "type": "object",
            "required": ["status_norm", "priority_norm", "resolution_norm"],
            "properties": {
                "status_norm": {"type": "string", "minLength": 1},
                "priority_norm": {"type": "string", "minLength": 1},
                "resolution_norm": {"type": "string", "minLength": 1},
                "labels": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
            },
            "additionalProperties": True,
        },
        "normalized_fields": {"type": "object"},
        "content_sections": {"type": "array", "items": {"$ref": "#/$defs/contentSection"}},
        "relations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "relation_type": {"type": "string"},
                    "target_issue_key": {"type": "string"},
                    "source_field": {"type": "string"},
                },
                "additionalProperties": True,
            },
        },
        "provenance": {
            "type": "object",
            "required": ["source_fields", "raw_record_preserved", "normalization", "quarantine_policy"],
            "properties": {
                "source_fields": {"type": "array", "items": {"type": "string"}},
                "raw_record_preserved": {"const": True},
                "normalization": {"type": "string", "minLength": 1},
                "quarantine_policy": {"type": "string", "minLength": 1},
            },
            "additionalProperties": True,
        },
        "hashes": {
            "type": "object",
            "required": ["source_hash", "content_hash", "index_hash"],
            "properties": {
                "source_hash": {"$ref": "#/$defs/sha256"},
                "content_hash": {"$ref": "#/$defs/sha256"},
                "index_hash": {"$ref": "#/$defs/sha256"},
            },
            "additionalProperties": False,
        },
        "embedding_metadata": {
            "type": "object",
            "required": ["model", "version", "dimension", "distance", "collection"],
            "properties": {
                "model": {"type": "string", "minLength": 1},
                "version": {"type": "string", "minLength": 1},
                "dimension": {"type": "integer", "const": DEFAULT_VECTOR_DIMENSION},
                "distance": {"type": "string", "const": "Cosine"},
                "collection": {"type": "string", "minLength": 1},
            },
            "additionalProperties": True,
        },
        "quality_flags": {"type": "array", "items": {"$ref": "#/$defs/qualityFlag"}},
        "queryable": {"type": "boolean"},
        "acl": {
            "type": "object",
            "required": ["classification", "allowed_groups", "policy_version"],
            "properties": {
                "classification": {"type": "string", "minLength": 1},
                "allowed_groups": {"type": "array", "items": {"type": "string"}},
                "policy_version": {"type": "string", "minLength": 1},
            },
            "additionalProperties": True,
        },
        "raw_record": {"type": "object", "additionalProperties": {"type": "string"}},
        "custom_fields": {"type": "object", "additionalProperties": {"type": "string"}},
        "chunks": {"type": "array", "items": {"$ref": "#/$defs/chunk"}},
    },
    "additionalProperties": True,
}
