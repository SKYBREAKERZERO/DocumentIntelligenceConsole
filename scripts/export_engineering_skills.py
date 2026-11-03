"""Export Skill instructions only; never package internal credentials or data."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.skills.catalog import DOC_ROOT, SKILLS  # noqa: E402

README = """# DocumentIntelligenceConsole Engineering Skills — definitions only

Contains SKILL.md + manifest.json for seven engineering work patterns.
These Skill definitions are not an executable plugin or a PostgreSQL/Qdrant client.
The Python execution implementation stays in the v1.50 source ZIP:
app/skills/executor.py, app/skills/live_evidence.py, scripts/run_skill.py.
Importing text alone does NOT grant the skill access to tools, files, SQL or Ollama.
Check compatibility of the target Agent/Open WebUI version before importing.
INTERNAL_ONLY; do not upload customer evidence to third-party hosting.
"""


def export(target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(target, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("README.md", README)
        for slug in SKILLS:
            for name in ("SKILL.md", "manifest.json"):
                source = DOC_ROOT / slug / name
                if not source.is_file():
                    raise FileNotFoundError(source)
                archive.write(source, f"{slug}/{name}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    export(args.output)
    print(f"Exported {len(SKILLS)} Skill definitions to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
