"""Validate a bounded, read-only Engineering Agent workflow graph.

A plan is data, not generated code. This module never executes an LLM output
as Python/SQL or turns evidence content into developer instructions.
"""
from __future__ import annotations

from dataclasses import dataclass

TOOL_ALLOWLIST = frozenset({
    "semantic_search", "sql_exact_search", "relation_search", "version_search",
    "unresolved_reference_search", "specification_catalog", "specification_lookup",
    "issue_search", "issue_statistics", "system_metadata",
})
MAX_TOOLS = 8
MAX_QUERY_VARIANTS = 4
MAX_SCOPES = 4


@dataclass(frozen=True, slots=True)
class WorkflowNode:
    tool: str
    depends_on: tuple[str, ...] = ()
    class_: str = "RETRIEVAL"


@dataclass(frozen=True, slots=True)
class WorkflowGraph:
    nodes: tuple[WorkflowNode, ...]
    scopes: tuple[str, ...]
    max_recovery_passes: int = 1
    max_query_variants: int = MAX_QUERY_VARIANTS

    def validate(self) -> None:
        if len(self.nodes) > MAX_TOOLS or not self.nodes:
            raise ValueError("Workflow must have 1 to 8 tools")
        if len(self.scopes) > MAX_SCOPES or not self.scopes:
            raise ValueError("Workflow has an invalid scope count")
        names = [node.tool for node in self.nodes]
        if len(names) != len(set(names)) or any(name not in TOOL_ALLOWLIST for name in names):
            raise ValueError("Duplicate or unauthorized workflow tool")
        # Fixed topological order is required. This prevents self-dependencies
        # and cycles without depending on a third-party workflow framework.
        seen = set()
        for node in self.nodes:
            if any(dep not in seen for dep in node.depends_on):
                raise ValueError("Workflow dependency must appear before dependent tool")
            seen.add(node.tool)
        if self.max_recovery_passes not in (0, 1):
            raise ValueError("Recovery must be bounded to at most one pass")


def build_workflow(tools: tuple[str, ...], scopes: tuple[str, ...]) -> WorkflowGraph:
    """Build an explainable DAG from a planner-selected allow-listed tool set.

    Independent read queries have no dependency; the executor may later run
    them concurrently when isolation and budgets are established. In the current implementation the
    existing execution engine stays sequential to preserve DB safety.
    """
    dedup = tuple(dict.fromkeys(tools))
    if len(dedup) > MAX_TOOLS:
        # Never silently discard an LLM-selected tool request.
        raise ValueError("Workflow would exceed read-only budget")
    graph = WorkflowGraph(
        nodes=tuple(WorkflowNode(tool=t) for t in dedup), scopes=scopes,
    )
    graph.validate()
    return graph
