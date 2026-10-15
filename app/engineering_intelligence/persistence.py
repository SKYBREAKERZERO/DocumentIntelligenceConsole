"""Explicit, additive PostgreSQL persistence for Engineering Intelligence v7.

Never creates schema implicitly, never deletes source business data, and never
changes APPROVED requirements or CONFIRMED relationships during a backfill.
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import asdict
from typing import Any

from .requirements import RequirementUnit, EXTRACTOR_VERSION, extract_requirement_units, validate_extracted_units
from .history import align_requirement_versions
from .relations import link_explicit_references


class IntelligenceSchemaMissing(RuntimeError):
    pass


def schema_ready(cur: Any) -> bool:
    cur.execute("""SELECT
        to_regclass('public.ei_requirement_units') AS requirement_units,
        to_regclass('public.ei_content_relations') AS content_relations,
        to_regclass('public.ei_version_mappings') AS version_mappings,
        to_regclass('public.ei_relation_review_audit') AS review_audit,
        to_regclass('public.ei_version_backfill_receipts') AS backfill_receipts,
        to_regclass('public.ei_unresolved_relation_anchors') AS unresolved_anchors
    """)
    row = cur.fetchone()
    values = tuple(row.values()) if isinstance(row, dict) else row
    return bool(values and len(values) == 6 and all(value is not None for value in values))


def stage1_v2_schema_ready(cur: Any) -> bool:
    cur.execute("""SELECT
        to_regclass('public.ei_requirement_enrichments'),
        to_regclass('public.ei_stage1_enrichment_receipts')""")
    row = cur.fetchone()
    values = tuple(row.values()) if isinstance(row, dict) else row
    return bool(values and len(values) == 2 and all(value is not None for value in values))


def require_stage1_v2_schema(cur: Any) -> None:
    if not stage1_v2_schema_ready(cur):
        raise IntelligenceSchemaMissing(
            "Run database/upgrade_engineering_stage1_v2.sql (additive) after v7 schema"
        )


def ensure_schema_ready(cur: Any) -> None:
    if not schema_ready(cur):
        raise IntelligenceSchemaMissing("Run database/upgrade_engineering_intelligence_v7.sql first")


def _signals(unit: RequirementUnit) -> str:
    return json.dumps(asdict(unit.signals), ensure_ascii=False)


def save_units(cur: Any, *, specification_id: int, version_id: int, units: tuple[RequirementUnit, ...]) -> int:
    """Upsert source spans, preserving any human review decision."""
    if not units:
        return 0
    statement = """
        INSERT INTO public.ei_requirement_units (
            unit_key, extractor_version, specification_id, version_id, document_id,
            content_id, section_id, page_number, start_offset, end_offset,
            unit_type, raw_text, text_hash, signals
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
        ON CONFLICT (unit_key) DO NOTHING
    """
    records = [
        (item.unit_key, EXTRACTOR_VERSION, specification_id, version_id,
         item.document_id, item.content_id, item.section_id, item.page_number,
         item.start_offset, item.end_offset, item.unit_type, item.text,
         item.text_hash, _signals(item)) for item in units
    ]
    # psycopg3 executemany uses pipelining when supported, markedly reducing
    # round trips during million-line backfills; memory stays batch bounded.
    cur.executemany(statement, records)
    return len(units)


def backfill_version(cur: Any, *, specification_id: int, version_id: int, document_id: str,
                     max_rows: int = 100_000, dry_run: bool = True,
                     max_units_per_content: int = 200,
                     max_chars_per_content: int = 2_000_000) -> tuple[int, int]:
    """Atomic, bounded backfill with source completeness receipt.

    Callers must commit or roll back the *entire version* as one transaction;
    a failed extract must not leave partial ei_* units or a success receipt.
    """
    if max_rows < 1 or not 1 <= max_units_per_content <= 10_000 or max_chars_per_content < 1:
        raise ValueError("max_rows must be positive and max_units_per_content must be 1..10000")
    if not dry_run:
        ensure_schema_ready(cur)
    digest = hashlib.sha256()
    last_content_id = 0
    chunks = 0
    units = 0
    while chunks < max_rows:
        size = min(200, max_rows - chunks)
        cur.execute(
            """
            SELECT id, section_id, page_number, content
            FROM public.contents
            WHERE document_id = %s AND id > %s
            ORDER BY id LIMIT %s
            """, (document_id, last_content_id, size)
        )
        rows = cur.fetchall()
        if not rows:
            break
        batch: list[RequirementUnit] = []
        for content_id, section_id, page, body in rows:
            last_content_id = int(content_id)
            source_text = str(body or "")
            # Length-prefixed bytes to avoid ambiguous concatenation. This
            # fingerprint includes zero-unit rows, unlike index row counts.
            for part in (str(content_id), str(section_id), str(page), source_text):
                raw = part.encode('utf-8', 'surrogatepass')
                digest.update(len(raw).to_bytes(8, 'big'))
                digest.update(raw)
            atoms = extract_requirement_units(
                document_id=document_id, content_id=int(content_id),
                section_id=str(section_id), page_number=page,
                text=source_text, max_units=max_units_per_content,
                max_source_chars=max_chars_per_content,
            )
            batch.extend(atoms)
            units += len(atoms)
        if not dry_run and batch:
            save_units(cur, specification_id=specification_id, version_id=version_id, units=tuple(batch))
        chunks += len(rows)
    if chunks == max_rows:
        cur.execute("SELECT 1 FROM public.contents WHERE document_id=%s AND id>%s LIMIT 1", (document_id, last_content_id))
        if cur.fetchone() is not None:
            raise ValueError("max_rows reached before finishing document: refusing incomplete backfill")
    if not dry_run:
        cur.execute("""SELECT COUNT(*), COALESCE(MAX(id),0)
                       FROM public.contents WHERE document_id=%s""", (document_id,))
        actual_chunks, actual_max = cur.fetchone()
        if int(actual_chunks) != chunks or int(actual_max) != last_content_id:
            raise ValueError("Source document changed during backfill; refusing incomplete receipt")
        cur.execute("SELECT COUNT(*) FROM public.ei_requirement_units WHERE version_id=%s", (version_id,))
        indexed_units = int(cur.fetchone()[0])
        if indexed_units != units:
            raise ValueError("Indexed requirement count differs from source extraction; refusing receipt")
        # A prior receipt with a changed source digest cannot be silently
        # overwritten: reviewed relations and prior version maps may depend
        # on old source spans. Require explicit migration/review.
        cur.execute("""
            INSERT INTO public.ei_version_backfill_receipts (
                version_id, document_id, extractor_version,
                source_chunk_count, source_unit_count, max_content_id, source_digest
            ) VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(version_id) DO UPDATE SET
                completed_at=NOW()
            WHERE ei_version_backfill_receipts.document_id=EXCLUDED.document_id
              AND ei_version_backfill_receipts.extractor_version=EXCLUDED.extractor_version
              AND ei_version_backfill_receipts.source_digest=EXCLUDED.source_digest
              AND ei_version_backfill_receipts.source_chunk_count=EXCLUDED.source_chunk_count
              AND ei_version_backfill_receipts.source_unit_count=EXCLUDED.source_unit_count
              AND ei_version_backfill_receipts.max_content_id=EXCLUDED.max_content_id
            RETURNING version_id
        """, (version_id, document_id, EXTRACTOR_VERSION, chunks, units,
              last_content_id, digest.hexdigest()))
        if cur.fetchone() is None:
            raise ValueError("Source version changed after an earlier backfill; refusing unsafe overwrite")
    return chunks, units


def require_complete_backfill(cur: Any, version_id: int) -> None:
    """Validate a complete receipt, including source rows with zero units.

    Snapshot source contents are assumed immutable. Running the backfill again
    also verifies a strong digest and refuses silent source modifications.
    """
    cur.execute("""
        SELECT r.document_id, r.extractor_version, r.source_chunk_count,
               r.source_unit_count, r.max_content_id,
               sv.document_id,
               (SELECT COUNT(*) FROM public.contents c WHERE c.document_id=sv.document_id),
               (SELECT COALESCE(MAX(c.id),0) FROM public.contents c WHERE c.document_id=sv.document_id),
               (SELECT COUNT(*) FROM public.ei_requirement_units u WHERE u.version_id=sv.id)
        FROM public.specification_versions sv
        LEFT JOIN public.ei_version_backfill_receipts r ON r.version_id=sv.id
        WHERE sv.id=%s
    """, (version_id,))
    row = cur.fetchone()
    if not row or row[0] is None:
        raise ValueError(f"Version {version_id} lacks a complete backfill receipt")
    receipt_doc, extractor, chunks, unit_count, max_content, doc, current_chunks, current_max, current_units = row
    if (str(receipt_doc) != str(doc)
        or int(chunks) != int(current_chunks) or int(max_content) != int(current_max)
        or int(unit_count) != int(current_units)):
        raise ValueError(f"Version {version_id} backfill receipt is stale or incomplete")
    if extractor == EXTRACTOR_VERSION:
        return
    if extractor == "requirement-units-v1":
        require_stage1_v2_schema(cur)
        cur.execute("""SELECT extractor_version, source_digest, source_unit_count
                       FROM public.ei_stage1_enrichment_receipts WHERE version_id=%s""",
                    (version_id,))
        enriched = cur.fetchone()
        # Both receipts must refer to the *same* immutable source snapshot.
        cur.execute("SELECT source_digest FROM public.ei_version_backfill_receipts WHERE version_id=%s", (version_id,))
        receipt = cur.fetchone()
        if (not enriched or not receipt or enriched[0] != EXTRACTOR_VERSION
            or enriched[1] != receipt[0] or int(enriched[2]) != int(unit_count)):
            raise ValueError(f"Version {version_id} requires verified v2 legacy enrichment")
        return
    raise ValueError(f"Version {version_id} has unsupported Stage-1 extractor {extractor!r}")


def load_version_units(cur: Any, version_id: int, *, max_units: int = 40_000) -> tuple[RequirementUnit, ...]:
    from .requirements import RequirementSignals
    require_stage1_v2_schema(cur)
    cur.execute(
        """
        SELECT u.unit_key, u.document_id, u.content_id, u.section_id, u.page_number,
               u.start_offset, u.end_offset, u.raw_text, u.text_hash,
               COALESCE(e.derived_unit_type,u.unit_type), COALESCE(e.signals,u.signals)
        FROM public.ei_requirement_units u
        LEFT JOIN public.ei_requirement_enrichments e ON e.unit_id=u.id
        WHERE u.version_id = %s
        ORDER BY u.id LIMIT %s
        """, (version_id, max_units)
    )
    loaded = []
    for key, doc, content, section, page, start, end, body, digest, kind, signals in cur.fetchall():
        if isinstance(signals, str):
            signals = json.loads(signals)
        flags = dict(signals or {})
        loaded.append(RequirementUnit(
            unit_key=str(key).strip(), document_id=str(doc), content_id=int(content), section_id=str(section),
            page_number=page, start_offset=int(start), end_offset=int(end), text=str(body),
            text_hash=str(digest).strip(), unit_type=str(kind),
            signals=RequirementSignals(
                normative=bool(flags.get('normative')), conditional=bool(flags.get('conditional')),
                exception=bool(flags.get('exception')), timing=tuple(flags.get('timing') or ()),
                reference_cue=bool(flags.get('reference_cue')),
                identifiers=tuple(flags.get('identifiers') or ()),
                modality=str(flags.get('modality') or 'NONE'),
                structural_kind=str(flags.get('structural_kind') or 'PROSE'),
                quantity_cues=tuple(flags.get('quantity_cues') or ()),
                cue_spans=tuple(tuple(cue) for cue in (flags.get('cue_spans') or ())),
                table_cells=tuple(tuple(cell) for cell in (flags.get('table_cells') or ())),
            ),
        ))
    return tuple(loaded)


def backfill_version_mappings(cur: Any, *, old_version_id: int, new_version_id: int,
                              max_units: int = 10_000) -> dict[str, int]:
    ensure_schema_ready(cur)
    if old_version_id == new_version_id:
        raise ValueError("Cannot align a version to itself")
    cur.execute("SELECT id, specification_id FROM public.specification_versions WHERE id IN (%s,%s)",
                (old_version_id, new_version_id))
    versions = cur.fetchall()
    if len(versions) != 2 or len({r[1] for r in versions}) != 1:
        raise ValueError("Version pair must belong to the same specification")
    # Empty or whitespace-only source rows legitimately yield zero units.
    # Receipts validate *all* source chunks, not only indexed content IDs.
    require_complete_backfill(cur, old_version_id)
    require_complete_backfill(cur, new_version_id)
    old = load_version_units(cur, old_version_id, max_units=max_units+1)
    new = load_version_units(cur, new_version_id, max_units=max_units+1)
    if len(old) > max_units or len(new) > max_units:
        raise ValueError("Version exceeds max_units; refusing incomplete mapping")
    aligned = align_requirement_versions(old, new)
    stats: dict[str, int] = defaultdict(int)
    for item in aligned:
        cur.execute(
            """
            INSERT INTO public.ei_version_mappings (
                source_version_id,target_version_id,old_unit_id,new_unit_id,
                old_unit_key,new_unit_key,change_type,similarity,review_status
            ) VALUES (
                %s,%s,
                (SELECT id FROM public.ei_requirement_units WHERE unit_key=%s),
                (SELECT id FROM public.ei_requirement_units WHERE unit_key=%s),
                %s,%s,%s,%s,%s
            ) ON CONFLICT (source_version_id,target_version_id,old_unit_key,new_unit_key,algorithm_version)
            DO NOTHING
            """,
            (old_version_id,new_version_id,item.old_key,item.new_key,
             item.old_key or '',item.new_key or '',item.change_type,item.similarity,item.review_status)
        )
        stats[item.change_type] += 1
    return dict(stats)


def backfill_content_relations(cur: Any, *, source_specification_id: int, source_version_id: int,
                               source_spec_code: str, max_units: int = 10_000) -> dict[str, int]:
    """Produce only unambiguous, explicit source-to-target paragraph proposals.

    If a reference names a target *section* containing multiple paragraphs,
    store a reviewable section-level anchor, never pretend one arbitrary
    paragraph is the referenced requirement.
    """
    ensure_schema_ready(cur)
    require_complete_backfill(cur, source_version_id)
    source_units = load_version_units(cur, source_version_id, max_units=max_units + 1)
    if len(source_units) > max_units:
        raise ValueError("Source specification exceeds max_units; no partial relation generation")
    cur.execute(
        """SELECT id, spec_code, current_version_id FROM public.specifications
           WHERE id <> %s AND current_version_id IS NOT NULL AND spec_code IS NOT NULL
             AND project_code = (SELECT project_code FROM public.specifications WHERE id=%s)""",
        (source_specification_id, source_specification_id),
    )
    catalog: dict[str, list[tuple[int,int]]] = defaultdict(list)
    for sid, code, vid in cur.fetchall():
        catalog[str(code).upper()].append((int(sid), int(vid)))
    from .relations import _SPEC_REFERENCE, _SECTION_REFERENCE
    anchors: dict[tuple[str, str], dict[str, RequirementUnit]] = defaultdict(dict)
    ambiguous_catalog = 0
    for unit in source_units:
        refs = list(_SPEC_REFERENCE.finditer(unit.text))
        for i, match in enumerate(refs):
            code = match.group('code').upper()
            segment_end = refs[i + 1].start() if i + 1 < len(refs) else len(unit.text)
            section = _SECTION_REFERENCE.search(unit.text[match.end():segment_end])
            if not section or code not in catalog:
                continue
            if len(catalog[code]) != 1:
                ambiguous_catalog += 1
                continue
            anchors[(code, section.group(1))][unit.unit_key] = unit

    generated = 0
    unresolved = 0
    ambiguous = 0
    not_indexed = 0
    for (target_code, section_id), source_by_key in sorted(anchors.items()):
        _, target_vid = catalog[target_code][0]
        sources = tuple(source_by_key.values())
        try:
            require_complete_backfill(cur, target_vid)
            target_ready = True
        except ValueError:
            target_ready = False

        target_rows: list[tuple] = []
        if target_ready:
            cur.execute(
                """SELECT u.unit_key, u.document_id, u.content_id, u.section_id, u.page_number,
                          u.start_offset, u.end_offset, u.raw_text, u.text_hash,
                          COALESCE(e.derived_unit_type,u.unit_type), COALESCE(e.signals,u.signals)
                   FROM public.ei_requirement_units u
                   LEFT JOIN public.ei_requirement_enrichments e ON e.unit_id=u.id
                   WHERE u.version_id=%s AND u.section_id=%s ORDER BY u.id LIMIT 2""",
                (target_vid, section_id),
            )
            target_rows = list(cur.fetchall())

        if not target_ready:
            reason = 'TARGET_VERSION_NOT_INDEXED'
        elif not target_rows:
            reason = 'TARGET_SECTION_MISSING'
        elif len(target_rows) > 1:
            reason = 'AMBIGUOUS_TARGET_SECTION'
        else:
            reason = None

        if reason is not None:
            if reason == 'AMBIGUOUS_TARGET_SECTION':
                ambiguous += len(sources)
            elif reason == 'TARGET_VERSION_NOT_INDEXED':
                not_indexed += len(sources)
            unresolved += len(sources)
            for source in sources:
                cur.execute("""
                    INSERT INTO public.ei_unresolved_relation_anchors (
                        source_unit_id, target_version_id, target_section_id, reason, candidate_count
                    ) VALUES (
                        (SELECT id FROM public.ei_requirement_units WHERE unit_key=%s), %s, %s, %s, %s
                    )
                    ON CONFLICT(source_unit_id,target_version_id,target_section_id)
                    DO UPDATE SET reason=EXCLUDED.reason, candidate_count=EXCLUDED.candidate_count,
                                  updated_at=NOW()
                    WHERE public.ei_unresolved_relation_anchors.reason<>'RESOLVED'
                """, (source.unit_key, target_vid, section_id, reason,
                      min(len(target_rows),2)))
            continue

        from .requirements import RequirementSignals
        key, doc, content, section, page, start, end, body, digest, kind, flags = target_rows[0]
        if isinstance(flags, str):
            flags = json.loads(flags)
        flags = dict(flags or {})
        target_unit = RequirementUnit(
            unit_key=str(key).strip(), document_id=str(doc), content_id=int(content),
            section_id=str(section), page_number=page, start_offset=int(start),
            end_offset=int(end), text=str(body), text_hash=str(digest).strip(), unit_type=str(kind),
            signals=RequirementSignals(
                normative=bool(flags.get('normative')), conditional=bool(flags.get('conditional')),
                exception=bool(flags.get('exception')), timing=tuple(flags.get('timing') or ()),
                reference_cue=bool(flags.get('reference_cue')),
                identifiers=tuple(flags.get('identifiers') or ()),
                modality=str(flags.get('modality') or 'NONE'),
                structural_kind=str(flags.get('structural_kind') or 'PROSE'),
                quantity_cues=tuple(flags.get('quantity_cues') or ()),
                cue_spans=tuple(tuple(cue) for cue in (flags.get('cue_spans') or ())),
                table_cells=tuple(tuple(cell) for cell in (flags.get('table_cells') or ())),
            ),
        )
        proposals = link_explicit_references(
            source_spec_code=source_spec_code, source_units=sources,
            target_spec_code=target_code, target_units=(target_unit,),
            max_candidates_per_source=1,
        )
        for proposal in proposals:
            cur.execute("""
                INSERT INTO public.ei_content_relations (
                    candidate_key,source_unit_id,target_unit_id,relation_type,
                    extraction_method,score,status,rationale
                ) VALUES (
                    %s,
                    (SELECT id FROM public.ei_requirement_units WHERE unit_key=%s),
                    (SELECT id FROM public.ei_requirement_units WHERE unit_key=%s),
                    %s,'EXPLICIT_SPEC_SECTION',%s,'REVIEW_REQUIRED',%s
                ) ON CONFLICT(candidate_key) DO NOTHING
            """, (proposal.candidate_key,proposal.source_unit_key,proposal.target_unit_key,
                  proposal.relation_type, proposal.score,proposal.reason))
            generated += 1
            cur.execute("""
                UPDATE public.ei_unresolved_relation_anchors
                SET reason='RESOLVED',updated_at=NOW()
                WHERE source_unit_id=(SELECT id FROM public.ei_requirement_units WHERE unit_key=%s)
                  AND target_version_id=%s AND target_section_id=%s
            """, (proposal.source_unit_key,target_vid,section_id))
    return {
        'candidate_edges': generated, 'unresolved_anchors': unresolved,
        'ambiguous_sections': ambiguous, 'target_versions_not_indexed': not_indexed,
        'ambiguous_target_catalog': ambiguous_catalog,
        'explicit_anchors': sum(len(value) for value in anchors.values()),
    }


def enrich_legacy_stage1_version(
    cur: Any, *, version_id: int, document_id: str,
    max_rows: int = 100_000, max_units_per_content: int = 200,
    max_chars_per_content: int = 2_000_000, dry_run: bool = True,
) -> tuple[int, int]:
    """Upgrade v1 source-unit *signals* without editing reviewed units.

    Uses separate v2 enrichment rows, never alters existing ei_requirement_units,
    any APPROVED review, old v1 receipt, mappings or relation decisions.
    All statements must be committed as ONE caller transaction for this version.
    """
    if max_rows < 1 or max_units_per_content < 1 or max_chars_per_content < 1:
        raise ValueError("Invalid legacy enrichment resource bounds")
    ensure_schema_ready(cur)
    require_stage1_v2_schema(cur)
    cur.execute("""SELECT document_id,extractor_version,source_chunk_count,
                          source_unit_count,max_content_id,source_digest
                   FROM public.ei_version_backfill_receipts WHERE version_id=%s""", (version_id,))
    receipt = cur.fetchone()
    if not receipt or str(receipt[0]) != str(document_id) or receipt[1] != "requirement-units-v1":
        raise ValueError("Expected a complete legacy requirement-units-v1 receipt for this document")
    expected_chunks, expected_units, expected_max, expected_digest = (
        int(receipt[2]), int(receipt[3]), int(receipt[4]), str(receipt[5])
    )
    digest = hashlib.sha256()
    last_content_id = 0
    chunks = units = 0
    while chunks < max_rows:
        cur.execute("""SELECT id,section_id,page_number,content FROM public.contents
                       WHERE document_id=%s AND id>%s ORDER BY id LIMIT %s""",
                    (document_id, last_content_id, min(64, max_rows - chunks)))
        rows = cur.fetchall()
        if not rows:
            break
        for content_id, section_id, page, body in rows:
            last_content_id = int(content_id)
            source = str(body or "")
            for value in (str(content_id), str(section_id), str(page), source):
                raw = value.encode("utf-8", "surrogatepass")
                digest.update(len(raw).to_bytes(8, "big"))
                digest.update(raw)
            extracted = extract_requirement_units(
                document_id=document_id, content_id=int(content_id),
                section_id=str(section_id), page_number=page, text=source,
                max_units=max_units_per_content,
                max_source_chars=max_chars_per_content,
            )
            cur.execute("""SELECT id,unit_key,raw_text,text_hash,start_offset,end_offset
                           FROM public.ei_requirement_units
                           WHERE version_id=%s AND content_id=%s""",
                        (version_id, content_id))
            old_by_key = {str(row[1]).strip(): row for row in cur.fetchall()}
            if len(old_by_key) != len(extracted):
                raise ValueError("Legacy unit count differs from v2 extraction; review manually")
            for unit in extracted:
                old = old_by_key.get(unit.unit_key)
                if (old is None or str(old[2]) != unit.text or
                    str(old[3]).strip() != unit.text_hash or
                    int(old[4]) != unit.start_offset or int(old[5]) != unit.end_offset):
                    raise ValueError("Legacy source span differs from v2; refusing mutation")
                if not dry_run:
                    cur.execute("""INSERT INTO public.ei_requirement_enrichments
                               (unit_id,extractor_version,derived_unit_type,source_text_hash,signals)
                               VALUES (%s,%s,%s,%s,%s::jsonb)
                               ON CONFLICT(unit_id) DO UPDATE SET
                                   extractor_version=EXCLUDED.extractor_version,
                                   derived_unit_type=EXCLUDED.derived_unit_type,
                                   source_text_hash=EXCLUDED.source_text_hash,
                                   signals=EXCLUDED.signals, enriched_at=NOW()
                               WHERE ei_requirement_enrichments.source_text_hash=EXCLUDED.source_text_hash
                               RETURNING unit_id""",
                                (old[0], EXTRACTOR_VERSION, unit.unit_type, unit.text_hash, _signals(unit)))
                    if cur.fetchone() is None:
                        raise ValueError("Existing enrichment hash mismatch; refusing silent overwrite")
            units += len(extracted)
        chunks += len(rows)
    if chunks == max_rows:
        cur.execute("SELECT 1 FROM public.contents WHERE document_id=%s AND id>%s LIMIT 1",
                    (document_id, last_content_id))
        if cur.fetchone() is not None:
            raise ValueError("Legacy source exceeds max_rows; refusing partial enrichment")
    if (chunks != expected_chunks or units != expected_units or
        last_content_id != expected_max or digest.hexdigest() != expected_digest):
        raise ValueError("Legacy source snapshot changed; refusing enrichment")
    if not dry_run:
        cur.execute("""INSERT INTO public.ei_stage1_enrichment_receipts
                       (version_id,extractor_version,source_digest,source_unit_count)
                       VALUES (%s,%s,%s,%s)
                       ON CONFLICT(version_id) DO UPDATE SET completed_at=NOW()
                       WHERE ei_stage1_enrichment_receipts.extractor_version=EXCLUDED.extractor_version
                         AND ei_stage1_enrichment_receipts.source_digest=EXCLUDED.source_digest
                         AND ei_stage1_enrichment_receipts.source_unit_count=EXCLUDED.source_unit_count
                       RETURNING version_id""",
                    (version_id, EXTRACTOR_VERSION, digest.hexdigest(), units))
        if cur.fetchone() is None:
            raise ValueError("Existing enrichment receipt differs; refusing unsafe overwrite")
    return chunks, units
