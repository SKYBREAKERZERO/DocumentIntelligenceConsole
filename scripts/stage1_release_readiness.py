"""v1.29 local Stage 1 release / handoff preflight. No external service calls."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.engineering_intelligence.stage1_release_acceptance import (
    build_release_readiness,
    hash_pdf,
    inspect_evidence,
    inspect_local_environment,
    read_report,
)


def _exclusive_write(path: Path, data: dict) -> None:
    if path.is_symlink() or path.exists():
        raise ValueError("Output already exists or is a symlink")
    raw = (json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    if len(raw) > 1_000_000:
        raise ValueError("Readiness report too large")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as dest:
            dest.write(raw)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Stage 1 v1.29 fail-closed readiness report")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--native-report", type=Path)
    p.add_argument("--chain-report", type=Path)
    p.add_argument("--gold-score", type=Path)
    p.add_argument("--original-pdf", type=Path)
    a = p.parse_args(argv)
    paths = [x for x in (a.native_report, a.chain_report, a.gold_score, a.original_pdf) if x]
    if len(paths) not in (0, 4) or len({x.resolve() for x in paths}) != len(paths):
        p.error("Provide all four evidence files together, or none")
    if a.out.resolve() in {x.resolve() for x in paths}:
        p.error("Output may not overwrite an input")
    try:
        if a.out.exists() or a.out.is_symlink():
            raise ValueError("Report destination already exists")
        local = inspect_local_environment(ROOT)
        if paths:
            native, chain, gold = (read_report(a.native_report), read_report(a.chain_report),
                                   read_report(a.gold_score))
            original_sha = hash_pdf(a.original_pdf)
            evidence = inspect_evidence(native=native, chain=chain, gold_score=gold,
                                        original_pdf_sha256=original_sha)
            # A second read catches replacement after the first source hash.
            if hash_pdf(a.original_pdf) != original_sha:
                raise ValueError("Original PDF changed during handoff inspection")
        else:
            evidence = inspect_evidence()
        result = build_release_readiness(local, evidence)
        _exclusive_write(a.out, result)
    except (ValueError, TypeError, KeyError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "NOT_VERIFIED", "error_type": type(exc).__name__}), file=sys.stderr)
        return 2
    print(json.dumps({"status": result["stage1_production_acceptance"],
                      "local_preflight_blocked_checks": result["local_preflight_blocked_checks"],
                      "evidence_status": evidence["status"],
                      "report_written": True}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
