"""Read-only, source-bound evaluation of operator-approved finite checks.

No LLM adjudication. Results cover the configured measurements/conditions only,
never certify a complete specification or that a user's observation is true.
"""
from decimal import Decimal,InvalidOperation
import hashlib,re
from app.ai.retrieval import EvidenceItem
from app.storage.primary_document_policy import PrimarySpecificationDocumentPolicy


def _number(value):
    if isinstance(value,bool) or len(str(value))>40:raise ValueError("Invalid numeric observation")
    try:n=Decimal(str(value))
    except InvalidOperation:raise ValueError("Invalid numeric observation") from None
    if not n.is_finite() or abs(n)>Decimal("1e12"):raise ValueError("Nonfinite/out-of-range observation")
    return n


def evaluate_rule(rule,content,observations):
    allowed={"kind","measurement_key","measurement_label","conditions","anchor","unit","expected"}
    if not isinstance(rule,dict) or set(rule)-allowed:raise ValueError("Unsupported check schema")
    key=rule.get("measurement_key","");anchor=rule.get("anchor","")
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}",key) or not isinstance(anchor,str) or not anchor or content.count(anchor)!=1:
        raise ValueError("Check is not bound to the current literal source")
    conditions=rule.get("conditions")
    if not isinstance(conditions,dict) or len(conditions)>12:raise ValueError("Explicit bounded conditions required")
    missing=[k for k in conditions if k not in observations]
    if missing:return {"status":"UNKNOWN","reason":"缺少适用条件："+", ".join(missing)}
    def same(a,b):
        if isinstance(b,bool):return isinstance(a,bool) and a==b
        if isinstance(b,str):return isinstance(a,str) and a.casefold()==b.casefold()
        return not isinstance(a,bool) and _number(a)==_number(b)
    if not all(same(observations[k],v) for k,v in conditions.items()):
        return {"status":"NOT_APPLICABLE","reason":"提供的条件不满足该检查的适用条件；不能据此判定不符合"}
    if key not in observations:return {"status":"UNKNOWN","reason":"缺少实测值："+key}
    kind=rule.get("kind");expected=rule.get("expected")
    if kind in {"MAXIMUM","RANGE"} and re.search(r"\b(?:not|never)\b",content,re.I):
        raise ValueError("Negated numeric clauses require manual review")
    if kind=="MAXIMUM":
        match=re.fullmatch(r"within\s+(\d+(?:\.\d+)?)\s*(ms|s)",anchor,re.I)
        if not match or rule.get("unit")!=match.group(2).lower() or _number(expected)!=_number(match.group(1)):
            raise ValueError("Approved limit differs from the quoted source")
        actual=_number(observations[key])
        if actual<0:raise ValueError("Time measurement cannot be negative")
        passed=actual<=_number(expected)
    elif kind=="RANGE":
        match=re.fullmatch(r"between\s+(\d+(?:\.\d+)?)\s*(ms|s)\s+and\s+(\d+(?:\.\d+)?)\s*\2",anchor,re.I)
        if not match or not isinstance(expected,list) or len(expected)!=2 or rule.get("unit")!=match.group(2).lower():raise ValueError("Unsupported range anchor")
        lo,hi=map(_number,expected)
        if lo!=_number(match.group(1)) or hi!=_number(match.group(3)) or lo>hi:raise ValueError("Range differs from source")
        actual=_number(observations[key])
        if actual<0:raise ValueError("Time measurement cannot be negative")
        passed=lo<=actual<=hi
    elif kind=="STATE_EQUALS":
        match=re.fullmatch(r"shall\s+(?:be|remain)\s+([a-z][a-z0-9_]{0,59})",anchor,re.I)
        if not isinstance(expected,str) or not match or expected.casefold()!=match.group(1).casefold():
            raise ValueError("State value is not explicit in source")
        actual=observations[key]
        if not isinstance(actual,str):raise ValueError("Explicit observed state required")
        passed=actual.casefold()==expected.casefold()
    else:raise ValueError("Unsupported finite check kind")
    return {"status":"PASS" if passed else "FAIL","measurement_key":key,"actual":str(actual),"expected":expected,
            "unit":rule.get("unit",""),"reason":"仅对已配置的该项条件和实测值进行比较"}


def validate_rule(rule,content):
    """Validate the complete finite schema before accepting an operator proposal."""
    if not isinstance(rule,dict) or not isinstance(rule.get("conditions"),dict):raise ValueError("Explicit conditions required")
    for key,value in rule["conditions"].items():
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}",key):raise ValueError("Invalid condition key")
        if isinstance(value,str):
            if not value or len(value)>120:raise ValueError("Invalid condition value")
        elif not isinstance(value,bool):_number(value)
    label=rule.get("measurement_label","")
    if not isinstance(label,str) or len(label)>120:raise ValueError("Invalid measurement label")
    expected=rule.get("expected")
    sample=expected[0] if rule.get("kind")=="RANGE" and isinstance(expected,list) and expected else expected
    observations={**rule["conditions"],rule.get("measurement_key",""):sample}
    result=evaluate_rule(rule,content,observations)
    if result["status"]!="PASS":raise ValueError("Check schema cannot produce its source-bound expected value")
    return rule


class ConformanceService:
    def __init__(self,retriever):self.retriever=retriever

    def ready(self,conn):
        row=conn.execute("""SELECT to_regclass('dic_conformance.checks') IS NOT NULL
          AND EXISTS(SELECT 1 FROM pg_proc p JOIN pg_roles r ON r.oid=p.proowner
            WHERE p.oid=to_regprocedure('dic_conformance.review(text,bigint,text,text,text)')
              AND p.prosecdef AND NOT r.rolcanlogin AND NOT r.rolsuper AND 'search_path=pg_catalog'=ANY(p.proconfig)
              AND NOT EXISTS(SELECT 1 FROM aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a WHERE a.grantee=0 AND a.privilege_type='EXECUTE'))
          AND (SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE NOT t.tgisinternal AND t.tgenabled='O' AND t.tgtype=31
              AND ((n.nspname='public' AND c.relname='qabug_software_spec_versions' AND t.tgname='dic_conformance_mapping_guard'
                    AND t.tgfoid=to_regprocedure('dic_conformance.guard_object_write()'))
                OR (n.nspname='dic_conformance' AND c.relname='checks' AND t.tgname='dic_conformance_check_guard'
                    AND t.tgfoid=to_regprocedure('dic_conformance.guard_object_write()'))
                OR (n.nspname='dic_conformance' AND c.relname='review_audit' AND t.tgname='dic_conformance_audit_guard'
                    AND t.tgfoid=to_regprocedure('dic_conformance.guard_audit_write()'))))=3 AS ready""").fetchone()
        return bool(row and row["ready"])

    def assess(self,scope,code,context,observations):
        result={"verdict":"UNDETERMINED","label":"证据不足，无法判断","scope":"CONFIGURED_CHECKS_ONLY",
            "observations_origin":"DECLARED_OBSERVATIONS","specification_conformance_certified":False,
            "mapping_verified":False,"checks":[],"reason":"缺少明确工程、式样编号、软件版本、车型或地域"}
        evidence=[]
        if scope not in {"21MM","24MM"} or not re.fullmatch(r"[A-Za-z0-9]{3,8}",code or "") or not isinstance(context,dict) or not isinstance(observations,dict):return result,()
        if not {"model","destination"}<=set(context) or not (context.get("software_version") or context.get("affected_version")):return result,()
        if any(not isinstance(v,str) or not v.strip() or len(v)>120 for v in context.values()):return result,()
        code=code.upper()
        try:
            with self.retriever._connect("QABUG") as conn:
                if not self.ready(conn):result["reason"]="受控适用版本和检查规则尚未部署/启用";return result,()
                mappings=conn.execute("""SELECT m.*,dic_conformance.fingerprint('MAPPING',m.id) AS fingerprint
                  FROM public.qabug_software_spec_versions m
                  JOIN LATERAL(SELECT * FROM dic_conformance.review_audit a WHERE a.object_kind='MAPPING' AND a.object_id=m.id ORDER BY id DESC LIMIT 1) a
                    ON a.new_status='APPROVED' AND a.fingerprint=dic_conformance.fingerprint('MAPPING',m.id)
                  WHERE m.status='APPROVED' AND m.spec_scope=%s AND m.spec_code=%s AND m.model=%s AND m.destination=%s
                    AND ((m.issue_version_field='software_version' AND m.software_version=%s)
                      OR (m.issue_version_field='affected_version' AND m.software_version=%s)) LIMIT 3""",
                  (scope,code,context["model"],context["destination"],context.get("software_version"),context.get("affected_version"))).fetchall()
                if len(mappings)!=1:result["reason"]="没有唯一、经受控审核且与当前上下文完全一致的适用版本映射";return result,()
                mapping=dict(mappings[0])
                checks=[dict(r) for r in conn.execute("""SELECT c.* FROM dic_conformance.checks c
                    JOIN LATERAL(SELECT * FROM dic_conformance.review_audit a WHERE a.object_kind='CHECK' AND a.object_id=c.id ORDER BY id DESC LIMIT 1) a
                      ON a.new_status='APPROVED' AND a.fingerprint=dic_conformance.fingerprint('CHECK',c.id)
                    WHERE c.mapping_id=%s AND c.mapping_fingerprint=%s AND c.status='APPROVED' ORDER BY c.id LIMIT 33""",
                    (mapping["id"],mapping["fingerprint"])).fetchall()]
            if not checks or len(checks)>32:result["reason"]="没有可用的已审核检查，或检查数量超过本次受控上限";return result,()
            result["mapping_verified"]=True
            result["mapping"]={k:mapping[k] for k in ("id","software_version","model","destination","spec_scope","spec_code","spec_version_id","spec_version_label","spec_document_id")}
            for check in checks:
                with self.retriever._connect(scope) as conn:
                    row=conn.execute("""SELECT c.content,c.content_hash,c.document_id,c.section_id,c.page_number,d.file_name,d.metadata,
                        sp.spec_code,sp.project_code,sv.version_label,sv.id AS version_id,sv.status,sv.branch_name
                        FROM public.contents c JOIN public.documents d ON d.document_id=c.document_id
                        JOIN public.specification_versions sv ON sv.document_id=d.document_id JOIN public.specifications sp ON sp.id=sv.specification_id
                        WHERE c.id=%s AND sv.id=%s""",(check["content_id"],mapping["spec_version_id"])).fetchone()
                intact=bool(row and row["document_id"]==check["document_id"]==mapping["spec_document_id"]
                    and row["project_code"]==scope and row["spec_code"]==code and row["version_label"]==mapping["spec_version_label"]
                    and row["status"]=="APPROVED" and row["branch_name"]=="MAIN" and row["section_id"]==check["section_id"]
                    and hashlib.sha256(row["content"].encode()).hexdigest()==row["content_hash"]==check["content_hash"]
                    and PrimarySpecificationDocumentPolicy.is_primary(file_name=row["file_name"],metadata=row["metadata"]))
                entry={"id":check["id"],"status":"UNKNOWN","reason":"目标正文、版本或哈希已变化"}
                if intact:
                    try:entry.update(evaluate_rule(check["rule_json"],row["content"],observations))
                    except (ValueError,TypeError,KeyError):entry["reason"]="已配置规则与原文不一致或该比较类型不受支持"
                    evidence.append(EvidenceItem(evidence_id=str(len(evidence)+1),scope=scope,source_type="CONFIGURED_CHECK_SOURCE",
                        content=row["content"],content_id=check["content_id"],document_id=row["document_id"],file_name=row["file_name"],
                        specification_code=code,specification_version=row["version_label"],page_number=row["page_number"],section_id=row["section_id"],
                        metadata={"source_authority":"POSTGRESQL","content_id":check["content_id"],"content_hash":row["content_hash"],"check_id":check["id"],"mapping_id":mapping["id"]}))
                    entry["evidence_id"]=str(len(evidence))
                result["checks"].append(entry)
            states=[c["status"] for c in result["checks"]]
            if "FAIL" in states:result.update(verdict="POTENTIAL_NONCONFORMANCE",label="在已核查的条件内疑似不符合",reason="至少一个已审核检查与提供的实测值不一致，需测试人员复核")
            elif states and "PASS" in states and all(s in {"PASS","NOT_APPLICABLE"} for s in states):
                result.update(verdict="MATCHED_CONFIGURED_CHECKS",label="在已核查的条件内符合",reason="仅覆盖此次适用的已配置检查，不代表整本式样全部符合")
            else:result["reason"]="缺少实测值/条件、检查不适用或原文发生变化"
        except Exception as exc:result.update(verdict="UNDETERMINED",label="证据不足，无法判断",reason="检查来源不可用："+type(exc).__name__)
        return result,tuple(evidence)


def parse_declared_observations(text):
    aliases={"软件版本":"software_version","车型":"model","地域":"destination","式样编号":"spec_code"}
    data={}
    for match in re.finditer(r'([a-z][a-z0-9_]{0,63}|软件版本|车型|地域|式样编号)\s*[=:：]\s*("[^"\n]{0,120}"|[^;；,，\s]{1,120})',str(text),re.I):
        k=aliases.get(match[1],match[1].lower());v=match[2].strip('"')
        value=True if v.lower()=="true" else False if v.lower()=="false" else v
        if k in data and data[k]!=value:raise ValueError("Conflicting declared observation")
        data[k]=value
        if len(data)>32:raise ValueError("Too many declared observations")
    context={k:data.pop(k) for k in ("software_version","affected_version","model","destination") if k in data}
    return str(data.pop("spec_code","")),context,data
