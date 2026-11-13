import json,logging,traceback
from types import SimpleNamespace
from pathlib import Path
import pytest,requests
from app.ai import approved_inference as module
from app.ai.approved_inference import ApprovedOpenAIClient,InferenceError

BASE="https://private-unit.example.invalid/v1"
KEY="unit-test-key-not-real"
class Response:
 def __init__(self,events=(),status=200,payload=None):self.events=events;self.status_code=status;self.payload=payload;self.closed=False
 def __enter__(self):return self
 def __exit__(self,*a):self.close()
 def close(self):self.closed=True
 def iter_lines(self):
  for event in self.events:yield event.encode("utf-8")
 def json(self):return self.payload

def packet(content=None,reason=None,**extra):
 delta={}
 if content is not None:delta["content"]=content
 delta.update(extra)
 return "data: "+json.dumps({"choices":[{"delta":delta,"finish_reason":reason}]},ensure_ascii=False)

def client(response):
 c=ApprovedOpenAIClient(BASE,KEY);calls=[]
 def request(*a,**kw):calls.append((a,kw));return response
 c.session.request=request
 return c,calls

def stream(c,**kw):return "".join(c.chat_stream(model="Qwen3.6-35B-A3B",messages=[{"role":"user","content":"分析正文"}],**kw))

def test_api_adapter_preserves_existing_chat_contract():
 c,calls=client(Response([packet("正文分析"),packet(reason="stop"),"data: [DONE]"]))
 assert stream(c,max_tokens=512)=="正文分析"
 args,options=calls[0]
 assert args==("POST",BASE+"/chat/completions")
 assert options["headers"]["Authorization"]=="Bearer "+KEY
 assert options["allow_redirects"] is False and options["verify"] is True
 body=options["json"]
 assert body["model"]=="Qwen3.6-35B-A3B" and body["max_tokens"]==512
 assert "options" not in body and "keep_alive" not in body
 assert not c.session.trust_env

@pytest.mark.parametrize("events",[
 [packet("partial")], [packet("partial"),"data: [DONE]"],
 [packet("partial",reason="length")], [packet(reason="tool_calls")],
 ['data: {"error":{"message":"'+BASE+' '+KEY+'"}}'],["not an SSE stream"],['data: {bad json}']])
def test_incomplete_or_error_stream_never_releases_partial_text(events):
 c,_=client(Response(events))
 output=[]
 with pytest.raises(InferenceError) as exc:
  output.extend(c.chat_stream(model="m",messages=[{"role":"user","content":"test"}]))
 assert output==[]
 assert BASE not in str(exc.value) and KEY not in str(exc.value)

@pytest.mark.parametrize("status",[301,302,401,403,404,429,500])
def test_error_status_is_sanitized_and_redirect_not_followed(status):
 response=Response(status=status,payload={"url":BASE,"key":KEY})
 c,calls=client(response)
 with pytest.raises(InferenceError) as exc:stream(c)
 assert BASE not in str(exc.value) and KEY not in str(exc.value)
 assert response.closed and not calls[0][1]["allow_redirects"]


def test_private_connection_exception_has_no_url_or_key_in_traceback():
 c,_=client(Response())
 def fail(*a,**kw):raise requests.ConnectionError(BASE+KEY)
 c.session.request=fail
 try:stream(c)
 except InferenceError as exc:
  formatted="".join(traceback.format_exception(exc))
  assert BASE not in formatted and KEY not in formatted
 else:pytest.fail("Expected sanitized failure")


def test_reasoning_and_returned_connection_information_are_redacted():
 c,_=client(Response([packet(reasoning_content="private reasoning"),packet("<thi"),packet("nk>hidden</think>Final "+BASE+" "+KEY),packet(reason="stop")]))
 text=stream(c)
 assert "hidden" not in text and "private reasoning" not in text
 assert BASE not in text and KEY not in text and "Final" in text
 assert BASE not in repr(c) and KEY not in repr(c)


def test_structured_json_planning_uses_same_private_client():
 c,_=client(Response([packet('```json\n{"task":"TEST_DESIGN"}\n```'),packet(reason="stop")]))
 assert c.chat_json(model="m",messages=[{"role":"user","content":"test"}])=={"task":"TEST_DESIGN"}


def test_cancel_prevents_network_and_publication():
 c,calls=client(Response([packet("answer"),packet(reason="stop")]))
 assert stream(c,cancel_check=lambda:True)=="" and not calls

@pytest.mark.parametrize("url",["https://user:pass@private-unit.example.invalid/v1","https://private-unit.example.invalid/v1?key=x","https://private-unit.example.invalid/v1#x","file:///secret","https://private-unit.example.invalid/v1/chat/completions","https://private-unit.example.invalid/v1/models","https://private-unit.example.invalid:99999/v1","https://private-unit.example.invalid/\n"])
def test_invalid_secret_endpoint_is_rejected_without_echo(url):
 with pytest.raises(InferenceError) as exc:module.normalize_base_url(url)
 assert "private-unit" not in str(exc.value)


def test_save_keeps_both_secrets_out_of_plaintext_profile(tmp_path,monkeypatch):
 secrets={}
 monkeypatch.setattr(module,"get_secret",lambda kind,account:secrets.get(account,""))
 def store(kind,account,value):secrets[account]=value;return True
 monkeypatch.setattr(module,"set_secret",store)
 module.store_connection(tmp_path,base_url=BASE,api_key=KEY,model="Qwen3.6-35B-A3B")
 text=(tmp_path/module.PROFILE_FILE).read_text(encoding="utf-8")
 assert BASE not in text and KEY not in text
 sealed=json.loads(secrets[module.ALIAS])
 assert sealed["base_url"]==BASE and sealed["api_key"]==KEY
 assert module.load_profile(tmp_path)["model"]=="Qwen3.6-35B-A3B"


def test_no_plaintext_fallback_when_credential_store_fails(tmp_path,monkeypatch):
 monkeypatch.setattr(module,"get_secret",lambda *a:"")
 monkeypatch.setattr(module,"set_secret",lambda *a:False)
 with pytest.raises(InferenceError):module.store_connection(tmp_path,base_url=BASE,api_key=KEY,model="m")
 assert not (tmp_path/module.PROFILE_FILE).exists()


def test_logging_redacts_endpoint_key_and_traceback(caplog):
 module._install_redaction(BASE,KEY)
 with caplog.at_level(logging.WARNING):
  logging.getLogger("urllib3.connectionpool").warning("failure %s %s",BASE,KEY)
 assert BASE not in caplog.text and KEY not in caplog.text


def test_profile_cannot_embed_a_plaintext_url_or_key(tmp_path):
 (tmp_path/module.PROFILE_FILE).write_text(json.dumps({"enabled":True,"alias":module.ALIAS,"model":"m","base_url":BASE}),encoding="utf-8")
 with pytest.raises(InferenceError):module.load_profile(tmp_path)


def test_settings_keep_database_and_gateway_local_when_private_inference_enabled(tmp_path):
 from app.server.configuration import load_settings
 (tmp_path/module.PROFILE_FILE).write_text(json.dumps({"enabled":True,"alias":module.ALIAS,"model":"Qwen3.6-35B-A3B"}),encoding="utf-8")
 settings=load_settings(tmp_path)
 assert settings.inference_provider=="approved_openai"
 assert settings.chat_model=="Qwen3.6-35B-A3B"
 assert settings.api_host=="127.0.0.1" and settings.ollama_url.startswith("http://127.0.0.1")


def test_hot_reload_preserves_inflight_service_and_loaded_retriever(tmp_path,monkeypatch):
 import threading
 import app.server.runtime as runtime_module
 from app.ai.config import AISettings
 old_client=object();new_client=object();retriever=object()
 old_settings=AISettings(chat_model="old")
 old=SimpleNamespace(settings=old_settings,ollama=old_client,agent=SimpleNamespace(ollama=old_client),retriever=retriever)
 runtime=runtime_module.PersistentRAGRuntime.__new__(runtime_module.PersistentRAGRuntime)
 runtime.config_dir=tmp_path;runtime._lock=threading.RLock();runtime._instance=old;runtime.settings=old_settings
 monkeypatch.setattr(runtime_module,"load_settings",lambda p:AISettings(chat_model="new",inference_provider="approved_openai"))
 monkeypatch.setattr(module,"make_client",lambda s:new_client)
 assert runtime.reload_inference()["status"]=="APPLIED"
 assert runtime._instance is not old and runtime._instance.retriever is retriever
 assert old.ollama is old_client and old.agent.ollama is old_client and old.settings.chat_model=="old"
 assert runtime._instance.ollama is new_client and runtime._instance.agent.ollama is new_client


def test_private_reload_api_is_authenticated_and_does_not_accept_secrets():
 from fastapi.testclient import TestClient
 from app.server.api import create_app
 calls=[]
 runtime=SimpleNamespace(reload_inference=lambda:calls.append(True) or {"status":"APPLIED","provider":"approved_openai","model":"m"})
 token="local-unit-api-token-"+"x"*32
 with TestClient(create_app(runtime,token),base_url="http://127.0.0.1") as api:
  denied=api.post("/api/v1/inference/reload")
  assert denied.status_code==401 and not calls
  result=api.post("/api/v1/inference/reload",headers={"Authorization":"Bearer "+token})
  assert result.status_code==200 and result.json()["status"]=="APPLIED"
  assert BASE not in result.text and KEY not in result.text


def test_credential_pair_is_restored_if_profile_cannot_be_written(tmp_path,monkeypatch):
 secrets={module.ALIAS:'{"base_url":"previous","api_key":"previous"}'}
 original=secrets[module.ALIAS]
 monkeypatch.setattr(module,"get_secret",lambda kind,account:secrets.get(account,""))
 def store(kind,account,value):secrets[account]=value;return True
 monkeypatch.setattr(module,"set_secret",store)
 def fail(*a,**kw):raise OSError("write unavailable")
 monkeypatch.setattr(Path,"write_text",fail)
 with pytest.raises(InferenceError):module.store_connection(tmp_path,base_url=BASE,api_key=KEY,model="m")
 assert secrets[module.ALIAS]==original


@pytest.mark.parametrize("error,code",[
 (requests.exceptions.SSLError("CERTIFICATE_VERIFY_FAILED "+BASE+KEY),"TLS_CERTIFICATE"),
 (requests.exceptions.SSLError("handshake failed "+BASE),"TLS_HANDSHAKE"),
 (requests.exceptions.ConnectTimeout(BASE+KEY),"CONNECT_TIMEOUT"),
 (requests.exceptions.ReadTimeout(BASE),"RESPONSE_TIMEOUT"),
 (requests.exceptions.ConnectionError("getaddrinfo failed "+BASE),"DNS"),
 (requests.exceptions.ConnectionError("connection refused "+BASE),"NETWORK_ROUTE"),
])
def test_safe_network_diagnostic_codes(error,code):
 c,_=client(Response())
 def fail(*a,**kw):raise error
 c.session.request=fail
 with pytest.raises(InferenceError) as exc:stream(c)
 report=module.safe_diagnostic(exc.value)
 assert report["code"]==code
 text=json.dumps(report)
 assert BASE not in text and KEY not in text

@pytest.mark.parametrize("status,code",[(401,"AUTHENTICATION"),(403,"FORBIDDEN"),(404,"API_PATH"),(400,"REQUEST_FORMAT"),(429,"RATE_LIMIT"),(502,"SERVER_ERROR"),(302,"REDIRECT")])
def test_safe_http_diagnostic_codes(status,code):
 c,_=client(Response(status=status))
 with pytest.raises(InferenceError) as exc:stream(c)
 assert module.safe_diagnostic(exc.value)["code"]==code
 assert module.safe_diagnostic(exc.value)["http_status"]==status


def test_windows_trust_adapter_keeps_tls_and_hostname_validation_enabled():
 import ssl
 c,_=client(Response())
 adapter=c.session.get_adapter(BASE)
 assert isinstance(adapter,module._SystemTrustAdapter)
 assert adapter._system_context.verify_mode==ssl.CERT_REQUIRED
 assert adapter._system_context.check_hostname is True
 request=requests.Request("GET",BASE).prepare()
 host,pool=adapter.build_connection_pool_key_attributes(request,True)
 assert pool["ssl_context"] is adapter._system_context


def test_draft_is_encrypted_and_does_not_enable_active_profile(tmp_path,monkeypatch):
 vault={}
 monkeypatch.setattr(module,"get_secret",lambda kind,account:vault.get(account,""))
 def store(kind,account,value):vault[account]=value;return True
 monkeypatch.setattr(module,"set_secret",store)
 assert module.store_draft(base_url=BASE,api_key=KEY,model="model")
 assert module.load_draft()["base_url"]==BASE
 assert module.load_draft()["api_key"]==KEY
 assert module.ALIAS not in vault
 assert module.load_profile(tmp_path) is None
 assert not list(tmp_path.iterdir())


def test_safe_diagnostic_does_not_echo_arbitrary_exception_metadata():
 error=RuntimeError(BASE+KEY)
 error.code=[BASE,KEY];error.http_status=KEY
 report=module.safe_diagnostic(error,phase=[BASE])
 assert report=={"phase":"probe","code":"PRIVATE_INFERENCE_ERROR","http_status":None}


@pytest.mark.parametrize("value,scheme,expected",[
 ("private-unit.example.invalid/v1","https",BASE),
 ("192.0.2.1:1234","http","http://192.0.2.1:1234"),
 ("http://private-unit.example.invalid/v1","https","http://private-unit.example.invalid/v1"),
])
def test_form_protocol_selection_accepts_addresses_without_a_prefix(value,scheme,expected):
 assert module.endpoint_from_form(value,scheme)==expected
