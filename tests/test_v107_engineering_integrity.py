from __future__ import annotations

from dataclasses import replace

import pytest

from app.engineering_intelligence.history import align_requirement_versions
from app.engineering_intelligence.persistence import backfill_content_relations, backfill_version, schema_ready
from app.engineering_intelligence.requirements import extract_requirement_units


def unit(text, *, section="4.1", doc="DOC1", content=1):
    return extract_requirement_units(
        document_id=doc, content_id=content, section_id=section, text=text
    )[0]


def test_extraction_fails_closed_before_truncating_source():
    text = "first\nsecond\nthird\n"
    with pytest.raises(ValueError, match="exceeds 2 requirement units"):
        extract_requirement_units(
            document_id="D", content_id=12, section_id="4.1", text=text,
            max_units=2,
        )


def test_exact_duplicate_boilerplate_alignment_is_linearish():
    # Thousands of identical rows previously induced repeated quadratic
    # scanning over identical hashes.
    row = unit("The default mode shall remain enabled.")
    old = tuple(replace(row, unit_key=f"old-{i}", start_offset=i) for i in range(3500))
    new = tuple(replace(row, unit_key=f"new-{i}", start_offset=i) for i in range(3500))
    matched = align_requirement_versions(old, new, max_comparisons=0)
    assert len(matched) == 3500
    assert all(item.change_type == "UNCHANGED" for item in matched)


def test_schema_ready_requires_all_six_derived_tables():
    class Cursor:
        def __init__(self, row):
            self.row = row
            self.sql = None
        def execute(self, sql):
            self.sql = sql
        def fetchone(self):
            return self.row
    full = Cursor(tuple(f"table{i}" for i in range(6)))
    assert schema_ready(full)
    assert "ei_version_backfill_receipts" in full.sql
    incomplete = Cursor(("units", "relations", "versions", "audit", None, "unresolved"))
    assert not schema_ready(incomplete)


class BackfillCursor:
    def __init__(self, text="Hello\n \nWorld"):
        self.text = text
        self.query = ""
        self.params = None
        self.batch_called = 0
        self.receipt_calls = []
        self.rows = [(1, "4.1", 5, self.text)]
        self.last_id = 0
        self.next_row = (1,)
        self.units = 0

    def execute(self, sql, params=None):
        self.query, self.params = sql, params
        if 'INSERT INTO public.ei_version_backfill_receipts' in sql:
            self.receipt_calls.append(params)

    def fetchall(self):
        if "FROM public.contents" in self.query:
            # Paged read: only return after id 0.
            return self.rows if self.params[1] == 0 else []
        raise AssertionError(f"unexpected fetchall: {self.query}")

    def fetchone(self):
        sql = self.query
        if 'to_regclass(' in sql:
            return tuple(f"ei{i}" for i in range(6))
        if 'SELECT COUNT(*), COALESCE(MAX(id),0)' in sql:
            return (len(self.rows), max([r[0] for r in self.rows], default=0))
        if 'SELECT COUNT(*) FROM public.ei_requirement_units' in sql:
            return (self.units,)
        if 'INSERT INTO public.ei_version_backfill_receipts' in sql:
            return self.next_row
        if 'SELECT 1 FROM public.contents' in sql:
            return None
        raise AssertionError(f"unexpected fetchone: {self.query}")

    def executemany(self, sql, records):
        self.batch_called += 1
        self.units += len(records)


def test_full_backfill_receipt_includes_zero_unit_content():
    cur = BackfillCursor(text=" A\n \nB")
    chunks, count = backfill_version(cur, specification_id=1, version_id=2,
                                     document_id="D1", dry_run=False)
    assert (chunks,count)==(1,2)
    assert cur.receipt_calls[0][3:6] == (1,2,1)
    assert len(cur.receipt_calls[0][-1]) == 64


def test_backfill_refuses_stale_receipt():
    cur = BackfillCursor()
    cur.next_row = None
    with pytest.raises(ValueError, match="Source version changed"):
        backfill_version(cur, specification_id=1, version_id=2, document_id="D1", dry_run=False)


def test_backfill_dryrun_never_writes_receipt_or_units():
    cur = BackfillCursor()
    backfill_version(cur, specification_id=1, version_id=2, document_id="D1", dry_run=True)
    assert cur.batch_called == 0
    assert cur.receipt_calls == []


class RelationCursor:
    def __init__(self, target_count):
        self.query = ""
        self.target_count = target_count
        self.operations = []
        self.catalog = [(2,"550",20)]

    def execute(self, sql, params=None):
        self.query = sql
        self.operations.append((sql,params))

    def fetchall(self):
        if 'FROM public.specifications' in self.query:
            return self.catalog
        if 'FROM public.ei_requirement_units' in self.query:
            rows = [
                (f"target-{i}","D2",i+100,"4.1",2,0,15,
                 f"Memory ID must be selected {i}","a"*64,"REQUIREMENT",{})
                for i in range(self.target_count)
            ]
            return rows[:2]
        raise AssertionError(self.query)


def test_ambiguous_section_never_creates_false_paragraph_edge(monkeypatch):
    from app.engineering_intelligence import persistence
    monkeypatch.setattr(persistence, "ensure_schema_ready", lambda cur: None)
    monkeypatch.setattr(persistence, "require_complete_backfill", lambda cur,vid: None)
    monkeypatch.setattr(persistence, "load_version_units", lambda *a,**k: (
        unit("Refer to Spec 550 section 4.1 for Memory ID."),
    ))
    cur = RelationCursor(target_count=2)
    result = backfill_content_relations(cur, source_specification_id=1,
                                        source_version_id=10, source_spec_code="508")
    assert result['candidate_edges'] == 0
    assert result['ambiguous_sections'] == 1
    assert result['unresolved_anchors'] == 1
    assert any("INSERT INTO public.ei_unresolved_relation_anchors" in sql for sql,_ in cur.operations)
    assert not any("INSERT INTO public.ei_content_relations" in sql for sql,_ in cur.operations)


def test_unique_section_can_create_unreviewed_content_edge(monkeypatch):
    from app.engineering_intelligence import persistence
    monkeypatch.setattr(persistence, "ensure_schema_ready", lambda cur: None)
    monkeypatch.setattr(persistence, "require_complete_backfill", lambda cur,vid: None)
    monkeypatch.setattr(persistence, "load_version_units", lambda *a,**k: (
        unit("Refer to Spec 550 section 4.1 for Memory ID."),
    ))
    cur = RelationCursor(target_count=1)
    result = backfill_content_relations(cur, source_specification_id=1,
                                        source_version_id=10, source_spec_code="508")
    assert result['candidate_edges'] == 1
    assert result['ambiguous_sections'] == 0
    assert any("INSERT INTO public.ei_content_relations" in sql for sql,_ in cur.operations)


def test_missing_target_backfill_records_unresolved_not_fabricated(monkeypatch):
    from app.engineering_intelligence import persistence
    monkeypatch.setattr(persistence, "ensure_schema_ready", lambda cur: None)
    def require(cur, vid):
        if vid == 20:
            raise ValueError("not indexed")
    monkeypatch.setattr(persistence, "require_complete_backfill", require)
    monkeypatch.setattr(persistence, "load_version_units", lambda *a,**k: (
        unit("Refer to Spec 550 section 4.1 for Memory ID."),
    ))
    cur = RelationCursor(target_count=0)
    stats = backfill_content_relations(cur, source_specification_id=1,
                                       source_version_id=10, source_spec_code="508")
    assert stats['target_versions_not_indexed'] == 1
    assert stats['candidate_edges'] == 0
