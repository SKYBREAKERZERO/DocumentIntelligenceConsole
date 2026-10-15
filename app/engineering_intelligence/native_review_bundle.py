"""Conservative physical-table / SQL requirement evidence joins (read-only).

Does NOT approve semantic headers, merged cells or cross-page continuity.
Every physical cell anchor is taken from an exact matched native source file.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any

from .crosspage import suggest_crosspage_tables
from .native_layout import NativeLayoutReport
from .source_binding import SourceBinding, native_anchors


@dataclass(frozen=True, slots=True)
class NativeTableReview:
    identity: str
    kind: str
    page_number: int | None
    structural_flags: tuple[str, ...]
    exact_cell_links: tuple[dict[str, Any], ...]
    unbound_cells: int
    source_coverage: str  # REVIEW_ONLY, never engineering fact


def prepare_native_table_reviews(
    report: NativeLayoutReport, bindings: tuple[SourceBinding, ...],
    *, max_tables: int = 10_000, max_cells: int = 200_000,
    max_geometry_checks: int = 5_000_000,
) -> tuple[NativeTableReview, ...]:
    """Join verified physical cell anchors with SQL spans, without guessing cells.

    PDF geometry is not text identity: lines must be assigned to exactly one
    physical geometry cell using fully contained, nonoverlapping bounding boxes.
    """
    if max_tables < 1 or max_cells < 1 or max_geometry_checks < 1:
        raise ValueError("Invalid native review budget")
    anchors = native_anchors(report)
    if len(anchors) != len(bindings):
        raise ValueError("Native binding completeness mismatch")
    mapping = {b.anchor_key: b for b in bindings}
    anchor_lookup = {a.anchor_key: a for a in anchors}
    if len(mapping) != len(bindings) or set(mapping) != set(anchor_lookup):
        raise ValueError("Foreign or repeated native binding identity")
    if any((b.kind, b.position, b.text_hash) !=
           (anchor_lookup[b.anchor_key].kind, anchor_lookup[b.anchor_key].position,
            anchor_lookup[b.anchor_key].text_hash) for b in bindings):
        raise ValueError("Native binding identity or hash drift")
    cell_anchor = {a.position: a for a in anchors if a.kind == "DOCX_CELL"}
    page_anchors: dict[int, list[Any]] = {}
    for anchor in anchors:
        if anchor.kind == "PDF_LINE":
            page_anchors.setdefault(anchor.position[0], []).append(anchor)
    exact = {"UNIQUE_REQUIREMENT_SPAN_CANDIDATE", "UNIQUE_CONTENT_SPAN_CANDIDATE"}
    results: list[NativeTableReview] = []
    cells_budget = 0
    geometry_checks = 0
    if report.format == "DOCX":
        for table in report.tables:
            links: list[dict[str, Any]] = []
            unavailable = 0
            for cell in table.cells:
                cells_budget += 1
                if cells_budget > max_cells:
                    raise ValueError("Native cell budget exceeded")
                if cell.is_merge_continuation or cell.nested_table:
                    unavailable += 1
                    continue
                a = cell_anchor.get((table.table_index, cell.row, cell.column))
                if a is None:
                    raise ValueError("Native table physical cell anchor is missing")
                b = mapping[a.anchor_key]
                if b.status in exact and b.content_id is not None:
                    links.append({"row": cell.row, "column": cell.column,
                                  "row_span": cell.row_span, "column_span": cell.column_span,
                                  "content_id": b.content_id,
                                  "start_offset": b.start_offset,
                                  "end_offset": b.end_offset,
                                  "requirement_unit_key": b.requirement_unit_key,
                                  "anchor_key": b.anchor_key,
                                  "source_text_sha256": b.text_hash})
                else:
                    unavailable += 1
            flags = set(table.review_reasons)
            if unavailable:
                flags.add("UNBOUND_OR_UNSAFE_NATIVE_CELLS")
            if any(c.column_span > 1 or c.row_span > 1 for c in table.cells):
                flags.add("MERGED_CELL_SEMANTICS_UNVERIFIED")
            results.append(NativeTableReview(
                f"DOCX:{table.table_index}", "DOCX", None, tuple(sorted(flags)),
                tuple(links), unavailable, "REVIEW_ONLY"))
    elif report.format == "PDF":
        # Geometry alone cannot map SQL chunks: only unambiguous contained
        # text lines count as physical-to-SQL evidence; unknown cells stay unknown.
        line_lookup = {(x.page_number, x.block_index, x.line_index): x for x in report.lines}
        pages = Counter(t.page_number for t in report.pdf_table_candidates)
        for table in report.pdf_table_candidates:
            links = []
            unavailable = 0
            for cell in table.cells:
                cells_budget += 1
                if cells_budget > max_cells:
                    raise ValueError("Native cell budget exceeded")
                if cell.bbox is None:
                    unavailable += 1
                    continue
                possible = []
                page_lines = page_anchors.get(table.page_number, [])
                geometry_checks += len(page_lines)
                if geometry_checks > max_geometry_checks:
                    raise ValueError("PDF geometry comparison budget exceeded")
                for a in page_lines:
                    ln = line_lookup.get(a.position)
                    if ln is None:
                        raise ValueError("Native PDF line identity drift")
                    if (ln.bbox[0] >= cell.bbox[0] and ln.bbox[1] >= cell.bbox[1] and
                        ln.bbox[2] <= cell.bbox[2] and ln.bbox[3] <= cell.bbox[3]):
                        possible.append(a)
                # A cell may wrap over several lines; no composite span
                # is asserted from multiple PDF line anchors.
                if len(possible) != 1:
                    unavailable += 1
                    continue
                b = mapping[possible[0].anchor_key]
                if b.status not in exact or b.content_id is None:
                    unavailable += 1
                    continue
                # Shared text lines overlapping other PDF cells are ambiguous.
                geometry_checks += len(table.cells)
                if geometry_checks > max_geometry_checks:
                    raise ValueError("PDF geometry comparison budget exceeded")
                other_hits = sum(1 for other in table.cells if other is not cell and other.bbox is not None
                                 and line_lookup[possible[0].position].bbox[0] >= other.bbox[0]
                                 and line_lookup[possible[0].position].bbox[1] >= other.bbox[1]
                                 and line_lookup[possible[0].position].bbox[2] <= other.bbox[2]
                                 and line_lookup[possible[0].position].bbox[3] <= other.bbox[3])
                if other_hits:
                    unavailable += 1
                    continue
                links.append({"row": cell.row, "column": cell.column,
                              "content_id": b.content_id,
                              "start_offset": b.start_offset, "end_offset": b.end_offset,
                              "requirement_unit_key": b.requirement_unit_key,
                              "anchor_key": b.anchor_key,
                              "source_text_sha256": b.text_hash})
            flags = set(table.review_reasons)
            flags.update(("PDF_CELL_GEOMETRY_UNVERIFIED", "TABLE_HEADER_UNVERIFIED"))
            if unavailable:
                flags.add("UNBOUND_OR_AMBIGUOUS_PDF_CELLS")
            if pages[table.page_number] > 1:
                flags.add("MULTIPLE_TABLES_SAME_PAGE")
            results.append(NativeTableReview(
                f"PDF:{table.page_number}:{table.table_index}", "PDF", table.page_number,
                tuple(sorted(flags)), tuple(links), unavailable, "REVIEW_ONLY"))
    else:
        raise ValueError("Unsupported native source type")
    if len(results) > max_tables:
        raise ValueError("Native table review budget exceeded")
    return tuple(results)


def native_bundle_summary(report: NativeLayoutReport,
                          bindings: tuple[SourceBinding, ...]) -> dict[str, Any]:
    tables = prepare_native_table_reviews(report, bindings)
    continuations = suggest_crosspage_tables(report) if report.format == "PDF" else ()
    return {
        "source_sha256": report.file_sha256,
        "native_format": report.format,
        "table_count": len(tables),
        "review_only": True,
        "tables": [asdict(t) for t in tables],
        "possible_crosspage": [asdict(c) for c in continuations],
        "crosspage_unverified": True,
    }
