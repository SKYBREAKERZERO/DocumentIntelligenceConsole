"""v1.37 offline adverse contracts (NOT real PostgreSQL integration tests)."""
from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.engineering_intelligence import content_relation_review as review
from app.engineering_intelligence import review_outcome_reconciliation as reconciliation
from app.engineering_intelligence.stage2_db_preflight import (
    REFERENCE_HELPER_SHA256, SEMANTIC_GUARD_SHA256, inspect_stage2_db,
)
from tests.test_v134_stage2_permission_preflight import SnapshotCursor, invoke
from tests.test_v131_stage2_content_relation_review import example_row

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "database/upgrade_engineering_stage2_v1_37.sql"


def test_sql_migration_requires_pg17_scoped_nonlogin_authority_and_atomic_lock():
    ddl = MIGRATION.read_text(encoding="utf-8")
    assert ddl.startswith("-- Stage 2 v1.37:")
    for literal in ("BEGIN;", "COMMIT;", "SET LOCAL lock_timeout", "SET LOCAL statement_timeout",
                    "server_version_num", "count(*) FROM public.project_info", "QABUG",
                    "current_user", "rolcanlogin", "proowner", "RAG_QABUG",
                    "dic_stage2_relation_guard", "t.tgenabled='O'", "t.tgtype=31"):
        assert literal in ddl
    assert "DROP TRIGGER" not in ddl and "DELETE FROM public.ei_content_relations" not in ddl
    assert "UPDATE public.ei_content_relations" not in ddl
    assert "GRANT EXECUTE" not in ddl
    assert "SECURITY DEFINER" not in ddl.split("CREATE OR REPLACE FUNCTION")[1]


def test_sql_reference_gate_is_inside_writable_relation_trigger_not_client_only():
    ddl = MIGRATION.read_text(encoding="utf-8")
    func = ddl.split("CREATE OR REPLACE FUNCTION dic_review.guard_relation_write()")[1]
    assert "IF NEW.status='CONFIRMED' AND NEW.relation_type='REFERENCES_SECTION'" in func
    assert "current_user <> fn_owner" in func
    assert "dic_review.has_unambiguous_section_reference" in func
    assert "matching_target_units <> 1" in func
    assert "tu.version_id" in func
    assert "ei_requirement_units" in func
    assert "Stage 2 DB reference anchor missing or ambiguous" in func
    assert "REVOKE ALL ON FUNCTION dic_review.has_unambiguous_section_reference" in ddl


def test_sql_parser_is_bounded_and_explicitly_fails_on_multi_reference_section():
    ddl = MIGRATION.read_text(encoding="utf-8")
    helper = ddl.split("CREATE OR REPLACE FUNCTION dic_review.has_unambiguous_section_reference")[1].split("CREATE OR REPLACE FUNCTION dic_review.guard_relation_write")[0]
    assert "IMMUTABLE" in helper and "search_path=pg_catalog" in helper
    assert "length(p_source)>32768" in helper
    assert "reference_count=1 AND section_count=1" in helper
    assert "explicit_spec_count <= 1" in helper
    assert "p_code" in helper and "p_section" in helper
    assert "RETURN FALSE;" in helper
    assert "regexp_split_to_table" in helper


def test_sql_body_pins_match_shipped_upgrade():
    import re
    ddl = MIGRATION.read_text(encoding="utf-8")
    for function, digest in (("guard_relation_write", SEMANTIC_GUARD_SHA256),
                             ("has_unambiguous_section_reference", REFERENCE_HELPER_SHA256)):
        match = re.search(r"CREATE OR REPLACE FUNCTION dic_review\." + function +
                          r"\([^)]*\).*?AS \$body\$(.*?)\$body\$;", ddl, re.S)
        assert match is not None
        assert hashlib.sha256(match.group(1).encode("utf-8")).hexdigest() == digest


@pytest.mark.parametrize("options,expected", [
    ({"semantic_guard_hash": "0" * 64}, "v137_pinned_relation_guard"),
    ({"reference_hash": "0" * 64}, "v137_pinned_reference_helper"),
    ({"semantic_flags": (False, True, True)}, "v137_helper_authority"),
    ({"semantic_flags": (True, False, True)}, "v137_helper_authority"),
    ({"semantic_flags": (True, True, False)}, "v137_helper_authority"),
])
def test_preflight_rejects_old_or_changed_db_guard(options, expected):
    result = invoke(SnapshotCursor(**options))
    assert result.passed is False
    assert any(c.name == expected and c.passed is False for c in result.checks)


class AuditCursor:
    def __init__(self, *, actor="stage2_reviewer", events=None):
        self.actor = actor
        self.events = list(events or [])
        self.statements = []
        self.description = [(k,) for k in ("id", "actor", "previous_status", "new_status", "reason", "identity_provenance")]

    def execute(self, sql, params=None):
        assert sql.strip().startswith("SELECT")
        self.statements.append((sql, params))

    def fetchone(self):
        assert "session_user" in self.statements[-1][0]
        return (self.actor,)

    def fetchall(self):
        assert "ei_relation_review_audit" in self.statements[-1][0]
        _, params = self.statements[-1]
        return [item for item in self.events if item["id"] > params[1]][:2]


def sample_case(*, status="CONFIRMED", audit_id=8):
    row = example_row()
    return review.ContentRelationReviewCase(
        candidate_id=12, relation_type="REFERENCES_SECTION", status=status,
        score=.8, rationale="Explicit citation", source=review.EvidenceEndpoint(
            specification_code="508", version="v1", document_id="doc508", file_name="508 body.pdf",
            unit_key="src", content_id=1, section_id="3.2", page_number=1,
            start_offset=0, end_offset=8, raw_text="source", text_hash=row["source_hash"]),
        target=review.EvidenceEndpoint(
            specification_code="550", version="v2.52", document_id="doc550", file_name="550 body.pdf",
            unit_key="tgt", content_id=2, section_id="4.1", page_number=2,
            start_offset=0, end_offset=8, raw_text="target", text_hash=row["target_hash"]),
        last_audit_id=audit_id,
    )


REASON = "Reviewed both complete original requirement clauses"
REASON_DIGEST = hashlib.sha256(REASON.encode("utf-8")).hexdigest()


def payload(**changes):
    row = example_row()
    base = dict(candidate_id=12, project="21MM", before_audit_id=7,
                expected_status="REVIEW_REQUIRED", intended_status="CONFIRMED",
                source_hash=row["source_hash"], target_hash=row["target_hash"],
                reason_sha256=REASON_DIGEST)
    return base | changes


def event(**changes):
    base = dict(id=8, actor="stage2_reviewer", previous_status="REVIEW_REQUIRED",
                new_status="CONFIRMED", reason=REASON, identity_provenance="DB_SESSION_USER")
    return base | changes


@pytest.fixture
def reviewed_case(monkeypatch):
    obj = sample_case()
    fn = Mock(return_value=obj)
    monkeypatch.setattr(reconciliation, "load_review_case", fn)
    return fn


def test_reconcile_one_exactly_matching_audit_observed_no_retry(reviewed_case):
    cur = AuditCursor(events=[event()])
    outcome = reconciliation.reconcile_review_commit(cur, **payload())
    assert outcome.status == "MATCHING_COMMIT_OBSERVED"
    assert outcome.as_dict()["automatic_retry_allowed"] is False
    assert outcome.as_dict()["latest_audit_id"] == 8
    assert outcome.as_dict()["production_acceptance"] == "NOT_VERIFIED"
    assert reviewed_case.call_args.kwargs == {"candidate_id": 12, "project": "21MM"}


def test_reconcile_no_event_observed_does_not_authorize_retry(reviewed_case):
    reviewed_case.return_value = sample_case(status="REVIEW_REQUIRED", audit_id=7)
    outcome = reconciliation.reconcile_review_commit(AuditCursor(), **payload())
    assert outcome.status == "NO_COMMIT_VISIBLE_AT_SNAPSHOT"
    assert outcome.as_dict()["automatic_retry_allowed"] is False


@pytest.mark.parametrize("edited,expected", [
    ({"actor": "someone_else"}, "INDETERMINATE_AUDIT_IDENTITY_OR_REASON"),
    ({"identity_provenance": "UNVERIFIED_LEGACY"}, "INDETERMINATE_AUDIT_IDENTITY_OR_REASON"),
    ({"reason": "unrelated reason"}, "INDETERMINATE_AUDIT_IDENTITY_OR_REASON"),
    ({"new_status": "REJECTED"}, "INDETERMINATE_AUDIT_IDENTITY_OR_REASON"),
    ({"previous_status": "REJECTED"}, "INDETERMINATE_AUDIT_IDENTITY_OR_REASON"),
])
def test_reconcile_rejects_audit_spoof_or_another_action(reviewed_case, edited, expected):
    res = reconciliation.reconcile_review_commit(AuditCursor(events=[event(**edited)]), **payload())
    assert res.status == expected


def test_reconcile_concurrent_multiple_audits_unknown(reviewed_case):
    reviewed_case.return_value = sample_case(audit_id=9)
    res = reconciliation.reconcile_review_commit(
        AuditCursor(events=[event(), event(id=9, previous_status="CONFIRMED", new_status="REVIEW_REQUIRED")]),
        **payload())
    assert res.status == "INDETERMINATE_CONCURRENT_TRANSITION"


def test_reconcile_drift_and_out_of_date_watermark_unknown(reviewed_case):
    reviewed_case.return_value = replace(sample_case(), last_audit_id=6)
    assert reconciliation.reconcile_review_commit(AuditCursor(), **payload()).status == "INDETERMINATE_EVIDENCE_OR_AUDIT_DRIFT"
    reviewed_case.return_value = sample_case()
    assert reconciliation.reconcile_review_commit(
        AuditCursor(events=[event()]), **payload(source_hash="a" * 64)
    ).status == "INDETERMINATE_EVIDENCE_OR_AUDIT_DRIFT"


@pytest.mark.parametrize("changed", [
    {"before_audit_id": -1}, {"candidate_id": 0},
    {"intended_status": "STALE"}, {"expected_status": "CONFIRMED"},
    {"source_hash": "notahash"}, {"reason_sha256": "Z" * 64},
])
def test_invalid_reconcile_proof_no_sql(monkeypatch, changed):
    fn = Mock()
    monkeypatch.setattr(reconciliation, "load_review_case", fn)
    cur = AuditCursor()
    with pytest.raises(review.ContentRelationReviewError):
        reconciliation.reconcile_review_commit(cur, **payload(**changed))
    assert fn.call_count == 0 and cur.statements == []


def test_v137_client_denies_sql_write_if_gate_missing(monkeypatch):
    from tests.test_v133_stage2_controlled_review import ControlledCursor, apply

    monkeypatch.setattr(review, "ensure_schema_ready", Mock())
    monkeypatch.setattr(review, "require_complete_backfill", Mock())

    class OldDatabaseCursor(ControlledCursor):
        def fetchone(self):
            if "has_unambiguous_section_reference(text,text,text)" in self.queries[-1][0]:
                return (False,)
            return super().fetchone()

    cur = OldDatabaseCursor()
    with pytest.raises(review.ContentRelationReviewError, match="v1.37 semantic"):
        apply(cur)
    assert not any(sql.startswith("SELECT dic_review.apply_content_relation_review")
                   for sql, _ in cur.queries)
    assert any("sha256(convert_to(p.prosrc" in sql for sql, _ in cur.queries)


def test_reconciliation_cli_uses_read_only_transaction_and_nonzero_uncertain(monkeypatch, capsys):
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location("dic_v137_reconcile_cli", ROOT / "scripts/reconcile_stage2_review.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    class Cursor:
        def __init__(self):
            self.commands = []
        def execute(self, sql, params=None):
            self.commands.append(sql)
            if sql.lstrip().startswith(("UPDATE", "INSERT", "DELETE")):
                raise AssertionError("reconciliation performed a write")
        def fetchall(self):
            return [("rag_21mm", "21MM")]
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False

    class Connection:
        def __init__(self):
            self.cur = Cursor()
            self.rollbacks = 0
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False
        def cursor(self):
            return self.cur
        def rollback(self):
            self.rollbacks += 1

    conn = Connection()
    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=lambda **kwargs: conn))
    monkeypatch.setenv("POSTGRES_PASSWORD", "test-secret")
    mocked = Mock(return_value=reconciliation.ReviewOutcome(
        candidate_id=12, status="NO_COMMIT_VISIBLE_AT_SNAPSHOT", observed_state="REVIEW_REQUIRED",
        latest_audit_id=7, post_watermark_events=0))
    monkeypatch.setattr(mod, "reconcile_review_commit", mocked)
    args = ["--database", "rag_21mm", "--project", "21MM", "--user", "stage2_reviewer",
            "--candidate-id", "12", "--before-audit-id", "7", "--expected-status", "REVIEW_REQUIRED",
            "--intended-status", "CONFIRMED", "--source-sha256", example_row()["source_hash"],
            "--target-sha256", example_row()["target_hash"], "--reason-sha256", REASON_DIGEST]
    assert mod.main(args) == 2
    out = capsys.readouterr().out
    assert '"automatic_retry_allowed": false' in out
    assert "test-secret" not in out
    assert conn.cur.commands[0] == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
    assert conn.rollbacks == 1
    assert mocked.call_args.kwargs["before_audit_id"] == 7
