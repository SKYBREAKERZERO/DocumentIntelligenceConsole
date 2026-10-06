from __future__ import annotations

import json
from pathlib import Path

from app.relations.extractor import EngineeringRelationExtractor, ExtractionUnit
from app.relations.graph_service import EngineeringRelationGraphService

ROOT = Path(__file__).resolve().parents[1]


def _refs(result):
    return {
        rel.target_external_key or f"ID:{rel.target_specification_id}": rel
        for rel in result.relations
        if rel.relation_type == "REFERENCES"
    }


def test_toyota_title_like_tokens_never_become_primary_spec_codes():
    extractor = EngineeringRelationExtractor()
    for value in (
        "21CY_APP_DATA_REPORTING",
        "21CY_CONNECTEDAUDIO_FUNCTION_SPEC",
        "21CY_SYSTEM/SPEC",
        "530NA21MMAUDIOFUNCTIONSPECNA",
        "BLUETOOTHSPECIFICATIONS",
        "BLUETOOTH仕様書",
    ):
        assert extractor._is_plausible_spec_code(value) is False
    for value in ("408", "703", "A103", "502 NA", "530_NA", "ABC-1234"):
        assert extractor._is_plausible_spec_code(value) is True


def test_generic_reference_section_does_not_promote_title_cells_to_spec_nodes():
    result = EngineeringRelationExtractor().extract(
        source_specification_id=900,
        source_specification_key="21MM:NA:900",
        units=[
            ExtractionUnit(
                content_id=1,
                section_title="Relevant Specification",
                text=(
                    "21CY_APP_DATA_REPORTING | something\n"
                    "BLUETOOTHSPECIFICATIONS | something\n"
                    "408 | User Profile Sequence Spec\n"
                ),
            )
        ],
        known_specifications=[],
    )
    refs = _refs(result)
    assert "SPEC:408" in refs
    assert "SPEC:21CY_APP_DATA_REPORTING" not in refs
    assert "SPEC:BLUETOOTHSPECIFICATIONS" not in refs


def test_multi_bracket_compliance_extracts_every_toyota_spec_number():
    result = EngineeringRelationExtractor().extract(
        source_specification_id=508,
        source_specification_key="24MM:NA:508",
        units=[
            ExtractionUnit(
                content_id=1,
                text="HU shall comply specification [190] and [191] documents.",
            )
        ],
        known_specifications=[],
    )
    refs = _refs(result)
    assert refs["SPEC:190"].metadata["toyota_reference_kind"] == "COMPLIES_WITH"
    assert refs["SPEC:191"].metadata["toyota_reference_kind"] == "COMPLIES_WITH"


def test_graph_hides_title_only_unresolved_candidates_by_default():
    rows = [
        {
            "relation_id": 1,
            "source_specification_id": 508,
            "source_specification_key": "24MM:NA:508",
            "source_specification_name": "User Profile function Spec",
            "source_spec_code": "508",
            "source_version": "Ver.7.80",
            "target_external_key": "SPEC:408",
            "relation_type": "REFERENCES",
            "status": "UNRESOLVED",
            "confidence": 0.995,
            "evidence_count": 2,
            "evidence_chunk_count": 1,
            "metadata": {"target_title_hint": "User Profile Sequence Spec", "target_identity_kind": "SPEC_CODE"},
        },
        {
            "relation_id": 2,
            "source_specification_id": 508,
            "source_specification_key": "24MM:NA:508",
            "source_specification_name": "User Profile function Spec",
            "source_spec_code": "508",
            "source_version": "Ver.7.80",
            "target_external_key": "SPEC_TITLE:BLUETOOTHSPECIFICATIONS",
            "relation_type": "REFERENCES",
            "status": "UNRESOLVED",
            "confidence": 0.82,
            "evidence_count": 1,
            "metadata": {"matched_reference": "Bluetooth specifications", "target_identity_kind": "TITLE_ONLY"},
        },
    ]
    graph = EngineeringRelationGraphService.build(rows, include_unresolved=True)
    assert {node.label for node in graph.nodes} == {"508", "408"}
    assert graph.hidden_title_candidates == 1
    graph_all = EngineeringRelationGraphService.build(
        rows, include_unresolved=True, include_title_candidates=True
    )
    assert "Bluetooth specifications" in {node.label for node in graph_all.nodes}


def test_graph_uses_toyota_semantic_meaning_and_title_hint():
    rows = [
        {
            "relation_id": 1,
            "source_specification_id": 508,
            "source_specification_key": "24MM:NA:508",
            "source_specification_name": "User Profile function Spec",
            "source_spec_code": "508",
            "source_version": "Ver.7.80",
            "target_external_key": "SPEC:703",
            "relation_type": "REFERENCES",
            "status": "UNRESOLVED",
            "confidence": 0.995,
            "evidence_count": 2,
            "evidence_chunk_count": 2,
            "metadata": {
                "toyota_reference_kind": "COMPLIES_WITH",
                "target_title_hint": "HU Communication Function specification",
                "target_identity_kind": "SPEC_CODE",
            },
        }
    ]
    graph = EngineeringRelationGraphService.build(rows, include_unresolved=True)
    assert graph.edges[0].semantic_type == "COMPLIES_WITH"
    target = next(node for node in graph.nodes if node.label == "703")
    assert "HU Communication Function" in target.subtitle


def test_real_508_precision_has_only_plausible_primary_spec_codes():
    payload = json.loads(
        (ROOT / "output" / "508_24MM_User Profile function Spec_Ver.7.80.json").read_text(encoding="utf-8")
    )
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
    refs = _refs(result)
    assert {"SPEC:408", "SPEC:703", "SPEC:165", "SPEC:190", "SPEC:191"} <= set(refs)
    assert refs["SPEC:703"].metadata["toyota_reference_kind"] == "COMPLIES_WITH"
    assert refs["SPEC:190"].metadata["toyota_reference_kind"] == "COMPLIES_WITH"
    assert refs["SPEC:191"].metadata["toyota_reference_kind"] == "COMPLIES_WITH"
    assert not any("BLUETOOTH" in key and not key.startswith("SPEC:") for key in refs)
    assert all(
        not key.startswith("SPEC:21CY_") and "FUNCTIONSPEC" not in key
        for key in refs
    )
