"""Explicit, bounded offline BUG -> historical SPEC requirement candidate backfill.

DRY RUN by default. Database credentials come from environment DSNs; no LLM,
no Qdrant, no network except the operator-configured local PostgreSQL servers.
Different databases intentionally have different DB roles/passwords.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.engineering_intelligence.bug_spec_candidates import (
    SCORER_VERSION, SpecUnitSnapshot, extract_bug_observations,
    rank_candidates, sha256_normalized, source_digest,
)
from app.storage.primary_document_policy import PrimarySpecificationDocumentPolicy

ISSUE_SQL = """
 SELECT r.id, r.record_type, r.issue_key, r.summary, r.description,
        r.affected_version, r.software_version, r.model, r.destination,
        b.phenomenon, b.repro_steps, b.expected_result, b.actual_result,
        m.id AS mapping_id, m.issue_version_field, m.software_version AS mapped_software_version,
        m.spec_scope, m.spec_code, m.spec_version_id, m.spec_version_label, m.spec_document_id
 FROM public.qabug_records r
 JOIN public.bug_details b ON b.record_id=r.id
 JOIN public.qabug_software_spec_versions m
   ON m.status='APPROVED'
   AND m.model=coalesce(r.model,'') AND m.destination=coalesce(r.destination,'')
   AND ((m.issue_version_field='affected_version' AND m.software_version=r.affected_version)
     OR (m.issue_version_field='software_version' AND m.software_version=r.software_version))
 WHERE r.issue_key=%s AND r.record_type='BUG' AND r.is_current AND r.is_queryable
 ORDER BY m.id
"""


def load_spec_units(spec_conn, mapping: dict, *, max_units: int) -> list[SpecUnitSnapshot]:
    """Verify historical (not current-only) version identity and every offset."""
    primary = PrimarySpecificationDocumentPolicy.sql_primary_predicate("d")
    with spec_conn.cursor() as cur:
        cur.execute(f"""
        SELECT u.id AS unit_id, u.content_id, u.section_id, u.page_number,
               u.start_offset, u.end_offset, u.raw_text, u.text_hash,
               c.content AS chunk_text, d.file_name, d.metadata,
               sv.id AS version_id, sv.version_label, sv.document_id,
               sp.spec_code, sp.project_code
        FROM public.ei_requirement_units u
        JOIN public.specification_versions sv ON sv.id=u.version_id
             AND sv.specification_id=u.specification_id AND sv.document_id=u.document_id
        JOIN public.specifications sp ON sp.id=sv.specification_id
        JOIN public.documents d ON d.document_id=sv.document_id
        JOIN public.contents c ON c.id=u.content_id AND c.document_id=u.document_id
        WHERE sv.id=%s AND sv.version_label=%s AND sv.document_id=%s
          AND sp.spec_code=%s AND sp.project_code=%s
          AND {primary}
        ORDER BY u.id LIMIT %s
        """, (mapping['spec_version_id'], mapping['spec_version_label'],
              mapping['spec_document_id'], mapping['spec_code'],
              mapping['spec_scope'], max_units+1))
        rows = [dict(r) for r in cur.fetchall()]
        if len(rows) > max_units:
            raise RuntimeError("Spec requirement bound exceeded; refusing partial corpus")
        # A valid version may have ZERO units; distinguish absence from a wrong
        # version, a wrong document or a coversheet-only version.
        cur.execute(f"""
        SELECT sv.id FROM public.specification_versions sv
        JOIN public.specifications sp ON sp.id=sv.specification_id
        JOIN public.documents d ON d.document_id=sv.document_id
        WHERE sv.id=%s AND sv.version_label=%s AND sv.document_id=%s
          AND sp.spec_code=%s AND sp.project_code=%s AND {primary}
        """, (mapping['spec_version_id'], mapping['spec_version_label'],
              mapping['spec_document_id'], mapping['spec_code'],mapping['spec_scope']))
        if cur.fetchone() is None:
            raise RuntimeError("Mapped specification version is missing or not SPEC_BODY")
    units = [SpecUnitSnapshot(
        scope=mapping['spec_scope'], spec_code=r['spec_code'], version_id=r['version_id'],
        version_label=r['version_label'], document_id=r['document_id'],
        file_name=r['file_name'], document_metadata=r['metadata'] or {},
        unit_id=r['unit_id'], content_id=r['content_id'], section_id=r['section_id'],
        page_number=r['page_number'], start_offset=r['start_offset'],
        end_offset=r['end_offset'], raw_text=r['raw_text'], text_hash=r['text_hash'],
        chunk_text=r['chunk_text'],
    ) for r in rows]
    if any(not u.verify() for u in units):
        raise RuntimeError("SPEC_BODY/requirement offset/text hash mismatch; refusing candidates")
    return units


def process_issue(qabug_conn, *, issue_key: str, scope_dsns: dict[str, str],
                  top_n: int, max_units: int, apply: bool) -> list[dict]:
    import psycopg
    from psycopg.rows import dict_row

    with qabug_conn.cursor() as cur:
        cur.execute(ISSUE_SQL, (issue_key,))
        rows = [dict(x) for x in cur.fetchall()]
    if not rows:
        raise RuntimeError("No approved exact-version mapping / current BUG record")
    # Never silently select between competing versions of the same spec.
    seen_versions: dict[tuple, tuple] = {}
    for row in rows:
        identity = (row['spec_scope'], row['spec_code'])
        version = (row['spec_version_id'], row['spec_document_id'])
        if identity in seen_versions and seen_versions[identity] != version:
            raise RuntimeError("Ambiguous approved versions for one spec; refuse all candidates")
        seen_versions[identity] = version
    results: list[dict] = []
    for row in rows:
        if row['spec_scope'] not in scope_dsns or not scope_dsns[row['spec_scope']]:
            raise RuntimeError(f"Missing separate read-only spec DSN for {row['spec_scope']}")
        digest = source_digest(row)
        observation = extract_bug_observations(row)
        if not observation['phenomenon']:
            raise RuntimeError("BUG observation missing; refuse unsupported inference")
        with psycopg.connect(scope_dsns[row['spec_scope']], row_factory=dict_row) as spec:
            with spec.cursor() as cur:
                cur.execute("SET TRANSACTION READ ONLY")
            units = load_spec_units(spec, row, max_units=max_units)
            ranked = rank_candidates(observation, units, top_n=top_n)
        result = {'issue_key': issue_key, 'mapping_id': row['mapping_id'],
                  'spec_scope': row['spec_scope'], 'spec_code': row['spec_code'],
                  'spec_version': row['spec_version_label'], 'score_method': SCORER_VERSION,
                  'status': 'UNREVIEWED', 'possible_matches': len(ranked),
                  'candidates': [{'unit_id':u.unit_id, 'section':u.section_id,
                                  'page':u.page_number,'score':score} for u,score in ranked]}
        results.append(result)
        if not apply:
            continue
        # The mapping and BUG fields must not change between read and write.
        # Roll back this transaction on any drift; never write stale evidence.
        with qabug_conn.cursor() as cur:
            cur.execute("SELECT r.id, r.record_type, r.issue_key, r.summary, r.description,"
                        " r.affected_version,r.software_version,r.model,r.destination,"
                        " b.phenomenon,b.repro_steps,b.expected_result,b.actual_result"
                        " FROM public.qabug_records r JOIN public.bug_details b ON b.record_id=r.id"
                        " WHERE r.id=%s AND r.is_current AND r.is_queryable FOR UPDATE OF r",
                        (row['id'],))
            current = cur.fetchone()
            if current is None or source_digest(current) != digest:
                raise RuntimeError("BUG fields changed during indexing")
            cur.execute("SELECT status,issue_version_field,software_version,model,destination,"
                        " spec_scope,spec_code,spec_version_id,spec_version_label,spec_document_id"
                        " FROM public.qabug_software_spec_versions WHERE id=%s FOR UPDATE", (row['mapping_id'],))
            mapping_now = dict(cur.fetchone() or {})
            if (mapping_now.get('status') != 'APPROVED' or
                any(mapping_now.get(k) != row.get(k) for k in (
                    'issue_version_field','spec_scope','spec_code','spec_version_id',
                    'spec_version_label','spec_document_id','model','destination')) or
                mapping_now.get('software_version') != row['mapped_software_version']):
                raise RuntimeError("Approved version map changed during indexing")
            for unit, score in ranked:
                cur.execute("""
                  INSERT INTO public.qabug_spec_candidates (
                    bug_record_id,mapping_id,issue_key,issue_snapshot_hash,phenomenon_json,
                    spec_scope,spec_code,spec_version_id,spec_version_label,
                    spec_document_id,spec_file_name,spec_unit_id,spec_content_id,
                    section_id,page_number,start_offset,end_offset,spec_raw_text,
                    spec_text_hash,rank_score,ranker_version,status)
                  VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'UNREVIEWED')
                  ON CONFLICT (bug_record_id,mapping_id,spec_unit_id)
                  DO UPDATE SET issue_snapshot_hash=excluded.issue_snapshot_hash,
                     phenomenon_json=excluded.phenomenon_json, spec_file_name=excluded.spec_file_name,
                     spec_raw_text=excluded.spec_raw_text,spec_text_hash=excluded.spec_text_hash,
                     spec_scope=excluded.spec_scope, spec_code=excluded.spec_code,
                     spec_version_id=excluded.spec_version_id,
                     spec_version_label=excluded.spec_version_label,
                     spec_document_id=excluded.spec_document_id,
                     spec_content_id=excluded.spec_content_id,section_id=excluded.section_id,
                     page_number=excluded.page_number,start_offset=excluded.start_offset,
                     end_offset=excluded.end_offset,rank_score=excluded.rank_score,
                     ranker_version=excluded.ranker_version,status='UNREVIEWED',updated_at=now()
                  WHERE public.qabug_spec_candidates.status IN ('UNREVIEWED','STALE')
                """, (row['id'],row['mapping_id'],issue_key,digest,
                      json.dumps(observation,ensure_ascii=False),unit.scope,unit.spec_code,
                      unit.version_id,unit.version_label,unit.document_id,unit.file_name,
                      unit.unit_id,unit.content_id,unit.section_id,unit.page_number,
                      unit.start_offset,unit.end_offset,unit.raw_text,unit.text_hash,score,
                      SCORER_VERSION))
            keep_ids = [unit.unit_id for unit,_ in ranked]
            cur.execute("""UPDATE public.qabug_spec_candidates SET status='STALE',updated_at=now()
                         WHERE bug_record_id=%s AND mapping_id=%s AND status='UNREVIEWED'
                           AND NOT (spec_unit_id=ANY(%s::bigint[]))""",
                        (row['id'],row['mapping_id'],keep_ids))
    if apply:
        qabug_conn.commit()
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--issue-key', required=True)
    parser.add_argument('--apply', action='store_true', help='Write candidate rows in QABUG DB (default dry-run)')
    parser.add_argument('--qabug-dsn-env', default='DIC_QABUG_DSN')
    parser.add_argument('--top-n', type=int, default=8)
    parser.add_argument('--max-units', type=int, default=20000)
    args = parser.parse_args(argv)
    if not 1 <= args.top_n <= 30 or not 1 <= args.max_units <= 200000:
        parser.error('out of bounds')
    env_dsn = os.environ.get(args.qabug_dsn_env)
    if not env_dsn:
        parser.error(f'missing {args.qabug_dsn_env} environment variable')
    scope_dsns = {scope:os.environ.get('DIC_SPEC_DSN_'+scope,'') for scope in ('21MM','24MM','COMMON')}
    import psycopg
    from psycopg.rows import dict_row
    try:
        with psycopg.connect(env_dsn,row_factory=dict_row) as qabug:
            if not args.apply:
                with qabug.cursor() as cur:
                    cur.execute("SET TRANSACTION READ ONLY")
            print(json.dumps(process_issue(qabug,issue_key=args.issue_key,
                    scope_dsns=scope_dsns,top_n=args.top_n,max_units=args.max_units,
                    apply=args.apply),ensure_ascii=False,indent=2))
            print('APPLIED candidates only' if args.apply else 'DRY RUN: no writes',file=sys.stderr)
        return 0
    except (RuntimeError,ValueError,psycopg.Error) as exc:
        print(f'INDEX REFUSED: {type(exc).__name__}: {exc}',file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
