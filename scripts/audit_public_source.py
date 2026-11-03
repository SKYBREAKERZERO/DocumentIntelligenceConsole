"""Check a source snapshot for runtime state and common credential formats.

This is a packaging check, not a guarantee that every document is public.
Only filenames and finding types are printed, never matched secret values.
"""
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
SKIP = {'.git', '__pycache__', '.pytest_cache', '.venv', '.mypy_cache', '.ruff_cache'}
DENY_DIRS = {'local_backups', 'live_acceptance'}
DENY_FILES = {'local_api_token.txt', 'ai_config.json', 'console_profile.json', 'backend_spawn.json', '.env'}
DENY_SUFFIXES = {'.log', '.sqlite', '.sqlite3', '.db', '.private', '.gguf', '.safetensors', '.pt', '.pth'}
PATTERNS = {
    'private-key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'github-token': re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b'),
    'provider-key': re.compile(r'\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}\b'),
}


def main():
    findings = []
    count = 0
    for path in ROOT.rglob('*'):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT)
        if SKIP.intersection(rel.parts):
            continue
        count += 1
        if (rel.parts[0] in DENY_DIRS or path.name in DENY_FILES or
                path.suffix.lower() in DENY_SUFFIXES or
                (rel.parts[0] in {'input', 'output', 'logs'} and path.name != '.gitkeep')):
            findings.append((str(rel), 'runtime-or-private-state'))
        if path.stat().st_size > 10 * 1024 * 1024:
            findings.append((str(rel), 'oversized-artifact'))
        try:
            content = path.read_text(encoding='utf-8')
        except (UnicodeError, OSError):
            continue
        for label, pattern in PATTERNS.items():
            if pattern.search(content):
                findings.append((str(rel), label))
    for name, label in findings:
        print(f'{label}: {name}')
    print(f'Checked {count} files; findings={len(findings)}')
    return 1 if findings else 0


if __name__ == '__main__':
    sys.exit(main())
