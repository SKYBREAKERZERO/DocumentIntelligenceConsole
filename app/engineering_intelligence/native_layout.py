"""Local, read-only physical layout evidence for Stage 1.

DOCX: physical OOXML grid cells, horizontal/vertical merges, nested-table flags.
PDF: PyMuPDF native text line boxes; this is *not* inferred table geometry.

All evidence is derived from the supplied source file. A physical position is
not asserted to match an existing SQL `contents` row without an exact, unique
source match. No OCR, network/model calls, uploads, or database writes.
"""
from __future__ import annotations

import hashlib
import math
from zipfile import ZipFile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

NATIVE_LAYOUT_VERSION = "stage1-native-layout-v2"


@dataclass(frozen=True, slots=True)
class NativeCell:
    row: int
    column: int
    row_span: int
    column_span: int
    text: str
    text_hash: str
    is_merge_continuation: bool
    merge_origin: tuple[int, int] | None
    nested_table: bool
    source_kind: str = "DOCX_OOXML"


@dataclass(frozen=True, slots=True)
class NativeTable:
    table_index: int
    body_order: int
    grid_columns: int
    cells: tuple[NativeCell, ...]
    review_reasons: tuple[str, ...]
    page_number: None = None  # DOCX physical page is unknown


@dataclass(frozen=True, slots=True)
class DocxParagraph:
    body_order: int
    text: str
    text_hash: str
    source_kind: str = "DOCX_BODY_PARAGRAPH"


@dataclass(frozen=True, slots=True)
class PdfTextLine:
    page_number: int
    block_index: int
    line_index: int
    bbox: tuple[float, float, float, float]
    text: str
    text_hash: str
    source_kind: str = "PDF_NATIVE_TEXT_LINE"


@dataclass(frozen=True, slots=True)
class PdfGeometryCell:
    row: int
    column: int
    bbox: tuple[float, float, float, float] | None
    # Geometry cannot prove merges or semantic header authority.
    status: str = "GEOMETRY_ONLY_UNVERIFIED"


@dataclass(frozen=True, slots=True)
class PdfTableCandidate:
    page_number: int
    table_index: int
    bbox: tuple[float, float, float, float]
    row_count: int
    column_count: int
    cells: tuple[PdfGeometryCell, ...]
    review_reasons: tuple[str, ...] = ("PDF_TABLE_SEMANTICS_NOT_VERIFIED",)
    page_width: float | None = None
    page_height: float | None = None


@dataclass(frozen=True, slots=True)
class NativeLayoutReport:
    version: str
    format: str
    file_sha256: str
    source_size: int
    tables: tuple[NativeTable, ...] = ()
    lines: tuple[PdfTextLine, ...] = ()
    pdf_table_candidates: tuple[PdfTableCandidate, ...] = ()
    review_reasons: tuple[str, ...] = ()
    paragraphs: tuple[DocxParagraph, ...] = ()
    pdf_page_count: int | None = None

    def as_dict(self, *, include_text: bool = False) -> dict[str, Any]:
        """Privacy-first serialization; source texts excluded unless requested."""
        data = asdict(self)
        if not include_text:
            for table in data["tables"]:
                for cell in table["cells"]:
                    cell.pop("text")
            for line in data["lines"]:
                line.pop("text")
            for paragraph in data["paragraphs"]:
                paragraph.pop("text")
        return data


def _source(path: Path, *, max_file_bytes: int) -> tuple[str, int]:
    if max_file_bytes < 1 or not path.is_file():
        raise ValueError("Missing source file or invalid file-size limit")
    size = path.stat().st_size
    if size > max_file_bytes:
        raise ValueError("Native source exceeds size budget")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest(), size


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()


def inspect_docx_native(
    path: str | Path, *, max_file_bytes: int = 100_000_000,
    max_tables: int = 5_000, max_cells: int = 200_000,
    max_total_chars: int = 20_000_000, max_paragraphs: int = 200_000,
) -> NativeLayoutReport:
    from docx import Document
    from docx.oxml.table import CT_Tbl
    from docx.oxml.text.paragraph import CT_P
    from docx.text.paragraph import Paragraph
    from docx.table import Table, _Cell

    p = Path(path)
    source_hash, size = _source(p, max_file_bytes=max_file_bytes)
    if (p.suffix.lower() != ".docx" or max_tables < 1 or max_cells < 1 or
            max_total_chars < 1 or max_paragraphs < 1):
        raise ValueError("Expected DOCX and positive layout budgets")
    # Defend against compressed ZIP documents expanding far beyond source size.
    with ZipFile(p) as archive:
        members = archive.infolist()
        if len(members) > 20_000 or sum(i.file_size for i in members) > max_file_bytes * 8:
            raise ValueError("DOCX uncompressed XML exceeds safety budget")
    doc = Document(str(p))
    tables: list[NativeTable] = []
    paragraphs: list[DocxParagraph] = []
    total_cells = 0
    total_chars = 0
    # Count paragraphs as well so the index can be related to the physical
    # ordering of document.xml; nested tables are flagged, not flattened.
    for body_order, child in enumerate(doc.element.body.iterchildren()):
        if isinstance(child, CT_P):
            value = Paragraph(child, doc._body).text
            if value.strip():
                if len(paragraphs) >= max_paragraphs:
                    raise ValueError("Native DOCX paragraph budget exceeded")
                total_chars += len(value)
                if total_chars > max_total_chars:
                    raise ValueError("Native DOCX text budget exceeded")
                paragraphs.append(DocxParagraph(body_order, value, _hash(value)))
            continue
        if not isinstance(child, CT_Tbl):
            continue
        if len(tables) >= max_tables:
            raise ValueError("Native DOCX table budget exceeded")
        table = Table(child, doc._body)
        grid_columns = len(child.tblGrid.gridCol_lst)
        if not grid_columns:
            raise ValueError("DOCX table grid is missing")
        reasons: set[str] = set()
        physical: list[NativeCell] = []
        # A vertical merge has a physical continuation cell but may not repeat
        # text; resolve its origin explicitly, instead of treating repeated
        # python-docx .rows[n].cells as independent text evidence.
        active: dict[int, tuple[int, int]] = {}
        for row_i, row in enumerate(table.rows):
            try:
                col = int(row.grid_cols_before)
            except AttributeError:
                col = 0
            next_active: dict[int, tuple[int, int]] = {}
            if col:
                reasons.add("OMITTED_GRID_COLUMNS")
            for tc in row._tr.tc_lst:
                total_cells += 1
                if total_cells > max_cells:
                    raise ValueError("Native DOCX cell budget exceeded")
                grid_span = tc.grid_span or 1
                if grid_span < 1 or col + grid_span > grid_columns:
                    raise ValueError("DOCX cell exceeds table grid")
                vm = tc.vMerge
                continuation = vm == "continue"
                origin = active.get(col) if continuation else None
                if continuation:
                    if origin is None or any(active.get(c) != origin for c in range(col, col + grid_span)):
                        reasons.add("UNRESOLVED_VERTICAL_MERGE")
                    else:
                        for c in range(col, col + grid_span):
                            next_active[c] = origin
                elif vm == "restart":
                    origin = (row_i, col)
                    for c in range(col, col + grid_span):
                        next_active[c] = origin
                # Only physical tc text is read; no duplicate merged-cell
                # materialization. `_Cell.text` can include nested table text;
                # mark that as requiring independent review.
                cell_text = _Cell(tc, table).text
                total_chars += len(cell_text)
                if total_chars > max_total_chars:
                    raise ValueError("Native DOCX text budget exceeded")
                nested = bool(tc.xpath('.//w:tbl'))
                if nested:
                    reasons.add("NESTED_TABLE_REQUIRES_REVIEW")
                physical.append(NativeCell(
                    row=row_i, column=col, row_span=1, column_span=grid_span,
                    text=cell_text, text_hash=_hash(cell_text),
                    is_merge_continuation=continuation, merge_origin=origin,
                    nested_table=nested,
                ))
                if grid_span > 1:
                    reasons.add("HORIZONTAL_MERGE_PRESENT")
                col += grid_span
            try:
                trailing = row.grid_cols_after
            except AttributeError:
                trailing = 0
            if col + trailing != grid_columns:
                reasons.add("ROW_GRID_WIDTH_MISMATCH")
            if trailing:
                reasons.add("OMITTED_GRID_COLUMNS")
            active = next_active
        # Calculate known row spans from unambiguous merge continuation only.
        widths: dict[tuple[int, int], int] = {}
        for cell in physical:
            if cell.is_merge_continuation and cell.merge_origin is not None:
                widths[cell.merge_origin] = max(widths.get(cell.merge_origin, 1), cell.row - cell.merge_origin[0] + 1)
        physical = [
            NativeCell(**{**asdict(cell), "row_span": widths.get((cell.row, cell.column), 1)})
            if not cell.is_merge_continuation else cell for cell in physical
        ]
        reasons.add("TABLE_HEADER_NOT_AUTOMATICALLY_VERIFIED")
        tables.append(NativeTable(
            table_index=len(tables), body_order=body_order,
            grid_columns=grid_columns, cells=tuple(physical),
            review_reasons=tuple(sorted(reasons)),
        ))
    return NativeLayoutReport(
        NATIVE_LAYOUT_VERSION, "DOCX", source_hash, size, tables=tuple(tables),
        paragraphs=tuple(paragraphs),
        review_reasons=("DOCX_PHYSICAL_PAGE_UNAVAILABLE",),
    )


def inspect_pdf_native(
    path: str | Path, *, max_file_bytes: int = 100_000_000,
    max_pages: int = 3_000, max_lines: int = 500_000,
    max_total_chars: int = 20_000_000,
    include_table_geometry: bool = False,
    max_table_candidates: int = 5_000, max_table_cells: int = 200_000,
) -> NativeLayoutReport:
    import fitz

    p = Path(path)
    source_hash, size = _source(p, max_file_bytes=max_file_bytes)
    if (p.suffix.lower() != ".pdf" or max_pages < 1 or max_lines < 1
            or max_total_chars < 1 or max_table_candidates < 1 or max_table_cells < 1):
        raise ValueError("Expected PDF and positive layout budgets")
    lines: list[PdfTextLine] = []
    geometry_tables: list[PdfTableCandidate] = []
    total_geometry_cells = 0
    warnings: set[str] = set()
    total_chars = 0
    with fitz.open(str(p)) as doc:
        if doc.needs_pass:
            raise ValueError("Encrypted PDF is not supported")
        page_count = len(doc)
        if page_count > max_pages:
            raise ValueError("Native PDF page budget exceeded")
        for page_no, page in enumerate(doc, 1):
            page_data = page.get_text("dict", sort=False)
            for block_id, block in enumerate(page_data.get("blocks", [])):
                if block.get("type") != 0:
                    if block.get("type") == 1:
                        warnings.add("PDF_IMAGE_REQUIRES_NON_OCR_REVIEW")
                    continue
                for line_id, line in enumerate(block.get("lines", [])):
                    value = "".join(span.get("text", "") for span in line.get("spans", []))
                    if not value.strip():
                        continue
                    total_chars += len(value)
                    if total_chars > max_total_chars:
                        raise ValueError("Native PDF text budget exceeded")
                    if len(lines) >= max_lines:
                        raise ValueError("Native PDF line budget exceeded")
                    box = tuple(float(n) for n in line["bbox"])
                    if len(box) != 4 or not all(math.isfinite(c) for c in box) or box[0] > box[2] or box[1] > box[3]:
                        raise ValueError("PDF source bounding box invalid")
                    lines.append(PdfTextLine(page_no, block_id, line_id, box, value, _hash(value)))
            if include_table_geometry:
                try:
                    found = page.find_tables()
                except (AttributeError, RuntimeError, ValueError) as exc:
                    raise ValueError(f"PDF table geometry unavailable on page {page_no}: {exc}") from exc
                for table in found.tables:
                    if len(geometry_tables) >= max_table_candidates:
                        raise ValueError("PDF table candidate budget exceeded")
                    rows, cols = int(table.row_count), int(table.col_count)
                    if rows < 1 or cols < 1 or rows * cols > max_table_cells - total_geometry_cells:
                        raise ValueError("PDF table cell budget exceeded")
                    total_geometry_cells += rows * cols
                    cells: list[PdfGeometryCell] = []
                    # Table.cells can be column-major; assigning coordinates by
                    # index would fabricate a row/column identity. Use rows.
                    for row_idx, row in enumerate(table.rows):
                        if len(row.cells) != cols:
                            raise ValueError("PDF geometry column count inconsistent")
                        for col_idx, rect in enumerate(row.cells):
                            bbox = None if rect is None else tuple(float(n) for n in rect)
                            if bbox is not None and (len(bbox) != 4 or
                                    not all(math.isfinite(n) for n in bbox) or
                                    bbox[0] > bbox[2] or bbox[1] > bbox[3]):
                                raise ValueError("PDF table cell rectangle invalid")
                            cells.append(PdfGeometryCell(row_idx, col_idx, bbox))
                    box = tuple(float(n) for n in table.bbox)
                    if not all(math.isfinite(n) for n in box):
                        raise ValueError("PDF table bounding box invalid")
                    geometry_tables.append(PdfTableCandidate(
                        page_number=page_no, table_index=len(geometry_tables),
                        bbox=box, row_count=rows, column_count=cols, cells=tuple(cells),
                        page_width=float(page.rect.width), page_height=float(page.rect.height),
                    ))
    warnings.add("PDF_TABLE_GRID_NOT_VERIFIED")
    return NativeLayoutReport(
        NATIVE_LAYOUT_VERSION, "PDF", source_hash, size,
        lines=tuple(lines), pdf_table_candidates=tuple(geometry_tables),
        review_reasons=tuple(sorted(warnings)),
        pdf_page_count=page_count,
    )


def inspect_native_file(path: str | Path, **kwargs: Any) -> NativeLayoutReport:
    ext = Path(path).suffix.lower()
    if ext == ".docx":
        return inspect_docx_native(path, **kwargs)
    if ext == ".pdf":
        return inspect_pdf_native(path, **kwargs)
    raise ValueError("Native Stage1 evidence supports only DOCX or PDF")
