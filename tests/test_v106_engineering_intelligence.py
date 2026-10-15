from __future__ import annotations

from dataclasses import replace

import pytest

from app.engineering_intelligence import (
    extract_requirement_units,
    align_requirement_versions,
    link_explicit_references,
)
from app.engineering_intelligence.persistence import schema_ready


def units(text: str, *, doc: str = "D1", section: str = "4.1", content_id: int = 1):
    return extract_requirement_units(
        document_id=doc, content_id=content_id, section_id=section,
        page_number=5, text=text,
    )


def test_preserve_exact_unicode_source_offsets_and_hashes():
    text = "  When HFP connects, Memory ID shall be selected within 5s.  \n\n  ただし、NA の場合は例外とする。\n"
    result = units(text)
    assert len(result) == 2
    assert all(text[x.start_offset:x.end_offset] == x.text for x in result)
    assert result[0].signals.normative
    assert result[0].signals.conditional
    assert result[0].signals.timing == ("5s",)
    assert result[1].signals.exception
    assert result[0].page_number == 5
    assert result == units(text)
    assert result != units(text, doc="D2")


def test_fuzzy_text_is_not_confirmed_normative_requirement():
    result = units("Bluetooth device recognition is described here.")
    assert result[0].unit_type == "SOURCE_TEXT"
    assert result[0].signals.normative is False


def test_no_split_on_decimal_or_ids():
    assert len(units("REQ-ID: 4.2; wait 3.5s.")) == 1


def test_empty_and_bounds():
    assert units("\n \n") == ()
    assert units("A\nB\nC")[:2] == units("A\nB\nC", content_id=1)[:2]
    with pytest.raises(ValueError, match="exceeds 2 requirement units"):
        extract_requirement_units(document_id="x",content_id=1,section_id="1",text="A\nB\nC",max_units=2)
    assert extract_requirement_units(document_id="",content_id=1,section_id="1",text="foo") == ()


def test_exact_cross_version_keeps_and_moves():
    a = units("Memory ID shall follow BT.")
    b = units("Memory ID shall follow BT.", doc="D2", section="5.1")
    found = align_requirement_versions(a, b)
    assert len(found) == 1
    assert found[0].change_type == "MOVED"
    assert found[0].review_status == "DETERMINISTIC"


def test_approximate_change_requires_review():
    a = units("Memory ID shall be selected when Bluetooth connects.")
    b = units("Memory ID shall be selected when Bluetooth reconnects.", doc="D2")
    found = align_requirement_versions(a, b, threshold=.65)
    assert len(found) == 1
    assert found[0].change_type == "MODIFIED"
    assert found[0].review_status == "REVIEW_REQUIRED"


def test_unmatched_units_not_lost():
    found = align_requirement_versions(units("A"), units("B", doc="D2"))
    assert {x.change_type for x in found} == {"ADDED", "REMOVED"}


def test_relations_require_explicit_spec_and_section():
    source = units("Refer to Spec 550 section 4.1 for Memory ID conditions.")
    target = units("Memory ID must use detected User ID.",doc="D2")
    relations = link_explicit_references(
        source_spec_code="508", source_units=source,
        target_spec_code="550", target_units=target,
    )
    assert len(relations)==1
    assert relations[0].status=="REVIEW_REQUIRED"
    assert relations[0].reason=="EXPLICIT_SPEC_AND_SECTION_ANCHOR"
    assert relations[0] == link_explicit_references(source_spec_code="508",source_units=source,target_spec_code="550",target_units=target)[0]


@pytest.mark.parametrize('text', [
    "Bluetooth 550ms signal is required.",
    "550 is similar to 508.",
    "See specification 550 for more information.",
    "Refer to Spec 542 section 4.1 for requirements.",
])
def test_no_unjustified_content_relation(text):
    found = link_explicit_references(
        source_spec_code="508",source_units=units(text),
        target_spec_code="550",target_units=units("Bluetooth shall be active.",doc="D2"),
    )
    assert not found


def test_relations_not_resolved_on_wrong_section():
    found = link_explicit_references(source_spec_code="508", source_units=units("Refer to Spec 550 section 4.1"),
                                     target_spec_code="550",target_units=units("User Profile",doc="D2",section="5.2"))
    assert found == ()


def test_schema_check_nonmutating_cursor():
    class Cursor:
        def execute(self, q):
            assert "to_regclass" in q
        def fetchone(self):
            return (None,) * 6
    assert schema_ready(Cursor()) is False


def test_alignment_budget_exhaustion_fails_closed():
    before=units('A will be modified.\nB will be modified.')
    after=units('A has been modified.\nB has been modified.',doc='D2')
    with pytest.raises(ValueError, match='budget'):
        align_requirement_versions(before,after,threshold=.9,max_comparisons=1)


def test_alignment_unrelated_sections_do_not_consume_similarity_budget():
    before=units('A must be completed.',section='2')
    after=units('B must be completed.',doc='D2',section='9')
    result=align_requirement_versions(before,after,max_comparisons=0)
    assert {x.change_type for x in result} == {'ADDED','REMOVED'}


def test_two_references_in_one_source_keep_correct_target_sections():
    source=units('Refer to Spec 550 section 4.1 and refer to Spec 542 section 5.2.')
    target=units('Memory ID shall use bluetooth.', doc='D2', section='5.2')
    found=link_explicit_references(source_spec_code='508',source_units=source,
                                   target_spec_code='542',target_units=target)
    assert len(found)==1
    target_wrong=units('Memory ID shall use bluetooth.',doc='D3',section='4.1')
    assert link_explicit_references(source_spec_code='508',source_units=source,
                                    target_spec_code='542',target_units=target_wrong)==()
