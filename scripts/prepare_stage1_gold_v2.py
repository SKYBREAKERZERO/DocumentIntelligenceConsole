"""Prepare an UNLABELLED local human annotation worksheet from authorized text.

NO model predictions are used as labels. This script never reads PostgreSQL,
never sends network requests, and refuses to overwrite existing files.
It creates a private local copy of the text only after explicit operator consent.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def build_unlabelled_gold(source: dict, *, max_cases: int = 2000,
                          max_chars: int = 5_000_000) -> dict:
    if source.get('schema') != 'stage1-source-items-v1' or not isinstance(source.get('cases'), list):
        raise ValueError('Source must use stage1-source-items-v1 with cases')
    cases = source['cases']
    if not cases or len(cases) > max_cases:
        raise ValueError('Nonempty bounded source items required')
    total = 0
    used = set()
    output = []
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError('Invalid source case')
        text = case.get('text')
        doc = case.get('document_id')
        cid = case.get('content_id')
        sec = case.get('section_id')
        if (not isinstance(text, str) or not text or not isinstance(doc, str) or not doc
                or type(cid) is not int or cid < 1 or not isinstance(sec, str) or not sec):
            raise ValueError('Original source identity and nonempty text required')
        total += len(text)
        if total > max_chars:
            raise ValueError('Source text character budget exceeded')
        identity = (doc, cid)
        if identity in used:
            raise ValueError('Duplicate document/content source identity')
        used.add(identity)
        digest = hashlib.sha256(text.encode('utf-8', 'surrogatepass')).hexdigest()
        if 'source_sha256' in case and case['source_sha256'] != digest:
            raise ValueError('Original source text SHA-256 drift')
        output.append({
            'document_id': doc, 'content_id': cid, 'section_id': sec,
            'text': text, 'source_sha256': digest,
            'expected_requirement_spans': [], 'scope_annotations': [],
        })
    return {'schema': 'stage1-gold-v2', 'cases': output}


def main(argv=None):
    p = argparse.ArgumentParser(description='Prepare unlabelled, private Stage 1 gold-v2 annotation set (offline)')
    p.add_argument('--source-json', required=True, type=Path)
    p.add_argument('--out', required=True, type=Path)
    p.add_argument('--confirm-local-sensitive-copy', action='store_true',
                   help='Explicitly confirm that output contains the original proprietary source text')
    args = p.parse_args(argv)
    if not args.confirm_local_sensitive_copy:
        p.error('Explicit --confirm-local-sensitive-copy required: output contains raw document text')
    if not args.source_json.is_file() or args.source_json.stat().st_size > 10_000_000:
        p.error('Missing or oversized source JSON')
    source = json.loads(args.source_json.read_text(encoding='utf-8'))
    unlabelled = build_unlabelled_gold(source)
    payload = (json.dumps(unlabelled, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    # Exclusive create; no silent overwrite of a manually labelled gold set.
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    fd = os.open(args.out, flags, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(payload)
    except BaseException:
        args.out.unlink(missing_ok=True)
        raise
    print(json.dumps({'status': 'UNLABELLED_REQUIRES_HUMAN_ANNOTATION',
                      'source_items': len(unlabelled['cases']),
                      'output_contains_proprietary_text': True,
                      'no_accuracy_computed': True}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
