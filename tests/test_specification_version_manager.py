from app.model.content import Content
from app.model.document import Document
from app.storage.specification_version_manager import SpecificationVersionManager


def test_numeric_version_comparison():
    compare = SpecificationVersionManager.compare_version_labels
    assert compare("v2.53", "v2.52") == 1
    assert compare("v2.51", "v2.52") == -1
    assert compare("v7.60", "v7.6") == 1


def test_revision_version_comparison():
    compare = SpecificationVersionManager.compare_version_labels
    assert compare("Rev.B", "Rev.A") == 1
    assert compare("R04", "R03") == 1


def test_document_content_hash_changes_with_content():
    a = Document(file_name="a.pdf", file_type="pdf", contents=[Content(text="A")])
    b = Document(file_name="a.pdf", file_type="pdf", contents=[Content(text="B")])
    assert SpecificationVersionManager.document_content_hash(a) != SpecificationVersionManager.document_content_hash(b)
