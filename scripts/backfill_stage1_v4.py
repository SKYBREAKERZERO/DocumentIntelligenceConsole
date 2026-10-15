"""Explicit offline Stage1 v4 review proposal builder. PREVIEW by default."""
from __future__ import annotations
import argparse
import getpass
import os
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.engineering_intelligence.stage1_v4 import backfill_stage1_v4


def main(argv: list[str] | None = None) -> int:
    p=argparse.ArgumentParser(description="Stage1 v4 immutable review evidence backfill")
    p.add_argument('--project',required=True,choices=('21MM','24MM'))
    p.add_argument('--database',required=True)
    p.add_argument('--version-id',type=int,required=True)
    p.add_argument('--host',default='127.0.0.1')
    p.add_argument('--port',type=int,default=5432)
    p.add_argument('--user',default='postgres')
    p.add_argument('--password-env',default='POSTGRES_PASSWORD')
    p.add_argument('--max-units',type=int,default=40000)
    p.add_argument('--max-proposals',type=int,default=120000)
    p.add_argument('--apply',action='store_true')
    p.add_argument('--backup-confirmed',action='store_true')
    a=p.parse_args(argv)
    if a.database.lower()!='rag_'+a.project.lower() or a.version_id < 1 or a.max_units<1 or a.max_proposals<1 or not 1<=a.port<=65535:
        p.error('Invalid project/database, version or budget')
    if a.apply and not a.backup_confirmed:
        p.error('--apply requires --backup-confirmed')
    import psycopg
    secret=os.environ.get(a.password_env) or getpass.getpass('PostgreSQL password: ')
    with psycopg.connect(host=a.host,port=a.port,dbname=a.database,user=a.user,password=secret,
                         connect_timeout=10,application_name='dic-stage1-v4-build') as conn:
        try:
            with conn.cursor() as cur:
                cur.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
                if not a.apply:
                    cur.execute('SET TRANSACTION READ ONLY')
                cur.execute('SELECT project_code FROM public.project_info LIMIT 1')
                row=cur.fetchone()
                if not row or str(row[0]).upper()!=a.project:
                    raise RuntimeError('DB identity mismatch')
                cur.execute('''SELECT 1 FROM public.specification_versions v
                    JOIN public.specifications s ON s.id=v.specification_id
                    WHERE v.id=%s AND s.project_code=%s''',(a.version_id,a.project))
                if not cur.fetchone():
                    raise RuntimeError('Version not in requested project')
                result=backfill_stage1_v4(cur,version_id=a.version_id,dry_run=not a.apply,
                                           max_units=a.max_units,max_proposals=a.max_proposals)
            if a.apply: conn.commit()
            else: conn.rollback()
        except Exception:
            conn.rollback()
            raise
    print(f"{'APPLIED' if a.apply else 'PREVIEW'} version={result.version_id} "
          f"source_units={result.source_units} scopes={result.scope_proposals} "
          f"tables={result.table_proposals} digest={result.proposal_digest}")
    return 0


if __name__=='__main__':
    raise SystemExit(main())
