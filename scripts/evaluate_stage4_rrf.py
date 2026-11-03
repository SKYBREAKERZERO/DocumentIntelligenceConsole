"""Offline RRF baseline evaluation; no DB/model/network access, no index writes.

Golden JSONL rows contain query_id, relevant, sql, dense and optional sparse arrays. Each
result item must provide scope, document_id, specification_version, content_id,
file_name, content. Reviewer labels must be created from genuine documents;
this script cannot label a synthetic corpus as production acceptance.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Supports `python scripts/evaluate_stage4_rrf.py` from release root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ai.hybrid_evaluation import aggregate, evaluate_one
from app.ai.hybrid_retrieval import _identity, fuse_spec_rrf
from app.ai.retrieval import EvidenceItem


def _item(record: object) -> EvidenceItem:
    if not isinstance(record, dict):
        raise ValueError('Golden item must be an object')
    required = ('scope', 'document_id', 'specification_version', 'content_id', 'file_name', 'content')
    if any(field not in record for field in required):
        raise ValueError('Golden item missing identity, filename, or original text')
    if not isinstance(record['content_id'], int) or isinstance(record['content_id'], bool) or record['content_id'] < 1:
        raise ValueError('content_id must be a positive integer')
    if not all(isinstance(record[field], str) for field in required if field != 'content_id'):
        raise ValueError('Golden item contains invalid string identity')
    if not record['document_id'].strip() or not record['content'].strip():
        raise ValueError('Golden item must contain source identity and nonempty text')
    return EvidenceItem(
        evidence_id='', scope=record['scope'], source_type='GOLDEN',
        content=record['content'], content_id=record['content_id'],
        document_id=record['document_id'], file_name=record['file_name'],
        specification_version=record['specification_version'],
        metadata=dict(record.get('metadata') or {}),
    )


def _key(item: EvidenceItem) -> tuple[str, str, str, int]:
    assert item.content_id is not None
    return item.scope, item.document_id, item.specification_version, item.content_id


def evaluate(path: Path, *, k: int = 10) -> dict[str, object]:
    if not 1 <= k <= 100:
        raise ValueError('k must be between 1 and 100')
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError('Golden file must be a bounded non-symlink regular file')
    by_method = {'sql': [], 'dense': [], 'sparse': [], 'rrf': [], 'rrf_three_way': []}
    ids: set[str] = set()
    sparse_covered = 0
    for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict) or not all(key in row for key in ('query_id', 'relevant', 'sql', 'dense')):
            raise ValueError(f'Line {number}: missing golden fields')
        query_id = str(row['query_id']).strip()
        if not query_id or query_id in ids:
            raise ValueError(f'Line {number}: blank or duplicate query_id')
        ids.add(query_id)
        if len(ids) > 1000:
            raise ValueError('Golden evaluation exceeds 1000 labeled questions')
        if any(not isinstance(row[key], list) for key in ('sql', 'dense', 'relevant')) or not isinstance(row.get('sparse', []), list):
            raise ValueError(f'Line {number}: candidate lists must be arrays')
        if any(len(row[key]) > 500 for key in ('sql', 'dense', 'relevant')) or len(row.get('sparse', [])) > 500:
            raise ValueError(f'Line {number}: too many candidate records')
        sql, dense, relevant = [[_item(entry) for entry in row[key]] for key in ('sql', 'dense', 'relevant')]
        sparse = [_item(entry) for entry in row.get('sparse', [])]
        if sparse:
            sparse_covered += 1
        if any(_identity(item) is None for item in relevant):
            raise ValueError(f'Line {number}: relevance labels must be identified SPEC_BODY chunks')
        # Match runtime source-policy filtration for fair per-source baselines.
        sql = [item for item in sql if _identity(item) is not None]
        dense = [item for item in dense if _identity(item) is not None]
        sparse = [item for item in sparse if _identity(item) is not None]
        relevant_keys = set(map(_key, relevant))
        if not relevant_keys:
            raise ValueError(f'Line {number}: relevance labels are mandatory')
        for name, items in (('sql', sql), ('dense', dense), ('sparse', sparse),
                            ('rrf', fuse_spec_rrf(sql, dense)),
                            ('rrf_three_way', fuse_spec_rrf(sql, dense, sparse_hits=sparse))):
            by_method[name].append(evaluate_one(map(_key, items), relevant_keys, k=k))
    return {'schema': 'stage4-rrf-baseline-v2', 'offline_only': True, 'k': k,
            'sparse_coverage_queries': sparse_covered,
            'results': {name: aggregate(values) for name, values in by_method.items()}}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('golden_jsonl', type=Path)
    parser.add_argument('--k', type=int, default=10)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--ranker', choices=('rrf','rrf_three_way'), default='rrf',
                        help='Select which RRF method is subject to quality gates')
    parser.add_argument('--min-recall', type=float, default=None,
                        help='Optional 0..1 RRF Recall@k quality gate')
    parser.add_argument('--min-mrr', type=float, default=None,
                        help='Optional 0..1 RRF MRR@k quality gate')
    args = parser.parse_args()
    try:
        for threshold in (args.min_recall, args.min_mrr):
            if threshold is not None and not 0 <= threshold <= 1:
                raise ValueError('quality gates must be within 0..1')
        result = evaluate(args.golden_jsonl, k=args.k)
        if (args.ranker == 'rrf_three_way'
                and result['sparse_coverage_queries'] != result['results']['rrf_three_way']['queries']):
            raise ValueError('Three-way gate requires sparse hits for every labeled query')
        rrf = result['results'][args.ranker]
        result['quality_gate_ranker'] = args.ranker
        passed = (args.min_recall is None or rrf['recall_at_k'] >= args.min_recall) and (
            args.min_mrr is None or rrf['mrr_at_k'] >= args.min_mrr)
        result['quality_gate'] = 'PASS' if passed else 'FAIL'
        output = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
        if args.output:
            args.output.write_text(output, encoding='utf-8')
        print(output, end='')
        return 0 if passed else 2
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        print(f'BLOCKED: {type(exc).__name__}: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
