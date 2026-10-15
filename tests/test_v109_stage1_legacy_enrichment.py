"""v1 -> v2 enrichment must preserve approved source and review decisions."""
from __future__ import annotations

import hashlib
import json

import pytest

from app.engineering_intelligence.persistence import (
    EXTRACTOR_VERSION,
    IntelligenceSchemaMissing,
    enrich_legacy_stage1_version,
    require_complete_backfill,
    require_stage1_v2_schema,
)
from app.engineering_intelligence.requirements import extract_requirement_units


class LegacyCursor:
    def __init__(self, *, source="REQ-0100: The ECU must not load.\n| NA | 5s |",
                 stale_digest=False, changed_source=False):
        self.source = source
        self.text = source + "changed" if changed_source else source
        self.query = ""
        self.params = None
        self.operations = []
        self.writes = []
        atoms = extract_requirement_units(document_id="D",content_id=1,section_id="4.1",text=self.source, page_number=5)
        self.old = [(i+100, a.unit_key,a.text,a.text_hash,a.start_offset,a.end_offset) for i,a in enumerate(atoms)]
        self.old_count = len(atoms)
        digest = hashlib.sha256()
        for value in ("1","4.1","5",self.source):
            raw = value.encode("utf-8", "surrogatepass")
            digest.update(len(raw).to_bytes(8,"big"))
            digest.update(raw)
        self.digest = ("0" * 64) if stale_digest else digest.hexdigest()
        self._read_once = False

    def execute(self, query, params=None):
        self.query,self.params=query,params
        self.operations.append((query,params))
        if query.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            self.writes.append(query)

    def fetchone(self):
        q=self.query
        if "to_regclass" in q:
            return tuple("exists" for _ in range(q.count("to_regclass")))
        if "SELECT document_id,extractor_version" in q:
            return ("D", "requirement-units-v1",1,self.old_count,1,self.digest)
        if "SELECT 1 FROM public.contents" in q:
            return None
        if "INSERT INTO public.ei_requirement_enrichments" in q:
            return (100,)
        if "INSERT INTO public.ei_stage1_enrichment_receipts" in q:
            return (3,)
        raise AssertionError(f"unexpected fetchone: {q}")

    def fetchall(self):
        q=self.query
        if "FROM public.contents" in q:
            return [(1,"4.1",5,self.text)] if self.params[1] == 0 else []
        if "FROM public.ei_requirement_units" in q:
            return self.old
        raise AssertionError(f"unexpected fetchall: {q}")


def test_legacy_dry_run_checks_all_spans_without_writing():
    cur=LegacyCursor()
    assert enrich_legacy_stage1_version(cur, version_id=3, document_id="D") == (1, 2)
    assert cur.writes == []


def test_legacy_apply_writes_only_sidecar_not_approved_units():
    cur=LegacyCursor()
    assert enrich_legacy_stage1_version(cur,version_id=3,document_id="D",dry_run=False) == (1,2)
    assert len([x for x in cur.writes if "ei_requirement_enrichments" in x]) == 2
    assert len([x for x in cur.writes if "ei_stage1_enrichment_receipts" in x]) == 1
    assert all("UPDATE public.ei_requirement_units" not in x for x in cur.writes)
    assert all("DELETE" not in x for x in cur.writes)


@pytest.mark.parametrize("change",["digest","source"])
def test_legacy_source_mismatch_refuses_receipt(change):
    cur=LegacyCursor(stale_digest=change=="digest",changed_source=change=="source")
    with pytest.raises(ValueError, match="snapshot changed|differs|mismatch"):
        enrich_legacy_stage1_version(cur,version_id=3,document_id="D",dry_run=False)
    assert not any("INSERT INTO public.ei_stage1_enrichment_receipts" in x for x in cur.writes)


def test_legacy_requires_same_document_and_v1_receipt():
    cur=LegacyCursor()
    with pytest.raises(ValueError,match="Expected a complete legacy"):
        enrich_legacy_stage1_version(cur,version_id=3,document_id="WRONG")


def test_legacy_v2_schema_check_is_explicit():
    class Missing:
        def execute(self,sql): pass
        def fetchone(self): return ("exists",None)
    with pytest.raises(IntelligenceSchemaMissing,match="upgrade_engineering_stage1_v2.sql"):
        require_stage1_v2_schema(Missing())


class ReceiptCursor:
    def __init__(self, enrichment):
        self.query = ""
        self.enrichment = enrichment
    def execute(self,query,params=None): self.query=query
    def fetchone(self):
        q=self.query
        if "to_regclass" in q: return ("exists","exists")
        if "FROM public.specification_versions sv" in q:
            return ("D","requirement-units-v1",1,2,1,"D",1,1,2)
        if "FROM public.ei_stage1_enrichment_receipts" in q: return self.enrichment
        if "SELECT source_digest FROM public.ei_version_backfill_receipts" in q:
            return ("a"*64,)
        raise AssertionError(q)


def test_legacy_receipt_accepted_only_when_enrichment_matches():
    require_complete_backfill(ReceiptCursor((EXTRACTOR_VERSION,"a"*64,2)),3)
    with pytest.raises(ValueError,match="requires verified"):
        require_complete_backfill(ReceiptCursor((EXTRACTOR_VERSION,"b"*64,2)),3)
    with pytest.raises(ValueError,match="requires verified"):
        require_complete_backfill(ReceiptCursor(None),3)


def test_additive_v2_migration_never_mutates_approved_v1_requirements():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    sql = (root / 'database/upgrade_engineering_stage1_v2.sql').read_text(encoding='utf-8')
    assert 'CREATE TABLE IF NOT EXISTS public.ei_requirement_enrichments' in sql
    assert 'CREATE TABLE IF NOT EXISTS public.ei_stage1_enrichment_receipts' in sql
    assert 'REFERENCES public.ei_requirement_units(id) ON DELETE CASCADE' in sql
    forbidden = ('TRUNCATE ', 'DROP TABLE ', 'DELETE FROM ', 'UPDATE public.ei_requirement_units',
                 'ALTER TABLE public.ei_requirement_units')
    assert all(word not in sql.upper() for word in forbidden)


def test_stage1_v2_schema_check_refuses_missing_or_empty_results():
    from app.engineering_intelligence.persistence import stage1_v2_schema_ready
    class Cursor:
        def __init__(self, val): self.val=val
        def execute(self,sql): assert 'ei_requirement_enrichments' in sql
        def fetchone(self): return self.val
    assert stage1_v2_schema_ready(Cursor(('units','receipts')))
    assert not stage1_v2_schema_ready(Cursor(()))
    assert not stage1_v2_schema_ready(Cursor(None))
    assert not stage1_v2_schema_ready(Cursor(('units', None)))
