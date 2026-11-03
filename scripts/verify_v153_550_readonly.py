"""Local v1.53 acceptance: physical SPEC_BODY discovery vs verified numeric identity.

Read-only POST /api/v1/retrieval; no Ollama and no database writes.
No tokens or document content are printed. Output is ASCII-safe JSON on CP932.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import urllib.request
import urllib.error

EXPECTED_FILE = 'Bluetooth Audio function spec v2.52.pdf'
EXPECTED_ID = '21MM:UNSPECIFIED:NAME:a349727f3f5156c17204@v2.52'


def main() -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--base-url', default='http://127.0.0.1:18080')
    args = cli.parse_args()
    # Only loopback may be contacted: no external dependencies.
    if args.base_url.rstrip('/') not in ('http://127.0.0.1:18080', 'http://localhost:18080'):
        cli.error('Only the local loopback backend on port 18080 is allowed')
    token_path = Path(os.environ['LOCALAPPDATA']) / 'DocumentIntelligenceConsole' / 'local_api_token.txt'
    token = token_path.read_text(encoding='utf-8-sig').strip()
    cases = (
        ('spec_lookup', '550 BT式样书有吗'),
        ('spec_outline', '查一下21MM的550 BT式样书按目录总结'),
        ('spec_version_facts', '查询21MM 550 BT相邻两个本体版本之间的变更'),
    )
    failed = False
    for operation, question in cases:
        payload = json.dumps({'scope': '21MM', 'question': question, 'operation': operation},
                             ensure_ascii=False).encode('utf-8')
        req = urllib.request.Request(args.base_url.rstrip('/') + '/api/v1/retrieval',
            data=payload, headers={'Authorization': 'Bearer '+token,
                                   'Content-Type': 'application/json; charset=utf-8'}, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=12) as response:
                data = json.loads(response.read().decode('utf-8'))
        except (OSError, ValueError, urllib.error.HTTPError) as exc:
            print(json.dumps({'operation': operation, 'error': type(exc).__name__}, ensure_ascii=True))
            failed = True
            continue
        items = data.get('evidence', ())
        expected_type = {'spec_lookup': 'SPEC_IDENTITY_CANDIDATE',
                         'spec_outline': 'SPEC_OUTLINE_CANDIDATE',
                         'spec_version_facts': 'SPEC_VERSION_FACTS'}[operation]
        seen = [e for e in items if e.get('type') == expected_type]
        matched = [e for e in seen if e.get('document_id') == EXPECTED_ID
                   and e.get('file_name') == EXPECTED_FILE]
        # spec_version_facts deliberately does not make trusted version claims.
        if operation == 'spec_version_facts':
            valid = bool(seen) and all(
                len((e.get('metadata') or {}).get('versions') or ()) < 2
                and not (e.get('metadata') or {}).get('change_set_ready')
                for e in seen)
        else:
            valid = bool(matched) and data.get('strength') == 'LIMITED'
            if operation == 'spec_outline':
                valid = valid and bool((matched[0].get('metadata') or {}).get('outline'))
        print(json.dumps({
            'operation': operation, 'route': data.get('route'),
            'strength': data.get('strength'),
            'warnings': data.get('warnings'),
            'matching_physical_body': bool(matched),
            'outline_items': len((matched[0].get('metadata') or {}).get('outline', ())) if matched else 0,
            'non_empty_evidence': bool(items), 'acceptance_pass': valid,
        }, ensure_ascii=True))
        failed |= not valid
    return 2 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
