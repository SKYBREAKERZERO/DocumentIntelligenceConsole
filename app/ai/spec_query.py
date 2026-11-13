"""Specification-only request processing with an independently pinned local model."""
from __future__ import annotations

from dataclasses import replace

from app.ai.config import AISettingsStore
from app.ai.ollama_client import OllamaClient
from app.ai.rag_service import EnterpriseRAGService
from app.ai.request_understanding import understand
from app.ai.retrieval import EnterpriseRetriever, RetrievalResult
from app.ai.spec_workflow import SPEC_SCOPES

MODEL_ID = "document-intelligence-spec-query"
LOCAL_MODEL = "dic-spec-query-4b:latest"



def load_spec_settings(config_dir, base):
    path = config_dir / "spec_query_config.json"
    settings = AISettingsStore(path).load() if path.is_file() else replace(
        base, chat_model=LOCAL_MODEL, num_ctx=8192,
        prompt_budget_tokens=6400, evidence_budget_tokens=4000,
    )
    # This task always uses local inference, independently of the shared VPN profile.
    from app.server.configuration import require_local_url
    require_local_url(settings.ollama_url, label="Specification Ollama")
    if not settings.chat_model.strip():
        raise ValueError("Specification model is required")
    return replace(settings, inference_provider="ollama", inference_ca_bundle="",
                   collections={k: v for k, v in settings.collections.items() if k in SPEC_SCOPES})


class SpecificationRetriever(EnterpriseRetriever):
    def _connect(self, scope):
        if scope not in SPEC_SCOPES:
            raise ValueError("Specification scope required")
        return super()._connect(scope)


class SpecificationQueryService(EnterpriseRAGService):
    """Own orchestration, shared verified SQL/body/version/relation read models."""

    _body_answer_tokens = 768
    _body_answer_guidance = (
        "\nKeep the answer concise: up to six points and about 250 English words "
        "or 400 Chinese/Japanese characters. Include material conditions, citations "
        "and evidence gaps. Avoid repeating the source text or self-certification."
    )

    def __init__(self, *, settings, postgres_profiles, postgres_password,
                 qdrant_profile, qdrant_api_key=""):
        self.settings = settings
        self.ollama = OllamaClient(
            settings.ollama_url, connect_timeout=settings.ollama_connect_timeout,
            read_timeout=settings.ollama_read_timeout,
            first_load_timeout=settings.ollama_first_load_timeout,
            stream_idle_timeout=settings.ollama_stream_idle_timeout,
        )
        self.retriever = SpecificationRetriever(
            postgres_profiles={k: v for k, v in postgres_profiles.items() if k in SPEC_SCOPES},
            postgres_password=postgres_password, qdrant=qdrant_profile,
            qdrant_api_key=qdrant_api_key, collections=settings.collections,
            embedding_model=settings.embedding_model, vector_dimension=settings.vector_dimension,
            top_k=max(settings.top_k, settings.evidence_limit), min_score=settings.min_score,
        )

    def prepare(self, messages, *, model, scope="AUTO", current_scope="",
                retrieval_mode=None, cancel_check=None, on_stage=None):
        if model != self.settings.chat_model:
            raise ValueError("Use the configured specification model")
        if scope not in {"AUTO", "ALL", "21MM", "24MM", "COMMON",
                         "21MM + COMMON", "24MM + COMMON"}:
            raise ValueError("Specification scope required")
        question = self._latest_user_message(messages)
        if not question:
            raise ValueError("A user question is required")
        if cancel_check and cancel_check():
            return [], RetrievalResult(evidence=(), resolved_scopes=())
        self._emit_stage(on_stage, "SPEC_UNDERSTAND", "正在理解式样查询及历史上下文")
        try:
            contract, subject = understand(
                self.ollama, messages, model=model, num_ctx=self.settings.num_ctx,
                keep_alive=self.settings.ollama_keep_alive,
                audit=False,
                strict_schema=True,
                task_guidance=(
                    "This is the dedicated specification query task. "
                    "CATALOG lists or counts registered specification documents in a project or repository. "
                    "LOOKUP resolves the identity or metadata of a particular specification. "
                    "OUTLINE returns chapters or section headings INSIDE a particular specification. "
                    "QUOTE reproduces explicitly requested verbatim source text. "
                    "A request to provide citations or summarize source text is not QUOTE. "
                    "PROCESS explains execution order. "
                    "ANALYSIS explains or evaluates specification content. "
                    "COMPARE examines historical revisions, changes, or revision availability. "
                    "RELATIONS examines engineering references, dependencies, and related specifications. "
                    "A directory of specification BOOKS is CATALOG; a directory of CHAPTERS is OUTLINE. "
                    "Use the requested work and conversational target to distinguish these tasks."
                ),
            )
        except Exception as exc:
            return [], RetrievalResult(
                evidence=(), resolved_scopes=(), route="REQUEST_UNDERSTANDING_FAILED",
                warnings=("Specification interpretation: " + type(exc).__name__,),
            )
        if contract.domain != "SPEC" or contract.task in {"CHAT", "TEST_DESIGN"}:
            return [], RetrievalResult(evidence=(), resolved_scopes=(), route="SPEC_SCOPE_REQUIRED")
        return self._prepare_specification(
            messages, question=question, contract=contract, subject=subject,
            model=model, scope=scope, current_scope=current_scope,
            cancel_check=cancel_check, on_stage=on_stage,
        )
