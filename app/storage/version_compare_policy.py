from __future__ import annotations


def legal_version_compare_values(
    labels_desc: list[str],
    *,
    from_label: str = "",
    to_label: str = "",
) -> tuple[list[str], list[str]]:
    """Return legal From/To domains for a forward-only version comparison.

    ``labels_desc`` is semantic business-version order, newest -> oldest.
    A valid comparison is strictly older -> newer.

    Tk/ttk Combobox does not support disabling individual popup rows, so the
    desktop UI uses these filtered domains: illegal versions never appear in
    the corresponding selector and therefore cannot be selected.
    """
    labels = [str(label) for label in labels_desc if str(label)]
    index = {label: position for position, label in enumerate(labels)}

    if to_label in index:
        # In newest->oldest order, every row after To is strictly older.
        from_values = labels[index[to_label] + 1 :]
    else:
        # Until To is known, every version except the newest can be From.
        from_values = labels[1:]

    if from_label in index:
        # Every row before From is strictly newer.
        to_values = labels[: index[from_label]]
    else:
        # Until From is known, every version except the oldest can be To.
        to_values = labels[:-1]

    return from_values, to_values


def is_forward_version_compare(
    labels_desc: list[str],
    *,
    from_label: str,
    to_label: str,
) -> bool:
    """Return True only when From is strictly older than To."""
    index = {str(label): position for position, label in enumerate(labels_desc)}
    if from_label not in index or to_label not in index:
        return False
    return index[from_label] > index[to_label]
