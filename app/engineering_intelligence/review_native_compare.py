"""Read-only native-vs-SQL evidence comparison for a HUMAN Stage 1 reviewer.

Verbatim evidence is not an approval of table headers or engineering semantics.
No LLM, OCR, heuristics, DB writes, or normalization of engineering text.
"""
from __future__ import annotations

import hashlib
from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .native_layout import NativeLayoutReport, inspect_native_file
from .crosspage import suggest_crosspage_tables
from .native_review_bundle import prepare_native_table_reviews
from .persistence import load_version_units, require_complete_backfill
from .review_queue import ReviewDetail, check_database_project
from .source_binding import SourceBinding, SourceContent, bind_native_to_sql, native_anchors
from .stage1_review import sha
from .stage1_v3 import verify_live_source_snapshot


@dataclass(frozen=True, slots=True)
class NativeComparison:
    status: str  # EXACT_NATIVE_CELLS_CANDIDATE / PARTIAL_OR_AMBIGUOUS / NO_EXACT_NATIVE_CELL
    source_sha256: str
    proposal_key: str
    sql_text: str
    native_text: str
    matches: tuple[dict[str, Any], ...]
    review_flags: tuple[str, ...]
    reviewed_cells: int
    exact_cells: int
    sql_excerpt_start: int = 0
    # Never represents confirmed engineering logic or approved table structure.
    human_review_required: bool = True


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8', 'surrogatepass')).hexdigest()


def _source_rows_for_version(
    cur: Any, *, project: str, version_id: int, report: NativeLayoutReport,
    max_contents: int, max_source_chars: int, max_units: int,
) -> tuple[str, tuple[SourceContent, ...], tuple[Any, ...]]:
    if project not in ('21MM', '24MM') or version_id < 1:
        raise ValueError('Explicit authorized engineering project/version required')
    if min(max_contents, max_source_chars, max_units) < 1:
        raise ValueError('Native comparison resource limits must be positive')
    check_database_project(cur, project)
    cur.execute('''SELECT sv.document_id,d.source_hash FROM public.specification_versions sv
        JOIN public.specifications sp ON sp.id=sv.specification_id
        JOIN public.documents d ON d.document_id=sv.document_id
        WHERE sv.id=%s AND sp.project_code=%s''', (version_id, project))
    owned = cur.fetchone()
    if not owned:
        raise ValueError('Version does not belong to authorized project')
    document_id, trusted_sha = str(owned[0]), str(owned[1] or '').lower()
    if len(trusted_sha) != 64 or trusted_sha != report.file_sha256.lower():
        raise ValueError('Native file SHA-256 disagrees with authoritative SQL document')
    require_complete_backfill(cur, version_id)
    verify_live_source_snapshot(cur, version_id=version_id, max_rows=max_contents)
    cur.execute('''SELECT count(*),coalesce(sum(length(content)),0)
        FROM public.contents WHERE document_id=%s''', (document_id,))
    row = cur.fetchone()
    if not row or int(row[0]) > max_contents or int(row[1]) > max_source_chars:
        raise ValueError('SQL source exceeds native comparison budget')
    cur.execute('''SELECT id,page_number,content FROM public.contents
        WHERE document_id=%s ORDER BY id LIMIT %s''', (document_id, max_contents + 1))
    rows = cur.fetchall()
    if len(rows) > max_contents:
        raise ValueError('SQL source rows exceed budget')
    contents = tuple(SourceContent(int(cid), document_id, str(body or ''),
                       int(page) if page is not None else None) for cid, page, body in rows)
    units = load_version_units(cur, version_id, max_units=max_units + 1)
    if len(units) > max_units:
        raise ValueError('Requirement count exceeds budget')
    return document_id, contents, units


def read_verified_native_context(
    cur: Any, *, project: str, version_id: int, source_path: str | Path,
    max_contents: int = 20_000, max_source_chars: int = 30_000_000,
    max_units: int = 80_000, max_anchors: int = 100_000,
) -> tuple[NativeLayoutReport, tuple[SourceContent, ...], tuple[SourceBinding, ...]]:
    """Caller MUST hold a read-only repeatable-read transaction."""
    report = inspect_native_file(source_path)
    document_id, contents, units = _source_rows_for_version(
        cur, project=project, version_id=version_id, report=report,
        max_contents=max_contents, max_source_chars=max_source_chars, max_units=max_units)
    bindings = bind_native_to_sql(report, document_id=document_id,
        expected_source_file_sha256=report.file_sha256, contents=contents,
        units=units, max_anchors=max_anchors, max_source_chars=max_source_chars)
    return report, contents, bindings


def compare_review_to_native(
    detail: ReviewDetail, report: NativeLayoutReport,
    contents: tuple[SourceContent, ...], bindings: tuple[SourceBinding, ...],
    *, max_review_cells: int = 4096, max_display_chars: int = 30_000,
) -> NativeComparison:
    """Only exact SQL span + exact physical anchor can match a proposal cell.

    A native cell may contain extra text; its SQL offset must contain the whole
    candidate span and the exact native substring must equal the source value.
    Multiple possible cells => AMBIGUOUS, not a best-guess match.
    """
    if max_review_cells < 1 or max_display_chars < 1:
        raise ValueError('Invalid native evidence display budget')
    if sha(detail.payload) != detail.item.digest:
        raise ValueError('Review evidence digest drift')
    if detail.item.kind not in ('TABLE', 'SCOPE'):
        raise ValueError('Unknown review evidence kind')
    bundle = prepare_native_table_reviews(report, bindings)
    physical = {a.anchor_key: a for a in native_anchors(report)}
    lookup = {c.content_id: c for c in contents}
    if len(lookup) != len(contents):
        raise ValueError('Duplicate SQL content rows')
    # The physical evidence bundle itself proves every anchor identity matches
    # the parsed source file; it does not prove business table semantics.
    links: list[dict[str, Any]] = []
    structural_flags: set[str] = set()
    table_flags = {table.identity: table.structural_flags for table in bundle}
    for table in bundle:
        for link in table.exact_cell_links:
            links.append({**link, 'native_table': table.identity})
    if detail.item.kind == 'SCOPE':
        # Scope requirements may come from Word body paragraphs or native PDF
        # text lines, not just tables. Preserve the actual physical locator.
        allowed = {'UNIQUE_REQUIREMENT_SPAN_CANDIDATE', 'UNIQUE_CONTENT_SPAN_CANDIDATE'}
        for binding in bindings:
            if (binding.kind not in ('DOCX_PARAGRAPH', 'PDF_LINE') or
                    binding.status not in allowed or binding.content_id is None or
                    binding.start_offset is None or binding.end_offset is None):
                continue
            locator = ':'.join(map(str, binding.position))
            links.append({'native_table': f'{binding.kind}:{locator}',
                          'row': binding.position[0], 'column': 0,
                          'content_id': binding.content_id,
                          'start_offset': binding.start_offset,
                          'end_offset': binding.end_offset,
                          'anchor_key': binding.anchor_key,
                          'source_text_sha256': binding.text_hash})
    # Per-content interval index: query candidate containment in O(log N + K),
    # not O(proposal_cells * whole_document_native_cells).
    by_content: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for link in links:
        by_content[link['content_id']].append(link)
    indexed: dict[int, tuple[list[int], list[int], list[dict[str, Any]]]] = {}
    for content_id, evidence in by_content.items():
        evidence.sort(key=lambda x: (x['start_offset'], x['end_offset']))
        starts = [x['start_offset'] for x in evidence]
        prefix_max_end: list[int] = []
        furthest = -1
        for item in evidence:
            furthest = max(furthest, item['end_offset'])
            prefix_max_end.append(furthest)
        indexed[content_id] = starts, prefix_max_end, evidence
    payload = detail.payload
    if detail.item.kind == 'TABLE':
        cid = payload.get('content_id')
        rows = payload.get('rows')
        if not isinstance(cid, int) or cid not in lookup or not isinstance(rows, list):
            raise ValueError('Table proposal has no verifiable SQL contents/rows')
        cells = [cell for row in rows for cell in row.get('cells', [])]
        if len(cells) > max_review_cells:
            raise ValueError('Table cell review budget exceeded')
        proposal_flags = payload.get('review_reasons') or []
        structural_flags.update(str(f) for f in proposal_flags)
        if not cells:
            raise ValueError('Empty table evidence')
    else:
        cid = payload.get('source_content_id')
        if not isinstance(cid, int) or cid not in lookup:
            raise ValueError('Scope proposal missing SQL source content')
        left = payload.get('candidate_source_start')
        right = payload.get('candidate_source_end')
        if not isinstance(left, int) or not isinstance(right, int) or not 0 <= left < right <= len(lookup[cid].text):
            raise ValueError('Invalid scope proposal source span')
        phrase = lookup[cid].text[left:right]
        if _digest(phrase) != payload.get('candidate_text_hash'):
            raise ValueError('Scope proposal text fingerprint disagrees with SQL source')
        cells = [{'start_in_content': left, 'end_in_content': right, 'source_text': phrase}]
        structural_flags.update(str(f) for f in payload.get('review_reasons') or [])
    sql_body = lookup[cid].text
    previews: list[str] = []
    matches: list[dict[str, Any]] = []
    exact_count = 0
    preview_size = 0
    for cell in cells:
        left, right, value = cell.get('start_in_content'), cell.get('end_in_content'), cell.get('source_text')
        if (not isinstance(left, int) or not isinstance(right, int) or
                not isinstance(value, str) or not 0 <= left < right <= len(sql_body) or
                sql_body[left:right] != value):
            raise ValueError('Proposal cell text/SQL offset does not match source snapshot')
        valid = []
        starts, maximal_ends, evidence = indexed.get(cid, ([], [], []))
        lower = bisect_left(maximal_ends, right)
        upper = bisect_right(starts, left)
        for link in evidence[lower:upper]:
            if link['end_offset'] < right:
                continue
            a = physical.get(link['anchor_key'])
            if a is None or a.text_hash != link['source_text_sha256']:
                raise ValueError('Native physical evidence identity drift')
            offset = left - link['start_offset']
            if a.text[offset:offset + len(value)] == value:
                valid.append((link, a))
        unique = len(valid) == 1
        status = ('EXACT_NATIVE_SUBSPAN_CANDIDATE' if unique else
                  'AMBIGUOUS_NATIVE_CELLS' if len(valid) > 1 else 'NO_EXACT_NATIVE_CELL')
        if unique:
            exact_count += 1
            link, anchor = valid[0]
            table_id = link['native_table']
            structural_flags.update(table_flags.get(table_id, ()))
            preview_line = f"{table_id} | row={link['row']} col={link['column']} | {anchor.text}"
            preview_size += len(preview_line) + 1
            if preview_size > max_display_chars:
                raise ValueError('Native matching evidence exceeds display budget')
            previews.append(preview_line)
            matches.append({'sql_start': left, 'sql_end': right, 'status': status,
                            'native_table': table_id, 'native_row': link['row'],
                            'native_column': link['column'], 'native_anchor_sha256': anchor.text_hash,
                            'source_text_sha256': _digest(value)})
        else:
            structural_flags.add(status)
            matches.append({'sql_start': left, 'sql_end': right, 'status': status,
                            'candidate_native_cells': len(valid), 'source_text_sha256': _digest(value)})
    # A cross-page continuation is only a geometry hint. Expose the flag
    # exclusively when a matched cell belongs to a candidate pair.
    matched_tables = {m.get('native_table') for m in matches if m.get('native_table')}
    if report.format == 'PDF' and matched_tables:
        for c in suggest_crosspage_tables(report):
            before = f'PDF:{c.previous_page}:{c.previous_table_index}'
            after = f'PDF:{c.next_page}:{c.next_table_index}'
            if before in matched_tables or after in matched_tables:
                structural_flags.add('POSSIBLE_CROSSPAGE_CONTINUATION_REQUIRES_HUMAN_REVIEW')
    if not exact_count:
        status = 'NO_EXACT_NATIVE_CELL'
    elif exact_count < len(cells):
        status = 'PARTIAL_OR_AMBIGUOUS'
    else:
        status = 'EXACT_NATIVE_CELLS_CANDIDATE'
    # Display a bounded window containing EVERY candidate span. Never let a
    # silent prefix truncation hide the particular evidence under review.
    first = min(m['sql_start'] for m in matches)
    last = max(m['sql_end'] for m in matches)
    if last - first > max_display_chars:
        raise ValueError('Review spans exceed SQL display budget; use narrower proposal')
    offset = min(max(0, first - 160), max(0, len(sql_body) - max_display_chars))
    if last > offset + max_display_chars:
        offset = last - max_display_chars
    excerpt = sql_body[offset:offset + max_display_chars]
    native_excerpt = '\n'.join(previews)
    if len(native_excerpt) > max_display_chars:
        raise ValueError('Native matching evidence exceeds display budget')
    return NativeComparison(
        status, report.file_sha256, detail.item.key,
        excerpt, native_excerpt,
        tuple(matches), tuple(sorted(structural_flags)), len(cells), exact_count,
        sql_excerpt_start=offset)
