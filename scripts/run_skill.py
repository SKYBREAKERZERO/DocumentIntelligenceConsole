r"""Execute an allow-listed DIC Skill against bounded local JSON (offline only).

Usage (Windows PowerShell): python .\scripts\run_skill.py --list
python .\scripts\run_skill.py rag-evidence-verification --input .\examples\skills\evidence.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Supports direct python scripts/run_skill.py in the distributed source tree.
PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from app.skills.catalog import SKILLS, list_skills, suggest_skills  # noqa: E402
from app.skills.executor import SkillInputError, execute_skill  # noqa: E402

MAX_INPUT_BYTES = 2_000_000


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("skill", nargs="?", choices=SKILLS)
    parser.add_argument("--input", type=Path, help="Local offline JSON snapshot (max 2 MB)")
    parser.add_argument("--list", action="store_true", help="List available fixed Skills")
    parser.add_argument("--suggest", type=str, help="Suggest skills from a question; never execute them")
    args = parser.parse_args(argv)
    if args.list:
        print(json.dumps({"skills": list_skills()}, ensure_ascii=True, indent=2))
        return 0
    if args.suggest is not None:
        print(json.dumps({"suggested_skills": suggest_skills(args.suggest), "executed": False}, ensure_ascii=True, indent=2))
        return 0
    if not args.skill or args.input is None:
        parser.error("Specify --list or a Skill and --input local JSON")
    try:
        with args.input.open("rb") as stream:
            raw = stream.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            raise SkillInputError("INPUT_TOO_LARGE")
        payload = json.loads(raw)
        outcome = execute_skill(args.skill, payload)
    except (SkillInputError, ValueError, KeyError, TypeError, OSError) as exc:
        print(json.dumps({"skill": args.skill, "status": "BLOCKED",
                          "reason": str(exc)[:180], "production_acceptance": "NOT_ASSESSED"},
                         ensure_ascii=True), file=sys.stderr)
        return 2
    # ASCII JSON escapes round-trip all source text under Windows cp932/GBK
    # consoles and redirected pipes; consumers decode with json.loads().
    print(json.dumps(outcome, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
