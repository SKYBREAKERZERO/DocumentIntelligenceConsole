from __future__ import annotations

import hmac
import logging
import os
import json
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Query
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field

from app.ai.retrieval import EvidenceItem, RetrievalResult
from app.ai.testing_workflow import TestingWorkflow
from app.server.runtime import PersistentRAGRuntime
from app.server.resilience import AdmissionController


MODEL_ID = "document-intelligence-rag"
MAX_BODY = 2 * 1024 * 1024
LOGGER = logging.getLogger("dic.backend")


def configured_chat_limit() -> int:
    """Explicit bounded limit; refuse a bad value rather than run unbounded."""
    raw = os.environ.get("DIC_MAX_ACTIVE_CHATS", "2").strip()
    try:
        count = int(raw)
    except ValueError as exc:
        raise ValueError("DIC_MAX_ACTIVE_CHATS must be an integer from 1 to 8") from exc
    if not 1 <= count <= 8:
        raise ValueError("DIC_MAX_ACTIVE_CHATS must be between 1 and 8")
    return count


class CompletionInput(BaseModel):
    model: str
    messages: list[dict[str, Any]] = Field(min_length=1)
    stream: bool = False
    scope: str = "AUTO"


class RetrievalInput(BaseModel):
    question: str = Field(min_length=1, max_length=10000)
    scope: str = "AUTO"
    operation: str = "spec_lookup"


class AssessmentInput(BaseModel):
    scope: str
    specification_code: str = Field(min_length=1,max_length=8)
    context: dict[str,str] = Field(max_length=4)
    observations: dict[str,Any] = Field(max_length=32)


def serialize_evidence(result: RetrievalResult) -> dict[str, Any]:
    return {
        "request_id": result.request_id,
        "route": result.route,
        "tools": list(result.tools),
        "scopes": list(result.resolved_scopes),
        "strength": result.evidence_strength,
        "warnings": list(result.warnings),
        **({"workflow":result.workflow} if result.workflow else {}),
        "evidence": [
            {
                "id": e.evidence_id, "scope": e.scope, "type": e.source_type,
                "citation": e.citation_label, "content": e.content,
                "specification_code": e.specification_code, "version": e.specification_version,
                "content_id": e.content_id, "document_id": e.document_id, "file_name": e.file_name,
                "section_id": e.section_id, "page_number": e.page_number,
                "metadata": e.metadata,
            }
            for e in result.evidence
        ],
    }


def create_app(runtime: PersistentRAGRuntime, token: str, *, max_active_chats: int | None = None, ragflow_compat: bool | None = None) -> FastAPI:
    if len(token) < 32:
        raise ValueError("API token must be at least 32 characters")
    app = FastAPI(title="Document Intelligence Local RAG API", version="1.20", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.runtime = runtime
    admissions = AdmissionController(max_active_chats if max_active_chats is not None else configured_chat_limit())
    factual_admissions = AdmissionController(2)
    app.state.chat_admissions = admissions
    app.state.factual_admissions = factual_admissions

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        # Validation errors may contain the entire input conversation. Return
        # only field diagnostics, never echo private ticket/document history.
        detail = [{"loc": list(e.get("loc", ())), "type": e.get("type"),
                   "msg": e.get("msg")} for e in exc.errors()]
        return JSONResponse(status_code=422, content={
            "error": {"message": "请求格式不正确，请检查消息内容。", "type": "invalid_request_error"},
            "detail": detail})

    @app.middleware("http")
    async def secure_request(request: Request, call_next: Callable):
        # Local Docker Desktop proxy uses the host-gateway address to connect.
        # Loopback binding is enforced separately at startup; Host is restricted
        # against DNS rebinding and accidental gateway/public deployments.
        host = (request.headers.get("host") or "").split(":", 1)[0].lower()
        if host not in {"127.0.0.1", "localhost", "host.docker.internal", "[::1]"}:
            return JSONResponse(status_code=403, content={"detail": "Nonlocal Host header prohibited"})
        try:
            content_length = int(request.headers.get("content-length") or "0")
        except ValueError:
            return JSONResponse(status_code=400, content={"detail": "Invalid content length"})
        if content_length < 0 or content_length > MAX_BODY:
            return JSONResponse(status_code=413, content={"detail": "Request too large"})
        if request.url.path.startswith(("/compat/ragflow/", "/api/v1/skills/", "/api/v1/testing/")):
            # Bound actual bytes, including a chunked request without Content-Length.
            parts, size = [], 0
            async for part in request.stream():
                size += len(part)
                if size > MAX_BODY:
                    return JSONResponse(status_code=413, content={"detail": "Request too large"})
                parts.append(part)
            request._body = b"".join(parts)
        return await call_next(request)

    def require_auth(authorization: str | None = Header(default=None)) -> None:
        supplied = authorization or ""
        if not hmac.compare_digest(supplied.encode(), ("Bearer " + token).encode()):
            raise HTTPException(status_code=401, detail="Authentication required")

    @app.post("/api/v1/testing/assess")
    def assess(body: AssessmentInput, _: None = Depends(require_auth)):
        if body.scope not in {"21MM","24MM"}:raise HTTPException(400,"Explicit engineering scope required")
        lease=factual_admissions.acquire()
        if lease is None:raise HTTPException(429,"Local retrieval busy",headers={"Retry-After":"3"})
        try:
            from app.ai.conformance import ConformanceService
            from app.ai.retrieval_deadline import retrieval_budget
            with retrieval_budget(25):
                result,items=ConformanceService(runtime.get_service().retriever).assess(body.scope,body.specification_code,body.context,body.observations)
            return {"assessment":result,"evidence":serialize_evidence(RetrievalResult(evidence=items,resolved_scopes=(body.scope,),route="CONFORMANCE_CHECK"))["evidence"]}
        except Exception:
            raise HTTPException(503,"Local configured checks unavailable") from None
        finally:lease.release()

    @app.get("/api/v1/health")
    def health(_: None = Depends(require_auth)):
        return runtime.local_status()

    @app.get("/v1/models")
    def models(_: None = Depends(require_auth)):
        return {"object": "list", "data": [
            {"id": MODEL_ID, "object": "model", "created": int(time.time()), "owned_by": "document-intelligence-local"}
        ]}

    @app.get("/api/v1/services")
    def services(_: None = Depends(require_auth)):
        # No network access when merely asking about configured state.
        return runtime.local_status()

    @app.get("/api/v1/runtime/metrics")
    def runtime_metrics(_: None = Depends(require_auth)):
        """Authenticated, no source data or credentials; metrics reset per process."""
        return {"mode": "LOCAL_ONLY", "chat": admissions.snapshot(), "factual": factual_admissions.snapshot()}

    @app.get("/api/v1/skills")
    def skills(_: None = Depends(require_auth)):
        from app.skills.catalog import list_skills
        return {"skills": list(list_skills()), "execution": "OFFLINE_SNAPSHOT_ONLY", "database_write": False}

    @app.post("/api/v1/skills/{skill_name}/evaluate")
    def evaluate_skill(skill_name: str, payload: dict[str, Any], _: None = Depends(require_auth)):
        from app.skills.executor import execute_skill, SkillInputError
        lease = factual_admissions.acquire()
        if lease is None:
            raise HTTPException(429, "Skill evaluation busy", headers={"Retry-After": "3"})
        try:
            result = execute_skill(skill_name, payload)
        except (SkillInputError, ValueError, KeyError, TypeError):
            lease.release(failed=True)
            raise HTTPException(400, "Invalid Skill or snapshot; consult the local Skill input contract") from None
        except Exception:
            lease.release(failed=True)
            raise HTTPException(503, "Skill evaluation unavailable") from None
        else:
            lease.release()
            return result

    @app.get("/api/v1/operations/receipts")
    def ingestion_receipts(limit: int = Query(20, ge=1, le=100), _: None = Depends(require_auth)):
        from app.enterprise.core_operations import recent_receipts
        return {"items": recent_receipts(runtime.config_dir / "ingestion_runs", limit),
                "policy": "LOCAL_ONLY", "running_state": "UNCONFIRMED_UNTIL_OPERATOR_CHECK"}

    @app.get("/api/v1/health/ready")
    def readiness(_: None = Depends(require_auth)):
        """A cheap state check, NOT a SQL/Qdrant/Ollama end-to-end probe."""
        state = runtime.local_status()
        embedding = state.get("embedding")
        ready = bool(runtime.preferred_model) and embedding == "READY"
        return JSONResponse(
            status_code=200 if ready else 503,
            content={"ready": ready, "embedding": embedding,
                     "rag_loaded": state.get("rag_loaded", False),
                     "dependency_checks": "NOT_PERFORMED"},
        )

    @app.get("/api/v1/health/dependencies")
    def dependency_health(_: None = Depends(require_auth)):
        return runtime.probe_dependencies()

    @app.get("/api/v1/health/knowledge")
    def knowledge_health(_: None = Depends(require_auth)):
        """Live per-scope SQL / vector status; does not expose source documents."""
        return runtime.probe_internal_knowledge()

    @app.post("/api/v1/retrieval")
    def retrieval(body: RetrievalInput, _: None = Depends(require_auth)):
        # Fixed read-only routes, not arbitrary SQL; user supplied scopes are
        # interpreted by the existing EnterpriseRetriever scope governance.
        allowed = {"AUTO", "21MM", "24MM", "COMMON", "QABUG", "QA/BUG", "ALL", "21MM + COMMON", "24MM + COMMON"}
        if body.scope not in allowed:
            raise HTTPException(status_code=400, detail="Invalid knowledge scope")
        retriever = runtime.get_service().retriever
        def governed_tool(tool: str) -> RetrievalResult:
            if body.scope not in {"21MM", "24MM"}:
                raise HTTPException(status_code=400, detail="Version/relation retrieval requires explicit 21MM or 24MM scope")
            return retriever.retrieve_agentic(
                question=body.question, scopes=(body.scope,), rewritten_queries=(body.question,),
                tools=(tool,), entities=(),
            )

        operations = {
            "testing_check": lambda: TestingWorkflow(retriever).run(body.question,scope=body.scope),
            "semantic": lambda: retriever.retrieve_qdrant_only(body.question, scope=body.scope, current_scope=""),
            "spec_catalog": lambda: retriever.retrieve_spec_catalog(body.question, scope=body.scope, current_scope=""),
            "spec_lookup": lambda: retriever.retrieve_spec_lookup(body.question, scope=body.scope, current_scope=""),
            "spec_outline": lambda: retriever.retrieve_spec_outline(body.question, scope=body.scope, current_scope=""),
            "spec_version_facts": lambda: retriever.retrieve_spec_version_facts(body.question, scope=body.scope, current_scope=""),
            "qa_bug": lambda: retriever.retrieve_issue_register(body.question, scope="QABUG", current_scope="", statistics=False),
            "qa_bug_statistics": lambda: retriever.retrieve_issue_register(body.question, scope="QABUG", current_scope="", statistics=True),
            "version_history": lambda: governed_tool("version_search"),
            "engineering_relations": lambda: governed_tool("relation_search"),
            "system_metadata": lambda: retriever.retrieve_system_meta(body.question, scope=body.scope, current_scope=""),
        }
        if body.operation not in operations:
            raise HTTPException(status_code=400, detail="Unsupported read-only operation")
        if body.operation in {"semantic","testing_check"}:
            if body.operation == "semantic" and body.scope not in {"21MM", "24MM", "COMMON"}:
                raise HTTPException(400, "Semantic diagnostics requires an explicit specification scope; use qa_bug for issue retrieval")
            lease = factual_admissions.acquire()
            if lease is None:
                raise HTTPException(429, "Local retrieval busy", headers={"Retry-After": "3"})
            try:
                result = operations[body.operation]()
            except Exception:
                lease.release(failed=True)
                raise HTTPException(503, "Local retrieval unavailable") from None
            else:
                lease.release()
                return serialize_evidence(result)
        if body.operation in {"qa_bug", "qa_bug_statistics"} and body.scope not in {"AUTO", "QABUG", "QA/BUG", "ALL"}:
            raise HTTPException(status_code=400, detail="QA/BUG operation requires an explicit compatible scope")
        result = operations[body.operation]()
        return serialize_evidence(result)

    @app.get("/api/v1/intelligence/requirements")
    def engineering_requirements(
        scope: str, spec_code: str, limit: int = Query(30, ge=1, le=100),
        _: None = Depends(require_auth),
    ):
        if scope not in {"21MM", "24MM"} or not spec_code.isalnum() or len(spec_code) > 20:
            raise HTTPException(400, "Explicit local specification scope/code required")
        retriever = runtime.get_service().retriever
        with retriever._connect(scope) as conn, conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.ei_requirement_units') AS table_name")
            if not (cur.fetchone() or {}).get('table_name'):
                return {"status": "SCHEMA_NOT_INSTALLED", "items": []}
            cur.execute(
                """
                SELECT su.unit_key,su.section_id,su.page_number,su.start_offset,su.end_offset,
                       su.unit_type,su.raw_text,su.signals,su.review_status,
                       su.content_id,sv.version_label
                FROM public.ei_requirement_units su
                JOIN public.specifications sp ON sp.id=su.specification_id
                JOIN public.specification_versions sv ON sv.id=su.version_id
                WHERE sp.spec_code=%s AND sp.project_code=%s AND su.version_id=sp.current_version_id
                ORDER BY su.content_id,su.start_offset LIMIT %s
                """, (spec_code,scope,limit)
            )
            return {"status":"OK", "scope":scope, "spec_code":spec_code,
                    "items":[dict(r) for r in cur.fetchall()]}

    @app.get("/api/v1/intelligence/content/{scope}/{content_id}")
    def engineering_content_location(
        scope: str, content_id: int, _: None = Depends(require_auth),
    ):
        """Resolve an Open WebUI content_id to its exact stored location.

        Authentication is intentionally retained: enterprise body text must not
        become anonymously enumerable merely to make a convenient hyperlink.
        """
        if scope not in {"21MM", "24MM"} or content_id < 1:
            raise HTTPException(400, "Explicit local scope/content_id required")
        retriever = runtime.get_service().retriever
        with retriever._connect(scope) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.id AS content_id,c.section_id,c.page_number,c.content,
                       d.document_id,d.file_name,sv.version_label,sp.spec_code,
                       sp.canonical_name
                FROM public.contents c
                JOIN public.documents d ON d.document_id=c.document_id
                JOIN public.specification_versions sv ON sv.document_id=c.document_id
                JOIN public.specifications sp ON sp.id=sv.specification_id
                WHERE c.id=%s AND sp.project_code=%s
                LIMIT 1
                """, (content_id, scope),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, "Content location not found in selected scope")
            return {"status": "OK", "scope": scope, **dict(row)}

    @app.get("/api/v1/intelligence/relations")
    def engineering_content_relations(
        scope: str, spec_code: str, _: None = Depends(require_auth),
    ):
        if scope not in {"21MM", "24MM"} or not spec_code.isalnum() or len(spec_code)>20:
            raise HTTPException(400, "Explicit local specification scope/code required")
        retriever = runtime.get_service().retriever
        with retriever._connect(scope) as conn, conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.ei_content_relations') AS table_name")
            if not (cur.fetchone() or {}).get('table_name'):
                return {"status":"SCHEMA_NOT_INSTALLED","items":[]}
        items = retriever._content_relation_search((scope,),spec_codes=(spec_code,))
        return {"status":"OK", "scope":scope, "spec_code":spec_code,
                "items":[{
                    "id":e.relation_id,"status":e.metadata.get('status'),
                    "relation_type":e.metadata.get('relation_type'),
                    "source":e.metadata.get('source'),"target":e.metadata.get('target'),
                    "two_sided_evidence":e.content,
                } for e in items]}

    @app.get("/api/v1/intelligence/unresolved-anchors")
    def engineering_unresolved_anchors(
        scope: str, spec_code: str, limit: int = Query(50, ge=1, le=100),
        _: None = Depends(require_auth),
    ):
        """A named section is not a validated paragraph-to-paragraph edge."""
        if scope not in {"21MM", "24MM"} or not spec_code.isalnum() or len(spec_code) > 20:
            raise HTTPException(400, "Explicit local specification scope/code required")
        retriever = runtime.get_service().retriever
        with retriever._connect(scope) as conn, conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.ei_unresolved_relation_anchors') AS table_name")
            if not (cur.fetchone() or {}).get('table_name'):
                return {"status": "SCHEMA_NOT_INSTALLED", "items": []}
            cur.execute("""
                SELECT a.id, a.reason, a.candidate_count, a.target_section_id,
                       su.unit_key AS source_unit_key,
                       su.document_id AS source_document_id,
                       su.section_id AS source_section_id,
                       su.page_number AS source_page_number,
                       su.raw_text AS source_text,
                       target.spec_code AS target_spec_code,
                       tv.version_label AS target_version
                FROM public.ei_unresolved_relation_anchors a
                JOIN public.ei_requirement_units su ON su.id=a.source_unit_id
                JOIN public.specifications source ON source.id=su.specification_id
                JOIN public.specification_versions tv ON tv.id=a.target_version_id
                JOIN public.specifications target ON target.id=tv.specification_id
                WHERE source.project_code=%s AND source.spec_code=%s
                  AND a.reason<>'RESOLVED'
                  AND su.version_id=source.current_version_id
                  AND tv.id=target.current_version_id
                ORDER BY a.id DESC LIMIT %s
            """, (scope, spec_code, limit))
            return {"status": "OK", "scope": scope, "spec_code": spec_code,
                    "items": [dict(row) for row in cur.fetchall()]}

    @app.get("/api/v1/intelligence/version-mappings")
    def requirement_version_mappings(
        scope: str, spec_code: str, limit: int = Query(100, ge=1, le=300),
        _: None = Depends(require_auth),
    ):
        if scope not in {"21MM", "24MM"} or not spec_code.isalnum() or len(spec_code)>20:
            raise HTTPException(400, "Explicit local specification scope/code required")
        retriever=runtime.get_service().retriever
        with retriever._connect(scope) as conn,conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.ei_version_mappings') AS table_name")
            if not (cur.fetchone() or {}).get('table_name'):
                return {"status":"SCHEMA_NOT_INSTALLED","items":[]}
            cur.execute(
                """
                SELECT vm.change_type,vm.review_status,vm.similarity,
                       old.version_label AS from_version,new.version_label AS to_version,
                       old_unit.section_id AS old_section,new_unit.section_id AS new_section,
                       old_unit.raw_text AS old_text,new_unit.raw_text AS new_text
                FROM public.ei_version_mappings vm
                JOIN public.specification_versions old ON old.id=vm.source_version_id
                JOIN public.specification_versions new ON new.id=vm.target_version_id
                JOIN public.specifications sp ON sp.id=old.specification_id
                LEFT JOIN public.ei_requirement_units old_unit ON old_unit.id=vm.old_unit_id
                LEFT JOIN public.ei_requirement_units new_unit ON new_unit.id=vm.new_unit_id
                WHERE sp.project_code=%s AND sp.spec_code=%s
                ORDER BY vm.id DESC LIMIT %s
                """,(scope,spec_code,limit)
            )
            return {"status":"OK","scope":scope,"spec_code":spec_code,
                    "items":[dict(r) for r in cur.fetchall()]}

    @app.post("/v1/chat/completions")
    def completions(body: CompletionInput, _: None = Depends(require_auth)):
        if body.model != MODEL_ID:
            raise HTTPException(status_code=400, detail="Use the local RAG model identifier")
        supported_scopes = {"AUTO", "21MM", "24MM", "COMMON", "QABUG", "QA/BUG", "ALL", "21MM + COMMON", "24MM + COMMON"}
        if body.scope not in supported_scopes:
            raise HTTPException(status_code=400, detail="Invalid knowledge scope")
        messages = []
        for item in body.messages:
            role = item.get("role")
            if role not in {"user", "assistant"}:
                continue  # never let client override evidence-grounding system prompt
            content = item.get("content")
            if isinstance(content, str):
                value = content
            elif isinstance(content, list):
                value = "\n".join(str(part.get("text", "")) for part in content if isinstance(part, dict) and part.get("type") == "text")
            else:
                continue
            if len(value) > 50000:
                raise HTTPException(status_code=413, detail="Message too long")
            messages.append({"role": role, "content": value})
        if not any(x["role"] == "user" for x in messages):
            raise HTTPException(status_code=400, detail="At least one user message required")
        # The deterministic SQL lane stays responsive even while two slow
        # Ollama generations occupy the model lane. Both remain bounded.
        from app.ai.agent import FastIntentRouter
        last_user = next((x['content'] for x in reversed(messages) if x['role']=='user'), '')
        classification = FastIntentRouter.classify(
            FastIntentRouter.contextualize(last_user, messages)
        )
        structured_routes = {'SPEC_CATALOG', 'SPEC_LOOKUP', 'SPEC_OUTLINE',
                             'SPEC_CHAPTER_CONTENT', 'SPEC_SUMMARY', 'SPEC_VERSION_FACTS', 'SPEC_RELATIONS',
                             'PRE_TICKET_CHECK','TICKET_SPEC_TRACE','CONFORMANCE_CHECK'}
        if classification.route not in structured_routes and not runtime.preferred_model:
            raise HTTPException(status_code=503, detail="Configure a local Ollama model in AI / RAG settings first")
        selected_admissions = factual_admissions if classification.route in structured_routes else admissions
        lease = selected_admissions.acquire()
        if lease is None:
            raise HTTPException(
                status_code=429, detail="Local RAG is busy; retry shortly",
                headers={"Retry-After": "3"},
            )
        try:
            service = runtime.get_service()
        except Exception:
            lease.release(failed=True)
            raise
        model = runtime.preferred_model
        cid = "chatcmpl-" + uuid.uuid4().hex
        created = int(time.time())
        scope = body.scope
        if not body.stream:
            try:
                answer, evidence, _ = service.answer(messages, model=model, scope=scope, current_scope="")
            except Exception:
                lease.release(failed=True)
                LOGGER.exception("Non-stream RAG request failed (id=%s)", cid)
                raise HTTPException(status_code=503, detail="Local RAG generation failed; inspect backend logs") from None
            else:
                lease.release()
            return {
                "id": cid, "object": "chat.completion", "created": created, "model": MODEL_ID,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": answer}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                "rag": {
                    "request_id": evidence.request_id, "route": evidence.route,
                    "scopes": list(evidence.resolved_scopes), "evidence_count": len(evidence.evidence),
                    "tools": list(evidence.tools), "warnings": list(evidence.warnings),
                    **({"workflow":evidence.workflow,"strength":evidence.evidence_strength} if evidence.workflow else {}),
                },
            }

        def emit():
            captured=[]
            try:
                yield "data: " + json.dumps({"id": cid, "object": "chat.completion.chunk", "created": created, "model": MODEL_ID, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}, ensure_ascii=False) + "\n\n"
                callbacks={"on_retrieval":captured.append} if classification.route in {"PRE_TICKET_CHECK","TICKET_SPEC_TRACE","CONFORMANCE_CHECK"} else {}
                for chunk in service.answer_stream(messages, model=model, scope=scope, current_scope="",**callbacks):
                    packet = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": MODEL_ID, "choices": [{"index": 0, "delta": {"content": chunk}, "finish_reason": None}]}
                    yield "data: " + json.dumps(packet, ensure_ascii=False) + "\n\n"
                final={"id": cid, "object": "chat.completion.chunk", "created": created, "model": MODEL_ID, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
                if captured and captured[-1].workflow:
                    final["rag"]={"route":captured[-1].route,"workflow":captured[-1].workflow}
                yield "data: " + json.dumps(final) + "\n\n"
            except Exception:
                lease.release(failed=True)
                LOGGER.exception("Stream RAG request failed (id=%s)", cid)
                # Never leak traceback, credentials, or retrieved content to clients.
                yield "data: " + json.dumps({
                    "id": cid, "object": "chat.completion.chunk", "created": created,
                    "model": MODEL_ID, "choices": [{"index": 0,
                    "delta": {"content": "\n[Local RAG error: response incomplete. Check backend logs.]"},
                    "finish_reason": None}],
                }) + "\n\n"
            finally:
                lease.release()
                yield "data: [DONE]\n\n"

        return StreamingResponse(
            emit(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            background=BackgroundTask(lease.release),
        )

    enabled = ragflow_compat if ragflow_compat is not None else os.environ.get("DIC_RAGFLOW_COMPAT", "0") == "1"
    if enabled:
        from app.server.ragflow_compat import install_adapter
        install_adapter(app, runtime, require_auth, completions, CompletionInput, factual_admissions)
    return app
