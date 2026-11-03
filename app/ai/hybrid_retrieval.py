"""Stage 4: bounded, provenance-preserving specification RRF.

This module optionally fuses SQL, Dense and separately indexed native BGE-M3
Sparse ranks; never confirms an engineering relation or an unreviewed candidate.
"""
from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import TYPE_CHECKING, Iterable

from app.storage.primary_document_policy import PrimarySpecificationDocumentPolicy

if TYPE_CHECKING:
    from app.ai.retrieval import EvidenceItem


def _identity(item: EvidenceItem) -> tuple[str, str, str, int] | None:
    if (item.scope == "QABUG" or item.content_id is None
            or not str(item.document_id).strip()):
        return None
    if not PrimarySpecificationDocumentPolicy.is_primary(
        file_name=item.file_name, metadata=item.metadata
    ):
        return None
    return (item.scope, item.document_id, item.specification_version, item.content_id)


def _fingerprint(item: EvidenceItem) -> str:
    return hashlib.sha256(item.content.encode("utf-8")).hexdigest()


def fuse_spec_rrf(
    sql_hits: Iterable[EvidenceItem],
    dense_hits: Iterable[EvidenceItem],
    *,
    sparse_hits: Iterable[EvidenceItem] = (),
    rrf_k: int = 60,
    max_candidates: int = 60,
) -> list[EvidenceItem]:
    """Fuse two or three independently ranked lists by scoped revision ID.

    The same content ID from another project/document/revision must NEVER join.
    If any vector disagrees about original text with SQL (or with the first
    vector when SQL is absent), the stale vote contributes no RRF score.
    Scores are *ranking only*, not confidence, corroboration, or source trust.
    """
    if not 1 <= rrf_k <= 1000 or not 1 <= max_candidates <= 500:
        raise ValueError("Invalid bounded RRF configuration")

    sources: dict[tuple[str, str, str, int], dict[str, tuple[int, EvidenceItem]]] = {}
    # Ranking position is the supplied rank, not the position after a filter;
    # discarded non-body or malformed candidates cannot raise lower hits' rank.
    for source, hits in (("POSTGRESQL", sql_hits), ("QDRANT_DENSE", dense_hits),
                         ("QDRANT_SPARSE", sparse_hits)):
        for rank, item in enumerate(hits, 1):
            if rank > max_candidates:
                break
            key = _identity(item)
            if key is None:
                continue
            group = sources.setdefault(key, {})
            if source not in group:
                group[source] = (rank, item)

    results: list[tuple[float, int, tuple[str, str, str, int], EvidenceItem]] = []
    for key, pair in sources.items():
        sql_entry = pair.get("POSTGRESQL")
        dense_entry = pair.get("QDRANT_DENSE")
        sparse_entry = pair.get("QDRANT_SPARSE")
        conflict = False
        # Compare both vectors to PostgreSQL when available. Otherwise compare
        # sparse to dense; they must never corroborate conflicting body text.
        authority = sql_entry or dense_entry or sparse_entry
        valid = {}
        if authority is not None:
            for name, entry in (("POSTGRESQL", sql_entry), ("QDRANT_DENSE", dense_entry),
                                ("QDRANT_SPARSE", sparse_entry)):
                if entry is None:
                    continue
                auth_item, other = authority[1], entry[1]
                auth_hash = str(auth_item.metadata.get("content_hash") or "").strip()
                other_hash = str(other.metadata.get("content_hash") or "").strip()
                if ((auth_hash and other_hash and auth_hash != other_hash)
                        or _fingerprint(auth_item) != _fingerprint(other)):
                    conflict = True
                    continue
                valid[name] = entry
        if not valid:
            continue
        score = sum(1.0 / (rrf_k + entry[0]) for entry in valid.values())
        representative = authority[1]
        md = dict(representative.metadata)
        md.update({
            "retrieval_sources": list(valid),
            "fusion_method": "RRF_V2" if "QDRANT_SPARSE" in valid else "RRF_V1",
            "rrf_k": rrf_k,
            "rrf_score": round(score, 9),
            "rrf_ranks": {name: entry[0] for name, entry in valid.items()},
            "evidence_role": "PRIMARY_CONTENT",
            "ranking_only": True,
        })
        if sql_entry:
            md["source_authority"] = "POSTGRESQL"
        if conflict:
            md["vector_source_mismatch"] = True
        # Preserve the original Dense similarity for audit, not as a verdict.
        if dense_entry:
            md["dense_score"] = dense_entry[1].score
        # RRF is deliberately not represented as a 0..1 engineering confidence.
        # Downstream EvidenceGovernor must not treat it as a fact-strength boost.
        fused = replace(representative, source_type="HYBRID_CHUNK", score=None, metadata=md)
        results.append((score, len(valid), key, fused))

    results.sort(key=lambda v: (-v[0], -v[1], v[2]))
    return [item for _, _, _, item in results[:max_candidates]]
