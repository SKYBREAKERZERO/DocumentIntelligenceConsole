from pathlib import Path


def test_schema_v6_contains_version_diff_and_current_views():
    source = (Path(__file__).parents[1] / "app" / "storage" / "schema_manager.py").read_text(encoding="utf-8")
    assert "RAG_SCHEMA_VERSION = 6" in source
    assert "CREATE TABLE IF NOT EXISTS public.specifications" in source
    assert "CREATE TABLE IF NOT EXISTS public.specification_versions" in source
    assert "CREATE TABLE IF NOT EXISTS public.specification_change_sets" in source
    assert "CREATE TABLE IF NOT EXISTS public.specification_changes" in source
    assert "version_sort_key" in source
    assert "CREATE OR REPLACE VIEW public.rag_current_chunks" in source
    assert "CREATE OR REPLACE VIEW public.specification_catalog" in source
    assert "CREATE OR REPLACE VIEW public.specification_version_history" in source
    assert "CREATE OR REPLACE VIEW public.specification_change_summary" in source
    assert "CREATE OR REPLACE VIEW public.specification_change_detail" in source
    assert "fk_specifications_current_version" in source
