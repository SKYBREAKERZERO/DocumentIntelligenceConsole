"""v1.33 controlled review: no in-client UPDATE/INSERT, human ACK, and SQL gate contracts."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.engineering_intelligence import content_relation_review as review
from app.gui.stage2_review_window import Stage2ReviewUIError, confirm_review_inputs, run_db_task
from tests.test_v131_stage2_content_relation_review import Cursor, example_row

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def no_schema(monkeypatch):
    monkeypatch.setattr(review, "ensure_schema_ready", Mock())
    monkeypatch.setattr(review, "require_complete_backfill", Mock())


class ControlledCursor(Cursor):
    def __init__(self, row=None, *, result_id=12):
        super().__init__(row)
        self.result_id = result_id

    def fetchone(self):
        if self.queries[-1][0].startswith("SELECT dic_review.apply_content_relation_review"):
            return (self.result_id,)
        if "has_unambiguous_section_reference(text,text,text)" in self.queries[-1][0]:
            return (True,)
        return super().fetchone()


def apply(cur, **changes):
    kwargs = {
        "candidate_id": 12,
        "project": "21MM",
        "expected_status": "REVIEW_REQUIRED",
        "new_status": "CONFIRMED",
        "reason": "Reviewed source and target content carefully",
        "source_hash": example_row()["source_hash"],
        "target_hash": example_row()["target_hash"],
    }
    kwargs.update(changes)
    return review.review_content_relation_secured(cur, **kwargs)


def test_controlled_commit_uses_single_function_no_raw_dml():
    cur = ControlledCursor()
    case = apply(cur)
    assert case.candidate_id == 12
    sql, params = cur.queries[-1]
    assert sql == "SELECT dic_review.apply_content_relation_review(%s,%s,%s,%s,%s,%s,%s)"
    assert params == (12, "21MM", "REVIEW_REQUIRED", "CONFIRMED",
                      "Reviewed source and target content carefully",
                      example_row()["source_hash"], example_row()["target_hash"])
    assert not any("FOR UPDATE" in s for s, _ in cur.queries)
    # The DB-owned function itself, not the caller, obtains row locks.
    assert not any(s.lstrip().startswith(("UPDATE ", "INSERT ", "DELETE ")) for s, _ in cur.queries)


@pytest.mark.parametrize("changes,reason", [
    ({"source_hash": "0" * 64}, "since preview"),
    ({"target_hash": "Z" * 64}, "SHA-256"),
    ({"expected_status": "REJECTED"}, "transition"),
    ({"new_status": "REVIEW_REQUIRED"}, "transition"),
    ({"reason": "tiny"}, "reason"),
    ({"source_hash": ""}, "SHA-256"),
])
def test_invalid_contract_does_not_call_db_controlled_function(changes, reason):
    cur = ControlledCursor()
    with pytest.raises(review.ContentRelationReviewError, match=reason):
        apply(cur, **changes)
    assert not any("apply_content_relation_review" in query for query, _ in cur.queries)


def test_changed_provenance_refused_before_sql_write():
    row = example_row()
    row["target_file_name"] = "550_Bluetooth Audio Function Spec Cover Sheet v2.50.pdf"
    with pytest.raises(review.ContentRelationReviewError, match="cover/supporting"):
        apply(ControlledCursor(row))


def test_database_returns_other_candidate_id_fail_closed():
    with pytest.raises(review.ContentRelationReviewError, match="did not return"):
        apply(ControlledCursor(result_id=13))


def test_manual_ack_requires_both_hash_prefixes_and_state():
    cur = ControlledCursor()
    case = review.load_review_case(cur, candidate_id=12, project="21MM")
    base = dict(candidate="12", source_hash12=case.source.text_hash[:12],
                target_hash12=case.target.text_hash[:12], outcome="CONFIRMED",
                reason="Reviewed full verbatim evidence and section")
    confirm_review_inputs(case, **base)
    for key, wrong in (("candidate", "13"), ("source_hash12", "0"*12),
                       ("target_hash12", "0"*12), ("reason", "short"),
                       ("outcome", "REVIEW_REQUIRED")):
        invalid = dict(base, **{key: wrong})
        with pytest.raises(Stage2ReviewUIError):
            confirm_review_inputs(case, **invalid)
    with pytest.raises(Stage2ReviewUIError):
        confirm_review_inputs(None, **base)


def test_review_gui_full_original_body_visible():
    from app.gui.stage2_review_window import Stage2ReviewWindow
    case = review.load_review_case(ControlledCursor(), candidate_id=12, project="21MM")
    rendered = Stage2ReviewWindow._describe(case)
    assert case.source.raw_text in rendered
    assert case.target.raw_text in rendered
    assert case.source.text_hash in rendered
    assert case.target.text_hash in rendered
    assert "v2.52" in rendered


class DB:
    def __init__(self, *, identity="21MM", result_id=12):
        self.identity = identity
        self.queries = []
        self.rolled_back = False
        self.cur = ControlledCursor(result_id=result_id)
        self.cur.execute = self.execute

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def cursor(self):
        return self

    def execute(self, sql, params=None):
        self.queries.append((sql, params))
        self.cur.queries.append((sql, params))

    def fetchall(self):
        return [(self.identity,)]

    def fetchone(self):
        return self.cur.fetchone()

    def rollback(self):
        self.rolled_back = True


def test_gui_preview_must_start_readonly_and_check_identity(monkeypatch):
    conn = DB()
    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=lambda **kw: conn))
    result = run_db_task(dict(host="127.0.0.1", port=5432, database="rag_21mm",
                              user="reviewer", password="private", project="21MM"),
                         "preview", candidate_id=12)
    assert result.candidate_id == 12
    assert conn.queries[0][0] == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
    assert [item[0] for item in conn.queries[1:4]] == [
        "SET LOCAL lock_timeout = '5s'", "SET LOCAL statement_timeout = '60s'",
        "SELECT project_code FROM public.project_info ORDER BY project_code LIMIT 2",
    ]
    assert conn.rolled_back


def test_gui_wrong_project_fails_before_any_relation_write(monkeypatch):
    conn = DB(identity="24MM")
    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=lambda **kw: conn))
    with pytest.raises(Stage2ReviewUIError, match="身份"):
        run_db_task(dict(host="127.0.0.1", port=5432, database="rag_24mm",
                         user="reviewer", password="private", project="21MM"),
                    "apply", candidate_id=12, expected="REVIEW_REQUIRED", outcome="CONFIRMED",
                    reason="Reviewed all original paragraphs", source_hash=example_row()["source_hash"],
                    target_hash=example_row()["target_hash"])
    assert len(conn.queries) == 3
    assert conn.queries[-1][0].startswith("SELECT project_code")


def test_review_cli_no_actor_or_superuser_and_requires_manual_preview_digest():
    path = ROOT / "scripts/review_engineering_relation.py"
    spec = importlib.util.spec_from_file_location("dic_v133_review_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    prefix = ["--database", "rag_21mm", "--project", "21MM", "--candidate-id", "12",
              "--user", "reviewer", "--apply", "--status", "CONFIRMED",
              "--expected-status", "REVIEW_REQUIRED", "--reason", "Legitimate review source and target"]
    with pytest.raises(SystemExit) as missing:
        module.main(prefix)
    assert missing.value.code == 2
    with pytest.raises(SystemExit) as actor:
        module.main(prefix + ["--actor", "I-am-admin"])
    assert actor.value.code == 2
    with pytest.raises(SystemExit) as privileged:
        module.main(["postgres" if x == "reviewer" else x for x in prefix] +
                    ["--source-sha256", "0"*64, "--target-sha256", "0"*64])
    assert privileged.value.code == 2


def test_migration_has_function_owner_identity_and_deny_public_grants():
    ddl = (ROOT / "database/upgrade_engineering_stage2_v1_33.sql").read_text(encoding="utf-8")
    for signal in (
        "SECURITY DEFINER", "SET search_path=pg_catalog", "session_user",
        "authorized_reviewers", "FOR UPDATE OF r, src, tgt, su, tu, sv, tv, sc, tc, sd, td",
        "source_chunk", "source_text", "target_chunk", "target_text",
        "source_hash", "target_hash", "sha256(convert_to",
        "source_current", "target_current", "source_primary_doc", "target_primary_doc",
        "ei_version_backfill_receipts", "ei_relation_review_audit",
        "REVOKE ALL ON FUNCTION dic_review.apply_content_relation_review",
        "dic_stage2_relation_guard", "dic_stage2_audit_guard",
        "Direct relation update forbidden", "append-only",
    ):
        assert signal in ddl
    assert ddl.rstrip().endswith("COMMIT;")


def test_no_ddl_triggered_during_regular_gui_or_cli_import():
    import app.gui.stage2_review_window
    import scripts.review_engineering_relation
    assert not any("psql" in a for a in sys.argv)


def test_unknown_explicit_document_role_must_not_be_treated_as_spec_body():
    from app.storage.primary_document_policy import PrimarySpecificationDocumentPolicy as Primary
    for role in ("COVERSHEET", "UNKNOWN", "PRIMARY_CANDIDATE", "APPROVAL_SHEET"):
        assert not Primary.is_primary(file_name="550 spec.pdf", metadata={"document_role": role})
    for role in ("PRIMARY", "SPEC_BODY", "PRIMARY_CONTENT"):
        assert Primary.is_primary(file_name="Bluetooth Audio function spec v2.52.pdf",
                                  metadata={"document_role": role})
    assert " NOT IN " not in Primary.sql_primary_predicate("d")
    assert " IN (" in Primary.sql_primary_predicate("d")
    assert "('PRIMARY','SPEC_BODY','PRIMARY_CONTENT')" in (ROOT / "database/upgrade_engineering_stage2_v1_33.sql").read_text(encoding="utf-8")


def test_rag_must_reject_historical_self_asserted_reviewers():
    from app.ai.retrieval import EnterpriseRetriever
    from tests.test_v131_stage2_content_relation_review import _retriever, retrieval_row
    row = retrieval_row()
    row["review_identity_provenance"] = "UNVERIFIED_LEGACY"
    retriever, _ = _retriever(row)
    assert retriever._content_relation_search(["COMMON"], spec_codes=("508",)) == []


def test_rag_must_reject_missing_database_authority_or_disabled_guards():
    from tests.test_v131_stage2_content_relation_review import _retriever, retrieval_row
    for missing in ("authenticated_review_authority", "active_review_guards"):
        retriever, conn = _retriever(retrieval_row())
        base = {"relation_table": "ei_content_relations",
                "authenticated_review_authority": "review-function", "trusted_review_authority": True, "active_review_guards": 2}
        base[missing] = None if missing == "authenticated_review_authority" else 1
        conn.fetchone = lambda metadata=base: metadata
        assert retriever._content_relation_search(["COMMON"], spec_codes=("508",)) == []
        assert len(conn.queries) == 1


def test_review_audit_provenance_migration_defaults_legacy_to_unverified():
    ddl = (ROOT / "database/upgrade_engineering_stage2_v1_33.sql").read_text(encoding="utf-8")
    assert "DEFAULT 'UNVERIFIED_LEGACY'" in ddl
    assert "'DB_SESSION_USER'" in ddl
    assert "identity_provenance" in ddl
    from app.ai import retrieval
    source = (ROOT / "app/ai/retrieval.py").read_text(encoding="utf-8")
    assert "audit.identity_provenance='DB_SESSION_USER'" in source
    assert "rel.reviewed_by=audit.actor" in source
    assert "active_review_guards" in source
