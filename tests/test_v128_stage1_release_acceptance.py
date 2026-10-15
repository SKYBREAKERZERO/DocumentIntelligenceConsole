"""Stage1 v1.29: prevent offline reports masquerading as production sign-off."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.engineering_intelligence import stage1_release_acceptance as release
from scripts import stage1_release_readiness as cli


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def minimal_evidence(content: bytes):
    document_sha = digest(b"one-version")
    pdf_sha = digest(content)
    native = {
        "schema": "stage1-native-pipeline-fidelity-v1", "document_hash": document_sha,
        "native_file_sha256": pdf_sha, "contents_not_sampled": 0, "entries_omitted": 0,
        "pipeline_evidence_queue_complete": True, "crosspage_candidate_queue_complete": True,
        "crosspage_pair_universe_complete": True,
    }
    chain = {"schema": "stage1-native-sql-chain-v1", "document_hash": document_sha,
             "native_file_sha256": pdf_sha, "project": "21MM", "source_review_queue_complete": True,
             "live_sql_transaction_verified_by_cli": True}
    score = {"schema": "stage1-acceptance-gate-v1",
             "measured_human_gold_gates_passed": True,
             "v3_project_complexity_coverage_passed": True,
             "structural_metrics": {
                 "document_hash": document_sha, "native_file_sha256": pdf_sha,
                 "project": "21MM", "declared_independent_review": True}}
    return native, chain, score


def test_no_artifacts_is_pending_not_success():
    e = release.inspect_evidence()
    assert e["status"] == "NOT_SUPPLIED"
    assert not e["source_identity_consistent"]
    assert not e["measured_gold_gate_reported_passed"]


def test_consistent_reports_only_get_unauthenticated_status():
    content = b"%PDF-1.4\nexample\n%%EOF"
    n, c, g = minimal_evidence(content)
    result = release.inspect_evidence(native=n, chain=c, gold_score=g,
                                      original_pdf_sha256=digest(content))
    assert result["source_identity_consistent"]
    assert result["measured_gold_gate_reported_passed"]
    assert result["checks"]["human_gold_thresholds"] == "UNAUTHENTICATED_SCORE_CLAIM"
    assert not result["live_database_authenticated_by_this_tool"]
    assert not result["reviewer_identity_authenticated_by_this_tool"]


@pytest.mark.parametrize("mutate", [
    lambda n, c, g: n.update(native_file_sha256=digest(b"other")),
    lambda n, c, g: c.update(native_file_sha256=digest(b"other")),
    lambda n, c, g: c.update(document_hash=digest(b"other")),
    lambda n, c, g: g["structural_metrics"].update(document_hash=digest(b"other")),
    lambda n, c, g: g["structural_metrics"].update(native_file_sha256=digest(b"other")),
    lambda n, c, g: c.update(project="OTHER"),
    lambda n, c, g: g["structural_metrics"].update(project="24MM"),
    lambda n, c, g: n.update(schema="other"),
    lambda n, c, g: g.update(schema="other"),
])
def test_cross_report_mismatch_rejected(mutate):
    raw = b"%PDF-1.4\nexample"
    n, c, g = minimal_evidence(raw)
    mutate(n, c, g)
    with pytest.raises(ValueError):
        release.inspect_evidence(native=n, chain=c, gold_score=g,
                                 original_pdf_sha256=digest(raw))


@pytest.mark.parametrize("field", [
    "contents_not_sampled", "entries_omitted", "pipeline_evidence_queue_complete",
    "crosspage_candidate_queue_complete", "crosspage_pair_universe_complete",
])
def test_incomplete_native_queue_cannot_be_coherent(field):
    raw = b"%PDF-1.4\nexample"
    n, c, g = minimal_evidence(raw)
    n[field] = 1 if field in ("contents_not_sampled", "entries_omitted") else False
    result = release.inspect_evidence(native=n, chain=c, gold_score=g,
                                      original_pdf_sha256=digest(raw))
    assert result["status"] == "BLOCKED"


def test_incomplete_sql_queue_blocked():
    raw = b"%PDF-1.4\nexample"
    n, c, g = minimal_evidence(raw)
    c["source_review_queue_complete"] = False
    assert release.inspect_evidence(native=n, chain=c, gold_score=g,
                                    original_pdf_sha256=digest(raw))["status"] == "BLOCKED"


def test_missing_v3_coverage_never_approved():
    raw = b"%PDF-1.4\nexample"
    n, c, g = minimal_evidence(raw)
    g["v3_project_complexity_coverage_passed"] = False
    e = release.inspect_evidence(native=n, chain=c, gold_score=g,
                                 original_pdf_sha256=digest(raw))
    assert not e["measured_gold_gate_reported_passed"]


def test_release_is_never_production_accepted():
    local = {"checks": {"core_source_files": "PASS", "release_metadata_version": "PASS",
                        "internal_python_import_paths": "PASS",
                        "test_and_runtime_dependencies": "PASS", "legacy_pdf_test_fixture": "PASS"}}
    raw = b"%PDF-1.4\nexample"
    n, c, g = minimal_evidence(raw)
    e = release.inspect_evidence(native=n, chain=c, gold_score=g,
                                 original_pdf_sha256=digest(raw))
    report = release.build_release_readiness(local, e)
    assert report["stage1_production_acceptance"] == "NOT_VERIFIED"
    assert not report["windows_gui_executed_by_this_tool"]
    assert not report["postgresql_connected_by_this_tool"]
    assert report["acceptance_checks"]["real_dcm_bluetooth_gold_signoff"].startswith("PENDING")


def test_static_missing_runtime_modules_and_fixture(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "scripts").mkdir()
    result = release.inspect_local_environment(tmp_path, module_present=lambda _: False,
                                               platform_name="linux")
    assert result["missing_core_file_count"] > 0
    assert result["missing_dependency_modules"] == ["fitz", "psycopg", "ttkbootstrap", "pytest"]
    assert result["checks"]["native_windows_environment"] == "NOT_WINDOWS"
    assert not result["can_collect_full_legacy_suite_on_this_interpreter"]


def test_hash_pdf_bounded_and_changed(tmp_path):
    pdf = tmp_path / "dcm.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF")
    first = release.hash_pdf(pdf)
    assert first == digest(pdf.read_bytes())
    pdf.write_bytes(b"%PDF-1.4\nmodified\n%%EOF")
    assert release.hash_pdf(pdf) != first
    with pytest.raises(ValueError):
        release.hash_pdf(pdf, limit=5)


@pytest.mark.parametrize("raw", [b"not a pdf", b""])
def test_pdf_rejects_non_pdf_and_empty(tmp_path, raw):
    pdf = tmp_path / "dcm.pdf"
    pdf.write_bytes(raw)
    with pytest.raises(ValueError):
        release.hash_pdf(pdf)


def test_reject_input_symlinks(tmp_path):
    good = tmp_path / "a.pdf"
    good.write_bytes(b"%PDF-1.4\n%%EOF")
    link = tmp_path / "b.pdf"
    try:
        link.symlink_to(good)
    except (OSError, NotImplementedError):
        pytest.skip("Host cannot create symbolic links")
    with pytest.raises(ValueError):
        release.hash_pdf(link)
    report = tmp_path / "in.json"
    report.write_text("{}")
    report_link = tmp_path / "linked.json"
    report_link.symlink_to(report)
    with pytest.raises(ValueError):
        release.read_report(report_link)


@pytest.mark.parametrize("data", ["[]", "null", '{"broken":', '"x"'])
def test_read_report_rejects_malformed_or_wrong_shape(tmp_path, data):
    path = tmp_path / "in.json"
    path.write_text(data)
    with pytest.raises((ValueError, json.JSONDecodeError)):
        release.read_report(path)


def test_report_file_size_limit(tmp_path):
    path = tmp_path / "in.json"
    path.write_bytes(b" " * 16)
    with pytest.raises(ValueError):
        release.read_report(path, limit=12)


def test_cli_preflight_report_and_no_overwrite(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "ROOT", Path(__file__).resolve().parents[1])
    path = tmp_path / "acceptance.json"
    assert cli.main(["--out", str(path)]) == 0
    data = json.loads(path.read_text())
    assert data["stage1_production_acceptance"] == "NOT_VERIFIED"
    assert "password" not in path.read_text().lower()
    assert cli.main(["--out", str(path)]) == 2
    assert "NOT_VERIFIED" in capsys.readouterr().err


def test_cli_evidence_roundtrip_and_reject_file_drift(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "ROOT", Path(__file__).resolve().parents[1])
    source = b"%PDF-1.4\nexample\n%%EOF"
    pdf = tmp_path / "dcm.pdf"
    pdf.write_bytes(source)
    n, c, g = minimal_evidence(source)
    inputs = [tmp_path / label for label in ("native.json", "chain.json", "gold.json")]
    for path, data in zip(inputs, (n, c, g)):
        path.write_text(json.dumps(data))
    out = tmp_path / "acceptance.json"
    argv = ["--out", str(out), "--original-pdf", str(pdf),
            "--native-report", str(inputs[0]), "--chain-report", str(inputs[1]),
            "--gold-score", str(inputs[2])]
    assert cli.main(argv) == 0
    assert json.loads(out.read_text())["evidence_review"]["source_identity_consistent"]
    pdf.write_bytes(b"%PDF-1.4\nchanged")
    assert cli.main(["--out", str(tmp_path / "other.json"), *argv[2:]]) == 2


def test_cli_does_not_write_if_evidence_incomplete(tmp_path):
    n, c, g = minimal_evidence(b"%PDF-1.4\nexample")
    src = tmp_path / "n.json"
    src.write_text(json.dumps(n))
    target = tmp_path / "out.json"
    with pytest.raises(SystemExit) as exc:
        cli.main(["--out", str(target), "--native-report", str(src)])
    assert exc.value.code == 2
    assert not target.exists()


def test_release_metadata_mismatch_is_explicitly_blocked(tmp_path):
    (tmp_path / "app" / "gui").mkdir(parents=True)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "app" / "gui" / "application.py").write_text('APP_VERSION = "1.27"')
    (tmp_path / "README.md").write_text("# DocumentIntelligenceConsole Enterprise v1.29\n")
    (tmp_path / "README-EN.md").write_text("# DocumentIntelligenceConsole Enterprise v1.29\n")
    local = release.inspect_local_environment(tmp_path, module_present=lambda _: True, platform_name="win32")
    assert local["checks"]["release_metadata_version"] == "BLOCKED"
    assert local["checks"]["native_windows_environment"] == "AVAILABLE_FOR_MANUAL_TEST"


def test_current_repository_release_metadata_consistent():
    root = Path(__file__).resolve().parents[1]
    local = release.inspect_local_environment(root, module_present=lambda _: True, platform_name="win32")
    assert local["version_consistent"]
    assert local["checks"]["legacy_pdf_test_fixture"] == "SELF_CONTAINED_GENERATED"
    assert not local["legacy_test_pdf_fixture_provided"]
