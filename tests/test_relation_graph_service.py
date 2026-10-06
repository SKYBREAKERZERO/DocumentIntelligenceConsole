from app.relations.graph_service import EngineeringRelationGraphService


def _rows():
    return [
        {
            "relation_id": 11,
            "source_specification_key": "24MM:NA:1001",
            "source_specification_name": "Main Function Spec",
            "source_version": "Rev1.0",
            "target_specification_id": 1,
            "target_specification_key": "24MM:UNSPECIFIED:2007",
            "target_specification_name": "Communication Spec",
            "relation_type": "REFERENCES",
            "status": "CONFIRMED",
            "confidence": 1.0,
            "evidence_count": 11,
        },
        {
            "relation_id": 1,
            "source_specification_key": "24MM:UNSPECIFIED:2007",
            "source_specification_name": "Communication Spec",
            "source_version": "Rev2.1",
            "target_specification_id": 2,
            "target_specification_key": "24MM:NA:1001",
            "target_specification_name": "Main Function Spec",
            "relation_type": "REFERENCES",
            "status": "CONFIRMED",
            "confidence": 1.0,
            "evidence_count": 12,
        },
        {
            "relation_id": 12,
            "source_specification_key": "24MM:NA:1001",
            "source_specification_name": "Main Function Spec",
            "source_version": "Rev1.0",
            "target_external_key": "SPEC:3999",
            "relation_type": "REFERENCES",
            "status": "UNRESOLVED",
            "confidence": 0.985,
            "evidence_count": 1,
        },
    ]


def test_graph_defaults_to_confirmed_reference_edges():
    graph = EngineeringRelationGraphService.build(_rows())
    assert len(graph.nodes) == 2
    assert {edge.relation_id for edge in graph.edges} == {1, 11}


def test_graph_can_include_unresolved_reference():
    graph = EngineeringRelationGraphService.build(_rows(), include_unresolved=True)
    assert {edge.relation_id for edge in graph.edges} == {1, 11, 12}
    assert any(node.kind == "EXTERNAL" and node.label == "3999" for node in graph.nodes)


def test_bounded_root_graph_handles_cycles():
    root = "SPEC::24MM:NA:1001"
    graph = EngineeringRelationGraphService.build(_rows(), root_id=root, depth=1)
    assert len(graph.nodes) == 2
    assert {edge.relation_id for edge in graph.edges} == {1, 11}
