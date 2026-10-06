from __future__ import annotations

from pathlib import Path

from app.relations.extractor import EngineeringRelationExtractor, ExtractionUnit, KnownSpecification
from app.relations.graph_service import EngineeringRelationGraphService

ROOT = Path(__file__).resolve().parents[1]


def _ref_map(result):
    return {
        rel.target_external_key or f"ID:{rel.target_specification_id}": rel
        for rel in result.relations
        if rel.relation_type == "REFERENCES"
    }


def test_document_local_reference_table_resolves_title_in_later_chunk():
    result = EngineeringRelationExtractor().extract(
        source_specification_id=508,
        source_specification_key="21MM:NA:508",
        units=[
            ExtractionUnit(
                content_id=1,
                section_title="Relevant Specification",
                text=(
                    "Specification name | Specification No.\n"
                    "User Profile Sequence Spec | 408\n"
                    "HU Communication Function Spec | 703\n"
                ),
            ),
            ExtractionUnit(
                content_id=2,
                section_title="Sequence diagram with concerned device",
                text="Refer to User Profile Sequence Spec for sequence diagram.",
            ),
        ],
        known_specifications=[
            KnownSpecification(
                specification_id=508,
                specification_key="21MM:NA:508",
                canonical_name="User Profile function Spec",
                spec_code="508",
                project_code="21MM",
            )
        ],
    )
    refs = _ref_map(result)
    assert "SPEC:408" in refs
    rel = refs["SPEC:408"]
    assert rel.status == "UNRESOLVED"
    assert "LOCAL_REFERENCE_TABLE_TITLE" in rel.metadata["extraction_methods"]
    assert rel.metadata["target_identity_kind"] == "SPEC_CODE"


def test_primary_graph_hides_supporting_sources_and_invalid_spec_identity():
    rows = [
        {
            "relation_id": 1,
            "source_specification_id": 508,
            "source_specification_key": "21MM:NA:508",
            "source_specification_name": "User Profile function Spec",
            "source_spec_code": "508",
            "target_external_key": "SPEC:408",
            "relation_type": "REFERENCES",
            "status": "UNRESOLVED",
            "confidence": 0.99,
            "evidence_count": 1,
            "metadata": {"target_identity_kind": "SPEC_CODE"},
        },
        {
            "relation_id": 2,
            "source_specification_id": 99,
            "source_specification_key": "21MM:UNSPECIFIED:NAME:abcdef",
            "source_specification_name": "Bluetooth Audio Function Spec Cover Sheet",
            "source_spec_code": "",
            "target_external_key": "SPEC:100",
            "relation_type": "REFERENCES",
            "status": "UNRESOLVED",
            "confidence": 0.98,
            "evidence_count": 1,
            "metadata": {"target_identity_kind": "SPEC_CODE"},
        },
        {
            "relation_id": 3,
            "source_specification_id": 508,
            "source_specification_key": "21MM:NA:508",
            "source_specification_name": "User Profile function Spec",
            "source_spec_code": "508",
            "target_external_key": "SPEC:21CY_APP_DATA_REPORTING",
            "relation_type": "REFERENCES",
            "status": "UNRESOLVED",
            "confidence": 0.98,
            "evidence_count": 1,
            "metadata": {"target_identity_kind": "SPEC_CODE"},
        },
    ]

    graph = EngineeringRelationGraphService.build(rows, include_unresolved=True)
    assert {edge.relation_id for edge in graph.edges} == {1}
    assert {node.label for node in graph.nodes} == {"508", "408"}
    assert graph.hidden_title_candidates >= 2


def test_recommended_root_uses_primary_source_with_most_edges():
    rows = [
        {
            "relation_id": 1,
            "source_specification_key": "21MM:NA:508",
            "source_specification_name": "User Profile function Spec",
            "source_spec_code": "508",
            "target_external_key": "SPEC:408",
            "relation_type": "REFERENCES",
            "status": "UNRESOLVED",
            "metadata": {"target_identity_kind": "SPEC_CODE"},
        },
        {
            "relation_id": 2,
            "source_specification_key": "21MM:NA:508",
            "source_specification_name": "User Profile function Spec",
            "source_spec_code": "508",
            "target_external_key": "SPEC:703",
            "relation_type": "REFERENCES",
            "status": "UNRESOLVED",
            "metadata": {"target_identity_kind": "SPEC_CODE"},
        },
        {
            "relation_id": 3,
            "source_specification_key": "21MM:NA:550",
            "source_specification_name": "Bluetooth Audio Function Spec",
            "source_spec_code": "550",
            "target_external_key": "SPEC:132",
            "relation_type": "REFERENCES",
            "status": "UNRESOLVED",
            "metadata": {"target_identity_kind": "SPEC_CODE"},
        },
    ]
    assert EngineeringRelationGraphService.recommended_root(rows) == "SPEC::21MM:NA:508"

    graph = EngineeringRelationGraphService.build(rows, include_unresolved=True)
    assert {node.label for node in graph.nodes} == {"508", "408", "703"}
    assert {edge.relation_id for edge in graph.edges} == {1, 2}


def test_compact_header_removes_capability_subtitle_and_visible_version_badge():
    source = (ROOT / "app" / "gui" / "application.py").read_text(encoding="utf-8")
    assert "Ingestion · Specification Lifecycle · Change Intelligence · PostgreSQL · Vector · RAG" not in source
    assert 'text=f"v{APP_VERSION}"' not in source
    assert '"All loaded specifications"' not in source
    assert 'APP_VERSION = "5.2.8"' in source


def test_strict_identity_rejects_title_file_tokens():
    for value in (
        "21CY_APP_DATA_REPORTING",
        "21CY_CONNECTEDAUDIO_FUNCTION_SPEC",
        "21CY_SYSTEM/SPEC",
        "BLUETOOTHSPECIFICATIONS",
        "530NA21MMAUDIOFUNCTIONSPECNA",
    ):
        assert EngineeringRelationGraphService.is_primary_spec_code(value) is False

    for value in ("100", "132", "165", "408", "508", "703", "A103", "530_NA"):
        assert EngineeringRelationGraphService.is_primary_spec_code(value) is True
