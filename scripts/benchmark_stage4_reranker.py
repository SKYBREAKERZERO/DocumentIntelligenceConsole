"""Local Stage 4 Golden reranker benchmark with measured per-query CPU latency.

Runs a genuine local CrossEncoder if no test scorer is injected. No model
network downloads; no PostgreSQL/Qdrant writes. The time includes cold spawn
and model load for each request (current production architecture). Human
relevance labels are required; self-reported/fabricated scores are forbidden.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path
from typing import Callable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ai.hybrid_evaluation import aggregate, evaluate_one
from app.ai.hybrid_retrieval import _identity, fuse_spec_rrf
from app.ai.stage4_reranker import RerankConfig, rerank_spec_chunks
from scripts.evaluate_stage4_rrf import _item, _key


def percentile(values: list[float], p: float) -> float:
    if not values or not 0 <= p <= 100 or any(not math.isfinite(x) or x < 0 for x in values):
        raise ValueError('Invalid latency sample')
    ordered = sorted(values)
    n = (len(ordered) - 1) * p / 100
    lo, hi = math.floor(n), math.ceil(n)
    return round(ordered[lo] + (ordered[hi] - ordered[lo]) * (n - lo), 3)


def benchmark(
    path: Path, *, config: RerankConfig, k: int = 10,
    score_fn: Callable[[list[tuple[str, str]], RerankConfig], Sequence[float]] | None = None,
) -> dict[str, object]:
    config.validate()
    if not config.enabled:
        raise ValueError('Reranker benchmark requires opt-in enabled')
    if not 1 <= k <= 100 or path.is_symlink() or not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError('Invalid bounded Golden file or k')
    rrf_metrics = []
    reranked_metrics = []
    rrf_times: list[float] = []
    rerank_times: list[float] = []
    seen: set[str] = set()
    for row_number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict) or not all(key in row for key in ('query_id', 'query', 'relevant', 'sql', 'dense', 'sparse')):
            raise ValueError(f'Line {row_number}: missing mandatory three-way Golden fields')
        ident, question = row['query_id'], row['query']
        if not isinstance(ident, str) or not ident.strip() or ident in seen or len(seen) >= 1000:
            raise ValueError(f'Line {row_number}: duplicate/invalid query_id or size limit')
        seen.add(ident)
        if not isinstance(question, str) or not question.strip() or len(question) > 4000:
            raise ValueError(f'Line {row_number}: invalid question')
        if any(not isinstance(row[name], list) or len(row[name]) > 500 for name in ('relevant', 'sql', 'dense', 'sparse')):
            raise ValueError(f'Line {row_number}: unbounded candidates')
        relevant, sql, dense, sparse = [
            [_item(value) for value in row[name]]
            for name in ('relevant', 'sql', 'dense', 'sparse')
        ]
        if not relevant or any(_identity(item) is None for item in relevant):
            raise ValueError(f'Line {row_number}: missing labeled primary-body truth')
        if not sparse:
            raise ValueError(f'Line {row_number}: missing Sparse baseline')
        # Match the trusted-body policy and record *observed* execution times.
        t0 = time.perf_counter()
        fused = fuse_spec_rrf(sql, dense, sparse_hits=sparse)
        t1 = time.perf_counter()
        if len(fused) < 2:
            raise ValueError(f'Line {row_number}: at least two fused candidates required')
        reranked, status = rerank_spec_chunks(question, fused, config=config, score_fn=score_fn)
        t2 = time.perf_counter()
        if status.state != 'APPLIED':
            raise RuntimeError(f'Line {row_number}: CrossEncoder not applied ({status.state}); benchmark BLOCKED')
        truth = set(map(_key, relevant))
        rrf_metrics.append(evaluate_one(map(_key, fused), truth, k=k))
        reranked_metrics.append(evaluate_one(map(_key, reranked), truth, k=k))
        rrf_times.append((t1 - t0) * 1000)
        rerank_times.append((t2 - t0) * 1000)
    if not seen:
        raise ValueError('Empty Golden dataset')
    before, after = aggregate(rrf_metrics), aggregate(reranked_metrics)
    return {
        'schema': 'stage4-local-reranker-benchmark-v1',
        'scope': 'offline Golden replay; locally measured per-query wall clock; no live PG/Qdrant latency',
        'queries': len(seen), 'k': k,
        'rrf': before, 'reranked': after,
        'recall_delta': round(after['recall_at_k'] - before['recall_at_k'], 6),
        'mrr_delta': round(after['mrr_at_k'] - before['mrr_at_k'], 6),
        'rrf_latency_ms': {'p50': percentile(rrf_times, 50), 'p95': percentile(rrf_times, 95)},
        'reranked_latency_ms': {'p50': percentile(rerank_times, 50), 'p95': percentile(rerank_times, 95)},
        'reranker_state': 'APPLIED_FOR_ALL',
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('golden_jsonl', type=Path)
    parser.add_argument('--model-dir', required=True, help='Existing local CrossEncoder model directory')
    parser.add_argument('--k', type=int, default=10)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--min-recall-delta', type=float, default=None)
    parser.add_argument('--max-p95-ms', type=float, default=None)
    args = parser.parse_args()
    try:
        config = RerankConfig(enabled=True, model_dir=args.model_dir)
        result = benchmark(args.golden_jsonl, config=config, k=args.k)
        if args.min_recall_delta is not None and not -1 <= args.min_recall_delta <= 1:
            raise ValueError('Invalid minimum recall delta')
        if args.max_p95_ms is not None and (not math.isfinite(args.max_p95_ms) or args.max_p95_ms <= 0):
            raise ValueError('Invalid latency gate')
        passed = (args.min_recall_delta is None or result['recall_delta'] >= args.min_recall_delta) and (
            args.max_p95_ms is None or result['reranked_latency_ms']['p95'] <= args.max_p95_ms)
        result['quality_gate'] = 'PASS' if passed else 'FAIL'
        output = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
        if args.output:
            args.output.write_text(output, encoding='utf-8')
        print(output, end='')
        return 0 if passed else 2
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        print(f'BLOCKED: {type(exc).__name__}: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
