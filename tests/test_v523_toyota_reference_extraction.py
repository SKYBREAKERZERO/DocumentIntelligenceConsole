from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.relations.extractor import (
    EXTRACTOR_VERSION,
    EngineeringRelationExtractor,
    ExtractionUnit,
    KnownSpecification,
)
from app.relations.graph_service import EngineeringRelationGraphService

ROOT = Path(__file__).resolve().parents[1]


def _reference_map(result):
    return {
        (rel.target_external_key or f"ID:{rel.target_specification_id}"): rel
        for rel in result.relations
        if rel.relation_type == "REFERENCES"
    }


def test_toyota_unknown_code_before_title_and_bracket_reference_are_preserved():
    result = EngineeringRelationExtractor().extract(
        source_specification_id=508,
        source_specification_key="24MM:NA:508",
        units=[
            ExtractionUnit(
                content_id=36,
                text="Please refer to 408_User Profile Sequence Specfor the sequence",
            ),
            ExtractionUnit(
                content_id=67,
                text="The HU and server data communication shall comply with [703] specification.",
            ),
        ],
        known_specifications=[
            KnownSpecification(508, "24MM:NA:508", "User Profile function Spec", "508", "24MM")
        ],
    )
    refs = _reference_map(result)
    assert EXTRACTOR_VERSION == "engineering-relations-v5-toyota-dependency-graph"
    assert refs["SPEC:408"].status == "UNRESOLVED"
    assert refs["SPEC:408"].extraction_method == "TOYOTA_CODE_TITLE_REFERENCE"
    assert refs["SPEC:408"].metadata["target_title_hint"] == "User Profile Sequence Spec"
    assert refs["SPEC:408"].metadata["toyota_reference_kind"] == "REFERENCES_SPEC"
    assert refs["SPEC:703"].status == "UNRESOLVED"
    assert refs["SPEC:703"].extraction_method == "TOYOTA_BRACKET_SPEC_REFERENCE"
    assert refs["SPEC:703"].metadata["toyota_reference_kind"] == "COMPLIES_WITH"


def test_toyota_reference_resolves_when_target_master_exists():
    known = [
        KnownSpecification(508, "24MM:NA:508", "User Profile function Spec", "508", "24MM"),
        KnownSpecification(408, "24MM:NA:408", "User Profile Sequence Spec", "408", "24MM"),
        KnownSpecification(703, "24MM:NA:703", "HU Communication Function specification", "703", "24MM"),
    ]
    result = EngineeringRelationExtractor().extract(
        source_specification_id=508,
        source_specification_key="24MM:NA:508",
        units=[
            ExtractionUnit(content_id=36, text="Please refer to 408_User Profile Sequence Specfor the sequence"),
            ExtractionUnit(content_id=67, text="The HU and server data communication shall comply with [703] specification."),
        ],
        known_specifications=known,
    )
    by_id = {rel.target_specification_id: rel for rel in result.relations if rel.relation_type == "REFERENCES"}
    assert by_id[408].status == "CONFIRMED"
    assert by_id[408].metadata["toyota_reference_kind"] == "REFERENCES_SPEC"
    assert "TOYOTA_CODE_TITLE_REFERENCE" in by_id[408].metadata["extraction_methods"]
    assert by_id[703].status == "CONFIRMED"
    assert by_id[703].metadata["toyota_reference_kind"] == "COMPLIES_WITH"
    assert "TOYOTA_BRACKET_SPEC_REFERENCE" in by_id[703].metadata["extraction_methods"]


def test_real_508_json_regression_contains_408_and_703_without_target_imports():
    source = ROOT / "output" / "508_24MM_User Profile function Spec_Ver.7.80.json"
    if not source.is_file():
        pytest.skip("Requires separately authorized real 508 JSON; not distributed in source ZIP")
    payload = json.loads(source.read_text(encoding="utf-8"))
    sections = {str(item.get("id")): item for item in payload.get("sections", [])}
    chapters = {str(item.get("id")): item for item in payload.get("chapters", [])}
    units = []
    for content_id, item in enumerate(payload.get("contents", []), start=1):
        section = sections.get(str(item.get("section_id"))) or {}
        chapter = chapters.get(str(item.get("chapter_id"))) or {}
        units.append(
            ExtractionUnit(
                content_id=content_id,
                text=str(item.get("text") or ""),
                page_number=item.get("page_number"),
                chapter_id=item.get("chapter_id"),
                chapter_title=chapter.get("title_jp") or chapter.get("title_en"),
                section_id=item.get("section_id"),
                section_title=section.get("title_jp") or section.get("title_en"),
                metadata=dict(item.get("metadata") or {}),
                section_metadata=dict(section.get("metadata") or {}),
            )
        )
    result = EngineeringRelationExtractor().extract(
        source_specification_id=508,
        source_specification_key="24MM:NA:508",
        units=units,
        known_specifications=[],
    )
    refs = _reference_map(result)
    assert refs["SPEC:408"].status == "UNRESOLVED"
    assert refs["SPEC:408"].metadata["target_title_hint"] == "User Profile Sequence Spec"
    assert refs["SPEC:703"].status == "UNRESOLVED"
    assert refs["SPEC:703"].metadata["toyota_reference_kind"] == "COMPLIES_WITH"
    # Regression guard: title cells in the Toyota reference table must not be
    # misclassified as catalog numbers.
    assert "SPEC:USER_REGISTRATION_FUNC_SPEC" not in refs
    assert "SPEC:21CY_APP_DATA_REPORTING" not in refs


def test_graph_can_render_source_only_toyota_unresolved_placeholders():
    rows = [
        {
            "relation_id": 1,
            "source_specification_key": "24MM:NA:508",
            "source_specification_name": "User Profile function Spec",
            "source_version": "Ver.7.80",
            "target_external_key": "SPEC:408",
            "relation_type": "REFERENCES",
            "status": "UNRESOLVED",
            "confidence": 0.995,
            "evidence_count": 1,
        },
        {
            "relation_id": 2,
            "source_specification_key": "24MM:NA:508",
            "source_specification_name": "User Profile function Spec",
            "source_version": "Ver.7.80",
            "target_external_key": "SPEC:703",
            "relation_type": "REFERENCES",
            "status": "UNRESOLVED",
            "confidence": 0.995,
            "evidence_count": 1,
        },
    ]
    graph = EngineeringRelationGraphService.build(rows, include_unresolved=True)
    assert len(graph.edges) == 2
    labels = {node.label for node in graph.nodes}
    assert {"408", "703"} <= labels
