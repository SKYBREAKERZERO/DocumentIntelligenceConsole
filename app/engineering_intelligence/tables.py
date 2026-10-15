"""Conservative table row/header evidence from already preserved source units.

A markdown rule provides an explicit header boundary. Arbitrary first rows,
flattened merged cells or page-crossing table continuations are *never*
automatically treated as verified column semantics.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from .requirements import RequirementUnit

TABLE_VERSION = "table-evidence-v1"
_MD_RULE_CELL = re.compile(r"^:?-{2,}:?$", re.ASCII)


@dataclass(frozen=True, slots=True)
class TableCellEvidence:
    column_index: int
    start_in_content: int
    end_in_content: int
    source_text: str
    header_text: str | None
    header_unit_key: str | None


@dataclass(frozen=True, slots=True)
class TableRowEvidence:
    unit_key: str
    section_id: str
    page_number: int | None
    row_kind: str  # HEADER | DATA | SEPARATOR | UNKNOWN
    cells: tuple[TableCellEvidence, ...]


@dataclass(frozen=True, slots=True)
class TableBlockEvidence:
    table_key: str
    document_id: str
    content_id: int
    header_status: str   # EXPLICIT_MARKDOWN | UNRESOLVED
    review_reasons: tuple[str, ...]
    rows: tuple[TableRowEvidence, ...]


def _separator(unit: RequirementUnit) -> bool:
    cells = unit.signals.table_cells
    return bool(cells and all(_MD_RULE_CELL.fullmatch(value.strip()) for _, _, value in cells))


def _table_rows(units: tuple[RequirementUnit, ...]):
    """Contiguous groups; never merge across content/section or intervening prose."""
    group: list[RequirementUnit] = []
    previous: RequirementUnit | None = None
    for unit in units:
        is_table = unit.signals.structural_kind in ("TABLE_ROW", "TABLE_SEPARATOR")
        contiguous = (previous is not None and previous.document_id == unit.document_id and
                      previous.content_id == unit.content_id and
                      previous.section_id == unit.section_id and
                      unit.start_offset >= previous.end_offset and
                      unit.start_offset - previous.end_offset <= 4)
        if group and (not is_table or not contiguous):
            yield tuple(group)
            group = []
        if is_table:
            group.append(unit)
        previous = unit
    if group:
        yield tuple(group)


def extract_table_blocks(units: tuple[RequirementUnit, ...], *, max_rows: int = 1000,
                         max_columns: int = 128) -> tuple[TableBlockEvidence, ...]:
    if max_rows < 1 or max_columns < 1:
        raise ValueError("Invalid table extraction resource limits")
    blocks: list[TableBlockEvidence] = []
    for group in _table_rows(units):
        if len(group) > max_rows:
            raise ValueError("Table row budget exceeded; refusing truncated output")
        header_status = "UNRESOLVED"
        header: RequirementUnit | None = None
        rule_indices = [i for i, u in enumerate(group) if _separator(u)]
        reasons: list[str] = []
        if len(rule_indices) == 1 and rule_indices[0] == 1:
            header = group[0]
            header_status = "EXPLICIT_MARKDOWN"
        else:
            reasons.append("HEADER_NOT_EXPLICIT")
        if len(rule_indices) > 1 or (rule_indices and rule_indices[0] != 1):
            reasons.append("AMBIGUOUS_TABLE_SEPARATOR")
        if any('\\|' in u.text for u in group):
            reasons.append("ESCAPED_PIPE_OR_LAYOUT_UNSUPPORTED")
            header = None
            header_status = "UNRESOLVED"
        cols = [len(u.signals.table_cells) for u in group]
        if any(n > max_columns for n in cols):
            raise ValueError("Table column budget exceeded")
        if not cols or min(cols) == 0 or len(set(cols)) != 1:
            reasons.append("COLUMN_COUNT_MISMATCH")
            header = None
            header_status = "UNRESOLVED"
        if header is not None:
            labels = [cell[2] for cell in header.signals.table_cells]
            if any(not label.strip() for label in labels) or len(set(labels)) != len(labels):
                reasons.append("HEADER_LABELS_NOT_UNIQUE")
                header = None
                header_status = "UNRESOLVED"
        # Source is flattened text, not a reliable DOCX/PDF cell merge tree.
        reasons.append("MERGED_CELL_GEOMETRY_UNAVAILABLE")
        rows: list[TableRowEvidence] = []
        for idx, unit in enumerate(group):
            if header is not None and idx == 0:
                kind = "HEADER"
            elif _separator(unit):
                kind = "SEPARATOR"
            elif header is None:
                kind = "UNKNOWN"
            else:
                kind = "DATA"
            cells: list[TableCellEvidence] = []
            for col, (start, end, value) in enumerate(unit.signals.table_cells):
                if unit.text[start:end] != value:
                    raise ValueError("Table cell source span mismatch")
                header_text = header_key = None
                if header is not None and kind == "DATA":
                    header_text = header.signals.table_cells[col][2]
                    header_key = header.unit_key
                cells.append(TableCellEvidence(
                    column_index=col, start_in_content=unit.start_offset + start,
                    end_in_content=unit.start_offset + end, source_text=value,
                    header_text=header_text, header_unit_key=header_key,
                ))
            rows.append(TableRowEvidence(
                unit_key=unit.unit_key, section_id=unit.section_id,
                page_number=unit.page_number, row_kind=kind, cells=tuple(cells),
            ))
        digest = hashlib.sha256((TABLE_VERSION + '|'+ '|'.join(u.unit_key for u in group)).encode('utf-8')).hexdigest()
        blocks.append(TableBlockEvidence(
            table_key=digest, document_id=group[0].document_id,
            content_id=group[0].content_id, header_status=header_status,
            review_reasons=tuple(reasons), rows=tuple(rows),
        ))
    return tuple(blocks)


def verify_table_blocks(units: tuple[RequirementUnit, ...], blocks: tuple[TableBlockEvidence, ...]) -> None:
    source = {u.unit_key: u for u in units}
    for block in blocks:
        for row in block.rows:
            if row.unit_key not in source:
                raise ValueError("Table row has no source unit")
            unit = source[row.unit_key]
            if block.document_id != unit.document_id or block.content_id != unit.content_id:
                raise ValueError("Table source identity mismatch")
            if len(row.cells) != len(unit.signals.table_cells):
                raise ValueError("Table cell count mismatch")
            for cell in row.cells:
                if cell.column_index >= len(unit.signals.table_cells):
                    raise ValueError("Table column index invalid")
                left, right, value = unit.signals.table_cells[cell.column_index]
                if (cell.start_in_content != unit.start_offset + left or
                    cell.end_in_content != unit.start_offset + right or
                    cell.source_text != value):
                    raise ValueError("Table evidence coordinate mismatch")
                if cell.header_unit_key is not None:
                    header_unit = source.get(cell.header_unit_key)
                    if (header_unit is None or cell.column_index >= len(header_unit.signals.table_cells)
                        or header_unit.signals.table_cells[cell.column_index][2] != cell.header_text):
                        raise ValueError("Table header evidence mismatch")
