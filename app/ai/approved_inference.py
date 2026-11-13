"""Explicitly approved inference connection; endpoint and key stay in OS keyring."""
from __future__ import annotations
import json
import logging
import re
import ssl
from pathlib import Path
from urllib.parse import urlsplit
import requests
from app.ai.ollama_client import OllamaClient, OllamaModel
from app.enterprise.credential_store import get_secret, set_secret

ALIAS = "approved-vpn"
PROFILE_FILE = "approved_inference.json"

class InferenceError(RuntimeError):
    """Safe machine code; never retain a raw response or connection exception."""
    def __init__(self,message,*,code="PRIVATE_INFERENCE_ERROR",http_status=None):
        super().__init__(message)
        self.code=code
        self.http_status=http_status


DIAGNOSTICS = {
    "ENDPOINT_FORMAT":"接口地址格式不正确。请填写基础地址，不要带聊天接口结尾、账号、查询参数或密钥。",
    "TLS_CERTIFICATE":"服务器证书未通过验证。可能需要公司 CA，也可能是证书过期、名称或链不匹配。请向管理员确认。",
    "TLS_HANDSHAKE":"TLS 连接未建立，请检查服务器协议、证书或是否要求客户端证书。",
    "CONNECT_TIMEOUT":"连接超时。VPN 已连接仍可能没有该服务的路由或端口权限。",
    "RESPONSE_TIMEOUT":"服务器响应超时，请稍后重试或确认模型服务状态。",
    "DNS":"当前进程无法解析服务器名称，请检查 VPN 的公司 DNS。",
    "NETWORK_ROUTE":"无法连接服务器，请检查 VPN 路由、端口及是否需要公司指定代理。",
    "AUTHENTICATION":"认证失败（401）。请确认密钥及服务器认证方式；密钥框只填密钥本身。",
    "FORBIDDEN":"服务器拒绝访问（403），请确认账号权限、VPN 访问策略。",
    "API_PATH":"接口不存在（404）。请确认是否缺少或重复了 /v1，或填成了网页地址。",
    "MODEL_ID":"服务器不接受当前模型 ID，请确认模型的实际登记名称。",
    "REQUEST_FORMAT":"服务器不接受请求参数（400/422），需要确认接口兼容方式。",
    "RATE_LIMIT":"服务器限流（429），请稍后重试。",
    "SERVER_ERROR":"模型服务返回服务器错误，请联系服务管理员。",
    "REDIRECT":"服务器要求跳转，未发送密钥到跳转地址。请使用管理员给出的最终 API 基础地址。",
    "INVALID_STREAM":"服务没有返回兼容的聊天数据，可能填了网页地址或接口格式不同。",
    "INCOMPLETE":"模型输出未正常完成，可能是输出预算或服务器思考模式设置。",
    "INVALID_JSON":"模型返回了文本，但未按要求返回 JSON；需确认模型或接口设置。",
    "CREDENTIAL_STORE":"无法写入 Windows 凭据库，连接信息未以明文保存。",
    "CONFIG_SAVE":"本机配置保存失败，已尝试恢复原凭据。",
    "PRIVATE_INFERENCE_ERROR":"操作未完成，请保留此错误代码进一步排查。",
}


def safe_diagnostic(exc,phase="probe"):
    code=getattr(exc,"code","PRIVATE_INFERENCE_ERROR")
    if not isinstance(code,str) or code not in DIAGNOSTICS:code="PRIVATE_INFERENCE_ERROR"
    status=getattr(exc,"http_status",None)
    return {"phase":phase if isinstance(phase,str) and phase in {"probe","save","reload"} else "probe",
            "code":code,"http_status":status if type(status) is int and 100<=status<=599 else None}


class _SystemTrustAdapter(requests.adapters.HTTPAdapter):
    def __init__(self,*args,**kwargs):
        # On Windows this includes the Windows ROOT/CA stores. TLS validation
        # and hostname checks remain enabled; no verify=False fallback.
        self._system_context=ssl.create_default_context()
        super().__init__(*args,**kwargs)
    def init_poolmanager(self,*args,**kwargs):
        kwargs["ssl_context"]=self._system_context
        return super().init_poolmanager(*args,**kwargs)
    def build_connection_pool_key_attributes(self,request,verify,cert=None):
        host,pool=super().build_connection_pool_key_attributes(request,verify,cert)
        if verify is True:pool["ssl_context"]=self._system_context
        return host,pool


def normalize_base_url(value: str) -> str:
    if not isinstance(value,str) or any(c.isspace() or ord(c)<32 for c in value):
        raise InferenceError("Invalid private inference endpoint",code="ENDPOINT_FORMAT")
    try:
        parts=urlsplit(value)
        if parts.scheme not in {"http","https"} or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError()
        if parts.port is not None and not 1<=parts.port<=65535: raise ValueError()
        if parts.path.rstrip("/").endswith(("/chat/completions","/models")):raise ValueError()
    except Exception:
        raise InferenceError("Invalid private inference endpoint",code="ENDPOINT_FORMAT") from None
    return value.rstrip("/")


def endpoint_from_form(value,scheme="https"):
    if not isinstance(value,str):raise InferenceError("Invalid private inference endpoint",code="ENDPOINT_FORMAT")
    value=value.strip()
    if "://" not in value:
        if scheme not in {"http","https"}:raise InferenceError("Invalid private inference endpoint",code="ENDPOINT_FORMAT")
        value=scheme+"://"+value
    return normalize_base_url(value)


def load_profile(directory: Path):
    path=Path(directory)/PROFILE_FILE
    if not path.exists():return None
    try:
        raw=json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw,dict) or raw.get("enabled") is not True:return None
        if set(raw)-{"enabled","alias","model","ca_bundle"}:raise ValueError()
        if raw.get("alias")!=ALIAS or not isinstance(raw.get("model"),str) or not raw["model"].strip() or len(raw["model"])>200:raise ValueError()
        if any(ord(c)<32 for c in raw["model"]):raise ValueError()
        if raw.get("ca_bundle") and not Path(raw["ca_bundle"]).is_file():raise ValueError()
        return raw
    except Exception:
        raise InferenceError("Private inference configuration is invalid") from None


def store_connection(directory: Path, *, base_url: str, api_key: str, model: str, ca_bundle: str=""):
    endpoint=normalize_base_url(base_url.strip() if isinstance(base_url,str) else "")
    if not isinstance(api_key,str) or not api_key or any(ord(c)<32 for c in api_key) or not isinstance(model,str) or not model.strip() or len(model)>200 or any(ord(c)<32 for c in model):
        raise InferenceError("Connection fields are incomplete or invalid")
    if ca_bundle and not Path(ca_bundle).is_file():raise InferenceError("Certificate file is unavailable")
    # No plaintext fallback. The account names contain no endpoint information.
    prior=get_secret("inference",ALIAS)
    sealed=json.dumps({"base_url":endpoint,"api_key":api_key},ensure_ascii=False)
    if not set_secret("inference",ALIAS,sealed):
        raise InferenceError("System credential store is unavailable",code="CREDENTIAL_STORE")
    try:
        path=Path(directory)/PROFILE_FILE
        path.parent.mkdir(parents=True,exist_ok=True)
        tmp=path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"enabled":True,"alias":ALIAS,"model":model.strip(),"ca_bundle":ca_bundle},ensure_ascii=False,indent=2),encoding="utf-8")
        tmp.replace(path)
    except Exception:
        set_secret("inference",ALIAS,prior)
        raise InferenceError("Private inference configuration could not be saved",code="CONFIG_SAVE") from None


def store_draft(*,base_url,api_key,model,ca_bundle=""):
    # Encrypted draft is separate from the active connection; a failed probe
    # must not activate the endpoint or force the user to retype next time.
    values={"base_url":base_url,"api_key":api_key,"model":model,"ca_bundle":ca_bundle}
    if any(not isinstance(v,str) for v in values.values()):return False
    try:return set_secret("inference",ALIAS+".draft",json.dumps(values,ensure_ascii=False))
    except Exception:return False


def load_draft():
    try:
        values=json.loads(get_secret("inference",ALIAS+".draft"))
        if not isinstance(values,dict) or set(values)!={"base_url","api_key","model","ca_bundle"}:return None
        if any(not isinstance(v,str) for v in values.values()):return None
        return values
    except Exception:return None


class _Redact(logging.Filter):
    def __init__(self, endpoint, key):
        super().__init__()
        host=urlsplit(endpoint).hostname or ""
        path=urlsplit(endpoint).path.rstrip("/")
        try:
            canonical=requests.Request("GET",endpoint).prepare().url or endpoint
            ascii_host=host.encode("idna").decode("ascii")
            canonical_path=urlsplit(canonical).path.rstrip("/")
        except Exception:
            canonical=endpoint;ascii_host=host;canonical_path=path
        self.values=sorted(set(x for x in (endpoint,key,host,ascii_host,canonical,path,canonical_path) if x),key=len,reverse=True)
    def filter(self,record):
        try:
            message=record.getMessage()
            for value in self.values:message=message.replace(value,"[PRIVATE]")
            record.msg=message;record.args=()
            # Tracebacks may include underlying requests exceptions with URLs.
            if record.exc_info:
                record.exc_info=None;record.exc_text=None
            record.stack_info=None
        except Exception:
            record.msg="[Private inference log redacted]";record.args=();record.exc_info=None;record.exc_text=None
        return True


def _install_redaction(endpoint,key):
    filt=_Redact(endpoint,key)
    loggers=[logging.getLogger(),logging.getLogger("urllib3"),logging.getLogger("urllib3.connectionpool"),logging.getLogger("requests")]
    for logger in loggers:
        logger.addFilter(filt)
        for handler in logger.handlers:handler.addFilter(filt)


class ApprovedOpenAIClient:
    def __init__(self, endpoint, api_key, *, connect_timeout=10, read_timeout=600, ca_bundle=""):
        self._endpoint=normalize_base_url(endpoint)
        if not isinstance(api_key,str) or not api_key or any(ord(c)<32 for c in api_key):raise InferenceError("Private inference credentials are missing")
        self._key=api_key
        self.timeout=(max(1,float(connect_timeout)),max(1,float(read_timeout)))
        self.verify=ca_bundle or True
        self.session=requests.Session();self.session.trust_env=False
        if not ca_bundle:self.session.mount("https://",_SystemTrustAdapter())
        _install_redaction(self._endpoint,self._key)
    def __repr__(self):return "<ApprovedOpenAIClient connection=[PRIVATE]>"
    def _request(self,method,path,**kwargs):
        try:
            response=self.session.request(method,self._endpoint+path,
                headers={"Authorization":"Bearer "+self._key,"Content-Type":"application/json"},
                timeout=self.timeout,verify=self.verify,allow_redirects=False,**kwargs)
            if not 200<=response.status_code<300:
                status=int(response.status_code)
                code={401:"AUTHENTICATION",403:"FORBIDDEN",404:"API_PATH",400:"REQUEST_FORMAT",422:"REQUEST_FORMAT",429:"RATE_LIMIT"}.get(status,
                    "REDIRECT" if 300<=status<400 else "SERVER_ERROR" if status>=500 else "PRIVATE_INFERENCE_ERROR")
                # Only map known machine codes; never copy provider messages.
                try:
                    raw=response.json()
                    error=raw.get("error") if isinstance(raw,dict) else None
                    machine=error.get("code") if isinstance(error,dict) else None
                    if machine in {"model_not_found","invalid_model","model_not_exist"}:code="MODEL_ID"
                except Exception:pass
                response.close()
                raise InferenceError(f"Private inference request failed (HTTP {status})",code=code,http_status=status)
            return response
        except InferenceError:raise
        except requests.exceptions.SSLError as exc:
            cert_failure="CERTIFICATE_VERIFY_FAILED" in str(exc) or "certificate verify failed" in str(exc).lower()
            raise InferenceError("Private inference TLS validation failed",code="TLS_CERTIFICATE" if cert_failure else "TLS_HANDSHAKE") from None
        except requests.exceptions.ConnectTimeout:
            raise InferenceError("Private inference connection timed out",code="CONNECT_TIMEOUT") from None
        except requests.exceptions.ReadTimeout:
            raise InferenceError("Private inference response timed out",code="RESPONSE_TIMEOUT") from None
        except requests.exceptions.ConnectionError as exc:
            text=str(exc).lower()
            dns=any(marker in text for marker in ("nameresolutionerror","getaddrinfo failed","failed to resolve","name or service not known"))
            raise InferenceError("Private inference connection failed",code="DNS" if dns else "NETWORK_ROUTE") from None
        except Exception:
            raise InferenceError("Private inference connection failed",code="NETWORK_ROUTE") from None
    def list_models(self):
        try:
            with self._request("GET","/models") as response:raw=response.json()
            return [OllamaModel(name=str(row["id"]),size=0) for row in raw.get("data",[]) if isinstance(row,dict) and isinstance(row.get("id"),str)]
        except InferenceError:raise
        except Exception:raise InferenceError("Private inference model list is unavailable") from None
    def chat_stream(self,*,model,messages,temperature=0.2,num_ctx=4096,cancel_check=None,keep_alive=None,think=False,max_tokens=None):
        del num_ctx,keep_alive,think
        if cancel_check and cancel_check():return
        if not isinstance(model,str) or not model.strip():raise InferenceError("No private inference model selected")
        if max_tokens is not None and (type(max_tokens) is not int or not 16<=max_tokens<=4096):raise InferenceError("Invalid generation budget")
        try:
            normalized=OllamaClient._normalize_messages(messages)
            body={"model":model,"messages":normalized,"stream":True,"temperature":float(temperature)}
        except Exception:
            raise InferenceError("Private inference request format is invalid") from None
        if max_tokens is not None:body["max_tokens"]=max_tokens
        chunks=[];size=0;finished=False
        try:
            with self._request("POST","/chat/completions",json=body,stream=True) as response:
                for raw in response.iter_lines():
                    if cancel_check and cancel_check():return
                    if not raw:continue
                    line=raw.decode("utf-8") if isinstance(raw,bytes) else raw
                    if line.startswith(":") or line.startswith(("event:","id:")):continue
                    if not line.startswith("data:"):raise InferenceError("Private inference returned invalid stream data",code="INVALID_STREAM")
                    value=line[5:].strip()
                    if value=="[DONE]":
                        if not finished:raise InferenceError("Private inference stream ended without a completion status")
                        break
                    payload=json.loads(value)
                    if payload.get("error"):raise InferenceError("Private inference rejected the request")
                    for choice in payload.get("choices",[]):
                        reason=choice.get("finish_reason")
                        if reason is not None:
                            if reason!="stop":raise InferenceError("Private inference response is incomplete or unsupported",code="INCOMPLETE")
                            finished=True
                        content=(choice.get("delta") or {}).get("content")
                        if content:
                            if not isinstance(content,str):raise InferenceError("Private inference content is invalid")
                            chunks.append(content);size+=len(content)
                            if size>64000:raise InferenceError("Private inference response exceeds size budget")
            if not finished:raise InferenceError("Private inference stream was interrupted",code="INCOMPLETE")
            text="".join(chunks)
            text=re.sub(r"<think>.*?</think>","",text,flags=re.I|re.S)
            text=re.sub(r"<think>.*$","",text,flags=re.I|re.S).strip()
            for private_value in (self._endpoint,self._key,urlsplit(self._endpoint).hostname or ""):
                if private_value:text=text.replace(private_value,"[PRIVATE]")
            if not text:raise InferenceError("Private inference returned no final answer")
            if not (cancel_check and cancel_check()):yield text
        except InferenceError:raise
        except requests.exceptions.ReadTimeout:
            raise InferenceError("Private inference response timed out",code="RESPONSE_TIMEOUT") from None
        except requests.exceptions.ConnectionError as exc:
            code="RESPONSE_TIMEOUT" if "read timed out" in str(exc).lower() else "NETWORK_ROUTE"
            raise InferenceError("Private inference stream connection failed",code=code) from None
        except Exception:raise InferenceError("Private inference stream failed",code="INVALID_STREAM") from None
    def chat_json(self,*,model,messages,temperature=0,num_ctx=4096,keep_alive=None,schema=None,max_tokens=1024):
        if schema is not None:
            messages = [
                {"role": "system", "content": "Return a JSON object matching this schema: " + json.dumps(schema, ensure_ascii=False)},
                *messages,
            ]
        try:
            text="".join(self.chat_stream(model=model,messages=messages,temperature=temperature,num_ctx=num_ctx,keep_alive=keep_alive,max_tokens=max_tokens))
            if text.startswith("```"):
                text=re.sub(r"^```(?:json)?\s*|\s*```$","",text,flags=re.I)
            value=json.loads(text)
            if not isinstance(value,dict):raise ValueError()
            return value
        except InferenceError:raise
        except Exception:raise InferenceError("Private inference returned invalid structured output",code="INVALID_JSON") from None


def make_client(settings):
    if getattr(settings,"inference_provider","ollama")=="approved_openai":
        try:
            sealed=json.loads(get_secret("inference",ALIAS))
            endpoint=sealed["base_url"];key=sealed["api_key"]
            if not isinstance(endpoint,str) or not isinstance(key,str) or not endpoint or not key:raise ValueError()
        except Exception:
            raise InferenceError("Private inference credentials are not configured") from None
        return ApprovedOpenAIClient(endpoint,key,connect_timeout=settings.ollama_connect_timeout,
            read_timeout=settings.ollama_read_timeout,ca_bundle=getattr(settings,"inference_ca_bundle",""))
    return OllamaClient(settings.ollama_url,connect_timeout=settings.ollama_connect_timeout,
        read_timeout=settings.ollama_read_timeout,first_load_timeout=settings.ollama_first_load_timeout,
        stream_idle_timeout=settings.ollama_stream_idle_timeout)
