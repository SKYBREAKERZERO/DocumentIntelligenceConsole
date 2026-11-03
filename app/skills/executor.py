"""Deterministic offline Skill execution against explicitly supplied JSON snapshots.

No network, arbitrary SQL, model/tool invocation, database writes, or automatic
human-approval transitions. Standalone snapshots are NOT trusted database facts.
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict
from types import SimpleNamespace
from typing import Any, Mapping

from app.ai.answer_integrity import validate_enterprise_answer
from app.ai.claim_anchor import validate_claim_anchors
from app.ai.retrieval import EvidenceItem
from app.ai.hybrid_retrieval import fuse_spec_rrf
from app.engineering_intelligence.bug_spec_candidates import (
    SpecUnitSnapshot, extract_bug_observations, rank_candidates,
)
from app.engineering_intelligence.deterministic_read_models import (
    verify_section_reference,
)
from app.engineering_intelligence.history import align_requirement_versions
from app.engineering_intelligence.requirements import extract_requirement_units, normalize_text
from app.storage.primary_document_policy import PrimarySpecificationDocumentPolicy
from app.skills.catalog import SKILLS

MAX_TEXT = 150_000
MAX_ITEMS = 120
MAX_RESULT_ITEMS = 30


class SkillInputError(ValueError):
    pass


def _map(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SkillInputError(f"{name}: expected JSON object")
    return value


def _text(value: Any, label: str, *, optional: bool = False) -> str:
    if not isinstance(value, str) or (not optional and not value.strip()) or len(value) > MAX_TEXT:
        raise SkillInputError(f"{label}: nonempty bounded text required")
    return value


def _items(value: Any, label: str, *, max_items: int = MAX_ITEMS) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > max_items:
        raise SkillInputError(f"{label}: invalid bounded list")
    return [_map(x, label) for x in value]


def _int(value: Any, label: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise SkillInputError(f"{label}: integer >= {minimum} required")
    return value


def _primary(snapshot: Mapping[str, Any]) -> None:
    name = _text(snapshot.get("file_name"), "SPEC_BODY file_name")
    role = str(snapshot.get("document_role") or "").upper()
    if role not in ("SPEC_BODY", "PRIMARY_CONTENT"):
        raise SkillInputError("EXPLICIT_SPEC_BODY_ROLE_REQUIRED")
    if not PrimarySpecificationDocumentPolicy.is_primary(
        file_name=name, metadata={"document_role": role}
    ):
        raise SkillInputError("SUPPORTING_DOCUMENT_REJECTED")


def _unit_snapshot(value: dict[str, Any]):
    _primary(value)
    source = _text(value.get("text"), "text")
    return extract_requirement_units(
        document_id=_text(value.get("document_id"), "document_id"),
        content_id=_int(value.get("content_id"), "content_id"),
        section_id=_text(value.get("section_id"), "section_id"),
        page_number=value.get("page_number"),
        text=source, max_units=MAX_ITEMS, max_source_chars=MAX_TEXT,
    )


def _unit_json(unit: Any) -> dict[str, Any]:
    return {
        "unit_key": unit.unit_key, "source_text": unit.text, "unit_type": unit.unit_type,
        "evidence": unit.evidence, "signals": asdict(unit.signals),
    }


def _extract(value: dict[str, Any]) -> dict[str, Any]:
    units = _unit_snapshot(value)
    if len(units) > MAX_RESULT_ITEMS:
        raise SkillInputError("TOO_MANY_UNITS_FOR_COMPLETE_RESPONSE")
    return {"findings": [_unit_json(u) for u in units],
            "evidence_level": "OFFLINE_SOURCE_SPANS", "needs_human_review": True}


def _relation(value: dict[str, Any]) -> dict[str, Any]:
    source, target = _map(value.get("source"), "source"), _map(value.get("target"), "target")
    _primary(source)
    _primary(target)
    if source.get("project") != target.get("project") or source.get("project") not in ("21MM", "24MM", "COMMON"):
        raise SkillInputError("PROJECT_SCOPE_MISMATCH")
    if not source.get("document_id") or not target.get("document_id"):
        raise SkillInputError("DOCUMENT_ID_REQUIRED")
    if (source.get("source_document") != source.get("document_id")
            or target.get("target_document") != target.get("document_id")):
        raise SkillInputError("DOCUMENT_IDENTITY_MISMATCH")
    if not verify_section_reference(source, target):
        raise SkillInputError("SOURCE_TARGET_SPAN_OR_ANCHOR_INVALID")
    return {
        "findings": [{"type": "STRUCTURAL_REFERENCE", "anchor": source["source_text"],
                      "source_document": source["document_id"], "target_document": target["document_id"],
                      "target_section": target["target_section"],
                      "source_hash": source["source_hash"], "target_hash": target["target_hash"]}],
        "evidence_level": "OFFLINE_DOUBLE_SPAN_CONSISTENCY",
        "needs_human_review": True,
        "limitations": ["章节引用不证明特定目标要求的唯一性、依赖或影响", "离线快照不能证明数据库授权与状态"],
    }


def _version(value: dict[str, Any]) -> dict[str, Any]:
    old, new = _map(value.get("old"), "old"), _map(value.get("new"), "new")
    for endpoint in (old, new):
        _primary(endpoint)
    if not old.get("spec_code") or old.get("spec_code") != new.get("spec_code") or old.get("project") != new.get("project"):
        raise SkillInputError("SPEC_IDENTITY_MISMATCH")
    if old.get("version_id") == new.get("version_id") or not old.get("version_id") or not new.get("version_id"):
        raise SkillInputError("DISTINCT_EXPLICIT_VERSIONS_REQUIRED")
    left, right = _unit_snapshot(old), _unit_snapshot(new)
    mappings = align_requirement_versions(left, right, max_comparisons=4000)
    if len(mappings) > MAX_RESULT_ITEMS:
        raise SkillInputError("TOO_MANY_VERSION_MAPPINGS_FOR_COMPLETE_RESPONSE")
    # The caller can submit an arbitrary subset of a revision. Unmatched
    # entries in these snapshots must NOT become confirmed removals/additions.
    findings = []
    for mapping in mappings:
        finding = asdict(mapping)
        if finding["change_type"] in {"REMOVED", "ADDED"}:
            finding["snapshot_alignment_type"] = finding["change_type"]
            finding["change_type"] = "UNMATCHED_IN_SUPPLIED_SNAPSHOT"
            finding["review_status"] = "REVIEW_REQUIRED"
        findings.append(finding)
    return {"findings": findings,
            "evidence_level": "OFFLINE_VERSION_TEXT_ALIGNMENT",
            "needs_human_review": True,
            "limitations": ["旧版与新版顺序、生效时间及版本归属未由 PostgreSQL 认证", "文本连续不证明工程关系有效性", "部分快照无法证明要求新增或删除"]}


def _bug(value: dict[str, Any]) -> dict[str, Any]:
    bug = _map(value.get("bug"), "bug")
    observations = extract_bug_observations(bug)
    units: list[SpecUnitSnapshot] = []
    for item in _items(value.get("units", []), "units", max_items=MAX_ITEMS):
        try:
            snap = SpecUnitSnapshot(
                scope=item["scope"], spec_code=item["spec_code"], version_id=item["version_id"],
                version_label=item["version_label"], document_id=item["document_id"],
                file_name=item["file_name"], document_metadata=item["document_metadata"],
                unit_id=item["unit_id"], content_id=item["content_id"], section_id=item["section_id"],
                page_number=item.get("page_number"), start_offset=item["start_offset"],
                end_offset=item["end_offset"], raw_text=item["raw_text"], text_hash=item["text_hash"],
                chunk_text=item["chunk_text"],
            )
        except (KeyError, TypeError) as exc:
            raise SkillInputError("INVALID_UNIT_SNAPSHOT_FIELDS") from exc
        # Explicitly reject malformed data rather than silently downgrading.
        if not snap.verify():
            raise SkillInputError("INVALID_SPEC_BODY_SNAPSHOT")
        units.append(snap)
    ranked = rank_candidates(observations, units, top_n=8)
    return {"observations": observations,
            "findings": [{"unit_id": u.unit_id, "document_id": u.document_id,
                          "version_id": u.version_id, "section": u.section_id,
                          "page": u.page_number, "source_text": u.raw_text,
                          "hash": u.text_hash, "ranking_score": score,
                          "status": "UNREVIEWED_CANDIDATE"} for u, score in ranked],
            "evidence_level": "OFFLINE_CANDIDATES_ONLY", "conformance_verdict": "证据不足",
            "needs_human_review": True,
            "limitations": ["尚未从可信 PostgreSQL 审核软件版本到式样版本的映射", "词面候选不能自动进入模型正式证据"]}


def _hybrid(value: dict[str, Any]) -> dict[str, Any]:
    lanes: dict[str, list[EvidenceItem]] = {}
    for lane in ("sql", "dense", "sparse"):
        records = _items(value.get(lane, []), lane)
        lane_hits: list[EvidenceItem] = []
        for n, record in enumerate(records):
            _primary(record)
            content = _text(record.get("content"), f"{lane}.content")
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            if record.get("content_hash") != digest:
                raise SkillInputError("CONTENT_HASH_MISMATCH")
            if record.get("scope") not in ("21MM", "24MM", "COMMON"):
                raise SkillInputError("INVALID_PROJECT_SCOPE")
            lane_hits.append(EvidenceItem(
                evidence_id=str(n + 1), scope=record["scope"],
                source_type={"sql":"SQL_EXACT", "dense":"SEMANTIC", "sparse":"QDRANT_SPARSE"}[lane],
                content=content, content_id=_int(record.get("content_id"), "content_id"),
                document_id=_text(record.get("document_id"), "document_id"),
                file_name=record["file_name"], specification_version=_text(record.get("version"), "version"),
                metadata={"document_role":record["document_role"], "content_hash":digest},
            ))
        lanes[lane] = lane_hits
    # An offline JSON document cannot certify Qdrant's SQL hydration. Require
    # an explicitly matching SQL snapshot before showing a vector ranking.
    sql_keys = {(x.scope, x.document_id, x.specification_version, x.content_id, x.content)
                for x in lanes["sql"]}
    for lane in ("dense", "sparse"):
        lanes[lane] = [x for x in lanes[lane] if
                       (x.scope, x.document_id, x.specification_version, x.content_id, x.content) in sql_keys]
    ranked = fuse_spec_rrf(lanes["sql"], lanes["dense"], sparse_hits=lanes["sparse"],
                           max_candidates=MAX_RESULT_ITEMS)
    return {"findings": [{"document_id": x.document_id, "content_id":x.content_id,
                          "version": x.specification_version,
                          "rrf_score":x.metadata["rrf_score"],
                          "retrieval_sources":x.metadata["retrieval_sources"]} for x in ranked],
            "evidence_level": "OFFLINE_RANKING_ONLY", "needs_human_review": True,
            "limitations": ["传入 SQL 结果为离线快照，未建立真实数据库来源认证", "排名分数不是工程可信度"]}


def _evidence(value: dict[str, Any]) -> dict[str, Any]:
    answer = _text(value.get("answer"), "answer")
    records = _items(value.get("evidence"), "evidence")
    if not records:
        raise SkillInputError("NO_ADMITTED_EVIDENCE")
    seen = set()
    objects = []
    for item in records:
        eid = str(item.get("id") or "")
        if not eid.isdecimal() or not 1 <= int(eid) <= 999999 or eid in seen or eid.startswith("0"):
            raise SkillInputError("INVALID_OR_DUPLICATE_CITATION_ID")
        seen.add(eid)
        objects.append(SimpleNamespace(evidence_id=eid, content=_text(item.get("text"), "evidence.text"),
                                       citation_label=_text(item.get("label", ""), "evidence.label", optional=True)))
    allowed = tuple(sorted(seen, key=int))
    result = validate_enterprise_answer(answer, [], route="ENGINEERING_AGENT", allowed_ids=allowed)
    if result.accepted:
        result = validate_claim_anchors(answer, admitted_evidence=objects, allowed_ids=allowed)
    return {"findings": [{"accepted": result.accepted, "reason":result.reason,
                          "cited_ids": list(result.cited_ids)}],
            "evidence_level": "OFFLINE_CITATION_AND_LITERAL_ANCHORS_ONLY",
            "needs_human_review": True,
            "limitations": ["引用编号、参数字面与版本匹配不等于条件适用或语义蕴含成立", "样例证据来自离线输入，不证明数据库身份"]}


def _quality(value: dict[str, Any]) -> dict[str, Any]:
    # Existing evaluator enforces dataset types and explicit reviewer/source digest.
    from scripts.evaluate_answer_gate import evaluate_case
    cases = _items(value.get("cases"), "cases", max_items=200)
    if not cases:
        raise SkillInputError("EMPTY_GOLDEN_CASES")
    ids: set[str] = set()
    results = []
    for case in cases:
        result = evaluate_case(case)
        if result["case_id"] in ids:
            raise SkillInputError("DUPLICATE_GOLDEN_CASE_ID")
        ids.add(result["case_id"])
        results.append(result)
    return {"findings": results, "matched": sum(x["matched"] for x in results),
            "total":len(results),
            "human_reviewed_real_cases":sum(x["dataset_type"] == "HUMAN_REVIEWED_REAL" for x in results),
            "evidence_level":"OFFLINE_DETERMINISTIC_GATE_EVALUATION",
            "needs_human_review": True,
            "limitations": ["规则评测不是工程语义蕴含评测", "没有真实数据库及人工 Golden 独立核验，不得授予生产签收"]}


_EXECUTORS = {
    "spec-requirement-extraction": _extract,
    "content-relation-audit": _relation,
    "spec-version-impact": _version,
    "engineering-hybrid-retrieval": _hybrid,
    "engineering-qa-bug-analysis": _bug,
    "rag-evidence-verification": _evidence,
    "rag-quality-gate": _quality,
}


def execute_skill(skill_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Fixed dispatch, not dynamic imports or model-authored executable actions."""
    if skill_name not in SKILLS or skill_name not in _EXECUTORS:
        raise SkillInputError("UNKNOWN_SKILL")
    value = _map(payload, "payload")
    details = _EXECUTORS[skill_name](value)
    return {"skill": skill_name, "status": "EVALUATED_OFFLINE",
            "production_acceptance": "NOT_ASSESSED", "database_access": "NONE",
            "database_mutations": 0, **details}
