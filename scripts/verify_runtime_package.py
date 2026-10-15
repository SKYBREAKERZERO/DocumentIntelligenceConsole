from __future__ import annotations

"""Release integrity check: static local application imports without third-party imports.

Run before launching the Windows desktop GUI. This checks shipped internal module
paths, not whether Windows dependencies or external services are installed.
"""

import ast
from pathlib import Path
import sys


def missing_internal_imports(root: Path) -> list[tuple[str, str]]:
    """Scan all shipped Python files for absent absolute `app.*` modules."""
    root = root.resolve()
    app_root = root / "app"
    if not app_root.is_dir():
        return [("<package>", "app")]
    missing: set[tuple[str, str]] = set()
    for path in sorted((root / "app").rglob("*.py")) + sorted((root / "scripts").rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        except (SyntaxError, UnicodeError, OSError) as error:
            missing.add((str(path.relative_to(root)), f"invalid Python source: {error}"))
            continue
        modules = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and (
                node.module == "app" or node.module.startswith("app.")
            ) and node.level == 0:
                modules.append(node.module)
            elif isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names if alias.name.startswith("app."))
        for module in modules:
            target = root.joinpath(*module.split("."))
            if not (target.is_dir() or target.with_suffix(".py").is_file()):
                missing.add((str(path.relative_to(root)), module))
    return sorted(missing)


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    problems = missing_internal_imports(project_root)
    if problems:
        print("[ERROR] Incomplete application package; internal imports are missing:")
        for owner, module in problems:
            print(f"  {owner} -> {module}")
        print("Replace the entire source directory with a complete release; do not mix versions.")
        return 2
    print("[OK] Local application source/import paths are complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
