"""Stage 1 review of *existing pipeline JSON* from authorised local files.

Read-only and privacy-preserving: the report contains no source text, document
names, addresses, emails, or sensitive identifiers. It does not infer approved
requirement relationships, alter specification identity, or claim accuracy.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from .filtered_json_diagnostics import read_limited_json
from .stage1_dcm import inspect_dcm_sources
from .requirements import extract_requirement_units
from .stage1_semantics import inspect_requirement_signals, pdf_table_fragment_pairs
from .stage1_review_evidence import make_pointer, safe_page, safe_section_id

_SCHEMA = "stage1-pipeline-review-v1"
_PROJECTS = frozenset({"21MM", "24MM", "COMMON"})
_VERSION = re.compile(r"^v?\d+(?:\.\d+){1,3}$", re.I)
_REFERENCE_SUFFIX = re.compile(
    r"(?:\s+|を)参照(?:する|ください|下さい)?\s*$|"
    r"(?:\s+|を)参照先\s*$|\bsee\s+(?:section|chapter)\s+\d+(?:\.\d+)*\s*$", re.I
)
_NUMERIC_SECTION = re.compile(r"\d+(?:\.\d+){1,6}\Z")
_COVER_ROW = re.compile(
    r"(?:SpecID\.|Spec\s*ID)\s*\|\s*(?P<code>A?\d{3,4})\s*\|\s*"
    r"(?:Ver\.|Version)\s*\|\s*(?P<ver>\d+(?:\.\d+){1,3})", re.I,
)
_MAX_FILENAME = 512


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()[:20]


def _simple_version(value: Any) -> str | None:
    if not isinstance(value, str) or not _VERSION.fullmatch(value):
        return None
    return "v" + value.removeprefix("v").removeprefix("V")


def _check_items(root: Any, *, max_docs: int, max_contents: int, max_chars: int, max_sections: int) -> list[dict]:
    if not isinstance(root, list) or not 1 <= len(root) <= max_docs:
        raise ValueError("Input must contain a bounded nonempty list of pipeline documents")
    total_contents = total_chars = total_sections = 0
    identities: set[tuple[str, str]] = set()
    for item in root:
        if not isinstance(item, dict) or not all(k in item for k in ("document", "metadata", "sections", "contents")):
            raise ValueError("Expected standard pipeline JSON: document/metadata/sections/contents")
        doc, metadata, sections, contents = (item[k] for k in ("document", "metadata", "sections", "contents"))
        if not isinstance(doc, dict) or not isinstance(metadata, dict) or not isinstance(sections, list) or not isinstance(contents, list):
            raise ValueError("Invalid pipeline JSON structure")
        ident = doc.get("document_id")
        project = doc.get("project_code") or metadata.get("project_code")
        if not isinstance(ident, str) or not 1 <= len(ident) <= _MAX_FILENAME or project not in _PROJECTS:
            raise ValueError("Invalid scoped document identifier")
        name = doc.get("file_name", "")
        if not isinstance(name, str) or len(name) > _MAX_FILENAME:
            raise ValueError("Invalid document name budget")
        if (project, ident) in identities:
            raise ValueError("Duplicate pipeline document identity")
        identities.add((project, ident))
        total_contents += len(contents)
        total_sections += len(sections)
        if total_sections > max_sections:
            raise ValueError("Section budget exceeded")
        if total_contents > max_contents:
            raise ValueError("Content budget exceeded")
        for unit in contents:
            if (not isinstance(unit, dict) or not isinstance(unit.get("text"), str)
                    or (unit.get("section_id") is not None
                        and (not isinstance(unit["section_id"], str)
                             or len(unit["section_id"]) > 512))):
                raise ValueError("Invalid content record")
            total_chars += len(unit["text"])
            if total_chars > max_chars:
                raise ValueError("Text budget exceeded")
        for section in sections:
            if (not isinstance(section, dict) or not isinstance(section.get("id"), str)
                    or not 1 <= len(section["id"]) <= 512
                    or any(not isinstance(section.get(k), (str, type(None)))
                           or len(section.get(k) or "") > 512
                           for k in ("title_jp", "title_en"))):
                raise ValueError("Invalid section record")
    return root


def review_pipeline_documents(
    documents: list[dict], *, max_docs: int = 8,
    max_contents: int = 30_000, max_chars: int = 18_000_000,
    max_findings: int = 200, max_sections: int = 30_000,
    max_evidence: int = 200,
) -> dict[str, Any]:
    if not all(type(v) is int and v > 0 for v in (max_docs, max_contents, max_chars, max_findings, max_sections, max_evidence)):
        raise ValueError("Positive integer budgets required")
    items = _check_items(documents, max_docs=max_docs, max_contents=max_contents,
                         max_chars=max_chars, max_sections=max_sections)
    reports = []
    findings = []
    evidence_queue: list[dict[str, Any]] = []
    evidence_total = 0
    cover_hints: list[tuple[str, str, str, str, str]] = []
    primaries: list[tuple[str, str, str, str | None, str]] = []
    for item in items:
        document, metadata = item["document"], item["metadata"]
        sections, contents = item["sections"], item["contents"]
        project = document.get("project_code") or metadata.get("project_code")
        identity_hash = _digest(project + "\0" + document["document_id"])
        role = metadata.get("document_role") or "PRIMARY_CANDIDATE"
        role = role if role in ("SUPPORTING", "PRIMARY_CANDIDATE") else "UNRECOGNISED_ROLE"
        raw_format = metadata.get("source_format")
        source_format = (raw_format.lower() if isinstance(raw_format, str)
                         and len(raw_format) <= 16 else "unknown")
        if source_format not in {"pdf", "docx", "xlsx", "pptx", "txt", "json", "unknown"}:
            source_format = "unknown"
        rev = _simple_version(metadata.get("specification_version"))
        local: list[dict] = []

        def issue(code: str, *, section_id: str | None = None, severity: str = "REVIEW", page: int | None = None) -> None:
            record = {"code": code, "severity": severity,
                      "section_id": safe_section_id(section_id), "page": safe_page(page)}
            local.append(record)
            if len(findings) < max_findings:
                findings.append({"document_hash": identity_hash, **record})

        if role == "SUPPORTING":
            if len(contents) > 100:
                issue("LARGE_SUPPORTING_DOCUMENT_METADATA_NOT_REQUIREMENTS")
            if metadata.get("qa_index_policy") != "EXCLUDE":
                issue("SUPPORTING_INDEX_POLICY_NOT_EXPLICITLY_EXCLUDED", severity="BLOCK")
            if metadata.get("specification_key") or metadata.get("specification_code"):
                issue("SUPPORTING_DOCUMENT_HAS_PRIMARY_IDENTITY", severity="BLOCK")
            # Only explicit visible in-sheet Cover Sheet identity is a hint.
            if metadata.get("supporting_document_type") == "COVER_SHEET":
                found: set[tuple[str, str]] = set()
                for c in contents[:max_contents]:
                    if c.get("section_id") and c.get("text"):
                        for match in _COVER_ROW.finditer(c["text"]):
                            found.add((match.group("code").upper(), "v" + match.group("ver")))
                if len(found) == 1:
                    code, version = next(iter(found))
                    cover_hints.append((project, identity_hash, code, version, document.get("file_name", "")))
                    rev_filename = _simple_version(metadata.get("parent_specification_version"))
                    if rev_filename and rev_filename != version:
                        issue("COVER_FILENAME_VERSION_DIFFERS_FROM_SHEET_VERSION")
                elif len(found) > 1:
                    issue("COVER_VISIBLE_IDENTITIES_AMBIGUOUS", severity="BLOCK")
        else:
            if not metadata.get("specification_code"):
                issue("PRIMARY_SPECIFICATION_CODE_UNRESOLVED")
            if not rev:
                issue("PRIMARY_SPECIFICATION_VERSION_UNRESOLVED")
            primaries.append((project, identity_hash, str(document.get("file_name") or ""), rev, str(metadata.get("specification_code") or "")))

        # Duplicate IDs destroy deterministic association between physical
        # paragraphs and sections. Report one issue per ID, without leaking an
        # arbitrary identifier that may itself contain confidential text.
        section_ids = Counter(s["id"] for s in sections)
        for duplicate_id, count in section_ids.items():
            if count > 1:
                issue("DUPLICATE_SECTION_IDENTIFIER_REQUIRES_REVIEW", section_id=duplicate_id,
                      severity="BLOCK")
        prev_major: int | None = None
        prev_minor: int | None = None
        for section in sections:
            sid = section["id"]
            title = section.get("title_jp") or section.get("title_en") or ""
            if isinstance(title, str) and _REFERENCE_SUFFIX.search(title):
                issue("CROSS_REFERENCE_MISREAD_AS_SECTION_HEADING", section_id=sid,
                      page=section.get("page_number"), severity="BLOCK")
            if _NUMERIC_SECTION.fullmatch(sid) and "." in sid:
                parts = sid.split(".")
                major, minor = int(parts[0]), int(parts[1])
                if prev_major == major and prev_minor is not None and minor < prev_minor:
                    issue("OUT_OF_ORDER_SECTION_NUMBER_REQUIRES_REVIEW", section_id=sid, page=section.get("page_number"))
                prev_major, prev_minor = major, minor
        # Parse each exported source line once, rather than searching all
        # contents for each section (quadratic on long specification books).
        # Matched text is only a *possible* cross-reference; no node is edited.
        source_ref_line = re.compile(
            r"(?m)^[ \t]*(?P<sid>\d+(?:\.\d+){0,6})[ \t]+"
            r"(?P<title>[^\r\n]{1,512}?)"
            r"[ \t]*(?:参照|を参照|see[ \t]+section[ \t]+\d+(?:\.\d+)*)[ \t]*$", re.I,
        )
        reference_sections: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for section in sections:
            title = section.get("title_jp") or section.get("title_en") or ""
            if _NUMERIC_SECTION.fullmatch(section["id"]) and title.strip():
                reference_sections.setdefault((section["id"], title.strip().casefold()), []).append(section)
        seen_reference_sections: set[int] = set()
        for content_index, content in enumerate(contents, 1):
            for match in source_ref_line.finditer(content["text"]):
                key = (match.group("sid"), match.group("title").strip().casefold())
                for section in reference_sections.get(key, ()):
                    section_identity = id(section)
                    if section_identity in seen_reference_sections:
                        continue
                    seen_reference_sections.add(section_identity)
                    issue("SECTION_NODE_MAY_BE_CROSS_REFERENCE_CELL", section_id=section["id"],
                          severity="REVIEW", page=section.get("page_number"))
                    evidence_total += 1
                    if len(evidence_queue) < max_evidence:
                        evidence_queue.append(make_pointer(
                            identity_hash, content, content_index, match.start(), match.end(),
                            category="SECTION_REFERENCE", status="HEADING_UNVERIFIED",
                        ))

        line_units = requirement_candidates = 0
        # Diagnostic coverage is not recall: it only tells reviewers how many
        # line-level source items our *existing* extraction path considered.
        # Bound this optional probe so a long-form export cannot monopolize
        # the local desktop process. Supporting metadata is not requirement text.
        extraction_complete = role != "SUPPORTING"
        if role != "SUPPORTING":
            analysed_chars = analysed_items = 0
            for index, content in enumerate(contents, 1):
                body = content["text"]
                if analysed_chars + len(body) > 1_000_000 or analysed_items >= 1_000:
                    extraction_complete = False
                    break
                analysed_chars += len(body)
                analysed_items += 1
                try:
                    units = extract_requirement_units(
                        document_id=identity_hash, content_id=index,
                        section_id=str(content.get("section_id") or "unknown"), text=body,
                    )
                except ValueError:
                    extraction_complete = False
                    issue("REQUIREMENT_UNIT_LIMIT_NEEDS_UPSTREAM_CHUNKING")
                    break
                line_units += len(units)
                requirement_candidates += sum(u.signals.normative for u in units)
            if not extraction_complete:
                issue("REQUIREMENT_PROBE_BUDGET_LIMITED")
            if (extraction_complete and line_units >= 200 and
                    requirement_candidates * 10 < line_units):
                issue("LOW_NORMATIVE_SIGNAL_COVERAGE_REVIEW_RECALL")
        # DCM lifecycle evidence is lexical and read-only. A sequence of
        # initial/before/after mentions is not a verified state transition.
        dcm = {
            "dcm_lines_sampled": 0, "related_ui_lines_sampled": 0,
            "phase_counts_sampled": {},
            "multi_phase_lines_sampled": 0, "multi_phase_sections_sampled": 0,
            "exception_lines_sampled": 0, "negation_lines_sampled": 0,
            "probe_complete": True, "state_transitions_verified": False,
            "event_action_scope_verified": False, "evidence_queue": [],
            "evidence_total": 0,
        }
        if role != "SUPPORTING":
            dcm = inspect_dcm_sources(identity_hash, contents, max_evidence=max_evidence)
            evidence_total += dcm["evidence_total"]
            evidence_queue.extend(dcm["evidence_queue"][:max(0, max_evidence - len(evidence_queue))])
            if dcm["related_ui_lines_sampled"]:
                issue("DCM_RELATED_UI_CONTEXT_REQUIRES_REVIEW")
            if dcm["multi_phase_lines_sampled"]:
                issue("DCM_MULTIPLE_PHASES_IN_SOURCE_LINE_REQUIRES_REVIEW")
            if dcm["multi_phase_sections_sampled"]:
                issue("DCM_MULTIPHASE_SECTION_REQUIRES_REVIEW")
            if dcm["exception_lines_sampled"]:
                issue("DCM_EXCEPTION_OR_FALLBACK_SCOPE_UNVERIFIED")
            if dcm["negation_lines_sampled"]:
                issue("DCM_NEGATED_STATE_SCOPE_UNVERIFIED")
            if dcm["dcm_lines_sampled"] and not dcm["probe_complete"]:
                issue("DCM_PROBE_BUDGET_LIMITED")
        semantic = {
            "language_counts": {}, "bilingual_section_count": 0,
            "bilingual_document": False, "mixed_language_count": 0,
            "scope_candidates_sampled": 0, "scope_unresolved_sampled": 0,
            "probe_complete": True,
            "evidence_queue": [], "evidence_total": 0,
        }
        if role != "SUPPORTING":
            semantic = inspect_requirement_signals(
                identity_hash, contents, max_evidence=max_evidence,
            )
            evidence_total += semantic["evidence_total"]
            evidence_queue.extend(semantic["evidence_queue"][:max(0, max_evidence - len(evidence_queue))])
            if semantic["bilingual_document"]:
                issue("JA_EN_REQUIREMENTS_EQUIVALENCE_NOT_VERIFIED")
            if semantic["mixed_language_count"]:
                issue("MIXED_LANGUAGE_NORMATIVE_SOURCE_REQUIRES_REVIEW")
            if semantic["scope_unresolved_sampled"]:
                issue("CONDITION_ACTION_SCOPE_UNRESOLVED_REQUIRES_REVIEW")
            if not semantic["probe_complete"]:
                issue("LANGUAGE_AND_SCOPE_PROBE_BUDGET_LIMITED")
        if source_format == "pdf":
            for sid, page, before, after in pdf_table_fragment_pairs(contents):
                issue("PDF_CROSSPAGE_TABLE_FRAGMENT_REQUIRES_NATIVE_VERIFICATION",
                      section_id=sid, page=page)
                evidence_total += 2
                # A one-sided pointer would misleadingly suggest that only
                # one page was checked. Append both or neither under budget.
                if len(evidence_queue) + 2 <= max_evidence:
                    for index, start, end in (before, after):
                        evidence_queue.append(make_pointer(
                            identity_hash, contents[index-1], index, start, end,
                            category="PDF_TABLE_FRAGMENT", status="CROSSPAGE_UNVERIFIED",
                        ))
            if any(not c.get("metadata") for c in contents):
                issue("PROCESSED_PDF_CONTENT_HAS_NO_NATIVE_TEXT_OFFSETS")
            # A long table flattened into one string can no longer preserve
            # column identity. This is a layout warning, not a fake rule.
            table_sections = set()
            for c in contents:
                text = c["text"]
                if (len(text) >= 200 and text.count("\n") >= 20 and
                        sum(marker in text for marker in ("○", "×", "0x", "Parameter", "Function", "機能")) >= 2):
                    sid = str(c.get("section_id"))
                    if sid not in table_sections:
                        table_sections.add(sid)
                        issue("PDF_FLATTENED_TABLE_STRUCTURE_REVIEW", section_id=sid, page=c.get("page_number"))
        else:
            # Logical sheet numbers are *not* physical PDF page numbers.
            if source_format == "xlsx" and metadata.get("xlsx_page_number_semantics") == "logical_sheet_number":
                issue("XLSX_PAGE_NUMBER_IS_LOGICAL_SHEET_NOT_PDF_PAGE")
        reports.append({
            "document_hash": identity_hash, "project": project, "role": role,
            "source_format": source_format, "version_detected": rev is not None,
            "specification_code_detected": bool(metadata.get("specification_code")),
            "chapter_count": len(item.get("chapters") or []),
            "section_count": len(sections), "content_count": len(contents),
            "line_level_source_units_sampled": line_units,
            "normative_signal_units_sampled": requirement_candidates,
            # These are redacted, sampled diagnostic counts, not accuracy,
            # human-approved translation pairs or requirements.
            "normative_language_counts_sampled": semantic["language_counts"],
            "bilingual_section_count_sampled": semantic["bilingual_section_count"],
            "scope_candidates_sampled": semantic["scope_candidates_sampled"],
            "scope_unresolved_sampled": semantic["scope_unresolved_sampled"],
            "dcm_lines_sampled": dcm["dcm_lines_sampled"],
            "dcm_related_ui_lines_sampled": dcm["related_ui_lines_sampled"],
            "dcm_phase_counts_sampled": dcm["phase_counts_sampled"],
            "dcm_multi_phase_lines_sampled": dcm["multi_phase_lines_sampled"],
            "dcm_multi_phase_sections_sampled": dcm["multi_phase_sections_sampled"],
            "dcm_exception_lines_sampled": dcm["exception_lines_sampled"],
            "dcm_negation_lines_sampled": dcm["negation_lines_sampled"],
            "dcm_probe_complete": dcm["probe_complete"],
            "dcm_state_transitions_verified": False,
            "dcm_event_action_scope_verified": False,
            "requirement_probe_complete": extraction_complete,
            "candidate_counts_are_not_recall_or_gold": True,
            "evidence_pointers_are_pipeline_json_only": True,
            "issue_counts": dict(sorted(Counter(i["code"] for i in local).items())),
            "requires_manual_review": bool(local) or bool(dcm["dcm_lines_sampled"]),
        })

    relations = []
    for project, cover_hash, code, version, cover_name in cover_hints:
        for primary_project, primary_hash, primary_name, primary_version, primary_code in primaries:
            # Safe suggestion only. Cross-document name anchor cannot prove
            # identity, especially when an old Cover Sheet is repurposed.
            norm = lambda v: re.sub(r"[^a-z0-9]", "", str(v).lower())
            terms = ("bluetooth", "audio")
            names_match = all(t in norm(cover_name) and t in norm(primary_name) for t in terms)
            if (project == primary_project and primary_version == version and
                    names_match and (not primary_code or primary_code == code)):
                relations.append({
                    "supporting_document_hash": cover_hash,
                    "primary_document_hash": primary_hash,
                    "candidate_specification_code": code,
                    "candidate_version": version,
                    "status": "MANUAL_VERIFICATION_REQUIRED",
                    "authority": "SHEET_IDENTITY_AND_NAME_HINT_ONLY",
                    "automatically_assign_identity": False,
                })
    return {
        "schema": _SCHEMA, "status": "DIAGNOSTIC_ONLY",
        "document_count": len(reports), "documents": reports,
        "finding_count": sum(sum(report["issue_counts"].values()) for report in reports),
        "findings": findings,
        "findings_truncated": max(0, sum(sum(r["issue_counts"].values()) for r in reports) - len(findings)),
        "identity_candidates": relations,
        "evidence_queue_schema": "stage1-pipeline-source-pointer-v1",
        "evidence_queue": evidence_queue,
        "evidence_queue_count": len(evidence_queue),
        "evidence_queue_truncated": max(0, evidence_total - len(evidence_queue)),
        "source_verified": False, "quality_metrics_available": False,
        "database_modified": False, "qdrant_modified": False,
        "original_native_layout_verified": False,
        "warnings": ["PIPELINE_JSON_IS_NOT_AN_ORIGINAL_FILE", "NO_AUTOMATIC_SPECIFICATION_IDENTITY_APPROVAL",
                     "NO_REQUIREMENT_RELATIONSHIP_APPROVAL", "MANUAL_PRIVACY_REVIEW_BEFORE_SHARING"],
    }


def read_pipeline_files(paths: list[Path], *, max_input_bytes: int = 12_000_000) -> list[dict[str, Any]]:
    if not paths or len(paths) > 8:
        raise ValueError("Provide 1..8 bounded pipeline JSON files")
    return [read_limited_json(p, max_input_bytes=max_input_bytes) for p in paths]
