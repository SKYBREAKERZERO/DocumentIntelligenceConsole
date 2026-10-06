from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from app.config.config_loader import ConfigLoader


class EngineeringRelationPolicyError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class EngineeringRelationPolicy:
    version: str
    entity_types: tuple[str, ...]
    relation_types: tuple[str, ...]
    authoritative_methods: tuple[str, ...]
    llm_candidates_enabled: bool
    confirmed_only_default_graph: bool
    preserve_unresolved_spec_references: bool
    retain_extraction_generations: bool
    max_evidence_snippet_chars: int
    default_hop_depth: int
    max_hop_depth: int
    max_nodes: int
    max_edges: int
    source_path: Path


class EngineeringRelationPolicyLoader:
    ENV_NAME = "ENGINEERING_RELATION_RULES_FILE"
    RELATIVE_PATH = Path("config") / "engineering_relation_rules.yaml"
    _cache: EngineeringRelationPolicy | None = None
    _cache_path: Path | None = None
    _cache_mtime_ns: int | None = None

    @classmethod
    def load(cls, path: str | Path | None = None, *, force_reload: bool = False) -> EngineeringRelationPolicy:
        resolved = cls.resolve_path(path)
        stat = resolved.stat()
        if (
            not force_reload
            and cls._cache is not None
            and cls._cache_path == resolved
            and cls._cache_mtime_ns == stat.st_mtime_ns
        ):
            return cls._cache
        try:
            raw = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            raise EngineeringRelationPolicyError(f"Failed to read engineering relation policy: {resolved}: {exc}") from exc
        policy = cls._build(raw, resolved)
        cls._cache = policy
        cls._cache_path = resolved
        cls._cache_mtime_ns = stat.st_mtime_ns
        return policy

    @classmethod
    def resolve_path(cls, path: str | Path | None = None) -> Path:
        candidates: list[Path] = []
        if path is not None:
            candidates.append(Path(path).expanduser())
        else:
            env = os.getenv(cls.ENV_NAME)
            if env and env.strip():
                candidates.append(Path(env.strip()).expanduser())
            candidates.append(ConfigLoader.get_application_directory() / cls.RELATIVE_PATH)
            candidates.append(ConfigLoader.get_resource_directory() / cls.RELATIVE_PATH)
        seen: set[Path] = set()
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            if resolved.is_file():
                return resolved
        searched = "\n".join(f"  - {item}" for item in seen)
        raise EngineeringRelationPolicyError("Engineering relation rules file was not found. Searched:\n" + searched)

    @staticmethod
    def _build(raw: Any, source_path: Path) -> EngineeringRelationPolicy:
        if not isinstance(raw, dict):
            raise EngineeringRelationPolicyError("Engineering relation policy root must be a mapping.")
        extraction = raw.get("extraction_policy") or {}
        query = raw.get("query_policy") or {}
        relation_groups = raw.get("relation_types") or {}
        relation_types: list[str] = []
        if isinstance(relation_groups, dict):
            for values in relation_groups.values():
                if isinstance(values, list):
                    relation_types.extend(str(value).strip().upper() for value in values if str(value).strip())
        entity_types = tuple(str(value).strip().upper() for value in (raw.get("entity_types") or []) if str(value).strip())
        authoritative = tuple(str(value).strip().upper() for value in (extraction.get("authoritative_methods") or []) if str(value).strip())
        max_snippet = int(extraction.get("max_evidence_snippet_chars", 360) or 360)
        max_snippet = max(120, min(max_snippet, 4000))
        default_hop = int(query.get("default_hop_depth", 1) or 1)
        max_hop = max(default_hop, int(query.get("max_hop_depth", 2) or 2))
        return EngineeringRelationPolicy(
            version=str(raw.get("version") or "engineering-relations-v1"),
            entity_types=entity_types,
            relation_types=tuple(dict.fromkeys(relation_types)),
            authoritative_methods=authoritative,
            llm_candidates_enabled=bool(extraction.get("llm_candidates_enabled", False)),
            confirmed_only_default_graph=bool(extraction.get("confirmed_only_default_graph", True)),
            preserve_unresolved_spec_references=bool(extraction.get("preserve_unresolved_spec_references", True)),
            retain_extraction_generations=bool(extraction.get("retain_extraction_generations", True)),
            max_evidence_snippet_chars=max_snippet,
            default_hop_depth=default_hop,
            max_hop_depth=max_hop,
            max_nodes=max(10, int(query.get("max_nodes", 150) or 150)),
            max_edges=max(10, int(query.get("max_edges", 300) or 300)),
            source_path=source_path,
        )
