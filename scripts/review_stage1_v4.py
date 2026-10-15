"""Operator-authorized append-only Stage1 review. No AI automatically approves.

`list` is metadata only; `show` explicitly displays sensitive engineering text.
`review` requires evidence digest and last-event CAS token from `show`.
"""
from __future__ import annotations
import argparse
import getpass
import json
import os
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.engineering_intelligence.review_events import record_review_event


def main(argv: list[str] | None = None) -> int:
    p=argparse.ArgumentParser(description='Stage1 v4 manual review; read-only except explicit review action')
    p.add_argument('action',choices=('list','show','review'))
    p.add_argument('--project',required=True,choices=('21MM','24MM'))
    p.add_argument('--database',required=True)
    p.add_argument('--version-id',required=True,type=int)
    p.add_argument('--host',default='127.0.0.1')
    p.add_argument('--port',type=int,default=5432)
    p.add_argument('--user',default='postgres')
    p.add_argument('--password-env',default='POSTGRES_PASSWORD')
    p.add_argument('--proposal-key')
    p.add_argument('--show-source',action='store_true')
    p.add_argument('--limit',type=int,default=30)
    p.add_argument('--request-id')
    p.add_argument('--expected-digest')
    p.add_argument('--expected-event-id',type=int)
    p.add_argument('--reviewer')
    p.add_argument('--reason')
    p.add_argument('--status',choices=('CONFIRMED','REJECTED','NEEDS_CLARIFICATION'))
    p.add_argument('--assertion',choices=('ACTION_ONLY','CONDITION_GOVERNS_ACTION',
                  'EXCEPTION_OVERRIDES_ACTION','HEADER_ROW_APPLIES'))
    p.add_argument('--target-unit-key')
    p.add_argument('--backup-confirmed',action='store_true')
    a=p.parse_args(argv)
    if a.database.lower()!='rag_'+a.project.lower() or a.version_id<1 or not 1<=a.port<=65535 or not 1<=a.limit<=100:
        p.error('Invalid DB/project/limit/version')
    if a.action!='list' and (not a.proposal_key or len(a.proposal_key)!=64):
        p.error('show/review requires --proposal-key SHA256')
    if a.action=='review' and (not a.backup_confirmed or not all((a.request_id,a.expected_digest,a.reviewer,a.reason,a.status))
                               or a.expected_event_id is None):
        p.error('Review requires backup confirmation, request UUID, exact evidence digest, latest event ID, reviewer, reason and status')
    import psycopg
    secret=os.environ.get(a.password_env) or getpass.getpass('PostgreSQL password: ')
    recorded_event_id = None
    with psycopg.connect(host=a.host,port=a.port,dbname=a.database,user=a.user,password=secret,
                         connect_timeout=10,application_name='dic-stage1-v4-manual-review') as conn:
        try:
            with conn.cursor() as cur:
                cur.execute('SET TRANSACTION ISOLATION LEVEL SERIALIZABLE')
                if a.action != 'review':
                    cur.execute('SET TRANSACTION READ ONLY')
                cur.execute('SELECT project_code FROM public.project_info LIMIT 1')
                row=cur.fetchone()
                if not row or str(row[0]).upper()!=a.project:
                    raise RuntimeError('Project identity mismatch')
                if a.action == 'review':
                    event_id=record_review_event(cur,version_id=a.version_id,project=a.project,
                              proposal_key=a.proposal_key,expected_evidence_digest=a.expected_digest,
                              expected_last_event_id=a.expected_event_id,request_id=a.request_id,
                              reviewer=a.reviewer,reason=a.reason,status=a.status,
                              assertion=a.assertion,target_unit_key=a.target_unit_key)
                    recorded_event_id = event_id
                else:
                    q='''SELECT p.proposal_key,p.kind,p.evidence_digest,p.payload,
                        (SELECT e.id FROM public.ei_stage1_review_events e
                         WHERE e.proposal_key=p.proposal_key ORDER BY e.id DESC LIMIT 1) AS latest_id,
                        (SELECT e.status FROM public.ei_stage1_review_events e
                         WHERE e.proposal_key=p.proposal_key ORDER BY e.id DESC LIMIT 1) AS last_status
                        FROM public.ei_stage1_review_proposals p
                        JOIN public.specification_versions v ON v.id=p.version_id
                        JOIN public.specifications s ON s.id=v.specification_id
                        WHERE p.version_id=%s AND s.project_code=%s'''
                    params=[a.version_id,a.project]
                    if a.proposal_key:
                        q+=' AND p.proposal_key=%s'
                        params.append(a.proposal_key)
                    q+=' ORDER BY p.proposal_key LIMIT %s'
                    params.append(1 if a.action=='show' else a.limit)
                    cur.execute(q,tuple(params))
                    rows=cur.fetchall()
                    for key,kind,digest,payload,event_id,status in rows:
                        if isinstance(payload,str): payload=json.loads(payload)
                        data=dict(key=str(key).strip(),kind=kind,evidence_digest=str(digest).strip(),
                                  expected_event_id=int(event_id or 0),review_status=status or 'PENDING')
                        if a.action=='show':
                            data['evidence']=payload if a.show_source else {'review_reasons':payload.get('review_reasons',[]),
                                                   'source_content_id':payload.get('source_content_id'),
                                                   'section_id':payload.get('section_id'),
                                                   'row_count':len(payload.get('rows',[]))}
                        print(json.dumps(data,ensure_ascii=False,sort_keys=True))
            if a.action=='review': conn.commit()
            else: conn.rollback()
        except Exception:
            conn.rollback()
            raise
    if recorded_event_id is not None:
        print(f'RECORDED review_event_id={recorded_event_id} (committed human decision, not model-certified)')
    return 0


if __name__=='__main__':
    raise SystemExit(main())
