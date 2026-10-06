from __future__ import annotations

import ast
from pathlib import Path


def test_data_console_helpers_remain_staticmethods() -> None:
    source_path = Path(__file__).parents[1] / "app" / "enterprise" / "data_module.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    target = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "DataConsoleModule")
    methods = {node.name: node for node in target.body if isinstance(node, ast.FunctionDef)}

    for name in ("_clear_tree", "_display_value", "_qident"):
        method = methods[name]
        decorators = {d.id for d in method.decorator_list if isinstance(d, ast.Name)}
        assert "staticmethod" in decorators, f"{name} must remain a staticmethod; otherwise bound calls inject self"
