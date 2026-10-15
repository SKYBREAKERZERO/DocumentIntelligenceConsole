"""Offline ingestion dispatcher tests; importing this file never reads user PDFs."""
from __future__ import annotations

import pytest

from app.pipeline.ingestion_pipeline import IngestionPipeline


@pytest.mark.parametrize("ext", ["pdf", "docx", "pptx", "xlsx"])
def test_supported_format_detection(ext):
    assert IngestionPipeline.supports(f"document.{ext.upper()}")


def test_unsupported_format_rejected(tmp_path):
    invalid = tmp_path / "not-supported.exe"
    invalid.write_bytes(b"not a PDF")
    with pytest.raises(ValueError, match="Unsupported"):
        IngestionPipeline().run(invalid, tmp_path / "out.json")


def test_dispatch_without_creating_external_services(tmp_path):
    source = tmp_path / "doc.pdf"
    source.write_bytes(b"dummy content for dispatcher test")
    output = tmp_path / "out.json"
    calls = []

    class FakePipeline:
        def run(self, file_path, output_path):
            calls.append((file_path, output_path))
            return {"document": "fake"}

    dispatcher = IngestionPipeline()
    dispatcher._pipelines[".pdf"] = FakePipeline()
    assert dispatcher.run(source, output) == {"document": "fake"}
    assert calls == [(source, output)]
    assert not output.exists()
