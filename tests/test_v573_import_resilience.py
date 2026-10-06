from __future__ import annotations

import json

from app.analyzer.document_variant_policy import DocumentVariantPolicy
from app.analyzer.import_resilience_policy import ImportResiliencePolicy
from app.analyzer.supporting_document_policy import SupportingDocumentPolicy
from app.builder.json_builder import JsonBuilder
from app.model.document import Document
from app.normalizer.postgres_safe_sanitizer import PostgresSafeSanitizer
from app.parser.pdf_parser import PDFParser
from app.parser.xlsx_parser import XLSXParser


def test_postgres_safe_sanitizer_removes_pdf_metadata_nul_without_touching_japanese():
    document = Document(
        file_name="訂正_標準データ申請書.pdf",
        file_type="pdf",
        metadata={
            "project_code": "21MM",
            "pdf_metadata": {
                "producer": "Adobe PSL 1.2e for Canon\x00",
                "title": "車両連携仕様",
            },
        },
    )

    report = PostgresSafeSanitizer.sanitize_document(document)

    assert report.removed_nul_count == 1
    assert document.metadata["pdf_metadata"]["producer"] == "Adobe PSL 1.2e for Canon"
    assert document.metadata["pdf_metadata"]["title"] == "車両連携仕様"
    assert document.metadata["postgres_sanitizer_status"] == "SANITIZED"
    assert "POSTGRES_UNICODE_SANITIZED" in document.metadata["import_warning_codes"]

    encoded = json.dumps(JsonBuilder().build(document), ensure_ascii=False)
    assert "\\u0000" not in encoded
    assert "\x00" not in encoded


def test_pdf_zero_pages_is_auditable_supporting_warning_not_failure():
    document = Document(
        file_name="車両連携表紙_添付資料.pdf",
        file_type="pdf",
        metadata={"project_code": "21MM"},
    )

    result = PDFParser().parse(document)

    assert result is document
    assert result.metadata["document_role"] == "SUPPORTING"
    assert result.metadata["qa_index_policy"] == "EXCLUDE"
    assert result.metadata["import_status"] == "IMPORTED_WITH_WARNING"
    assert "PDF_NO_PAGES" in result.metadata["import_warning_codes"]
    assert result.contents == []


def test_xlsx_no_blocks_is_metadata_only_warning_not_failure():
    document = Document(
        file_name="20200324_Car_rental_plan_Appendix_en.xlsx",
        file_type="xlsx",
        metadata={"project_code": "21MM", "sheet_names": ["Sheet1"]},
    )

    result = XLSXParser().parse(document)

    assert result is document
    assert result.metadata["document_role"] == "METADATA_ONLY"
    assert result.metadata["qa_index_policy"] == "EXCLUDE"
    assert result.metadata["import_status"] == "IMPORTED_WITH_WARNING"
    assert "XLSX_NO_BLOCKS" in result.metadata["import_warning_codes"]


def test_japanese_supporting_file_names_are_recognized():
    cover = SupportingDocumentPolicy.classify("車両連携表紙_添付資料.pdf")
    assert cover is not None
    assert cover.document_type in {"COVER_SHEET", "ATTACHMENT"}

    attachment = SupportingDocumentPolicy.classify("530_添付資料_Ver.2.51.pdf")
    assert attachment is not None
    assert attachment.document_type == "ATTACHMENT"


def test_same_version_english_japanese_pair_is_translation_variant_candidate():
    english = "156_[Common]21MM Meter-Multimedia coordination requirement specification ver2.51_en.ppt"
    japanese = "156_[共通]21MM メータ×マルチメディア連携 要求仕様ver.2.51.ppt"

    assert DocumentVariantPolicy.detect_language(english) == "EN"
    assert DocumentVariantPolicy.detect_language(japanese) == "JA"
    assert DocumentVariantPolicy.is_translation_pair(
        existing_file_name=english,
        incoming_file_name=japanese,
        existing_metadata={},
        incoming_metadata={},
    )


def test_same_language_different_content_does_not_bypass_primary_conflict_policy():
    a = "530_A_NA_21MM_ConnectedAudio_Function_Spec_Ver.2.51.docx"
    b = "530_A_NA_21MM_ConnectedAudio_Function_Spec_Ver.2.51_revised.docx"

    assert not DocumentVariantPolicy.is_translation_pair(
        existing_file_name=a,
        incoming_file_name=b,
        existing_metadata={},
        incoming_metadata={},
    )


def test_detach_metadata_only_identity_prevents_spec_version_collision():
    document = Document(
        file_name="empty_appendix.xlsx",
        file_type="xlsx",
        metadata={
            "document_role": "METADATA_ONLY",
            "qa_index_policy": "EXCLUDE",
            "specification_code": "156",
            "specification_key": "21MM:NA:156",
            "specification_name": "Some spec",
            "specification_version": "v2.51",
            "document_id": "21MM:NA:156@v2.51",
        },
    )

    ImportResiliencePolicy.detach_primary_identity(document)

    assert document.metadata["parent_specification_key"] == "21MM:NA:156"
    assert document.metadata["parent_specification_version"] == "v2.51"
    assert document.metadata["specification_key"] is None
    assert document.metadata["specification_version"] is None
    assert document.metadata["document_id"] == "empty_appendix.xlsx"

class _VariantCursor:
    def __init__(self):
        self.calls = []
        self._fetch_queue = [
            None,  # document_id pre-check introduced in v5.9.4
            (42, 100),
            (
                100,
                "21MM:NA:156@v2.51",
                "different-existing-hash",
                "156_[Common]21MM Meter-Multimedia coordination requirement specification ver2.51_en.ppt",
                {},
            ),
        ]

    def execute(self, statement, params=None):
        self.calls.append((statement, params))

    def fetchone(self):
        return self._fetch_queue.pop(0)


def test_version_manager_accepts_clear_language_variant_without_replacing_primary():
    from app.model.content import Content
    from app.storage.specification_version_manager import SpecificationVersionManager

    document = Document(
        file_name="156_[共通]21MM メータ×マルチメディア連携 要求仕様ver.2.51.ppt",
        file_type="ppt",
        contents=[Content(text="日本語版の本文")],
        metadata={
            "project_code": "21MM",
            "region_scope": "NA",
            "specification_code": "156",
            "specification_key": "21MM:NA:156",
            "specification_name": "メータ×マルチメディア連携 要求仕様",
            "specification_version": "v2.51",
            "specification_version_source": "filename",
            "document_id": "21MM:NA:156@v2.51",
        },
    )
    cur = _VariantCursor()

    plan = SpecificationVersionManager("21MM").prepare(
        cur=cur,
        document=document,
        document_id="21MM:NA:156@v2.51",
    )

    assert plan is None
    assert document.metadata["document_role"] == "TRANSLATION"
    assert document.metadata["qa_index_policy"] == "EXCLUDE"
    assert document.metadata["parent_specification_key"] == "21MM:NA:156"
    assert document.metadata["parent_specification_version"] == "v2.51"
    assert document.metadata["specification_key"] is None
    assert document.metadata["document_id"].startswith("21MM:NA:156@v2.51#TRANSLATION:JA:")

def test_postgres_storage_uses_sanitizer_at_json_boundary():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "app/storage/postgres_storage.py").read_text(encoding="utf-8")
    assert "PostgresSafeSanitizer.sanitize_document(document)" in source
    assert "cleaned, _ = PostgresSafeSanitizer.sanitize_value(value)" in source
