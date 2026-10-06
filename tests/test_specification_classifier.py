from __future__ import annotations

import unittest

from app.analyzer.specification_classifier import SpecificationClassifier
from app.model.content import Content
from app.model.document import Document


class SpecificationClassifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.classifier = SpecificationClassifier()

    def test_recognizes_21up_bt_from_file_name(self) -> None:
        document = Document(
            file_name="501_21UP_BT_Function_Specification_v2.1.docx",
            file_type="docx",
        )

        result = self.classifier.process(document)

        self.assertEqual(result.metadata["series"], "21UP")
        self.assertEqual(result.metadata["spec_type"], "BT")
        self.assertEqual(
            result.metadata["specification_classifier_status"],
            "SUCCESS",
        )
        self.assertEqual(
            result.metadata["specification_classification"]["series"]["source"],
            "file_name",
        )


    def test_wifi_function_spec_uses_wifi_type(self) -> None:
        document = Document(
            file_name="24CY_Wi-Fi_Function_Spec_v7.60.docx",
            file_type="docx",
            contents=[
                Content(text="Refer to Bluetooth specification for coexistence behavior."),
            ],
        )

        result = self.classifier.process(document)

        self.assertEqual(result.metadata["series"], "24CY")
        self.assertEqual(result.metadata["spec_type"], "WIFI")
        self.assertIsNone(result.metadata["spec_subtype"])
        self.assertEqual(
            result.metadata["specification_classification"]["spec_type"]["source"],
            "file_name",
        )

    def test_recognizes_24mm_carplay_from_existing_style_name(self) -> None:
        document = Document(
            file_name="547_24MM_CarPlay_function_specification_ver.26.01.15.0.pptx",
            file_type="pptx",
        )

        result = self.classifier.process(document)

        self.assertEqual(result.metadata["series"], "24MM")
        self.assertEqual(result.metadata["spec_type"], "CARPLAY")

    def test_series_pattern_supports_value_immediately_followed_by_version(self) -> None:
        document = Document(
            file_name="Appendix_24MMv3.5.xlsx",
            file_type="xlsx",
        )

        result = self.classifier.process(document)

        self.assertEqual(result.metadata["series"], "24MM")

    def test_body_reference_does_not_classify_unidentified_document(self) -> None:
        document = Document(
            file_name="meeting_notes.txt",
            file_type="txt",
            contents=[
                Content(
                    text="Refer to Bluetooth and HMI specifications.",
                )
            ],
        )

        result = self.classifier.process(document)

        self.assertIsNone(result.metadata["series"])
        self.assertIsNone(result.metadata["spec_type"])
        self.assertEqual(
            result.metadata["specification_classifier_status"],
            "UNRESOLVED",
        )


    def test_bluetooth_audio_scheme_a_uses_bt_parent_and_subtype(self) -> None:
        document = Document(
            file_name="Bluetooth Audio function spec v2.52.pdf",
            file_type="pdf",
        )

        result = self.classifier.process(document)

        self.assertEqual(result.metadata["spec_type"], "BT")
        self.assertEqual(result.metadata["spec_subtype"], "BLUETOOTH_AUDIO")
        self.assertEqual(
            result.metadata["specification_classification"]["spec_type"]["status"],
            "DERIVED_FROM_SUBTYPE",
        )
        self.assertEqual(
            result.metadata["specification_classifier_status"],
            "PARTIAL",
        )

    def test_referenced_hmi_does_not_override_bluetooth_audio_identity(self) -> None:
        document = Document(
            file_name="Bluetooth Audio function spec v2.52.pdf",
            file_type="pdf",
            contents=[
                Content(text="Refer to Audio HMI specification and Bluetooth specification."),
            ],
        )

        result = self.classifier.process(document)

        self.assertEqual(result.metadata["spec_type"], "BT")
        self.assertEqual(result.metadata["spec_subtype"], "BLUETOOTH_AUDIO")

    def test_conflicting_types_in_same_file_name_are_ambiguous(self) -> None:
        document = Document(
            file_name="21UP_BT_HMI_Interface_Specification.docx",
            file_type="docx",
        )

        result = self.classifier.process(document)

        self.assertEqual(result.metadata["series"], "21UP")
        self.assertIsNone(result.metadata["spec_type"])
        self.assertEqual(
            result.metadata["specification_classification"]["spec_type"]["status"],
            "AMBIGUOUS",
        )

    def test_explicit_override_wins(self) -> None:
        document = Document(
            file_name="21UP_BT_Function_Specification.docx",
            file_type="docx",
            metadata={
                "series_override": "21MM",
                "spec_type_override": "DTV",
            },
        )

        result = self.classifier.process(document)

        self.assertEqual(result.metadata["series"], "21MM")
        self.assertEqual(result.metadata["spec_type"], "DTV")
        self.assertEqual(
            result.metadata["specification_classification"]["series"]["status"],
            "OVERRIDE",
        )

    def test_except_na_550_exact_rule(self) -> None:
        document = Document(
            file_name="550_Bluetooth Audio Function Spec_Cov.docx",
            file_type="docx",
        )
        result = self.classifier.process(document)
        self.assertEqual(result.metadata["region_scope"], "EXCEPT_NA")
        self.assertEqual(result.metadata["spec_type"], "BT")
        self.assertEqual(result.metadata["spec_subtype"], "BLUETOOTH_AUDIO")

    def test_same_number_555_is_disambiguated_by_na_catalog(self) -> None:
        na = Document(
            file_name="555_NA_21MM_Virtual_Assistant_Spec[NA].docx",
            file_type="docx",
        )
        except_na = Document(
            file_name="555_VUI Function Spec.docx",
            file_type="docx",
        )

        na_result = self.classifier.process(na)
        except_result = self.classifier.process(except_na)

        self.assertEqual(na_result.metadata["region_scope"], "NA")
        self.assertEqual(na_result.metadata["spec_type"], "VIRTUAL_ASSISTANT")
        self.assertEqual(except_result.metadata["region_scope"], "EXCEPT_NA")
        self.assertEqual(except_result.metadata["spec_type"], "VUI")



if __name__ == "__main__":
    unittest.main()
