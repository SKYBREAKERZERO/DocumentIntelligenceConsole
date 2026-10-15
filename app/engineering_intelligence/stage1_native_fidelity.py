"""v1.25 offline PDF-versus-pipeline *literal* fidelity review.

The PDF text layer is independent evidence, but a text match is NOT proof of
PDF table semantics, section authority, provenance or requirement correctness.
Never emit native or pipeline source text. No database / cloud side effects.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import Counter, defaultdict
from typing import Any

from .crosspage import suggest_crosspage_tables
from .native_layout import NativeLayoutReport
from .pipeline_json_review import review_pipeline_documents
from .stage1_review_evidence import safe_page, safe_section_id, verify_review_pointers

SCHEMA = "stage1-native-pipeline-fidelity-v1"


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()


def _normalized(value: str) -> str:
    # Intentionally do not casefold, translate, strip punctuation or NFKC-fold:
    # those changes can turn two distinct specification requirements into one.
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value)).strip()


def reconcile_pdf_pipeline(
    documents: list[dict[str, Any]], review: dict[str, Any],
    native: NativeLayoutReport, *, document_index: int = 1,
    max_contents: int = 2_000, max_lines: int = 100_000,
    max_group_lines: int = 4, max_anchor_chars: int = 2_048,
    min_anchor_chars: int = 12, max_entries: int = 400,
    max_crosspage_candidates: int = 100, max_crosspage_pairs: int = 10_000,
) -> dict[str, Any]:
    """Conservatively compare source lines; never declare semantic equivalence.

    `document_index` is 1-based to avoid disclosure of document IDs in CLI.
    Content budget truncation and unreadable / image-only PDFs are explicit.
    """
    budgets = (max_contents, max_lines, max_group_lines, max_anchor_chars,
               min_anchor_chars, max_entries, max_crosspage_candidates,
               max_crosspage_pairs)
    if (any(type(n) is not int or n < 1 for n in budgets)
            or min_anchor_chars > max_anchor_chars or max_group_lines > 12):
        raise ValueError("Invalid fidelity review budgets")
    if not isinstance(documents, list) or not 1 <= document_index <= len(documents):
        raise ValueError("Invalid pipeline document index")
    if review != review_pipeline_documents(documents):
        raise ValueError("Pipeline review drift; regenerate the original review")
    verify_review_pointers(review, documents)
    if (native.format != "PDF" or len(native.lines) > max_lines
            or not re.fullmatch(r"[0-9a-f]{64}", native.file_sha256)
            or type(native.source_size) is not int or native.source_size < 1):
        raise ValueError("Expected bounded, native PDF text evidence")
    selected = documents[document_index - 1]
    if str(selected["metadata"].get("source_format", "")).lower() != "pdf":
        raise ValueError("Pipeline document is not identified as PDF")
    contents = selected["contents"]
    section_ids = {s["id"] for s in selected["sections"]}
    page_count = native.pdf_page_count
    if page_count is not None and (type(page_count) is not int or page_count < 1):
        raise ValueError("Invalid native PDF page count")
    # No assumption that lines are in visual reading order. A cross-line
    # candidate is a literal contiguous sequence in the extractor's order.
    targets: dict[str, list[int]] = defaultdict(list)
    early: dict[int, str] = {}
    for i, item in enumerate(contents[:max_contents], 1):
        text = _normalized(item["text"])
        if len(text) < min_anchor_chars or sum(c.isalpha() for c in text) < 4:
            early[i] = "WEAK_NATIVE_TEXT_ANCHOR_REVIEW"
        elif len(text) > max_anchor_chars:
            early[i] = "LONG_SOURCE_NEEDS_MANUAL_ALIGNMENT"
        else:
            targets[text].append(i)
    # (page, first PDF line position, last position). A source can match many
    # native places; an exact match is only emitted when uniquely located.
    matches: dict[int, list[tuple[int, int, int]]] = defaultdict(list)
    pages: dict[int, list] = defaultdict(list)
    for line in native.lines:
        if (type(line.page_number) is not int or line.page_number < 1
                or (page_count is not None and line.page_number > page_count)):
            raise ValueError("Invalid native PDF line page")
        pages[line.page_number].append(line)
    for page_number, lines in pages.items():
        fragments = [_normalized(line.text) for line in lines]
        for start, fragment in enumerate(fragments):
            if not fragment:
                continue
            combined = ""
            for end in range(start, min(start + max_group_lines, len(lines))):
                if not fragments[end]:
                    break
                combined = (combined + " " + fragments[end]).strip()
                if len(combined) > max_anchor_chars:
                    break
                for index in targets.get(combined, ()):
                    matches[index].append((page_number, start, end))
    entries: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    structural_counts: Counter[str] = Counter()
    for index, content in enumerate(contents[:max_contents], 1):
        declared_page = safe_page(content.get("page_number"))
        raw_section = content.get("section_id")
        section_status = ("SECTION_ID_IN_EXPORTED_TREE" if raw_section in section_ids
                          else "SECTION_NOT_IN_EXPORTED_TREE")
        structural_counts[section_status] += 1
        found = matches.get(index, ())
        locator = None
        if index in early:
            status = early[index]
        elif declared_page is None:
            status = "PIPELINE_PAGE_UNRESOLVED"
        elif page_count is not None and declared_page > page_count:
            status = "PIPELINE_PAGE_OUT_OF_RANGE"
        elif not found:
            status = "NO_EXACT_NATIVE_TEXT_SEQUENCE_REVIEW"
        elif len(found) > 1:
            status = "AMBIGUOUS_REPEATED_NATIVE_TEXT_REVIEW"
        elif found[0][0] != declared_page:
            status = "EXACT_NATIVE_TEXT_ON_DIFFERENT_PAGE"
        else:
            status = "UNIQUE_NATIVE_LINE_CANDIDATE" if found[0][1] == found[0][2] else "UNIQUE_NATIVE_MULTILINE_CANDIDATE"
            # The following is an extractor-line locator, not a semantic or
            # authoritative section/requirement binding.
            _, first, last = found[0]
            start_line, end_line = pages[declared_page][first], pages[declared_page][last]
            locator = {
                "page": declared_page,
                "first_block": start_line.block_index,
                "first_line": start_line.line_index,
                "last_block": end_line.block_index,
                "last_line": end_line.line_index,
                "first_bbox": list(start_line.bbox),
                "last_bbox": list(end_line.bbox),
            }
        counts[status] += 1
        if len(entries) < max_entries:
            entries.append({
                "content_index": index, "page": declared_page,
                "section_id": safe_section_id(raw_section),
                "content_sha256": _sha(content["text"]),
                "text_alignment_status": status,
                "section_tree_status": section_status,
                "native_locator": locator,
                "semantic_relationship_verified": False,
            })
    # Enumerate the *entire* adjacent-page geometry comparison universe, not
    # just suggested continuations. Only a complete universe can support a
    # defensible false-negative/recall measurement against human labels.
    by_page: dict[int, list[int]] = defaultdict(list)
    for table in native.pdf_table_candidates:
        by_page[table.page_number].append(table.table_index)
    pair_count = sum(len(indices) * len(by_page.get(page + 1, ()))
                     for page, indices in by_page.items())
    pair_complete = pair_count <= max_crosspage_pairs
    pair_universe: list[dict[str, int]] = []
    if pair_complete:
        for page in sorted(by_page):
            for before in sorted(by_page[page]):
                for after in sorted(by_page.get(page + 1, ())):
                    pair_universe.append({
                        "previous_page": page, "previous_table_index": before,
                        "next_page": page + 1, "next_table_index": after,
                    })
    continuations = suggest_crosspage_tables(native)
    if len(continuations) > max_crosspage_candidates:
        # Comparisons may still be investigated, but must not be portrayed as
        # exhaustively included in the reviewer queue.
        crosspage_complete = False
    else:
        crosspage_complete = True
    crosspage_entries = [{
        "previous_page": c.previous_page,
        "previous_table_index": c.previous_table_index,
        "next_page": c.next_page,
        "next_table_index": c.next_table_index,
        "column_count": c.column_count,
        "status": c.status,
        "reasons": list(c.reasons),
        "header_verified": False, "continuation_verified": False,
    } for c in continuations[:max_crosspage_candidates]]
    sampled = min(len(contents), max_contents)
    return {
        "schema": SCHEMA,
        "status": "NATIVE_PDF_REVIEW_REQUIRED",
        "document_index": document_index,
        "document_hash": review["documents"][document_index-1]["document_hash"],
        "native_file_sha256": native.file_sha256,
        "native_pdf_page_count": page_count,
        "native_line_count": len(native.lines),
        "pipeline_content_count": len(contents),
        "contents_sampled": sampled,
        "contents_not_sampled": len(contents) - sampled,
        "alignment_status_counts": dict(sorted(counts.items())),
        "section_tree_status_counts": dict(sorted(structural_counts.items())),
        "entries": entries,
        "entries_omitted": sampled - len(entries),
        "native_pdf_has_no_text_layer": not bool(native.lines),
        "native_pdf_warnings": list(native.review_reasons),
        "pdf_table_geometry_candidates": len(native.pdf_table_candidates),
        "crosspage_continuity_candidates_total": len(continuations),
        "crosspage_candidate_queue_complete": crosspage_complete,
        "crosspage_review_queue": crosspage_entries,
        "crosspage_pair_universe": pair_universe,
        "crosspage_pair_universe_count": pair_count,
        "crosspage_pair_universe_complete": pair_complete,
        "pipeline_evidence_queue_complete": review["evidence_queue_truncated"] == 0,
        "pipeline_json_review_replayed": True,
        "native_extraction_provenance_authenticated": False,
        "physical_table_header_verified": False,
        "bilingual_equivalence_verified": False,
        "condition_action_scope_verified": False,
        "semantic_acceptance_complete": False,
        "database_modified": False,
        "qdrant_modified": False,
    }
