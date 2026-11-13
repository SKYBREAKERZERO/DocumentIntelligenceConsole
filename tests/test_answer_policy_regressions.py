from types import SimpleNamespace
import pytest
from app.ai.answer_policy import language, intent, MESSAGES
from app.ai.rag_service import EnterpriseRAGService
from app.ai.retrieval import EvidenceItem, RetrievalResult
from app.ai.agent import FastIntentRouter

@pytest.mark.parametrize("q,expected", [
 ("登录车机用户的流程是什么", "zh"), ("ログインの手順を説明", "ja"),
 ("Explain the login process", "en"), ("Explain Memory ID，用中文回答", "zh"),
 ("请用英文回答车机登录流程", "en"), ("Reply in Japanese: summarize Profile", "ja"),
 ("登录流程を日本語で説明", "ja"), ("Explain `日本語` profile loading", "en")])
def test_language(q,expected): assert language(q)==expected

@pytest.mark.parametrize("q", ["21MM 508 explain the process", "21MM 508 の手順を説明", "21MM 508 summarize the content", "21MM 508 要約してください"])
def test_routes(q): assert FastIntentRouter.classify(q).route == "SPEC_SUMMARY"

def evidence(doc="21MM:508@v1", version="v1", cid=8392):
 return EvidenceItem(evidence_id="1",scope="21MM",source_type="SPEC_CHAPTER_CONTENT",content="SQL body read model",document_id=doc,file_name="Profile.pdf",specification_version=version,metadata={"version_id":12,"excerpts":[{"section_id":"3.2.1","content_id":cid,"page_number":0,"title":"Detection of Memory ID flow","excerpt":"Body ECU detects Memory ID. H/U loads the associated Profile."}]})

def service(items=(),outputs=(),route="SPEC_SUMMARY"):
 svc=EnterpriseRAGService.__new__(EnterpriseRAGService)
 svc.settings=SimpleNamespace(num_ctx=8192,ollama_keep_alive="5m")
 result=RetrievalResult(evidence=tuple(items),resolved_scopes=("21MM",),route=route)
 svc.prepare=lambda *a,**k:([],result)
 svc._runtime_context_for=lambda r:8192
 calls=[]
 def stream(**kw):
  calls.append(kw)
  response=outputs[min(len(calls)-1,len(outputs)-1)]
  if isinstance(response,Exception): raise response
  yield response
 svc.ollama=SimpleNamespace(chat_stream=stream)
 return svc,calls

def run(svc,q): return svc.answer([{"role":"user","content":q}],model="fake")[0]

@pytest.mark.parametrize("q,draft",[
 ("登录车机用户的流程是什么","## 识别与配置加载\n1. Body ECU 判定 Memory ID，然后 H/U 加载对应用户配置。[E1]"),
 ("ログインの手順を説明してください","## 識別と設定の読み込み\n1. Body ECU が Memory ID を判定し、H/U が設定を読み込みます。[E1]"),
 ("Explain the login process","## Identification and Profile loading\n1. Body ECU determines Memory ID; H/U loads the associated Profile. [E1]")])
def test_real_publication(q,draft):
 svc,calls=service([evidence()],[draft])
 answer=run(svc,q)
 for value in ["content_id=8392","section=3.2.1","version=v1","document_id=21MM:508@v1","page=0"]: assert value in answer
 assert "[E1]" not in answer
 prompt=calls[0]["messages"][0]["content"]
 assert "account authentication" in prompt and "token issuance" in prompt and "Body ECU detects" in prompt

@pytest.mark.parametrize("draft",[
 "1. H/U loads Profile. [E999]", "1. H/U loads the associated Profile without a citation.",
 "1. [Row 1] Column A: Profile [E1]", "1. 系统完成用户配置加载以及对应设置处理。[E1]",
 "H/U loads the associated Profile. [E1]", RuntimeError("model unavailable")])
def test_quality_rejection(draft):
 svc,calls=service([evidence()],[draft])
 assert run(svc,"Explain the login process")==MESSAGES["en"]
 assert len(calls)==2

@pytest.mark.parametrize("route",["SPEC_CATALOG","SPEC_LOOKUP","SPEC_OUTLINE","SPEC_VERSION_FACTS","ISSUE_QUERY","ISSUE_STATS","SYSTEM_META","DIRECT_CHAT","PRE_TICKET_CHECK"])
def test_global_legacy_language(route):
 svc,calls=service(outputs=["The requested information is currently unavailable."],route=route)
 svc._answer_stream_impl=lambda *a,**k:iter(["当前没有可用的正文证据。"])
 assert run(svc,"Explain this result")=="The requested information is currently unavailable."
 assert "English" in calls[0]["messages"][0]["content"]

def test_cannot_drop_protected_locations():
 svc,_=service(outputs=["The system loads Profile."])
 svc._answer_stream_impl=lambda *a,**k:iter(["系统在5秒加载。[content_id=8392]"])
 assert run(svc,"Explain this process")==MESSAGES["en"]

def test_empty_body():
 svc,calls=service()
 assert run(svc,"要約してください")==MESSAGES["ja"]
 assert not calls

def test_qa_enterprise_answer_keeps_ticket_source_in_details():
 ticket = EvidenceItem(
  evidence_id="1", scope="QABUG", source_type="ISSUE_EXACT", content="",
  document_id="register-1", file_name="qa.xlsx",
  metadata={"issue_key": "QA-321", "summary": "Display restarts",
            "status_raw": "Closed", "countermeasure": "Software update planned"})
 svc, _ = service([ticket], route="ISSUE_QUERY")
 requests = []
 def chat_json(**kwargs):
  requests.append(kwargs)
  if "findings" in kwargs["schema"]["properties"]:
   return {"findings": [
    {"conclusion": "QA-321 records a planned software update.",
     "basis": "The record lists the update as planned. [E1]", "source": "1",
     "quote": "Software update planned"},
   ], "gaps": ["Fix verification"]}
  return {
  "task_fulfilled": True, "supported": True, "issues": [],
  "source_scope_overstated": False, "source_codes_expanded": False,
  }
 svc.ollama.chat_json = chat_json
 answer = run(svc, "Was QA-321 fixed?")
 conclusion, details = answer.split("<details", 1)
 assert "planned software update" in conclusion
 assert "document_id=register-1" in details and "Ticket excerpts" in details
 assert "Software update planned" in details
 assert "[E1]" not in answer
 assert "Unconfirmed by available evidence: Fix verification" in conclusion
 assert "do not equate closed with fixed" in requests[0]["messages"][0]["content"].lower()

def test_grounded_answer_rejects_passage_not_in_selected_evidence():
 from app.ai.grounded_answer import draft_from_evidence
 result = {"points": [{"topic": "Verification", "status": "supported",
                       "conclusion": "It was verified.", "basis": "Verification was recorded.",
                       "passage": "3.1"}]}
 client = SimpleNamespace(chat_json=lambda **kwargs: result)
 with pytest.raises(ValueError, match="supplied passage"):
  draft_from_evidence(client, model="fake", instruction="", request="", num_ctx=4096,
      sources={"1": "Verification is pending.", "2": "Verification succeeded."},
      keep_alive="5m", max_tokens=512, gap_label="Unconfirmed:")

def test_grounded_answer_accounts_for_an_omitted_named_variant():
 from app.ai.grounded_answer import draft_from_evidence
 result = {"points": [{"topic": "Type C", "status": "supported",
                       "conclusion": "Type C 使用中心服务。", "basis": "正文定义了中心服务。",
                       "passage": "1.1"}]}
 client = SimpleNamespace(chat_json=lambda **kwargs: result)
 conclusion, _, _ = draft_from_evidence(
  client, model="fake", instruction="", request="", num_ctx=4096,
  sources={"1": "Type C uses the Center."}, keep_alive="5m", max_tokens=512,
  gap_label="现有证据未能确认：", question="TYPE C 和 TYPE A在式样书中是什么意思")
 assert "现有证据未能确认： TYPE A" in conclusion

def test_quote_intent():
 assert intent("Show original verbatim")=="QUOTE"
 assert intent("Explain workflow")=="PROCESS"

def test_unverified_spec_answer_preserves_version_inventory():
 item = EvidenceItem(evidence_id="1", scope="21MM", source_type="SPEC_VERSION_FACTS",
                     content="", specification_code="508", metadata={
                         "inventory_only": True, "identity_verified": True,
                         "versions": [{"version_label": "v2.52", "file_name": "Profile.docx"}],
                     })
 result = RetrievalResult(evidence=(item,), resolved_scopes=("21MM",), route="SPEC_MODEL_ANSWER",
                          workflow={"spec_parts": [{"route": "SPEC_VERSION_FACTS", "indices": [0]}]})
 svc, _ = service()
 answer = svc._spec_evidence_without_conclusion(result, "zh", {}, {}, [])
 assert "v2.52" in answer.split("<details")[0]
 assert "Profile.docx" in answer.split("<details")[1]

def test_version_binding():
 svc,_=service([evidence(),evidence("21MM:508@v2","v2",9000)],["The first version records detection. [E1]\n\nThe second version records detection. [E2]"])
 answer=run(svc,"Analyze these versions")
 for value in ["version=v1","content_id=8392","version=v2","content_id=9000"]: assert value in answer

def test_cancel():
 svc,calls=service([evidence()])
 assert not list(svc.answer_stream([{"role":"user","content":"Explain process"}],model="fake",cancel_check=lambda:True))
 assert not calls

@pytest.mark.parametrize("question", ["Explain the process", "手順を説明してください", "总结一下内容"])
def test_followup_keeps_selected_specification(question):
 messages=[{"role":"user","content":"21MM 508"},{"role":"assistant","content":"Selected"},{"role":"user","content":question}]
 contextual=FastIntentRouter.contextualize(question,messages)
 assert "508" in contextual
 assert FastIntentRouter.classify(contextual).route=="SPEC_SUMMARY"

@pytest.mark.parametrize("q,heading", [("Show original verbatim", "Source body excerpts"),("请给出原文", "正文原文节选"),("原文引用してください", "本文の原文抜粋")])
def test_original_quote_keeps_source_language(q,heading):
 svc,calls=service([evidence()],route="SPEC_CHAPTER_CONTENT")
 answer=run(svc,q)
 assert heading in answer and "Body ECU detects Memory ID" in answer
 assert "content_id=8392" in answer
 assert not calls

def test_mixed_technical_terms_do_not_override_chinese_question():
 assert language("请总结仕様書中的Profile和Memory ID")=="zh"

def test_language_repair_preserves_locations_and_values():
 svc,calls=service(outputs=["The system loads the Profile in @@KEEP0@@ seconds. @@KEEP1@@"])
 svc._answer_stream_impl=lambda *a,**k:iter(["系统在5秒加载。[content_id=8392]"])
 assert run(svc,"Explain this process")=="The system loads the Profile in 5 seconds. [content_id=8392]"

def test_cover_sheet_cannot_be_used_as_body():
 item=evidence()
 item.metadata["document_role"]="COVER_SHEET"
 svc,calls=service([item])
 assert run(svc,"Explain the process")==MESSAGES["en"]
 assert not calls


def test_long_body_copy_is_not_a_summary():
 from app.ai.answer_policy import validate_draft
 body="The configured user profile is loaded by the head unit after the body controller selects the memory identifier. "*4
 assert not validate_draft(body+" [E1]", {"1"}, "Summarize this specification", (body,))

def test_short_uncited_process_step_rejected():
 from app.ai.answer_policy import validate_draft
 assert not validate_draft("1. Load the Profile. [E1]\n2. Connect.", {"1"}, "Explain the process")

def test_translation_cannot_add_numeric_claims():
 svc,_=service(outputs=["The system loads the Profile in @@KEEP0@@ seconds, with 99 retries. @@KEEP1@@"])
 svc._answer_stream_impl=lambda *a,**k:iter(["系统在5秒加载。[content_id=8392]"])
 assert run(svc,"Explain this process")==MESSAGES["en"]
