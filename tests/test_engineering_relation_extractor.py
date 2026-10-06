from app.relations.extractor import (
    EngineeringRelationExtractor,
    ExtractionUnit,
    KnownSpecification,
)


def test_extracts_explicit_engineering_entities_and_relations():
    extractor = EngineeringRelationExtractor()
    known = [
        KnownSpecification(1, "24MM:BT", "Bluetooth Audio Specification", "550", "24MM"),
        KnownSpecification(2, "24MM:COMMON", "Audio Common Specification", "888", "24MM"),
    ]
    unit = ExtractionUnit(
        content_id=10,
        text=(
            "Refer to Audio Common Specification. "
            "Requirement ID: REQ-BT-00125. "
            "Signal Name: VehicleSpeed; Direction: Rx. "
            "Interface Name: BluetoothAudioControl; client uses interface. "
            "Protocol: SOMEIP"
        ),
        page_number=3,
        chapter_id="4",
        chapter_title="BT",
        section_id="4.3",
        section_title="Control",
        section_metadata={"sheet_name": "Audio Control", "first_row_number": 37, "last_row_number": 40},
    )

    result = extractor.extract(
        source_specification_id=1,
        source_specification_key="24MM:BT",
        units=[unit],
        known_specifications=known,
    )

    entity_types = {e.entity_type for e in result.entities}
    relation_types = {r.relation_type for r in result.relations}
    assert {"REQUIREMENT", "SIGNAL", "INTERFACE", "PROTOCOL"} <= entity_types
    assert "REFERENCES" in relation_types
    assert "DEFINES_REQUIREMENT" in relation_types
    assert "CONSUMES_SIGNAL" in relation_types
    assert "USES_INTERFACE" in relation_types
    assert "USES_PROTOCOL" in relation_types
    assert result.unresolved_count == 0
    assert all(r.evidence for r in result.relations)


def test_unresolved_spec_reference_is_preserved():
    result = EngineeringRelationExtractor().extract(
        source_specification_id=1,
        source_specification_key="24MM:SOURCE",
        units=[ExtractionUnit(content_id=1, text="Refer to Specification 9999 for details.")],
        known_specifications=[KnownSpecification(1, "24MM:SOURCE", "Source Spec", "100", "24MM")],
    )
    refs = [r for r in result.relations if r.relation_type == "REFERENCES"]
    assert len(refs) == 1
    assert refs[0].status == "UNRESOLVED"
    assert refs[0].target_external_key == "SPEC:9999"
    assert result.unresolved_count == 1


def test_known_spec_code_requires_explicit_spec_context():
    extractor = EngineeringRelationExtractor()
    known = [
        KnownSpecification(1, "24MM:SRC", "Source Spec", "100", "24MM"),
        KnownSpecification(2, "24MM:TGT", "Target Spec", "555", "24MM"),
    ]
    no_ref = extractor.extract(
        source_specification_id=1,
        source_specification_key="24MM:SRC",
        units=[ExtractionUnit(content_id=1, text="The value is 555 milliseconds.")],
        known_specifications=known,
    )
    assert not [r for r in no_ref.relations if r.target_specification_id == 2]

    yes_ref = extractor.extract(
        source_specification_id=1,
        source_specification_key="24MM:SRC",
        units=[ExtractionUnit(content_id=2, text="Refer to Spec 555 for behavior.")],
        known_specifications=known,
    )
    assert any(r.target_specification_id == 2 for r in yes_ref.relations)



def test_japanese_labels_preserve_sheet_row_evidence():
    extractor = EngineeringRelationExtractor()
    unit = ExtractionUnit(
        content_id=42,
        text="要件 ID: REQ-JP-1001 | 信号名: VehicleMode | Interface Name: VehicleControlAPI",
        section_id="3.2",
        section_title="信号・I/F 定義",
        section_metadata={"sheet_name": "Interface", "first_row_number": 15, "last_row_number": 18},
    )
    result = extractor.extract(
        source_specification_id=10,
        source_specification_key="24MM:TEST",
        units=[unit],
        known_specifications=[KnownSpecification(10, "24MM:TEST", "Test Specification", "100", "24MM")],
    )
    assert {e.entity_type for e in result.entities} >= {"REQUIREMENT", "SIGNAL", "INTERFACE"}
    evidence = [ev for rel in result.relations for ev in rel.evidence]
    assert evidence
    assert all(ev.sheet_name == "Interface" for ev in evidence)
    assert all(ev.cell_range == "rows 15-18" for ev in evidence)


def test_v2_reference_section_resolves_numeric_code_without_explicit_spec_label():
    extractor = EngineeringRelationExtractor()
    known = [
        KnownSpecification(1, "24MM:JP:1001", "User Profile Main Function Spec", "1001", "24MM"),
        KnownSpecification(2, "24MM:JP:2007", "User Profile Communication Interface Spec", "2007", "24MM"),
    ]
    unit = ExtractionUnit(
        content_id=101,
        text="Document No. | Document Title | Revision\n2007 | User Profile Communication Interface Spec | Rev2.1",
        section_id="2",
        section_title="Applicable Documents / 適用文書",
    )
    result = extractor.extract(
        source_specification_id=1,
        source_specification_key="24MM:JP:1001",
        units=[unit],
        known_specifications=known,
    )
    refs = [r for r in result.relations if r.relation_type == "REFERENCES"]
    assert len(refs) == 1
    assert refs[0].target_specification_id == 2
    assert refs[0].status == "CONFIRMED"
    assert refs[0].confidence >= 0.99
    assert result.stats["qdrant_used"] is False


def test_v2_numeric_code_without_reference_context_is_not_a_relation():
    extractor = EngineeringRelationExtractor()
    known = [
        KnownSpecification(1, "24MM:JP:1001", "Source Spec", "1001", "24MM"),
        KnownSpecification(2, "24MM:JP:2007", "Target Spec", "2007", "24MM"),
    ]
    result = extractor.extract(
        source_specification_id=1,
        source_specification_key="24MM:JP:1001",
        units=[ExtractionUnit(content_id=1, text="The retry timeout shall be 2007 ms under the stress condition.")],
        known_specifications=known,
    )
    assert not [r for r in result.relations if r.target_specification_id == 2]


def test_v2_fullwidth_code_is_normalized_and_resolved():
    result = EngineeringRelationExtractor().extract(
        source_specification_id=1,
        source_specification_key="24MM:JP:1001",
        units=[ExtractionUnit(content_id=2, text="中心通信时序应依据 Specification ２００７ 第4.2节。")],
        known_specifications=[
            KnownSpecification(1, "24MM:JP:1001", "Source Spec", "1001", "24MM"),
            KnownSpecification(2, "24MM:JP:2007", "Target Spec", "2007", "24MM"),
        ],
    )
    refs = [r for r in result.relations if r.relation_type == "REFERENCES"]
    assert len(refs) == 1
    assert refs[0].target_specification_id == 2
    assert refs[0].status == "CONFIRMED"


def test_v2_title_alias_requires_reference_context_and_can_resolve():
    known = [
        KnownSpecification(1, "24MM:JP:1001", "Source Spec", "1001", "24MM"),
        KnownSpecification(
            2,
            "24MM:JP:2007",
            "User Profile Communication Interface Spec",
            "2007",
            "24MM",
            aliases=("用户配置通信接口式样书", "ユーザープロファイル通信I/F仕様書"),
        ),
    ]
    no_ref = EngineeringRelationExtractor().extract(
        source_specification_id=1,
        source_specification_key="24MM:JP:1001",
        units=[ExtractionUnit(content_id=3, text="用户配置通信接口式样书用于说明通信概念。")],
        known_specifications=known,
    )
    assert not [r for r in no_ref.relations if r.target_specification_id == 2]

    yes_ref = EngineeringRelationExtractor().extract(
        source_specification_id=1,
        source_specification_key="24MM:JP:1001",
        units=[ExtractionUnit(content_id=4, text="详细行为参照《用户配置通信接口式样书》。")],
        known_specifications=known,
    )
    refs = [r for r in yes_ref.relations if r.target_specification_id == 2]
    assert len(refs) == 1
    assert refs[0].status == "CONFIRMED"


def test_v2_unresolved_quoted_title_is_preserved_but_internal_section_is_ignored():
    result = EngineeringRelationExtractor().extract(
        source_specification_id=1,
        source_specification_key="24MM:JP:1001",
        units=[
            ExtractionUnit(content_id=5, text="安全处理参照《车辆隐私处理共通式样书》。"),
            ExtractionUnit(content_id=6, text="状态定义参照本书第5.1节。"),
        ],
        known_specifications=[KnownSpecification(1, "24MM:JP:1001", "Source Spec", "1001", "24MM")],
    )
    refs = [r for r in result.relations if r.relation_type == "REFERENCES"]
    assert len(refs) == 1
    assert refs[0].status == "UNRESOLVED"
    assert refs[0].target_external_key.startswith("SPEC_TITLE:")
    assert result.unresolved_count == 1


def test_v2_standard_reference_does_not_pollute_spec_graph():
    result = EngineeringRelationExtractor().extract(
        source_specification_id=1,
        source_specification_key="24MM:JP:1001",
        units=[ExtractionUnit(
            content_id=7,
            text="ISO 8601 | Date and time format standard",
            section_title="Applicable Documents",
        )],
        known_specifications=[KnownSpecification(1, "24MM:JP:1001", "Source Spec", "1001", "24MM")],
    )
    assert not [r for r in result.relations if r.relation_type == "REFERENCES"]
    assert result.stats["ignored_standard_reference_count"] >= 1
