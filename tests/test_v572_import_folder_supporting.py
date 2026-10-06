from pathlib import Path


def _read(rel: str) -> str:
    root = Path(__file__).resolve().parents[1]
    return (root / rel).read_text(encoding='utf-8')


def test_gui_product_title_has_no_visible_enterprise_or_version_suffix():
    app = _read('app/gui/application.py')
    assert 'APP_NAME = "文档智能控制台"' in app
    assert 'self.root.title(APP_NAME)' in app
    assert 'text="＋  Add Folder"' in app
    assert 'def select_folder_files' in app
    assert 'text="＋  Add JSON Folder"' in app
    assert 'def select_json_folder' in app


def test_qdrant_current_sync_excludes_supporting_documents():
    source = _read('app/rag/qdrant_sync.py')
    assert "metadata ->> 'document_role'" in source
    assert "= 'SUPPORTING'" in source
    assert "metadata ->> 'qa_index_policy'" in source
    assert "= 'EXCLUDE'" in source


def test_storage_bypasses_spec_version_for_supporting_documents():
    storage = _read('app/storage/postgres_storage.py')
    manager = _read('app/storage/specification_version_manager.py')
    assert 'SupportingDocumentPolicy.apply(document)' in storage
    assert 'not ImportResiliencePolicy.is_non_versioned(document)' in storage
    assert 'if ImportResiliencePolicy.is_non_versioned(document):' in manager
    assert 'return None' in manager
