"""Deterministic offline Stage 4 ranking metrics over labeled, explicit corpus keys."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


Key = tuple[str, str, str, int]


@dataclass(frozen=True)
class QueryMetrics:
    recall_at_k: float
    precision_at_k: float
    reciprocal_rank_at_k: float


def evaluate_one(ranked: Iterable[Key], relevant: set[Key], *, k: int) -> QueryMetrics:
    if not relevant or k < 1:
        raise ValueError("Need a nonempty human-labeled relevance set and positive k")
    seen: set[Key] = set()
    picks: list[Key] = []
    for key in ranked:
        if key in seen:
            continue
        seen.add(key)
        picks.append(key)
        if len(picks) >= k:
            break
    match = sum(key in relevant for key in picks)
    first = next((i for i, key in enumerate(picks, 1) if key in relevant), None)
    return QueryMetrics(match / len(relevant), match / k, 1.0 / first if first else 0.0)


def aggregate(metrics: Iterable[QueryMetrics]) -> dict[str, float | int]:
    rows = list(metrics)
    if not rows:
        raise ValueError("Golden dataset must contain at least one labeled query")
    return {
        "queries": len(rows),
        "recall_at_k": round(sum(row.recall_at_k for row in rows) / len(rows), 6),
        "precision_at_k": round(sum(row.precision_at_k for row in rows) / len(rows), 6),
        "mrr_at_k": round(sum(row.reciprocal_rank_at_k for row in rows) / len(rows), 6),
    }
