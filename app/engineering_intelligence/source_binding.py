"""Read-only, fail-closed native-to-SQL provenance candidates (Stage 1 v1.12).

A binding is *not* an assertion that a requirement is correct. Raw file digest
must match the authoritative document record, and text must match verbatim and
uniquely within the snapshot of SQL `contents`. Text normalization, fuzzy
search, and LLM inference are intentionally forbidden in this evidence layer.
"""
from __future__ import annotations

import hashlib
from collections import defaultdict, deque
from dataclasses import dataclass, replace
from typing import Iterable

from .native_layout import NativeLayoutReport
from .requirements import RequirementUnit

BINDING_VERSION = "native-sql-unique-verbatim-v1"


@dataclass(frozen=True, slots=True)
class SourceContent:
    content_id: int
    document_id: str
    text: str
    page_number: int | None = None


@dataclass(frozen=True, slots=True)
class NativeAnchor:
    anchor_key: str
    kind: str
    position: tuple[int, ...]
    text_hash: str
    text: str


@dataclass(frozen=True, slots=True)
class SourceBinding:
    anchor_key: str
    kind: str
    position: tuple[int, ...]
    text_hash: str
    status: str
    content_id: int | None
    start_offset: int | None
    end_offset: int | None
    requirement_unit_key: str | None
    review_reasons: tuple[str, ...]


def native_anchors(report: NativeLayoutReport) -> tuple[NativeAnchor, ...]:
    anchors: list[NativeAnchor] = []
    def add(kind: str, pos: tuple[int, ...], value: str, checksum: str) -> None:
        if hashlib.sha256(value.encode('utf-8', 'surrogatepass')).hexdigest() != checksum:
            raise ValueError("Native text hash inconsistent with source")
        key = hashlib.sha256((report.file_sha256 + ':' + kind + ':' + ':'.join(map(str, pos))).encode()).hexdigest()
        anchors.append(NativeAnchor(key, kind, pos, checksum, value))
    if report.format == "PDF":
        for line in report.lines:
            add("PDF_LINE", (line.page_number, line.block_index, line.line_index), line.text, line.text_hash)
    elif report.format == "DOCX":
        for paragraph in report.paragraphs:
            add("DOCX_PARAGRAPH", (paragraph.body_order,), paragraph.text, paragraph.text_hash)
        for table in report.tables:
            for cell in table.cells:
                if cell.is_merge_continuation or cell.nested_table:
                    continue  # a copied/flattened merge is not independent evidence
                add("DOCX_CELL", (table.table_index, cell.row, cell.column), cell.text, cell.text_hash)
    else:
        raise ValueError("Unsupported native report format")
    if len({a.anchor_key for a in anchors}) != len(anchors):
        raise ValueError("Repeated native anchor identity")
    return tuple(anchors)


def _build_prefix_automaton(prefixes: set[str], *, max_nodes: int) -> tuple[list[dict[str, int]], list[int], list[list[str]]]:
    """Aho-Corasick over bounded literal prefixes. Scans SQL once, no O(lines * chunks) lookup."""
    edges: list[dict[str, int]] = [{}]
    fail: list[int] = [0]
    outputs: list[list[str]] = [[]]
    for word in sorted(prefixes):
        state = 0
        for ch in word:
            nxt = edges[state].get(ch)
            if nxt is None:
                if len(edges) >= max_nodes:
                    raise ValueError("Native prefix index budget exceeded")
                nxt = len(edges)
                edges[state][ch] = nxt
                edges.append({})
                fail.append(0)
                outputs.append([])
            state = nxt
        outputs[state].append(word)
    queue = deque(edges[0].values())
    while queue:
        state = queue.popleft()
        for ch, nxt in edges[state].items():
            queue.append(nxt)
            fallback = fail[state]
            while fallback and ch not in edges[fallback]:
                fallback = fail[fallback]
            fail[nxt] = edges[fallback].get(ch, 0)
            outputs[nxt].extend(outputs[fail[nxt]])
    return edges, fail, outputs


def bind_native_to_sql(
    report: NativeLayoutReport,
    *, document_id: str,
    expected_source_file_sha256: str,
    contents: Iterable[SourceContent],
    units: Iterable[RequirementUnit] = (),
    min_exact_chars: int = 12,
    max_anchors: int = 100_000,
    max_source_chars: int = 30_000_000,
    max_automaton_nodes: int = 500_000,
    max_prefix_hits: int = 500_000,
    max_prefix_bucket: int = 1_000,
) -> tuple[SourceBinding, ...]:
    """Produce only exact/unique, reviewable evidence; never mutate SQL.

    `expected_source_file_sha256` MUST be sourced from the version-owned
    `documents.source_hash` record, not supplied by trusting the file itself.
    Every content row must be from that one version's document and snapshot.
    """
    if (not document_id or not expected_source_file_sha256 or
            expected_source_file_sha256.lower() != report.file_sha256.lower() or
            len(expected_source_file_sha256) != 64):
        raise ValueError("Stored version document source SHA-256 missing or mismatched: refusing binding")
    if (min_exact_chars < 8 or max_anchors < 1 or max_source_chars < 1 or
            max_prefix_hits < 1 or max_prefix_bucket < 1 or max_automaton_nodes < 1):
        raise ValueError("Invalid source binding budgets")
    anchors = native_anchors(report)
    if len(anchors) > max_anchors:
        raise ValueError("Native anchor budget exceeded")
    source_rows = tuple(contents)
    if len({row.content_id for row in source_rows}) != len(source_rows):
        raise ValueError("Duplicate SQL content identity")
    total_chars = 0
    for row in source_rows:
        if (row.document_id != document_id or row.content_id <= 0 or
                (row.page_number is not None and row.page_number < 1)):
            raise ValueError("Foreign or invalid SQL content identity")
        total_chars += len(row.text)
        if total_chars > max_source_chars:
            raise ValueError("SQL source text budget exceeded")
    by_content: dict[int, list[RequirementUnit]] = defaultdict(list)
    for unit in units:
        if unit.document_id != document_id:
            raise ValueError("Foreign requirement unit")
        by_content[unit.content_id].append(unit)
    rows_by_id = {row.content_id: row for row in source_rows}
    for cid, requirement_units in by_content.items():
        row = rows_by_id.get(cid)
        if row is None:
            raise ValueError("Requirement unit references unknown SQL content")
        for unit in requirement_units:
            if (unit.start_offset < 0 or unit.end_offset > len(row.text) or
                    row.text[unit.start_offset:unit.end_offset] != unit.text):
                raise ValueError("Requirement unit source span drift")
    # Start from bounded prefixes; extend collision buckets only where needed,
    # instead of doing O(number_of_anchors * number_of_SQL_rows) comparisons.
    prefix_groups: dict[str, list[int]] = defaultdict(list)
    for idx, anchor in enumerate(anchors):
        if len(anchor.text.strip()) >= min_exact_chars and len(anchor.text) >= min_exact_chars:
            prefix_groups[anchor.text[:min_exact_chars]].append(idx)
    pending = list(prefix_groups.items())
    by_prefix: dict[str, list[int]] = {}
    while pending:
        prefix, indexes = pending.pop()
        if len(indexes) <= max_prefix_bucket:
            by_prefix[prefix] = indexes
            continue
        if len(prefix) >= max(len(anchors[i].text) for i in indexes):
            raise ValueError("Repeated native prefix exceeds candidate budget")
        subdivided: dict[str, list[int]] = defaultdict(list)
        for idx in indexes:
            subdivided[anchors[idx].text[:len(prefix) + 1]].append(idx)
        pending.extend(subdivided.items())
    matches: dict[int, set[tuple[int, int, int]]] = defaultdict(set)
    if by_prefix:
        edges, fail, outs = _build_prefix_automaton(set(by_prefix), max_nodes=max_automaton_nodes)
        prefix_hits = 0
        for row in source_rows:
            state = 0
            for right, ch in enumerate(row.text):
                while state and ch not in edges[state]:
                    state = fail[state]
                state = edges[state].get(ch, 0)
                if not outs[state]:
                    continue
                for prefix in outs[state]:
                    left = right + 1 - len(prefix)
                    prefix_hits += 1
                    if prefix_hits > max_prefix_hits:
                        raise ValueError("Ambiguous native-prefix hit budget exceeded")
                    for idx in by_prefix[prefix]:
                        text = anchors[idx].text
                        if row.text.startswith(text, left) and len(matches[idx]) < 2:
                            matches[idx].add((row.content_id, left, left + len(text)))
    result: list[SourceBinding] = []
    for idx, anchor in enumerate(anchors):
        coords = sorted(matches.get(idx, ()))
        cid: int | None = None
        left: int | None = None
        right: int | None = None
        unit_key: str | None = None
        reasons: tuple[str, ...]
        if len(anchor.text.strip()) < min_exact_chars or len(anchor.text) < min_exact_chars:
            status = "TOO_SHORT_FOR_UNIQUE_BINDING"
            reasons = ("SHORT_ANCHOR_REQUIRES_CONTEXT",)
        elif not coords:
            status = "NO_VERBATIM_SQL_MATCH"
            reasons = ("PDF_DOCX_TEXT_MAY_DIFFER_FROM_INGESTED_CONTENT",)
        elif len(coords) > 1:
            status = "AMBIGUOUS_VERBATIM_SQL_MATCH"
            reasons = ("REPEATED_SOURCE_TEXT_REQUIRES_REVIEW",)
        else:
            cid, left, right = coords[0]
            sql_page = rows_by_id[cid].page_number
            if anchor.kind == "PDF_LINE" and sql_page is not None and sql_page != anchor.position[0]:
                result.append(SourceBinding(anchor.anchor_key, anchor.kind, anchor.position,
                                            anchor.text_hash, "PAGE_NUMBER_MISMATCH", None,
                                            None, None, None,
                                            ("SQL_PAGE_DISAGREES_WITH_NATIVE_PDF_PAGE",)))
                continue
            possible = [u.unit_key for u in by_content.get(cid, ())
                        if (u.start_offset <= left and right <= u.end_offset)]
            if len(possible) == 1:
                status = "UNIQUE_REQUIREMENT_SPAN_CANDIDATE"
                unit_key = possible[0]
                reasons = (("SQL_PAGE_UNAVAILABLE", "HUMAN_REVIEW_REQUIRED_FOR_ENGINEERING_SEMANTICS")
                           if anchor.kind == "PDF_LINE" and sql_page is None else
                           ("HUMAN_REVIEW_REQUIRED_FOR_ENGINEERING_SEMANTICS",))
            else:
                status = "UNIQUE_CONTENT_SPAN_CANDIDATE"
                reasons = (("NO_UNIQUE_CONTAINING_REQUIREMENT" if not possible
                            else "MULTIPLE_CONTAINING_REQUIREMENTS"),)
                if anchor.kind == "PDF_LINE" and sql_page is None:
                    reasons += ("SQL_PAGE_UNAVAILABLE",)
        result.append(SourceBinding(
            anchor.anchor_key, anchor.kind, anchor.position, anchor.text_hash,
            status, cid, left, right, unit_key, reasons,
        ))
    # An exact SQL occurrence also must not be claimed by two different
    # physical anchors (e.g. two Word table cells with identical value).
    occupied: dict[tuple[int, int, int], list[int]] = defaultdict(list)
    for idx, binding in enumerate(result):
        if binding.content_id is not None:
            occupied[(binding.content_id, binding.start_offset, binding.end_offset)].append(idx)
    for indexes in occupied.values():
        if len(indexes) > 1:
            for idx in indexes:
                result[idx] = replace(result[idx], status="AMBIGUOUS_NATIVE_SOURCE_ANCHORS",
                                      content_id=None, start_offset=None, end_offset=None,
                                      requirement_unit_key=None,
                                      review_reasons=("MULTIPLE_PHYSICAL_ANCHORS_FOR_ONE_SQL_SPAN",))
    return tuple(result)
