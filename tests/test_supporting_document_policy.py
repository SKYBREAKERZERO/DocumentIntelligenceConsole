from app.analyzer.supporting_document_policy import SupportingDocumentPolicy
from app.model.document import Document


def test_coversheet_detects_parent_identity_without_becoming_primary_spec():
    name = "530_A_NA_21MM_ConnectedAudio_Function_Spec[NA]_Ver.2.51_CoverSheet.docx"
    match = SupportingDocumentPolicy.classify(name)
    assert match is not None
    assert match.document_type == "COVER_SHEET"
    assert match.parent_specification_code == "530"
    assert match.parent_specification_version == "v2.51"

    document = Document(file_name=name, file_type="docx", metadata={
        "specification_code": "530",
        "specification_key": "21MM:NA:530",
        "specification_version": "v2.51",
        "document_id": "21MM:NA:530@v2.51",
    })
    SupportingDocumentPolicy.apply(document)
    assert document.metadata["document_role"] == "SUPPORTING"
    assert document.metadata["qa_index_policy"] == "EXCLUDE"
    assert document.metadata["specification_key"] is None
    assert document.metadata["specification_version"] is None
    assert document.metadata["document_id"] == name


def test_primary_spec_is_not_misclassified_as_supporting():
    name = "530_A_NA_21MM_ConnectedAudio_Function_Spec[NA]_Ver.2.51.docx"
    assert SupportingDocumentPolicy.classify(name) is None


def test_common_supporting_filename_variants():
    names = {
        "408_User_Profile_CheckSheet_Ver.1.00.xlsx": "CHECK_SHEET",
        "703_HU_Test Sheet_v2.1.docx": "TEST_SHEET",
        "111_ReviewSheet_v1.2.xlsx": "REVIEW_SHEET",
        "190_ReferenceList_v3.0.docx": "REFERENCE_LIST",
    }
    for name, expected in names.items():
        match = SupportingDocumentPolicy.classify(name)
        assert match is not None
        assert match.document_type == expected
