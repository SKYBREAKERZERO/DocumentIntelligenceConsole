from app.storage.specification_change_manager import (
    SectionSnapshot,
    SpecificationChangeManager,
)


def section(section_id: str, text: str, *, order: int = 1, title: str = "Title") -> SectionSnapshot:
    return SectionSnapshot(
        section_id=section_id,
        chapter_id="1",
        parent_section_id=None,
        chapter_title="Chapter",
        section_title=title,
        sort_order=order,
        page_number=1,
        text=text,
        content_ids=(),
    )


def test_compare_sections_detects_modified_added_removed():
    old = [
        section("1.1", "timeout = 15"),
        section("1.2", "legacy"),
    ]
    new = [
        section("1.1", "timeout = 10"),
        section("1.3", "new requirement"),
    ]

    changes, unchanged = SpecificationChangeManager.compare_sections(old, new)
    types = sorted(change.change_type for change in changes)

    assert unchanged == 0
    assert types == ["ADDED", "MODIFIED", "REMOVED"]


def test_compare_sections_detects_move_without_content_change():
    old = [section("1.1", "same", order=1)]
    new = [section("1.1", "same", order=4)]

    changes, unchanged = SpecificationChangeManager.compare_sections(old, new)

    assert unchanged == 0
    assert len(changes) == 1
    assert changes[0].change_type == "MOVED"
    assert changes[0].similarity_score == 1.0


def test_compare_sections_recovers_changed_parser_id():
    old = [section("AUTO-001", "Bluetooth retry count = 3", title="Bluetooth")]
    new = [section("AUTO-009", "Bluetooth retry count = 3", title="Bluetooth")]

    changes, unchanged = SpecificationChangeManager.compare_sections(old, new)

    assert unchanged == 0
    assert len(changes) == 1
    assert changes[0].change_type == "MOVED"
