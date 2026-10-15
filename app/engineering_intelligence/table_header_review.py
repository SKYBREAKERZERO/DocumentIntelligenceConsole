"""Bounded, source-anchored physical-column evidence for *human* table review.

A reviewer chooses one specific native table and header row(s). This module
verifies exact native-to-SQL anchors for the selected columns, but intentionally
NEVER approves a logical header, propagates it over page boundaries, or updates
any Stage 1/2 relation. Complex merges remain explicit review blockers.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .native_layout import NativeLayoutReport
from .native_review_bundle import prepare_native_table_reviews
from .source_binding import SourceBinding

HEADER_REVIEW_VERSION = "stage1-header-evidence-v1"


@dataclass(frozen=True, slots=True)
class ColumnReview:
    column: int
    header_links: tuple[dict[str, Any], ...]
    data_links: tuple[dict[str, Any], ...]
    problems: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HeaderReviewPreview:
    version: str
    source_sha256: str
    native_table: str
    header_rows: tuple[int, ...]
    columns: tuple[ColumnReview, ...]
    status: str  # REVIEW_ONLY; never a confirmation
    review_blockers: tuple[str, ...]
    exact_header_cells: int
    exact_data_cells: int


def preview_table_header_columns(
    report: NativeLayoutReport,
    bindings: tuple[SourceBinding, ...],
    *, table_identity: str, header_rows: tuple[int, ...],
    max_columns: int = 512, max_header_rows: int = 24,
    max_links: int = 20_000, max_column_memberships: int = 100_000,
) -> HeaderReviewPreview:
    """Render a *review-only* header-to-column evidence matrix with no text.

    No guesses: only a reviewer-selected physical row; only a unique, verified
    SQL span; header links must lie on a physical header row. Multi-row headers
    are supported as evidence, but merge and cross-page semantics are blocked.
    """
    if not table_identity or not isinstance(header_rows, tuple) or not header_rows:
        raise ValueError("Explicit table identity and header rows are required")
    if (len(header_rows) > max_header_rows or max_header_rows < 1 or max_columns < 1
            or max_links < 1 or max_column_memberships < 1 or any(type(i) is not int or i < 0 for i in header_rows)
            or len(set(header_rows)) != len(header_rows) or tuple(sorted(header_rows)) != header_rows):
        raise ValueError("Header rows must be unique, bounded, ascending nonnegative integers")
    matched = [x for x in prepare_native_table_reviews(report, bindings,
                max_cells=max_links) if x.identity == table_identity]
    if len(matched) != 1:
        raise ValueError("Selected native table does not exist uniquely")
    selected = matched[0]
    if selected.kind != report.format:
        raise ValueError("Native table format mismatch")
    if selected.kind == "DOCX":
        table = next(t for t in report.tables if f"DOCX:{t.table_index}" == table_identity)
        count, rows = table.grid_columns, {c.row for c in table.cells}
        physical = {(c.row, c.column): c for c in table.cells}
    else:
        table = next(t for t in report.pdf_table_candidates
                     if f"PDF:{t.page_number}:{t.table_index}" == table_identity)
        count, rows = table.column_count, {c.row for c in table.cells}
        physical = {(c.row, c.column): c for c in table.cells}
    if count < 1 or count > max_columns or not set(header_rows).issubset(rows):
        raise ValueError("Selected header row or table width is outside physical source")
    if max(header_rows) >= max(rows):
        raise ValueError("Header rows must precede at least one physical data row")
    if len(selected.exact_cell_links) > max_links:
        raise ValueError("Header review link budget exceeded")

    header_by_col: dict[int, list[dict[str, Any]]] = {i: [] for i in range(count)}
    body_by_col: dict[int, list[dict[str, Any]]] = {i: [] for i in range(count)}
    blockers: set[str] = set(selected.structural_flags)
    if selected.unbound_cells:
        blockers.add("NATIVE_UNBOUND_CELLS")
    if selected.kind == "PDF":
        blockers.add("PDF_HEADER_AND_CROSSPAGE_SEMANTICS_UNVERIFIED")
    headers = set(header_rows)
    if min(rows) < min(header_rows):
        blockers.add("PREHEADER_ROWS_NOT_SELECTED")
    if any(row not in headers for row in range(min(header_rows), max(header_rows) + 1)):
        blockers.add("NONCONTIGUOUS_HEADER_ROWS_REQUIRES_REVIEW")
    last_header = max(header_rows)
    memberships = 0
    seen: set[tuple[int, int, int, int]] = set()
    physical_header_links: set[str] = set()
    physical_data_links: set[str] = set()
    for link in selected.exact_cell_links:
        row, col = link["row"], link["column"]
        span = link.get("column_span", 1)
        if (type(row) is not int or type(col) is not int or type(span) is not int
                or not (row in rows and 0 <= col < count and 1 <= span <= count - col)
                or (row, col) not in physical):
            raise ValueError("Physical cell link is outside native grid")
        signature = (row, col, link["content_id"], link["start_offset"])
        if signature in seen:
            raise ValueError("Repeated native-to-SQL header evidence")
        seen.add(signature)
        # Keep physical locator / SHA only, never the engineering source text.
        anchor = {k: link[k] for k in (
            "row", "column", "content_id", "start_offset", "end_offset",
            "source_text_sha256", "anchor_key")}
        anchor["column_span"] = span
        if row in headers:
            physical_header_links.add(link["anchor_key"])
        elif row > last_header:
            physical_data_links.add(link["anchor_key"])
        memberships += span
        if memberships > max_column_memberships:
            raise ValueError("Native table column membership budget exceeded")
        for c in range(col, col + span):
            if row in headers:
                header_by_col[c].append(anchor)
            elif row > last_header:
                body_by_col[c].append(anchor)
            else:
                blockers.add("UNSELECTED_PREHEADER_ROW_REQUIRES_REVIEW")
        if span > 1:
            blockers.add("MERGED_HEADER_OR_DATA_CELL_REQUIRES_HUMAN_REVIEW")
    columns: list[ColumnReview] = []
    for col in range(count):
        heads = sorted(header_by_col[col], key=lambda x: (x["row"], x["column"]))
        body = sorted(body_by_col[col], key=lambda x: (x["row"], x["column"]))
        problems = []
        if not heads:
            problems.append("NO_EXACT_HEADER_ANCHOR")
        if not body:
            problems.append("NO_EXACT_DATA_ANCHOR")
        # Each physical cell may appear in multiple columns due to merges;
        # this is evidence of geometry, NOT proof of inherited field meaning.
        if any(x["column_span"] > 1 for x in heads):
            problems.append("SHARED_MERGED_HEADER_ANCHOR")
        if any(x["column_span"] > 1 for x in body):
            problems.append("SHARED_MERGED_DATA_ANCHOR")
        blockers.update(problems)
        columns.append(ColumnReview(col, tuple(heads), tuple(body), tuple(problems)))
    return HeaderReviewPreview(
        HEADER_REVIEW_VERSION, report.file_sha256, table_identity, header_rows,
        tuple(columns), "REVIEW_ONLY", tuple(sorted(blockers)),
        len(physical_header_links), len(physical_data_links),
    )


def redacted_header_review(preview: HeaderReviewPreview) -> dict[str, Any]:
    """Safe for developer console: bounded metadata and hashes, no source text."""
    return asdict(preview)


def require_proposal_matches_header_table(comparison: Any, table_identity: str) -> None:
    """Prohibit attaching an unrelated physical table to a SQL review proposal.

    This is evidence *eligibility*, never formal approval. Call after the
    source SHA, SQL snapshots and proposal digest have all been verified.
    """
    if getattr(comparison, "status", None) != "EXACT_NATIVE_CELLS_CANDIDATE":
        raise ValueError("SQL review proposal is not completely source bound")
    matches = getattr(comparison, "matches", None)
    if not isinstance(matches, tuple) or not matches:
        raise ValueError("No matched physical source evidence")
    matched_tables = {m.get("native_table") for m in matches
                      if m.get("status") == "EXACT_NATIVE_SUBSPAN_CANDIDATE"}
    if matched_tables != {table_identity}:
        raise ValueError("Chosen physical table does not uniquely match this review proposal")
