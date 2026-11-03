from __future__ import annotations

import hashlib
import logging
import math
import os
import re
import time
from collections import OrderedDict
from threading import RLock
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable

from app.enterprise.profiles import PostgresProfile, QdrantProfile
from app.relations.graph_service import EngineeringRelationGraphService
from app.engineering_intelligence.temporal_query import (
    parse_temporal_audit_ids, choose_audit_scope,
)
from app.storage.primary_document_policy import PrimarySpecificationDocumentPolicy
from app.ai.hybrid_retrieval import fuse_spec_rrf
from app.skills.live_workflows import build_live_skill_workflow, issue_symptom_queries

LOGGER = logging.getLogger("document_intelligence_console.ai.retrieval")


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    evidence_id: str
    scope: str
    source_type: str
    content: str
    content_id: int | None = None
    score: float | None = None
    document_id: str = ""
    file_name: str = ""
    specification_code: str = ""
    specification_name: str = ""
    specification_version: str = ""
    chapter_title: str = ""
    section_id: str = ""
    section_title: str = ""
    page_number: int | None = None
    relation_id: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def citation_label(self) -> str:
        spec = " ".join(x for x in (self.specification_code, self.specification_name) if x).strip()
        if not spec:
            spec = self.file_name or self.document_id or self.source_type
        if self.specification_version:
            spec = f"{spec} {self.specification_version}".strip()
        location = self.section_title or self.section_id
        if location:
            spec = f"{spec} · {location}"
        if self.source_type in {"SQL_EXACT", "SEMANTIC", "QDRANT", "HYBRID_CHUNK", "QDRANT_SPARSE", "CONFIGURED_CHECK_SOURCE"}:
            page = self.page_number if self.page_number is not None else "PAGE_UNKNOWN"
            spec += f" · PDF页={page} · content_id={self.content_id if self.content_id is not None else 'UNKNOWN'}"
            if self.file_name:
                spec += f" · 源文件={self.file_name}"
            content_hash = str(self.metadata.get("content_hash") or "").strip()
            if content_hash:
                spec += f" · SHA256={content_hash}"
        return f"{self.scope} · {spec}"


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    evidence: tuple[EvidenceItem, ...]
    resolved_scopes: tuple[str, ...]
    semantic_ms: int = 0
    sql_ms: int = 0
    relation_ms: int = 0
    version_ms: int = 0
    planner_ms: int = 0
    warnings: tuple[str, ...] = ()
    plan_source: str = ""
    plan_summary: str = ""
    intents: tuple[str, ...] = ()
    entities: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    rewritten_queries: tuple[str, ...] = ()
    evidence_strength: str = "NONE"
    conflicts: tuple[str, ...] = ()
    trace: tuple[str, ...] = ()
    request_id: str = ""
    route: str = "QDRANT_RAG"
    knowledge_source: str = "QDRANT_ONLY"
    embedding_ms: int = 0
    qdrant_ms: int = 0
    hydration_ms: int = 0
    # Display-only suggestions. NEVER passed to EvidenceGovernor/model context.
    candidate_suggestions: tuple[EvidenceItem, ...] = ()
    # IDs actually admitted by the bounded prompt assembler; not parsed from
    # untrusted source text that can contain fake "[2] | source=" headings.
    prompt_evidence_ids: tuple[str, ...] = ()
    workflow: dict[str, Any] = field(default_factory=dict)


ISSUE_OBSERVATION_SECTIONS=frozenset({"SUMMARY","DESCRIPTION","BUG_PHENOMENON","BUG_ACTUAL_RESULT","QA_ACTUAL_BEHAVIOR"})


class EnterpriseRetriever:
    def __init__(
        self,
        *,
        postgres_profiles: dict[str, PostgresProfile],
        postgres_password: str,
        qdrant: QdrantProfile,
        qdrant_api_key: str,
        collections: dict[str, str],
        embedding_model: str = "BAAI/bge-m3",
        vector_dimension: int = 1024,
        top_k: int = 8,
        min_score: float = 0.25,
    ) -> None:
        self.postgres_profiles = dict(postgres_profiles)
        self.postgres_password = str(postgres_password or "")
        self.qdrant_profile = qdrant
        self.qdrant_api_key = str(qdrant_api_key or "")
        self.collections = {str(k).upper(): str(v) for k, v in collections.items()}
        self.embedding_model = str(embedding_model)
        self.vector_dimension = int(vector_dimension)
        self.top_k = max(1, min(int(top_k), 30))
        self.min_score = float(min_score)
        self.sparse_enabled = os.environ.get("DIC_STAGE4_SPARSE_ENABLED", "0") == "1"
        self._sparse_encoder: Any | None = None
        self._embedding_client: Any | None = None
        self._embedding_lock = RLock()
        self._embedding_cache: OrderedDict[str, tuple[float, ...]] = OrderedDict()
        self._embedding_cache_limit = 128
        self._qdrant_client: Any | None = None
        self._qdrant_lock = RLock()
        self._collection_exists_cache: dict[str, tuple[float, bool]] = {}

    @property
    def embedding_loaded(self) -> bool:
        return bool(self._embedding_client is not None and getattr(self._embedding_client, "_model", None) is not None)

    def warmup_embedding(self) -> None:
        with self._embedding_lock:
            if self._embedding_client is None:
                from app.embedding.embedding_client import EmbeddingClient, EmbeddingClientConfig
                self._embedding_client = EmbeddingClient(
                    EmbeddingClientConfig(
                        model_name=self.embedding_model,
                        expected_dimension=self.vector_dimension,
                        batch_size=1,
                        normalize_embeddings=True,
                        show_progress_bar=False,
                        trust_remote_code=False,
                    )
                )
            self._embedding_client.warmup()

    def _embed(self, question: str) -> list[float]:
        cache_key = re.sub(r"\s+", " ", str(question or "").strip())
        with self._embedding_lock:
            cached = self._embedding_cache.get(cache_key)
            if cached is not None:
                self._embedding_cache.move_to_end(cache_key)
                return list(cached)
            if self._embedding_client is None:
                from app.embedding.embedding_client import EmbeddingClient, EmbeddingClientConfig
                self._embedding_client = EmbeddingClient(
                    EmbeddingClientConfig(
                        model_name=self.embedding_model,
                        expected_dimension=self.vector_dimension,
                        batch_size=1,
                        normalize_embeddings=True,
                        show_progress_bar=False,
                        trust_remote_code=False,
                    )
                )
            result = self._embedding_client.embed_text(cache_key)
            if result.dimension != self.vector_dimension:
                raise RuntimeError(
                    f"Embedding dimension mismatch: expected {self.vector_dimension}, got {result.dimension}."
                )
            vector = tuple(float(value) for value in result.vector)
            self._embedding_cache[cache_key] = vector
            self._embedding_cache.move_to_end(cache_key)
            while len(self._embedding_cache) > self._embedding_cache_limit:
                self._embedding_cache.popitem(last=False)
            return list(vector)

    def _get_qdrant_client(self):
        with self._qdrant_lock:
            if self._qdrant_client is None:
                try:
                    from qdrant_client import QdrantClient
                except ImportError as exc:
                    raise RuntimeError("qdrant-client is not installed") from exc
                self._qdrant_client = QdrantClient(
                    url=self.qdrant_profile.url,
                    api_key=self.qdrant_api_key or None,
                    timeout=self.qdrant_profile.timeout,
                )
            return self._qdrant_client

    def _collection_exists(self, client: Any, collection: str) -> bool:
        now = time.monotonic()
        cached = self._collection_exists_cache.get(collection)
        if cached and now - cached[0] < 60.0:
            return cached[1]
        exists = bool(client.collection_exists(collection_name=collection))
        self._collection_exists_cache[collection] = (now, exists)
        return exists

    @staticmethod
    def resolve_scopes(scope: str, question: str, current_scope: str = "") -> tuple[str, ...]:
        requested = str(scope or "AUTO").strip().upper().replace("QA/BUG", "QABUG").replace("QA_BUG", "QABUG")
        if requested in {"21MM", "24MM", "COMMON", "QABUG"}:
            return (requested,)
        if requested in {"ALL", "21MM + 24MM + COMMON"}:
            # ALL is an engineering-spec scope by default. QA/BUG remains an
            # isolated corpus unless explicitly selected or AUTO-routed as an
            # issue question; this prevents accidental cross-domain leakage.
            return ("21MM", "24MM", "COMMON")
        if requested == "21MM + COMMON":
            return ("21MM", "COMMON")
        if requested == "24MM + COMMON":
            return ("24MM", "COMMON")
        upper = question.upper()
        has_21 = "21MM" in upper
        has_24 = "24MM" in upper
        if has_21 and has_24:
            return ("21MM", "24MM", "COMMON")
        if has_21:
            return ("21MM", "COMMON")
        if has_24:
            return ("24MM", "COMMON")
        normalized_current = str(current_scope or "").strip().upper().replace("QA/BUG", "QABUG")
        if normalized_current in {"21MM", "24MM", "COMMON", "QABUG"}:
            return (normalized_current,)
        return ("21MM", "24MM", "COMMON")

    @staticmethod
    def resolve_issue_scopes(scope: str, question: str, current_scope: str = "") -> tuple[str, ...]:
        """Resolve QA/BUG separately from specification repositories.

        AUTO issue routes always target the dedicated QABUG database/collection.
        An explicit QA/BUG selection does the same. A deliberately selected
        legacy engineering scope is respected for backward compatibility, but
        no automatic query fans out QA/BUG into specification databases.
        """
        requested = str(scope or "AUTO").strip().upper().replace("QA/BUG", "QABUG").replace("QA_BUG", "QABUG")
        if requested in {"AUTO", "QABUG"}:
            return ("QABUG",)
        if requested in {"21MM", "24MM", "COMMON"}:
            return (requested,)
        normalized_current = str(current_scope or "").strip().upper().replace("QA/BUG", "QABUG")
        if normalized_current == "QABUG":
            return ("QABUG",)
        return ("QABUG",)

    def retrieve_qdrant_only(
        self,
        question: str,
        *,
        scope: str = "AUTO",
        current_scope: str = "",
        cancel_check: Callable[[], bool] | None = None,
        stage_callback: Callable[[str, str, int], None] | None = None,
    ) -> RetrievalResult:
        """Fast repository path: Qdrant is the only retrieval candidate source.

        PostgreSQL is never searched for extra knowledge here. If the current
        Qdrant payload does not yet contain full text, SQL may hydrate only the
        exact content_id values already selected by Qdrant.
        """
        import uuid

        scopes = self.resolve_scopes(scope, question, current_scope)
        entity_codes = self._entity_spec_codes((), question)
        metrics: dict[str, int] = {}
        trace: list[str] = []
        if stage_callback:
            stage_callback("ROUTE", f"QDRANT_RAG · scopes={', '.join(scopes)}", 0)
        started = time.perf_counter()
        try:
            items = self._semantic_search(
                question,
                scopes,
                entity_codes=entity_codes,
                cancel_check=cancel_check,
                source_type="QDRANT",
                metrics=metrics,
                stage_callback=stage_callback,
            )
            warning_items: list[str] = []
        except Exception as exc:
            items = []
            warning_items = [f"Qdrant retrieval unavailable: {type(exc).__name__}"]
            trace.append(f"Qdrant retrieval ERROR: {exc}")
            LOGGER.warning("Qdrant-only retrieval failed", exc_info=True)
        semantic_ms = int((time.perf_counter() - started) * 1000)
        if entity_codes:
            trace.append(f"Qdrant filter: spec_code={', '.join(entity_codes)}")
        else:
            trace.append("Qdrant filter: none")
        trace.append(
            f"Qdrant-only retrieval: {len(items)} candidate(s) · "
            f"embedding={metrics.get('embedding_ms', 0)} ms · "
            f"qdrant={metrics.get('qdrant_ms', 0)} ms · "
            f"hydration={metrics.get('hydration_ms', 0)} ms"
        )
        return RetrievalResult(
            evidence=tuple(items),
            resolved_scopes=scopes,
            semantic_ms=semantic_ms,
            sql_ms=0,
            relation_ms=0,
            version_ms=0,
            planner_ms=0,
            warnings=tuple(warning_items),
            plan_source="fast-deterministic-router",
            plan_summary="Repository knowledge candidates are retrieved only from the configured current Qdrant collection(s).",
            intents=("QDRANT_RAG",),
            entities=entity_codes,
            tools=("qdrant_semantic_search",),
            rewritten_queries=(question,),
            trace=tuple(trace),
            request_id="rag-" + uuid.uuid4().hex[:16],
            route="QDRANT_RAG",
            knowledge_source="QDRANT_ONLY",
            # Retrieved candidates do not by themselves prove a complete answer.
            evidence_strength="LIMITED" if items else "NONE",
            embedding_ms=int(metrics.get("embedding_ms", 0)),
            qdrant_ms=int(metrics.get("qdrant_ms", 0)),
            hydration_ms=int(metrics.get("hydration_ms", 0)),
        )

    def retrieve(
        self,
        question: str,
        *,
        scope: str = "AUTO",
        current_scope: str = "",
        mode: str = "Hybrid",
        cancel_check: Callable[[], bool] | None = None,
    ) -> RetrievalResult:
        scopes = self.resolve_scopes(scope, question, current_scope)
        normalized_mode = str(mode or "Hybrid").strip().lower()
        warnings: list[str] = []
        semantic_items: list[EvidenceItem] = []
        sparse_items: list[EvidenceItem] = []
        exact_items: list[EvidenceItem] = []
        relation_items: list[EvidenceItem] = []
        semantic_ms = sql_ms = relation_ms = 0

        if normalized_mode not in {"sql exact", "engineering relation", "no rag"}:
            started = time.perf_counter()
            try:
                semantic_items = self._semantic_search(question, scopes, cancel_check=cancel_check)
            except Exception as exc:
                warnings.append(f"Semantic retrieval unavailable: {type(exc).__name__}")
                LOGGER.warning("Semantic retrieval failed", exc_info=True)
            semantic_ms = int((time.perf_counter() - started) * 1000)

        if normalized_mode in {"hybrid", "sql exact", "reference", "version comparison"}:
            started = time.perf_counter()
            try:
                exact_items = self._sql_exact_search(question, scopes)
            except Exception as exc:
                warnings.append(f"SQL exact retrieval unavailable: {type(exc).__name__}")
                LOGGER.warning("SQL exact retrieval failed", exc_info=True)
            sql_ms = int((time.perf_counter() - started) * 1000)

        relation_requested = normalized_mode in {"hybrid", "engineering relation", "reference", "version comparison"}
        if relation_requested:
            started = time.perf_counter()
            try:
                relation_items = self._relation_search(question, scopes)
            except Exception as exc:
                warnings.append(f"Relation retrieval unavailable: {type(exc).__name__}")
                LOGGER.warning("Relation retrieval failed", exc_info=True)
            relation_ms = int((time.perf_counter() - started) * 1000)

        merged: list[EvidenceItem] = []
        seen: set[tuple[str, str, int | None]] = set()
        sparse_items: list[EvidenceItem] = []
        if normalized_mode == "hybrid" and getattr(self, "sparse_enabled", False):
            try:
                sparse_items = self._sparse_search(question, scopes)
            except Exception as exc:
                warnings.append(f"Stage 4 Sparse unavailable: {type(exc).__name__}")
                LOGGER.warning("Optional Sparse hybrid retrieval failed", exc_info=True)
        if normalized_mode == "hybrid" and (sparse_items or (semantic_items and exact_items)):
            spec_candidates = fuse_spec_rrf(
                exact_items, semantic_items, sparse_hits=sparse_items,
                max_candidates=max(self.top_k * 4, 32),
            )
        else:
            spec_candidates = [*semantic_items, *exact_items]
        if (normalized_mode == "hybrid" and spec_candidates
                and os.environ.get("DIC_STAGE4_RERANK_ENABLED", "0") == "1"):
            from app.ai.stage4_reranker import rerank_spec_chunks
            spec_candidates, rerank_status = rerank_spec_chunks(question, spec_candidates)
            if rerank_status.state in {"FAILED", "TIMEOUT", "BUSY"}:
                warnings.append(f"Optional Stage 4 reranker: {rerank_status.state}; original ranks preserved")
        for item in [*relation_items, *spec_candidates]:
            key = (item.scope, item.source_type, item.content_id if item.content_id is not None else item.relation_id)
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
            if len(merged) >= self.top_k:
                break
        normalized: list[EvidenceItem] = []
        for index, item in enumerate(merged, start=1):
            normalized.append(
                EvidenceItem(
                    evidence_id=str(index),
                    scope=item.scope,
                    source_type=item.source_type,
                    content=item.content,
                    content_id=item.content_id,
                    score=item.score,
                    document_id=item.document_id,
                    file_name=item.file_name,
                    specification_code=item.specification_code,
                    specification_name=item.specification_name,
                    specification_version=item.specification_version,
                    chapter_title=item.chapter_title,
                    section_id=item.section_id,
                    section_title=item.section_title,
                    page_number=item.page_number,
                    relation_id=item.relation_id,
                    metadata=dict(item.metadata),
                )
            )
        return RetrievalResult(
            evidence=tuple(normalized),
            resolved_scopes=scopes,
            semantic_ms=semantic_ms,
            sql_ms=sql_ms,
            relation_ms=relation_ms,
            warnings=tuple(warnings),
        )

    @staticmethod
    def _fuse_semantic_evidence(items: list[EvidenceItem]) -> list[EvidenceItem]:
        """Fuse repeated multi-query hits with a bounded consensus bonus.

        Rewritten queries often retrieve the same authoritative chunk. Repeated
        agreement should improve ranking, but must never overpower a poor base
        similarity score; therefore the bonus is deliberately small and capped.
        """
        grouped: dict[tuple[str, int | None], list[EvidenceItem]] = {}
        passthrough: list[EvidenceItem] = []
        for item in items:
            if item.content_id is None:
                passthrough.append(item)
                continue
            grouped.setdefault((item.scope, item.content_id), []).append(item)
        fused: list[EvidenceItem] = []
        for group in grouped.values():
            best = max(group, key=lambda item: float(item.score or 0.0))
            hits = len(group)
            bonus = min(0.075, max(0, hits - 1) * 0.025)
            md = dict(best.metadata)
            md["semantic_query_hits"] = hits
            md["semantic_fusion_bonus"] = round(bonus, 4)
            fused.append(replace(best, score=min(1.0, float(best.score or 0.0) + bonus), metadata=md))
        fused.extend(passthrough)
        fused.sort(key=lambda item: float(item.score or 0.0), reverse=True)
        return fused

    def retrieve_agentic(
        self,
        *,
        question: str,
        scopes: tuple[str, ...],
        rewritten_queries: tuple[str, ...],
        tools: tuple[str, ...],
        entities: tuple[str, ...] = (),
        cancel_check: Callable[[], bool] | None = None,
    ) -> RetrievalResult:
        """Execute a planner-selected, fixed allow-list of read-only retrieval tools.

        The LLM selects tools and query rewrites; this method owns all actual
        database/vector access and never accepts model-generated SQL.
        """
        tool_set = set(tools)
        warnings: list[str] = []
        trace: list[str] = []
        semantic_items: list[EvidenceItem] = []
        sparse_items: list[EvidenceItem] = []
        exact_items: list[EvidenceItem] = []
        relation_items: list[EvidenceItem] = []
        version_items: list[EvidenceItem] = []
        unresolved_items: list[EvidenceItem] = []
        catalog_items: list[EvidenceItem] = []
        issue_items: list[EvidenceItem] = []
        candidate_suggestions: list[EvidenceItem] = []
        semantic_ms = sql_ms = relation_ms = version_ms = 0
        queries = tuple(dict.fromkeys(q.strip() for q in rewritten_queries if q.strip())) or (question,)
        entity_codes = self._entity_spec_codes(entities, question)
        # Exact Stage 3 audit is its own terminal, single-database read path.
        # Do not mix newer/other documents' semantic hits into a temporal
        # assessment, and do not consume rewritten query strings for IDs.
        coordinates = parse_temporal_audit_ids(question)
        if coordinates:
            started = time.perf_counter()
            selected_scope = choose_audit_scope(question, tuple(scopes))
            if selected_scope is None:
                return RetrievalResult(
                    evidence=(), resolved_scopes=tuple(scopes),
                    warnings=('Historical relation audit requires exactly one engineering project scope.',),
                    trace=('temporal_relation_audit: BLOCKED_AMBIGUOUS_PROJECT',),
                )
            try:
                item = self._temporal_relation_audit_search(
                    selected_scope, relation_id=coordinates[0],
                    new_target_version_id=coordinates[1],
                )
            except Exception as exc:
                LOGGER.warning('Stage 3 temporal audit failed closed', exc_info=True)
                return RetrievalResult(
                    evidence=(), resolved_scopes=(selected_scope,),
                    version_ms=int((time.perf_counter() - started) * 1000),
                    warnings=(f'Historical relation audit unavailable: {type(exc).__name__}',),
                    trace=(f'temporal_relation_audit: ERROR {type(exc).__name__}',),
                )
            return RetrievalResult(
                evidence=(item,), resolved_scopes=(selected_scope,),
                version_ms=int((time.perf_counter() - started) * 1000),
                trace=('temporal_relation_audit: ' + str(item.metadata['audit_status']),),
            )

        if "semantic_search" in tool_set:
            started = time.perf_counter()
            try:
                for q in queries[:4]:
                    if cancel_check and cancel_check():
                        break
                    semantic_items.extend(self._semantic_search(
                        q, scopes, entity_codes=entity_codes, cancel_check=cancel_check
                    ))
                raw_semantic_count = len(semantic_items)
                semantic_items = self._fuse_semantic_evidence(semantic_items)
                filter_note = f" · spec_code={entity_codes[0]}" if len(entity_codes) == 1 else ""
                trace.append(
                    f"semantic_search: {raw_semantic_count} raw → {len(semantic_items)} fused candidates "
                    f"from {min(len(queries),4)} query variant(s){filter_note}"
                )
            except Exception as exc:
                warnings.append(f"Semantic retrieval unavailable: {type(exc).__name__}")
                trace.append(f"semantic_search: ERROR {type(exc).__name__}")
                LOGGER.warning("Agent semantic retrieval failed", exc_info=True)
            semantic_ms = int((time.perf_counter() - started) * 1000)

        # Sparse is an optional third retriever within the existing semantic
        # tool, not an LLM-selectable tool or implicit migration. A missing
        # sidecar simply leaves the original two-source path unchanged.
        if ("semantic_search" in tool_set and "sql_exact_search" in tool_set
                and getattr(self, "sparse_enabled", False)):
            sparse_started = time.perf_counter()
            try:
                for q in queries[:2]:
                    if cancel_check and cancel_check():
                        break
                    sparse_items.extend(self._sparse_search(
                        q, scopes, entity_codes=entity_codes, cancel_check=cancel_check,
                    ))
                trace.append(f"stage4_sparse: {len(sparse_items)} SQL-verified candidates")
            except Exception as exc:
                warnings.append(f"Stage 4 Sparse unavailable: {type(exc).__name__}")
                trace.append(f"stage4_sparse: ERROR {type(exc).__name__}")
                LOGGER.warning("Optional BGE-M3 sparse retrieval failed", exc_info=True)
            semantic_ms += int((time.perf_counter() - sparse_started) * 1000)

        if "sql_exact_search" in tool_set:
            started = time.perf_counter()
            try:
                for q in queries[:4]:
                    exact_items.extend(self._sql_exact_search(q, scopes))
                trace.append(f"sql_exact_search: {len(exact_items)} candidates")
            except Exception as exc:
                warnings.append(f"SQL exact retrieval unavailable: {type(exc).__name__}")
                trace.append(f"sql_exact_search: ERROR {type(exc).__name__}")
                LOGGER.warning("Agent SQL exact retrieval failed", exc_info=True)
            sql_ms = int((time.perf_counter() - started) * 1000)

        if "relation_search" in tool_set:
            started = time.perf_counter()
            try:
                for q in queries[:3]:
                    relation_items.extend(self._relation_search(q, scopes, entity_codes=entity_codes))
                trace.append(f"relation_search: {len(relation_items)} candidates")
            except Exception as exc:
                warnings.append(f"Relation retrieval unavailable: {type(exc).__name__}")
                trace.append(f"relation_search: ERROR {type(exc).__name__}")
                LOGGER.warning("Agent relation retrieval failed", exc_info=True)
            relation_ms = int((time.perf_counter() - started) * 1000)

        # When the question has no exact SPEC number, semantic and SQL hits may
        # supply bounded *search candidates*, never trusted relation endpoints.
        # Both endpoints are independently revalidated by the structural tools.
        discovered_codes = list(entity_codes)
        if not discovered_codes:
            for hit in [*semantic_items, *exact_items]:
                if (hit.scope in scopes and hit.scope != 'QABUG'
                        and hit.metadata.get('evidence_role') == 'PRIMARY_CONTENT'
                        and re.fullmatch(r'\d{2,6}', str(hit.specification_code or ''))
                        and hit.specification_code not in discovered_codes):
                    discovered_codes.append(hit.specification_code)
                    if len(discovered_codes) >= 8:
                        break
            if discovered_codes:
                trace.append(f'structural_discovery: {len(discovered_codes)} body-backed search candidate(s)')

        if "relation_search" in tool_set:
            # Optional v7 content-level edges; legacy document graph remains
            # authoritative where these evidence-backed units do not exist.
            started = time.perf_counter()
            try:
                found = self._content_relation_search(scopes, spec_codes=entity_codes)
                relation_items.extend(found)
                if found:
                    trace.append(f"content_relation_search: {len(found)} two-sided evidence pair(s)")
            except Exception as exc:
                warnings.append(f"Content-level graph unavailable: {type(exc).__name__}")
                LOGGER.warning("Optional v7 content graph retrieval failed", exc_info=True)
            relation_ms += int((time.perf_counter() - started) * 1000)

        if "relation_search" in tool_set:
            started = time.perf_counter()
            try:
                structural = self._deterministic_section_reference_search(
                    question, scopes, spec_codes=tuple(discovered_codes),
                )
                relation_items.extend(structural)
                trace.append(f"structural_reference_search: {len(structural)} verified citation(s); not dependency proof")
            except Exception as exc:
                warnings.append(f"Structural reference retrieval unavailable: {type(exc).__name__}")
                trace.append(f"structural_reference_search: ERROR {type(exc).__name__}")
                LOGGER.warning("Rule-verified structural reference retrieval failed", exc_info=True)
            relation_ms += int((time.perf_counter() - started) * 1000)

        if "relation_search" in tool_set:
            started = time.perf_counter()
            try:
                layout = self._deterministic_layout_relation_search(
                    question, scopes, spec_codes=tuple(discovered_codes),
                )
                relation_items.extend(layout)
                trace.append(f'structural_layout_search: {len(layout)} layout fact(s); never a dependency')
            except Exception as exc:
                warnings.append(f'Structural layout retrieval unavailable: {type(exc).__name__}')
                trace.append(f'structural_layout_search: ERROR {type(exc).__name__}')
                LOGGER.warning('Read-only structural layout search failed', exc_info=True)
            relation_ms += int((time.perf_counter() - started) * 1000)

        if "version_search" in tool_set:
            started = time.perf_counter()
            try:
                version_items.extend(self._version_search(question, scopes))
                trace.append(f"version_search: {len(version_items)} candidates")
            except Exception as exc:
                warnings.append(f"Version retrieval unavailable: {type(exc).__name__}")
                trace.append(f"version_search: ERROR {type(exc).__name__}")
                LOGGER.warning("Agent version retrieval failed", exc_info=True)
            version_ms += int((time.perf_counter() - started) * 1000)

        if "version_search" in tool_set:
            started = time.perf_counter()
            try:
                detail = self._requirement_version_search(scopes, spec_codes=entity_codes)
                version_items.extend(detail)
                if detail:
                    trace.append(f"requirement_version_search: {len(detail)} version-aligned source change(s)")
            except Exception as exc:
                warnings.append(f"Requirement-level version mapping unavailable: {type(exc).__name__}")
                LOGGER.warning("Optional v7 requirement version mapping failed", exc_info=True)
            version_ms += int((time.perf_counter()-started)*1000)

        if "version_search" in tool_set:
            started = time.perf_counter()
            try:
                changes = self._chapter_version_diff_search(question, scopes)
                version_items.extend(changes)
                if changes:
                    trace.append(f"chapter_version_diff_search: {len(changes)} hash-verified persisted change(s)")
            except Exception as exc:
                warnings.append(f"Chapter-level history retrieval unavailable: {type(exc).__name__}")
                LOGGER.warning("Bounded chapter diff retrieval failed", exc_info=True)
            version_ms += int((time.perf_counter() - started) * 1000)

        if ('relation_search' in tool_set and 'version_search' in tool_set
                and not re.search(r'(?:section|sec\.?|§|章节|章節|第\s*)\s*[1-9]\d*(?:\.\d+){0,6}',
                                  question, flags=re.I)):
            # Follow a maximum of two VERIFIED references to their exact target
            # section, without requiring the user to spell that section number.
            # The persisted diff itself is still checked against real body text.
            checked_sections: set[tuple[str, str]] = set()
            for ref in relation_items:
                if ref.source_type != 'STRUCTURAL_REFERENCE':
                    continue
                t = ref.metadata.get('target') or {}
                code, section = str(t.get('spec_code') or ''), str(t.get('section') or '')
                if not code or not section or (code, section) in checked_sections:
                    continue
                checked_sections.add((code, section))
                if len(checked_sections) > 2:
                    break
                try:
                    # Explicit numeric code is used as a filter, not as a fact.
                    # Even if other documents match the question, the joint
                    # verifier allows only this exact cited target identity.
                    version_items.extend(self._chapter_version_diff_search(
                        f'{question} SPEC {code} section {section}', scopes,
                    ))
                except Exception as exc:
                    warnings.append(f'Reference-target historical lookup unavailable: {type(exc).__name__}')
                    trace.append(f'reference_target_diff: ERROR {type(exc).__name__}')
                    LOGGER.warning('Bounded reference target diff lookup failed', exc_info=True)
            if checked_sections:
                trace.append(f'reference_target_diff: {min(2, len(checked_sections))} cited section(s) checked')

        if 'relation_search' in tool_set and 'version_search' in tool_set:
            joined = self._joint_reference_change_alerts(relation_items, version_items)
            relation_items.extend(joined)
            if joined:
                trace.append(f'reference_diff_joint: {len(joined)} revalidation alert(s), no impact claim')

        if "unresolved_reference_search" in tool_set:
            started = time.perf_counter()
            try:
                unresolved_items.extend(self._unresolved_reference_search(question, scopes))
                trace.append(f"unresolved_reference_search: {len(unresolved_items)} candidates")
            except Exception as exc:
                warnings.append(f"Unresolved-reference retrieval unavailable: {type(exc).__name__}")
                trace.append(f"unresolved_reference_search: ERROR {type(exc).__name__}")
                LOGGER.warning("Agent unresolved reference retrieval failed", exc_info=True)
            relation_ms += int((time.perf_counter() - started) * 1000)

        if "specification_catalog" in tool_set:
            started = time.perf_counter()
            try:
                found, catalog_warnings = self._specification_catalog(question, scopes)
                catalog_items.extend(found)
                warnings.extend(catalog_warnings)
                trace.append(f"specification_catalog: {len(found)} scope result(s)")
            except Exception as exc:
                warnings.append(f"Specification catalog unavailable: {type(exc).__name__}")
                trace.append(f"specification_catalog: ERROR {type(exc).__name__}")
            sql_ms += int((time.perf_counter() - started) * 1000)

        if "specification_lookup" in tool_set:
            started = time.perf_counter()
            try:
                # Title discovery accepts alternate orthographies/translations,
                # but a hit is only a candidate with real SQL identity metadata.
                for q in queries[:4]:
                    if cancel_check and cancel_check():
                        break
                    found, lookup_warnings = self._specification_lookup(q, tuple(x for x in scopes if x != "QABUG"))
                    catalog_items.extend(found)
                    warnings.extend(lookup_warnings)
                trace.append(f"specification_lookup: {len(catalog_items)} SQL identity candidate(s)")
            except Exception as exc:
                warnings.append(f"Specification lookup unavailable: {type(exc).__name__}")
                trace.append(f"specification_lookup: ERROR {type(exc).__name__}")
            sql_ms += int((time.perf_counter() - started) * 1000)

        if "issue_search" in tool_set or "issue_statistics" in tool_set:
            started = time.perf_counter()
            try:
                issue_filters = self._parse_issue_filters(question)
                if "issue_statistics" in tool_set:
                    issue_items.extend(self._issue_statistics(question, scopes, issue_filters))
                    trace.append(f"issue_statistics: {len(issue_items)} scope result(s)")
                else:
                    issue_items.extend(self._issue_sql_search(question, scopes, issue_filters))
                    trace.append(f"issue_search: {len(issue_items)} candidate(s)")
            except Exception as exc:
                warnings.append(f"Issue retrieval unavailable: {type(exc).__name__}")
                trace.append(f"issue retrieval: ERROR {type(exc).__name__}")
            sql_ms += int((time.perf_counter() - started) * 1000)

        # A bounded second query can be derived from AUTHORITATIVE QA/BUG
        # observations when a joint query had no SPEC_BODY retrieval hit.
        # The issue key and model prose are not used to infer spec versions.
        # Results still flow through the normal SQL/Dense provenance gates.
        live_skill = build_live_skill_workflow(question)
        if ("engineering-qa-bug-analysis" in live_skill.skills
                and "sql_exact_search" in tool_set
                and not exact_items and not semantic_items
                and not (cancel_check and cancel_check())):
            symptom_queries = issue_symptom_queries(issue_items)
            engineering_scopes = tuple(s for s in scopes if s != "QABUG")
            for sq in symptom_queries[:2]:
                if not engineering_scopes or (cancel_check and cancel_check()):
                    break
                started = time.perf_counter()
                try:
                    found = self._sql_exact_search(sq, engineering_scopes)
                    # Explicit spec identifiers remain search filters for
                    # this second pass, not evidence of applicable SW version.
                    exact_items.extend(
                        item for item in found
                        if not entity_codes or item.specification_code in entity_codes
                    )
                except Exception as exc:
                    warnings.append(f"Symptom-backed SQL retrieval unavailable: {type(exc).__name__}")
                sql_ms += int((time.perf_counter() - started) * 1000)
                if exact_items:
                    break
            if symptom_queries:
                trace.append(f"issue_symptom_followup: {min(2, len(symptom_queries))} bounded SQL query/queries, "
                             f"{len(exact_items)} body hit(s); NOT a version/conformance mapping")

        # Independent display-only list for explicitly requested cross-domain
        # historical BUG + specification comparisons. DO NOT merge into model
        # evidence: a lexical candidate cannot prove compliance or dependency.
        if (re.search(r"(?<![A-Z0-9])[A-Z][A-Z0-9]{1,19}-\d{1,10}(?![A-Z0-9])", question, re.I)
                and re.search(r"式样|式樣|仕様|规格|規格|spec|requirement|符合|满足|滿足|对照|對照|照合", question, re.I)
                and re.search(r"(?:bug|qa|缺陷|不具合|故障|问题单|問題單|是否|关联|關聯|符合|满足|滿足|对照|對照|照合)", question, re.I)):
            try:
                candidate_suggestions = self._bug_spec_candidate_search(question)
                trace.append(f"bug_spec_candidate_display_only: {len(candidate_suggestions)} unreviewed, non-LLM candidate(s)")
            except Exception as exc:
                warnings.append(f"BUG/SPEC candidate index unavailable: {type(exc).__name__}")
                trace.append(f"bug_spec_candidate_display_only: ERROR {type(exc).__name__}")
                LOGGER.warning("BUG/SPEC candidate lookup rejected", exc_info=True)

        # Stage 4 phase 1: fuse *body-only* SQL and Dense chunk rankings.
        # Structured catalog, relationships, history, and QA/BUG retain their
        # independent evidentiary status; do not RRF them into content chunks.
        if "sql_exact_search" in tool_set and "semantic_search" in tool_set:
            hybrid_chunks = fuse_spec_rrf(
                exact_items, semantic_items, sparse_hits=sparse_items,
                max_candidates=max(self.top_k * 5, 30),
            )
            trace.append(
                f"stage4_rrf: SQL={len(exact_items)} Dense={len(semantic_items)} "
                f"Sparse={len(sparse_items)} → {len(hybrid_chunks)} unique body chunk(s); "
                "ranking only, no reranker"
            )
        else:
            hybrid_chunks = [*exact_items, *semantic_items]
        if ("sql_exact_search" in tool_set and "semantic_search" in tool_set
                and hybrid_chunks and os.environ.get("DIC_STAGE4_RERANK_ENABLED", "0") == "1"):
            from app.ai.stage4_reranker import rerank_spec_chunks
            hybrid_chunks, rerank_status = rerank_spec_chunks(question, hybrid_chunks)
            trace.append(f"stage4_reranker: {rerank_status.state}; "
                         f"candidates={rerank_status.candidates}; elapsed_ms={rerank_status.elapsed_ms}")
            if rerank_status.state in {"FAILED", "TIMEOUT", "BUSY"}:
                warnings.append(f"Optional Stage 4 reranker: {rerank_status.state}; original RRF ranks preserved")

        # Keep a wider candidate pool for governance upstream.
        merged: list[EvidenceItem] = []
        seen: set[tuple[Any, ...]] = set()
        for item in [*catalog_items, *issue_items, *relation_items, *version_items, *unresolved_items, *hybrid_chunks]:
            key = (item.scope, item.source_type, item.content_id, item.relation_id, item.specification_code, item.section_id, item.content[:120])
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
            if len(merged) >= max(self.top_k * 5, 30):
                break
        return RetrievalResult(
            evidence=tuple(merged),
            resolved_scopes=scopes,
            semantic_ms=semantic_ms,
            sql_ms=sql_ms,
            relation_ms=relation_ms,
            version_ms=version_ms,
            warnings=tuple(warnings),
            trace=tuple(trace),
            candidate_suggestions=tuple(candidate_suggestions),
        )

    # ------------------------------------------------------------------
    # Structured enterprise retrieval: catalog + QA/BUG issue registers
    # ------------------------------------------------------------------

    def retrieve_spec_catalog(
        self,
        question: str,
        *,
        scope: str = "AUTO",
        current_scope: str = "",
    ) -> RetrievalResult:
        scopes = self._factual_spec_scopes(scope, question, current_scope)
        started = time.perf_counter()
        evidence, warnings = self._specification_catalog(question, scopes)
        elapsed = int((time.perf_counter() - started) * 1000)
        return RetrievalResult(
            evidence=tuple(evidence),
            resolved_scopes=scopes,
            sql_ms=elapsed,
            warnings=tuple(warnings),
            plan_source="structured-fast-path",
            plan_summary="Authoritative specification catalog query from PostgreSQL.",
            intents=("SPECIFICATION_CATALOG",),
            tools=("specification_catalog",),
            rewritten_queries=(question,),
            evidence_strength="STRONG" if evidence else "NONE",
            trace=(f"specification_catalog: {len(evidence)} scope result(s) · {elapsed} ms",),
            route="SPEC_CATALOG",
            knowledge_source="POSTGRES_STRUCTURED",
        )

    def retrieve_spec_lookup(
        self,
        question: str,
        *,
        scope: str = "AUTO",
        current_scope: str = "",
    ) -> RetrievalResult:
        scopes = self._factual_spec_scopes(scope, question, current_scope)
        started = time.perf_counter()
        evidence, warnings = self._specification_lookup(question, scopes)
        elapsed = int((time.perf_counter() - started) * 1000)
        return RetrievalResult(
            evidence=tuple(evidence),
            resolved_scopes=scopes,
            sql_ms=elapsed,
            warnings=tuple(warnings),
            plan_source="structured-fast-path",
            plan_summary="Exact specification identity/current-version lookup from PostgreSQL.",
            intents=("SPECIFICATION_LOOKUP",),
            tools=("specification_lookup",),
            rewritten_queries=(question,),
            evidence_strength=("STRONG" if any(x.source_type == "SPEC_LOOKUP" for x in evidence)
                               else "LIMITED" if evidence else "NONE"),
            trace=(f"specification_lookup: {len(evidence)} match(es) · {elapsed} ms",),
            route="SPEC_LOOKUP",
            knowledge_source="POSTGRES_STRUCTURED",
        )

    def retrieve_spec_relations(
        self,
        question: str,
        *,
        scope: str = "AUTO",
        current_scope: str = "",
    ) -> RetrievalResult:
        """Read an exact specification graph neighborhood without an LLM."""
        scopes = self._factual_spec_scopes(scope, question, current_scope)
        started = time.perf_counter()
        codes = self._entity_spec_codes((), question)
        graph_evidence = self._relation_search(question, scopes, entity_codes=codes)
        # The reviewed relation registry is not the only factual source. A
        # specification may contain explicit section references that have not
        # yet been promoted into the graph. Read those source-exact citations
        # deterministically and label them as structural references, not as
        # reviewed functional dependencies.
        try:
            structural_evidence = self._deterministic_section_reference_search(
                question, scopes, spec_codes=codes,
            )
        except Exception as exc:
            # Older/import-only databases may not have the Stage-2 requirement
            # tables yet. Explicit document-level references in public.contents
            # are still factual and must remain queryable.
            LOGGER.info("Requirement-level relation scan unavailable: %s", type(exc).__name__)
            structural_evidence = []
        document_references = self._document_reference_search(question, scopes, spec_codes=codes)
        evidence = [*graph_evidence, *structural_evidence, *document_references]
        elapsed = int((time.perf_counter() - started) * 1000)
        return RetrievalResult(
            evidence=tuple(evidence),
            resolved_scopes=scopes,
            sql_ms=elapsed,
            plan_source="structured-fast-path",
            plan_summary="Deterministic incoming/outgoing specification relation graph lookup.",
            intents=("RELATION_LOOKUP",),
            tools=("relation_search",),
            rewritten_queries=(question,),
            evidence_strength=("STRONG" if evidence else "NONE"),
            trace=(
                f"relation_graph: {len(graph_evidence)} result(s); "
                f"structural_references: {len(structural_evidence)} result(s); "
                f"document_references: {len(document_references)} result(s) · {elapsed} ms",
            ),
            route="SPEC_RELATIONS",
            knowledge_source="POSTGRES_STRUCTURED",
        )

    def _document_reference_search(
        self,
        question: str,
        scopes: Iterable[str],
        *,
        spec_codes: tuple[str, ...] = (),
    ) -> list[EvidenceItem]:
        """Extract explicit document-level specification mentions from contents.

        These are source-exact references, not inferred dependencies. This path
        intentionally works before the optional requirement-unit/graph pipeline
        has been created.
        """
        codes = spec_codes or self._entity_spec_codes((), question)
        found, _ = self._specification_lookup(question, tuple(scopes))
        source_marker = re.search(
            r"Conversation source specification:\s*SPEC[-_: ]*(\d{3,6})", question, re.I,
        )
        target_marker = re.search(
            r"Conversation target specification:\s*SPEC[-_: ]*(\d{3,6})", question, re.I,
        )
        explicit_pair = re.search(
            r"(?<!\d)(\d{3,6})(?!\d).{0,40}(?:引用|参照|參照|参考|參考|依赖|依賴|关联|關聯|关系|關係|有关|有關|对应|對應|和|与|與).{0,40}(?<!\d)(\d{3,6})(?!\d)",
            question, re.I,
        )
        source_codes: tuple[str, ...] = ()
        requested_targets: tuple[str, ...] = ()
        if source_marker and target_marker:
            source_codes = (source_marker.group(1),)
            requested_targets = (target_marker.group(1),)
        elif explicit_pair:
            source_codes = (explicit_pair.group(1),)
            requested_targets = (explicit_pair.group(2),)
        if source_codes:
            found = [item for item in found if str(item.specification_code or "") in source_codes]
        incoming_requested = (not source_codes) and bool(re.search(
            r"(?:哪些|什么|什麼|谁|誰).{0,20}(?:引用|参照|參照|参考|參考|依赖|依賴).{0,20}(?:了|到|向)?\s*(?:SPEC[-_: ]*)?\d|"
            r"(?:被谁|被誰|被哪些|被什么|被什麼|谁在|誰在).{0,12}(?:引用|参照|參照|依赖|依賴)|"
            r"(?:incoming|inbound|reverse\s+reference)",
            question,
            re.I,
        ))
        if not codes:
            # Name-based identity must require all meaningful Latin title words,
            # not the broad OR semantics used for discovery. This prevents a
            # title such as “User Profile function Spec” from selecting every
            # document that happens to contain “User”.
            cleaned = re.sub(
                r"(?:引用了?|参照了?|參照了?|参考了?|參考了?|依赖了?|依賴了?|关联|關聯|关系|關係|"
                r"哪些|什么|什麼|其他|其它|式样书|式樣書|仕様書|规格书|規格書|references?|depends?\s+on|which|what)",
                " ", question, flags=re.I,
            )
            title_tokens = [
                token.casefold() for token in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", cleaned)
                if token.casefold() not in {"spec", "specification", "document"}
            ]
            if len(title_tokens) >= 2:
                narrowed = []
                for item in found:
                    haystack = f"{item.specification_name} {item.file_name}".casefold()
                    if all(token in haystack for token in title_tokens):
                        narrowed.append(item)
                if narrowed:
                    found = narrowed
            codes = tuple(dict.fromkeys(str(item.specification_code) for item in found if item.specification_code))[:8]
        result: list[EvidenceItem] = []
        for scope in scopes:
            documents = [] if incoming_requested else [item for item in found if item.scope == scope and item.document_id]
            for document in documents[:8]:
                with self._connect(scope) as conn, conn.cursor() as cur:
                    cur.execute("SET TRANSACTION READ ONLY")
                    cur.execute(
                        "SELECT id, section_id, page_number, content FROM public.contents "
                        "WHERE document_id=%s ORDER BY id LIMIT 5000",
                        (document.document_id,),
                    )
                    rows = [dict(row) for row in cur.fetchall()]
                    references: dict[str, dict[str, Any]] = {}
                    for row in rows:
                        text = str(row.get("content") or "")
                        for target in self._extract_document_reference_codes(text):
                            if target == str(document.specification_code or "").upper() or re.fullmatch(r"(?:19|20)\d{2}", target):
                                continue
                            entry = references.setdefault(target, {"sections": set(), "content_ids": set(), "samples": []})
                            entry["sections"].add(str(row.get("section_id") or "-") )
                            entry["content_ids"].add(int(row["id"]))
                            if len(entry["samples"]) < 2:
                                entry["samples"].append(text[:900])
                    if not references:
                        continue
                    if requested_targets:
                        references = {
                            code: value for code, value in references.items()
                            if code in requested_targets
                        }
                        if not references:
                            continue
                    target_codes = list(references)
                    cur.execute(
                        "SELECT spec_code, canonical_name FROM public.specifications "
                        "WHERE project_code=%s AND spec_code=ANY(%s::text[])",
                        (scope, target_codes),
                    )
                    names = {
                        str(row["spec_code"]): str(row.get("canonical_name") or "")
                        for row in (dict(value) for value in cur.fetchall())
                    }
                lines = [
                    f"Explicit document references in {document.file_name} "
                    f"(source SPEC {document.specification_code}, version={document.specification_version}):"
                ]
                for target in sorted(references, key=lambda x: (not x.isdigit(), int(x) if x.isdigit() else x)):
                    ref = references[target]
                    label = names.get(target) or "target name not registered"
                    status = "REGISTERED" if target in names else "UNRESOLVED_TARGET"
                    sections = ", ".join(sorted(ref["sections"]))
                    content_ids = ",".join(str(x) for x in sorted(ref["content_ids"]))
                    lines.append(
                        f"- {target} | {label} | {status} | source_sections={sections} | content_ids={content_ids}"
                    )
                result.append(EvidenceItem(
                    evidence_id="", scope=scope, source_type="DOCUMENT_REFERENCE",
                    content="\n".join(lines), score=1.0,
                    document_id=document.document_id, file_name=document.file_name,
                    specification_code=document.specification_code,
                    specification_name=document.specification_name,
                    specification_version=document.specification_version,
                    metadata={
                        "reference_count": len(references),
                        "reference_codes": tuple(target_codes),
                        "requested_target_codes": requested_targets,
                        "reference_sources": {
                            code: {
                                "sections": tuple(sorted(value["sections"])),
                                "content_ids": tuple(sorted(value["content_ids"])),
                                "samples": tuple(value["samples"]),
                            }
                            for code, value in references.items()
                        },
                    },
                ))
        if codes and incoming_requested:
            result.extend(self._incoming_document_reference_search(codes, scopes))
        if (not incoming_requested and result and re.search(
                r"(?:哪段|哪一段|哪些内容|哪些內容|具体哪些内容|具體哪些內容|对应内容|對應內容|内容对应|內容對應|内容.{0,8}(?:有关|有關|相关|相關|关联|關聯)|相关片段|相關片段|目标内容|目標內容|content\s+to\s+content)",
                question, re.I)):
            sources = [item for item in result if item.source_type == "DOCUMENT_REFERENCE"]
            target_codes = tuple(dict.fromkeys(
                code for item in sources for code in tuple((item.metadata or {}).get("reference_codes") or ())
            ))[:24]
            query_parts = [
                sample for item in sources
                for value in dict((item.metadata or {}).get("reference_sources") or {}).values()
                for sample in tuple(value.get("samples") or ())
            ][:8]
            if target_codes and query_parts:
                try:
                    candidates = self._semantic_search(
                        "\n".join(query_parts), scopes, entity_codes=target_codes,
                        source_type="REFERENCE_TARGET_CANDIDATE",
                    )
                except Exception:
                    candidates = []
                for candidate in candidates[:12]:
                    source_locations = []
                    for item in sources:
                        source = dict((item.metadata or {}).get("reference_sources") or {}).get(candidate.specification_code)
                        if source:
                            source_locations.append({
                                "document_id": item.document_id,
                                "file_name": item.file_name,
                                "sections": source.get("sections"),
                                "content_ids": source.get("content_ids"),
                            })
                    result.append(replace(
                        candidate,
                        source_type="REFERENCE_TARGET_CANDIDATE",
                        content=(
                            "[SEMANTIC TARGET CANDIDATE — NOT A CONFIRMED REFERENCE EDGE]\n"
                            + candidate.content
                        ),
                        metadata={
                            **dict(candidate.metadata or {}),
                            "relation_semantics": "SEMANTIC_CANDIDATE",
                            "human_confirmed": False,
                            "source_reference_locations": tuple(source_locations),
                        },
                    ))
        return result

    @staticmethod
    def _extract_document_reference_codes(text: str) -> tuple[str, ...]:
        """Extract only explicit specification-number syntax from source text."""
        value = str(text or "")
        candidates: set[str] = set()
        if re.search(r"Specification\s+name\s*\|\s*Specification\s+No", value, re.I):
            candidates.update(re.findall(r"(?m)^\s*([A-Z]?\d{3}(?:_[A-Z]{2})?)\s*$", value, re.I))
            candidates.update(
                item.replace(" ", "_")
                for item in re.findall(
                    r"\|\s*([A-Z]?\d{3}(?:[_ ](?:NA|JP|EU))?)\s*(?:\n|$)", value, re.I,
                )
            )
        candidates.update(re.findall(r"\[([A-Z]?\d{3}(?:_[A-Z]{2})?)\]", value, re.I))
        candidates.update(re.findall(
            r"(?:spec(?:ification)?(?:\s+no\.?)?|仕様書)\s*[:#]?\s*\[?([A-Z]?\d{3}(?:_[A-Z]{2})?)\]?",
            value, re.I,
        ))
        candidates.update(re.findall(
            r"(?<![A-Za-z0-9])([A-Z]?\d{3}(?:_[A-Z]{2})?)_(?=[A-Za-z][^\n]{0,100}\bSpec)",
            value, re.I,
        ))
        return tuple(sorted(
            (item.upper() for item in candidates if not re.fullmatch(r"(?:19|20)\d{2}", item)),
            key=lambda item: (not item.isdigit(), int(item) if item.isdigit() else item),
        ))

    def _incoming_document_reference_search(
        self, target_codes: tuple[str, ...], scopes: Iterable[str],
    ) -> list[EvidenceItem]:
        """Find current SPEC_BODY documents that explicitly cite target codes."""
        primary = PrimarySpecificationDocumentPolicy.sql_primary_predicate("d")
        preferred = PrimarySpecificationDocumentPolicy.sql_preferred_body_version_id("sp")
        result: list[EvidenceItem] = []
        for scope in scopes:
            matches: dict[str, dict[str, Any]] = {}
            with self._connect(scope) as conn, conn.cursor() as cur:
                cur.execute("SET TRANSACTION READ ONLY")
                patterns = [f"%{code}%" for code in target_codes]
                cur.execute(f"""
                    SELECT sp.spec_code, sp.canonical_name, sv.version_label, sv.document_id,
                           d.file_name, c.id AS content_id, c.section_id, c.content
                    FROM public.specifications sp
                    JOIN public.specification_versions sv ON sv.id={preferred}
                    JOIN public.documents d ON d.document_id=sv.document_id
                    JOIN public.contents c ON c.document_id=sv.document_id
                    WHERE sp.project_code=%s AND sp.spec_code<>ALL(%s::text[])
                      AND sv.branch_name='MAIN' AND {primary}
                      AND c.content ILIKE ANY(%s::text[])
                    ORDER BY c.id LIMIT 5000
                """, (scope, list(target_codes), patterns))
                for raw in cur.fetchall():
                    row = dict(raw)
                    cited = set(self._extract_document_reference_codes(str(row.get("content") or "")))
                    overlap = cited.intersection(target_codes)
                    if not overlap:
                        continue
                    source = str(row.get("spec_code") or "")
                    entry = matches.setdefault(source, {
                        "name": str(row.get("canonical_name") or ""),
                        "version": str(row.get("version_label") or ""),
                        "document_id": str(row.get("document_id") or ""),
                        "file_name": str(row.get("file_name") or ""),
                        "targets": set(), "sections": set(), "content_ids": set(),
                    })
                    entry["targets"].update(overlap)
                    entry["sections"].add(str(row.get("section_id") or "-"))
                    entry["content_ids"].add(int(row["content_id"]))
            if matches:
                lines = [f"Current SPEC_BODY documents explicitly referencing {', '.join(target_codes)} (scope={scope}):"]
                for source, entry in sorted(matches.items()):
                    lines.append(
                        f"- source={source} {entry['name']} | targets={','.join(sorted(entry['targets']))} | "
                        f"version={entry['version']} | sections={','.join(sorted(entry['sections']))} | "
                        f"content_ids={','.join(str(x) for x in sorted(entry['content_ids']))}"
                    )
                result.append(EvidenceItem(
                    evidence_id="", scope=scope, source_type="INCOMING_DOCUMENT_REFERENCE",
                    content="\n".join(lines), score=1.0,
                    specification_code=target_codes[0],
                    metadata={"direction": "INCOMING", "source_count": len(matches), "target_codes": target_codes},
                ))
        return result

    @classmethod
    def _factual_spec_scopes(cls, scope: str, question: str, current_scope: str = "") -> tuple[str, ...]:
        """Do not append COMMON to a specifically named project fact query."""
        chosen = cls.resolve_scopes(scope, question, current_scope)
        upper = str(question).upper()
        requested = str(scope or 'AUTO').upper()
        if requested == 'AUTO' and ('21MM' in upper) != ('24MM' in upper):
            target = '21MM' if '21MM' in upper else '24MM'
            return (target,)
        return chosen

    def retrieve_spec_outline(self, question: str, *, scope: str = 'AUTO', current_scope: str = '') -> RetrievalResult:
        from app.ai.spec_facts import lookup_outline
        scopes = self._factual_spec_scopes(scope, question, current_scope)
        started = time.perf_counter()
        rows, warnings = lookup_outline(self, question, scopes)
        elapsed = int((time.perf_counter() - started) * 1000)
        return RetrievalResult(evidence=tuple(rows), resolved_scopes=scopes,
            sql_ms=elapsed, warnings=tuple(warnings), plan_source='structured-fast-path',
            plan_summary='PostgreSQL SPEC_BODY chapter and section directory (no LLM).',
            intents=('SPECIFICATION_OUTLINE',), tools=('specification_lookup',),
            route='SPEC_OUTLINE', knowledge_source='POSTGRES_STRUCTURED',
            evidence_strength=('STRONG' if any(x.source_type == 'SPEC_OUTLINE' for x in rows)
                               else 'LIMITED' if rows else 'NONE'))

    def retrieve_spec_chapter_content(self, question: str, *, scope: str = 'AUTO', current_scope: str = '') -> RetrievalResult:
        from app.ai.spec_facts import lookup_chapter_content
        scopes = self._factual_spec_scopes(scope, question, current_scope)
        started = time.perf_counter()
        rows, warnings = lookup_chapter_content(self, question, scopes)
        elapsed = int((time.perf_counter() - started) * 1000)
        return RetrievalResult(evidence=tuple(rows), resolved_scopes=scopes,
            sql_ms=elapsed, warnings=tuple(warnings), plan_source='structured-fast-path',
            plan_summary='Bounded PostgreSQL chapter content excerpts (no LLM).',
            intents=('SPEC_CHAPTER_CONTENT',), tools=('specification_lookup',),
            route='SPEC_CHAPTER_CONTENT', knowledge_source='POSTGRES_STRUCTURED',
            evidence_strength=('LIMITED' if rows else 'NONE'))

    def retrieve_spec_version_facts(self, question: str, *, scope: str = 'AUTO', current_scope: str = '') -> RetrievalResult:
        from app.ai.spec_facts import lookup_adjacent_versions
        scopes = self._factual_spec_scopes(scope, question, current_scope)
        started = time.perf_counter()
        rows, warnings = lookup_adjacent_versions(self, question, scopes)
        elapsed = int((time.perf_counter() - started) * 1000)
        return RetrievalResult(evidence=tuple(rows), resolved_scopes=scopes,
            sql_ms=elapsed, warnings=tuple(warnings), plan_source='structured-fast-path',
            plan_summary='Adjacent MAIN SPEC_BODY version/diff inventory (no LLM).',
            intents=('BODY_VERSION_FACTS',), tools=('version_search',),
            route='SPEC_VERSION_FACTS', knowledge_source='POSTGRES_STRUCTURED',
            evidence_strength=('STRONG' if any(x.metadata.get('identity_verified', True) for x in rows)
                               else 'LIMITED' if rows else 'NONE'))

    @staticmethod
    def _issue_query_mode(question: str, filters: dict[str, str] | None = None) -> str:
        """Classify an issue request without invoking the LLM planner.

        EXACT is reserved for an explicit Jira/issue key.  FIND_ONE is a
        retrieval request asking for one representative ticket.  SEARCH covers
        descriptive/fuzzy issue discovery.  Keeping these modes separate avoids
        presenting a semantic candidate as an exact-key lookup.
        """
        filters = filters or {}
        if str(filters.get("issue_key") or "").strip():
            return "EXACT"
        q = str(question or "")
        if re.search(
            r"(?:找(?:一|1)(?:个|個|条|條)?|给我(?:找)?(?:一|1)(?:个|個|条|條)?|給我(?:找)?(?:一|1)(?:個|條)?|"
            r"推荐(?:一|1)(?:个|個|条|條)?|推薦(?:一|1)(?:個|條)?|举(?:一|1)(?:个|個)?|舉(?:一|1)(?:個)?|"
            r"one\s+(?:jira|bug|issue|ticket)|(?:jira|issue|ticket)\s*(?:号|號|key|id))",
            q,
            re.I,
        ):
            return "FIND_ONE"
        return "SEARCH"

    @staticmethod
    def _issue_free_text_query(question: str, filters: dict[str, str] | None = None) -> str:
        """Reduce issue-search prose to the terms that carry semantic meaning.

        Example: ``我想找个关于WIFI的BUG 给我找一个JIRA号`` becomes ``WIFI``.
        The output is used for Qdrant embedding and bounded SQL text matching;
        structured predicates remain in ``filters`` and are never rewritten here.
        """
        q = re.sub(r"\s+", " ", str(question or "").strip())
        if not q:
            return ""
        filters = filters or {}
        issue_key = str(filters.get("issue_key") or "").strip()
        if issue_key:
            return ""

        # Remove explicit field:value clauses after they have been captured by
        # the reviewed filter parser.  This prevents metadata syntax from
        # dominating the semantic embedding.
        q = re.sub(
            r"(?:status|ステータス|状态|狀態|priority|優先度|优先度|component|module|组件|組件|"
            r"コンポーネント|assignee|owner|担当者|负责人|負責人|reporter|報告者|release(?:\s*plan)?\s*version|"
            r"fix\s*version|リリース計画バージョン|affected\s*version|software\s*version|ソフトバージョン|"
            r"environment|発生環境|root\s*cause|根本原因|countermeasure|対策内容|effect\s*(?:confirmation\s*)?result)"
            r"\s*[:=：]\s*[^,，;；\n]+",
            " ",
            q,
            flags=re.I,
        )
        # Strip request scaffolding and repository nouns while preserving the
        # actual symptom/technology phrase (WiFi, CarPlay, 黑屏, 蓝牙, etc.).
        scaffolding = (
            r"(?:我想|我想要|想要|请|請|麻烦|麻煩|帮我|幫我|给我|給我|能不能|可以|请问|請問|"
            r"查一下|查找|查询|查詢|检索|檢索|搜索|搜尋|找一下|找个|找個|找一个|找一個|找|"
            r"有哪些|有什麼|有什么|列出|列表|关于|關於|有关|有關|相关|相關|里面|裡面|数据库里|資料庫裡|库里|庫裡|"
            r"一个|一個|一条|一條|任意|随便|隨便|给出|給出|返回|告诉我|告訴我)"
        )
        q = re.sub(scaffolding, " ", q, flags=re.I)
        q = re.sub(
            r"(?<![A-Za-z0-9])(?:jira|bug|qa|issue|ticket|defect)(?![A-Za-z0-9])|"
            r"(?:JIRA号|JIRA號|问题单|問題單|課題|不具合|バグ|缺陷|故障)",
            " ",
            q,
            flags=re.I,
        )
        # Common Chinese particles are removed only when they stand around
        # whitespace after the previous normalization; technical CJK terms stay.
        q = re.sub(r"[，,。.!！?？;；:：()（）\[\]{}<>《》\"“”'‘’]+", " ", q)
        q = re.sub(r"\s+", " ", q).strip()
        q = re.sub(r"^(?:的|和|与|與|及|中|里|裡)\s+|\s+(?:的|和|与|與|及|中|里|裡)$", "", q)
        if not q:
            return ""

        # Keep a small number of distinct technical terms/phrases.  This also
        # protects local CPU embedding latency from unnecessarily long queries.
        terms: list[str] = []
        for token in re.findall(r"[A-Za-z][A-Za-z0-9_.+/#:-]{1,40}|[\u3400-\u9fff]{2,12}|[\u3040-\u30ff]{2,20}", q):
            clean = token.strip()
            if re.fullmatch(r"[\u3400-\u9fff]+", clean):
                clean = re.sub(r"(?:相关|相關|有关|有關|的)$", "", clean)
            if not clean:
                continue
            if clean.lower() in {"jira", "bug", "issue", "ticket", "defect"}:
                continue
            if clean not in terms:
                terms.append(clean)
            if len(terms) >= 8:
                break
        return " ".join(terms) or q[:160]

    @staticmethod
    def _issue_logical_key(item: EvidenceItem) -> tuple[str, str]:
        md = item.metadata or {}
        issue_key = str(md.get("issue_key") or "").strip().upper()
        logical_id = str(md.get("logical_issue_id") or md.get("record_key") or item.document_id or "").strip().upper()
        logical = issue_key or logical_id
        if not logical and item.content_id is not None:
            logical = f"CONTENT:{item.content_id}"
        return item.scope, logical

    @classmethod
    def _fuse_issue_candidates(
        cls,
        sql_items: list[EvidenceItem],
        semantic_items: list[EvidenceItem],
        *,
        mode: str,
    ) -> list[EvidenceItem]:
        """Fuse SQL authority with Qdrant relevance by logical issue identity.

        SQL remains authoritative for record fields.  When a semantic hit points
        at the same issue, the SQL evidence is retained but inherits the Qdrant
        relevance score and is marked ISSUE_HYBRID.  This fixes the old
        first-SQL-wins behavior that discarded semantic ranking.
        """
        if mode == "EXACT":
            return list(sql_items)
        semantic_by_key: dict[tuple[str, str], EvidenceItem] = {}
        for item in semantic_items:
            key = cls._issue_logical_key(item)
            if not key[1]:
                continue
            existing = semantic_by_key.get(key)
            if existing is None or float(item.score or 0.0) > float(existing.score or 0.0):
                semantic_by_key[key] = item

        merged: list[EvidenceItem] = []
        seen: set[tuple[str, str]] = set()
        for item in sql_items:
            key = cls._issue_logical_key(item)
            sem = semantic_by_key.get(key)
            if sem is not None:
                md = dict(item.metadata)
                md["semantic_score"] = float(sem.score or 0.0)
                md["retrieval_sources"] = ["POSTGRESQL", "QDRANT"]
                md["semantic_section_type"] = str((sem.metadata or {}).get("section_type") or sem.section_id or "")
                item = replace(item, source_type="ISSUE_HYBRID", score=sem.score, metadata=md)
            merged.append(item)
            if key[1]:
                seen.add(key)

        for item in semantic_items:
            key = cls._issue_logical_key(item)
            if key[1] and key in seen:
                continue
            merged.append(item)
            if key[1]:
                seen.add(key)

        if mode in {"SEARCH", "FIND_ONE"}:
            # Hybrid/Qdrant relevance is the primary ordering for fuzzy search;
            # SQL-only lexical matches remain useful fallbacks below them.
            merged.sort(
                key=lambda x: (
                    1 if x.source_type == "ISSUE_HYBRID" else 0,
                    float(x.score or 0.0),
                    1 if x.source_type == "ISSUE_SQL_MATCH" else 0,
                ),
                reverse=True,
            )
        return merged

    def retrieve_issue_register(
        self,
        question: str,
        *,
        scope: str = "AUTO",
        current_scope: str = "",
        statistics: bool = False,
        cancel_check: Callable[[], bool] | None = None,
    ) -> RetrievalResult:
        scopes = self.resolve_issue_scopes(scope, question, current_scope)
        filters = self._parse_issue_filters(question)
        mode = "STATS" if statistics else self._issue_query_mode(question, filters)
        semantic_query = "" if statistics else self._issue_free_text_query(question, filters)
        warnings: list[str] = []
        trace: list[str] = [f"issue_mode={mode}"]
        if semantic_query:
            trace.append(f"issue_semantic_query={semantic_query[:160]}")

        started = time.perf_counter()
        if statistics:
            sql_items = self._issue_statistics(question, scopes, filters)
        else:
            # SQL uses the compacted free-text terms for fuzzy discovery, while
            # exact structured predicates are still parsed from the original text.
            sql_question = semantic_query or question
            sql_items = self._issue_sql_search(sql_question, scopes, filters)
        sql_ms = int((time.perf_counter() - started) * 1000)
        trace.append(
            f"{'issue_statistics' if statistics else 'issue_search'}: {len(sql_items)} structured result(s) · {sql_ms} ms"
        )

        semantic_items: list[EvidenceItem] = []
        semantic_ms = 0
        semantic_allowed = bool(not statistics and not filters.get("issue_key") and semantic_query)
        structured_only_filters: tuple[str, ...] = ()
        if scopes == ("QABUG",):
            structured_only_filters = self._qabug_structured_only_filter_keys(filters)
            if structured_only_filters:
                semantic_allowed = False
                trace.append(
                    "issue_semantic_search: disabled; SQL-only constraints="
                    + ",".join(structured_only_filters)
                )

        # Exact counts/aggregates are never approximated by vectors. Descriptive
        # issue searches embed only the distilled semantic query and use Qdrant
        # filters for every mirrorable structured predicate.
        if semantic_allowed:
            sem_started = time.perf_counter()
            try:
                semantic_items = self._semantic_search(
                    semantic_query,
                    scopes,
                    artifact_type="QABUG_REGISTER" if scopes == ("QABUG",) else "ISSUE_REGISTER",
                    field_filters=filters if scopes == ("QABUG",) else None,
                    cancel_check=cancel_check,
                    source_type="ISSUE_SEMANTIC",
                )
                trace.append(f"issue_semantic_search: {len(semantic_items)} candidate(s)")
            except Exception as exc:
                warnings.append(f"Issue semantic retrieval unavailable: {type(exc).__name__}")
                trace.append(f"issue_semantic_search: ERROR {type(exc).__name__}")
            semantic_ms = int((time.perf_counter() - sem_started) * 1000)

        merged = self._fuse_issue_candidates(sql_items, semantic_items, mode=mode)
        # Search-one is intentionally bounded before prompt construction. Keep two
        # candidates for corroboration/fallback; the prompt governor can reduce to
        # one final evidence block when confidence is high.
        if mode == "FIND_ONE":
            merged = merged[:2]

        if statistics:
            intent = "ISSUE_STATISTICS"
        elif mode == "EXACT":
            intent = "ISSUE_EXACT_LOOKUP"
        elif mode == "FIND_ONE":
            intent = "ISSUE_SEARCH_ONE"
        else:
            intent = "ISSUE_SEARCH"

        return RetrievalResult(
            evidence=tuple(merged),
            resolved_scopes=scopes,
            semantic_ms=semantic_ms,
            sql_ms=sql_ms,
            warnings=tuple(warnings),
            plan_source="structured-fast-path",
            plan_summary=(
                "Exact PostgreSQL QA/BUG statistics; vector estimation disabled."
                if statistics
                else (
                    "Explicit issue-key lookup from PostgreSQL; semantic search disabled."
                    if mode == "EXACT"
                    else (
                        "Explicit SQL-only QA/BUG constraints enforced; semantic corroboration disabled to preserve filter semantics."
                        if structured_only_filters
                        else "PostgreSQL authoritative issue fields fused with filtered Qdrant semantic relevance."
                    )
                )
            ),
            intents=(intent,),
            entities=tuple(v for v in (str(filters.get("issue_key") or ""),) if v),
            tools=(
                ("issue_statistics",)
                if statistics
                else (
                    ("issue_search",)
                    if mode == "EXACT" or structured_only_filters or not semantic_query
                    else ("issue_search", "semantic_search")
                )
            ),
            rewritten_queries=tuple(dict.fromkeys(x for x in (semantic_query, question) if x)),
            evidence_strength=(
                ("STRONG" if sql_items else "NONE") if statistics
                else ("STRONG" if sql_items else "NONE") if mode == "EXACT"
                else "STRONG" if any(x.source_type == "ISSUE_HYBRID" for x in merged)
                else "MODERATE" if merged
                else "NONE"
            ),
            trace=tuple(trace),
            route="ISSUE_STATS" if statistics else "ISSUE_QUERY",
            knowledge_source=(
                "POSTGRES_STRUCTURED"
                if statistics or mode == "EXACT" or structured_only_filters
                else "POSTGRES+QDRANT"
            ),
        )

    def retrieve_system_meta(
        self,
        question: str,
        *,
        scope: str = "AUTO",
        current_scope: str = "",
    ) -> RetrievalResult:
        """Return live, read-only runtime/schema/index evidence for this console.

        This route exists so questions such as "what Qdrant indexes are active"
        or "what does one-click QA/BUG initialization create" are never answered
        from LLM memory. Credentials and secret values are deliberately omitted.
        """
        started = time.perf_counter()
        warnings: list[str] = []
        lines: list[str] = [
            "Document Intelligence Console runtime contract",
            f"Embedding model: {self.embedding_model}",
            f"Vector dimension: {self.vector_dimension}",
            f"Candidate TopK: {self.top_k}",
            "PostgreSQL authority: enabled; Qdrant is a derived semantic index.",
            "One-click initialization policy: additive/idempotent; CREATE IF NOT EXISTS / ALTER ADD COLUMN IF NOT EXISTS; no DROP/TRUNCATE of business data.",
        ]

        q = str(question or "")
        wants_qdrant = bool(re.search(r"qdrant|collection|集合|payload|向量|vector|索引|index|rag", q, re.I))
        wants_sql = bool(re.search(r"postgres(?:ql)?|sql|schema|table|view|表|建表|建库|建庫|索引|index|rag", q, re.I))

        # Runtime collection contract is always useful context; live state is added
        # when Qdrant is reachable.
        lines.append("Configured collections:")
        for key, collection in sorted(self.collections.items()):
            lines.append(f"- {key}: {collection}")
        if wants_qdrant:
            try:
                client = self._get_qdrant_client()
                lines.append("Live Qdrant state:")
                for key, collection in sorted(self.collections.items()):
                    if not self._collection_exists(client, collection):
                        lines.append(f"- {key}/{collection}: MISSING")
                        continue
                    info = client.get_collection(collection_name=collection)
                    vectors = getattr(getattr(getattr(info, "config", None), "params", None), "vectors", None)
                    size = getattr(vectors, "size", "?") if not isinstance(vectors, dict) else "named"
                    distance = getattr(vectors, "distance", "?") if not isinstance(vectors, dict) else "named"
                    points = getattr(info, "points_count", None)
                    payload_schema = getattr(info, "payload_schema", None) or {}
                    payload_names = sorted(str(name) for name in dict(payload_schema).keys())
                    lines.append(
                        f"- {key}/{collection}: points={points if points is not None else '?'}; "
                        f"vector={size}/{distance}; payload_indexes={','.join(payload_names) if payload_names else '(none)'}"
                    )
            except Exception as exc:
                warnings.append(f"Qdrant runtime introspection unavailable: {type(exc).__name__}")

        # SQL metadata is read only from pg_catalog/known schema tables. Never
        # execute SQL supplied by the user.
        if wants_sql:
            requested = str(scope or "AUTO").strip().upper().replace("QA/BUG", "QABUG")
            if requested == "QABUG" or "QA/BUG" in q.upper() or "QABUG" in q.upper():
                sql_scopes = ("QABUG",)
            elif requested in {"21MM", "24MM", "COMMON"}:
                sql_scopes = (requested,)
            else:
                sql_scopes = tuple(k for k in ("21MM", "24MM", "COMMON", "QABUG") if k in self.postgres_profiles)
            lines.append("Live PostgreSQL metadata:")
            for sql_scope in sql_scopes:
                try:
                    with self._connect(sql_scope) as conn, conn.cursor() as cur:
                        cur.execute(
                            """
                            SELECT tablename FROM pg_tables
                            WHERE schemaname='public'
                              AND (%s <> 'QABUG' OR tablename LIKE 'qabug_%%' OR tablename IN ('qa_details','bug_details','schema_version'))
                            ORDER BY tablename
                            """,
                            (sql_scope,),
                        )
                        tables = [str(row["tablename"]) for row in cur.fetchall()]
                        cur.execute(
                            """
                            SELECT viewname FROM pg_views
                            WHERE schemaname='public'
                              AND (%s <> 'QABUG' OR viewname LIKE 'qabug_%%')
                            ORDER BY viewname
                            """,
                            (sql_scope,),
                        )
                        views = [str(row["viewname"]) for row in cur.fetchall()]
                        cur.execute(
                            """
                            SELECT indexname FROM pg_indexes
                            WHERE schemaname='public'
                              AND (%s <> 'QABUG' OR tablename LIKE 'qabug_%%' OR tablename IN ('qa_details','bug_details'))
                            ORDER BY indexname
                            """,
                            (sql_scope,),
                        )
                        indexes = [str(row["indexname"]) for row in cur.fetchall()]
                        try:
                            cur.execute("SELECT component,version,name FROM public.schema_version ORDER BY component")
                            versions = [f"{row['component']}=v{row['version']}({row['name']})" for row in cur.fetchall()]
                        except Exception:
                            versions = []
                    lines.append(
                        f"- {sql_scope}: db={self.postgres_profiles[sql_scope].database}; "
                        f"tables={len(tables)} [{', '.join(tables[:40])}]; "
                        f"views={len(views)} [{', '.join(views[:40])}]; "
                        f"indexes={len(indexes)} [{', '.join(indexes[:80])}]; "
                        f"schema_versions={'; '.join(versions) if versions else '-'}"
                    )
                except Exception as exc:
                    warnings.append(f"{sql_scope} PostgreSQL runtime introspection unavailable: {type(exc).__name__}")

        elapsed = int((time.perf_counter() - started) * 1000)
        evidence = EvidenceItem(
            evidence_id="",
            scope="SYSTEM",
            source_type="SYSTEM_META",
            content="\n".join(lines),
            metadata={
                "source_authority": "LIVE_RUNTIME_AND_STATIC_CONTRACT",
                "secrets_included": False,
                "runtime_introspection": True,
            },
        )
        return RetrievalResult(
            evidence=(evidence,),
            resolved_scopes=("SYSTEM",),
            sql_ms=elapsed,
            warnings=tuple(warnings),
            plan_source="system-meta-fast-path",
            plan_summary="Read-only runtime/schema/index introspection; model memory excluded.",
            intents=("SYSTEM_METADATA",),
            tools=("system_metadata",),
            rewritten_queries=(question,),
            evidence_strength="STRONG",
            trace=(f"system_metadata: runtime introspection · {elapsed} ms",),
            route="SYSTEM_META",
            knowledge_source="LIVE_RUNTIME_AND_STATIC_CONTRACT",
        )

    def _specification_catalog(
        self,
        question: str,
        scopes: Iterable[str],
    ) -> tuple[list[EvidenceItem], list[str]]:
        count_only = bool(re.search(r"(?:多少|几本|幾本|count|how many|件数|何件)", question, re.I))
        result: list[EvidenceItem] = []
        warnings: list[str] = []
        for scope in scopes:
            try:
                with self._connect(scope) as conn, conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT sp.id AS specification_id, sp.specification_key, sp.project_code,
                               sp.region_scope, sp.spec_code, sp.spec_type, sp.spec_subtype,
                               sp.canonical_name, sp.status AS specification_status,
                               pv.version_label AS current_version, pv.status AS current_version_status,
                               pv.document_id AS current_document_id, pv.file_name AS current_file_name,
                               COALESCE(vc.version_count, 0) AS version_count,
                               GREATEST(COALESCE(vc.version_count, 0) - 1, 0) AS history_count
                        FROM public.specifications sp
                        LEFT JOIN LATERAL (
                            SELECT sv.*, d.file_name FROM public.specification_versions sv
                            JOIN public.documents d ON d.document_id=sv.document_id
                            WHERE sv.specification_id=sp.id AND sv.branch_name='MAIN'
                              AND {PrimarySpecificationDocumentPolicy.sql_primary_predicate("d")}
                            ORDER BY CASE WHEN sv.id=sp.current_version_id THEN 0 ELSE 1 END,
                                     sv.version_sort_key DESC NULLS LAST, sv.version_seq DESC, sv.id DESC
                            LIMIT 1
                        ) pv ON TRUE
                        LEFT JOIN LATERAL (
                            SELECT COUNT(*)::INTEGER AS version_count
                            FROM public.specification_versions cv
                            JOIN public.documents dv ON dv.document_id=cv.document_id
                            WHERE cv.specification_id=sp.id AND cv.branch_name='MAIN'
                              AND {PrimarySpecificationDocumentPolicy.sql_primary_predicate("dv")}
                        ) vc ON TRUE
                        WHERE sp.project_code = %s
                        ORDER BY sp.spec_code NULLS LAST, sp.canonical_name
                        """,
                        (scope,),
                    )
                    rows = [dict(row) for row in cur.fetchall()]
            except Exception as exc:
                warnings.append(f"{scope} specification catalog unavailable: {type(exc).__name__}")
                continue
            if count_only:
                content = f"{scope} 当前已注册式样书数量：{len(rows)}。"
            else:
                max_rows = 200
                lines = [f"{scope} 当前式样书目录，共 {len(rows)} 本："]
                for row in rows[:max_rows]:
                    code = str(row.get("spec_code") or "-")
                    name = str(row.get("canonical_name") or "-")
                    file_name=str(row.get("current_file_name") or "")
                    markers=tuple(dict.fromkeys(x.upper() for x in re.findall(
                        r"(?<![A-Z0-9])(21MM|24MM)(?![A-Z0-9])",file_name,re.I)))
                    conflict=bool(markers and scope.upper() not in markers)
                    version = "-" if conflict else str(row.get("current_version") or "-")
                    status = ("PROJECT_SCOPE_CONFLICT" if conflict else
                              str(row.get("current_version_status") or "-") if row.get("current_version")
                              else "NO_REGISTERED_SPEC_BODY")
                    lines.append(f"- {code} | {name} | body_version={version} | status={status}")
                if len(rows) > max_rows:
                    lines.append(f"- … 其余 {len(rows) - max_rows} 本未展开；总数仍为 {len(rows)}。")
                content = "\n".join(lines)
            result.append(
                EvidenceItem(
                    evidence_id="",
                    scope=scope,
                    source_type="SPEC_CATALOG",
                    content=content,
                    metadata={"total_count": len(rows), "count_only": count_only, "rows": rows[:200]},
                )
            )
        return result, warnings

    def _specification_lookup(
        self,
        question: str,
        scopes: Iterable[str],
    ) -> tuple[list[EvidenceItem], list[str]]:
        """Resolve a logical specification to its authoritative body document.

        v1.01 never lets a CoverSheet/CheckSheet/TestSheet become the lookup
        answer merely because a legacy database points ``current_version_id`` at
        that artefact.  The read path deterministically prefers PRIMARY content
        and uses supporting files only as metadata/audit records.
        """
        codes = self._entity_spec_codes((), question)
        # Never turn a year, BUG number or SW version into a specification ID.
        # Bluetooth/BT is a QUERY ALIAS, not a hard-coded 550 identity.
        from app.ai.spec_facts import extra_spec_codes, lookup_keywords, requested_version
        codes = tuple(dict.fromkeys((*codes, *extra_spec_codes(question))))[:8]
        keywords = lookup_keywords(question)
        requested = requested_version(question)
        result: list[EvidenceItem] = []
        warnings: list[str] = []
        primary_cd = PrimarySpecificationDocumentPolicy.sql_primary_predicate("cd")

        for scope in scopes:
            clauses: list[str] = []
            params: list[Any] = []
            if codes:
                clauses.append("sp.spec_code = ANY(%s::text[])")
                params.append(list(codes))
            if not codes:
                for keyword in keywords[:3]:
                    clauses.append(
                        "(sp.canonical_name ILIKE %s OR COALESCE(pd.metadata ->> 'specification_name','') ILIKE %s "
                        "OR COALESCE(pd.title,'') ILIKE %s OR pd.file_name ILIKE %s)"
                    )
                    value = f"%{keyword}%"
                    params.extend([value, value, value, value])
            where = " OR ".join(clauses) if clauses else "FALSE"
            order_sql = "CASE WHEN sp.spec_code = ANY(%s::text[]) THEN 0 ELSE 1 END," if codes else ""
            order_params: list[Any] = [list(codes)] if codes else []
            try:
                with self._connect(scope) as conn, conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT
                            sp.id AS specification_id, sp.specification_key, sp.project_code,
                            sp.region_scope, sp.spec_code, sp.spec_type, sp.spec_subtype,
                            COALESCE(
                                NULLIF(pd.metadata ->> 'specification_name', ''),
                                NULLIF(pd.title, ''),
                                sp.canonical_name
                            ) AS canonical_name,
                            sp.status AS specification_status,
                            pv.id AS current_version_id,
                            pv.version_label AS current_version,
                            pv.status AS current_version_status,
                            pv.document_id AS current_document_id,
                            COALESCE(pd.file_name, pv.document_id, '') AS current_file_name,
                            COALESCE(pd.metadata, '{{}}'::jsonb) AS current_document_metadata,
                            COALESCE(hist.version_count, 0) AS version_count,
                            GREATEST(COALESCE(hist.version_count, 0) - 1, 0) AS history_count
                        FROM public.specifications sp
                        LEFT JOIN LATERAL (
                            SELECT sv.*
                            FROM public.specification_versions sv
                            LEFT JOIN public.documents pd0 ON pd0.document_id = sv.document_id
                            WHERE sv.specification_id = sp.id
                              AND sv.branch_name = 'MAIN'
                              AND {PrimarySpecificationDocumentPolicy.sql_primary_predicate('pd0')}
                              AND (%s::text IS NULL OR
                                   lower(regexp_replace(sv.version_label, '^(ver(sion)?|v)[. ]*', '', 'i')) = %s::text)
                            ORDER BY
                                CASE WHEN sv.id = sp.current_version_id THEN 0 ELSE 1 END,
                                sv.version_sort_key DESC NULLS LAST, sv.version_seq DESC, sv.id DESC
                            LIMIT 1
                        ) pv ON TRUE
                        LEFT JOIN public.documents pd ON pd.document_id = pv.document_id
                        LEFT JOIN LATERAL (
                            SELECT COUNT(*)::INTEGER AS version_count
                            FROM public.specification_versions csv
                            LEFT JOIN public.documents cd ON cd.document_id = csv.document_id
                            WHERE csv.specification_id = sp.id
                              AND csv.branch_name = 'MAIN'
                              AND {primary_cd}
                        ) hist ON TRUE
                        WHERE sp.project_code = %s
                          AND pv.id IS NOT NULL
                          AND ({where})
                        ORDER BY {order_sql} sp.spec_code NULLS LAST, canonical_name
                        LIMIT 20
                        """,
                        tuple([requested, requested, scope, *params, *order_params]),
                    )
                    rows = [dict(row) for row in cur.fetchall()]
            except Exception as exc:
                # A broken version query must not erase independently verifiable
                # imported-file evidence; retain a warning and probe documents.
                warnings.append(f"{scope} specification lookup unavailable: {type(exc).__name__}")
                rows = []

            if not rows and keywords:
                # Distinguish an imported SPEC_BODY file without trusted version
                # linkage from a genuinely registered specification version.
                # Never claim a name/code/approval by inference from a filename.
                try:
                    with self._connect(scope) as conn, conn.cursor() as cur:
                        values = [f"%{v}%" for v in (keywords[:1] if codes else keywords[:2])]
                        # 550+Bluetooth should search the specific Bluetooth Audio
                        # title first, not the entire Bluetooth/BUG document corpus.
                        expressions = " OR ".join("d.file_name ILIKE %s" for _ in values)
                        # This read-only fallback resolves the *physical* body document.
                        # A document with an approved MAIN version may still belong to
                        # an unnumbered/other specification identity.  Never promote it
                        # to the user's requested numeric spec without catalog approval.
                        cur.execute(
                            f"""SELECT d.document_id, d.file_name, d.version, d.metadata,
                                       sv.id AS linked_version_id, sv.version_label AS linked_version,
                                       sv.branch_name AS linked_branch,
                                       sp.id AS linked_specification_id,
                                       sp.spec_code AS linked_spec_code,
                                       sp.region_scope AS linked_region
                                FROM public.documents d
                                LEFT JOIN public.specification_versions sv
                                  ON sv.document_id=d.document_id AND sv.branch_name='MAIN'
                                LEFT JOIN public.specifications sp
                                  ON sp.id=sv.specification_id AND sp.project_code=%s
                                WHERE d.project_code=%s
                                  AND {PrimarySpecificationDocumentPolicy.sql_primary_predicate('d')}
                                  AND ({expressions})
                                ORDER BY d.updated_at DESC, sv.id DESC NULLS LAST LIMIT 60""",
                            (scope, scope, *values),
                        )
                        candidates = [dict(row) for row in cur.fetchall()]
                    seen = set()
                    for d in candidates:
                        inferred = requested_version(str(d.get('file_name') or ''))
                        declared = str(d.get('version') or '').removeprefix('v').removeprefix('V').strip()
                        if requested and requested not in {inferred, declared.casefold()}:
                            continue
                        doc_id = str(d['document_id'])
                        if doc_id in seen:
                            continue
                        seen.add(doc_id)
                        linked = bool(d.get('linked_version_id') and d.get('linked_specification_id'))
                        candidate_kind = 'SPEC_IDENTITY_CANDIDATE' if linked else 'SPEC_UNBOUND_FILE'
                        result.append(EvidenceItem(
                            evidence_id='', scope=scope, source_type=candidate_kind,
                            content=('Registered MAIN SPEC_BODY with UNVERIFIED numeric identity'
                                     if linked else 'Imported SPEC_BODY without specification-version linkage'),
                            file_name=str(d['file_name']), document_id=doc_id,
                            specification_version=(str(d.get('linked_version') or '') if linked else declared),
                            metadata={'file_version': declared, 'document_role': 'PRIMARY',
                                      'linkage_verified': False,
                                      'linked_version_id': d.get('linked_version_id'),
                                      'linked_specification_id': d.get('linked_specification_id'),
                                      'linked_spec_code': d.get('linked_spec_code'),
                                      'linked_region': d.get('linked_region'),
                                      'linked_branch': d.get('linked_branch'),
                                      'requested_spec_codes': tuple(codes)},
                        ))
                except Exception as exc:
                    warnings.append(f"{scope} imported body lookup unavailable: {type(exc).__name__}")
            for row in rows:
                code = str(row.get("spec_code") or "")
                name = str(row.get("canonical_name") or "")
                version = str(row.get("current_version") or "")
                file_name = str(row.get("current_file_name") or "")
                document_metadata = dict(row.get("current_document_metadata") or {})
                explicit_projects = tuple(dict.fromkeys(
                    value.upper() for value in re.findall(
                        r"(?<![A-Z0-9])(21MM|24MM)(?![A-Z0-9])", file_name, re.I
                    )
                ))
                project_conflict = bool(explicit_projects and scope.upper() not in explicit_projects)
                is_primary = PrimarySpecificationDocumentPolicy.is_primary(
                    file_name=file_name, metadata=document_metadata
                )
                evidence_role = PrimarySpecificationDocumentPolicy.evidence_role(
                    file_name=file_name, metadata=document_metadata
                )
                row["document_role"] = (
                    PrimarySpecificationDocumentPolicy.PRIMARY_ROLE if is_primary else "SUPPORTING"
                )
                row["evidence_role"] = evidence_role
                row["document_priority"] = PrimarySpecificationDocumentPolicy.role_rank(
                    file_name=file_name, metadata=document_metadata
                )
                row["filename_project_markers"] = explicit_projects
                row["identity_verified"] = not project_conflict
                row["project_identity_conflict"] = project_conflict
                content = (
                    f"Specification {code or '-'}: {name or '-'}. "
                    f"Authoritative document={file_name or row.get('current_document_id') or '-'}; "
                    f"document_role={evidence_role}. "
                    f"Current version={version or '-'}, status={row.get('current_version_status') or row.get('specification_status') or '-'}, "
                    f"version_count={row.get('version_count') or 0}, history_count={row.get('history_count') or 0}."
                )
                result.append(
                    EvidenceItem(
                        evidence_id="", scope=scope,
                        source_type=("SPEC_SCOPE_CONFLICT" if project_conflict else "SPEC_LOOKUP"),
                        content=content, score=(0.0 if project_conflict else 1.0 if is_primary else 0.2),
                        document_id=str(row.get("current_document_id") or ""),
                        file_name=file_name, specification_code=code,
                        specification_name=name, specification_version=version,
                        metadata=row,
                    )
                )
        return result, warnings

    @staticmethod
    def _parse_issue_filters(question: str) -> dict[str, str]:
        """Parse only bounded, reviewable QA/BUG filters from user text.

        Free-form terms remain semantic/full-text queries; only explicit
        field:value forms and high-confidence workflow phrases become SQL
        predicates. This prevents accidental model/user prose from widening or
        mutating query semantics.
        """
        q = str(question or "")
        result: dict[str, str] = {}
        key = re.search(r"(?<![A-Z0-9])(?!SPEC-)([A-Z][A-Z0-9]{1,20}-\d{1,10})(?![A-Z0-9])", q, re.I)
        if key:
            result["issue_key"] = key.group(1).upper()

        if re.search(r"(?<![A-Za-z0-9])bug(?![A-Za-z0-9])|defect|缺陷|不具合|バグ|故障", q, re.I):
            result["record_type"] = "BUG"
        elif re.search(r"(?<![A-Za-z0-9])qa(?![A-Za-z0-9])|q&a|仕様問題|规格问答|規格問答|测试问答|測試問答", q, re.I):
            result["record_type"] = "QA"

        status_patterns = (
            ("OPEN", r"\bopen\b|未处理|未處理|未対応|オープン"),
            ("IN_PROGRESS", r"in[ -]?progress|处理中|處理中|进行中|進行中|対応中|解析中|検討中"),
            ("RESOLVED", r"\bresolved\b|已解决|已解決|解決済|対応済|修正済"),
            ("CLOSED", r"\bclosed\b|已关闭|已關閉|クローズ|完了"),
            ("REOPENED", r"reopened|重新打开|重新開啟|再オープン"),
            ("CANCELLED", r"cancelled|canceled|中止|対策中止|不実行|won.?t fix"),
        )
        for canonical, pattern in status_patterns:
            if re.search(pattern, q, re.I):
                result["status_norm"] = canonical
                break

        priority = re.search(r"(?<![A-Z0-9])(P[0-4])(?![A-Z0-9])", q, re.I)
        if priority:
            result["priority_norm"] = priority.group(1).upper()
        else:
            for canonical, pattern in (
                ("CRITICAL", r"critical|blocker|致命|紧急|緊急|最高"),
                ("HIGH", r"\bhigh\b|高优先|高優先|高優先度|高优先级"),
                ("MEDIUM", r"\bmedium\b|中优先|中優先|中優先度|普通"),
                ("LOW", r"\blow\b|低优先|低優先|低優先度"),
            ):
                if re.search(pattern, q, re.I):
                    result["priority_norm"] = canonical
                    break

        # Explicit workflow fields also accept the source-language values used
        # by Jira exports (for example 优先度:高 / priority:Highest). Normalize
        # them to the SQL canonical enums instead of relying on free-text tokens.
        explicit_status = re.search(r"(?:status|ステータス|状态|狀態)\s*[:=：]\s*([^,，;；\n]+)", q, re.I)
        if explicit_status:
            from app.qabug.canonical import normalize_status
            result["status_norm"] = normalize_status(explicit_status.group(1).strip())
        explicit_priority = re.search(r"(?:priority|優先度|优先度|優先級|优先级)\s*[:=：]\s*([^,，;；\n]+)", q, re.I)
        if explicit_priority:
            from app.qabug.canonical import normalize_priority
            result["priority_norm"] = normalize_priority(explicit_priority.group(1).strip())

        # Explicit filters. Values stop at punctuation, making the parser safe
        # for fixed read-only SQL templates.
        field_patterns = {
            "component": r"(?:component|module|组件|組件|モジュール|コンポーネント)\s*[:=：]\s*([^,，;；\n]+)",
            "assignee": r"(?:assignee|owner|担当者|负责人|負責人)\s*[:=：]\s*([^,，;；\n]+)",
            "reporter": r"(?:reporter|報告者|报告人|提出人)\s*[:=：]\s*([^,，;；\n]+)",
            "resolution": r"(?:resolution|解決状況|解决状态|解決狀態)\s*[:=：]\s*([^,，;；\n]+)",
            "release_version": r"(?:release(?:\s*plan)?\s*version|fix\s*version|リリース計画バージョン|修复版本|修復版本|目标版本)\s*[:=：]\s*([^,，;；\s]+)",
            "affected_version": r"(?:affected\s*version|affects\s*version|不具合バージョン|发生版本|發生版本)\s*[:=：]\s*([^,，;；\s]+)",
            "software_version": r"(?:software\s*version|ソフトバージョン|软件版本|軟體版本)\s*[:=：]\s*([^,，;；\s]+)",
            "project_key": r"(?:project\s*key|プロジェクト\s*キー)\s*[:=：]\s*([^,，;；\s]+)",
            "model": r"(?:model|機種|机型|機型)\s*[:=：]\s*([^,，;；\n]+)",
            "destination": r"(?:destination|market|region|仕向け|市场|市場)\s*[:=：]\s*([^,，;；\n]+)",
            "section_type": r"(?:section|section_type|证据段|證據段)\s*[:=：]\s*([^,，;；\s]+)",
            "issue_type": r"(?:issue\s*type|課題タイプ|问题类型|問題類型)\s*[:=：]\s*([^,，;；\n]+)",
            "creator": r"(?:creator|作成者|创建者|建立者)\s*[:=：]\s*([^,，;；\n]+)",
            "grade": r"(?:grade|グレード|等级|等級)\s*[:=：]\s*([^,，;；\n]+)",
            "label": r"(?:label|labels|ラベル|标签|標籤)\s*[:=：]\s*([^,，;；\n]+)",
            "answer_type": r"(?:answer\s*type|回答区分|回答類型|回答类型)\s*[:=：]\s*([^,，;；\n]+)",
            "final_answerer": r"(?:final\s*answerer|最終回答者|最终回答者)\s*[:=：]\s*([^,，;；\n]+)",
            "responsible_group": r"(?:responsible\s*group|担当Gr\.?|担当组|擔當組)\s*[:=：]\s*([^,，;；\n]+)",
            "reference_spec": r"(?:reference\s*spec|参照仕様書|参考式样书|參考式樣書)\s*[:=：]\s*([^,，;；\n]+)",
            "cause_module": r"(?:cause\s*module|原因モジュール|原因模块|原因模組)\s*[:=：]\s*([^,，;；\n]+)",
            "analysis_owner": r"(?:analysis\s*owner|解析者|分析者)\s*[:=：]\s*([^,，;；\n]+)",
            "countermeasure_owner": r"(?:countermeasure\s*owner|対策者|对策者|對策者)\s*[:=：]\s*([^,，;；\n]+)",
            "effect_result": r"(?:effect\s*(?:confirmation\s*)?result|効果確認結果|效果确认结果|效果確認結果)\s*[:=：]\s*([^,，;；\n]+)",
            "problem_type": r"(?:problem\s*type|問題種類|问题种类|問題種類)\s*[:=：]\s*([^,，;；\n]+)",
            "function_class": r"(?:function\s*(?:class|classification)|機能分類|功能分类|功能分類)\s*[:=：]\s*([^,，;；\n]+)",
            "linked_issue": r"(?:linked\s*issue|related\s*issue|关联问题|關聯問題|関連課題)\s*[:=：]\s*([A-Z][A-Z0-9]{1,20}-\d{1,10})",
            "source_file": r"(?:source\s*file|源文件|來源檔案|元ファイル)\s*[:=：]\s*([^,，;；\n]+)",
            "quality_code": r"(?:quality\s*code|dq\s*code|质量代码|品質コード)\s*[:=：]\s*([^,，;；\n]+)",
            "root_cause": r"(?:root\s*cause|根本原因|原因|混入原因)\s*[:=：]\s*([^,，;；\n]+)",
            "analysis_result": r"(?:analysis\s*result|解析結果|分析结果|分析結果)\s*[:=：]\s*([^,，;；\n]+)",
            "countermeasure": r"(?:countermeasure|対策内容|对策内容|對策內容|修正内容)\s*[:=：]\s*([^,，;；\n]+)",
            "effect_version": r"(?:effect\s*(?:confirmation\s*)?version|効果確認バージョン|效果确认版本|效果確認版本)\s*[:=：]\s*([^,，;；\s]+)",
            "environment": r"(?:environment|発生環境|环境|環境)\s*[:=：]\s*([^,，;；\n]+)",
            "final_answer": r"(?:final\s*answer|最終回答内容|最终回答内容)\s*[:=：]\s*([^,，;；\n]+)",
            "rationale": r"(?:rationale|根拠|依据|依據)\s*[:=：]\s*([^,，;；\n]+)",
            "relation_type": r"(?:relation\s*type|link\s*type|关联类型|關聯類型|リンク種別)\s*[:=：]\s*([^,，;；\n]+)",
        }
        for field, pattern in field_patterns.items():
            match = re.search(pattern, q, re.I)
            if match:
                result[field] = match.group(1).strip()

        # Lossless Jira/custom-field query escape hatch. Both key and value are
        # bound as SQL parameters later; neither is interpolated as an identifier.
        # Examples: custom[032D_RCブランチ_効確結果(STGr)]=OK
        #           field[カスタム フィールド (機能分類)]=Audio
        custom_match = re.search(
            r"(?:custom|field|custom_field|自定义字段|自訂欄位|カスタムフィールド)\s*\[([^\]\n]{1,240})\]\s*([:=：~])\s*([^,，;；\n]+)",
            q,
            re.I,
        )
        if custom_match:
            result["custom_field_key"] = custom_match.group(1).strip()
            result["custom_field_value"] = custom_match.group(3).strip()
            result["custom_field_match"] = "EXACT" if custom_match.group(2) == "=" else "CONTAINS"

        if re.search(r"created(?:\s*date)?|作成日|创建日|建立日期", q, re.I):
            result["date_basis"] = "created"
        elif re.search(r"resolved(?:\s*date)?|解決日|解决日|解決日期", q, re.I):
            result["date_basis"] = "resolved"
        elif re.search(r"effect(?:\s*date)?|効果確認実施日|效果确认日期|效果確認日期", q, re.I):
            result["date_basis"] = "effect"
        elif re.search(r"occurrence(?:\s*date)?|発生日|发生日|發生日", q, re.I):
            result["date_basis"] = "occurrence"
        elif re.search(r"answer\s*requested|回答希望日|期望回答日", q, re.I):
            result["date_basis"] = "answer_requested"
        elif re.search(r"due(?:\s*date)?|期限|截止日", q, re.I):
            result["date_basis"] = "due"
        else:
            result["date_basis"] = "updated"

        dates = re.findall(r"\b(20\d{2}[-/]\d{1,2}[-/]\d{1,2})\b", q)
        if len(dates) >= 2 and re.search(r"(?:到|至|~|～|through|between|から.*まで)", q, re.I):
            result["date_from"] = dates[0].replace("/", "-")
            result["date_to"] = dates[1].replace("/", "-")
        elif len(dates) == 1:
            if re.search(r"(?:之后|之後|以后|以後|after|since|以降)", q, re.I):
                result["date_from"] = dates[0].replace("/", "-")
            elif re.search(r"(?:之前|以前|before|まで)", q, re.I):
                result["date_to"] = dates[0].replace("/", "-")
        return result

    @staticmethod
    def _issue_where(filters: dict[str, str], *, include_text_tokens: list[str] | None = None) -> tuple[str, list[Any]]:
        clauses = ["UPPER(COALESCE(c.metadata ->> 'artifact_type','')) = 'ISSUE_REGISTER'"]
        params: list[Any] = []
        field_map = {
            "issue_key": "c.metadata ->> 'issue_key'",
            "status": "c.metadata ->> 'status'",
            "priority": "c.metadata ->> 'priority'",
            "issue_type": "c.metadata ->> 'issue_type'",
            "component": "c.metadata ->> 'component'",
            "assignee": "c.metadata ->> 'assignee'",
            "fix_version": "c.metadata ->> 'fix_version'",
        }
        for key, expression in field_map.items():
            value = str(filters.get(key) or "").strip()
            if not value:
                continue
            if key == "issue_key":
                clauses.append(f"LOWER(COALESCE({expression},'')) = LOWER(%s)")
                params.append(value)
            else:
                clauses.append(f"COALESCE({expression},'') ILIKE %s")
                params.append(f"%{value}%")
        tokens = [t for t in (include_text_tokens or []) if t]
        if tokens:
            token_clauses: list[str] = []
            for token in tokens[:5]:
                token_clauses.append("c.content ILIKE %s")
                params.append(f"%{token}%")
            clauses.append("(" + " OR ".join(token_clauses) + ")")
        return " AND ".join(clauses), params

    @staticmethod
    def _qabug_where(filters: dict[str, str], *, text_tokens: list[str] | None = None) -> tuple[str, list[Any]]:
        clauses = ["r.is_current", "r.is_queryable"]
        params: list[Any] = []
        exact = {
            "issue_key": "r.issue_key",
            "record_type": "r.record_type",
            "status_norm": "r.status_norm",
            "priority_norm": "r.priority_norm",
            "project_key": "r.project_key",
        }
        fuzzy = {
            "component": "r.component", "assignee": "r.assignee", "reporter": "r.reporter",
            "creator": "r.creator", "resolution": "r.resolution_raw", "release_version": "r.release_plan_version",
            "affected_version": "r.affected_version", "software_version": "r.software_version",
            "model": "r.model", "destination": "r.destination", "issue_type": "r.issue_type", "grade": "r.grade",
            "environment": "r.environment",
        }
        for key, expr in exact.items():
            value = str(filters.get(key) or "").strip()
            if value:
                clauses.append(f"UPPER(COALESCE({expr},'')) = UPPER(%s)")
                params.append(value)
        for key, expr in fuzzy.items():
            value = str(filters.get(key) or "").strip()
            if value:
                clauses.append(f"COALESCE({expr},'') ILIKE %s")
                params.append(f"%{value}%")

        label = str(filters.get("label") or "").strip()
        if label:
            clauses.append("EXISTS (SELECT 1 FROM unnest(r.labels) AS lbl WHERE lbl ILIKE %s)")
            params.append(f"%{label}%")

        # Detail-table filters remain bounded EXISTS predicates; arbitrary SQL is never accepted.
        detail_filters = {
            "answer_type": ("public.qa_details", "answer_type"),
            "final_answerer": ("public.qa_details", "final_answerer"),
            "responsible_group": ("public.qa_details", "responsible_group"),
            "reference_spec": ("public.qa_details", "reference_spec"),
            "final_answer": ("public.qa_details", "final_answer"),
            "rationale": ("public.qa_details", "rationale"),
            "cause_module": ("public.bug_details", "cause_module"),
            "root_cause": ("public.bug_details", "root_cause"),
            "analysis_result": ("public.bug_details", "analysis_result"),
            "countermeasure": ("public.bug_details", "countermeasure"),
            "effect_version": ("public.bug_details", "effect_version"),
            "analysis_owner": ("public.bug_details", "analysis_owner"),
            "countermeasure_owner": ("public.bug_details", "countermeasure_owner"),
            "effect_result": ("public.bug_details", "effect_result"),
            "problem_type": ("public.bug_details", "problem_type"),
            "function_class": ("public.bug_details", "function_class"),
        }
        for key, (table, column) in detail_filters.items():
            value = str(filters.get(key) or "").strip()
            if value:
                clauses.append(f"EXISTS (SELECT 1 FROM {table} df WHERE df.record_id=r.id AND COALESCE(df.{column},'') ILIKE %s)")
                params.append(f"%{value}%")

        linked = str(filters.get("linked_issue") or "").strip()
        relation_type = str(filters.get("relation_type") or "").strip()
        if linked or relation_type:
            relation_parts = ["qr.source_record_id=r.id"]
            if linked:
                relation_parts.append("UPPER(qr.target_issue_key)=UPPER(%s)")
                params.append(linked)
            if relation_type:
                relation_parts.append("qr.relation_type ILIKE %s")
                params.append(f"%{relation_type}%")
            clauses.append("EXISTS (SELECT 1 FROM public.qabug_relations qr WHERE " + " AND ".join(relation_parts) + ")")

        custom_key = str(filters.get("custom_field_key") or "").strip()
        custom_value = str(filters.get("custom_field_value") or "").strip()
        if custom_key and custom_value:
            if str(filters.get("custom_field_match") or "CONTAINS").upper() == "EXACT":
                # Uses the GIN(jsonb_path_ops) index on custom_fields.
                clauses.append("r.custom_fields @> %s::jsonb")
                params.append(__import__("json").dumps({custom_key: custom_value}, ensure_ascii=False))
            else:
                # Parameterized dynamic key + substring match. Slower than exact
                # GIN containment but safe and useful for long human-entered values.
                clauses.append("COALESCE(r.custom_fields ->> %s,'') ILIKE %s")
                params.extend([custom_key, f"%{custom_value}%"])

        source_file = str(filters.get("source_file") or "").strip()
        if source_file:
            clauses.append("EXISTS (SELECT 1 FROM public.qabug_source_files qsf WHERE qsf.id=r.source_file_id AND qsf.source_file_name ILIKE %s)")
            params.append(f"%{source_file}%")
        quality_code = str(filters.get("quality_code") or "").strip()
        if quality_code:
            clauses.append("EXISTS (SELECT 1 FROM public.qabug_quality_findings qf WHERE qf.record_id=r.id AND qf.resolved_at IS NULL AND UPPER(qf.code)=UPPER(%s))")
            params.append(quality_code)

        basis = str(filters.get("date_basis") or "updated")
        date_expr = {
            "created": "r.created_at_source",
            "updated": "COALESCE(r.updated_at_source, r.created_at_source)",
            "resolved": "r.resolved_at_source",
            "due": "r.due_at_source",
        }.get(basis, "COALESCE(r.updated_at_source, r.created_at_source)")
        if filters.get("date_from"):
            if basis == "effect":
                clauses.append("EXISTS (SELECT 1 FROM public.bug_details bd WHERE bd.record_id=r.id AND bd.effect_date >= %s::date)")
            elif basis == "occurrence":
                clauses.append("EXISTS (SELECT 1 FROM public.bug_details bd WHERE bd.record_id=r.id AND bd.occurrence_date >= %s::date)")
            elif basis == "answer_requested":
                clauses.append("EXISTS (SELECT 1 FROM public.qa_details qd WHERE qd.record_id=r.id AND qd.answer_requested_at >= %s::date)")
            else:
                clauses.append(f"{date_expr} >= %s::date")
            params.append(filters["date_from"])
        if filters.get("date_to"):
            if basis == "effect":
                clauses.append("EXISTS (SELECT 1 FROM public.bug_details bd WHERE bd.record_id=r.id AND bd.effect_date < (%s::date + INTERVAL '1 day'))")
            elif basis == "occurrence":
                clauses.append("EXISTS (SELECT 1 FROM public.bug_details bd WHERE bd.record_id=r.id AND bd.occurrence_date < (%s::date + INTERVAL '1 day'))")
            elif basis == "answer_requested":
                clauses.append("EXISTS (SELECT 1 FROM public.qa_details qd WHERE qd.record_id=r.id AND qd.answer_requested_at < (%s::date + INTERVAL '1 day'))")
            else:
                clauses.append(f"{date_expr} < (%s::date + INTERVAL '1 day')")
            params.append(filters["date_to"])

        tokens = [str(t).strip() for t in (text_tokens or []) if str(t).strip()]
        if tokens:
            token_clauses=[]
            for token in tokens[:5]:
                token_clauses.append("(COALESCE(r.summary,'') ILIKE %s OR COALESCE(r.description,'') ILIKE %s OR COALESCE(r.normalized_fields::text,'') ILIKE %s)")
                value=f"%{token}%"
                params.extend([value,value,value])
            clauses.append("("+" OR ".join(token_clauses)+")")
        return " AND ".join(clauses), params

    def _qabug_issue_sql_search(self, question: str, filters: dict[str, str]) -> list[EvidenceItem]:
        generic_stop = {"jira","bug","qa","issue","ticket","缺陷","问题","問題","多少","哪些","现在","目前","什么","什么样"}
        tokens=[t for t in self._tokens(question) if t.lower() not in generic_stop and t not in generic_stop]
        use_tokens=[] if any(k in filters for k in ("issue_key","component","assignee","reporter","creator","release_version","affected_version","software_version","project_key","model","destination","issue_type","grade","label","answer_type","final_answerer","responsible_group","reference_spec","final_answer","rationale","cause_module","root_cause","analysis_result","countermeasure","effect_version","analysis_owner","countermeasure_owner","effect_result","problem_type","function_class","linked_issue","relation_type","source_file","quality_code","environment","custom_field_key")) else tokens
        where, params = self._qabug_where(filters, text_tokens=use_tokens)
        section_filter = str(filters.get("section_type") or "").strip()
        if section_filter:
            where += " AND EXISTS (SELECT 1 FROM public.qabug_chunks fc WHERE fc.record_id=r.id AND fc.is_current AND fc.section_type ILIKE %s)"
            params.append(f"%{section_filter}%")
        try:
            with self._connect("QABUG") as conn, conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT r.id AS record_id, r.record_key, r.record_type, r.issue_key, r.issue_id,
                           r.issue_type, r.project_key, r.project_name, r.summary, r.description,
                           r.status_raw, r.status_norm, r.priority_raw, r.priority_norm,
                           r.resolution_raw, r.resolution_norm, r.assignee, r.reporter, r.creator,
                           r.component, r.release_plan_version, r.affected_version, r.software_version,
                           r.grade, r.destination, r.model, r.environment, r.labels,
                           r.created_at_source, r.updated_at_source, r.resolved_at_source,
                           r.source_sheet, r.source_row_number, r.quality_flags,
                           q.precondition, q.procedure_expected, q.actual_behavior, q.answer, q.answer_type,
                           q.final_answer, q.final_answerer, q.rationale,
                           COALESCE(q.reference_spec,r.normalized_fields->>'reference_spec') AS reference_spec,
                           q.sheet_name AS qa_sheet_name, q.test_spec_name, q.item_cell,
                           b.phenomenon, b.repro_steps, b.expected_result, b.actual_result,
                           b.analysis_result, b.analysis_comment, b.analysis_owner, b.root_cause,
                           b.cause_module, b.cause_company, b.countermeasure, b.countermeasure_owner,
                           b.countermeasure_scope, b.effect_result, b.effect_version, b.effect_date,
                           b.recurrence_prevention_escape, b.recurrence_prevention_injection,
                           b.impact_analysis, b.impact, b.occurrence_probability, b.occurrence_frequency,
                           b.occurrence_date, b.occurrence_environment_detail, b.recovery_procedure,
                           sf.source_file_name,
                           (SELECT string_agg(qr.relation_type || ':' || qr.target_issue_key, ', ' ORDER BY qr.relation_type, qr.target_issue_key)
                            FROM public.qabug_relations qr WHERE qr.source_record_id=r.id) AS linked_issues,
                           (SELECT string_agg(COALESCE(qc.commented_at::text,'') || ';' || COALESCE(qc.author,'') || ';' || qc.body, E'\n---\n' ORDER BY qc.sequence_no)
                            FROM (SELECT * FROM public.qabug_comments qcx WHERE qcx.record_id=r.id ORDER BY qcx.sequence_no DESC LIMIT 5) qc) AS latest_comments,
                           (SELECT string_agg(COALESCE(qa.file_name,'') || CASE WHEN COALESCE(qa.uri,'')<>'' THEN ' · ' || qa.uri ELSE '' END, E'\n' ORDER BY qa.id)
                            FROM public.qabug_attachments qa WHERE qa.record_id=r.id) AS attachments
                    FROM public.qabug_records r
                    LEFT JOIN public.qa_details q ON q.record_id=r.id
                    LEFT JOIN public.bug_details b ON b.record_id=r.id
                    LEFT JOIN public.qabug_source_files sf ON sf.id=r.source_file_id
                    WHERE {where}
                    ORDER BY COALESCE(r.updated_at_source,r.created_at_source) DESC NULLS LAST, r.issue_key
                    LIMIT %s
                    """,
                    tuple([*params, max(self.top_k*3,30)]),
                )
                rows=[dict(row) for row in cur.fetchall()]
        except Exception:
            LOGGER.warning("Dedicated QA/BUG SQL source unavailable", exc_info=True)
            raise RuntimeError("Authoritative QA/BUG SQL retrieval unavailable") from None
        result=[]
        exact_key = bool(str(filters.get("issue_key") or "").strip())
        search_terms = [t.lower() for t in self._tokens(question)]
        for row in rows:
            # Exact issue-key lookups may expose the richer record. Fuzzy/search
            # results deliberately use a compact source-backed projection so one
            # ticket cannot consume the entire local-model context window.
            if exact_key:
                field_order=(
                    ("Record Type","record_type"),("Issue Key","issue_key"),("Summary","summary"),
                    ("Status","status_raw"),("Priority","priority_raw"),("Resolution","resolution_raw"),
                    ("Component","component"),("Assignee","assignee"),("Reporter","reporter"),
                    ("Release Version","release_plan_version"),("Affected Version","affected_version"),
                    ("Software Version","software_version"),("Environment","environment"),
                    ("Description","description"),("QA Preconditions","precondition"),
                    ("QA Steps / Expected","procedure_expected"),("QA Actual","actual_behavior"),
                    ("QA Answer","answer"),("QA Final Answer","final_answer"),("QA Rationale","rationale"),
                    ("Reference Spec","reference_spec"),("Phenomenon","phenomenon"),
                    ("Reproduction Steps","repro_steps"),("Expected Result","expected_result"),
                    ("Actual Result","actual_result"),("Analysis Result","analysis_result"),
                    ("Root Cause","root_cause"),("Countermeasure","countermeasure"),
                    ("Effect Confirmation","effect_result"),("Effect Version","effect_version"),
                    ("Impact Analysis","impact_analysis"),("Recovery Procedure","recovery_procedure"),
                    ("Linked Issues","linked_issues"),("Latest Comments","latest_comments"),("Attachments","attachments"),
                )
                max_chars=7000
            else:
                field_order=(
                    ("Record Type","record_type"),("Issue Key","issue_key"),("Summary","summary"),
                    ("Status","status_raw"),("Priority","priority_raw"),("Component","component"),
                    ("Assignee","assignee"),("Release Version","release_plan_version"),
                    ("Phenomenon","phenomenon"),("Description","description"),
                )
                max_chars=2200
            parts=[]
            for label,key in field_order:
                value=str(row.get(key) or "").strip()
                if value:
                    # Search result bodies are snippets, not raw Jira exports.
                    # Metadata/raw rows remain available in PostgreSQL for exact
                    # follow-up queries.
                    if not exact_key and len(value) > 700:
                        value=value[:700].rstrip()+"…"
                    parts.append(f"{label}: {value}")
            content="\n".join(parts)[:max_chars]
            md=dict(row)
            md.update({
                "artifact_type":"QABUG_REGISTER",
                "record_type":str(row.get("record_type") or ""),
                "issue_key":str(row.get("issue_key") or ""),
                "knowledge_origin":"POSTGRES_QABUG_STRUCTURED",
                "source_authority":"POSTGRESQL",
                "query_match":"EXACT_KEY" if exact_key else "LEXICAL_SQL",
            })
            searchable=" ".join(str(row.get(k) or "") for k in ("summary","description","phenomenon","component")).lower()
            lexical_hits=sum(1 for term in search_terms if term and term in searchable)
            lexical_score=None if exact_key else min(0.80, 0.48 + 0.08*lexical_hits)
            result.append(EvidenceItem(
                evidence_id="", scope="QABUG",
                source_type="ISSUE_EXACT" if exact_key else "ISSUE_SQL_MATCH",
                content=content, score=lexical_score,
                document_id=str(row.get("record_key") or ""), file_name=str(row.get("source_file_name") or ""),
                metadata=md,
            ))
        return result

    def _qabug_issue_statistics(self, question: str, filters: dict[str, str]) -> list[EvidenceItem]:
        where, params=self._qabug_where(filters)
        try:
            with self._connect("QABUG") as conn, conn.cursor() as cur:
                cur.execute(f"SELECT COUNT(*)::bigint AS total FROM public.qabug_records r WHERE {where}",tuple(params))
                total=int((cur.fetchone() or {}).get("total") or 0)
                breakdowns={}
                for label,expr in (
                    ("record_type","r.record_type"),("status","r.status_norm"),("priority","r.priority_norm"),
                    ("resolution","r.resolution_norm"),("issue_type","r.issue_type"),("component","r.component"),
                    ("assignee","r.assignee"),("reporter","r.reporter"),("destination","r.destination"),
                    ("model","r.model"),("release_version","r.release_plan_version"),
                ):
                    cur.execute(
                        f"SELECT COALESCE(NULLIF(BTRIM({expr}),''),'UNSPECIFIED') AS key, COUNT(*)::bigint AS count FROM public.qabug_records r WHERE {where} GROUP BY 1 ORDER BY 2 DESC,1 LIMIT 100",
                        tuple(params),
                    )
                    breakdowns[label]=[(str(x["key"]),int(x["count"])) for x in cur.fetchall()]
                if re.search(r"trend|趋势|趨勢|推移|月別|按月|月份", question, re.I):
                    cur.execute(
                        f"SELECT to_char(date_trunc('month', COALESCE(r.updated_at_source,r.created_at_source)), 'YYYY-MM') AS key, COUNT(*)::bigint AS count FROM public.qabug_records r WHERE {where} AND COALESCE(r.updated_at_source,r.created_at_source) IS NOT NULL GROUP BY 1 ORDER BY 1 DESC LIMIT 36",
                        tuple(params),
                    )
                    breakdowns["monthly_trend"]=[(str(x["key"]),int(x["count"])) for x in cur.fetchall()]
        except Exception:
            LOGGER.warning("Dedicated QA/BUG SQL statistics unavailable", exc_info=True)
            raise RuntimeError("Authoritative QA/BUG statistics unavailable") from None
        filter_text=", ".join(f"{k}={v}" for k,v in filters.items()) or "none"
        lines=[f"QABUG exact statistics (filters: {filter_text})",f"Total logical records: {total}"]
        for label,values in breakdowns.items():
            if values:
                lines.append(label+": "+", ".join(f"{k}={v}" for k,v in values[:20]))
        return [EvidenceItem(
            evidence_id="",scope="QABUG",source_type="ISSUE_STATS",content="\n".join(lines),
            metadata={"total":total,"count_semantics":"AUTHORITATIVE_QABUG_RECORDS","filters":dict(filters),"breakdowns":breakdowns,"source_authority":"POSTGRESQL"},
        )]

    def _issue_sql_search(
        self,
        question: str,
        scopes: Iterable[str],
        filters: dict[str, str],
    ) -> list[EvidenceItem]:
        scope_tuple = tuple(scopes)
        if "QABUG" in scope_tuple:
            return self._qabug_issue_sql_search(question, filters)
        """Return one governed evidence item per logical QA/BUG issue row.

        Long issue rows may be split into multiple RAG chunks. Grouping by the
        stable issue key (or document+row fallback) prevents duplicate tickets
        in lists and exact-key answers while preserving the full row evidence.
        """
        result: list[EvidenceItem] = []
        successful_scopes = 0
        failed_scopes = 0
        generic_stop = {"jira", "bug", "qa", "issue", "ticket", "缺陷", "问题", "問題", "多少", "哪些", "现在", "目前"}
        tokens = [t for t in self._tokens(question) if t.lower() not in generic_stop and t not in generic_stop]
        use_tokens = [] if filters else tokens
        for scope in scopes:
            where, params = self._issue_where(filters, include_text_tokens=use_tokens)
            try:
                with self._connect(scope) as conn, conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT c.id AS content_id, d.document_id, d.file_name, d.project_code,
                               c.section_id, c.page_number, c.chunk_index, c.content, c.content_hash,
                               c.metadata ->> 'row_number' AS row_number,
                               c.metadata ->> 'issue_key' AS issue_key,
                               c.metadata ->> 'issue_type' AS issue_type,
                               c.metadata ->> 'status' AS issue_status,
                               c.metadata ->> 'priority' AS priority,
                               c.metadata ->> 'summary' AS summary,
                               c.metadata ->> 'description' AS description,
                               c.metadata ->> 'root_cause' AS root_cause,
                               c.metadata ->> 'steps_to_reproduce' AS steps_to_reproduce,
                               c.metadata ->> 'expected_result' AS expected_result,
                               c.metadata ->> 'actual_result' AS actual_result,
                               c.metadata ->> 'resolution' AS resolution,
                               c.metadata ->> 'component' AS component,
                               c.metadata ->> 'assignee' AS assignee,
                               c.metadata ->> 'reporter' AS reporter,
                               c.metadata ->> 'fix_version' AS fix_version,
                               c.metadata ->> 'affects_version' AS affects_version,
                               c.metadata ->> 'qa_result' AS qa_result,
                               c.metadata ->> 'test_case' AS test_case,
                               c.metadata ->> 'environment' AS environment,
                               c.metadata ->> 'created' AS issue_created,
                               c.metadata ->> 'updated' AS issue_updated
                        FROM public.contents c
                        JOIN public.documents d ON d.document_id = c.document_id
                        WHERE d.project_code = %s AND {where}
                        ORDER BY COALESCE(c.metadata ->> 'updated', c.metadata ->> 'created', '') DESC,
                                 d.document_id, c.metadata ->> 'row_number', c.chunk_index, c.id
                        LIMIT %s
                        """,
                        tuple([scope, *params, max(self.top_k * 8, 80)]),
                    )
                    rows = [dict(row) for row in cur.fetchall()]
            except Exception:
                failed_scopes += 1
                LOGGER.warning("Issue SQL retrieval unavailable for scope=%s", scope, exc_info=True)
                continue
            successful_scopes += 1

            grouped: dict[str, list[dict[str, Any]]] = {}
            order: list[str] = []
            for row in rows:
                issue_key = str(row.get("issue_key") or "").strip()
                row_number = str(row.get("row_number") or "").strip()
                logical_id = (
                    f"KEY:{issue_key.upper()}" if issue_key
                    else f"ROW:{row.get('document_id') or ''}:{row_number or row.get('content_id')}"
                )
                if logical_id not in grouped:
                    grouped[logical_id] = []
                    order.append(logical_id)
                grouped[logical_id].append(row)

            for logical_id in order:
                chunks = grouped[logical_id]
                first = chunks[0]
                unique_parts: list[str] = []
                seen_text: set[str] = set()
                for row in chunks:
                    text = str(row.get("content") or "").strip()
                    if text and text not in seen_text:
                        seen_text.add(text)
                        unique_parts.append(text)
                content = "\n".join(unique_parts)[:8000]
                md = dict(first)
                md["artifact_type"] = "ISSUE_REGISTER"
                md["logical_issue_id"] = logical_id
                md["chunk_content_ids"] = [int(row["content_id"]) for row in chunks if row.get("content_id") is not None]
                md["chunk_count"] = len(chunks)
                result.append(
                    EvidenceItem(
                        evidence_id="",
                        scope=scope,
                        source_type="ISSUE_EXACT",
                        content=content,
                        content_id=int(first["content_id"]),
                        document_id=str(first.get("document_id") or ""),
                        file_name=str(first.get("file_name") or ""),
                        section_id=str(first.get("section_id") or ""),
                        page_number=int(first["page_number"]) if first.get("page_number") is not None else None,
                        metadata=md,
                    )
                )
                if len(result) >= max(self.top_k * 3, 30):
                    return result
        if failed_scopes and not successful_scopes:
            raise RuntimeError("Issue SQL retrieval unavailable in all selected scopes")
        return result[: max(self.top_k * 3, 30)]

    def _issue_statistics(
        self,
        question: str,
        scopes: Iterable[str],
        filters: dict[str, str],
    ) -> list[EvidenceItem]:
        scope_tuple = tuple(scopes)
        if "QABUG" in scope_tuple:
            return self._qabug_issue_statistics(question, filters)
        """Compute exact logical-issue counts instead of chunk counts.

        Each Jira/QA spreadsheet row has a stable issue_key or row_number. A
        long row can produce multiple RAG chunks; exact statistics therefore
        COUNT DISTINCT logical issue identities to avoid over-counting.
        """
        result: list[EvidenceItem] = []
        identity_expr = (
            "COALESCE(NULLIF(BTRIM(c.metadata ->> 'issue_key'), ''), "
            "d.document_id || '#ROW:' || COALESCE(NULLIF(c.metadata ->> 'row_number',''), c.id::text))"
        )
        for scope in scopes:
            where, params = self._issue_where(filters)
            try:
                with self._connect(scope) as conn, conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT COUNT(DISTINCT {identity_expr})::bigint AS total
                        FROM public.contents c
                        JOIN public.documents d ON d.document_id = c.document_id
                        WHERE d.project_code = %s AND {where}
                        """,
                        tuple([scope, *params]),
                    )
                    total = int((cur.fetchone() or {}).get("total") or 0)
                    breakdowns: dict[str, list[tuple[str, int]]] = {}
                    for label, expr in (
                        ("status", "c.metadata ->> 'status'"),
                        ("type", "c.metadata ->> 'issue_type'"),
                        ("priority", "c.metadata ->> 'priority'"),
                    ):
                        cur.execute(
                            f"""
                            SELECT COALESCE(NULLIF(BTRIM({expr}),''), 'UNSPECIFIED') AS key,
                                   COUNT(DISTINCT {identity_expr})::bigint AS count
                            FROM public.contents c
                            JOIN public.documents d ON d.document_id = c.document_id
                            WHERE d.project_code = %s AND {where}
                            GROUP BY 1 ORDER BY 2 DESC, 1
                            LIMIT 50
                            """,
                            tuple([scope, *params]),
                        )
                        breakdowns[label] = [(str(r["key"]), int(r["count"])) for r in cur.fetchall()]
            except Exception:
                LOGGER.debug("Issue statistics retrieval skipped for scope=%s", scope, exc_info=True)
                continue
            filter_text = ", ".join(f"{k}={v}" for k, v in filters.items()) or "none"
            lines = [f"{scope} QA/BUG issue statistics (filters: {filter_text})", f"Total logical issues: {total}"]
            for label in ("status", "type", "priority"):
                values = breakdowns.get(label) or []
                if values:
                    lines.append(label.title() + ": " + ", ".join(f"{k}={v}" for k, v in values))
            result.append(
                EvidenceItem(
                    evidence_id="",
                    scope=scope,
                    source_type="ISSUE_STATS",
                    content="\n".join(lines),
                    metadata={
                        "total": total,
                        "count_semantics": "DISTINCT_LOGICAL_ISSUE",
                        "filters": dict(filters),
                        "breakdowns": breakdowns,
                    },
                )
            )
        return result

    @staticmethod
    def _version_labels_in_question(question: str) -> tuple[str, ...]:
        labels: list[str] = []
        # Preserve the business label itself (2.10, 3.0.1, Ver.2.11, etc.) while
        # avoiding plain Specification Nos. such as 550.
        for match in re.finditer(r"(?:ver(?:sion)?\.?\s*)?(\d+(?:\.\d+){1,3}[A-Za-z0-9_-]*)", str(question or ""), flags=re.I):
            value = match.group(1).strip()
            if value and value not in labels:
                labels.append(value)
        return tuple(labels[:4])

    @staticmethod
    def _version_label_matches(actual: Any, requested: str) -> bool:
        left = re.sub(r"^(?:ver(?:sion)?\.?\s*)", "", str(actual or "").strip(), flags=re.I).casefold()
        right = re.sub(r"^(?:ver(?:sion)?\.?\s*)", "", str(requested or "").strip(), flags=re.I).casefold()
        return bool(left and right and left == right)

    def _temporal_relation_audit_search(
        self, scope: str, *, relation_id: int, new_target_version_id: int,
    ) -> EvidenceItem:
        """Single snapshot, read-only and fail-closed Stage 3 evidence audit.

        The report measures versioned text mapping, not engineering validity.
        Incomplete audits have a distinct weak evidence class.
        """
        from app.engineering_intelligence.relation_temporal_audit import assess_temporal_relation

        profile = self.postgres_profiles.get(scope)
        if (scope not in {'21MM', '24MM', 'COMMON'} or profile is None
                or profile.database.lower() in {'rag', 'rag_qabug'}
                or profile.user.lower() in {'postgres', 'root'}):
            raise ValueError('Configured restricted engineering database required')
        with self._connect(scope) as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
                    cur.execute("SET LOCAL lock_timeout='5s'")
                    cur.execute("SET LOCAL statement_timeout='30s'")
                    cur.execute('SELECT current_database() AS db, project_code FROM public.project_info')
                    identity = cur.fetchall()
                    if (len(identity) != 1 or str(identity[0]['db']) != profile.database
                            or identity[0]['project_code'] != scope):
                        raise RuntimeError('Project database identity mismatch')
                    result = assess_temporal_relation(
                        cur, project=scope, relation_id=relation_id,
                        new_target_version_id=new_target_version_id,
                    )
            finally:
                conn.rollback()
        status = str(result['status'])
        issues = tuple(str(v)[:100] for v in result.get('issues', ())[:5])
        verified = status != 'INSUFFICIENT_EVIDENCE'
        content = (
            f'Stage 3 historical relation audit (read-only; NOT engineering approval): '
            f'project={scope}, relation_id={relation_id}, '
            f'new_target_version_id={new_target_version_id}\n'
            f'Historical target text mapping status: {status}. '
            f'Engineering relation validity: UNDETERMINED; human revalidation REQUIRED.\n'
            f'Old target: SPEC {result.get("target_spec_code", "UNVERIFIED")}, '
            f'version {result.get("old_target_version", "UNVERIFIED")}, '
            f'section {result.get("old_target_section", "UNVERIFIED")}; '
            f'new target version {result.get("new_target_version", "UNVERIFIED")}.\n'
            f'Old target SHA256={result.get("old_target_hash", "UNVERIFIED")}; '
            f'new target SHA256={result.get("new_target_hash", "UNVERIFIED")}.\n'
            f'Source version effectiveness alignment: '
            f'{result.get("source_version_alignment", "NOT_ESTABLISHED")}; '
            f'full content digest scan: {result.get("full_source_digest_check", "NOT_PERFORMED")}.\n'
            + (f'Rejection reasons: {", ".join(issues)}.\n' if issues else '')
            + 'Text continuity alone NEVER proves relation continuity, compliance, impact or invalidation.'
        )
        return EvidenceItem(
            evidence_id='', scope=scope,
            source_type='TEMPORAL_RELATION_AUDIT' if verified else 'TEMPORAL_RELATION_UNVERIFIED',
            content=content, relation_id=relation_id,
            specification_code=str(result.get('target_spec_code') or ''),
            specification_version=str(result.get('new_target_version') or ''),
            section_id=str(result.get('old_target_section') or ''),
            metadata={
                'evidence_role': 'TEMPORAL_MAPPING_STATUS_ONLY',
                'audit_status': status,
                'relation_validity': 'UNDETERMINED',
                'human_revalidation_required': True,
                'authority': 'READ_ONLY_EVIDENCE_AUDIT_NOT_APPROVAL',
                'relation_id': relation_id,
                'new_target_version_id': new_target_version_id,
                'issues': issues,
                'full_source_digest_check': result.get('full_source_digest_check', 'NOT_PERFORMED'),
            },
        )

    def _version_search(self, question: str, scopes: Iterable[str]) -> list[EvidenceItem]:
        # Compatibility note: public.specification_version_history,
        # public.specification_change_summary and public.specification_change_detail remain
        # governed read models for UI/report compatibility. v1.01 deliberately reads the
        # base tables here so legacy views cannot re-promote a cover sheet over the body.
        # SQL read model, while this runtime path reads base rows directly so v1.01 also
        # protects databases whose view has not yet been refreshed by one-click upgrade.
        """Read immutable body-only history and persisted body-to-body diffs.

        Cover sheets and other supporting artefacts are audit metadata, not
        business versions.  v1.01 therefore applies the same PRIMARY-content
        arbitration used by RAG and Engineering Relations to both endpoints.
        """
        result: list[EvidenceItem] = []
        question_text = str(question or "")
        requested_versions = self._version_labels_in_question(question_text)
        primary_vd = PrimarySpecificationDocumentPolicy.sql_primary_predicate("vd")
        primary_fd = PrimarySpecificationDocumentPolicy.sql_primary_predicate("fd")
        primary_td = PrimarySpecificationDocumentPolicy.sql_primary_predicate("td")

        for scope in scopes:
            try:
                lookup_items, _warnings = self._specification_lookup(question_text, (scope,))
                specs: list[dict[str, Any]] = []
                seen_ids: set[int] = set()
                for item in lookup_items:
                    spec_id = int((item.metadata or {}).get("specification_id") or 0)
                    if not spec_id or spec_id in seen_ids:
                        continue
                    seen_ids.add(spec_id)
                    specs.append({
                        "specification_id": spec_id,
                        "specification_key": (item.metadata or {}).get("specification_key"),
                        "spec_code": item.specification_code,
                        "canonical_name": item.specification_name,
                        "current_version": item.specification_version,
                        "current_document_id": item.document_id,
                    })
                if not specs:
                    continue

                with self._connect(scope) as conn, conn.cursor() as cur:
                    for spec in specs[:8]:
                        spec_id = int(spec["specification_id"])
                        code = str(spec.get("spec_code") or spec.get("specification_key") or "")
                        name = str(spec.get("canonical_name") or "")

                        cur.execute(
                            f"""
                            WITH primary_versions AS (
                                SELECT sv.id, sv.version_label, sv.version_sort_key, sv.version_seq,
                                       sv.document_id, sv.status, sv.created_at
                                FROM public.specification_versions sv
                                LEFT JOIN public.documents vd ON vd.document_id = sv.document_id
                                WHERE sv.specification_id = %s
                                  AND sv.branch_name = 'MAIN'
                                  AND {primary_vd}
                            ), ordered AS (
                                SELECT pv.*,
                                       LEAD(pv.id) OVER (
                                           ORDER BY pv.version_sort_key DESC NULLS LAST, pv.version_seq DESC, pv.id DESC
                                       ) AS previous_version_id,
                                       LEAD(pv.version_label) OVER (
                                           ORDER BY pv.version_sort_key DESC NULLS LAST, pv.version_seq DESC, pv.id DESC
                                       ) AS previous_version_label
                                FROM primary_versions pv
                            ), preferred AS (
                                SELECT pv.id
                                FROM primary_versions pv
                                JOIN public.specifications sp ON sp.id = %s
                                ORDER BY
                                    CASE WHEN pv.id = sp.current_version_id THEN 0 ELSE 1 END,
                                    pv.version_sort_key DESC NULLS LAST, pv.version_seq DESC, pv.id DESC
                                LIMIT 1
                            )
                            SELECT o.id AS specification_version_id, o.version_label, o.previous_version_label,
                                   o.document_id, o.status,
                                   (o.id = (SELECT id FROM preferred)) AS is_current,
                                   CASE WHEN o.id = (SELECT id FROM preferred) THEN 'CURRENT' ELSE 'HISTORY' END AS version_role,
                                   o.version_sort_key,
                                   COALESCE(cs.added_count, 0) AS added_count,
                                   COALESCE(cs.modified_count, 0) AS modified_count,
                                   COALESCE(cs.removed_count, 0) AS removed_count,
                                   COALESCE(cs.moved_count, 0) AS moved_count,
                                   cs.summary_text AS deterministic_change_summary,
                                   o.created_at
                            FROM ordered o
                            LEFT JOIN public.specification_change_sets cs
                              ON cs.specification_id = %s
                             AND cs.from_version_id = o.previous_version_id
                             AND cs.to_version_id = o.id
                             AND cs.is_adjacent IS TRUE
                             AND cs.status = 'READY'
                             AND cs.algorithm_version = 'section-structural-v1'
                            ORDER BY o.version_sort_key DESC NULLS LAST, o.version_seq DESC, o.id DESC
                            LIMIT 30
                            """,
                            (spec_id, spec_id, spec_id),
                        )
                        history = [dict(row) for row in cur.fetchall()]
                        if history:
                            lines = [f"Version history for {code or '-'} {name}:".rstrip()]
                            for row in history:
                                role = "CURRENT" if row.get("is_current") else "HISTORY"
                                lines.append(
                                    f"- {row.get('version_label') or '-'} [{role}/PRIMARY_CONTENT] "
                                    f"document={row.get('document_id') or '-'} "
                                    f"changes(+{row.get('added_count') or 0}/~{row.get('modified_count') or 0}/-"
                                    f"{row.get('removed_count') or 0}/moved {row.get('moved_count') or 0})"
                                )
                            current = next((row for row in history if row.get("is_current")), None)
                            result.append(EvidenceItem(
                                evidence_id="", scope=scope, source_type="VERSION_HISTORY",
                                content="\n".join(lines), specification_code=code,
                                specification_name=name,
                                specification_version=str((current or {}).get("version_label") or spec.get("current_version") or ""),
                                document_id=str((current or {}).get("document_id") or spec.get("current_document_id") or ""),
                                metadata={
                                    "specification_id": spec_id,
                                    "history_count": len(history),
                                    "document_role": "PRIMARY",
                                    "evidence_role": "PRIMARY_CONTENT",
                                },
                            ))

                        cur.execute(
                            f"""
                            SELECT cs.id AS change_set_id, cs.specification_id,
                                   sp.specification_key, sp.project_code, sp.canonical_name,
                                   fv.version_label AS from_version, tv.version_label AS to_version,
                                   cs.status, cs.added_count, cs.modified_count, cs.removed_count,
                                   cs.moved_count, cs.unchanged_count,
                                   (cs.added_count + cs.modified_count + cs.removed_count + cs.moved_count) AS total_change_count,
                                   cs.summary_text, cs.created_at, cs.updated_at
                            FROM public.specification_change_sets cs
                            JOIN public.specifications sp ON sp.id = cs.specification_id
                            JOIN public.specification_versions fv ON fv.id = cs.from_version_id
                            JOIN public.specification_versions tv ON tv.id = cs.to_version_id
                            LEFT JOIN public.documents fd ON fd.document_id = fv.document_id
                            LEFT JOIN public.documents td ON td.document_id = tv.document_id
                            WHERE cs.specification_id = %s
                              AND cs.status = 'READY'
                              AND {primary_fd}
                              AND {primary_td}
                            ORDER BY cs.updated_at DESC NULLS LAST, cs.id DESC
                            LIMIT 20
                            """,
                            (spec_id,),
                        )
                        change_sets = [dict(row) for row in cur.fetchall()]
                        if requested_versions:
                            requested = requested_versions[:2]
                            filtered: list[dict[str, Any]] = []
                            for row in change_sets:
                                endpoints = (str(row.get("from_version") or ""), str(row.get("to_version") or ""))
                                if len(requested) >= 2:
                                    if (
                                        self._version_label_matches(endpoints[0], requested[0])
                                        and self._version_label_matches(endpoints[1], requested[1])
                                    ) or (
                                        self._version_label_matches(endpoints[0], requested[1])
                                        and self._version_label_matches(endpoints[1], requested[0])
                                    ):
                                        filtered.append(row)
                                elif any(self._version_label_matches(value, requested[0]) for value in endpoints):
                                    filtered.append(row)
                            change_sets = filtered
                        else:
                            change_sets = change_sets[:3]

                        for row in change_sets:
                            change_set_id = int(row["change_set_id"])
                            cur.execute(
                                """
                                SELECT id AS change_id, change_type, entity_type, entity_key,
                                       chapter_id, chapter_title, section_id, section_title,
                                       old_page_number, new_page_number, similarity_score,
                                       old_text, new_text, details
                                FROM public.specification_changes
                                WHERE change_set_id = %s
                                ORDER BY
                                  CASE change_type WHEN 'MODIFIED' THEN 1 WHEN 'ADDED' THEN 2
                                                   WHEN 'REMOVED' THEN 3 WHEN 'MOVED' THEN 4 ELSE 9 END,
                                  id
                                LIMIT 40
                                """,
                                (change_set_id,),
                            )
                            details = [dict(item) for item in cur.fetchall()]
                            lines = [
                                f"Version comparison for {code or '-'} {name}: {row.get('from_version') or '-'} → {row.get('to_version') or '-' }.",
                                "Evidence endpoints: PRIMARY_CONTENT → PRIMARY_CONTENT.",
                                f"Counts: added={row.get('added_count') or 0}, modified={row.get('modified_count') or 0}, "
                                f"removed={row.get('removed_count') or 0}, moved={row.get('moved_count') or 0}, unchanged={row.get('unchanged_count') or 0}.",
                            ]
                            if row.get("summary_text"):
                                lines.append(f"Deterministic summary: {row.get('summary_text')}")
                            if details:
                                lines.append("Auditable changes:")
                                for idx, detail in enumerate(details, start=1):
                                    locator = " / ".join(
                                        x for x in (
                                            str(detail.get("chapter_title") or detail.get("chapter_id") or "").strip(),
                                            str(detail.get("section_title") or detail.get("section_id") or "").strip(),
                                        ) if x
                                    ) or str(detail.get("entity_key") or detail.get("entity_type") or "-")
                                    lines.append(f"{idx}. {detail.get('change_type') or '-'} | {locator}")
                                    old_text = re.sub(r"\s+", " ", str(detail.get("old_text") or "")).strip()
                                    new_text = re.sub(r"\s+", " ", str(detail.get("new_text") or "")).strip()
                                    if old_text:
                                        lines.append(f"   OLD: {old_text[:320]}")
                                    if new_text:
                                        lines.append(f"   NEW: {new_text[:320]}")
                            else:
                                lines.append("No persisted detail rows are available for this comparison.")
                            result.append(EvidenceItem(
                                evidence_id="", scope=scope, source_type="VERSION_CHANGE_DETAIL",
                                content="\n".join(lines), specification_code=code,
                                specification_name=name, specification_version=str(row.get("to_version") or ""),
                                metadata={
                                    **row,
                                    "detail_count": len(details),
                                    "from_document_role": "PRIMARY",
                                    "to_document_role": "PRIMARY",
                                    "evidence_role": "PRIMARY_CONTENT",
                                },
                            ))
            except Exception:
                LOGGER.debug("Version retrieval skipped for scope=%s", scope, exc_info=True)
                continue
        return result[: max(self.top_k * 2, 20)]

    def _unresolved_reference_search(self, question: str, scopes: Iterable[str]) -> list[EvidenceItem]:
        tokens = self._tokens(question)
        result: list[EvidenceItem] = []
        for scope in scopes:
            if scope == "QABUG":
                continue
            clauses: list[str] = []
            params: list[Any] = []
            for token in tokens[:5]:
                value = f"%{token}%"
                clauses.append("(COALESCE(source_specification_key,'') ILIKE %s OR COALESCE(source_specification_name,'') ILIKE %s OR COALESCE(target_external_key,'') ILIKE %s)")
                params.extend([value, value, value])
            where = " OR ".join(clauses) if clauses else "TRUE"
            sql = (
                "SELECT relation_id, source_specification_key, source_specification_name, source_version, "
                "target_external_key, extraction_method, confidence, status, evidence_count "
                f"FROM public.spec_reference_unresolved_current WHERE ({where}) "
                "ORDER BY confidence DESC NULLS LAST, evidence_count DESC LIMIT %s"
            )
            try:
                with self._connect(scope) as conn, conn.cursor() as cur:
                    cur.execute(sql, tuple(params + [max(1, min(self.top_k * 2, 20))]))
                    rows = [dict(row) for row in cur.fetchall()]
            except Exception:
                LOGGER.debug("Unresolved reference retrieval skipped for scope=%s", scope, exc_info=True)
                continue
            for row in rows:
                content = (
                    f"Unresolved engineering reference: {row.get('source_specification_name') or row.get('source_specification_key') or 'Unknown'} "
                    f"({row.get('source_specification_key') or '-'}) → {row.get('target_external_key') or 'unknown target'}. "
                    f"method={row.get('extraction_method') or '-'}, confidence={row.get('confidence')}, evidence_count={row.get('evidence_count')}."
                )
                result.append(EvidenceItem(
                    evidence_id="", scope=scope, source_type="UNRESOLVED_REFERENCE", content=content,
                    score=float(row.get('confidence') or 0.0),
                    specification_code=str(row.get('source_specification_key') or ''),
                    specification_name=str(row.get('source_specification_name') or ''),
                    specification_version=str(row.get('source_version') or ''),
                    relation_id=int(row['relation_id']) if row.get('relation_id') is not None else None,
                    metadata=row,
                ))
        return result[: max(self.top_k * 2, 20)]

    @staticmethod
    def _entity_spec_codes(entities: Iterable[str], question: str = "") -> tuple[str, ...]:
        """Extract specification codes conservatively for vector filtering.

        Arbitrary 3-6 digit values in engineering text can be years, ports,
        timing values or IDs. Planner entities may be plain numeric codes, but
        question text is accepted only when the number is explicitly marked as
        a specification identifier.
        """
        codes: list[str] = []
        for value in entities:
            raw = str(value or "").strip()
            match = re.fullmatch(r"(?:SPEC[-_: ]*)?(\d{3,6})", raw, flags=re.I)
            if match and match.group(1) not in codes:
                codes.append(match.group(1))
        patterns = (
            r"(?:SPEC[-_: ]+)(\d{3,6})",
            r"(?<![A-Za-z0-9])(\d{3,6})(?!\d)(?=\s*(?:式样书|式樣書|仕様書|规格书?|規格書?|specification\b|spec\b))",
            r"(?:21MM|24MM|COMMON)\s*(?:(?:项目|項目|工程)\s*)?(?:中|里|裡|内|內|的|中的|里的|裡的|内的|內的)?\s*(?:编号|編號|No\.?|SPEC[-_: ]*)?\s*(\d{3,6})(?!\d)",
            r"(?:式样书?|式樣書?|仕様書|规格书?|規格書?|specification|spec)\s*(?:编号|編號|No\.?|#)?\s*(\d{3,6})(?!\d)",
            r"(?:^|[\s,，:：])(\d{3,6})(?=\s*(?:的|是什么|是什麼|の|について|相关|關聯|关联|参照|參照|参考|引用|式样|式樣|仕様|spec))",
            r"^(?:(?:请|請)?(?:(?:给我|給我|帮我|幫我)\s*|(?:给我|給我|帮我|幫我)?\s*(?:找|查|搜|看|打开|打開)(?:一下|下|找|询|詢)?\s*))?(\d{3,6})(?:\s*(?:这本|這本|式样书?|式樣書?|仕様書|规格书?|規格書?|spec))?[?？。.!！\s]*$",
        )
        question_text = str(question or "")
        for pattern in patterns:
            for match in re.finditer(pattern, question_text, flags=re.I):
                code = match.group(1)
                if not re.fullmatch(r"(?:19|20)\d{2}", code) and code not in codes:
                    codes.append(code)
        # In an explicit relation/version question, a bare 2-6 digit token is
        # overwhelmingly likely to be a Specification No.  Avoid decimal
        # components so version labels such as 2.10 do not become spec code 10.
        if re.search(r"引用|参照|參照|参考|參考|依赖|依賴|关系|關係|reference|depend|upstream|downstream|关联|關聯|版本|revision|rev|diff|变化|变更|历史|修改", question_text, flags=re.I):
            for match in re.finditer(r"(?<![A-Za-z0-9_.-])(\d{2,6})(?![\d.])", question_text):
                code = match.group(1)
                if not re.fullmatch(r"(?:19|20)\d{2}", code) and code not in codes:
                    codes.append(code)
        return tuple(codes[:8])

    @staticmethod
    def _qabug_structured_only_filter_keys(filters: dict[str, str] | None) -> tuple[str, ...]:
        """Return explicit QA/BUG predicates that cannot be losslessly mirrored in Qdrant.

        `date_basis` is routing metadata, not a constraint by itself.  The set is
        deliberately allow-list based: a new SQL predicate defaults to SQL-only
        until a reviewed Qdrant payload/index contract is added for it.
        """
        if not filters:
            return ()
        qdrant_mirrorable = {
            "record_type", "status_norm", "priority_norm", "project_key",
            "issue_type", "component", "assignee", "reporter",
            "release_version", "affected_version", "software_version",
            "grade", "destination", "model", "section_type",
        }
        non_constraints = {"date_basis"}
        result: list[str] = []
        for key, value in filters.items():
            if key in qdrant_mirrorable or key in non_constraints or key == "issue_key":
                continue
            if str(value or "").strip() and key not in result:
                result.append(key)
        return tuple(sorted(result))

    @staticmethod
    def _semantic_filter(
        scope: str,
        spec_codes: tuple[str, ...],
        *,
        artifact_type: str = "",
        issue_key: str = "",
        field_filters: dict[str, str] | None = None,
    ):
        """Build selective Qdrant filters without exposing arbitrary expressions.

        Scope/CURRENT are collection invariants. Specification and issue-register
        filters are added only when they materially constrain the candidate set.
        """
        try:
            from qdrant_client.models import FieldCondition, Filter, MatchValue
        except ImportError:
            return None
        conditions = []
        must_not = []
        if len(spec_codes) == 1:
            conditions.append(FieldCondition(key="spec_code", match=MatchValue(value=spec_codes[0])))
        if artifact_type and not (scope == "QABUG" and str(artifact_type).upper() == "QABUG_REGISTER"):
            conditions.append(FieldCondition(key="artifact_type", match=MatchValue(value=str(artifact_type))))
        if issue_key:
            conditions.append(FieldCondition(key="issue_key", match=MatchValue(value=str(issue_key))))
        if scope != "QABUG":
            # New v1.01 indexes carry these governance fields. ``must_not`` also
            # remains backward-compatible because legacy points with a missing
            # field are not rejected; filename arbitration is applied after the
            # query for those older points.
            must_not.extend([
                FieldCondition(key="document_role", match=MatchValue(value="SUPPORTING")),
                FieldCondition(key="qa_index_policy", match=MatchValue(value="EXCLUDE")),
            ])
        if scope == "QABUG" and field_filters:
            qdrant_keys = {
                "record_type":"record_type", "status_norm":"status", "priority_norm":"priority",
                "project_key":"project_key", "issue_type":"issue_type", "component":"component",
                "assignee":"assignee", "reporter":"reporter", "release_version":"release_version",
                "affected_version":"affected_version", "software_version":"software_version",
                "grade":"grade", "destination":"destination", "model":"model", "section_type":"section_type",
            }
            for filter_key, payload_key in qdrant_keys.items():
                value = str(field_filters.get(filter_key) or "").strip()
                if value:
                    conditions.append(FieldCondition(key=payload_key, match=MatchValue(value=value)))
        return Filter(must=conditions, must_not=must_not) if (conditions or must_not) else None

    def _sparse_search(
        self, question: str, scopes: Iterable[str], *, entity_codes: tuple[str, ...] = (),
        cancel_check: Callable[[], bool] | None = None,
    ) -> list[EvidenceItem]:
        """Sparse-only Qdrant sidecar + mandatory PostgreSQL body hydration.

        Unlike Dense, a sparse hit is NEVER allowed to supply original content
        from its Qdrant payload. Failed hash/identity checks drop the hit.
        """
        from app.ai.stage4_sparse import (
            BGEM3SparseEncoder, MODEL_NAME, SPARSE_NAME, sparse_collection,
        )
        from qdrant_client.models import SparseVector

        if cancel_check and cancel_check():
            return []
        client = self._get_qdrant_client()
        available = []
        for scope in scopes:
            if scope == "QABUG" or scope not in self.collections:
                continue
            name = sparse_collection(self.collections[scope])
            if self._collection_exists(client, name):
                available.append((scope, name))
        if not available:
            return []  # no sidecar: do not load a second CPU model
        if self._sparse_encoder is None:
            self._sparse_encoder = BGEM3SparseEncoder()
        [encoded] = self._sparse_encoder.encode([question])
        results: list[EvidenceItem] = []
        for scope, name in available:
            if cancel_check and cancel_check():
                break
            result = client.query_points(
                collection_name=name, query=SparseVector(
                    indices=encoded.indices, values=encoded.values,
                ), using=SPARSE_NAME,
                query_filter=self._semantic_filter(scope, entity_codes),
                limit=max(self.top_k * 4, 32), with_payload=True, with_vectors=False,
            )
            hits = list(getattr(result, "points", []) or [])
            by_id: dict[int, tuple[Any, dict[str, Any]]] = {}
            for hit in hits:
                payload = dict(getattr(hit, "payload", {}) or {})
                try:
                    cid = int(payload.get("content_id"))
                except (TypeError, ValueError):
                    continue
                if (cid < 0 or cid in by_id or str(getattr(hit, "id", "")) != str(cid)
                        or payload.get("project_code") != scope
                        or payload.get("index_scope") != "CURRENT"
                        or payload.get("sparse_model") != MODEL_NAME
                        or payload.get("sparse_vector_name") != SPARSE_NAME
                        or not payload.get("content_hash")
                        or not PrimarySpecificationDocumentPolicy.is_primary(
                            file_name=str(payload.get("file_name") or ""), metadata=payload,
                        )):
                    continue
                by_id[cid] = (hit, payload)
            if not by_id:
                continue
            # SQL is the only source of sparse body text and identity. The
            # metadata-only sidecar is not a second source of engineering truth.
            authoritative = {int(r["content_id"]): r for r in self._fetch_chunks(scope, list(by_id))}
            for cid, (hit, payload) in by_id.items():
                row = authoritative.get(cid)
                if not row:
                    continue
                if any(str(row.get(row_key) or "") != str(payload.get(payload_key) or "")
                       for row_key, payload_key in (
                           ("document_id", "document_id"), ("file_name", "file_name"),
                           ("specification_version", "version"),
                           ("specification_code", "spec_code"),
                           ("content_hash", "content_hash"),
                       )):
                    continue
                if not PrimarySpecificationDocumentPolicy.is_primary(
                    file_name=str(row.get("file_name") or ""), metadata=row,
                ):
                    continue
                if hashlib.sha256(str(row.get("content") or "").encode("utf-8")).hexdigest() != str(row.get("content_hash") or "").lower():
                    continue
                if str(row.get("project_code") or "") != scope:
                    continue
                try:
                    sparse_score = float(getattr(hit, "score", None))
                except (TypeError, ValueError):
                    continue
                if not math.isfinite(sparse_score) or sparse_score < 0:
                    continue
                md = {**payload, "knowledge_origin": "QDRANT_SPARSE",
                      "hydration": "POSTGRES_HASH_AND_IDENTITY_VERIFIED",
                      "evidence_role": "PRIMARY_CONTENT", "ranking_only": True}
                results.append(self._chunk_to_evidence(
                    scope, row, "QDRANT_SPARSE", sparse_score, md,
                ))
        results.sort(key=lambda item: -(item.score or 0.0))
        return results[:max(self.top_k * 4, 32)]

    def _semantic_search(
        self,
        question: str,
        scopes: Iterable[str],
        *,
        entity_codes: tuple[str, ...] = (),
        cancel_check: Callable[[], bool] | None = None,
        source_type: str = "SEMANTIC",
        artifact_type: str = "",
        issue_key: str = "",
        field_filters: dict[str, str] | None = None,
        metrics: dict[str, int] | None = None,
        stage_callback: Callable[[str, str, int], None] | None = None,
        observation_only: bool = False,
    ) -> list[EvidenceItem]:
        if cancel_check and cancel_check():
            return []
        metrics = metrics if metrics is not None else {}

        from app.ai.retrieval_deadline import remaining_ms
        remaining_ms()

        cold_start = not self.embedding_loaded
        if stage_callback:
            stage_callback(
                "EMBEDDING",
                "正在加载 BGE-M3 并生成查询向量…" if cold_start else "正在生成查询向量…",
                0,
            )
        started = time.perf_counter()
        vector = self._embed(question)
        metrics["embedding_ms"] = int((time.perf_counter() - started) * 1000)
        if stage_callback:
            detail = f"查询向量完成 · {metrics['embedding_ms']} ms"
            if cold_start:
                detail += " · 首次模型冷启动"
            stage_callback("EMBEDDING", detail, metrics["embedding_ms"])

        client = self._get_qdrant_client()
        hits_by_scope: dict[str, list[Any]] = {}
        missing_collections: list[str] = []
        checked_collections = 0
        if stage_callback:
            stage_callback("QDRANT", "正在检索 Current Qdrant collection…", 0)
        qstarted = time.perf_counter()
        for scope in scopes:
            if cancel_check and cancel_check():
                break
            collection = self.collections.get(scope)
            if not collection:
                continue
            if not self._collection_exists(client, collection):
                missing_collections.append(scope)
                continue
            checked_collections += 1
            query_filter = self._semantic_filter(
                scope, entity_codes, artifact_type=artifact_type, issue_key=issue_key,
                field_filters=field_filters,
            )
            if scope=="QABUG" and observation_only:
                from qdrant_client.models import Filter,FieldCondition,MatchAny
                observation=FieldCondition(key="section_type",match=MatchAny(any=sorted(ISSUE_OBSERVATION_SECTIONS)))
                query_filter=Filter(must=[query_filter,observation] if query_filter is not None else [observation])
            # Supporting files can rank above a body in legacy collections.
            # Over-fetch before v1.01 filename/metadata arbitration so rejecting
            # a Cover Sheet does not accidentally discard the body at rank K+1.
            candidate_limit = self.top_k if scope == "QABUG" else max(self.top_k * 4, 32)
            remaining=remaining_ms()
            budget_options={"timeout":max(1,min(10,(remaining+999)//1000))} if remaining is not None else {}
            try:
                response = client.query_points(
                    collection_name=collection,
                    query=vector,
                    query_filter=query_filter,
                    limit=candidate_limit,
                    with_payload=True,
                    with_vectors=False,
                    **budget_options,
                )
                hits = list(getattr(response, "points", []) or [])
            except (AttributeError, TypeError):
                hits = list(
                    client.search(
                        collection_name=collection,
                        query_vector=vector,
                        query_filter=query_filter,
                        limit=candidate_limit,
                        with_payload=True,
                        with_vectors=False,
                        **budget_options,
                    )
                )
            hits_by_scope[scope] = [
                hit
                for hit in hits
                if float(getattr(hit, "score", 0.0) or 0.0) >= self.min_score
            ]
        metrics["qdrant_ms"] = int((time.perf_counter() - qstarted) * 1000)
        if not checked_collections and missing_collections:
            raise RuntimeError("Qdrant collection missing in all requested scopes: " + ", ".join(missing_collections))
        total_hits = sum(len(values) for values in hits_by_scope.values())
        if stage_callback:
            stage_callback("QDRANT", f"Qdrant 命中 {total_hits} 条 · {metrics['qdrant_ms']} ms", metrics["qdrant_ms"])

        items: list[EvidenceItem] = []
        hydration_started = time.perf_counter()
        hydrated_rows = 0
        rejected_rows = 0
        for scope, hits in hits_by_scope.items():
            # Qdrant supplies candidate IDs/ranks ONLY, never authoritative text,
            # document identity, revisions or roles. Fail closed on a failed SQL
            # lookup, including for legacy Dense payloads carrying full text.
            by_id: dict[int, tuple[float, dict[str, Any]]] = {}
            for hit in hits:
                payload = dict(getattr(hit, "payload", {}) or {})
                if scope != "QABUG" and not PrimarySpecificationDocumentPolicy.is_primary(
                    file_name=str(payload.get("file_name") or ""), metadata=payload,
                ):
                    rejected_rows += 1
                    continue
                try:
                    cid = int(payload.get("content_id", getattr(hit, "id", None)))
                    score = float(getattr(hit, "score", 0.0))
                except (ValueError, TypeError):
                    rejected_rows += 1
                    continue
                if cid <= 0 or not math.isfinite(score):
                    rejected_rows += 1
                    continue
                if cid not in by_id:
                    by_id[cid] = (score, payload)
            if not by_id:
                continue
            if stage_callback:
                stage_callback("HYDRATE", f"PostgreSQL 验证 Qdrant {len(by_id)} 条候选原文…", 0)
            try:
                rows = self._fetch_chunks(scope, list(by_id))
            except Exception:
                LOGGER.warning("Rejecting unverified Dense hits scope=%s: PostgreSQL unavailable", scope, exc_info=True)
                rejected_rows += len(by_id)
                continue
            hydrated_rows += len(rows)
            authoritative = {int(row["content_id"]): row for row in rows}
            for cid, (score, payload) in by_id.items():
                row = authoritative.get(cid)
                if row is None or not str(row.get("content") or "").strip():
                    rejected_rows += 1
                    continue
                # Verify actual SQL content against its stored digest, then
                # require the indexed digest to equal SQL. Optional or absent
                # hashes are not evidence of integrity.
                body_hash = hashlib.sha256(str(row["content"]).encode("utf-8")).hexdigest()
                sql_hash = str(row.get("content_hash") or "").lower()
                payload_hash = str(payload.get("content_hash") or "").lower()
                if not sql_hash or not payload_hash or body_hash != sql_hash or sql_hash != payload_hash:
                    LOGGER.warning("Skipping stale Qdrant hit scope=%s content_id=%s: hash verification failed", scope, cid)
                    rejected_rows += 1
                    continue
                if scope != "QABUG":
                    # A permissive legacy filename classifier is NOT proof of
                    # SPEC_BODY; SQL's join to the documents role predicate is
                    # authoritative, and identity must match the indexed point.
                    identities = (
                        ("project_code", "project_code"),
                        ("document_id", "document_id"),
                        ("file_name", "file_name"),
                    )
                    if (str(row.get("project_code") or "") != scope
                            or not all(str(row.get(k) or "") and
                                       str(row.get(k) or "") == str(payload.get(pk) or "")
                                       for k, pk in identities)
                            or any(str(row.get(k) or "") != str(payload.get(pk) or "")
                                   for k, pk in (("specification_code", "spec_code"),
                                                 ("specification_version", "version")))
                            or not PrimarySpecificationDocumentPolicy.is_primary(
                                file_name=str(row.get("file_name") or ""), metadata=row,
                            )):
                        rejected_rows += 1
                        continue
                else:
                    if observation_only and str(row.get("section_id") or "") not in ISSUE_OBSERVATION_SECTIONS:
                        rejected_rows += 1
                        continue
                    # Issue payload may omit some fields for old indexes, but
                    # supplied document identity must never contradict SQL.
                    if any(str(row.get(k) or "") != str(payload[k])
                           for k in ("project_code", "document_id", "file_name") if k in payload):
                        rejected_rows += 1
                        continue
                md = dict(payload)
                if scope=="QABUG" and observation_only:
                    md.update(issue_key=str(row.get("issue_key") or ""),record_key=str(row.get("document_id") or ""),
                              section_type=str(row.get("section_id") or ""))
                md.update({
                    "knowledge_origin": "QDRANT",
                    "hydration": "POSTGRES_HASH_AND_IDENTITY_VERIFIED",
                    "source_authority": "POSTGRESQL",
                    "content_hash": sql_hash,
                    "ranking_only": True,
                })
                if scope != "QABUG":
                    md["evidence_role"] = "PRIMARY_CONTENT"
                    md["identity_verified"] = bool(str(row.get("specification_code") or "").strip())
                    if not md["identity_verified"]:
                        md["identity_limitation"] = "Physical SQL body verified; numeric specification assignment is unverified"
                items.append(self._chunk_to_evidence(scope, row, source_type, score, md))

        metrics["hydration_ms"] = int((time.perf_counter() - hydration_started) * 1000)
        if stage_callback:
            stage_callback(
                "HYDRATE",
                f"SQL已验证 {len(items)} 条 · 拒绝 {rejected_rows} 条 · {metrics['hydration_ms']} ms",
                metrics["hydration_ms"],
            )
        items.sort(key=lambda item: float(item.score or 0.0), reverse=True)
        return items[: self.top_k]

    def _connect(self, scope: str):
        import psycopg
        from psycopg.rows import dict_row

        profile = self.postgres_profiles.get(scope)
        if profile is None:
            raise RuntimeError(f"PostgreSQL profile for {scope} is not configured")
        kwargs = profile.dsn_kwargs()
        kwargs["password"] = self.postgres_password
        from app.ai.retrieval_deadline import remaining_ms
        remaining=remaining_ms()
        if remaining is not None:
            kwargs["connect_timeout"]=max(1,min(3,(remaining+999)//1000))
            kwargs["options"]=f"-c default_transaction_read_only=on -c statement_timeout={max(1,min(5000,remaining))} -c lock_timeout=1000"
        return psycopg.connect(**kwargs, row_factory=dict_row)

    def _fetch_chunks(self, scope: str, content_ids: list[int]) -> list[dict[str, Any]]:
        if not content_ids:
            return []
        with self._connect(scope) as conn, conn.cursor() as cur:
            source_view = "public.qabug_current_chunks" if scope == "QABUG" else "public.rag_current_chunks"
            if scope == "QABUG":
                cur.execute(
                    f"""
                    SELECT content_id, document_id, file_name, project_code,
                           issue_key,
                           specification_code, specification_name, specification_version,
                           chapter_title, section_id, section_title, page_number,
                           chunk_index, content_hash, content
                    FROM {source_view}
                    WHERE content_id = ANY(%s)
                    """,
                    (content_ids,),
                )
            else:
                primary_d = PrimarySpecificationDocumentPolicy.sql_primary_predicate("d")
                cur.execute(
                    f"""
                    SELECT rc.content_id, rc.document_id, rc.file_name, rc.project_code,
                           rc.specification_code, rc.specification_name, rc.specification_version,
                           rc.chapter_title, rc.section_id, rc.section_title, rc.page_number,
                           rc.chunk_index, rc.content_hash, rc.content
                    FROM {source_view} rc
                    JOIN public.documents d ON d.document_id = rc.document_id
                    WHERE rc.content_id = ANY(%s)
                      AND {primary_d}
                    """,
                    (content_ids,),
                )
            return [dict(row) for row in cur.fetchall()]

    @staticmethod
    def _tokens(question: str) -> list[str]:
        tokens = re.findall(r"[A-Za-z0-9_\-]{2,}|[\u3400-\u9fff]{2,}|[\u3040-\u30ff]{2,}", question)
        stop = {"what", "which", "where", "when", "how", "the", "and", "with", "this", "that", "什么", "哪些", "怎么", "如何", "是否", "可以", "里面", "关于"}
        result: list[str] = []
        for token in tokens:
            cleaned = token.strip()
            if cleaned.lower() in stop or cleaned in stop:
                continue
            if cleaned not in result:
                result.append(cleaned)
            if len(result) >= 5:
                break
        return result

    def _sql_exact_search(self, question: str, scopes: Iterable[str]) -> list[EvidenceItem]:
        tokens = self._tokens(question)
        if not tokens:
            return []
        result: list[EvidenceItem] = []
        successful_scopes = 0
        failed_scopes = 0
        for scope in scopes:
            if scope == "QABUG":
                continue
            clauses: list[str] = []
            params: list[Any] = []
            for token in tokens:
                clauses.append(
                    "(rc.content ILIKE %s OR rc.specification_name ILIKE %s OR rc.specification_code ILIKE %s OR rc.section_title ILIKE %s)"
                )
                value = f"%{token}%"
                params.extend([value, value, value, value])
            sql = f"""
                SELECT rc.content_id, rc.document_id, rc.file_name, rc.project_code,
                       rc.specification_code, rc.specification_name, rc.specification_version,
                       rc.chapter_title, rc.section_id, rc.section_title, rc.page_number,
                       rc.chunk_index, rc.content, rc.content_hash
                FROM public.rag_current_chunks rc
                JOIN public.documents d ON d.document_id = rc.document_id
                WHERE LENGTH(BTRIM(rc.content)) > 0
                  AND {PrimarySpecificationDocumentPolicy.sql_primary_predicate("d")}
                  AND ({' OR '.join(clauses)})
                ORDER BY
                    CASE WHEN rc.specification_code = ANY(%s) THEN 0 ELSE 1 END,
                    rc.content_id
                LIMIT %s
            """
            try:
                with self._connect(scope) as conn, conn.cursor() as cur:
                    cur.execute(sql, tuple(params + [tokens, self.top_k]))
                    rows = [dict(row) for row in cur.fetchall()]
            except Exception:
                failed_scopes += 1
                LOGGER.debug("SQL exact retrieval unavailable for scope=%s", scope, exc_info=True)
                continue
            successful_scopes += 1
            for row in rows:
                # SQL remains the primary authority, but a damaged or missing
                # digest is not a valid traceable engineering original.
                content = str(row.get("content") or "")
                digest = str(row.get("content_hash") or "").lower()
                if not content.strip() or not digest or hashlib.sha256(content.encode("utf-8")).hexdigest() != digest:
                    LOGGER.warning("Skipping corrupt SQL exact chunk scope=%s content_id=%s", scope, row.get("content_id"))
                    continue
                result.append(self._chunk_to_evidence(
                    scope, row, "SQL_EXACT", None,
                    {"content_hash": row.get("content_hash"), "evidence_role": "PRIMARY_CONTENT",
                     "source_authority": "POSTGRESQL"},
                ))
                if len(result) >= self.top_k:
                    return result
        if failed_scopes and not successful_scopes:
            raise RuntimeError("SQL exact search unavailable in every authorized specification scope")
        return result[: self.top_k]

    def _relation_registry_rows(self, scope: str, *, spec_codes: tuple[str, ...] = ()) -> list[dict[str, Any]]:
        """Read Engineering Relations for the preferred PRIMARY specification body.

        Legacy databases may have a CoverSheet as ``specifications.current_version_id``.
        v1.01 resolves the preferred body from immutable version/document rows and
        selects the latest READY extraction for that body.  This makes relation
        retrieval independent from a polluted historical current pointer.
        """
        rows: list[dict[str, Any]] = []
        primary_pd = PrimarySpecificationDocumentPolicy.sql_primary_predicate("pd")
        with self._connect(scope) as conn, conn.cursor() as cur:
            params: list[Any] = []
            filter_sql = ""
            if spec_codes:
                external = [f"SPEC:{code}" for code in spec_codes]
                key_patterns = [f"%:{code}" for code in spec_codes]
                filter_sql = (
                    " AND (src.spec_code = ANY(%s) OR tgt.spec_code = ANY(%s) "
                    "OR cat.target_external_key = ANY(%s) "
                    "OR cat.source_specification_key LIKE ANY(%s) "
                    "OR cat.target_specification_key LIKE ANY(%s))"
                )
                params.extend([list(spec_codes), list(spec_codes), external, key_patterns, key_patterns])

            cur.execute(
                f"""
                SELECT cat.relation_id, cat.source_specification_id, cat.source_specification_key,
                       COALESCE(NULLIF(pref.source_name, ''), cat.source_specification_name) AS source_specification_name,
                       src.spec_code AS source_spec_code,
                       cat.source_version_id, cat.source_version,
                       cat.target_specification_id, cat.target_specification_key,
                       cat.target_specification_name, tgt.spec_code AS target_spec_code,
                       cat.target_external_key, cat.relation_type, cat.extraction_method,
                       cat.confidence, cat.status, cat.is_explicit, cat.evidence_count,
                       cat.metadata, cat.created_at
                FROM public.engineering_relation_catalog cat
                JOIN public.specifications src ON src.id = cat.source_specification_id
                JOIN LATERAL (
                    SELECT psv.id AS preferred_version_id,
                           COALESCE(
                               NULLIF(pd.metadata ->> 'specification_name', ''),
                               NULLIF(pd.title, ''),
                               src.canonical_name
                           ) AS source_name
                    FROM public.specification_versions psv
                    LEFT JOIN public.documents pd ON pd.document_id = psv.document_id
                    WHERE psv.specification_id = src.id
                      AND psv.branch_name = 'MAIN'
                      AND {primary_pd}
                    ORDER BY
                        CASE WHEN psv.id = src.current_version_id THEN 0 ELSE 1 END,
                        psv.version_sort_key DESC NULLS LAST, psv.version_seq DESC, psv.id DESC
                    LIMIT 1
                ) pref ON pref.preferred_version_id = cat.source_version_id
                LEFT JOIN public.specifications tgt ON tgt.id = cat.target_specification_id
                WHERE cat.relation_type = 'REFERENCES'
                  AND cat.extraction_run_id = (
                      SELECT rer.id
                      FROM public.relation_extraction_runs rer
                      WHERE rer.version_id = cat.source_version_id
                        AND rer.status = 'READY'
                      ORDER BY rer.id DESC
                      LIMIT 1
                  )
                """ + filter_sql +
                " ORDER BY cat.confidence DESC NULLS LAST, cat.evidence_count DESC, cat.relation_id",
                tuple(params),
            )
            rows.extend(dict(row) for row in cur.fetchall())

            cur.execute("SELECT to_regclass('public.manual_spec_relations')")
            manual_ready = (cur.fetchone() or (None,))[0] is not None
            if manual_ready:
                manual_params: list[Any] = []
                manual_filter = ""
                if spec_codes:
                    external = [f"SPEC:{code}" for code in spec_codes]
                    key_patterns = [f"%:{code}" for code in spec_codes]
                    manual_filter = (
                        " AND (src.spec_code = ANY(%s) OR tgt.spec_code = ANY(%s) "
                        "OR mr.target_external_key = ANY(%s) "
                        "OR src.specification_key LIKE ANY(%s) "
                        "OR tgt.specification_key LIKE ANY(%s))"
                    )
                    manual_params.extend([list(spec_codes), list(spec_codes), external, key_patterns, key_patterns])
                cur.execute(
                    f"""
                    SELECT mr.id AS manual_id, mr.source_specification_id,
                           src.specification_key AS source_specification_key,
                           COALESCE(NULLIF(pref.source_name, ''), src.canonical_name) AS source_specification_name,
                           src.spec_code AS source_spec_code,
                           mr.source_version_id, sv.version_label AS source_version,
                           mr.target_specification_id,
                           tgt.specification_key AS target_specification_key,
                           tgt.canonical_name AS target_specification_name,
                           tgt.spec_code AS target_spec_code,
                           mr.target_external_key, mr.semantic_type, mr.status,
                           mr.confidence, mr.evidence_text, mr.metadata, mr.created_at
                    FROM public.manual_spec_relations mr
                    JOIN public.specifications src ON src.id = mr.source_specification_id
                    JOIN public.specification_versions sv ON sv.id = mr.source_version_id
                    JOIN LATERAL (
                        SELECT psv.id AS preferred_version_id,
                               COALESCE(
                                   NULLIF(pd.metadata ->> 'specification_name', ''),
                                   NULLIF(pd.title, ''),
                                   src.canonical_name
                               ) AS source_name
                        FROM public.specification_versions psv
                        LEFT JOIN public.documents pd ON pd.document_id = psv.document_id
                        WHERE psv.specification_id = src.id
                          AND psv.branch_name = 'MAIN'
                          AND {primary_pd}
                        ORDER BY
                            CASE WHEN psv.id = src.current_version_id THEN 0 ELSE 1 END,
                            psv.version_sort_key DESC NULLS LAST, psv.version_seq DESC, psv.id DESC
                        LIMIT 1
                    ) pref ON pref.preferred_version_id = mr.source_version_id
                    LEFT JOIN public.specifications tgt ON tgt.id = mr.target_specification_id
                    WHERE mr.is_active IS TRUE
                    """ + manual_filter + " ORDER BY mr.id",
                    tuple(manual_params),
                )
                for raw in cur.fetchall():
                    item = dict(raw)
                    metadata = dict(item.get("metadata") or {})
                    metadata.update({
                        "manual_relation": True,
                        "toyota_reference_kind": item.get("semantic_type") or "REFERENCES_SPEC",
                    })
                    rows.append({
                        "relation_id": -abs(int(item["manual_id"])),
                        "source_specification_id": item.get("source_specification_id"),
                        "source_specification_key": item.get("source_specification_key"),
                        "source_specification_name": item.get("source_specification_name"),
                        "source_spec_code": item.get("source_spec_code"),
                        "source_version_id": item.get("source_version_id"),
                        "source_version": item.get("source_version"),
                        "target_specification_id": item.get("target_specification_id"),
                        "target_specification_key": item.get("target_specification_key"),
                        "target_specification_name": item.get("target_specification_name"),
                        "target_spec_code": item.get("target_spec_code"),
                        "target_external_key": item.get("target_external_key"),
                        "relation_type": "REFERENCES",
                        "extraction_method": "MANUAL_EDITOR",
                        "confidence": item.get("confidence"),
                        "status": item.get("status") or "CONFIRMED",
                        "is_explicit": True,
                        "evidence_count": 1 if str(item.get("evidence_text") or "").strip() else 0,
                        "metadata": metadata,
                        "created_at": item.get("created_at"),
                    })
        return rows

    @staticmethod
    def _relation_endpoint_label(code: str, name: Any, fallback: Any = "") -> str:
        clean_name = str(name or "").strip()
        clean_code = str(code or "").strip()
        if clean_code and clean_name and clean_code.casefold() not in clean_name.casefold():
            return f"{clean_code} {clean_name}"
        return clean_name or clean_code or str(fallback or "Unresolved target").strip()

    def _deterministic_section_reference_search(
        self, question: str, scopes: Iterable[str], *, spec_codes: tuple[str, ...] = (),
    ) -> list[EvidenceItem]:
        """Read source-exact cross-spec section citations without review authority.

        Distinct from CONTENT_RELATION: this proves a source paragraph *refers
        to* a target section, never a requirement dependency or impact. A
        multi-unit section is shown as a section, not guessed as one target unit.
        """
        from app.engineering_intelligence.deterministic_read_models import (
            verify_requirement_span, verify_section_reference,
        )
        from app.engineering_intelligence.relations import extract_reference_anchors

        codes = list(spec_codes)
        if not codes:
            # Entity discovery through existing project-scoped exact/name lookup;
            # no broad unrestricted scan of every requirement in the library.
            found, _ = self._specification_lookup(question, tuple(scopes))
            codes = list(dict.fromkeys(str(x.specification_code) for x in found
                                        if x.specification_code))[:8]
        if not codes:
            return []
        primary_sd = PrimarySpecificationDocumentPolicy.sql_primary_predicate('sd')
        preferred_source = PrimarySpecificationDocumentPolicy.sql_preferred_body_version_id('ss')
        primary_td = PrimarySpecificationDocumentPolicy.sql_primary_predicate('td')
        preferred_target = PrimarySpecificationDocumentPolicy.sql_preferred_body_version_id('ts')
        result: list[EvidenceItem] = []
        seen: set[tuple[str, int, str, str]] = set()
        for scope in scopes:
            if scope == 'QABUG' or scope not in self.postgres_profiles:
                continue
            with self._connect(scope) as conn, conn.cursor() as cur:
                cur.execute("SET TRANSACTION READ ONLY")
                # Bounded, indexable source-code path and bounded inbound
                # target-code lexical path. No user input is SQL-interpolated.
                cur.execute(f"""
                    SELECT su.id AS source_unit_id, ss.spec_code AS source_code,
                           su.document_id AS source_document,
                           su.content_id AS source_content_id,
                           su.section_id AS source_section, su.page_number AS source_page,
                           su.start_offset AS source_start, su.end_offset AS source_end,
                           su.raw_text AS source_text, su.text_hash AS source_hash,
                           su.review_status AS source_review_status,
                           sc.content AS source_chunk, sv.version_label AS source_version,
                           sd.file_name AS source_file
                    FROM public.ei_requirement_units su
                    JOIN public.specifications ss ON ss.id=su.specification_id
                    JOIN public.specification_versions sv ON sv.id=su.version_id
                    JOIN public.documents sd ON sd.document_id=su.document_id
                    JOIN public.contents sc ON sc.id=su.content_id AND sc.document_id=su.document_id
                    WHERE ss.project_code=%s AND sv.id={preferred_source}
                      AND sv.specification_id=ss.id AND sv.document_id=su.document_id
                      AND sv.branch_name='MAIN' AND {primary_sd}
                      AND su.raw_text ~* '(refer|according|defined|specified|see|spec|参照|引用|依据|準拠|根据)'
                      AND (ss.spec_code=ANY(%s::TEXT[]) OR su.raw_text ILIKE ANY(%s::TEXT[]))
                    ORDER BY su.id DESC LIMIT 241
                """, (scope, codes, [f'%{code}%' for code in codes]))
                sources = [dict(x) for x in cur.fetchall()]
                # Don't misrepresent a truncated source set as a complete graph.
                if len(sources) > 240:
                    LOGGER.warning('Structural reference scan exceeded 240 source units for %s; refine query', scope)
                    continue
                for source in sources:
                    if source.get('source_review_status') == 'REJECTED' or not verify_requirement_span(source, 'source'):
                        continue
                    anchors = extract_reference_anchors(str(source['source_text']))
                    for anchor in anchors[:8]:
                        if source['source_code'] not in codes and anchor.specification_code not in codes:
                            continue
                        key = (scope, int(source['source_unit_id']), anchor.specification_code, anchor.section_id)
                        if key in seen:
                            continue
                        seen.add(key)
                        cur.execute(f"""
                            SELECT ts.spec_code AS target_code,
                                   tu.document_id AS target_document,
                                   tu.content_id AS target_content_id,
                                   tu.section_id AS target_section, tu.page_number AS target_page,
                                   tu.start_offset AS target_start, tu.end_offset AS target_end,
                                   tu.raw_text AS target_text, tu.text_hash AS target_hash,
                                   tu.review_status AS target_review_status,
                                   tc.content AS target_chunk, tv.version_label AS target_version,
                                   td.file_name AS target_file
                            FROM public.ei_requirement_units tu
                            JOIN public.specifications ts ON ts.id=tu.specification_id
                            JOIN public.specification_versions tv ON tv.id=tu.version_id
                            JOIN public.documents td ON td.document_id=tu.document_id
                            JOIN public.contents tc ON tc.id=tu.content_id AND tc.document_id=tu.document_id
                            WHERE ts.project_code=%s AND ts.spec_code=%s
                              AND tv.id={preferred_target} AND tv.specification_id=ts.id
                              AND tv.document_id=tu.document_id AND tv.branch_name='MAIN'
                              AND tu.section_id=%s AND {primary_td}
                            ORDER BY tu.id LIMIT 9
                        """, (scope, anchor.specification_code, anchor.section_id))
                        targets = [dict(x) for x in cur.fetchall()]
                        if not targets or len(targets) > 8:
                            continue  # missing/oversized section; never infer an endpoint
                        if not all(t.get('target_review_status') != 'REJECTED'
                                   and verify_section_reference(source, t) for t in targets):
                            continue  # reject entire section on *any* evidence drift
                        # Purely structural reference: multiple units still mean
                        # one section endpoint, NOT multiple requirement dependencies.
                        target = targets[0]
                        excerpts = [
                            f"§{t['target_section']} page {t['target_page'] or '-'} "
                            f"unit {t['target_content_id']} offsets {t['target_start']}:{t['target_end']} "
                            f"SHA256 {str(t['target_hash']).strip()}\n{str(t['target_text'])[:1600]}"
                            for t in targets
                        ]
                        content = (
                            f"[RULE-VERIFIED SECTION REFERENCE, NOT HUMAN-REVIEWED DEPENDENCY] "
                            f"{source['source_code']} v{source['source_version']} §{source['source_section']} "
                            f"→ {anchor.specification_code} v{target['target_version']} §{anchor.section_id}\n"
                            f"Exact reference: {anchor.raw_text}\n"
                            f"Source page {source['source_page'] or '-'}, offsets "
                            f"{source['source_start']}:{source['source_end']}, "
                            f"SHA256 {str(source['source_hash']).strip()}\n"
                            f"Source verbatim: {str(source['source_text'])[:1600]}\n"
                            f"Target section contains {len(targets)} verified requirement unit(s):\n"
                            + '\n'.join(excerpts)
                            + '\nLimit: explicit citation only; no proof of dependency, equivalence, '
                              'compliance, conflict or change impact.'
                        )
                        result.append(EvidenceItem(
                            evidence_id='', scope=scope, source_type='STRUCTURAL_REFERENCE',
                            content=content, score=0.82,
                            content_id=int(source['source_content_id']),
                            document_id=str(source['source_document']),
                            file_name=str(source['source_file']),
                            specification_code=str(source['source_code']),
                            specification_version=str(source['source_version']),
                            section_id=str(source['source_section']),
                            page_number=source['source_page'],
                            metadata={
                                'status': 'RULE_VERIFIED', 'authority': 'EXPLICIT_REFERENCE_ONLY',
                                'target_version_selection': 'PREFERRED_MAIN_SPEC_BODY',
                                'evidence_role': 'PRIMARY_CONTENT',
                                'interpretation_limit': 'SECTION_CITATION_NOT_ENGINEERING_DEPENDENCY',
                                'anchor': {'raw_text': anchor.raw_text,
                                           'start_offset': anchor.start_offset,
                                           'end_offset': anchor.end_offset},
                                'source': {'document_id': source['source_document'],
                                           'content_id': source['source_content_id'],
                                           'page': source['source_page'], 'section': source['source_section'],
                                           'text_hash': str(source['source_hash']).strip()},
                                'target': {'spec_code': anchor.specification_code,
                                           'version': target['target_version'],
                                           'section': anchor.section_id,
                                           'unit_count': len(targets),
                                           'units': [{'document_id': t['target_document'],
                                                      'content_id': t['target_content_id'],
                                                      'page': t['target_page'],
                                                      'text_hash': str(t['target_hash']).strip()}
                                                     for t in targets]},
                            },
                        ))
                        if len(result) >= min(20, max(self.top_k * 2, 8)):
                            return result
        return result

    def _deterministic_layout_relation_search(
        self, question: str, scopes: Iterable[str], *, spec_codes: tuple[str, ...] = (),
    ) -> list[EvidenceItem]:
        """Read verifiable within-body section/layout facts, never causal links.

        No new graph table, write access, reviewer status or unbounded scan.
        Index order means adjacent *extracted* units, not adjacent PDF blocks.
        """
        from app.engineering_intelligence.deterministic_read_models import (
            section_selector, structural_unit_pair,
        )
        codes = list(dict.fromkeys(spec_codes))[:8]
        if not codes:
            found, _ = self._specification_lookup(question, tuple(scopes))
            codes = list(dict.fromkeys(str(x.specification_code) for x in found
                                        if x.specification_code))[:8]
        if not codes:
            return []
        section = section_selector(question)
        direct_parent = section.rpartition('.')[0] if '.' in section else ''
        primary_sd = PrimarySpecificationDocumentPolicy.sql_primary_predicate('sd')
        preferred_body = PrimarySpecificationDocumentPolicy.sql_preferred_body_version_id('ss')
        result: list[EvidenceItem] = []
        for scope in scopes:
            if scope == 'QABUG' or scope not in self.postgres_profiles:
                continue
            with self._connect(scope) as conn, conn.cursor() as cur:
                cur.execute('SET TRANSACTION READ ONLY')
                for code in codes:
                    cur.execute(f"""
                        SELECT su.id AS unit_id, su.document_id, su.version_id,
                               su.content_id, su.section_id, su.page_number,
                               su.start_offset, su.end_offset, su.raw_text,
                               su.text_hash, su.review_status, sc.content AS chunk, ss.spec_code,
                               sv.version_label, sd.file_name,
                               next_indexed.next_unit_id
                        FROM public.ei_requirement_units su
                        JOIN public.specifications ss ON ss.id=su.specification_id
                        JOIN public.specification_versions sv ON sv.id=su.version_id
                        JOIN public.documents sd ON sd.document_id=su.document_id
                        JOIN public.contents sc ON sc.id=su.content_id AND sc.document_id=su.document_id
                        LEFT JOIN LATERAL (
                            SELECT following.id AS next_unit_id
                            FROM public.ei_requirement_units following
                            WHERE following.document_id=su.document_id
                              AND following.version_id=su.version_id
                              AND following.content_id=su.content_id
                              AND (following.start_offset, following.end_offset, following.id)
                                  > (su.start_offset, su.end_offset, su.id)
                            ORDER BY following.start_offset, following.end_offset, following.id
                            LIMIT 1
                        ) next_indexed ON TRUE
                        WHERE ss.project_code=%s AND ss.spec_code=%s
                          AND sv.id={preferred_body} AND sv.specification_id=ss.id
                          AND sv.document_id=su.document_id AND sv.branch_name='MAIN'
                          AND {primary_sd}
                          AND (%s='' OR su.section_id=%s OR su.section_id LIKE %s
                               OR (%s<>'' AND su.section_id=%s))
                        ORDER BY su.content_id, su.start_offset, su.id LIMIT 121
                    """, (scope, code, section, section, section + '.%', direct_parent, direct_parent))
                    rows = [dict(x) for x in cur.fetchall()]
                    if len(rows) > 120:
                        LOGGER.warning('Structural layout scan exceeded 120 units for %s/%s', scope, code)
                        continue  # incomplete input must not imply consecutive units
                    rows.sort(key=lambda r: (str(r.get('document_id')), int(r['content_id']),
                                             int(r['start_offset']), int(r['unit_id'])))
                    # The DB computes the next unit over *all* indexed units
                    # before the section filter.  Comparing adjacent filtered
                    # rows alone is incorrect when a §4.2 unit was removed.
                    adjacent_pairs = {
                        (int(left['unit_id']), int(right['unit_id']))
                        for left, right in zip(rows, rows[1:])
                        if (left['document_id'] == right['document_id']
                            and left['version_id'] == right['version_id']
                            and left['content_id'] == right['content_id']
                            and left.get('next_unit_id') is not None
                            and int(left['next_unit_id']) == int(right['unit_id']))
                    }
                    sections: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
                    for row in rows:
                        group = (str(row['document_id']), str(row['version_id']), str(row['section_id']))
                        sections.setdefault(group, []).append(row)

                    pairs: list[tuple[dict[str, Any], dict[str, Any], bool]] = []
                    for (doc, version, sec), members in sections.items():
                        # Same section and adjacent-indexed-unit are distinct
                        # structural facts, both shown without causal semantics.
                        if len(members) >= 2:
                            for a, b in zip(members, members[1:]):
                                adjacent = ((int(a['unit_id']), int(b['unit_id'])) in adjacent_pairs
                                            and int(a['end_offset']) <= int(b['start_offset']))
                                pairs.append((a, b, adjacent))
                        # Direct section-parent relation (4 -> 4.1, not 4 -> 4.1.2).
                        for (other_doc, other_version, child_section), children in sections.items():
                            if (other_doc == doc and other_version == version
                                    and child_section.startswith(sec + '.')
                                    and child_section.count('.') == sec.count('.') + 1):
                                pairs.append((members[0], children[0], False))
                    for a, b, consecutive in pairs:
                        if a.get('review_status') == 'REJECTED' or b.get('review_status') == 'REJECTED':
                            continue
                        source = {
                            'source_document': a['document_id'], 'source_version_id': a['version_id'],
                            'source_code': a['spec_code'], 'source_unit_id': a['unit_id'],
                            'source_content_id': a['content_id'], 'source_section': a['section_id'],
                            'source_page': a['page_number'], 'source_start': a['start_offset'],
                            'source_end': a['end_offset'], 'source_text': a['raw_text'],
                            'source_hash': a['text_hash'], 'source_chunk': a['chunk'],
                        }
                        target = {
                            'target_document': b['document_id'], 'target_version_id': b['version_id'],
                            'target_code': b['spec_code'], 'target_unit_id': b['unit_id'],
                            'target_content_id': b['content_id'], 'target_section': b['section_id'],
                            'target_page': b['page_number'], 'target_start': b['start_offset'],
                            'target_end': b['end_offset'], 'target_text': b['raw_text'],
                            'target_hash': b['text_hash'], 'target_chunk': b['chunk'],
                        }
                        kind = structural_unit_pair(source, target, consecutive_in_chunk=consecutive)
                        if not kind:
                            continue
                        meaning = {
                            'ADJACENT_INDEXED_UNITS': 'Consecutive extracted units in one content chunk',
                            'SAME_SECTION': 'Both units carry the same exact section ID',
                            'PARENT_CHILD_SECTION': 'Direct parent/child section-ID hierarchy',
                        }[kind]
                        content = (
                            f'[RULE-VERIFIED LAYOUT · {kind} · NOT DEPENDENCY] '
                            f"{code} v{a['version_label']}: {meaning}\n"
                            f"A §{a['section_id']} page {a['page_number'] or '-'} "
                            f"offset {a['start_offset']}:{a['end_offset']} SHA256 {a['text_hash']}: "
                            f"{str(a['raw_text'])[:1100]}\n"
                            f"B §{b['section_id']} page {b['page_number'] or '-'} "
                            f"offset {b['start_offset']}:{b['end_offset']} SHA256 {b['text_hash']}: "
                            f"{str(b['raw_text'])[:1100]}\n"
                            'Limit: document layout/index order only; no engineering dependency or change impact.'
                        )
                        result.append(EvidenceItem(
                            evidence_id='', scope=scope, source_type='STRUCTURAL_LAYOUT',
                            content=content, score=0.65,
                            content_id=int(a['content_id']), document_id=str(a['document_id']),
                            file_name=str(a['file_name']), specification_code=str(code),
                            specification_version=str(a['version_label']),
                            section_id=str(a['section_id']), page_number=a['page_number'],
                            metadata={
                                'status': 'RULE_VERIFIED', 'authority': 'LAYOUT_ONLY',
                                'relation_kind': kind, 'evidence_role': 'PRIMARY_CONTENT',
                                'interpretation_limit': 'DOCUMENT_STRUCTURE_NOT_DEPENDENCY',
                                'source': {'unit_id': a['unit_id'], 'document_id': a['document_id'],
                                           'content_id': a['content_id'], 'hash': a['text_hash'],
                                           'page': a['page_number'], 'section': a['section_id']},
                                'target': {'unit_id': b['unit_id'], 'document_id': b['document_id'],
                                           'content_id': b['content_id'], 'hash': b['text_hash'],
                                           'page': b['page_number'], 'section': b['section_id']},
                            },
                        ))
                        if len(result) >= min(16, max(6, self.top_k * 2)):
                            return result
        return result

    @staticmethod
    def _joint_reference_change_alerts(
        relation_items: list[EvidenceItem], version_items: list[EvidenceItem],
    ) -> list[EvidenceItem]:
        """Reference-target diff = revalidation prompt, NOT stale/impact claim."""
        from app.engineering_intelligence.deterministic_read_models import join_reference_and_diff
        alerts: list[EvidenceItem] = []
        seen: set[tuple[Any, ...]] = set()
        for ref in relation_items:
            for diff in version_items:
                md = join_reference_and_diff(ref, diff)
                if not md:
                    continue
                key = (ref.scope, ref.document_id, md['reference_source_hash'],
                       md['change_set_id'], md['change_id'])
                if key in seen:
                    continue
                seen.add(key)
                alerts.append(EvidenceItem(
                    evidence_id='', scope=ref.scope, source_type='REFERENCE_CHANGE_REVALIDATION',
                    score=0.60, content=(
                        '[JOINT EXTRACTIVE EVIDENCE · REVALIDATION REQUIRED, NOT INVALIDATED] '
                        f"Source {ref.specification_code} v{ref.specification_version} §{ref.section_id} "
                        f"references {md['target_spec_code']} §{md['target_section']}; "
                        f"that target section has persisted {md['from_version']} → {md['to_version']} "
                        f"change #{md['change_id']}.\n"
                        f"Reference original: {ref.content[:2000]}\n"
                        f"Persisted difference: {diff.content[:2400]}\n"
                        'Limit: no proof of broken dependency, functional impact or compliance.'
                    ), document_id=ref.document_id, specification_code=ref.specification_code,
                    specification_version=ref.specification_version,
                    section_id=ref.section_id, page_number=ref.page_number,
                    metadata={**md, 'evidence_role': 'PRIMARY_CONTENT',
                              'prior_relation_validity': 'UNDETERMINED',
                              'source_version_alignment': 'NOT_ESTABLISHED',
                              'human_revalidation_required': True},
                ))
                if len(alerts) >= 8:
                    return alerts
        return alerts

    def _chapter_version_diff_search(
        self, question: str, scopes: Iterable[str],
    ) -> list[EvidenceItem]:
        """Extract hash-verified section/keyword diffs from READY body-to-body cache.

        No speculative semantic equivalence, no on-demand mutation or diff
        generation. An absent READY comparison is *not* evidence of no change.
        """
        from app.engineering_intelligence.deterministic_read_models import (
            keyword_selector, matches_change_selector, section_selector,
            verify_persisted_diff,
        )
        section, keyword = section_selector(question), keyword_selector(question)
        if not section and not keyword:
            return []  # legacy history query already handles unfiltered requests
        # Strip explicitly selected section before looking for revision numerals.
        # Otherwise §4.1 would be misclassified as software version 4.1.
        without_section = re.sub(
            r'(?:section|sec\.?|§|章节|章節|第\s*)\s*[1-9]\d*(?:\.\d+){0,6}(?:\s*章)?',
            ' ', question, flags=re.I,
        )
        requested = self._version_labels_in_question(without_section)[:2]
        primary_fd = PrimarySpecificationDocumentPolicy.sql_primary_predicate('fd')
        primary_td = PrimarySpecificationDocumentPolicy.sql_primary_predicate('td')
        result: list[EvidenceItem] = []
        for scope in scopes:
            if scope == 'QABUG' or scope not in self.postgres_profiles:
                continue
            specs, _ = self._specification_lookup(question, (scope,))
            spec_ids = list(dict.fromkeys(
                int(x.metadata.get('specification_id') or 0) for x in specs
                if int(x.metadata.get('specification_id') or 0) > 0
            ))[:8]
            if not spec_ids:
                continue
            with self._connect(scope) as conn, conn.cursor() as cur:
                cur.execute('SET TRANSACTION READ ONLY')
                for spec_id in spec_ids:
                    cur.execute(f"""
                        SELECT cs.id AS change_set_id, sp.spec_code,
                               fv.version_label AS from_version, tv.version_label AS to_version,
                               fv.document_id AS from_document, tv.document_id AS to_document
                        FROM public.specification_change_sets cs
                        JOIN public.specifications sp ON sp.id=cs.specification_id
                        JOIN public.specification_versions fv ON fv.id=cs.from_version_id
                        JOIN public.specification_versions tv ON tv.id=cs.to_version_id
                        JOIN public.documents fd ON fd.document_id=fv.document_id
                        JOIN public.documents td ON td.document_id=tv.document_id
                        WHERE cs.specification_id=%s AND sp.project_code=%s
                          AND cs.status='READY' AND cs.branch_name='MAIN'
                          AND fv.specification_id=sp.id AND tv.specification_id=sp.id
                          AND fv.branch_name='MAIN' AND tv.branch_name='MAIN'
                          AND {primary_fd} AND {primary_td}
                        ORDER BY cs.updated_at DESC, cs.id DESC LIMIT 80
                    """, (spec_id, scope))
                    change_sets = [dict(x) for x in cur.fetchall()]
                    selected = []
                    for row in change_sets:
                        a, b = str(row['from_version']), str(row['to_version'])
                        if len(requested) == 2 and not (
                            (self._version_label_matches(a, requested[0]) and self._version_label_matches(b, requested[1]))
                            or (self._version_label_matches(a, requested[1]) and self._version_label_matches(b, requested[0]))
                        ):
                            continue
                        if len(requested) == 1 and not any(self._version_label_matches(v, requested[0]) for v in (a, b)):
                            continue
                        selected.append(row)
                    for change_set in selected[:2]:
                        cur.execute("""
                            SELECT id, change_type, chapter_id, chapter_title,
                                   section_id, section_title, old_text, old_hash,
                                   new_text, new_hash, old_page_number, new_page_number
                            FROM public.specification_changes
                            WHERE change_set_id=%s
                            ORDER BY id LIMIT 401
                        """, (change_set['change_set_id'],))
                        changes = [dict(x) for x in cur.fetchall()]
                        if len(changes) > 400:
                            LOGGER.warning('Version change set %s exceeds bounded scan', change_set['change_set_id'])
                            continue
                        for row in changes:
                            if not matches_change_selector(row, section, keyword) or not verify_persisted_diff(row):
                                continue
                            def excerpt(value: Any) -> str:
                                text = str(value or '')
                                return text if len(text) <= 1800 else text[:1800] + ' [TRUNCATED; consult persisted diff]'
                            content = (
                                f"[PERSISTED STRUCTURAL DIFF · BODY→BODY] "
                                f"{change_set['spec_code']} {change_set['from_version']} → {change_set['to_version']} "
                                f"§{row.get('section_id') or row.get('chapter_id') or '-'} {row['change_type']}\n"
                                f"OLD page {row.get('old_page_number') or '-'}: {excerpt(row.get('old_text'))}\n"
                                f"NEW page {row.get('new_page_number') or '-'}: {excerpt(row.get('new_text'))}\n"
                                'Limit: persisted section text difference; does not determine engineering impact.'
                            )
                            result.append(EvidenceItem(
                                evidence_id='', scope=scope, source_type='CHAPTER_VERSION_DIFF',
                                content=content, score=0.87,
                                document_id=str(change_set['to_document']),
                                specification_code=str(change_set['spec_code']),
                                specification_version=str(change_set['to_version']),
                                section_id=str(row.get('section_id') or row.get('chapter_id') or ''),
                                page_number=row.get('new_page_number') or row.get('old_page_number'),
                                metadata={
                                    'evidence_role': 'PRIMARY_CONTENT', 'change_type': row['change_type'],
                                    'from_version': change_set['from_version'],
                                    'to_version': change_set['to_version'],
                                    'from_document': change_set['from_document'],
                                    'to_document': change_set['to_document'],
                                    'change_id': row['id'], 'change_set_id': change_set['change_set_id'],
                                    'old_page': row['old_page_number'], 'new_page': row['new_page_number'],
                                    'old_hash': row['old_hash'], 'new_hash': row['new_hash'],
                                    'query_section': section, 'query_keyword': keyword,
                                    'interpretation_limit': 'PERSISTED_SECTION_DIFF_NOT_IMPACT_ANALYSIS',
                                },
                            ))
                            if len(result) >= min(20, max(self.top_k * 2, 8)):
                                return result
        return result

    def _requirement_version_search(
        self, scopes: Iterable[str], *, spec_codes: tuple[str, ...] = (),
    ) -> list[EvidenceItem]:
        """Return exact old/new source spans for persisted v7 revision mappings."""
        evidence = []
        if not spec_codes:
            return evidence  # require explicit root for version-level provenance
        for scope in scopes:
            if scope not in {"21MM", "24MM"}:
                continue
            with self._connect(scope) as conn, conn.cursor() as cur:
                cur.execute("SELECT to_regclass('public.ei_version_mappings') AS table_name")
                if not (cur.fetchone() or {}).get('table_name'):
                    continue
                cur.execute(
                    """
                    SELECT vm.id, vm.change_type, vm.review_status, vm.similarity,
                           sp.spec_code, ov.version_label AS old_version, nv.version_label AS new_version,
                           ou.document_id AS old_doc,ou.content_id AS old_content_id,ou.section_id AS old_section,
                           ou.raw_text AS old_text,ou.start_offset AS old_offset,ou.page_number AS old_page,
                           nu.document_id AS new_doc,nu.content_id AS new_content_id,nu.section_id AS new_section,
                           nu.raw_text AS new_text,nu.start_offset AS new_offset,nu.page_number AS new_page
                    FROM public.ei_version_mappings vm
                    JOIN public.specification_versions ov ON ov.id=vm.source_version_id
                    JOIN public.specification_versions nv ON nv.id=vm.target_version_id
                    JOIN public.specifications sp ON sp.id=ov.specification_id
                    LEFT JOIN public.ei_requirement_units ou ON ou.id=vm.old_unit_id
                    LEFT JOIN public.ei_requirement_units nu ON nu.id=vm.new_unit_id
                    WHERE vm.change_type <> 'UNCHANGED'
                      AND (%s::TEXT[] IS NULL OR sp.spec_code = ANY(%s::TEXT[]))
                    ORDER BY vm.id DESC LIMIT %s
                    """, (list(spec_codes) if spec_codes else None,
                           list(spec_codes) if spec_codes else None,
                           min(20, max(self.top_k*2, 8))),
                )
                for item in cur.fetchall():
                    grade = str(item['review_status'])
                    label = 'REVIEW REQUIRED' if grade == 'REVIEW_REQUIRED' else grade
                    evidence.append(EvidenceItem(
                        evidence_id='', scope=scope, source_type='REQUIREMENT_VERSION_CHANGE',
                        score=0.8 if grade == 'DETERMINISTIC' else 0.5,
                        content=(
                            f"[{label}] {item['spec_code']} {item['old_version']} -> {item['new_version']} "
                            f"{item['change_type']} similarity={item['similarity']}\n"
                            f"Old §{item['old_section'] or '-'}: {str(item['old_text'] or '')[:1600]}\n"
                            f"New §{item['new_section'] or '-'}: {str(item['new_text'] or '')[:1600]}"
                        ),
                        document_id=str(item['new_doc'] or item['old_doc'] or ''),
                        content_id=item['new_content_id'] or item['old_content_id'],
                        specification_code=str(item['spec_code'] or ''),
                        specification_version=str(item['new_version']),
                        section_id=str(item['new_section'] or item['old_section'] or ''),
                        page_number=item['new_page'] or item['old_page'],
                        metadata={
                            'change_type':item['change_type'], 'review_status':grade,
                            'old_evidence':{'document_id':item['old_doc'], 'content_id':item['old_content_id'],
                                            'section_id':item['old_section'],'start_offset':item['old_offset']},
                            'new_evidence':{'document_id':item['new_doc'], 'content_id':item['new_content_id'],
                                            'section_id':item['new_section'],'start_offset':item['new_offset']},
                        },
                    ))
        return evidence

    def _bug_spec_candidate_search(self, question: str) -> list[EvidenceItem]:
        """Exact-key, version-bound, independently rehashed, display-only results.

        No cross-DB joins or table names from user input. The QA database
        records the approved mapping and snapshot, but the specification DB
        is re-read to exclude stale evidence and Coversheets at query time.
        """
        from app.engineering_intelligence.bug_spec_candidates import (
            SpecUnitSnapshot, source_digest,
        )

        match = re.search(r"(?<![A-Z0-9])(?!SPEC-)([A-Z][A-Z0-9]{1,19}-\d{1,10})(?![A-Z0-9])", question, re.I)
        if not match or 'QABUG' not in self.postgres_profiles:
            return []
        issue_key = match.group(1).upper()
        requested_codes = self._entity_spec_codes((), question)
        with self._connect('QABUG') as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT c.*, m.status AS mapping_status, m.issue_version_field,
                       m.software_version AS mapped_software_version,
                       m.model AS mapped_model, m.destination AS mapped_destination,
                       r.record_type, r.summary, r.description, r.affected_version,
                       r.software_version, r.model, r.destination,
                       b.phenomenon, b.repro_steps, b.expected_result, b.actual_result
                FROM public.qabug_spec_candidates c
                JOIN public.qabug_records r ON r.id=c.bug_record_id
                JOIN public.bug_details b ON b.record_id=r.id
                JOIN public.qabug_software_spec_versions m ON m.id=c.mapping_id
                WHERE c.issue_key=%s AND r.issue_key=%s AND r.record_type='BUG'
                  AND r.is_current AND r.is_queryable
                  AND c.status='UNREVIEWED' AND m.status='APPROVED'
                  AND c.spec_scope=m.spec_scope AND c.spec_code=m.spec_code
                  AND c.spec_version_id=m.spec_version_id
                  AND c.spec_version_label=m.spec_version_label
                  AND c.spec_document_id=m.spec_document_id
                  AND m.model=COALESCE(r.model,'')
                  AND m.destination=COALESCE(r.destination,'')
                  AND ((m.issue_version_field='affected_version'
                      AND m.software_version=r.affected_version)
                    OR (m.issue_version_field='software_version'
                      AND m.software_version=r.software_version))
                ORDER BY c.rank_score DESC, c.id LIMIT %s
            """, (issue_key, issue_key, min(12, max(4, self.top_k))))
            rows = [dict(r) for r in cur.fetchall()]
        results: list[EvidenceItem] = []
        primary = PrimarySpecificationDocumentPolicy.sql_primary_predicate('d')
        for row in rows:
            if (source_digest(row) != str(row['issue_snapshot_hash']).strip()
                    or (requested_codes and str(row['spec_code']).upper() not in requested_codes)
                    or row['spec_scope'] not in {'21MM','24MM','COMMON'}
                    or row['spec_scope'] not in self.postgres_profiles):
                continue
            # Validate identity, historical revision, source bytes & offsets
            # from the authoritative spec DB; reject cached row on any drift.
            try:
                with self._connect(row['spec_scope']) as conn, conn.cursor() as cur:
                    cur.execute(f"""
                        SELECT sp.spec_code, sv.id AS version_id, sv.version_label,
                               sv.document_id, d.file_name, d.metadata,
                               u.id AS unit_id, u.content_id, u.section_id,
                               u.page_number, u.start_offset, u.end_offset,
                               u.raw_text, u.text_hash, c.content AS chunk_text
                        FROM public.ei_requirement_units u
                        JOIN public.specification_versions sv
                          ON sv.id=u.version_id AND sv.specification_id=u.specification_id
                          AND sv.document_id=u.document_id
                        JOIN public.specifications sp ON sp.id=sv.specification_id
                        JOIN public.documents d ON d.document_id=sv.document_id
                        JOIN public.contents c ON c.id=u.content_id AND c.document_id=u.document_id
                        WHERE u.id=%s AND u.version_id=%s AND sp.spec_code=%s
                          AND sp.project_code=%s AND sv.version_label=%s
                          AND sv.document_id=%s AND {primary}
                    """, (row['spec_unit_id'], row['spec_version_id'], row['spec_code'],
                          row['spec_scope'], row['spec_version_label'],row['spec_document_id']))
                    current = cur.fetchone()
                if not current:
                    continue
                unit = SpecUnitSnapshot(
                    scope=row['spec_scope'], spec_code=current['spec_code'],
                    version_id=current['version_id'], version_label=current['version_label'],
                    document_id=current['document_id'],file_name=current['file_name'],
                    document_metadata=current['metadata'] or {}, unit_id=current['unit_id'],
                    content_id=current['content_id'],section_id=current['section_id'],
                    page_number=current['page_number'],start_offset=current['start_offset'],
                    end_offset=current['end_offset'],raw_text=current['raw_text'],
                    text_hash=current['text_hash'],chunk_text=current['chunk_text'])
                if (not unit.verify() or unit.text_hash.strip() != str(row['spec_text_hash']).strip()
                        or unit.raw_text != row['spec_raw_text']
                        or unit.file_name != row['spec_file_name']
                        or unit.content_id != row['spec_content_id']
                        or unit.start_offset != row['start_offset'] or unit.end_offset != row['end_offset']):
                    continue
            except Exception:
                LOGGER.warning('Historical BUG candidate %s source recheck failed',row['id'],exc_info=True)
                continue
            phenomenon = row.get('phenomenon') or row.get('actual_result') or row.get('description') or row.get('summary') or ''
            content = (
                f"[UNREVIEWED · DISPLAY ONLY · NOT ENGINEERING EVIDENCE] BUG {issue_key} "
                f"software={row['mapped_software_version']} "
                f"→ SPEC {unit.spec_code} v{unit.version_label} §{unit.section_id} "
                f"page={unit.page_number or '-'}\n"
                f"BUG phenomenon (verbatim): {str(phenomenon)[:1800]}\n"
                f"SPEC_BODY verbatim: {unit.raw_text[:1800]}\n"
                "Lexical retrieval only. No conformance, violation, causality, or "
                "normative engineering interpretation has been established. Verdict: 证据不足."
            )
            results.append(EvidenceItem(
                evidence_id=f"C{len(results)+1}", scope=unit.scope,source_type='BUG_SPEC_CANDIDATE',
                score=float(row['rank_score']),content=content,document_id=unit.document_id,
                file_name=unit.file_name,content_id=unit.content_id,
                specification_code=unit.spec_code,specification_version=unit.version_label,
                section_id=unit.section_id,page_number=unit.page_number,
                metadata={'non_authoritative_candidate':True,'status':'UNREVIEWED',
                          'issue_key':issue_key,'mapping_id':row['mapping_id'],
                          'candidate_id':row['id'],'verdict':'证据不足',
                          'evidence_role':'PRIMARY_CONTENT','spec_text_hash':unit.text_hash},
            ))
        return results

    def _content_relation_search(
        self, scopes: Iterable[str], *, spec_codes: tuple[str, ...] = (),
    ) -> list[EvidenceItem]:
        """Supply ONLY audited, current, primary-body, intact two-ended edges.

        Unreviewed proposals remain available in the operator review queue, NOT
        in model context.  The caller can discover configured engineering scopes
        without hard-coded 21MM/24MM restrictions.
        """
        import hashlib

        from app.engineering_intelligence.requirements import normalize_text

        if not spec_codes:
            return []  # explicitly bounded graph neighborhoods only
        primary_source = PrimarySpecificationDocumentPolicy.sql_primary_predicate("sd")
        primary_target = PrimarySpecificationDocumentPolicy.sql_primary_predicate("td")
        result: list[EvidenceItem] = []
        for scope in scopes:
            if scope not in self.postgres_profiles or scope.upper() in {"QABUG", "RAG_QABUG"}:
                continue
            with self._connect(scope) as conn, conn.cursor() as cur:
                cur.execute("""SELECT to_regclass('public.ei_content_relations') AS relation_table,
                    to_regprocedure('dic_review.apply_content_relation_review(bigint,text,text,text,text,text,text)')
                    AS authenticated_review_authority,
                    COALESCE((SELECT p.prosecdef
                           AND 'search_path=pg_catalog'=ANY(p.proconfig)
                           AND NOT owner.rolcanlogin AND NOT owner.rolsuper
                           AND NOT EXISTS (
                             SELECT 1 FROM pg_catalog.aclexplode(COALESCE(p.proacl,
                                       pg_catalog.acldefault('f',p.proowner))) acl
                             WHERE acl.grantee=0 AND acl.privilege_type='EXECUTE')
                        FROM pg_catalog.pg_proc p
                        JOIN pg_catalog.pg_roles owner ON owner.oid=p.proowner
                        WHERE p.oid=to_regprocedure('dic_review.apply_content_relation_review(bigint,text,text,text,text,text,text)')),false)
                    AS trusted_review_authority,
                    (SELECT count(*) FROM pg_catalog.pg_trigger t
                       JOIN pg_catalog.pg_class c ON c.oid=t.tgrelid
                       JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
                       WHERE n.nspname='public'
                         AND ((c.relname='ei_content_relations' AND t.tgname='dic_stage2_relation_guard')
                              OR (c.relname='ei_relation_review_audit' AND t.tgname='dic_stage2_audit_guard'))
                         AND t.tgenabled='O' AND NOT t.tgisinternal AND t.tgtype=31
                         AND ((c.relname='ei_content_relations' AND
                               t.tgfoid=to_regprocedure('dic_review.guard_relation_write()'))
                              OR (c.relname='ei_relation_review_audit' AND
                               t.tgfoid=to_regprocedure('dic_review.guard_audit_write()')))
                         ) AS active_review_guards""")
                exists = cur.fetchone()
                if not exists:
                    continue
                if isinstance(exists, dict):
                    guarded = bool(exists.get("relation_table") and exists.get("authenticated_review_authority")
                                   and exists.get("active_review_guards") == 2
                                   and exists.get("trusted_review_authority") is True)
                else:
                    guarded = len(exists) >= 4 and bool(exists[0] and exists[1]
                                                          and exists[2] is True and exists[3] == 2)
                if not guarded:
                    continue  # Fail closed until the v1.33 DB authority has been installed.
                cur.execute(
                    f"""
                    SELECT rel.id, rel.relation_type, rel.score, rel.status, rel.rationale,
                           src.spec_code AS source_code, tgt.spec_code AS target_code,
                           su.document_id AS source_document, su.content_id AS source_content_id,
                           su.section_id AS source_section, su.page_number AS source_page,
                           su.start_offset AS source_start, su.end_offset AS source_end,
                           su.raw_text AS source_text, su.text_hash AS source_hash,
                           sc.content AS source_chunk, sv.version_label AS source_version,
                           sd.file_name AS source_file_name,
                           tu.document_id AS target_document, tu.content_id AS target_content_id,
                           tu.section_id AS target_section, tu.page_number AS target_page,
                           tu.start_offset AS target_start, tu.end_offset AS target_end,
                           tu.raw_text AS target_text, tu.text_hash AS target_hash,
                           tc.content AS target_chunk, tv.version_label AS target_version,
                           td.file_name AS target_file_name,
                           audit.actor AS reviewed_by, audit.reason AS review_reason,
                           audit.identity_provenance AS review_identity_provenance
                    FROM public.ei_content_relations rel
                    JOIN public.ei_requirement_units su ON su.id=rel.source_unit_id
                    JOIN public.ei_requirement_units tu ON tu.id=rel.target_unit_id
                    JOIN public.specifications src ON src.id=su.specification_id
                    JOIN public.specifications tgt ON tgt.id=tu.specification_id
                    JOIN public.specification_versions sv ON sv.id=su.version_id
                    JOIN public.specification_versions tv ON tv.id=tu.version_id
                    JOIN public.documents sd ON sd.document_id=su.document_id
                    JOIN public.documents td ON td.document_id=tu.document_id
                    JOIN public.contents sc ON sc.id=su.content_id AND sc.document_id=su.document_id
                    JOIN public.contents tc ON tc.id=tu.content_id AND tc.document_id=tu.document_id
                    JOIN LATERAL (
                        SELECT a.actor, a.reason, a.new_status,
                               to_jsonb(a)->>'identity_provenance' AS identity_provenance
                        FROM public.ei_relation_review_audit a
                        WHERE a.candidate_id=rel.id ORDER BY a.id DESC LIMIT 1
                    ) audit ON audit.new_status='CONFIRMED' AND audit.identity_provenance='DB_SESSION_USER'
                    WHERE rel.status='CONFIRMED'
                      AND rel.reviewed_by=audit.actor
                      AND src.project_code=%s AND tgt.project_code=%s
                      AND su.version_id=src.current_version_id
                      AND tu.version_id=tgt.current_version_id
                      AND sv.specification_id=su.specification_id
                      AND tv.specification_id=tu.specification_id
                      AND sv.document_id=su.document_id
                      AND tv.document_id=tu.document_id
                      AND {primary_source} AND {primary_target}
                      AND (src.spec_code=ANY(%s::TEXT[]) OR tgt.spec_code=ANY(%s::TEXT[]))
                    ORDER BY rel.score DESC, rel.id
                    LIMIT %s
                    """,
                    (scope, scope, list(spec_codes), list(spec_codes),
                     min(30, max(self.top_k * 3, 8))),
                )
                for row in cur.fetchall():
                    if (row["status"] != "CONFIRMED"
                            or row.get("review_identity_provenance") != "DB_SESSION_USER"
                            or not str(row["reviewed_by"] or "").strip()):
                        LOGGER.warning("Content relation %s excluded: no valid confirmation", row["id"])
                        continue
                    # Independent runtime verification in addition to primary
                    # SQL predicates: the answer must cite the original spans.
                    valid = True
                    for side in ("source", "target"):
                        literal = str(row[f"{side}_text"] or "")
                        original = str(row[f"{side}_chunk"] or "")
                        left, right = int(row[f"{side}_start"]), int(row[f"{side}_end"])
                        expected_hash = str(row[f"{side}_hash"] or "").strip()
                        if (not (0 <= left < right <= len(original)) or
                                original[left:right] != literal or
                                hashlib.sha256(normalize_text(literal).encode("utf-8")).hexdigest() != expected_hash):
                            LOGGER.warning("Content relation %s excluded: %s evidence drift", row["id"], side)
                            valid = False
                            break
                    if not valid:
                        continue
                    # A graph edge labelled REFERENCES_SECTION must actually
                    # contain a same-clause source-exact reference to the
                    # currently selected target spec and section. Human audit
                    # cannot turn a mismatched anchor into engineering fact.
                    from app.engineering_intelligence.relations import find_reference_anchor
                    reference_anchor = None
                    if row["relation_type"] == "REFERENCES_SECTION":
                        reference_anchor = find_reference_anchor(
                            str(row["source_text"]), str(row["target_code"]),
                            str(row["target_section"]),
                        )
                        if reference_anchor is None:
                            LOGGER.warning("Content relation %s excluded: invalid explicit section anchor", row["id"])
                            continue
                    md = {
                        "status": "CONFIRMED", "relation_type": row["relation_type"],
                        "evidence_role": "PRIMARY_CONTENT",
                        "interpretation_limit": "Reference only; dependency/impact not proven by this edge",
                        "reference_anchor": (
                            {"raw_text": reference_anchor.raw_text,
                             "start_offset": reference_anchor.start_offset,
                             "end_offset": reference_anchor.end_offset}
                            if reference_anchor else None
                        ),
                        "reviewed_by": row["reviewed_by"], "review_reason": row["review_reason"],
                        "source": {
                            "document_id": row["source_document"], "content_id": row["source_content_id"],
                            "file_name": row["source_file_name"], "version": row["source_version"],
                            "section_id": row["source_section"], "page_number": row["source_page"],
                            "start_offset": row["source_start"], "end_offset": row["source_end"],
                            "text_hash": str(row["source_hash"]).strip(),
                        },
                        "target": {
                            "document_id": row["target_document"], "content_id": row["target_content_id"],
                            "file_name": row["target_file_name"], "version": row["target_version"],
                            "section_id": row["target_section"], "page_number": row["target_page"],
                            "start_offset": row["target_start"], "end_offset": row["target_end"],
                            "text_hash": str(row["target_hash"]).strip(),
                        },
                    }
                    # Prompt-context bounded display; exact entire original
                    # remains in the immutable SQL source and review workflow.
                    def excerpt(value: str) -> str:
                        return value if len(value) <= 1800 else value[:1800] + " [TRUNCATED — consult source offsets]"

                    content = (
                        f"[HUMAN REVIEWED · CONFIRMED] {row['source_code']} v{row['source_version']} "
                        f"§{row['source_section']} → {row['target_code']} v{row['target_version']} "
                        f"§{row['target_section']} ({row['relation_type']})\n"
                        f"Source verbatim: {excerpt(str(row['source_text']))}\n"
                        f"Target verbatim: {excerpt(str(row['target_text']))}\n"
                        f"Extraction basis: {row['rationale']}\n"
                        f"Reference anchor: {reference_anchor.raw_text if reference_anchor else 'not extracted'}\n"
                        "Engineering interpretation: an explicit section reference alone does NOT prove "
                        "functional dependency, causality, conflict, or change impact.\n"
                        f"Review reason: {row['review_reason']}"
                    )
                    result.append(EvidenceItem(
                        evidence_id="", scope=scope, source_type="CONTENT_RELATION",
                        content=content, score=float(row["score"]),
                        content_id=int(row["source_content_id"]),
                        document_id=str(row["source_document"]),
                        file_name=str(row["source_file_name"] or ""),
                        specification_code=str(row["source_code"] or ""),
                        specification_version=str(row["source_version"] or ""),
                        section_id=str(row["source_section"] or ""),
                        page_number=row["source_page"],
                        relation_id=int(row["id"]), metadata=md,
                    ))
        return result

    def _relation_search(
        self,
        question: str,
        scopes: Iterable[str],
        *,
        entity_codes: tuple[str, ...] = (),
    ) -> list[EvidenceItem]:
        """Retrieve the actual Engineering Relations graph neighborhood.

        Exact Specification Nos. are resolved as graph roots and both outgoing
        and incoming current edges are returned in one aggregate evidence block,
        preserving CONFIRMED/UNRESOLVED/AMBIGUOUS status.  This prevents generic
        token matches from outranking the graph that operators see in the UI.
        """
        tokens = self._tokens(question)
        codes = tuple(dict.fromkeys((*entity_codes, *self._entity_spec_codes((), question))))[:8]
        result: list[EvidenceItem] = []
        for scope in scopes:
            try:
                rows = self._relation_registry_rows(scope, spec_codes=codes)
            except Exception:
                LOGGER.debug("Relation graph retrieval skipped for scope=%s", scope, exc_info=True)
                continue
            # Use exactly the same primary-reference policy as the visual graph.
            primary_rows = [row for row in rows if EngineeringRelationGraphService.is_primary_reference_row(row)]

            if codes:
                for root_code in codes:
                    edges: list[tuple[str, dict[str, Any], str, str]] = []
                    for row in primary_rows:
                        source_code = EngineeringRelationGraphService._row_source_code(row)
                        target_code = EngineeringRelationGraphService._row_target_code(row)
                        if source_code == root_code:
                            edges.append(("OUTGOING", row, source_code, target_code))
                        elif target_code == root_code:
                            edges.append(("INCOMING", row, source_code, target_code))
                    if not edges:
                        continue
                    lines = [f"Engineering relation graph neighborhood for Specification {root_code} (scope={scope}, direction=BOTH):"]
                    relation_ids: list[int] = []
                    confirmed = unresolved = ambiguous = 0
                    root_name = ""
                    for direction, row, source_code, target_code in edges[:40]:
                        relation_ids.append(int(row.get("relation_id") or 0))
                        status = str(row.get("status") or "UNRESOLVED").upper()
                        confirmed += status == "CONFIRMED"
                        unresolved += status == "UNRESOLVED"
                        ambiguous += status == "AMBIGUOUS"
                        source_label = self._relation_endpoint_label(source_code, row.get("source_specification_name"), row.get("source_specification_key"))
                        target_label = self._relation_endpoint_label(target_code, row.get("target_specification_name"), row.get("target_external_key"))
                        if source_code == root_code:
                            root_name = str(row.get("source_specification_name") or root_name)
                        if target_code == root_code and not root_name:
                            root_name = str(row.get("target_specification_name") or root_name)
                        semantic = EngineeringRelationGraphService.semantic_type(row)
                        manual = " MANUAL" if dict(row.get("metadata") or {}).get("manual_relation") else ""
                        lines.append(
                            f"- {direction}: {source_label} --{row.get('relation_type') or 'REFERENCES'}/{semantic}--> {target_label} "
                            f"[{status}{manual}] confidence={row.get('confidence')} evidence={row.get('evidence_count') or 0} relation_id={row.get('relation_id')}"
                        )
                    lines.append(f"Graph totals for root {root_code}: edges={len(edges)}, confirmed={confirmed}, unresolved={unresolved}, ambiguous={ambiguous}.")
                    result.append(EvidenceItem(
                        evidence_id="", scope=scope, source_type="RELATION_GRAPH",
                        content="\n".join(lines), score=1.0,
                        specification_code=root_code, specification_name=root_name,
                        metadata={
                            "root_spec_code": root_code,
                            "direction": "BOTH",
                            "edge_count": len(edges),
                            "confirmed_count": confirmed,
                            "unresolved_count": unresolved,
                            "ambiguous_count": ambiguous,
                            "relation_ids": relation_ids,
                            "graph_policy": "EngineeringRelationGraphService.is_primary_reference_row",
                        },
                    ))
                continue

            # No exact root: retain a conservative graph-aware fuzzy fallback.
            lowered_tokens = [token.casefold() for token in tokens if token]
            candidates: list[dict[str, Any]] = []
            for row in primary_rows:
                haystack = " ".join(str(row.get(key) or "") for key in (
                    "source_specification_key", "source_specification_name", "source_spec_code",
                    "target_specification_key", "target_specification_name", "target_spec_code", "target_external_key",
                )).casefold()
                if lowered_tokens and not any(token in haystack for token in lowered_tokens):
                    continue
                candidates.append(row)
            candidates.sort(key=lambda row: (float(row.get("confidence") or 0.0), int(row.get("evidence_count") or 0)), reverse=True)
            for row in candidates[: max(1, min(self.top_k, 10))]:
                source_code = EngineeringRelationGraphService._row_source_code(row)
                target_code = EngineeringRelationGraphService._row_target_code(row)
                source = self._relation_endpoint_label(source_code, row.get("source_specification_name"), row.get("source_specification_key"))
                target = self._relation_endpoint_label(target_code, row.get("target_specification_name"), row.get("target_external_key"))
                content = (
                    f"Engineering graph edge: {source} → {target}. Status={row.get('status') or '-'}, "
                    f"semantic={EngineeringRelationGraphService.semantic_type(row)}, method={row.get('extraction_method') or '-'}, "
                    f"confidence={row.get('confidence')}, evidence_count={row.get('evidence_count') or 0}."
                )
                result.append(EvidenceItem(
                    evidence_id="", scope=scope, source_type="RELATION", content=content,
                    score=float(row.get("confidence") or 0.0), specification_code=source_code,
                    specification_name=str(row.get("source_specification_name") or ""),
                    specification_version=str(row.get("source_version") or ""),
                    relation_id=int(row["relation_id"]) if row.get("relation_id") is not None else None,
                    metadata=row,
                ))
        return result[: max(self.top_k * 2, 20)]

    @staticmethod
    def _chunk_to_evidence(scope: str, row: dict[str, Any], source_type: str, score: float | None, metadata: dict[str, Any]) -> EvidenceItem:
        return EvidenceItem(
            evidence_id="",
            scope=scope,
            source_type=source_type,
            content=str(row.get("content") or ""),
            content_id=int(row["content_id"]) if row.get("content_id") is not None else None,
            score=score,
            document_id=str(row.get("document_id") or ""),
            file_name=str(row.get("file_name") or ""),
            specification_code=str(row.get("specification_code") or ""),
            specification_name=str(row.get("specification_name") or ""),
            specification_version=str(row.get("specification_version") or ""),
            chapter_title=str(row.get("chapter_title") or ""),
            section_id=str(row.get("section_id") or ""),
            section_title=str(row.get("section_title") or ""),
            page_number=int(row["page_number"]) if row.get("page_number") is not None else None,
            metadata=metadata,
        )
