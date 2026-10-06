from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_configuration_control_plane_schema_is_isolated_and_versioned():
    source = (ROOT / "app/storage/configuration_schema_manager.py").read_text(encoding="utf-8")
    for token in (
        'CONFIG_SCHEMA = "app_config"',
        'CONFIG_SCHEMA_VERSION = 1',
        'app_config.app_settings',
        'app_config.project_scopes',
        'app_config.ai_model_profiles',
        'app_config.embedding_profiles',
        'app_config.rag_runtime_profiles',
        'app_config.vector_collection_registry',
        'app_config.prompt_templates',
        'app_config.prompt_versions',
        'app_config.data_quality_rules',
        "WHERE component = 'configuration'",
        "VALUES ('configuration', %s, %s, NOW())",
    ):
        assert token in source


def test_configuration_defaults_are_idempotent_and_secret_safe():
    source = (ROOT / "app/storage/configuration_schema_manager.py").read_text(encoding="utf-8")
    assert source.count("ON CONFLICT") >= 8
    assert "credential_reference" in source
    assert "setting_value IS NULL AND credential_reference IS NOT NULL" in source
    assert "BAAI/bge-m3" in source
    assert "1024" in source
    assert "current-index-v1" in source


def test_enterprise_initialize_button_runs_core_and_configuration_migrations():
    source = (ROOT / "app/gui/application.py").read_text(encoding="utf-8")
    for token in (
        "Initialize / Upgrade Enterprise Database",
        "ConfigurationSchemaManager",
        "config_manager.ensure_schema()",
        "Configuration control plane",
        "existing settings are preserved",
        "Qdrant points and embeddings are not rebuilt",
    ):
        assert token in source


def test_configuration_manager_has_no_destructive_sql_statements():
    source = (ROOT / "app/storage/configuration_schema_manager.py").read_text(encoding="utf-8")
    # Ignore documentation text and inspect executable SQL patterns only.
    upper = source.upper()
    assert 'CURSOR.EXECUTE("DROP ' not in upper
    assert "CURSOR.EXECUTE('DROP " not in upper
    assert 'CURSOR.EXECUTE("TRUNCATE ' not in upper
    assert "CURSOR.EXECUTE('TRUNCATE " not in upper
    assert 'CURSOR.EXECUTE("DELETE ' not in upper
    assert "CURSOR.EXECUTE('DELETE " not in upper
