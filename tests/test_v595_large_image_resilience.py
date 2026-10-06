from __future__ import annotations

from PIL import Image

from app.loader.image_loader import ImageLoader, ImageLoaderError
from app.parser.image_parser import ImageParser


def _raise_bomb(_path):
    try:
        raise Image.DecompressionBombError(
            "Image size (239823980 pixels) exceeds limit of 178956970 pixels, "
            "could be decompression bomb DOS attack."
        )
    except Image.DecompressionBombError as exc:
        raise ImageLoaderError(f"Invalid or unreadable image: {exc}") from exc


def test_oversized_image_is_metadata_only_warning_not_batch_failure(tmp_path, monkeypatch):
    path = tmp_path / "__Symbols.png"
    path.write_bytes(b"placeholder")

    loader = ImageLoader(enable_preprocessing=False, enable_multi_pass=False)
    monkeypatch.setattr(loader, "_read_image_metadata", _raise_bomb)
    monkeypatch.setattr(
        loader,
        "_get_ocr_engine",
        lambda: (_ for _ in ()).throw(AssertionError("OCR must not start")),
    )

    document = loader.load(path)

    assert document.blocks == []
    assert document.metadata["loader_status"] == "WARNING_OCR_SKIPPED"
    assert document.metadata["loader_warning_code"] == "IMAGE_OVERSIZED_OCR_SKIPPED"
    assert document.metadata["image_oversized"] is True
    assert document.metadata["image_pixel_count"] == 239823980
    assert document.metadata["ocr_attempted"] is False

    parsed = ImageParser().parse(document)
    assert parsed.metadata["document_role"] == "METADATA_ONLY"
    assert parsed.metadata["qa_index_policy"] == "EXCLUDE"
    assert parsed.metadata["import_status"] == "IMPORTED_WITH_WARNING"
    assert "IMAGE_OVERSIZED_OCR_SKIPPED" in parsed.metadata["import_warning_codes"]
    assert "IMAGE_OCR_NO_READABLE_TEXT" not in parsed.metadata["import_warning_codes"]


def test_non_bomb_image_loader_error_still_fails(tmp_path, monkeypatch):
    path = tmp_path / "broken.jpg"
    path.write_bytes(b"placeholder")

    loader = ImageLoader(enable_preprocessing=False, enable_multi_pass=False)

    def _raise_invalid(_path):
        raise ImageLoaderError("Invalid or unreadable image: truncated JPEG")

    monkeypatch.setattr(loader, "_read_image_metadata", _raise_invalid)

    try:
        loader.load(path)
    except ImageLoaderError as exc:
        assert "truncated JPEG" in str(exc)
    else:
        raise AssertionError("non-bomb image corruption must remain a hard failure")
