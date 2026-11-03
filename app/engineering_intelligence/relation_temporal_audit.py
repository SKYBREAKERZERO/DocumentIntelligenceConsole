"""v1.42: evidence-bounded, read-only historical relation continuity audit.

This inspects a previously CONFIRMED edge against a specified later SPEC_BODY
version and an existing v7 requirement mapping.  It does *not* carry human
approval forward or establish dependency, compliance, or engineering impact.
"""
from __future__ import annotations

from typing import Any

from app.engineering_intelligence.deterministic_read_models import verify_requirement_span
from app.storage.primary_document_policy import PrimarySpecificationDocumentPolicy as Primary


class TemporalAuditError(ValueError):
    """Invalid caller input (not an engineering conclusion)."""


def _span(row: dict[str, Any], prefix: str) -> bool:
    return verify_requirement_span({
        f'{prefix}_text': row.get(f'{prefix}_text'),
        f'{prefix}_chunk': row.get(f'{prefix}_chunk'),
        f'{prefix}_start': row.get(f'{prefix}_start'),
        f'{prefix}_end': row.get(f'{prefix}_end'),
        f'{prefix}_hash': row.get(f'{prefix}_hash'),
    }, prefix)


def assess_temporal_relation(cur: Any, *, project: str, relation_id: int,
                             new_target_version_id: int) -> dict[str, Any]:
    """Run only inside caller's REPEATABLE READ READ ONLY transaction.

    Every result is non-authoritative for temporal validity, even when the
    literal target requirement text is proven identical.  Data that cannot be
    verified is never treated as an absent/removed requirement.
    """
    import re

    if (not re.fullmatch(r'[A-Za-z0-9_-]{2,40}', project)
            or project.upper() in {'QABUG', 'RAG_QABUG'}
            or isinstance(relation_id, bool) or not isinstance(relation_id, int) or relation_id < 1
            or isinstance(new_target_version_id, bool)
            or not isinstance(new_target_version_id, int) or new_target_version_id < 1):
        raise TemporalAuditError('Engineering project, positive relation and version IDs required')

    result: dict[str, Any] = {
        'schema': 'dic-temporal-relation-audit-v1',
        'project': project, 'relation_id': relation_id,
        'new_target_version_id': new_target_version_id,
        'status': 'INSUFFICIENT_EVIDENCE', 'issues': [],
        'relation_validity': 'UNDETERMINED',
        'human_revalidation_required': True,
        'authority': 'READ_ONLY_EVIDENCE_AUDIT_NOT_APPROVAL',
        'production_acceptance': 'DATABASE_SNAPSHOT_ONLY',
    }

    def stop(reason: str) -> dict[str, Any]:
        result['issues'].append(reason)
        return result

    source_primary = Primary.sql_primary_predicate('sd')
    old_primary = Primary.sql_primary_predicate('od')
    cur.execute(f"""
        SELECT r.id, r.status AS relation_status, r.relation_type,
               su.id AS source_unit_id, su.specification_id AS source_spec_id,
               ss.spec_code AS source_code, ss.project_code AS source_project,
               su.version_id AS source_version_id, sv.version_label AS source_version,
               sv.branch_name AS source_branch,
               su.document_id AS source_document, sv.document_id AS source_version_document,
               sd.file_name AS source_file, sd.metadata AS source_metadata,
               su.review_status AS source_review_status,
               su.raw_text AS source_text, su.text_hash AS source_hash,
               su.start_offset AS source_start, su.end_offset AS source_end,
               sc.content AS source_chunk, su.section_id AS source_section,
               tu.id AS old_unit_id, tu.specification_id AS target_spec_id,
               ts.spec_code AS target_code, ts.project_code AS target_project,
               tu.version_id AS old_version_id, ov.version_label AS old_version,
               ov.version_seq AS old_version_seq, ov.branch_name AS old_branch,
               tu.document_id AS old_document, ov.document_id AS old_version_document,
               od.file_name AS old_file, od.metadata AS old_metadata,
               tu.review_status AS old_review_status,
               tu.raw_text AS old_text, tu.text_hash AS old_hash,
               tu.start_offset AS old_start, tu.end_offset AS old_end,
               oc.content AS old_chunk, tu.section_id AS old_section
        FROM public.ei_content_relations r
        JOIN public.ei_requirement_units su ON su.id=r.source_unit_id
        JOIN public.ei_requirement_units tu ON tu.id=r.target_unit_id
        JOIN public.specifications ss ON ss.id=su.specification_id
        JOIN public.specifications ts ON ts.id=tu.specification_id
        JOIN public.specification_versions sv ON sv.id=su.version_id AND sv.specification_id=ss.id
        JOIN public.specification_versions ov ON ov.id=tu.version_id AND ov.specification_id=ts.id
        JOIN public.documents sd ON sd.document_id=su.document_id
        JOIN public.documents od ON od.document_id=tu.document_id
        JOIN public.contents sc ON sc.id=su.content_id AND sc.document_id=su.document_id
        JOIN public.contents oc ON oc.id=tu.content_id AND oc.document_id=tu.document_id
        WHERE r.id=%s AND ss.project_code=%s AND ts.project_code=%s
          AND {source_primary} AND {old_primary}
        LIMIT 2
    """, (relation_id, project, project))
    old_rows = [dict(r) for r in cur.fetchall()]
    if len(old_rows) != 1:
        return stop('CONFIRMED_BODY_RELATION_NOT_UNIQUELY_FOUND')
    old = old_rows[0]
    if old['relation_status'] != 'CONFIRMED':
        return stop('RELATION_NOT_CONFIRMED')
    if (old['source_branch'] != 'MAIN' or old['old_branch'] != 'MAIN'
            or old['source_document'] != old['source_version_document']
            or old['old_document'] != old['old_version_document']
            or old['source_review_status'] == 'REJECTED'
            or old['old_review_status'] == 'REJECTED'
            or not Primary.is_primary(file_name=old['source_file'], metadata=old['source_metadata'])
            or not Primary.is_primary(file_name=old['old_file'], metadata=old['old_metadata'])
            or not _span(old, 'source') or not _span(old, 'old')):
        return stop('OLD_ENDPOINT_IDENTITY_OR_SOURCE_HASH_INVALID')

    cur.execute(f"""
        SELECT nv.id, nv.specification_id, nv.document_id, nv.branch_name,
               nv.version_label, nv.version_seq, nd.file_name, nd.metadata
        FROM public.specification_versions nv
        JOIN public.documents nd ON nd.document_id=nv.document_id
        WHERE nv.id=%s AND nv.specification_id=%s
          AND {Primary.sql_primary_predicate('nd')}
        LIMIT 2
    """, (new_target_version_id, old['target_spec_id']))
    versions = [dict(v) for v in cur.fetchall()]
    if len(versions) != 1:
        return stop('NEW_SPEC_BODY_VERSION_NOT_FOUND')
    new = versions[0]
    if (new['branch_name'] != 'MAIN'
            or int(new['version_seq']) <= int(old['old_version_seq'])
            or not Primary.is_primary(file_name=new['file_name'], metadata=new['metadata'])):
        return stop('NEW_VERSION_NOT_LATER_MAIN_SPEC_BODY')
    # Absence/removal in a partial requirement index is not evidence. Check
    # receipts for all versions in a single bounded snapshot query. This is a
    # count/identity gate, NOT the full v1.41 source-digest integrity audit.
    required_versions = {int(old['source_version_id']), int(old['old_version_id']),
                         new_target_version_id}
    cur.execute('''
        SELECT v.id, v.document_id, r.document_id AS receipt_document,
               r.source_chunk_count, r.source_unit_count, r.max_content_id,
               r.source_digest,
               (SELECT COUNT(*) FROM public.contents c WHERE c.document_id=v.document_id)
                 AS actual_chunks,
               (SELECT COALESCE(MAX(c.id),0) FROM public.contents c WHERE c.document_id=v.document_id)
                 AS actual_max_content,
               (SELECT COUNT(*) FROM public.ei_requirement_units u WHERE u.version_id=v.id)
                 AS actual_units
        FROM public.specification_versions v
        LEFT JOIN public.ei_version_backfill_receipts r ON r.version_id=v.id
        WHERE v.id=ANY(%s::BIGINT[])
        ORDER BY v.id LIMIT 4
    ''', (sorted(required_versions),))
    receipt_rows = [dict(x) for x in cur.fetchall()]
    if {int(x['id']) for x in receipt_rows} != required_versions:
        return stop('VERSION_BACKFILL_RECEIPT_MISSING_OR_INCOMPLETE')
    for receipt in receipt_rows:
        if (receipt['receipt_document'] != receipt['document_id']
                or not isinstance(receipt.get('source_digest'), str)
                or len(receipt['source_digest'].strip()) != 64
                or any(receipt.get(field) is None for field in (
                    'source_chunk_count', 'source_unit_count', 'max_content_id'))
                or int(receipt['source_chunk_count']) != int(receipt['actual_chunks'])
                or int(receipt['source_unit_count']) != int(receipt['actual_units'])
                or int(receipt['max_content_id']) != int(receipt['actual_max_content'])):
            return stop('VERSION_BACKFILL_RECEIPT_MISSING_OR_INCOMPLETE')
    result['receipt_identity_and_count_check'] = 'PASS'
    result['full_source_digest_check'] = 'NOT_PERFORMED'

    result.update({
        'source_spec_code': old['source_code'], 'source_version': old['source_version'],
        'source_unit_id': old['source_unit_id'], 'source_section': old['source_section'],
        'source_document': old['source_document'], 'source_hash': str(old['source_hash']).strip(),
        'target_spec_code': old['target_code'], 'old_target_version': old['old_version'],
        'new_target_version': new['version_label'], 'old_unit_id': old['old_unit_id'],
        'old_target_section': old['old_section'], 'old_target_hash': str(old['old_hash']).strip(),
    })

    # A READY persisted comparison establishes which version pair was compared;
    # it does NOT establish that the source specification is effective at either date.
    cur.execute("""
        SELECT id, is_adjacent, algorithm_version
        FROM public.specification_change_sets
        WHERE specification_id=%s AND from_version_id=%s AND to_version_id=%s
          AND status='READY' AND branch_name='MAIN'
        ORDER BY updated_at DESC, id DESC LIMIT 2
    """, (old['target_spec_id'], old['old_version_id'], new_target_version_id))
    change_sets = [dict(row) for row in cur.fetchall()]
    if len(change_sets) != 1:
        return stop('READY_VERSION_PAIR_NOT_UNIQUE_OR_MISSING')
    result['change_set_id'] = change_sets[0]['id']
    result['comparison_is_adjacent'] = bool(change_sets[0]['is_adjacent'])
    result['source_version_alignment'] = 'NOT_ESTABLISHED'

    cur.execute("""
        SELECT vm.id, vm.change_type, vm.review_status, vm.old_unit_id, vm.new_unit_id,
               nu.specification_id AS new_spec_id, nu.version_id AS new_version_id,
               nu.document_id AS new_document, nu.review_status AS new_review_status,
               nu.section_id AS new_section, nu.raw_text AS new_text,
               nu.text_hash AS new_hash, nu.start_offset AS new_start,
               nu.end_offset AS new_end, nc.content AS new_chunk
        FROM public.ei_version_mappings vm
        LEFT JOIN public.ei_requirement_units nu ON nu.id=vm.new_unit_id
        LEFT JOIN public.contents nc ON nc.id=nu.content_id AND nc.document_id=nu.document_id
        WHERE vm.source_version_id=%s AND vm.target_version_id=%s AND vm.old_unit_id=%s
        ORDER BY vm.id LIMIT 3
    """, (old['old_version_id'], new_target_version_id, old['old_unit_id']))
    mappings = [dict(x) for x in cur.fetchall()]
    if len(mappings) != 1:
        return stop('UNMAPPED_OR_AMBIGUOUS_OLD_REQUIREMENT')
    mapping = mappings[0]
    result['mapping_id'] = mapping['id']
    result['mapping_review_status'] = mapping['review_status']
    result['mapping_change_type'] = mapping['change_type']
    if mapping['review_status'] not in {'DETERMINISTIC', 'APPROVED'}:
        return stop('REQUIREMENT_MAPPING_REVIEW_REQUIRED')
    if mapping['change_type'] not in {'UNCHANGED', 'MOVED', 'MODIFIED', 'REMOVED'}:
        return stop('INCONSISTENT_MAPPING_KIND')
    if mapping['change_type'] == 'REMOVED':
        if mapping['new_unit_id'] is not None:
            return stop('INCONSISTENT_REMOVAL_ENDPOINT')
        result['status'] = 'TARGET_REMOVED_REVIEW_REQUIRED'
        return result
    if (mapping['new_unit_id'] is None or mapping['new_review_status'] == 'REJECTED'
            or mapping['new_spec_id'] != old['target_spec_id']
            or mapping['new_version_id'] != new_target_version_id
            or mapping['new_document'] != new['document_id']
            or not _span(mapping, 'new')):
        return stop('NEW_REQUIREMENT_ENDPOINT_OR_HASH_INVALID')
    result['new_unit_id'] = mapping['new_unit_id']
    result['new_target_section'] = mapping['new_section']
    result['new_target_hash'] = str(mapping['new_hash']).strip()
    same = (old['old_text'] == mapping['new_text']
            and str(old['old_hash']).strip() == str(mapping['new_hash']).strip())
    if mapping['change_type'] == 'UNCHANGED':
        if not same:
            return stop('UNCHANGED_MAPPING_HAS_CHANGED_TEXT')
        result['status'] = 'TARGET_TEXT_CONTINUITY_VERIFIED'
    elif mapping['change_type'] == 'MOVED':
        if not same:
            return stop('MOVED_MAPPING_HAS_CHANGED_TEXT')
        result['status'] = 'TARGET_MOVED_REVIEW_REQUIRED'
    else:
        if same:
            return stop('MODIFIED_MAPPING_HAS_IDENTICAL_TEXT')
        result['status'] = 'TARGET_CHANGED_REVIEW_REQUIRED'
    return result
