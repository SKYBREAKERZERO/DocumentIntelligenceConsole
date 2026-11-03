"""Versioned allow-list. Skill markdown cannot register executable code."""
from __future__ import annotations

from pathlib import Path
from typing import Final

SKILLS: Final[tuple[str, ...]] = (
    "spec-requirement-extraction", "content-relation-audit",
    "spec-version-impact", "engineering-hybrid-retrieval",
    "engineering-qa-bug-analysis", "rag-evidence-verification",
    "rag-quality-gate",
)
DOC_ROOT = Path(__file__).resolve().parents[2] / "config" / "skills"


def list_skills() -> tuple[dict[str, str], ...]:
    """Discovery reads metadata, never executes untrusted Skill files."""
    import json
    records = []
    for slug in SKILLS:
        manifest = DOC_ROOT / slug / "manifest.json"
        metadata = json.loads(manifest.read_text(encoding="utf-8"))
        if metadata.get("name") != slug or metadata.get("database_write") is not False:
            raise ValueError(f"Skill manifest drift: {slug}")
        records.append({"name": slug, "stage": metadata["stage"],
                        "capability": metadata["capability"],
                        "production_acceptance": "NOT_ASSESSED"})
    return tuple(records)


def suggest_skills(question: str) -> tuple[str, ...]:
    """Non-executing, deterministic capability discovery for Stage 5 planning.

    A suggestion NEVER confers database access or bypasses permission checks.
    An empty/chat question returns no skills.
    """
    import re
    query = str(question or "")[:4000]
    if not query.strip():
        return ()
    patterns = (
        ("engineering-qa-bug-analysis", r"BUG|QA|JIRA|故障|缺陷|不具合|現象|现象"),
        ("spec-version-impact", r"版本|改版|旧版|新版|变更|変更|revision|version|diff"),
        ("content-relation-audit", r"关联|关系|引用|参照|依赖|影响|link|relation|dependency|reference"),
        ("engineering-hybrid-retrieval", r"检索|召回|向量|sparse|dense|RRF|rerank|search"),
        ("spec-requirement-extraction", r"式样|式樣|仕様|规格|SPEC|requirement|要求|条件|时序|表格|参数"),
        ("rag-quality-gate", r"Golden|评测|評価|基准|准确率|质量门禁|test\s*dataset|eval"),
    )
    selected = [name for name, pattern in patterns if re.search(pattern, query, re.I)]
    if selected and "rag-evidence-verification" not in selected:
        selected.append("rag-evidence-verification")
    return tuple(selected)
