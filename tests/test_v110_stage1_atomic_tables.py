"""Stage1 candidate and table contract, generated test snippets only."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import random

import pytest

from app.engineering_intelligence.atomic import (
    extract_atomic_candidates, validate_atomic_candidates,
)
from app.engineering_intelligence.tables import (
    extract_table_blocks, verify_table_blocks,
)
from app.engineering_intelligence.requirements import extract_requirement_units
from app.engineering_intelligence.stage1_v3 import prepare_stage1_v3, _source_fingerprint
from app.engineering_intelligence.quality import review_stage1_v3


def units(source: str, **overrides):
    return extract_requirement_units(
        document_id='fixture-doc', content_id=9, section_id='4.1',
        text=source, **overrides,
    )


def test_atomic_independent_normative_clauses_preserve_citations():
    text = 'When BT connects, ECU shall load profile; ECU must notify Body ECU.'
    unit = units(text)[0]
    atoms = extract_atomic_candidates(unit)
    assert len(atoms) == 2
    assert all(a.role == 'REQUIREMENT_CANDIDATE' for a in atoms)
    assert atoms[0].text == 'When BT connects, ECU shall load profile;'
    assert atoms[1].text == 'ECU must notify Body ECU.'
    assert all('CONDITION_OR_EXCEPTION_SCOPE_UNRESOLVED' in a.review_reasons for a in atoms)
    validate_atomic_candidates(unit, atoms)
    assert atoms == extract_atomic_candidates(unit)


def test_no_split_when_second_clause_lacks_own_modality():
    unit = units('ECU shall load; and then store the profile.')[0]
    atom, = extract_atomic_candidates(unit)
    assert atom.text == unit.text
    assert 'COMPOUND_BOUNDARY_NOT_SAFE' in atom.review_reasons


def test_parentheses_and_unbalanced_quote_never_force_split():
    for text in ('ECU shall use (A; B) and notify.',
                 'ECU shall use "A; B; ECU shall notify.'):
        assert len(extract_atomic_candidates(units(text)[0])) == 1


def test_japanese_semicolon_cues():
    text = 'ECUは通知すること；HUは保存すること。'
    atoms = extract_atomic_candidates(units(text)[0])
    assert len(atoms) == 2
    assert atoms[0].text.endswith('；')


def test_do_not_split_table_row_even_if_semicolon():
    atom, = extract_atomic_candidates(units('| NA | must load; must save |')[0])
    assert atom.role == 'TABLE_CONTEXT'


def test_non_normative_source_is_context_not_requirement():
    atom, = extract_atomic_candidates(units('Refer to Spec 550 Section 4.1')[0])
    assert atom.role == 'CONTEXT'
    assert atom.review_status == 'SOURCE_ONLY'


def test_atomic_exact_offsets_and_fail_closed():
    unit = units(' ECU shall load; ECU must save ')[0]
    a, b = extract_atomic_candidates(unit)
    with pytest.raises(ValueError, match='source coordinate'):
        validate_atomic_candidates(unit, (replace(a,start_in_content=999), b))
    with pytest.raises(ValueError, match='digest'):
        validate_atomic_candidates(unit, (replace(a,text_hash='0'*64), b))
    with pytest.raises(ValueError, match='Uncovered'):
        validate_atomic_candidates(unit,(a,))


def test_budget_fail_and_full_source_preservation():
    unit = units('shall A; shall B; shall C')[0]
    with pytest.raises(ValueError, match='budget'):
        extract_atomic_candidates(unit, max_candidates=2)
    assert len(extract_atomic_candidates(unit, max_candidates=3)) == 3


def test_explicit_markdown_table_links_headers_exactly():
    source = '|条件|動作|\n|---|---|\n|NA|保存すること|\n|EU|送信すること|'
    body = units(source)
    blocks = extract_table_blocks(body)
    assert len(blocks) == 1
    table = blocks[0]
    assert table.header_status == 'EXPLICIT_MARKDOWN'
    assert [r.row_kind for r in table.rows] == ['HEADER','SEPARATOR','DATA','DATA']
    assert table.rows[2].cells[1].header_text == '動作'
    assert table.rows[2].cells[1].header_unit_key == body[0].unit_key
    verify_table_blocks(body, blocks)
    assert table.table_key == extract_table_blocks(body)[0].table_key


def test_arbitrary_table_does_not_infer_headers():
    table, = extract_table_blocks(units('A\tB\nif connected\tshall load'))
    assert table.header_status == 'UNRESOLVED'
    assert table.rows[1].cells[0].header_text is None
    assert 'HEADER_NOT_EXPLICIT' in table.review_reasons


def test_mismatched_columns_no_false_mapping():
    source='|A|B|\n|---|---|\n|1|2|3|'
    table, = extract_table_blocks(units(source))
    assert 'COLUMN_COUNT_MISMATCH' in table.review_reasons
    assert all(cell.header_text is None for row in table.rows for cell in row.cells)


def test_empty_middle_table_cells_not_silently_dropped():
    source='|A|B|C|\n|---|---|---|\n|1||3|'
    table,=extract_table_blocks(units(source))
    assert table.rows[-1].cells[1].source_text == ''
    assert table.rows[-1].cells[1].header_text == 'B'


def test_escaped_pipe_not_mapped_as_verified_header():
    source='|A|B|\n|---|---|\n|a\\|b|c|'
    table,=extract_table_blocks(units(source))
    assert table.header_status == 'UNRESOLVED'
    assert 'ESCAPED_PIPE_OR_LAYOUT_UNSUPPORTED' in table.review_reasons


def test_intervening_prose_and_content_scope_break_tables():
    values=units('|A|B|\nordinary text\n|1|2|')
    assert len(extract_table_blocks(values)) == 2
    second=replace(values[2], content_id=10)
    assert len(extract_table_blocks((values[0],second))) == 2


def test_table_evidence_integrity_violation_detected():
    values=units('|H|Q|\n|---|---|\n|A|1|')
    table,=extract_table_blocks(values)
    badcell=replace(table.rows[2].cells[0],start_in_content=555)
    badrow=replace(table.rows[2],cells=(badcell,table.rows[2].cells[1]))
    badtable=replace(table,rows=(table.rows[0],table.rows[1],badrow))
    with pytest.raises(ValueError,match='coordinate'):
        verify_table_blocks(values,(badtable,))


def test_v3_prepare_is_deterministic_and_reviewed():
    source='shall load; must store\n|A|B|\n|---|---|\n|NA|shall save|'
    values=units(source)
    a, candidates, tables=prepare_stage1_v3(version_id=1, units=values)
    b, *_=prepare_stage1_v3(version_id=1, units=values)
    assert a==b
    assert a.source_units==4
    assert a.atomic_candidates==5
    assert a.table_blocks==1
    assert a.review_required>=2
    assert all(c.review_status != 'APPROVED' for c in candidates)
    assert 'TABLE_MERGED_CELL_GEOMETRY_UNAVAILABLE' in [f.code for f in review_stage1_v3(values)]


def test_fingerprint_changes_when_source_identity_changes():
    values=units('ECU shall load.')
    other=(replace(values[0], text_hash='0'*64),)
    assert _source_fingerprint(values)!=_source_fingerprint(other)


def test_large_table_and_atomic_budgets_not_truncated():
    rows=units('\n'.join(f'ECU shall load {i}; ECU shall save {i}.' for i in range(1000)),max_units=1100)
    with pytest.raises(ValueError,match='atomic budget'):
        prepare_stage1_v3(version_id=3,units=rows,max_atomic_candidates=1000)
    result, *_=prepare_stage1_v3(version_id=3, units=rows,max_atomic_candidates=2100)
    assert result.atomic_candidates==2000


def test_table_budgets_fail_closed():
    rows=units('\n'.join(f'|A|{i}|' for i in range(100)),max_units=150)
    with pytest.raises(ValueError,match='row budget'):
        extract_table_blocks(rows,max_rows=50)
    with pytest.raises(ValueError,match='column budget'):
        extract_table_blocks(rows,max_columns=1)
