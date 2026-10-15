"""Conservative PDF cross-page table-continuity hints, NEVER merged tables.

Text/column geometry cannot prove a multi-page table shares its headers,
conditions or semantic identity. All outputs are review-only, with evidence.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace

from .native_layout import NativeLayoutReport, PdfTableCandidate

CONTINUITY_VERSION = "cross-page-geometry-candidate-v1"


@dataclass(frozen=True, slots=True)
class CrossPageTableCandidate:
    previous_page: int
    previous_table_index: int
    next_page: int
    next_table_index: int
    status: str
    reasons: tuple[str, ...]
    prior_bbox: tuple[float, float, float, float]
    next_bbox: tuple[float, float, float, float]
    column_count: int


def suggest_crosspage_tables(
    report: NativeLayoutReport, *, edge_band_points: float = 90.0,
    tolerance_points: float = 8.0,
    max_pairs: int = 20_000,
) -> tuple[CrossPageTableCandidate, ...]:
    if report.format != "PDF":
        return ()
    if edge_band_points <= 0 or tolerance_points < 0 or max_pairs < 1:
        raise ValueError("Invalid cross-page geometry budget")
    table_by_page: dict[int, list[PdfTableCandidate]] = {}
    for table in report.pdf_table_candidates:
        table_by_page.setdefault(table.page_number, []).append(table)
    results: list[CrossPageTableCandidate] = []
    for page_number, preceding in table_by_page.items():
        following = table_by_page.get(page_number + 1, [])
        if not following:
            continue
        if len(preceding) * len(following) > max_pairs - len(results):
            raise ValueError("Cross-page candidate comparison budget exceeded")
        for before in preceding:
            for after in following:
                if before.page_height is None or after.page_height is None:
                    continue
                if before.column_count != after.column_count:
                    continue
                # Tables must be close to the adjoining physical page edges.
                if before.page_height - before.bbox[3] > edge_band_points:
                    continue
                if after.bbox[1] > edge_band_points:
                    continue
                if (abs(before.bbox[0] - after.bbox[0]) > tolerance_points or
                        abs(before.bbox[2] - after.bbox[2]) > tolerance_points):
                    continue
                # Both tables need the same literal x-positions at each column
                # boundary, not just the same total width and column count.
                def x_boundaries(table: PdfTableCandidate) -> tuple[float, ...] | None:
                    first_row = sorted((c for c in table.cells if c.row == 0), key=lambda c: c.column)
                    if len(first_row) != table.column_count or any(c.bbox is None for c in first_row):
                        return None
                    return tuple([first_row[0].bbox[0]] + [c.bbox[2] for c in first_row])
                a, b = x_boundaries(before), x_boundaries(after)
                if a is None or b is None or len(a) != len(b):
                    continue
                if any(abs(x - y) > tolerance_points for x, y in zip(a, b)):
                    continue
                results.append(CrossPageTableCandidate(
                    page_number, before.table_index, page_number + 1, after.table_index,
                    "POSSIBLE_CONTINUATION_REVIEW_ONLY",
                    ("PAGE_EDGE_PROXIMITY", "CONSISTENT_COLUMN_GEOMETRY",
                     "HEADER_AND_SEMANTIC_CONTINUITY_UNVERIFIED"),
                    before.bbox, after.bbox, before.column_count,
                ))
    # A geometrically compatible page pair can still contain two different
    # tables. Record one-to-many/many-to-one ambiguity rather than promoting
    # all of those matches to a single inferred table relationship.
    outgoing = Counter((r.previous_page, r.previous_table_index) for r in results)
    incoming = Counter((r.next_page, r.next_table_index) for r in results)
    reviewed = []
    for candidate in results:
        left = (candidate.previous_page, candidate.previous_table_index)
        right = (candidate.next_page, candidate.next_table_index)
        if outgoing[left] > 1 or incoming[right] > 1:
            candidate = replace(
                candidate,
                reasons=(*candidate.reasons, "MULTIPLE_COMPATIBLE_TABLES_REVIEW"),
            )
        reviewed.append(candidate)
    return tuple(reviewed)
