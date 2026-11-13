from dataclasses import replace
from types import SimpleNamespace
import pytest
from app.ai.request_understanding import understand, review
from app.ai.answer_policy import contract, MESSAGES
from app.ai.rag_service import EnterpriseRAGService
from app.ai.config import AISettings
from test_answer_policy_regressions import service,evidence,run

HISTORY=[{"role":"user","content":"21MM 508 User Profile"},
 {"role":"assistant","content":"已定位508"},
 {"role":"user","content":"登录车机用户的流程是什么"},
 {"role":"assistant","content":"用户识别与配置加载"},
 {"role":"user","content":"假如你来验收这一块，会从哪些地方下手？"}]

def plan(task="TEST_DESIGN"):
 return {"domain":"SPEC","task":task,"subject":"用户识别与配置加载","subject_turn":2,"entity_turn":0,
 "deliverables":["提出验证动作和观察点"],"completion_checks":["回答可执行的检查而非重新讲流程"]}

@pytest.mark.parametrize("task",["TEST_DESIGN","PROCESS","ANALYSIS","COMPARE","QUOTE","RELATIONS"])
def test_work_and_entity_are_independent(task):
 captured=[]
 def chat(**kw):captured.append(kw);return plan(task)
 result,source=understand(SimpleNamespace(chat_json=chat),HISTORY,model="fake")
 assert result.task==task and source==HISTORY[0]["content"]
 assert result.completion_checks
 assert "conversational" in captured[0]["messages"][0]["content"]

@pytest.mark.parametrize("field,value",[("task","RUN_SQL"),("subject_turn",1),("entity_turn",3),("subject_turn",True),("deliverables",[]),("completion_checks",[]),("subject",""),("entity_turn",99)])
def test_invalid_contract_is_not_executed(field,value):
 data=plan();data[field]=value
 with pytest.raises(ValueError):understand(SimpleNamespace(chat_json=lambda **kw:data),HISTORY,model="fake")

def test_semantic_deliverables_control_answer_instead_of_process_keyword():
 text=contract("结合登录流程给我一些想法",task="TEST_DESIGN",request=plan())
 assert "completion checks" in text and "提出验证动作" in text
 assert "Numbered execution steps" not in text


@pytest.mark.parametrize("question",[
 "假如你来验收这一块，会从哪些地方下手？",
 "这个用户切换流程偶发失败，你会怎么试？",
 "What experiments would you try on that behavior?",
 "この動作の弱いところをどうやって確かめますか",
])
@pytest.mark.parametrize("domain",["SPEC","GENERAL"])
def test_real_prepare_uses_semantic_subject_and_work(question,domain):
 svc=EnterpriseRAGService.__new__(EnterpriseRAGService)
 svc.settings=AISettings(num_ctx=8192)
 data=plan();data["domain"]=domain
 requests=[]
 def chat(**kwargs):
  requests.append(kwargs["messages"])
  if "Select the minimum read operations" in requests[-1][0]["content"]:
   return {"operations": ["body"], "terms": ["User Profile"]}
  return data
 svc.ollama=SimpleNamespace(chat_json=chat)
 queries=[]
 def body(q,**kw):
  queries.append(q)
  from app.ai.retrieval import RetrievalResult
  return RetrievalResult(evidence=(evidence(),),resolved_scopes=("21MM",),route="SPEC_SUMMARY")
 svc.retriever=SimpleNamespace(**{
  f"retrieve_spec_{name}": body
  for name in ("catalog", "lookup", "outline", "chapter_content", "version_facts", "relations")
 })
 svc._renumber_governed=lambda raw,*a,**kw:raw
 messages=[*HISTORY[:-1],{"role":"user","content":question}]
 prepared,result=svc.prepare(messages,model="fake")
 assert result.route=="SPEC_MODEL_ANSWER", result.warnings
 assert result.workflow["request_contract"]["task"]=="TEST_DESIGN"
 assert "21MM" in queries[0] and "508" in queries[0]
 assert "用户识别与配置加载" in requests[-1][-1]["content"]


def test_answer_that_repeats_process_is_revised_before_release():
 svc,calls=service([evidence()],["1. H/U loads the associated Profile. [E1]",
 "Exploration suggestion: change the detected user during Profile loading and observe which identity is applied. Expected interruption behavior is to be confirmed. [E1]"])
 old_prepare=svc.prepare
 def prepare(*a,**kw):
  prepared,result=old_prepare(*a,**kw)
  return prepared,replace(result,workflow={"request_contract":plan()})
 svc.prepare=prepare
 verdicts=iter([{"task_fulfilled":False,"supported":True,"issues":["Repeated the process instead of proposing experiments."]},
 {"task_fulfilled":True,"supported":True,"issues":[]}])
 svc.ollama.chat_json=lambda **kw:next(verdicts)
 answer=run(svc,"What would you try on that behavior?")
 assert "Exploration suggestion" in answer and "1. H/U loads" not in answer
 assert len(calls)==2


def test_semantic_review_failure_never_releases_unreviewed_claims():
 svc,calls=service([evidence()],["The system always retries automatically. [E1]"])
 old_prepare=svc.prepare
 svc.prepare=lambda *a,**kw:([],replace(old_prepare(*a,**kw)[1],workflow={"request_contract":plan()}))
 svc.ollama.chat_json=lambda **kw:{"task_fulfilled":True,"supported":False,"issues":["No source supports automatic retry."]}
 assert run(svc,"What would you try?")==MESSAGES["en"]

def test_review_context_overflow_does_not_silently_drop_evidence():
 called=[]
 accepted,issues=review(SimpleNamespace(chat_json=lambda **kw:called.append(kw)),model="fake",contract=plan(),question="try?",draft="draft",blocks=["x"*9000],num_ctx=1000,estimate=len,keep_alive="5m")
 assert not accepted and not called


def test_spec_review_requires_explicit_source_audit():
 verdict = {"task_fulfilled": True, "supported": True, "issues": [],
            "source_scope_overstated": False, "source_codes_expanded": False}
 client = SimpleNamespace(chat_json=lambda **kwargs: verdict)
 args = dict(model="fake", contract=plan(), question="What does this term mean?",
             draft="The supplied section does not define the term.",
             blocks=["[E1] Related text."], num_ctx=4096, estimate=len,
             keep_alive="5m", strict_schema=True)
 assert review(client, **args) == (True, [])
 for flag in ("source_scope_overstated", "source_codes_expanded"):
  verdict[flag] = True
  assert not review(client, **args)[0]
  verdict[flag] = False
 del verdict["source_scope_overstated"]
 assert not review(client, **args)[0]


def test_semantic_quote_does_not_depend_on_quote_keywords():
 svc,calls=service([evidence()],route="SPEC_CHAPTER_CONTENT")
 old=svc.prepare
 svc.prepare=lambda *a,**kw:([],replace(old(*a,**kw)[1],workflow={"request_contract":plan("QUOTE")}))
 assert "Body ECU detects Memory ID" in run(svc,"Show exactly what it says there")
 assert not calls


def test_domain_is_interpreted_instead_of_assuming_every_bug_word_is_a_ticket():
 data=plan();data["domain"]="SPEC"
 result,_=understand(SimpleNamespace(chat_json=lambda **kw:data),HISTORY,model="fake")
 assert result.domain=="SPEC"
