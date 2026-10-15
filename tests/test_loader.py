"""PDF loader smoke regression with a hermetic, generated two-page fixture.

Old test imported input/test.pdf at collection time; it was not shipped.
Do not add user-owned PDFs or dummy business fixtures to the release archive.
"""
from __future__ import annotations

import fitz

from app.loader.loader_factory import LoaderFactory


def test_pdf_loader_preserves_page_order_and_text(tmp_path):
    pdf_path = tmp_path / "loader-smoke.pdf"
    pdf = fitz.open()
    page = pdf.new_page()
    page.insert_text((72, 96), "DCM initial value")
    page = pdf.new_page()
    page.insert_text((72, 96), "After DCM communication")
    pdf.save(pdf_path)
    pdf.close()

    loader = LoaderFactory.get_loader(pdf_path)
    document = loader.load(str(pdf_path))

    assert document.file_type == "pdf"
    assert len(document.pages) == 2
    assert [page.page_number for page in document.pages] == [1, 2]
    assert "DCM initial value" in document.pages[0].text
    assert "After DCM communication" in document.pages[1].text
    assert document.metadata["page_count"] == 2
    assert document.metadata["empty_page_count"] == 0
