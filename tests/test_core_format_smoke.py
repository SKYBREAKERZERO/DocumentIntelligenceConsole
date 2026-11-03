"""Real format parsers + JSON writer, synthetic files only; SQL is forbidden."""
import json
from pathlib import Path

import pytest

BODY="The controller shall stop playback within 500 ms after ignition becomes OFF. "*8


@pytest.mark.parametrize("suffix",[".txt",".pdf",".docx",".xlsx",".pptx"])
def test_real_format_to_json_without_database(tmp_path,suffix,monkeypatch):
    source=tmp_path/("synthetic_document"+suffix)
    if suffix==".txt":
        source.write_text("1 Requirements\n"+BODY,encoding="utf-8")
    elif suffix==".pdf":
        fitz=pytest.importorskip("fitz")
        with fitz.open() as doc:
            page=doc.new_page()
            page.insert_textbox(fitz.Rect(50,50,540,740),"1 Requirements\n"+BODY,fontsize=11)
            doc.save(source)
    elif suffix==".docx":
        docx=pytest.importorskip("docx")
        doc=docx.Document()
        doc.add_heading("1 Requirements",level=1)
        doc.add_paragraph(BODY)
        doc.save(source)
    elif suffix==".xlsx":
        openpyxl=pytest.importorskip("openpyxl")
        book=openpyxl.Workbook()
        book.active.title="Requirements"
        book.active.append(["Item","Requirement"])
        book.active.append(["Playback",BODY])
        book.save(source)
        book.close()
    else:
        pptx=pytest.importorskip("pptx")
        from pptx.util import Inches
        deck=pptx.Presentation()
        slide=deck.slides.add_slide(deck.slide_layouts[6])
        slide.shapes.add_textbox(Inches(1),Inches(1),Inches(8),Inches(5)).text_frame.text="1 Requirements\n"+BODY
        deck.save(source)
    import psycopg
    def no_database(*args,**kwargs):
        raise AssertionError("JSON-only parser must not connect to PostgreSQL")
    monkeypatch.setattr(psycopg,"connect",no_database)
    from app.pipeline.pipeline_factory import PipelineFactory
    pipeline=PipelineFactory.create(source,save_json=True,save_database=False,project_code="COMMON")
    output=tmp_path/"out.json"
    document=pipeline.run(file_path=source,output=output)
    assert document is not None
    assert output.is_file()
    payload=json.loads(output.read_text(encoding="utf-8"))
    assert "500" in json.dumps(payload)
    assert isinstance(payload,dict)
    if suffix==".pdf":
        assert payload["metadata"]["unstructured_page_fallback"] is True
        assert all(row["page_number"]==1 for row in payload["contents"])


def test_unstructured_spec_pdf_is_warning_not_invented_chapters(tmp_path):
    fitz=pytest.importorskip("fitz")
    source=tmp_path/"unstructured.pdf"
    with fitz.open() as doc:
        page=doc.new_page()
        page.insert_textbox(fitz.Rect(50,50,540,740),"1 Requirements\n"+BODY,fontsize=11)
        doc.save(source)
    from app.pipeline.pipeline_factory import PipelineFactory
    pipeline=PipelineFactory.create(source,save_json=True,save_database=False,project_code="21MM")
    output=tmp_path/"out.json"
    document=pipeline.run(file_path=source,output=output)
    assert not document.contents
    assert document.metadata["qa_index_policy"]=="EXCLUDE"
    assert "PDF_NO_STRUCTURED_BODY" in document.metadata["import_warning_codes"]
