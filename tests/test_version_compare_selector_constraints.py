from app.storage.version_compare_policy import (
    is_forward_version_compare,
    legal_version_compare_values,
)


def test_from_dropdown_excludes_to_and_newer_versions() -> None:
    labels = ["v1.03", "v1.02", "v1.01", "v1.00"]
    from_values, _ = legal_version_compare_values(
        labels,
        from_label="v1.00",
        to_label="v1.02",
    )
    assert from_values == ["v1.01", "v1.00"]
    assert "v1.02" not in from_values
    assert "v1.03" not in from_values


def test_to_dropdown_excludes_from_and_older_versions() -> None:
    labels = ["v1.03", "v1.02", "v1.01", "v1.00"]
    _, to_values = legal_version_compare_values(
        labels,
        from_label="v1.01",
        to_label="v1.03",
    )
    assert to_values == ["v1.03", "v1.02"]
    assert "v1.01" not in to_values
    assert "v1.00" not in to_values


def test_default_selector_domains_prevent_invalid_endpoints() -> None:
    labels = ["v1.03", "v1.02", "v1.01", "v1.00"]
    from_values, to_values = legal_version_compare_values(labels)
    assert from_values == ["v1.02", "v1.01", "v1.00"]
    assert to_values == ["v1.03", "v1.02", "v1.01"]


def test_forward_direction_guard() -> None:
    labels = ["v1.03", "v1.02", "v1.01", "v1.00"]
    assert is_forward_version_compare(labels, from_label="v1.00", to_label="v1.03")
    assert is_forward_version_compare(labels, from_label="v1.02", to_label="v1.03")
    assert not is_forward_version_compare(labels, from_label="v1.03", to_label="v1.02")
    assert not is_forward_version_compare(labels, from_label="v1.02", to_label="v1.02")
