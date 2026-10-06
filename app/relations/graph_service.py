from __future__ import annotations

import json
import re
import unicodedata
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True, slots=True)
class RelationGraphNode:
    node_id: str
    label: str
    subtitle: str
    kind: str
    status: str = "CONFIRMED"


@dataclass(frozen=True, slots=True)
class RelationGraphEdge:
    relation_id: int
    source_id: str
    target_id: str
    relation_type: str
    semantic_type: str
    status: str
    confidence: float | None
    evidence_count: int
    evidence_chunk_count: int = 0


@dataclass(frozen=True, slots=True)
class RelationGraph:
    nodes: tuple[RelationGraphNode, ...]
    edges: tuple[RelationGraphEdge, ...]
    truncated: bool = False
    hidden_title_candidates: int = 0


class EngineeringRelationGraphService:
    """Build a bounded, evidence-backed Toyota specification dependency graph.

    Primary graph identity is a Toyota Specification No.  Supporting documents,
    title/file-name candidates and malformed historical candidates remain
    auditable in the relation registry but do not pollute the dependency graph
    unless the operator explicitly enables candidate display.
    """

    DEFAULT_DEPTH = 1
    MAX_DEPTH = 3
    MAX_NODES = 200
    MAX_EDGES = 400
    AUTO_ROOT_ID = "__AUTO__"

    _PRIMARY_SPEC_CODE_PATTERN = re.compile(
        r"^(?:"
        r"(?:[A-Z]{0,3}\d{2,6})(?:[_ -]?(?:NA|EU|JP|CN|US|ROW|GLOBAL))?"
        r"|[A-Z]{2,12}[-_/]\d{2,10}(?:[-_/][A-Z0-9]{1,8}){0,2}"
        r")$",
        re.I,
    )
    _SUPPORTING_DOCUMENT_PATTERN = re.compile(
        r"\b(?:cover\s*sheet|check\s*sheet|test\s*sheet|change\s*request|"
        r"reference\s*list|review\s*sheet|evidence\s*sheet|summary\s*sheet|"
        r"catalog(?:ue)?|template|minutes|report)\b",
        re.I,
    )
    _TITLE_NOISE_TOKENS = (
        "FUNCTION", "SPECIFICATION", "SPECIFICATIONS", "SYSTEM", "AUDIO",
        "REPORTING", "BLUETOOTH", "PROFILE", "CONNECTED", "DOCUMENT",
    )

    @classmethod
    def _normalize_code(cls, value: Any) -> str:
        text = unicodedata.normalize("NFKC", str(value or "")).strip().upper()
        text = text.replace("－", "-").replace("–", "-").replace("—", "-").replace("―", "-")
        text = re.sub(r"\s+", " ", text)
        return text

    @classmethod
    def is_primary_spec_code(cls, value: Any) -> bool:
        raw = cls._normalize_code(value)
        if not raw or len(raw) > 32:
            return False
        generic_code = re.fullmatch(
            r"[A-Z]{2,12}[-_/]\d{2,10}(?:[-_/][A-Z0-9]{1,8}){0,2}",
            raw,
        )
        if any(token in raw for token in cls._TITLE_NOISE_TOKENS) and not generic_code:
            return False
        return bool(cls._PRIMARY_SPEC_CODE_PATTERN.fullmatch(raw))

    @classmethod
    def _code_from_key(cls, value: Any) -> str:
        key = str(value or "").strip()
        if not key:
            return ""
        tail = key.split(":")[-1].strip()
        return cls._normalize_code(tail) if cls.is_primary_spec_code(tail) else ""

    @classmethod
    def _row_source_code(cls, row: dict[str, Any]) -> str:
        direct = str(row.get("source_spec_code") or "").strip()
        if cls.is_primary_spec_code(direct):
            return cls._normalize_code(direct)
        return cls._code_from_key(row.get("source_specification_key"))

    @classmethod
    def _row_target_code(cls, row: dict[str, Any]) -> str:
        direct = str(row.get("target_spec_code") or "").strip()
        if cls.is_primary_spec_code(direct):
            return cls._normalize_code(direct)
        key_code = cls._code_from_key(row.get("target_specification_key"))
        if key_code:
            return key_code
        external = str(row.get("target_external_key") or "").strip()
        if external.startswith("SPEC:"):
            raw = external[len("SPEC:"):].strip()
            if cls.is_primary_spec_code(raw):
                return cls._normalize_code(raw)
        return ""

    @classmethod
    def is_supporting_source(cls, row: dict[str, Any]) -> bool:
        if cls._row_source_code(row):
            return False
        key = str(row.get("source_specification_key") or "")
        name = str(row.get("source_specification_name") or "")
        if "UNSPECIFIED:NAME:" in key.upper():
            return True
        return bool(cls._SUPPORTING_DOCUMENT_PATTERN.search(name))

    @classmethod
    def is_primary_reference_row(cls, row: dict[str, Any]) -> bool:
        if str(row.get("relation_type") or "").strip().upper() != "REFERENCES":
            return False
        if cls.is_supporting_source(row):
            return False
        if not cls._row_source_code(row):
            return False

        metadata = cls._metadata(row)
        identity_kind = str(metadata.get("target_identity_kind") or "").upper()
        external = str(row.get("target_external_key") or "")
        if identity_kind == "TITLE_ONLY" or external.startswith("SPEC_TITLE:"):
            return False

        # A resolved target must also carry a primary Spec No.; otherwise it is
        # a supporting/title-only master and belongs in the audit view.
        if row.get("target_specification_id") or row.get("target_specification_key"):
            return bool(cls._row_target_code(row))

        if external.startswith("SPEC:"):
            return bool(cls._row_target_code(row))
        return False

    @classmethod
    def recommended_root(cls, rows: Iterable[dict[str, Any]]) -> str | None:
        counts: dict[str, tuple[int, str]] = {}
        for row in rows:
            if not cls.is_primary_reference_row(row):
                continue
            source_id, source = cls._source_node(row)
            count, _ = counts.get(source_id, (0, cls._display_name(source)))
            counts[source_id] = (count + 1, cls._display_name(source))
        if not counts:
            return None
        return sorted(
            counts.items(),
            key=lambda item: (-item[1][0], item[1][1].casefold(), item[0]),
        )[0][0]

    @classmethod
    def root_options(cls, rows: Iterable[dict[str, Any]]) -> list[tuple[str, str]]:
        labels: dict[str, str] = {}
        for row in rows:
            if not cls.is_primary_reference_row(row):
                continue
            source_id, source = cls._source_node(row)
            labels[source_id] = cls._display_name(source)
            target = cls._target_node(row, include_title_candidates=False)
            if target is not None and target[1].kind == "SPECIFICATION":
                labels[target[0]] = cls._display_name(target[1])
        return sorted(labels.items(), key=lambda item: item[1].casefold())

    @classmethod
    def build(
        cls,
        rows: Iterable[dict[str, Any]],
        *,
        root_id: str | None = None,
        depth: int = DEFAULT_DEPTH,
        direction: str = "BOTH",
        include_unresolved: bool = False,
        include_title_candidates: bool = False,
        references_only: bool = True,
        max_nodes: int = MAX_NODES,
        max_edges: int = MAX_EDGES,
    ) -> RelationGraph:
        rows = tuple(rows)
        depth = max(1, min(int(depth), cls.MAX_DEPTH))
        direction = str(direction or "BOTH").strip().upper()
        if direction not in {"BOTH", "OUTGOING", "INCOMING"}:
            direction = "BOTH"
        max_nodes = max(2, int(max_nodes))
        max_edges = max(1, int(max_edges))

        if references_only and not root_id:
            root_id = cls.recommended_root(rows)

        nodes: dict[str, RelationGraphNode] = {}
        edges: list[RelationGraphEdge] = []
        hidden_title_candidates = 0
        for row in rows:
            relation_type = str(row.get("relation_type") or "").strip().upper()
            status = str(row.get("status") or "").strip().upper() or "UNRESOLVED"
            if references_only and relation_type != "REFERENCES":
                continue
            if not include_unresolved and status != "CONFIRMED":
                continue

            metadata = cls._metadata(row)
            identity_kind = str(metadata.get("target_identity_kind") or "").upper()
            external = str(row.get("target_external_key") or "")
            is_title_only = identity_kind == "TITLE_ONLY" or external.startswith("SPEC_TITLE:")

            if references_only and not include_title_candidates:
                if is_title_only or not cls.is_primary_reference_row(row):
                    hidden_title_candidates += 1
                    continue
            elif is_title_only and not include_title_candidates:
                hidden_title_candidates += 1
                continue

            source_id, source_node = cls._source_node(row)
            target_data = cls._target_node(row, include_title_candidates=include_title_candidates)
            if target_data is None:
                hidden_title_candidates += 1
                continue
            target_id, target_node = target_data
            nodes[source_id] = source_node
            nodes[target_id] = target_node
            confidence = row.get("confidence")
            edges.append(
                RelationGraphEdge(
                    relation_id=int(row.get("relation_id") or 0),
                    source_id=source_id,
                    target_id=target_id,
                    relation_type=relation_type or "RELATES_TO",
                    semantic_type=cls.semantic_type(row),
                    status=status,
                    confidence=float(confidence) if confidence is not None else None,
                    evidence_count=int(row.get("evidence_count") or 0),
                    evidence_chunk_count=int(row.get("evidence_chunk_count") or 0),
                )
            )

        # Defensive graph-level de-duplication. A legacy database may contain
        # more than one run marked current for the same Specification. The SQL
        # v6.3 guard repairs that state, but the renderer also refuses to draw
        # duplicate authoritative arrows while an upgrade is pending. Prefer a
        # CONFIRMED edge, then the newest relation id, without double-counting
        # evidence from duplicate extraction generations.
        deduped: dict[tuple[str, str, str], RelationGraphEdge] = {}
        for edge in edges:
            key = (edge.source_id, edge.target_id, edge.semantic_type)
            previous = deduped.get(key)
            if previous is None:
                deduped[key] = edge
                continue
            edge_rank = (edge.status == "CONFIRMED", edge.relation_id)
            previous_rank = (previous.status == "CONFIRMED", previous.relation_id)
            if edge_rank > previous_rank:
                deduped[key] = edge
        edges = sorted(deduped.values(), key=lambda edge: edge.relation_id)

        if not nodes or not edges:
            return RelationGraph(
                nodes=tuple(),
                edges=tuple(),
                hidden_title_candidates=hidden_title_candidates,
            )

        truncated = False
        selected_nodes: set[str]
        if root_id and root_id in nodes:
            outgoing: dict[str, set[str]] = defaultdict(set)
            incoming: dict[str, set[str]] = defaultdict(set)
            for edge in edges:
                outgoing[edge.source_id].add(edge.target_id)
                incoming[edge.target_id].add(edge.source_id)

            selected_nodes = {root_id}
            queue: deque[tuple[str, int]] = deque([(root_id, 0)])
            while queue:
                current, level = queue.popleft()
                if level >= depth:
                    continue
                neighbors: set[str] = set()
                if direction in {"BOTH", "OUTGOING"}:
                    neighbors.update(outgoing.get(current, set()))
                if direction in {"BOTH", "INCOMING"}:
                    neighbors.update(incoming.get(current, set()))
                for neighbor in sorted(neighbors):
                    if neighbor in selected_nodes:
                        continue
                    if len(selected_nodes) >= max_nodes:
                        truncated = True
                        break
                    selected_nodes.add(neighbor)
                    queue.append((neighbor, level + 1))
                if truncated:
                    break
        else:
            # Safety fallback: never silently render the entire repository in
            # the primary dependency view. Pick the first source instead.
            fallback_root = cls.recommended_root(rows)
            if fallback_root and fallback_root in nodes:
                return cls.build(
                    rows,
                    root_id=fallback_root,
                    depth=depth,
                    direction=direction,
                    include_unresolved=include_unresolved,
                    include_title_candidates=include_title_candidates,
                    references_only=references_only,
                    max_nodes=max_nodes,
                    max_edges=max_edges,
                )
            selected_nodes = set()

        selected_edges: list[RelationGraphEdge] = []
        for edge in edges:
            if edge.source_id not in selected_nodes or edge.target_id not in selected_nodes:
                continue
            selected_edges.append(edge)
            if len(selected_edges) >= max_edges:
                if len(edges) > len(selected_edges):
                    truncated = True
                break

        connected_nodes: set[str] = set()
        for edge in selected_edges:
            connected_nodes.add(edge.source_id)
            connected_nodes.add(edge.target_id)
        if root_id and root_id in selected_nodes:
            connected_nodes.add(root_id)

        node_list = tuple(
            nodes[node_id]
            for node_id in sorted(
                connected_nodes,
                key=lambda key: (nodes[key].kind, nodes[key].label.casefold(), key),
            )
        )
        return RelationGraph(
            nodes=node_list,
            edges=tuple(selected_edges),
            truncated=truncated,
            hidden_title_candidates=hidden_title_candidates,
        )

    @classmethod
    def semantic_type(cls, row: dict[str, Any]) -> str:
        relation_type = str(row.get("relation_type") or "RELATES_TO").strip().upper()
        if relation_type != "REFERENCES":
            return relation_type
        metadata = cls._metadata(row)
        kind = str(metadata.get("toyota_reference_kind") or "").strip().upper()
        if kind:
            return kind
        identity_kind = str(metadata.get("target_identity_kind") or "").strip().upper()
        if identity_kind == "TITLE_ONLY":
            return "TITLE_ONLY_CANDIDATE"
        return "REFERENCES_SPEC"

    @staticmethod
    def _metadata(row: dict[str, Any]) -> dict[str, Any]:
        value = row.get("metadata")
        if isinstance(value, dict):
            return dict(value)
        if isinstance(value, str) and value.strip():
            try:
                parsed = json.loads(value)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                pass
        return {}

    @staticmethod
    def _display_name(node: RelationGraphNode) -> str:
        if node.subtitle and node.subtitle != node.label:
            return f"{node.label} — {node.subtitle.split(' · ')[0]}"
        return node.label

    @classmethod
    def _source_node(cls, row: dict[str, Any]) -> tuple[str, RelationGraphNode]:
        key = str(row.get("source_specification_key") or row.get("source_specification_id") or "UNKNOWN")
        name = str(row.get("source_specification_name") or key)
        code = cls._row_source_code(row)
        version = str(row.get("source_version") or "").strip()
        node_id = f"SPEC::{key}"
        label = code or name
        subtitle_parts: list[str] = []
        if code and name and name != code:
            subtitle_parts.append(name)
        elif not code:
            subtitle_parts.append("Supporting / title-only source")
        if version:
            subtitle_parts.append(version)
        subtitle = " · ".join(subtitle_parts) or key
        return node_id, RelationGraphNode(
            node_id=node_id,
            label=label,
            subtitle=subtitle,
            kind="SPECIFICATION" if code else "SUPPORTING",
            status="CONFIRMED",
        )

    @classmethod
    def _target_node(
        cls,
        row: dict[str, Any],
        *,
        include_title_candidates: bool,
    ) -> tuple[str, RelationGraphNode] | None:
        target_spec_key = row.get("target_specification_key")
        target_spec_id = row.get("target_specification_id")
        if target_spec_key or target_spec_id:
            key = str(target_spec_key or target_spec_id)
            name = str(row.get("target_specification_name") or key)
            code = cls._row_target_code(row)
            if not code and not include_title_candidates:
                return None
            node_id = f"SPEC::{key}"
            label = code or name
            subtitle = name if code and name != code else (
                "Supporting / title-only master" if not code else key
            )
            return node_id, RelationGraphNode(
                node_id=node_id,
                label=label,
                subtitle=subtitle,
                kind="SPECIFICATION" if code else "SUPPORTING",
                status=str(row.get("status") or "CONFIRMED").upper(),
            )

        entity_name = row.get("target_entity_name")
        entity_key = row.get("target_entity_key")
        if entity_name or entity_key:
            if not include_title_candidates:
                return None
            entity_type = str(row.get("target_entity_type") or "ENTITY")
            key = str(entity_key or entity_name)
            node_id = f"ENTITY::{entity_type}::{key}"
            return node_id, RelationGraphNode(
                node_id=node_id,
                label=str(entity_name or key),
                subtitle=entity_type,
                kind="ENTITY",
                status=str(row.get("status") or "CONFIRMED").upper(),
            )

        external = str(row.get("target_external_key") or "").strip()
        if not external:
            return None
        metadata = cls._metadata(row)

        if external.startswith("SPEC:"):
            raw_code = external[len("SPEC:"):].strip()
            if not cls.is_primary_spec_code(raw_code):
                if not include_title_candidates:
                    return None
                return (
                    f"EXTERNAL::{external}",
                    RelationGraphNode(
                        node_id=f"EXTERNAL::{external}",
                        label=raw_code or external,
                        subtitle="Rejected primary identity · audit candidate",
                        kind="SUPPORTING",
                        status=str(row.get("status") or "UNRESOLVED").upper(),
                    ),
                )
            code = cls._normalize_code(raw_code)
            hint = str(metadata.get("target_title_hint") or "").strip()
            subtitle = f"{hint} · Unresolved" if hint else "Unresolved reference"
            return (
                f"EXTERNAL::SPEC:{code}",
                RelationGraphNode(
                    node_id=f"EXTERNAL::SPEC:{code}",
                    label=code,
                    subtitle=subtitle,
                    kind="EXTERNAL",
                    status=str(row.get("status") or "UNRESOLVED").upper(),
                ),
            )

        if external.startswith("SPEC_TITLE:"):
            if not include_title_candidates:
                return None
            matched = str(metadata.get("matched_reference") or "").strip()
            label = matched or external[len("SPEC_TITLE:"):]
            return (
                f"EXTERNAL::{external}",
                RelationGraphNode(
                    node_id=f"EXTERNAL::{external}",
                    label=label,
                    subtitle="Title-only candidate · audit only",
                    kind="SUPPORTING",
                    status=str(row.get("status") or "UNRESOLVED").upper(),
                ),
            )
        return None
