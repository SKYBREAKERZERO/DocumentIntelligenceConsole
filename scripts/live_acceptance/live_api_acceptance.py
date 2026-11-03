"""Local API checks against the actual corpus; save bodies locally, print summaries."""
from pathlib import Path
import argparse,json,os,re,sys,time,urllib.request,urllib.error
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from app.server.configuration import user_configuration_dir
config=user_configuration_dir()
token=(config/"local_api_token.txt").read_text(encoding="utf-8-sig").strip()
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
OUTPUT=ROOT/"live_acceptance"
OUTPUT.mkdir(exist_ok=True)
parser=argparse.ArgumentParser()
parser.add_argument("--stage",choices=["structured","model"],default="structured")
parser.add_argument("--case",default="")
args=parser.parse_args()
stage=args.stage
cases=(
    ("semantic_spec","/api/v1/retrieval",{"question":"Bluetooth Audio playback silence","scope":"21MM","operation":"semantic"}),
    ("version_facts","/api/v1/retrieval",{"question":"21MM 110 ServiceList v2.51 到 v2.52 有哪些新增、删除、修改？","scope":"21MM","operation":"spec_version_facts"}),
    ("version_detail","/api/v1/retrieval",{"question":"21MM Multi Media system CAN Com spec 从 v2.02 到 v2.51 的版本变更，列出新增、删除、修改和前后原文","scope":"21MM","operation":"version_history"}),
    ("relations","/api/v1/retrieval",{"question":"21MM Bluetooth Audio function spec v2.52 与 Bluetooth 式样书的内容关联、引用及页码","scope":"21MM","operation":"engineering_relations"}),
    ("ticket_exact","/api/v1/retrieval",{"question":"查询 QA MM21SPMAN-88 及其明确引用的式样书","scope":"QABUG","operation":"qa_bug"}),
) if stage=="structured" else (
    ("phenomenon","/v1/chat/completions",{"model":"document-intelligence-rag","scope":"21MM","stream":False,"messages":[{"role":"user","content":"21MM 蓝牙音频在信号差或电池电量低时没有声音，能找到有关式样条款和原文位置吗？请给出处。"}]}),
    ("ollama_chat","/v1/chat/completions",{"model":"document-intelligence-rag","stream":False,"messages":[{"role":"user","content":"你好，只回复验收成功。"}]}),
    ("ticket_to_spec","/v1/chat/completions",{"model":"document-intelligence-rag","scope":"21MM","stream":False,"messages":[{"role":"user","content":"查询 QA MM21SPMAN-88。依据这张票内明确引用的 531 Connected Radio 式样书，找到对应正文条款并给出版本、章节、页码及引用。区分票内原话和检索候选，不推断根因。"}]}),
    ("pre_ticket","/v1/chat/completions",{"model":"document-intelligence-rag","scope":"21MM","stream":False,"messages":[{"role":"user","content":"21MM 蓝牙切歌后无声音，起 BUG 前先查 QA/BUG 库是否有相同现象；如果没有，再查询式样书有关内容并判断是否符合式样。没有完整证据时请明确说明，不要把未检索到等同于不存在。"}]}),
)
summaries=[]
if args.case:
    cases=tuple(c for c in cases if c[0]==args.case)
    if not cases:parser.error("Unknown case")
for name,path,payload in cases:
    print(json.dumps({"start":name,"stage":stage}),flush=True)
    start=time.monotonic()
    summary={"case":name,"stage":stage}
    req=urllib.request.Request("http://127.0.0.1:18080"+path,
       data=json.dumps(payload,ensure_ascii=False).encode(),method="POST",
       headers={"Authorization":"Bearer "+token,"Content-Type":"application/json"})
    try:
        with opener.open(req,timeout=180 if stage=="model" else 75) as r:
            data=json.load(r)
            summary["http_status"]=r.status
        (OUTPUT/(name+".json")).write_text(json.dumps(data,ensure_ascii=False,indent=2,default=str),encoding="utf-8")
        rag=data.get("rag",data)
        evidence=data.get("evidence",[])
        answer=(data.get("choices") or [{}])[0].get("message",{}).get("content","")
        summary.update(route=rag.get("route"),tools=rag.get("tools"),warnings=rag.get("warnings"),
          evidence_count=rag.get("evidence_count",len(evidence)),evidence_types=list(dict.fromkeys(e.get("type") for e in evidence)),
          answer_chars=len(answer),has_answer_content_locator=bool(re.search(r"content_id=\d+",answer)),
          structured_locator_count=sum(bool((e.get("metadata") or {}).get("content_id") and (e.get("page_number") is not None or e.get("section_id"))) for e in evidence),
          strength=rag.get("strength"),
          answer_blocked=any(x in answer for x in ("本次模型回答未通过","Local RAG error")),
          metadata_keys=sorted({k for e in evidence for k in (e.get("metadata") or {})}))
    except Exception as exc:
        summary.update(error_type=type(exc).__name__,http_status=getattr(exc,"code",None))
    summary["duration_ms"]=round((time.monotonic()-start)*1000)
    summaries.append(summary)
    print(json.dumps(summary,ensure_ascii=True),flush=True)
(OUTPUT/(stage+("_"+args.case if args.case else "")+"_results.json")).write_text(json.dumps(summaries,ensure_ascii=False,indent=2),encoding="utf-8")
