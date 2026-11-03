"""Local operator proposals and enrolled-reviewer approval; dry-run by default.

Does not install schemas, grant privileges, enroll reviewers, or infer rules.
Read docs/CONDITIONAL_CHECKS_AND_SYNC_CN.md before production deployment.
"""
from pathlib import Path
import argparse,getpass,hashlib,json,os,re,sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from app.server.configuration import load_profiles,user_configuration_dir
from app.ai.conformance import ConformanceService,validate_rule
from app.storage.primary_document_policy import PrimarySpecificationDocumentPolicy


MAPPING_KEYS={'issue_version_field','software_version','model','destination','spec_scope','spec_code','spec_version_id'}
CHECK_KEYS={'mapping_id','content_id','rule_json'}


def proposal_shape(data,kind):
    keys=MAPPING_KEYS if kind=='MAPPING' else CHECK_KEYS
    if not isinstance(data,dict) or set(data)!=keys:raise ValueError('Proposal keys must be exactly: '+', '.join(sorted(keys)))
    if kind=='MAPPING':
        if data['issue_version_field'] not in {'software_version','affected_version'} or data['spec_scope'] not in {'21MM','24MM'}:
            raise ValueError('Explicit supported scope/version field required')
        if not isinstance(data['spec_code'],str) or not re.fullmatch('[A-Za-z0-9]{3,8}',data['spec_code']):raise ValueError('Invalid specification code')
        for key in ('software_version','model','destination'):
            if not isinstance(data[key],str) or not data[key].strip() or len(data[key])>120:raise ValueError('Explicit bounded context required: '+key)
        ids=['spec_version_id']
    else:ids=['mapping_id','content_id']
    for key in ids:
        if isinstance(data[key],bool) or not isinstance(data[key],int) or data[key]<=0:raise ValueError('Positive integer required: '+key)
    return data


class Operator:
    def __init__(self,profiles,password):self.profiles=profiles;self.password=password
    def connect(self,scope,*,review_login=None):
        import psycopg
        from psycopg.rows import dict_row
        kw=self.profiles[scope].dsn_kwargs();kw['password']=self.password
        if review_login:
            kw.update(user=review_login,password=os.environ.get('DIC_CONFORMANCE_REVIEW_PASSWORD') or getpass.getpass('Reviewer password: '))
        kw['options']='-c statement_timeout=15000 -c lock_timeout=3000'
        return psycopg.connect(**kw,row_factory=dict_row)
    def _connect(self,scope):
        conn=self.connect(scope);conn.execute('SET TRANSACTION READ ONLY');return conn
    def require_ready(self):
        with self._connect('QABUG') as conn:
            if not ConformanceService(self).ready(conn):raise ValueError('Controlled authority is not deployed/ready; no automatic migration')
    def source(self,mapping,content_id=None):
        with self._connect(mapping['spec_scope']) as conn:
            row=conn.execute("""SELECT s.spec_code,s.project_code,v.id AS version_id,v.version_label,v.status,v.branch_name,
                d.document_id,d.file_name,d.metadata FROM specifications s JOIN specification_versions v ON v.specification_id=s.id
                JOIN documents d ON d.document_id=v.document_id WHERE v.id=%s""",(mapping['spec_version_id'],)).fetchone()
            if not row or row['spec_code']!=str(mapping['spec_code']).upper() or row['project_code']!=mapping['spec_scope'] or row['status']!='APPROVED' or row['branch_name']!='MAIN':
                raise ValueError('Explicit approved main-branch specification identity required')
            if not PrimarySpecificationDocumentPolicy.is_primary(file_name=row['file_name'],metadata=row['metadata']):raise ValueError('Primary specification body required')
            value=dict(row)
            if content_id is not None:
                content=conn.execute('SELECT id,document_id,content_hash,content,section_id,page_number FROM contents WHERE id=%s AND document_id=%s',(content_id,row['document_id'])).fetchone()
                if not content or hashlib.sha256(content['content'].encode()).hexdigest()!=content['content_hash']:raise ValueError('Clause content/hash binding is not intact')
                value.update(dict(content))
            return value
    def object(self,conn,kind,ident):
        query='SELECT *,dic_conformance.fingerprint(%s,id) AS fingerprint FROM '+('public.qabug_software_spec_versions' if kind=='MAPPING' else 'dic_conformance.checks')+' WHERE id=%s'
        row=conn.execute(query,(kind,ident)).fetchone()
        if not row:raise ValueError('Object not found')
        return dict(row)
    def bind(self,data,kind):
        proposal_shape(data,kind)
        if kind=='MAPPING':
            result={**data,'spec_code':data['spec_code'].upper()};source=self.source(result)
            result.update(spec_version_label=source['version_label'],spec_document_id=source['document_id'])
        else:
            with self._connect('QABUG') as conn:
                mapping=self.object(conn,'MAPPING',data['mapping_id'])
                head=conn.execute("SELECT new_status,fingerprint FROM dic_conformance.review_audit WHERE object_kind='MAPPING' AND object_id=%s ORDER BY id DESC LIMIT 1",(mapping['id'],)).fetchone()
            if mapping['status']!='APPROVED' or not head or head['new_status']!='APPROVED' or head['fingerprint']!=mapping['fingerprint']:
                raise ValueError('Current controlled mapping approval required before proposing a check')
            source=self.source(mapping,data['content_id']);validate_rule(data['rule_json'],source['content'])
            result={**data,'mapping_fingerprint':mapping['fingerprint'],'document_id':source['document_id'],
                'content_hash':source['content_hash'],'section_id':source['section_id']}
        return result,source
    def inspect(self,kind,ident):
        with self._connect('QABUG') as conn:row=self.object(conn,kind,ident)
        output={'object':row,'review_fingerprint':row['fingerprint']}
        try:
            if kind=='MAPPING':source=self.source(row)
            else:
                data={key:row[key] for key in CHECK_KEYS};bound,source=self.bind(data,kind)
                if any(row[key]!=value for key,value in bound.items()):raise ValueError('Approved check binding has changed')
            output['source']=source
        except ValueError as exc:output['source_validation_error']=str(exc)
        return output
    def propose(self,data,kind,apply=False):
        result,source=self.bind(data,kind)
        output={'mode':'CREATE_PENDING_PROPOSAL' if apply else 'DRY_RUN','kind':kind,'proposal':result,'source':source,'approved':False}
        if apply:
            from psycopg import sql
            from psycopg.types.json import Jsonb
            table=sql.SQL('public.qabug_software_spec_versions' if kind=='MAPPING' else 'dic_conformance.checks')
            keys=list(result);values=[Jsonb(result[k]) if k=='rule_json' else result[k] for k in keys]
            with self.connect('QABUG') as conn:
                row=conn.execute(sql.SQL('INSERT INTO {} ({}) VALUES ({}) ON CONFLICT DO NOTHING RETURNING id,status').format(table,sql.SQL(',').join(map(sql.Identifier,keys)),sql.SQL(',').join(sql.Placeholder() for k in keys)),values).fetchone()
                output['inserted']=dict(row) if row else None
                output['note']='Duplicate proposals are preserved; inspect existing ID rather than changing approved data.'
        return output


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config-dir',type=Path,default=user_configuration_dir())
    sub=parser.add_subparsers(dest='action',required=True)
    propose=sub.add_parser('propose');propose.add_argument('--file',type=Path,required=True);propose.add_argument('--apply',action='store_true')
    inspect=sub.add_parser('inspect')
    review=sub.add_parser('review');review.add_argument('--review-login',required=True);review.add_argument('--fingerprint',required=True)
    review.add_argument('--status',choices=['APPROVED','REVOKED','PENDING'],required=True);review.add_argument('--reason',required=True)
    for command in (propose,inspect,review):command.add_argument('--kind',choices=['MAPPING','CHECK'],required=True)
    for command in (inspect,review):command.add_argument('--id',type=int,required=True)
    args=parser.parse_args(argv)
    profiles,password,_,_=load_profiles(args.config_dir);operator=Operator(profiles,password);operator.require_ready()
    if args.action=='propose':
        if args.file.stat().st_size>65536:raise ValueError('Proposal file too large')
        result=operator.propose(json.loads(args.file.read_text(encoding='utf-8-sig')),args.kind,args.apply)
    elif args.action=='inspect':result=operator.inspect(args.kind,args.id)
    else:
        inspected=operator.inspect(args.kind,args.id)
        if inspected['review_fingerprint']!=args.fingerprint:raise ValueError('Review fingerprint no longer matches the inspected proposal')
        if args.status=='APPROVED' and inspected.get('source_validation_error'):raise ValueError(inspected['source_validation_error'])
        with operator.connect('QABUG',review_login=args.review_login) as conn:
            conn.execute('SELECT dic_conformance.review(%s,%s,%s,%s,%s)',(args.kind,args.id,args.fingerprint,args.status,args.reason))
        result={'kind':args.kind,'id':args.id,'status':args.status,'review_login':args.review_login}
    print(json.dumps(result,ensure_ascii=False,indent=2,default=str))
    return 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except (ValueError,OSError) as exc:raise SystemExit(str(exc))
