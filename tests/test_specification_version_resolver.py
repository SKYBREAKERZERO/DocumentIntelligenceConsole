from app.analyzer.specification_classifier import SpecificationClassifier
from app.model.document import Document


def test_bluetooth_audio_gets_stable_master_and_version():
    doc = Document(
        file_name="550_Bluetooth Audio function spec v2.52.pdf",
        file_type="pdf",
    )
    SpecificationClassifier(project_code="21MM").process(doc)

    assert doc.metadata["specification_code"] == "550"
    assert doc.metadata["specification_key"] == "21MM:EXCEPT_NA:550"
    assert doc.metadata["specification_version"] == "v2.52"
    assert doc.metadata["document_id"] == "21MM:EXCEPT_NA:550@v2.52"
    assert doc.metadata["spec_type"] == "BT"
    assert doc.metadata["spec_subtype"] == "BLUETOOTH_AUDIO"


def test_wifi_version_is_detected():
    doc = Document(
        file_name="540_Wi-Fi_Function_Spec_v7.60.docx",
        file_type="docx",
    )
    SpecificationClassifier(project_code="24MM").process(doc)
    assert doc.metadata["specification_key"] == "24MM:EXCEPT_NA:540"
    assert doc.metadata["specification_version"] == "v7.60"
    assert doc.metadata["spec_type"] == "WIFI"


def test_common_does_not_create_specification_master_identity():
    doc = Document(file_name="news_2026-09-03.txt", file_type="txt")
    SpecificationClassifier(project_code="COMMON").process(doc)
    assert doc.metadata["specification_key"] is None
    assert doc.metadata["specification_version"] is None
    assert doc.metadata["specification_version_status"] == "NOT_APPLICABLE"
