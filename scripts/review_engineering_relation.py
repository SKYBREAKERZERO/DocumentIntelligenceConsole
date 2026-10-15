"""Explicit human approval/rejection of a source-backed content relation.

Default is read-only; --apply required for a write. Only local PostgreSQL.
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys


def main(argv=None):
    p=argparse.ArgumentParser(description="Review a v7 content-level relation; writes audited status only on --apply")
    p.add_argument('--database',required=True)
    p.add_argument('--project',required=True,choices=('21MM','24MM'))
    p.add_argument('--candidate-id',required=True,type=int)
    p.add_argument('--status',required=True,choices=('CONFIRMED','REJECTED'))
    p.add_argument('--actor',required=True)
    p.add_argument('--reason',required=True)
    p.add_argument('--host',default='127.0.0.1')
    p.add_argument('--port',type=int,default=5432)
    p.add_argument('--user',default='postgres')
    p.add_argument('--password-env',default='POSTGRES_PASSWORD')
    p.add_argument('--apply',action='store_true')
    a=p.parse_args(argv)
    if a.candidate_id <= 0 or not a.actor.strip() or len(a.reason.strip()) < 10:
        p.error('Candidate ID must be positive and reason >= 10 characters')
    if a.database.lower() in ('rag','rag_qabug'):
        p.error('Only explicit engineering specification databases are supported')
    import psycopg
    password=os.environ.get(a.password_env) or getpass.getpass('PostgreSQL password: ')
    with psycopg.connect(host=a.host,port=a.port,dbname=a.database,user=a.user,password=password,
                         connect_timeout=10,application_name='dic-engineering-review') as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT project_code FROM public.project_info LIMIT 1')
            identity=cur.fetchone()
            if not identity or str(identity[0]).upper()!=a.project:
                raise RuntimeError('Project database identity mismatch')
            cur.execute('''
                SELECT r.id,r.status,r.relation_type,s.raw_text,t.raw_text
                FROM public.ei_content_relations r
                JOIN public.ei_requirement_units s ON s.id=r.source_unit_id
                JOIN public.ei_requirement_units t ON t.id=r.target_unit_id
                WHERE r.id=%s FOR UPDATE OF r
            ''',(a.candidate_id,))
            row=cur.fetchone()
            if not row:
                raise RuntimeError('Candidate not found')
            _,previous,rel,source,target=row
            print('ID:',a.candidate_id,'Current status:',previous,'Type:',rel)
            print('Source:',str(source)[:900])
            print('Target:',str(target)[:900])
            print('Requested:',a.status,'Reason:',a.reason)
            if previous not in ('REVIEW_REQUIRED','CONFIRMED','REJECTED'):
                raise RuntimeError('Unreviewable/stale candidate')
            if not a.apply:
                conn.rollback()
                print('DRY RUN. No changes were made.')
                return 0
            if previous==a.status:
                raise RuntimeError('Already has requested status; no duplicate review generated')
            cur.execute('''UPDATE public.ei_content_relations
                SET status=%s,reviewed_at=NOW(),reviewed_by=%s WHERE id=%s''',
                (a.status,a.actor,a.candidate_id))
            cur.execute('''INSERT INTO public.ei_relation_review_audit
                (candidate_id,actor,previous_status,new_status,reason)
                VALUES (%s,%s,%s,%s,%s)''',
                (a.candidate_id,a.actor,previous,a.status,a.reason))
        conn.commit()
    print('REVIEW COMMITTED')
    return 0


if __name__=='__main__':
    raise SystemExit(main())
