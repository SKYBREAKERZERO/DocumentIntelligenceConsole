from pathlib import Path


def test_content_model_carries_source_metadata_but_version_hash_excludes_it():
    root = Path(__file__).parents[1]
    content_source = (root / "app" / "model" / "content.py").read_text(encoding="utf-8")
    version_source = (root / "app" / "storage" / "specification_version_manager.py").read_text(encoding="utf-8")
    assert "metadata: dict[str, object]" in content_source
    assert "v5.2 adds Content.metadata" in version_source
