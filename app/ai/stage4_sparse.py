"""Optional BGE-M3 lexical sparse vectors; isolated from legacy unnamed dense collections.

No network or model loading at import.  The same tokenizer/model revision must
be used for both query and document vectors.  Scores are ranking signals only.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from threading import RLock
from typing import Any, Mapping, Sequence

SPARSE_NAME = "bge_m3_lexical_v1"
SPARSE_SUFFIX = "__bge_m3_sparse_v1"
MODEL_NAME = "BAAI/bge-m3"
MAX_SPARSE_TERMS = 2048


def sparse_collection(dense_collection: str) -> str:
    name = str(dense_collection or "").strip()
    if not name or not re.fullmatch(r"[\w\-]{1,180}", name, re.ASCII):
        raise ValueError("Invalid Qdrant source collection name")
    if len(name) + len(SPARSE_SUFFIX) > 255:
        raise ValueError("Sparse collection name too long")
    return name + SPARSE_SUFFIX


def canonical_sparse(weights: Mapping[Any, Any]) -> tuple[list[int], list[float]]:
    """Canonical, bounded Qdrant sparse payload from BGE-M3 token-ID weights."""
    if not isinstance(weights, Mapping) or not weights:
        raise ValueError("No sparse lexical weights")
    if len(weights) > MAX_SPARSE_TERMS:
        raise ValueError("Sparse vector exceeds term bound")
    converted: dict[int, float] = {}
    for raw_id, raw_weight in weights.items():
        if isinstance(raw_id, bool) or not re.fullmatch(r"\d+", str(raw_id)):
            raise ValueError("Lexical weight key must be a tokenizer ID")
        idx = int(raw_id)
        if not 0 <= idx <= 4294967295 or isinstance(raw_weight, bool):
            raise ValueError("Invalid sparse index/weight")
        weight = float(raw_weight)
        if not math.isfinite(weight) or weight <= 0:
            raise ValueError("Sparse weights must be finite and positive")
        if idx in converted:
            raise ValueError("Duplicate lexical token ID")
        converted[idx] = weight
    ordered = sorted(converted.items())
    return [k for k, _ in ordered], [v for _, v in ordered]


@dataclass(frozen=True)
class SparseEncoding:
    indices: list[int]
    values: list[float]


class BGEM3SparseEncoder:
    """True BGE-M3 learned lexical weights (not BM25 or a hashed approximation)."""

    def __init__(self, model_name: str = MODEL_NAME, *, batch_size: int = 8) -> None:
        if model_name != MODEL_NAME or not 1 <= batch_size <= 64:
            raise ValueError("Sparse model must match pinned BGE-M3 index contract")
        self.batch_size = batch_size
        self._model: Any = None
        self._lock = RLock()

    def encode(self, texts: Sequence[str]) -> list[SparseEncoding]:
        texts = list(texts)
        if not texts or len(texts) > 64 or any(not str(t).strip() or len(t) > 32768 for t in texts):
            raise ValueError("Sparse request must contain 1..64 bounded, nonempty texts")
        with self._lock:
            if self._model is None:
                try:
                    from FlagEmbedding import BGEM3FlagModel
                except ImportError as exc:
                    raise RuntimeError("Install optional FlagEmbedding for native BGE-M3 Sparse") from exc
                self._model = BGEM3FlagModel(MODEL_NAME, use_fp16=False, devices="cpu")
            raw = self._model.encode(
                texts, batch_size=self.batch_size, return_dense=False,
                return_sparse=True, return_colbert_vecs=False,
            )
        weights = raw.get("lexical_weights") if isinstance(raw, dict) else None
        if weights is None or len(weights) != len(texts):
            raise RuntimeError("BGE-M3 did not return one sparse vector per input")
        return [SparseEncoding(*canonical_sparse(item)) for item in weights]


def ensure_sparse_collection(client: Any, name: str) -> None:
    """Idempotent sidecar schema creation; never mutates the dense collection."""
    from qdrant_client.models import SparseVectorParams, SparseIndexParams
    if client.collection_exists(collection_name=name):
        info = client.get_collection(name)
        vectors = getattr(info.config.params, "vectors", None)
        sparse = getattr(info.config.params, "sparse_vectors", None) or {}
        if vectors or SPARSE_NAME not in sparse or len(sparse) != 1:
            raise RuntimeError("Existing collection is not an isolated BGE-M3 sparse sidecar")
        return
    client.create_collection(
        collection_name=name, vectors_config={},
        sparse_vectors_config={SPARSE_NAME: SparseVectorParams(index=SparseIndexParams(on_disk=False))},
    )
