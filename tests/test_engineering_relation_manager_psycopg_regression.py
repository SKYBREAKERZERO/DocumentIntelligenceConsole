from app.relations.manager import EngineeringRelationManager


class FakeCursor:
    def __init__(self):
        self.calls = []
        self._rows = []

    def execute(self, query, params=None):
        self.calls.append((str(query), params))
        self._rows = []

    def fetchall(self):
        return list(self._rows)


def test_load_known_specifications_project_filter_uses_typed_comparison():
    cur = FakeCursor()
    rows = EngineeringRelationManager._load_known_specifications(cur, "24MM")
    assert rows == []
    query, params = cur.calls[0]
    assert "project_code = %s" in query
    assert "%s IS NULL" not in query
    assert params == ("24MM",)


def test_load_known_specifications_without_project_has_no_placeholder():
    cur = FakeCursor()
    rows = EngineeringRelationManager._load_known_specifications(cur, None)
    assert rows == []
    query, params = cur.calls[0]
    assert "%s" not in query
    assert params is None


def test_reconcile_escapes_literal_like_percent_for_psycopg():
    cur = FakeCursor()
    resolved = EngineeringRelationManager().reconcile_unresolved(
        cur=cur,
        project_code="24MM",
    )
    assert resolved == 0
    query, params = cur.calls[0]
    # Psycopg uses percent-style placeholders. Literal LIKE wildcards must be
    # doubled whenever execute() also receives bound parameters.
    assert "LIKE 'SPEC:%%'" in query
    assert "LIKE 'SPEC_TITLE:%%'" in query
    assert params == ("24MM",)
