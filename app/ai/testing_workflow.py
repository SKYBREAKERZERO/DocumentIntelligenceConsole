"""Fixed issue-first testing workflow and literal ticket/spec reference tracing.

Vector similarity is a discovery rank, never duplicate proof or conformance.
No database migrations, source writes or arbitrary model-generated tools.
"""
from dataclasses import replace
import hashlib
import re
import time
import uuid
from app.ai.retrieval import EvidenceItem,RetrievalResult
from app.ai.retrieval_deadline import retrieval_budget,remaining_ms
from app.storage.primary_document_policy import PrimarySpecificationDocumentPolicy
from app.ai.interaction_intelligence import evidence_usability

PRE_TICKET=re.compile(r"(?:起|提|创建|建立|登[録记錄記]|提交|报|報).{0,6}(?:bug|qa|票|缺陷|问题单).{0,5}(?:前|之前)|(?:查重|重复.{0,4}(?:bug|qa)|相同现象|同样现象|类似.{0,4}(?:bug|qa)|有没有人.{0,6}(?:遇到|报过|提过)|之前.{0,8}(?:有过|出现过|报过|提过)|历史.{0,6}(?:问题|票|现象)|before.{0,12}(?:filing|raising)|重複)",re.I)
ISSUE_KEY=re.compile(r"(?<![A-Z0-9])(?!SPEC-)([A-Z][A-Z0-9]{1,19}-\d{1,10})(?![A-Z0-9])",re.I)
FILE=re.compile(r"(?m)^\s*(?P<name>(?P<code>\d{3,6})[_ -][^\r\n]{1,180}?\.(?:docx?|pdf|xlsx?))\s*$",re.I)
INLINE_FILE=re.compile(r"(?P<name>(?P<code>\d{3,6}[_ -])?[A-Za-z0-9][A-Za-z0-9 _().&+\-]{2,180}?"
    r"(?:Specification|Spec)[A-Za-z0-9 _().&+\-]*?(?:_|\s)(?:v|ver)\.?\d+(?:\.\d+){1,4}\.pdf)",re.I)
VERSION=re.compile(r"(?:ver\.?|version|v)\s*(\d+(?:\.\d+){1,4})(?!\d)",re.I)
TEST_DESIGN=re.compile(r"回归测试|回歸測試|测试观点|測試觀點|测试点|測試點|测试项|測試項|"
    r"测试手顺|測試手順|操作手顺|操作手順|测试步骤|測試步驟|测试用例|測試案例|test\s*case|regression",re.I)
TICKET_ANALYSIS=re.compile(r"总结|總結|概括|展开|展開|详细|詳細|分析|说说|說說|梳理|"
    r"为什么|為什麼|原因|根因|影响|影響|怎么看|怎麼看|解读|解讀|explain|analy[sz]e|summary",re.I)


def testing_route(question):
    if PRE_TICKET.search(question):return "PRE_TICKET_CHECK"
    # When a concrete ticket is present, "why was it judged compliant" asks
    # for an explanation of the recorded ticket conclusion.  It is not a new
    # formal conformance certification and must retain the ticket context.
    if ISSUE_KEY.search(question) and TEST_DESIGN.search(question):
        return "TICKET_SPEC_TRACE"
    if ISSUE_KEY.search(question) and TICKET_ANALYSIS.search(question):
        return "TICKET_SPEC_TRACE"
    if ISSUE_KEY.search(question) and re.search(r"式样|式樣|仕様|spec|条款|正文|要求|设计依据|設計依據",question,re.I) and re.search(r"引用|对应|定位|找到|参照|refer|条款|用到|涉及|关联|依据|影响|哪(?:本|段|章)",question,re.I):
        return "TICKET_SPEC_TRACE"
    if re.search(r"式样通|式樣通|仕様通り|是否符合|符合性|符合.{0,4}(?:式样|要求)|违反.{0,4}(?:式样|要求)|违背.{0,4}(?:式样|要求)|这是.{0,3}(?:bug|缺陷)|是不是.{0,3}(?:bug|缺陷)|该不该.{0,3}(?:起票|提票)|要不要.{0,3}(?:起票|提票)|conformance",question,re.I):
        return "CONFORMANCE_CHECK"
    # A tester often pastes only the observed phenomenon without saying
    # “before filing a BUG”. Route technical symptom statements through the
    # same issue-first workflow instead of generic engineering generation.
    if (re.search(r"蓝牙|藍牙|Bluetooth|Profile|用户|用戶|HMI|ECU|CAN|连接|連接|切换|切換|画面|畫面|屏幕|音频|音頻|声音|导航|導航|信号|信號|按键|按鍵|摄像|相机|麦克风|网络|Wi-?Fi|USB|CarPlay|Android Auto|启动|起動|登录|登入|设置|設定",question,re.I)
            and re.search(r"现象|現象|没有|沒有|无法|無法|失败|失敗|异常|異常|偶发|偶現|间歇|不(?:能|会|會|显示|顯示|工作|响应|響應|保存|加载|播放|切换|连接)|断开|斷開|消失|卡住|死机|黑屏|白屏|闪退|重启|延迟|慢|错误|不一致|恢复|恢復|丢失|无声|杂音|花屏",question,re.I)):
        return "PRE_TICKET_CHECK"
    return ""


def explicit_references(text):
    """Only unambiguous filename/version blocks with explicit section anchors.

    A ticket's quoted spec filename is a citation, not an approved applicable
    software version. No LLM resolves a missing filename, version or page.
    """
    refs=[]
    source=str(text or "")[:20000]
    files=list(FILE.finditer(source))
    for n,match in enumerate(files):
        version=VERSION.search(match.group("name"))
        if not version:continue
        block=text[match.end():files[n+1].start() if n+1<len(files) else len(text)]
        page=None
        for line in block.splitlines():
            p=re.fullmatch(r"\s*(?:p\.?|page|页|頁)\s*[:.：]?\s*(\d{1,5})\s*",line,re.I)
            if p:page=int(p.group(1));continue
            s=re.match(r"\s*(?:(?:section|sec\.?|§|章节|章節)\s*)?(\d+(?:\.\d+){1,7})\.?[\s\t:：]+",line,re.I)
            if not s:
                s=re.fullmatch(r"\s*(\d+(?:\.\d+){1,7})\.?\s*",line)
            if s:
                refs.append({"file_name":match.group("name"),"code":match.group("code"),
                    "version":version.group(1),"section":s.group(1),"page":page})
    # Jira exports often keep the entire citation on one line, for example:
    # Handsfree Function Specification_v2.02.pdf ページ:93 9.7.3 ...
    for match in INLINE_FILE.finditer(source):
        version=VERSION.search(match.group("name"))
        if not version:continue
        tail=source[match.end():match.end()+300]
        page_match=re.search(r"(?:p\.?|page|ページ|页|頁)\s*[:.：]?\s*(\d{1,5})",tail,re.I)
        section_match=re.search(r"(?:(?:section|sec\.?|§|章节|章節)\s*)?(\d+(?:\.\d+){1,7})",tail,re.I)
        if not section_match:continue
        code_prefix=str(match.group("code") or "").strip("_ -")
        refs.append({"file_name":match.group("name"),"code":code_prefix,
            "version":version.group(1),"section":section_match.group(1),
            "page":int(page_match.group(1)) if page_match else None})
    return list({tuple(r.items()):r for r in refs}.values())[:12]


def symptom_text(question):
    carried=re.search(r"Conversation symptom:\s*(.+?)(?:\nConversation |$)",question,re.I|re.S)
    if carried:
        return re.sub(r"\s+"," ",carried.group(1)).strip()[:1000]
    first=re.split(r"[；;]|起\s*(?:BUG|QA|票)|(?:在|提交|创建).{0,3}(?:BUG|QA).{0,3}前",question,flags=re.I)[0]
    first=re.sub(r"(?:21MM|24MM|起票前|查重|查一下|请先|查询历史|查询|是否有相同现象|有没有重复)"," ",first,flags=re.I)
    return re.sub(r"\s+"," ",first).strip(" ，,。:")[:1000] or question[:1000]


def ticket_spec_query(ticket):
    """Build a bounded semantic query from engineering fields, not UI prose."""
    md=ticket.metadata or {}
    values=[]
    for key in ("summary","phenomenon","description","repro_steps","procedure_expected","expected_result",
                "actual_result","actual_behavior","answer","final_answer","rationale","analysis_result",
                "root_cause","effect_result","effect_version","countermeasure","impact_analysis","component"):
        value=" ".join(str(md.get(key) or "").split())
        if value and value not in values:values.append(value[:700])
    return "\n".join(values)[:3200]


def ticket_feature_entities(ticket):
    """Extract product/function names for entity-first specification lookup."""
    md=ticket.metadata or {}
    seed=" ".join(str(md.get(k) or "") for k in (
        "summary","phenomenon","component","description","procedure_expected","answer","final_answer"))[:5000]
    raw=re.findall(r"[A-Za-z][A-Za-z0-9+_.-]{2,}|[ァ-ヶー]{4,}",seed)
    stop={"record","type","issue","summary","status","priority","description","result","expected",
          "actual","steps","source","menu","screen","error","version","main","sub","boot",
          "現象発生","操作手順","期待結果","使用端末","備考"}
    entities=[]
    for token in raw:
        cleaned=token.strip("._-")
        if len(cleaned)<3 or cleaned.lower() in stop or cleaned in stop:continue
        if re.fullmatch(r"\d+(?:\.\d+)*",cleaned):continue
        if cleaned.lower() not in {x.lower() for x in entities}:entities.append(cleaned)
        if len(entities)>=8:break
    return tuple(entities)


class TestingWorkflow:
    def __init__(self,retriever):self.retriever=retriever

    def run(self,question,*,scope="AUTO",current_scope="",cancel_check=None,seconds=25):
        start=time.monotonic()
        route=testing_route(question) or "PRE_TICKET_CHECK"
        explicit=tuple(dict.fromkeys(s.upper() for s in re.findall(r"(?<![A-Z0-9])(21MM|24MM)(?![A-Z0-9])",question,re.I)))
        selected=str(scope or "AUTO").upper()
        scopes=((selected,) if selected in {"21MM","24MM","COMMON"} else explicit or
                ((current_scope.upper(),) if current_scope.upper() in {"21MM","24MM","COMMON"}
                 else ("21MM","24MM","COMMON")))
        workflow={"kind":route,"state":"STARTED","steps":[],"conformance":"UNDETERMINED","original_question":question,
            "duplicate_verdict":"UNDETERMINED","coverage":"UNVERIFIED",
            "negative_search_means_absent":False,"scope":list(scopes),"threshold":0.72,
            "applicable_version_mapping_verified":False,"unresolved_references":[],
            "test_design_requested":bool(TEST_DESIGN.search(question)),
            "ticket_analysis_requested":bool(TICKET_ANALYSIS.search(question))}
        workflow["spec_evidence_requested"]=bool(
            workflow["test_design_requested"] or re.search(
                r"式样|式樣|仕様|spec|条款|正文|引用|参照|參照|"
                r"关联|關聯|依据|仕様通り|式样通|符合性|回归测试|測試手順",
                question, re.I))
        items=[];warnings=[];tools=[]
        def check():
            if cancel_check and cancel_check():raise InterruptedError("Request cancelled")
            remaining_ms()
        def step(name,status,count=0):workflow["steps"].append({"step":name,"status":status,"count":count})
        with retrieval_budget(max(seconds,45) if route=="TICKET_SPEC_TRACE" else seconds):
            try:
                check()
                key=ISSUE_KEY.search(question)
                if route in {"TICKET_SPEC_TRACE","CONFORMANCE_CHECK"} and key:
                    if not explicit and selected not in {"21MM","24MM","COMMON"} and not current_scope:
                        issue_upper=key.group(1).upper()
                        if issue_upper.startswith("MM21"):scopes=("21MM",)
                        elif issue_upper.startswith("MM24"):scopes=("24MM",)
                        workflow["scope"]=list(scopes)
                    tools.append("issue_exact_sql")
                    tickets=self.retriever._qabug_issue_sql_search(key.group(1),{"issue_key":key.group(1).upper()})
                    items.extend(tickets[:3]);step("ISSUE_EXACT","DONE",len(tickets))
                    refs=[]
                    for ticket in tickets[:3]:
                        seen=set()
                        for field in ("reference_spec","description","phenomenon","actual_result","actual_behavior",
                                      "expected_result","repro_steps","procedure_expected","answer","final_answer",
                                      "rationale","analysis_result","root_cause","countermeasure","effect_result","impact_analysis"):
                            for ref in explicit_references(str(ticket.metadata.get(field) or "")):
                                identity=tuple(ref.items())
                                if identity in seen:continue
                                seen.add(identity)
                                ref["issue_key"]=str(ticket.metadata.get("issue_key") or key.group(1))
                                ref["source_field"]=field
                                refs.append(ref)
                    if not refs:step("EXPLICIT_REFERENCE","NO_UNAMBIGUOUS_ANCHOR")
                    if not scopes:
                        workflow["state"]="PROJECT_REQUIRED"
                        workflow["unresolved_references"]=refs
                    else:
                        tools.append("ticket_literal_reference_sql")
                        for ref in refs[:12]:
                            check()
                            found=self.resolve_reference(ref,scopes)
                            items.extend(found)
                            if not found:workflow["unresolved_references"].append(ref)
                        step("REFERENCE_BODY","DONE",sum(i.source_type=="TICKET_SPEC_CLAUSE" for i in items))
                        workflow["state"]="REFERENCE_RESOLVED" if refs and not workflow["unresolved_references"] else "REFERENCE_INCOMPLETE"
                        # A ticket may not contain a formal filename+version+section
                        # citation. In that common case, continue from its actual
                        # phenomenon, reproduction, analysis and countermeasure.
                        # These hits are candidates, never promoted to explicit refs.
                        semantic_query=ticket_spec_query(tickets[0]) if tickets else ""
                        if not workflow["spec_evidence_requested"]:
                            semantic_query=""
                        if semantic_query:
                            entities=ticket_feature_entities(tickets[0])
                            entity_hits=[]
                            for entity in entities[:5]:
                                check()
                                try:
                                    found_entities=self.retriever._sql_exact_search(entity,scopes)
                                except Exception:
                                    # Some upgraded databases do not yet expose
                                    # the exact-search compatibility view. Entity
                                    # semantic search still hydrates authoritative
                                    # source text and must keep the workflow alive.
                                    found_entities=self.retriever._semantic_search(
                                        entity,scopes,cancel_check=cancel_check,source_type="TICKET_ENTITY_MATCH")
                                for hit in found_entities:
                                    if (hit.document_id,hit.content_id) not in {(x.document_id,x.content_id) for x in entity_hits}:
                                        entity_hits.append(replace(hit,source_type="TICKET_ENTITY_MATCH",metadata={
                                            **(hit.metadata or {}),"matched_ticket_entity":entity,
                                            "relation_semantics":"ENTITY_MATCH_CANDIDATE"}))
                                # A strong first entity such as MiraCast already
                                # identifies the feature corpus. Do not dilute it
                                # with phone models, OS names or generic UI words.
                                if len(entity_hits)>=4:break
                            entity_codes=tuple(dict.fromkeys(str(i.specification_code) for i in entity_hits if i.specification_code))
                            tools.append("ticket_entity_spec_search")
                            tools.append("ticket_to_spec_semantic_search")
                            check()
                            raw_candidates=self.retriever._semantic_search(
                                semantic_query,scopes,entity_codes=entity_codes,cancel_check=cancel_check,
                                source_type="TICKET_SPEC_SEMANTIC")
                            # Exact entity matches answer “which specification”; the
                            # restricted semantic pass answers “which content”.
                            raw_candidates=[*entity_hits,*raw_candidates]
                            seen={(i.document_id,i.content_id) for i in items}
                            candidates=[];per_spec={}
                            for candidate in raw_candidates:
                                if (candidate.document_id,candidate.content_id) in seen:continue
                                if evidence_usability(candidate.content)[0] == "REJECT":continue
                                group=str(candidate.specification_code or candidate.document_id or "UNRESOLVED")
                                if per_spec.get(group,0)>=3:continue
                                per_spec[group]=per_spec.get(group,0)+1
                                candidates.append(candidate)
                                if len(candidates)>=10:break
                            items.extend(candidates)
                            step("TICKET_SPEC_CANDIDATES","DONE",len(candidates))
                            codes=tuple(dict.fromkeys(str(i.specification_code) for i in candidates if i.specification_code))
                            if codes:
                                related=self.retriever._content_relation_search(scopes,spec_codes=codes)
                                items.extend(related[:6])
                                step("CONTENT_RELATION_EXPANSION","DONE",len(related[:6]))
                            if candidates and not refs:
                                workflow["state"]="TICKET_SPEC_CANDIDATES"
                elif route!="CONFORMANCE_CHECK":
                    query=symptom_text(question)
                    workflow["symptom_query"]=query
                    tools.append("issue_semantic_sql_hydration")
                    try:
                        tickets=self.retriever._semantic_search(query,("QABUG",),artifact_type="QABUG_REGISTER",
                            source_type="ISSUE_SEMANTIC",cancel_check=cancel_check,observation_only=True)
                    except Exception as exc:
                        tickets=[];warnings.append("Issue vector retrieval unavailable: "+type(exc).__name__)
                    check()
                    # Highest scoring logical ticket survives; never conflate
                    # several chunks of one issue with several duplicate tickets.
                    unique={}
                    for item in sorted(tickets,key=lambda i:float(i.score or 0),reverse=True):
                        key=str(item.metadata.get("issue_key") or item.metadata.get("record_key") or "")
                        if key:unique.setdefault(key,item)
                    tickets=list(unique.values())[:8]
                    strong=[i for i in tickets if float(i.score or 0)>=workflow["threshold"]]
                    items.extend(tickets)
                    workflow["above_threshold_candidates"]=len(strong)
                    step("DUPLICATE_DISCOVERY","DONE",len(tickets))
                    if strong:
                        workflow["state"]="POSSIBLE_DUPLICATES"
                        workflow["duplicate_verdict"]="POSSIBLE_MATCH_REVIEW_REQUIRED"
                    if not scopes:
                        workflow["state"]="PROJECT_REQUIRED"
                        step("SPEC_FALLBACK","PROJECT_REQUIRED")
                    else:
                        tools.append("spec_semantic_sql_hydration")
                        step("SPEC_FALLBACK","STARTED")
                        check()
                        specs=self.retriever._semantic_search(query,scopes,cancel_check=cancel_check,source_type="SEMANTIC")
                        check()
                        items.extend(specs[:8])
                        step("SPEC_FALLBACK","DONE",len(specs[:8]))
                        if strong:
                            workflow["state"]="POSSIBLE_DUPLICATES_WITH_SPEC_CANDIDATES"
                        else:
                            workflow["state"]="SPEC_EVIDENCE_REVIEW_REQUIRED" if specs else "INSUFFICIENT_EVIDENCE"
                        codes=tuple(dict.fromkeys(str(i.specification_code) for i in specs[:8] if i.specification_code))
                        if codes:
                            tools.append("confirmed_content_relation_search")
                            related=self.retriever._content_relation_search(scopes,spec_codes=codes)
                            items.extend(related[:8])
                            step("CONTENT_RELATION_EXPANSION","DONE",len(related[:8]))
                if route=="CONFORMANCE_CHECK" or (workflow["state"]!="POSSIBLE_DUPLICATES" and re.search(r"式样通|式樣通|是否符合|符合性|conformance",question,re.I)):
                    check()
                    from app.ai.conformance import ConformanceService,parse_declared_observations
                    code,context,observations=parse_declared_observations(question)
                    conflict=False
                    for item in items:
                        if item.scope!="QABUG":continue
                        for k in ("software_version","affected_version","model","destination"):
                            actual=str(item.metadata.get(k) or "")
                            if actual:
                                if k in context and str(context[k])!=actual:conflict=True
                                else:context[k]=actual
                        for field in ("actual_result","actual_behavior","phenomenon","repro_steps"):
                            _,_,source_observations=parse_declared_observations(item.metadata.get(field) or "")
                            for k,v in source_observations.items():
                                if k in observations and observations[k]!=v:conflict=True
                                else:observations[k]=v
                    if not code:
                        referenced={r["code"] for item in items if item.scope=="QABUG"
                            for field in ("reference_spec","description") for r in explicit_references(str(item.metadata.get(field) or ""))}
                        if len(referenced)==1:code=next(iter(referenced))
                    if conflict:
                        assessment={"verdict":"UNDETERMINED","label":"证据不足，无法判断","mapping_verified":False,
                            "reason":"提供的观测/适用上下文与票内原始字段冲突，请先核对","checks":[],"specification_conformance_certified":False}
                        sources=()
                    else:
                        assessment,sources=ConformanceService(self.retriever).assess(scopes[0] if len(scopes)==1 else "",code,context,observations)
                    offset=len(items)
                    for entry in assessment.get("checks",[]):
                        if entry.get("evidence_id"):entry["evidence_id"]=str(int(entry["evidence_id"])+offset)
                    items.extend(sources);workflow["assessment"]=assessment
                    workflow["conformance"]=assessment["verdict"]
                    workflow["applicable_version_mapping_verified"]=assessment.get("mapping_verified",False)
                    if route=="CONFORMANCE_CHECK":workflow["state"]="CONFORMANCE_ASSESSED"
                    step("CONFORMANCE_CHECK","DONE",len(assessment.get("checks",[])))
            except (TimeoutError,InterruptedError) as exc:
                workflow["state"]="CANCELLED" if isinstance(exc,InterruptedError) else "BUDGET_EXCEEDED"
                warnings.append("Testing workflow incomplete: "+type(exc).__name__)
            except Exception as exc:
                workflow["state"]="SOURCE_UNAVAILABLE"
                warnings.append("Testing workflow source unavailable: "+type(exc).__name__)
        workflow["elapsed_ms"]=int((time.monotonic()-start)*1000)
        evidence=tuple(replace(item,evidence_id=str(n)) for n,item in enumerate(items,1))
        return RetrievalResult(evidence=evidence,resolved_scopes=tuple((*scopes,"QABUG")),
            warnings=tuple(warnings),route=route,tools=tuple(tools),request_id="test-"+uuid.uuid4().hex[:16],
            plan_source="fixed-testing-skill",evidence_strength="LIMITED" if evidence else "NONE",
            knowledge_source="POSTGRES+QDRANT",workflow=workflow,
            trace=tuple(f"{s['step']}: {s['status']} ({s['count']})" for s in workflow["steps"]))

    def resolve_reference(self,ref,scopes):
        found=[]
        primary=PrimarySpecificationDocumentPolicy.sql_primary_predicate("d")
        for scope in scopes:
            with self.retriever._connect(scope) as conn,conn.cursor() as cur:
                identity_sql="sp.spec_code=%s AND d.file_name=%s" if ref.get("code") else "d.file_name ILIKE %s"
                identity_params=(ref["code"],ref["file_name"]) if ref.get("code") else ("%"+ref["file_name"],)
                cur.execute(f"""SELECT c.id AS content_id,c.document_id,c.content,c.content_hash,c.page_number,
                    c.section_id,d.file_name,sv.version_label,sp.spec_code,sp.canonical_name
                    FROM public.specifications sp JOIN public.specification_versions sv ON sv.specification_id=sp.id
                    JOIN public.documents d ON d.document_id=sv.document_id
                    JOIN public.contents c ON c.document_id=d.document_id
                    WHERE sp.project_code=%s AND {identity_sql} AND sv.branch_name='MAIN'
                      AND regexp_replace(lower(sv.version_label),'^(v|ver[.]?)','','i')=%s
                      AND c.section_id=%s
                      AND {primary}
                    ORDER BY sv.id,c.id LIMIT 17""",
                    (scope,*identity_params,ref["version"],ref["section"]))
                rows=[dict(r) for r in cur.fetchall()]
            # A duplicated version/filename is ambiguous; never choose one.
            if len({r["document_id"] for r in rows})!=1 or len(rows)>16:continue
            for row in rows:
                text=str(row["content"] or "")
                if not text or hashlib.sha256(text.encode()).hexdigest()!=row["content_hash"]:continue
                found.append(EvidenceItem(evidence_id="",scope=scope,source_type="TICKET_SPEC_CLAUSE",content=text,
                    content_id=row["content_id"],document_id=row["document_id"],file_name=row["file_name"],
                    specification_code=row["spec_code"],specification_name=row.get("canonical_name"),specification_version=row["version_label"],
                    page_number=row["page_number"],section_id=row["section_id"],metadata={
                        "source_authority":"POSTGRESQL","content_hash":row["content_hash"],
                        "ticket_reference":dict(ref),"link_semantics":"LITERAL_TICKET_CITATION",
                        "applicable_version_mapping_verified":False}))
        # Multiple engineering projects with the same literal reference are
        # not automatically the intended target.
        return found if len({i.scope for i in found})<=1 else []


def _source(text,limit=1600):
    text=str(text or "")
    omitted=len(text)>limit
    text=text[:limit]
    fence="`"*max(3,1+max((len(s) for s in re.findall(r"`+",text)),default=0))
    return fence+"text\n"+text+"\n"+fence+("\n（仅展示摘录，不能据此排除全文中的条件或例外。）" if omitted else "")


def _clean(value,limit=500):
    text=" ".join(str(value or "").replace("\\","").split())
    return text if len(text)<=limit else text[:limit].rstrip()+"…"


def _ticket_overview(item,*,detailed=True):
    md=item.metadata or {}
    summary=re.split(r"\s*(?:■对象文书|■対象文書|■对象シート|■対象シート|■对策案|■対策案)",str(md.get('summary') or '标题未登记'))[0]
    lines=[f"### {md.get('issue_key') or '未编号票据'} · {_clean(summary,180)}"]
    basics=[]
    for label,key in (("类型","record_type"),("状态","status_raw"),("优先级","priority_raw"),
                      ("负责人","assignee"),("软件版本","software_version"),("影响版本","affected_version"),
                      ("发布版本","release_plan_version"),("解决结果","resolution_raw")):
        value=_clean(md.get(key),120)
        if value:basics.append(f"{label}：{value}")
    if basics:lines.append("- "+"；".join(basics))
    if detailed:
        for label,key in (("现象","phenomenon"),("复现步骤","repro_steps"),("期望结果","expected_result"),
                          ("分析/根因","analysis_result"),("根因","root_cause"),("对策","countermeasure"),
                          ("效果确认","effect_result")):
            value=_clean(md.get(key),700 if key in {"repro_steps","analysis_result","countermeasure"} else 420)
            if value:lines.append(f"- **{label}**：{value}")
    return "\n".join(lines)


def render_issue_query(result):
    """Formal, stable QA/BUG presentation without dumping raw export rows."""
    issues=[i for i in result.evidence if i.scope=="QABUG"]
    if not issues:return "目前没有找到匹配的QA/BUG记录。"
    exact=any(i.source_type=="ISSUE_EXACT" for i in issues)
    if exact:return _ticket_overview(issues[0],detailed=True)
    lines=[f"当前返回 {len(issues)} 条QA/BUG记录（这是本次结果页，不代表库内总数）："]
    for item in issues:
        lines.append(_ticket_overview(item,detailed=False))
    lines.append("如需完整清单，请指定工程、状态或时间范围后继续分页；查看单票详情或关联式样时，直接回复票号即可。")
    return "\n\n".join(lines)


def render_ticket_spec_trace(result):
    """Separate literal ticket citations from semantic specification candidates."""
    tickets=[i for i in result.evidence if i.scope=="QABUG"]
    explicit=[i for i in result.evidence if i.source_type=="TICKET_SPEC_CLAUSE"]
    candidates=[i for i in result.evidence if i.scope!="QABUG" and i.source_type!="TICKET_SPEC_CLAUSE"]
    focused=bool((result.workflow or {}).get("ticket_analysis_requested") or
                 (result.workflow or {}).get("test_design_requested"))
    lines=[]
    if tickets:
        # Analysis/test follow-ups need the ticket as context, not a second
        # full copy of every imported field.
        lines.extend(["## 票据背景",_ticket_overview(tickets[0],detailed=not focused)])
    if focused:
        explicit=[i for i in result.evidence if i.source_type=="TICKET_SPEC_CLAUSE"]
        unresolved=list((result.workflow or {}).get("unresolved_references") or [])
        if explicit or unresolved:
            lines.append("\n## 票内明确引用")
            for item in explicit[:5]:
                loc=" / ".join(x for x in (f"section={item.section_id}" if item.section_id else "",
                    f"content_id={item.content_id}" if item.content_id is not None else "",
                    f"page={item.page_number}" if item.page_number is not None else "") if x)
                lines.append(f"- {item.specification_code or '未编号'}《{item.specification_name or item.file_name or '式样'}》"
                             +(f"（{loc}）" if loc else ""))
            for ref in unresolved[:5]:
                anchor=" / ".join(x for x in (
                    f"version={ref.get('version')}" if ref.get('version') else "",
                    f"section={ref.get('section')}" if ref.get('section') else "",
                    f"page={ref.get('page')}" if ref.get('page') is not None else "") if x)
                lines.append(f"- {ref.get('file_name') or '未命名式样'}"
                             +(f"（{anchor}）" if anchor else "")+
                             "：票内明确记载，但当前库中尚未精确匹配到该版本正文。")
        if tickets and (result.workflow or {}).get("ticket_analysis_requested"):
            md=tickets[0].metadata or {}
            conclusion=_clean(md.get("analysis_result") or md.get("root_cause") or md.get("final_answer") or md.get("answer"),900)
            rationale=_clean(md.get("rationale"),500)
            if conclusion or rationale:
                lines.append("\n## 票据记录的历史判断")
                if conclusion: lines.append(f"- 记录内容：{conclusion}")
                if rationale: lines.append(f"- 记录依据：{rationale}")
                lines.append("- 这是票据中的历史记录，不代表系统已经用当前式样正文独立确认。")
        return "\n".join(lines)
    lines.append("\n## 票内明确引用")
    unresolved=list((result.workflow or {}).get("unresolved_references") or [])
    if explicit:
        for item in explicit:
            loc=" / ".join(x for x in (f"section={item.section_id}" if item.section_id else "",
                f"content_id={item.content_id}" if item.content_id is not None else "",
                f"page={item.page_number}" if item.page_number is not None else "") if x)
            lines.append(f"- **{item.specification_code or '未编号'} · {item.file_name or item.specification_name or '式样正文'}**"
                         +(f"（{loc}）" if loc else "")+f"\n  {_clean(item.content,500)}")
    if unresolved:
        for ref in unresolved[:8]:
            anchor=" / ".join(x for x in (
                f"version={ref.get('version')}" if ref.get('version') else "",
                f"section={ref.get('section')}" if ref.get('section') else "",
                f"page={ref.get('page')}" if ref.get('page') is not None else "") if x)
            lines.append(f"- **{ref.get('file_name') or '未命名式样'}**"
                         +(f"（{anchor}）" if anchor else "")+
                         "：票内明确记载，但当前库中尚未精确匹配到该版本正文。")
    if not explicit and not unresolved:
        lines.append("- 票内没有解析到同时包含式样文件、版本和章节位置的明确引用。")
    lines.append("\n## 根据票据内容检索出的相关式样候选")
    if candidates:
        groups={}
        for item in candidates:
            if item.score is not None and float(item.score) <= 0:
                continue
            group=(item.specification_code or "未编号",item.specification_name or item.file_name or "式样")
            groups.setdefault(group,[]).append(item)
        for (code,name),group_items in list(groups.items())[:8]:
            best=max((float(i.score or 0) for i in group_items),default=0)
            lines.append(f"### {code} · {name}（候选相关度最高 {best:.3f}）")
            seen=set()
            for item in group_items[:3]:
                key=(item.content_id,item.section_id)
                if key in seen:continue
                seen.add(key)
                loc=" / ".join(x for x in (f"section={item.section_id}" if item.section_id else "",
                    f"content_id={item.content_id}" if item.content_id is not None else "",
                    f"page={item.page_number}" if item.page_number is not None else "") if x)
                score=f"相关度={float(item.score):.3f}" if item.score is not None else ""
                lines.append(f"- 位置：{loc or '位置未登记'}"+(f"；{score}" if score else "")+f"\n  内容：{_clean(item.content,420)}")
    else:
        lines.append("- 当前没有检索到可定位的式样正文候选。")
    lines.append("\n> 候选结果来自票据的现象、复现步骤、分析结果和对策，与票内明确引用不是一回事；未经人工确认前不视为正式依赖关系。")
    return "\n".join(lines)


def render_symptom_workflow(result):
    """Tester-facing symptom results, grouped by tickets and specifications."""
    w=result.workflow or {}
    tickets=[i for i in result.evidence if i.scope=="QABUG"]
    specs=[i for i in result.evidence if i.scope!="QABUG"]
    lines=["## 现象检索结果"]
    if w.get("symptom_query"):lines.append(f"检索现象：{_clean(w['symptom_query'],500)}")
    question=str(w.get("original_question") or "")
    if re.search(r"(?:一定是\s*BUG|判断.*BUG|判斷.*BUG|下一步最小)",question,re.I):
        lines.extend(["\n## 判断", "当前结果不能证明该现象一定是BUG。相似历史票只证明现象值得核对，语义相关式样也不是适用条款确认。",
            "\n## 下一步最小确认步骤",
            "1. 固定工程、软件版本、车型/地域及用户切换前后的连接状态。",
            "2. 在最高相关式样位置核对完整条款、适用条件和例外。",
            "3. 按同一条件复现一次并保存日志；将实测结果与条款逐项对照后，再判断BUG或仕様通り。"])
    lines.append("\n## 相似历史QA/BUG")
    if not tickets:
        lines.append("- 当前没有找到相似历史票；这不代表历史库中一定不存在。")
    else:
        seen=set()
        for item in tickets:
            md=item.metadata or {};key=str(md.get("issue_key") or md.get("record_key") or "未编号")
            if key in seen:continue
            seen.add(key)
            summary=_clean(md.get("summary") or item.content,220)
            status=_clean(md.get("status_raw") or md.get("status_norm"),60)
            version=_clean(md.get("software_version") or md.get("affected_version"),100)
            score=f"{float(item.score):.3f}" if item.score is not None else "-"
            lines.append(f"### {key} · {summary}\n- 状态：{status or '-'}；软件版本：{version or '-'}；相关度：{score}")
            if len(seen)>=5:break
    lines.append("\n## 应优先核对的式样内容")
    if not specs:
        lines.append("- 当前没有定位到可核验的式样正文片段。")
    else:
        groups={}
        for item in specs:
            group=(item.specification_code or "未编号",item.specification_name or item.file_name or "式样")
            groups.setdefault(group,[]).append(item)
        for (code,name),items in list(groups.items())[:6]:
            lines.append(f"### {code} · {name}")
            seen=set()
            for item in items[:3]:
                key=(item.content_id,item.section_id)
                if key in seen:continue
                seen.add(key)
                loc=" / ".join(x for x in (f"section={item.section_id}" if item.section_id else "",
                    f"content_id={item.content_id}" if item.content_id is not None else "",
                    f"page={item.page_number}" if item.page_number is not None else "") if x)
                score=f"；相关度={float(item.score):.3f}" if item.score is not None else ""
                lines.append(f"- 位置：{loc or '位置未登记'}{score}\n  内容：{_clean(item.content,360)}")
    lines.append("\n> 相似票和式样片段用于定位与分析，不自动证明当前现象属于同一根因，也不直接判定BUG或仕様通り。")
    return "\n".join(lines)


def render_testing_workflow(result):
    w=result.workflow
    if w.get("kind")=="PRE_TICKET_CHECK":
        return render_symptom_workflow(result)
    if w.get("kind")=="TICKET_SPEC_TRACE":
        return render_ticket_spec_trace(result)
    labels={"POSSIBLE_DUPLICATES":"找到需要核对的相似历史票，已停止式样兜底查询。",
        "POSSIBLE_DUPLICATES_WITH_SPEC_CANDIDATES":"找到需要核对的相似历史票，并继续返回相关式样片段候选。",
        "SPEC_EVIDENCE_REVIEW_REQUIRED":"本次未找到达到相似度阈值的历史票，已继续查询相关式样。",
        "REFERENCE_RESOLVED":"票内明确引用已匹配到对应版本的式样正文。",
        "REFERENCE_INCOMPLETE":"票据引用尚未全部匹配到式样正文。",
        "PROJECT_REQUIRED":"需要明确工程（21MM/24MM/COMMON）后才能定位式样。",
        "BUDGET_EXCEEDED":"本次检索达到时间预算，结果不完整。",
        "CANCELLED":"本次检查已取消。","SOURCE_UNAVAILABLE":"部分数据来源不可用，检查未完成。",
        "INSUFFICIENT_EVIDENCE":"本次未取得足够证据。"}
    labels["CONFORMANCE_ASSESSED"]="已完成适用版本与已配置条件的核查。"
    step_names={"DUPLICATE_DISCOVERY":"历史票候选检索","SPEC_FALLBACK":"式样查询",
        "ISSUE_EXACT":"票据读取","EXPLICIT_REFERENCE":"票内引用识别","REFERENCE_BODY":"引用正文核验"}
    step_names["CONTENT_RELATION_EXPANSION"]="已确认内容关系扩展"
    step_names["CONFORMANCE_CHECK"]="条件与实测值核查"
    statuses={"DONE":"完成","STARTED":"开始","SKIPPED_POSSIBLE_DUPLICATE":"已跳过（存在相似票）",
        "PROJECT_REQUIRED":"需要选择工程","NO_UNAMBIGUOUS_ANCHOR":"缺少明确定位"}
    steps=" → ".join(step_names.get(s["step"],s["step"])+"："+statuses.get(s["status"],s["status"])
        +(f"（{s['count']}条）" if s["status"]=="DONE" else "") for s in w.get("steps",[]))
    lines=["我先查了相似的历史问题，再查看了与现象相关的式样内容。"+labels.get(w.get("state"),"检查未完成。"),
        "\n相似结果只代表值得核对，不代表是同一个问题或同一个根因；没有命中也不等于历史上一定不存在。",
        "\n当前能给出的是相关内容和位置，暂时不能据此认定现象一定是 BUG 或一定符合式样。"]
    if w.get('kind')=='CONFORMANCE_CHECK':
        lines[1]='\n以下观测值来自你的描述或票内明确字段，尚未单独验证观测本身。'
    if w.get("assessment"):
        assessment=w["assessment"]
        lines[2]="\n式样符合性：**"+assessment["label"]+"**。"+assessment.get("reason","")
        lines.append("\n核查仅覆盖已审核配置与提供的观测，不代表整本式样全部符合，也不证明观测本身真实。")
        checks=assessment.get("checks") or []
        if checks:
            lines.append("\n核查要点：")
            for check in checks[:8]:
                if isinstance(check,dict):
                    label=check.get("label") or check.get("name") or check.get("requirement") or "检查项"
                    outcome=check.get("result") or check.get("status") or check.get("reason") or "待核对"
                    lines.append(f"- {label}：{outcome}")
        if assessment["verdict"]=="UNDETERMINED":lines.append("\n补充信息格式：式样编号=目标编号；软件版本=实际版本；车型=实际车型；地域=实际地域；以及检查所需的实测值和条件。")
    # A conformance answer is a decision aid. Raw chunks and internal JSON are
    # available through diagnostics, but should not flood normal chat.
    evidence_items=[] if w.get('kind')=='CONFORMANCE_CHECK' else result.evidence
    for item in evidence_items:
        locator={"工程":item.scope,"式样编号":item.specification_code,"文件":item.file_name,"版本":item.specification_version,
            "章节":item.section_id,"页码":item.page_number,"content_id":item.content_id,
            "Sheet":item.metadata.get("sheet_name"),"行":item.metadata.get("row_number"),
            "SHA256":item.metadata.get("content_hash"),"票号":item.metadata.get("issue_key"),
            "票状态":item.metadata.get("status_raw") or item.metadata.get("status_norm"),
            "软件版本":item.metadata.get("software_version") or item.metadata.get("affected_version"),
            "优先级":item.metadata.get("priority_raw") or item.metadata.get("priority_norm"),
            "相似度（非概率）":item.score}
        lines.extend([f"\n[{item.evidence_id}] "+("票据原文/候选" if item.scope=="QABUG" else "式样原文"),
            _source(" | ".join(f"{k}={v}" for k,v in locator.items() if v is not None and v!=""),limit=1000),_source(item.content)])
    if w.get("unresolved_references"):
        import json
        lines.extend(["\n未解析引用（不能视为条款不存在）：",_source(json.dumps(w["unresolved_references"],ensure_ascii=False),2000)])
    if result.warnings:lines.append("\n提示：部分检索未完成，当前结果仅供核查。")
    return "\n".join(lines)
