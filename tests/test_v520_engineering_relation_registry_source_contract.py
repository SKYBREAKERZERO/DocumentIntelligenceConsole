from pathlib import Path


def test_v520_relation_registry_is_wired_into_fixed_enterprise_shell():
    root = Path(__file__).parents[1]
    gui = (root / "app" / "gui" / "application.py").read_text(encoding="utf-8")
    audit = (root / "app" / "relations" / "audit_service.py").read_text(encoding="utf-8")
    assert 'APP_VERSION = "5.2.8"' in gui
    assert '("spec_relations", "Engineering Relations")' in gui
    assert "_build_engineering_relations_page" in gui
    assert "Reconcile Unresolved" in gui
    assert "Relation Evidence Inspector" in gui
    assert "EngineeringRelationAuditService" in gui
    assert "engineering_relation_catalog" in audit
    assert "relation_evidence_detail" in audit
