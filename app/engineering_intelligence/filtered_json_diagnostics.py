"""Stage 1 private/filtered JSON diagnostics; no database, vector store or LLM.

A transformed excerpt is *never* authoritative original-source evidence, even
when it has the same length or happens to match another excerpt. This module
reports extraction behaviour to help review anonymised samples later; it does
not calculate accuracy, modify SQL, approve rules, or reverse redactions.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .atomic import extract_atomic_candidates
from .condition_scope import extract_requirement_scope
from .requirements import extract_requirement_units, validate_extracted_units
from .stage1_evaluation_v3 import COMPLEXITY_TAGS, SOURCE_KINDS

SCHEMA_FILTERED = "stage1-filtered-samples-v1"
SCHEMA_SOURCE = "stage1-source-items-v1"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_PROJECTS = frozenset({"21MM", "24MM"})
_TRANSFORMS = frozenset({"MASKED", "REPLACED", "EXCERPTED", "SYNTHETIC"})
_OFFSET_POLICIES = frozenset({"PRESERVED", "CHANGED", "UNKNOWN"})
_SUSPICIOUS = {
    "EMAIL_PATTERN": re.compile(r"(?<![\w.])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?!\w)"),
    "IPV4_PATTERN": re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])"),
    "WINDOWS_PATH_PATTERN": re.compile(r"(?i)(?:[A-Z]:\\|\\\\[^\s\\]+\\)"),
    "CREDENTIAL_LABEL": re.compile(r"(?i)(?:password|api[_-]?key|secret|token|パスワード|密码)\s*[:=]")
}


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()


def _case_identity(project: str, doc: str, cid: int, section: str) -> str:
    # Scoped and unambiguous; hashes are for diagnostic privacy, not proof.
    return _sha(json.dumps([project, doc, cid, section], ensure_ascii=False, separators=(",", ":")))[:24]


def _bounded_name(value: Any, field: str, *, maximum: int = 512) -> str:
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or any(ord(c) < 32 for c in value)):
        raise ValueError(f"Invalid {field} identifier")
    return value


@dataclass(frozen=True, slots=True)
class CaseDiagnostic:
    identity_hash_prefix: str
    text_hash_prefix: str
    project: str
    source_kind: str
    transformed: bool
    offset_policy: str
    character_count: int
    source_units: int
    requirement_candidates: int
    context_candidates: int
    table_units: int
    unresolved_scopes: int
    review_reason_counts: dict[str, int]
    # No raw text, paths, business identifiers or source excerpts.
    finding_offsets: tuple[dict[str, Any], ...]
    findings_truncated: int
    status: str


@dataclass(frozen=True, slots=True)
class FilteredJsonReport:
    schema: str
    result: str
    accepted_input_schema: str
    case_count: int
    case_count_by_project: dict[str, int]
    case_count_by_kind: dict[str, int]
    total_characters: int
    requirement_candidates: int
    unresolved_scopes: int
    table_units: int
    transformation_present: bool
    source_verified: bool
    original_offsets_verified: bool
    quality_metrics_available: bool
    warnings: tuple[str, ...]
    privacy_pattern_case_counts: dict[str, int]
    case_diagnostics: tuple[CaseDiagnostic, ...]
    review_required: bool


def diagnose_filtered_cases(
    data: dict[str, Any], *, max_cases: int = 2_000,
    max_chars: int = 5_000_000, max_findings_per_case: int = 12,
    max_atomic_per_unit: int = 64,
) -> FilteredJsonReport:
    """Bounded, fail-closed, provenance-sensitive local diagnostic.

    Input JSON explicitly declares its schema and, if filtered, transformation
    and offset policy. All reported offsets are relative to the *supplied*
    text, never an inference about offsets in a missing original PDF or SQL.
    """
    if not isinstance(data, dict) or data.get("schema") not in (SCHEMA_FILTERED, SCHEMA_SOURCE):
        raise ValueError("Expected explicit stage1-filtered-samples-v1 or stage1-source-items-v1 schema")
    schema = data["schema"]
    rows = data.get("cases")
    if type(max_cases) is not int or max_cases < 1 or type(max_chars) is not int or max_chars < 1:
        raise ValueError("Positive source budget required")
    if (type(max_findings_per_case) is not int or not 0 <= max_findings_per_case <= 100
            or type(max_atomic_per_unit) is not int or not 1 <= max_atomic_per_unit <= 1024):
        raise ValueError("Invalid bounded diagnostics configuration")
    if not isinstance(rows, list) or not rows or len(rows) > max_cases:
        raise ValueError("Nonempty bounded cases list required")
    manifest = data.get("manifest")
    if schema == SCHEMA_FILTERED:
        if not isinstance(manifest, dict) or manifest.get("transformation") not in _TRANSFORMS:
            raise ValueError("Explicit filtered JSON manifest.transformation required")
        if manifest.get("offset_policy") not in _OFFSET_POLICIES:
            raise ValueError("Explicit offset_policy required; never assume original offsets")
    diagnostics: list[CaseDiagnostic] = []
    project_counts: Counter[str] = Counter()
    kind_counts: Counter[str] = Counter()
    privacy_counts: Counter[str] = Counter()
    seen: set[tuple[str, str, int]] = set()
    total_chars = 0
    candidates_count = unresolved = table_count = 0
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Each case must be an object")
        project = row.get("project")
        if project not in _PROJECTS:
            raise ValueError("Explicit 21MM/24MM project scope required")
        doc = _bounded_name(row.get("document_id"), "document_id")
        cid = row.get("content_id")
        if type(cid) is not int or cid < 1:
            raise ValueError("content_id must be a positive integer")
        section = _bounded_name(row.get("section_id"), "section_id")
        identity = (project, doc, cid)
        if identity in seen:
            raise ValueError("Duplicate project/document/content identity")
        seen.add(identity)
        kind = row.get("source_kind")
        if kind not in SOURCE_KINDS:
            raise ValueError("Explicit DOCX/PDF text/table source_kind required")
        tags = row.get("complexity_tags", ["NORMAL"])
        if (not isinstance(tags, list) or not tags or len(tags) > 4
                or any(type(tag) is not str or tag not in COMPLEXITY_TAGS for tag in tags)
                or len(set(tags)) != len(tags)
                or ("NORMAL" in tags and len(tags) != 1)):
            raise ValueError("Invalid bounded complexity_tags")
        body = row.get("text")
        if not isinstance(body, str) or not body:
            raise ValueError("Nonempty literal text required; no reconstruction from digest")
        total_chars += len(body)
        if total_chars > max_chars:
            raise ValueError("Total supplied character budget exceeded")
        expected_hash = row.get("source_sha256")
        if expected_hash is None or not isinstance(expected_hash, str) or not _SHA256.fullmatch(expected_hash):
            raise ValueError("source_sha256 must be lowercase SHA-256 of supplied text")
        if _sha(body) != expected_hash:
            raise ValueError("Supplied text SHA-256 mismatch; no diagnostic returned")
        if schema == SCHEMA_FILTERED:
            # Even "PRESERVED" relative lengths cannot establish a trusted
            # original-source character coordinate or PDF page mapping.
            policy = manifest["offset_policy"]
            transformed = True
        else:
            policy = "UNKNOWN"
            transformed = False
        for name, pattern in _SUSPICIOUS.items():
            if pattern.search(body):
                privacy_counts[name] += 1
        project_counts[project] += 1
        kind_counts[kind] += 1
        findings: list[dict[str, Any]] = []
        omitted = 0
        local_candidates = local_context = local_table = local_unresolved = 0
        reasons: Counter[str] = Counter()
        units = extract_requirement_units(document_id=doc, content_id=cid, section_id=section, text=body)
        validate_extracted_units(body, units)
        for unit in units:
            if unit.signals.structural_kind in ("TABLE_ROW", "TABLE_SEPARATOR"):
                local_table += 1
            atoms = extract_atomic_candidates(unit, max_candidates=max_atomic_per_unit)
            for atom in atoms:
                if atom.role == "REQUIREMENT_CANDIDATE":
                    local_candidates += 1
                    evidence = extract_requirement_scope(unit, atom)
                    if evidence.status == "UNRESOLVED":
                        local_unresolved += 1
                    reasons.update(evidence.review_reasons)
                else:
                    local_context += 1
                    reasons.update(atom.review_reasons)
                if len(findings) < max_findings_per_case:
                    findings.append({"start": atom.start_in_content, "end": atom.end_in_content,
                                     "role": atom.role,
                                     "review_status": atom.review_status})
                else:
                    omitted += 1
        candidates_count += local_candidates
        table_count += local_table
        unresolved += local_unresolved
        diagnostics.append(CaseDiagnostic(
            identity_hash_prefix=_case_identity(project, doc, cid, section),
            text_hash_prefix=expected_hash[:16], project=project, source_kind=kind,
            transformed=transformed, offset_policy=policy, character_count=len(body),
            source_units=len(units), requirement_candidates=local_candidates,
            context_candidates=local_context, table_units=local_table,
            unresolved_scopes=local_unresolved, review_reason_counts=dict(sorted(reasons.items())),
            finding_offsets=tuple(findings), findings_truncated=omitted,
            status="REVIEW_ONLY_NOT_GOLD"))
    warnings = ["NO_GOLD_LABELS_NO_PRECISION_RECALL_OR_F1",
                "LOCAL_PATTERN_SCAN_NOT_PROOF_OF_DEIDENTIFICATION",
                "IDENTITY_HASHES_AND_OFFSETS_REQUIRE_PRIVACY_REVIEW_BEFORE_SHARING",
                "NO_ORIGINAL_SQL_OR_NATIVE_PDF_DOCX_BINDING"]
    if schema == SCHEMA_FILTERED:
        warnings.append("FILTERED_TEXT_OFFSETS_APPLY_ONLY_TO_SUPPLIED_TEXT")
    return FilteredJsonReport(
        schema="stage1-filtered-diagnostics-v1", result="UNLABELLED_DIAGNOSTIC_ONLY",
        accepted_input_schema=schema, case_count=len(rows),
        case_count_by_project=dict(sorted(project_counts.items())),
        case_count_by_kind=dict(sorted(kind_counts.items())),
        total_characters=total_chars, requirement_candidates=candidates_count,
        unresolved_scopes=unresolved, table_units=table_count,
        transformation_present=(schema == SCHEMA_FILTERED),
        source_verified=False, original_offsets_verified=False,
        quality_metrics_available=False, warnings=tuple(warnings),
        privacy_pattern_case_counts=dict(sorted(privacy_counts.items())),
        case_diagnostics=tuple(diagnostics), review_required=True)


def diagnostic_dict(data: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    return asdict(diagnose_filtered_cases(data, **kwargs))


def read_limited_json(path: Path, *, max_input_bytes: int = 12_000_000) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size > max_input_bytes:
        raise ValueError("Missing or oversized JSON input")
    with path.open("rb") as fh:
        raw = fh.read(max_input_bytes + 1)
    if len(raw) > max_input_bytes:
        raise ValueError("JSON byte budget exceeded")
    def no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Duplicate JSON object key; ambiguous input rejected")
            value[key] = item
        return value

    result = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=no_duplicate_keys)
    if not isinstance(result, dict):
        raise ValueError("JSON root must be an object")
    return result
