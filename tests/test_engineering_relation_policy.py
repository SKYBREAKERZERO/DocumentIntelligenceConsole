from app.relations.policy import EngineeringRelationPolicyLoader


def test_relation_policy_loads_enterprise_defaults():
    policy = EngineeringRelationPolicyLoader.load(force_reload=True)
    assert policy.version == "engineering-relations-v5-toyota-dependency-graph"
    assert "REQUIREMENT" in policy.entity_types
    assert "SIGNAL" in policy.entity_types
    assert "INTERFACE" in policy.entity_types
    assert "REFERENCES" in policy.relation_types
    assert policy.llm_candidates_enabled is False
    assert policy.confirmed_only_default_graph is True
    assert policy.max_hop_depth == 3
    assert policy.max_nodes == 200
