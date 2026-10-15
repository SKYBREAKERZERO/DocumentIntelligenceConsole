"""Privacy-preserving synthetic Stage 1 pipeline regression; no company documents."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.analyzer.title_detector import TitleDetector
from app.engineering_intelligence.pipeline_json_review import (
    read_pipeline_files, review_pipeline_documents,
)
from scripts.review_stage1_pipeline_json import main


def primary():
    return {
        "document": {"document_id": "secret-primary@v2.52", "file_name": "Bluetooth Audio function spec v2.52.pdf",
                     "project_code": "21MM"},
        "metadata": {"source_format": "pdf", "specification_code": None,
                     "specification_version": "v2.52"},
        "chapters": [{"id":"2"}],
        "sections": [
            {"id":"2.2.2", "title_jp":"Expected behavior", "page_number":19},
            {"id":"2.9", "title_jp":"Now Playing 参照", "page_number":20},
            {"id":"2.2.3", "title_jp":"Volume adjustment", "page_number":22},
        ],
        "contents": [{"section_id":"2.9", "page_number":20,
                      "text":"SUPPLIER-CONFIDENTIAL.\n0x01\n" + "○\n"*21 + "Parameter Value"}],
    }


def cover():
    return {
        "document": {"document_id":"internal-cover", "file_name":"550_Bluetooth Audio Function Spec_Cover_Sheet_Ver.2.50.xlsx",
                     "project_code":"21MM"},
        "metadata": {"source_format":"xlsx", "document_role":"SUPPORTING",
                     "supporting_document_type":"COVER_SHEET", "qa_index_policy":"EXCLUDE",
                     "parent_specification_version":"v2.50", "xlsx_page_number_semantics":"logical_sheet_number"},
        "chapters": [], "sections": [{"id":"1.1", "title_jp":"Cover"}],
        "contents": [{"section_id":"1.1", "text":"[Header row 6] SpecID. | 550 | Ver. | 2.52"}],
    }


def test_cross_reference_as_title_is_rejected_without_banning_heading_reference_methods():
    assert TitleDetector.detect("2.9 Now Playing 参照") is None
    assert TitleDetector.detect("2.9 Now Playingを参照") is None
    assert TitleDetector.detect("2.9 参照方法") is not None
    assert TitleDetector.detect("2.2.3 Playback volume specification") is not None


def test_real_world_pipeline_shape_returns_review_only_and_redacted_report():
    data=review_pipeline_documents([cover(),primary()])
    assert data["status"]=="DIAGNOSTIC_ONLY"
    assert len(data["identity_candidates"])==1
    candidate=data["identity_candidates"][0]
    assert candidate["candidate_specification_code"]=="550"
    assert candidate["candidate_version"]=="v2.52"
    assert not candidate["automatically_assign_identity"]
    types={f["code"] for f in data["findings"]}
    assert "CROSS_REFERENCE_MISREAD_AS_SECTION_HEADING" in types
    assert "OUT_OF_ORDER_SECTION_NUMBER_REQUIRES_REVIEW" in types
    assert "COVER_FILENAME_VERSION_DIFFERS_FROM_SHEET_VERSION" in types
    assert "PRIMARY_SPECIFICATION_CODE_UNRESOLVED" in types
    encoded=json.dumps(data,ensure_ascii=False)
    for private in ("secret-primary", "internal-cover", "SUPPLIER-CONFIDENTIAL", "Bluetooth Audio function spec"):
        assert private not in encoded
    assert not data["source_verified"] and not data["quality_metrics_available"]
    assert not data["database_modified"] and not data["qdrant_modified"]


def test_unsupported_or_ambiguous_identity_fails_closed():
    c=cover();p=primary()
    c["contents"].append({"section_id":"1.1", "text":"SpecID. | 551 | Ver. | 2.52"})
    report=review_pipeline_documents([c,p])
    assert not report["identity_candidates"]
    assert any(f["code"]=="COVER_VISIBLE_IDENTITIES_AMBIGUOUS" for f in report["findings"])
    c=cover();p=primary();p["metadata"]["specification_version"]="v2.53"
    assert review_pipeline_documents([c,p])["identity_candidates"]==[]
    c=cover();p=primary();p["document"]["project_code"]="24MM"
    assert review_pipeline_documents([c,p])["identity_candidates"]==[]


def test_rejects_duplicates_unknown_document_type_and_resource_budgets():
    with pytest.raises(ValueError,match="Duplicate"):
        review_pipeline_documents([cover(),cover()])
    with pytest.raises(ValueError,match="Content budget"):
        review_pipeline_documents([cover(),primary()],max_contents=1)
    with pytest.raises(ValueError,match="Text budget"):
        review_pipeline_documents([cover(),primary()],max_chars=10)
    with pytest.raises(ValueError,match="bounded"):
        review_pipeline_documents([])
    broken=primary();broken["document"]["project_code"]="unknown"
    with pytest.raises(ValueError,match="scoped"):
        review_pipeline_documents([broken])


def test_cli_creates_private_report_once_and_does_not_echo_text(tmp_path:Path,capsys):
    a=tmp_path/"original-private-cover.json"
    b=tmp_path/"original-private-spec.json"
    out=tmp_path/"redacted-review.json"
    a.write_text(json.dumps(cover()), encoding="utf-8")
    b.write_text(json.dumps(primary()), encoding="utf-8")
    assert main(["--json",str(a),"--json",str(b),"--report-out",str(out)])==0
    assert "original-private" not in capsys.readouterr().out
    report=json.loads(out.read_text(encoding="utf-8"))
    assert report["document_count"]==2
    assert "SUPPLIER-CONFIDENTIAL" not in out.read_text(encoding="utf-8")
    assert main(["--json",str(a),"--report-out",str(out)])==2


def test_reader_rejects_duplicate_json_keys(tmp_path:Path):
    doc=tmp_path/"dup.json"
    doc.write_text('{"document":{"document_id":"a","document_id":"b"}}',encoding="utf-8")
    with pytest.raises(ValueError,match="Duplicate JSON"):
        read_pipeline_files([doc])
