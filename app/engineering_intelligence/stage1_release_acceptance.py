"""Stage 1 v1.29 fail-closed release preflight and evidence handoff.

All checks are local/read-only. Report JSON is an operator-supplied snapshot,
NOT an authenticated statement that Windows, PostgreSQL or a human signed off.
Never use preflight success as a production acceptance flag.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable

_REQUIRED = (
    "README.md", "README-EN.md", "CHANGELOG.md", "run.ps1",
    "requirements.txt", "config/config.yaml", "app/gui/application.py",
    "app/engineering_intelligence/stage1_structural_gold.py",
    "app/engineering_intelligence/stage1_native_sql_chain.py",
    "scripts/verify_runtime_package.py",
    "scripts/inspect_stage1_native_sql_chain.py",
    "scripts/evaluate_stage1_structural_gold.py",
    "scripts/stage1_release_readiness.py",
)
_MODULES = ("fitz", "psycopg", "ttkbootstrap", "pytest")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_MAX_PDF = 100_000_000
_MAX_REPORT = 12_000_000


def _is_hash(value: object) -> bool:
    return isinstance(value, str) and _SHA.fullmatch(value) is not None


def read_report(path: Path, *, limit: int = _MAX_REPORT) -> dict[str, Any]:
    """Load a bounded, regular, non-symlink JSON object; no source text in outputs."""
    if (path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= limit):
        raise ValueError("Input must be a non-symlink regular file within the size budget")
    with path.open("rb") as f:
        payload = f.read(limit + 1)
    if len(payload) > limit:
        raise ValueError("Input changed or exceeded the size budget")
    data = json.loads(payload)
    if type(data) is not dict:
        raise ValueError("Input JSON must be an object")
    return data


def hash_pdf(path: Path, *, limit: int = _MAX_PDF) -> str:
    if (path.is_symlink() or not path.is_file() or path.suffix.lower() != ".pdf"
            or not 0 < path.stat().st_size <= limit):
        raise ValueError("Original PDF must be a bounded, regular non-symlink PDF")
    h = hashlib.sha256()
    total = 0
    with path.open("rb") as f:
        if f.read(5) != b"%PDF-":
            raise ValueError("Original source is not a PDF")
        f.seek(0)
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            total += len(chunk)
            if total > limit:
                raise ValueError("PDF changed or exceeded the byte budget")
            h.update(chunk)
    return h.hexdigest()


def inspect_local_environment(
    root: Path, *,
    module_present: Callable[[str], bool] | None = None,
    platform_name: str | None = None,
) -> dict[str, Any]:
    """Static package inspection and *current interpreter* dependency discovery."""
    from scripts.verify_runtime_package import missing_internal_imports

    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Package root must be an existing non-symlink directory")
    missing_files: list[str] = []
    for rel in _REQUIRED:
        target = root / rel
        if target.is_symlink() or not target.is_file():
            missing_files.append(rel)
    missing_imports = missing_internal_imports(root)
    present = module_present or (lambda name: importlib.util.find_spec(name) is not None)
    dependencies = {name: bool(present(name)) for name in _MODULES}
    win = (platform_name if platform_name is not None else sys.platform) == "win32"
    def release_header_ok(filename: str, prefix: str) -> bool:
        path = root / filename
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 100_000:
            return False
        with path.open("r", encoding="utf-8") as f:
            return f.readline().strip().startswith(prefix)

    gui = root / "app" / "gui" / "application.py"
    gui_version_ok = (gui.is_file() and not gui.is_symlink()
                      and gui.stat().st_size <= 1_000_000
                      and 'APP_VERSION = "1.30"' in gui.read_text(encoding="utf-8"))
    metadata_ok = (gui_version_ok
                   and release_header_ok("README.md", "# DocumentIntelligenceConsole Enterprise v1.30")
                   and release_header_ok("README-EN.md", "# DocumentIntelligenceConsole Enterprise v1.30"))
    fixture = root / "input" / "test.pdf"
    legacy_fixture_present = fixture.is_file() and not fixture.is_symlink()
    loader_test = root / "tests" / "test_loader.py"
    self_contained_loader_test = (loader_test.is_file() and not loader_test.is_symlink()
                                  and "def test_pdf_loader_preserves_page_order_and_text" in
                                  loader_test.read_text(encoding="utf-8"))
    fixture_satisfied = legacy_fixture_present or self_contained_loader_test
    checks = {
        "core_source_files": "PASS" if not missing_files else "BLOCKED",
        "release_metadata_version": "PASS" if metadata_ok else "BLOCKED",
        "internal_python_import_paths": "PASS" if not missing_imports else "BLOCKED",
        "test_and_runtime_dependencies": "PASS" if all(dependencies.values()) else "BLOCKED",
        "legacy_pdf_test_fixture": ("SELF_CONTAINED_GENERATED" if self_contained_loader_test
                                    else "PASS" if legacy_fixture_present else "BLOCKED"),
        "native_windows_environment": "AVAILABLE_FOR_MANUAL_TEST" if win else "NOT_WINDOWS",
    }
    return {
        "schema": "stage1-release-local-preflight-v1",
        "checks": checks,
        "missing_core_file_count": len(missing_files),
        "version_consistent": metadata_ok,
        "missing_core_files": missing_files,
        "missing_internal_import_count": len(missing_imports),
        # Don't copy absolute filesystem paths or module source into the report.
        "missing_dependency_modules": [name for name, ok in dependencies.items() if not ok],
        "legacy_test_pdf_fixture_provided": legacy_fixture_present,
        "legacy_test_has_generated_fixture": self_contained_loader_test,
        "interpreter_python_major_minor": f"{sys.version_info.major}.{sys.version_info.minor}",
        "can_collect_full_legacy_suite_on_this_interpreter": bool(
            all(dependencies.values()) and fixture_satisfied
        ),
        "can_claim_gui_smoke_pass": False,
        "can_claim_live_sql_pass": False,
    }


def inspect_evidence(
    *, native: dict[str, Any] | None = None,
    chain: dict[str, Any] | None = None,
    gold_score: dict[str, Any] | None = None,
    original_pdf_sha256: str | None = None,
) -> dict[str, Any]:
    """Review cross-file *claims* without accepting unauthenticated report flags."""
    supplied = (native is not None, chain is not None, gold_score is not None)
    if not any(supplied):
        return {"status": "NOT_SUPPLIED", "checks": {
            "native_pdf_hash": "PENDING", "json_sql_identity": "PENDING",
            "human_gold_thresholds": "PENDING"}, "source_identity_consistent": False,
            "measured_gold_gate_reported_passed": False,
            "live_database_authenticated_by_this_tool": False}
    if not all(supplied) or original_pdf_sha256 is None:
        raise ValueError("Evidence review requires native, chain, Gold score and original PDF")
    assert native is not None and chain is not None and gold_score is not None
    metrics = gold_score.get("structural_metrics")
    if not isinstance(metrics, dict):
        raise ValueError("Gold score missing structural metrics")
    if (native.get("schema") != "stage1-native-pipeline-fidelity-v1"
            or chain.get("schema") != "stage1-native-sql-chain-v1"
            or gold_score.get("schema") != "stage1-acceptance-gate-v1"):
        raise ValueError("Unexpected Stage 1 evidence schema")
    document_hash, native_sha = native.get("document_hash"), native.get("native_file_sha256")
    if not (_is_hash(document_hash) and _is_hash(native_sha)
            and original_pdf_sha256 == native_sha
            and chain.get("document_hash") == document_hash
            and metrics.get("document_hash") == document_hash
            and chain.get("native_file_sha256") == native_sha
            and metrics.get("native_file_sha256") == native_sha
            and chain.get("project") in ("21MM", "24MM")
            and metrics.get("project") == chain.get("project")):
        raise ValueError("Evidence source SHA-256, project or document identity mismatch")
    complete = (native.get("contents_not_sampled") == 0
                and native.get("entries_omitted") == 0
                and native.get("pipeline_evidence_queue_complete") is True
                and native.get("crosspage_candidate_queue_complete") is True
                and native.get("crosspage_pair_universe_complete") is True
                and chain.get("source_review_queue_complete") is True)
    declared_gold = (gold_score.get("measured_human_gold_gates_passed") is True
                     and gold_score.get("v3_project_complexity_coverage_passed") is True
                     and metrics.get("declared_independent_review") is True)
    checks = {
        "native_pdf_hash": "MATCHED_LOCAL_BYTES",
        "json_sql_identity": "REPORT_IDENTITIES_CONSISTENT" if complete else "BLOCKED_INCOMPLETE_QUEUE",
        "human_gold_thresholds": ("UNAUTHENTICATED_SCORE_CLAIM" if declared_gold
                                  else "NOT_PASSED_OR_MISSING"),
    }
    return {"status": "OFFLINE_REPORTS_COHERENT_UNAUTHENTICATED" if complete else "BLOCKED",
            "checks": checks, "source_identity_consistent": True,
            "measured_gold_gate_reported_passed": declared_gold,
            "live_sql_transaction_claimed_by_prior_cli": chain.get("live_sql_transaction_verified_by_cli") is True,
            "live_database_authenticated_by_this_tool": False,
            "real_pdf_semantics_validated_by_this_tool": False,
            "reviewer_identity_authenticated_by_this_tool": False}


def build_release_readiness(local: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    checks = {
        "source_package": local["checks"]["core_source_files"],
        "release_metadata": local["checks"]["release_metadata_version"],
        "internal_imports": local["checks"]["internal_python_import_paths"],
        "dependencies": local["checks"]["test_and_runtime_dependencies"],
        "legacy_test_fixture": local["checks"]["legacy_pdf_test_fixture"],
        "pdf_json_sql_gold_evidence": evidence["status"],
        "windows_gui_launch_and_navigation": "PENDING_REAL_WINDOWS_TEST",
        "postgresql_readonly_end_to_end": "PENDING_LIVE_DATABASE_TEST",
        "real_dcm_bluetooth_gold_signoff": "PENDING_AUTHENTICATED_HUMAN_REVIEW",
        "recovery_performance_and_security": "PENDING_DEPLOYMENT_ACCEPTANCE",
    }
    blocked = [k for k in ("source_package", "release_metadata", "internal_imports", "dependencies",
                           "legacy_test_fixture") if checks[k] == "BLOCKED"]
    return {"schema": "stage1-release-acceptance-v1", "release": "v1.30",
            "local_preflight": local, "evidence_review": evidence,
            "acceptance_checks": checks,
            "local_preflight_blocked_checks": blocked,
            "stage1_production_acceptance": "NOT_VERIFIED",
            "independent_human_approval_authenticated": False,
            "windows_gui_executed_by_this_tool": False,
            "postgresql_connected_by_this_tool": False,
            "qdrant_modified": False, "database_modified": False}
