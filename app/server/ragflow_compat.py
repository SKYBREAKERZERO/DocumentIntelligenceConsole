"""Optional, read-only RAGFlow-shaped facade. No RAGFlow runtime dependency.

This is a declared subset, not a drop-in RAGFlow server or canvas DSL engine.
All dataset IDs refer to existing configured scopes; no corpus is created.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from app.skills.catalog import list_skills

PREFIX = "/compat/ragflow"
SCOPES = ("21MM", "24MM", "COMMON", "QABUG")
STEPS = ("internal-retrieval", "evidence-governance", "local-answer")


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Retrieve(StrictInput):
    question: str = Field(min_length=1, max_length=10000)
    dataset_ids: list[str] = Field(min_length=1, max_length=4)
    document_ids: list[str] = Field(default_factory=list, max_length=100)
    page: int = Field(default=1, ge=1, le=100)
    page_size: int = Field(default=30, ge=1, le=100)


class Message(StrictInput):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=16000)


class Chat(StrictInput):
    chat_id: str
    question: str = Field(min_length=1, max_length=10000)
    stream: Literal[False] = False
    history: list[Message] = Field(default_factory=list, max_length=38)


class Workflow(StrictInput):
    schema_version: Literal["dic.ragflow-style.workflow.v1"]
    name: str = Field(min_length=1, max_length=100)
    dataset_ids: list[str] = Field(min_length=1, max_length=1)
    steps: list[str] = Field(min_length=3, max_length=3)
    skills: list[str] = Field(max_length=7)


class WorkflowRun(StrictInput):
    workflow: Workflow
    question: str = Field(min_length=1, max_length=10000)


def ok(data):
    return {"code": 0, "message": "success", "data": data}


def chunk(item):
    # A stable composite identity prevents collisions between scopes/documents.
    key = f"{item.scope}|{item.document_id}|{item.content_id}|{item.evidence_id}"
    return {
        "id": hashlib.sha256(key.encode()).hexdigest()[:32],
        "content": item.content, "document_id": item.document_id,
        "document_keyword": item.file_name, "dataset_id": "dic-" + item.scope.lower(),
        "similarity": None,  # heterogeneous native scores are NOT cosine similarity
        "dic": {"evidence_id": item.evidence_id, "content_id": item.content_id,
                "native_score": item.score, "source_type": item.source_type,
                "citation": item.citation_label, "page_number": item.page_number,
                "section_id": item.section_id, "version": item.specification_version},
    }


def install_adapter(app, runtime, require_auth, native_completions, completion_type, admissions):
    router = APIRouter(prefix=PREFIX + "/api/v1", dependencies=[Depends(require_auth)])

    def datasets():
        configured = runtime.local_status().get("configured_scopes", [])
        return [{"id": "dic-" + scope.lower(), "name": scope,
                 "description": "Existing internal scope; availability not probed",
                 "permission": "me", "dic": {"read_only": True, "scope": scope,
                 "availability": "NOT_CHECKED", "document_count": None}}
                for scope in SCOPES if scope in configured]

    def scopes_for(ids):
        mapping = {d["id"]: d["name"] for d in datasets()}
        if len(set(ids)) != len(ids) or any(x not in mapping for x in ids):
            raise HTTPException(400, "Unknown, duplicate or unconfigured dataset ID")
        return [mapping[x] for x in ids]

    def validate_workflow(body):
        scopes_for(body.dataset_ids)
        if tuple(body.steps) != STEPS:
            raise HTTPException(400, "Only the fixed native governed pipeline is supported")
        allowed = {x["name"] for x in list_skills()}
        if len(set(body.skills)) != len(body.skills) or not set(body.skills) <= allowed:
            raise HTTPException(400, "Unknown or duplicate Skill")
        return body.model_dump()

    @router.get("/capabilities")
    def capabilities():
        return ok({"adapter": "dic-ragflow-style-v1", "compatibility": "DECLARED_SUBSET",
                   "upstream_reference": "RAGFlow HTTP API DEV, consulted 2026-10-28",
                   "internal_only": True, "database_write": False,
                   "datasets": "configured native scopes; not RAGFlow UUIDs",
                   "retrieval": "native hybrid candidates, post-filtered by scope/document",
                   "chat": "stateless non-stream native completions; client-supplied history",
                   "workflow": "DIC fixed pipeline schema; NOT RAGFlow canvas DSL",
                   "unsupported": ["dataset mutation", "upload/parse", "sessions", "streaming",
                                   "RAGFlow canvas import", "custom nodes", "external tools",
                                   "similarity_threshold", "rerank_id", "metadata_condition"],
                   "skills": list(list_skills())})

    @router.get("/datasets")
    def get_datasets(page: int = Query(1, ge=1), page_size: int = Query(30, ge=1, le=100)):
        rows = datasets()
        return ok(rows[(page-1)*page_size:page*page_size])

    @router.get("/chats")
    def get_chats():
        return ok([{"id": d["id"], "name": d["name"] + " · Native AUTO",
                    "dataset_ids": [d["id"]], "dic": {"routing_hint_only": True}}
                   for d in datasets()])

    @router.post("/retrieval")
    def retrieve(body: Retrieve):
        scopes = scopes_for(body.dataset_ids)
        if not body.question.strip() or any(not x or len(x) > 512 for x in body.document_ids):
            raise HTTPException(400, "Invalid question or document ID")
        lease = admissions.acquire()
        if lease is None:
            raise HTTPException(429, "Local retrieval busy", headers={"Retry-After": "3"})
        try:
            retriever = runtime.get_service().retriever
            rows, seen, request_ids, degraded_scopes = [], set(), [], []
            for scope in scopes:
                result = (retriever.retrieve_issue_register(body.question, scope=scope, current_scope="", statistics=False)
                          if scope == "QABUG" else retriever.retrieve(body.question, scope=scope, current_scope=""))
                request_ids.append(result.request_id)
                if result.warnings:
                    degraded_scopes.append(scope)
                for item in result.evidence:
                    if item.scope != scope or (body.document_ids and item.document_id not in body.document_ids):
                        continue
                    row = chunk(item)
                    if row["id"] not in seen:
                        rows.append(row)
                        seen.add(row["id"])
        except Exception:
            lease.release(failed=True)
            raise HTTPException(503, "Native retrieval unavailable") from None
        else:
            lease.release()
        start = (body.page-1)*body.page_size
        return ok({"chunks": rows[start:start+body.page_size], "total": len(rows),
                   "doc_aggs": [], "dic": {"request_ids": request_ids,
                   "total_kind": "returned_native_candidates_not_corpus_count",
                   "ranking": "dataset order then native order",
                   "degraded_scopes": degraded_scopes,
                   "document_filter": "post-filter over bounded native candidates",
                   "warnings": ["Native scores are not normalized similarity; recall may be truncated."]}})

    def answer(body):
        scope = scopes_for([body.chat_id])[0]
        if not body.question.strip():
            raise HTTPException(400, "Question must not be blank")
        messages = [m.model_dump() for m in body.history] + [{"role": "user", "content": body.question}]
        if sum(len(m["content"]) for m in messages) > 64000:
            raise HTTPException(413, "Conversation too large")
        # Reuse native model allow-list, evidence governor and shared admission lanes.
        result = native_completions(completion_type(model="document-intelligence-rag",
                                    messages=messages, scope=scope, stream=False))
        return ok({"answer": result["choices"][0]["message"]["content"],
                   "id": result["id"], "reference": {"chunks": [], "doc_aggs": []},
                   "dic": {"native_rag": result["rag"], "sessions_persisted": False,
                           "scope_policy": "native AUTO routing; dataset is not an ACL",
                           "references": "native answer citation text; no fabricated chunk list"}})

    @router.post("/chat/completions")
    def chat(body: Chat):
        return answer(body)

    @router.get("/workflows/template")
    def template(dataset_id: str):
        scopes_for([dataset_id])
        return ok({"schema_version": "dic.ragflow-style.workflow.v1", "name": "Internal governed RAG",
                   "dataset_ids": [dataset_id], "steps": list(STEPS),
                   "skills": [x["name"] for x in list_skills()]})

    @router.post("/workflows/validate")
    def validate(body: Workflow):
        return ok({"workflow": validate_workflow(body), "persisted": False,
                   "skills_policy": "discovery annotations; native question router chooses actual tools"})

    @router.post("/workflows/run")
    def run(body: WorkflowRun):
        validate_workflow(body.workflow)
        return answer(Chat(chat_id=body.workflow.dataset_ids[0], question=body.question))

    app.include_router(router)

    @app.get(PREFIX, include_in_schema=False)
    def ui():
        # Public static shell contains no token/data. Every data call requires auth.
        return FileResponse(Path(__file__).with_name("ragflow_ui.html"), headers={
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
        })
