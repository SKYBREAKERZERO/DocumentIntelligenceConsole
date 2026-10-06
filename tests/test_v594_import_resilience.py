from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.loader.image_loader import ImageLoader
from app.loader.txt_loader import TXTLoader
from app.model.content import Content
from app.model.document import Document
from app.parser.image_parser import ImageParser
from app.parser.txt_parser import TXTParser
from app.storage.specification_version_manager import (
    SpecificationVersionConflictError,
    SpecificationVersionManager,
)


def test_image_loader_no_ocr_text_is_auditable_not_failure(tmp_path, monkeypatch):
    path = tmp_path / "__Symbols.png"
    path.write_bytes(b"placeholder")

    loader = ImageLoader(enable_preprocessing=False, enable_multi_pass=False)
    monkeypatch.setattr(loader, "_read_image_metadata", lambda _path: (640, 480, "RGB"))
    monkeypatch.setattr(
        loader,
        "_get_ocr_engine",
        lambda: (lambda _path: SimpleNamespace(txts=[], scores=[], boxes=None, elapse=0.01)),
    )

    document = loader.load(path)
    assert document.blocks == []
    assert document.metadata["loader_status"] == "WARNING_NO_EXTRACTABLE_TEXT"
    assert document.metadata["loader_warning_code"] == "IMAGE_OCR_NO_READABLE_TEXT"

    parsed = ImageParser().parse(document)
    assert parsed.metadata["document_role"] == "METADATA_ONLY"
    assert parsed.metadata["qa_index_policy"] == "EXCLUDE"
    assert parsed.metadata["import_status"] == "IMPORTED_WITH_WARNING"
    assert "IMAGE_OCR_NO_READABLE_TEXT" in parsed.metadata["import_warning_codes"]


def test_txt_loader_empty_file_is_auditable_not_failure(tmp_path):
    path = tmp_path / "empty.txt"
    path.write_text(" \n\t\n", encoding="utf-8")

    document = TXTLoader().load(path)
    assert document.blocks == []
    assert document.metadata["loader_status"] == "WARNING_NO_EXTRACTABLE_TEXT"
    assert document.metadata["loader_warning_code"] == "TXT_NO_READABLE_TEXT"

    parsed = TXTParser().parse(document)
    assert parsed.metadata["document_role"] == "METADATA_ONLY"
    assert parsed.metadata["qa_index_policy"] == "EXCLUDE"
    assert parsed.metadata["import_status"] == "IMPORTED_WITH_WARNING"
    assert "TXT_NO_READABLE_TEXT" in parsed.metadata["import_warning_codes"]


class _DocumentIdentityCursor:
    def __init__(self, row):
        self.row = row
        self.calls = []
        self.fetchone_calls = 0

    def execute(self, statement, params=None):
        self.calls.append((str(statement), params))

    def fetchone(self):
        self.fetchone_calls += 1
        if self.fetchone_calls == 1:
            return self.row
        raise AssertionError("prepare() should return/raise from document_id pre-check")


def _versioned_document() -> Document:
    return Document(
        file_name="Appendix 9_Test.xlsx",
        file_type="xlsx",
        contents=[Content(text="stable business content")],
        metadata={
            "project_code": "21MM",
            "region_scope": "NA",
            "specification_code": "254",
            "specification_key": "21MM:NA:254",
            "specification_name": "Voice sequence",
            "specification_version": "v2.51",
            "specification_version_source": "filename",
            "document_id": "Appendix 9_Test.xlsx",
        },
    )


def test_version_manager_reuses_existing_document_id_when_content_is_identical(monkeypatch):
    document = _versioned_document()
    manager = SpecificationVersionManager("21MM")
    digest = manager.document_content_hash(document)
    cur = _DocumentIdentityCursor(
        (901, 42, "21MM:NA:254", "v2.51", digest, "Appendix 9_Test.xlsx")
    )

    monkeypatch.setattr(
        manager,
        "_upsert_master",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must not create/update master on idempotent document_id retry")),
    )

    plan = manager.prepare(
        cur=cur,
        document=document,
        document_id="Appendix 9_Test.xlsx",
    )

    assert plan is not None
    assert plan.duplicate is True
    assert plan.existing_version_id == 901
    assert plan.existing_document_id == "Appendix 9_Test.xlsx"


def test_version_manager_reports_document_id_conflict_before_postgres_unique_violation(monkeypatch):
    document = _versioned_document()
    manager = SpecificationVersionManager("21MM")
    cur = _DocumentIdentityCursor(
        (901, 42, "21MM:NA:254", "v2.51", "different-hash", "Appendix 9_Test.xlsx")
    )

    monkeypatch.setattr(
        manager,
        "_upsert_master",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must not touch master on immutable document_id conflict")),
    )

    with pytest.raises(SpecificationVersionConflictError) as exc_info:
        manager.prepare(
            cur=cur,
            document=document,
            document_id="Appendix 9_Test.xlsx",
        )

    message = str(exc_info.value)
    assert "Specification version conflict:" in message
    assert "document_id 'Appendix 9_Test.xlsx'" in message
    assert "already bound" in message
    assert "duplicate key" not in message.lower()
