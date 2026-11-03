"""Run the affected offline regression set; no live service credentials required."""
from __future__ import annotations
import os
from pathlib import Path
import subprocess
import sys
import uuid

ROOT=Path(__file__).resolve().parents[1]
TESTS=[
    "test_streaming_qabug_sync.py", "test_streaming_qabug_quality.py", "test_conformance.py", "test_conformance_operator.py", "test_v592_qabug_sync_browser_hotfix.py",
    "test_core_operations.py", "test_core_format_smoke.py", "test_ragflow_compat.py",
    "test_v147_request_safety.py", "test_v147_enterprise_answer_quality.py",
    "test_v148_claim_parameter_and_coverage.py", "test_v148_answer_gate_evaluator.py",
    "test_v149_engineering_skills.py", "test_v150_live_engineering_skills.py",
    "test_v151_spec_sql_answer_paths.py", "test_v152_pg_spec_registration.py",
    "test_v153_unverified_body_identity_and_outline.py", "test_v154_multiturn_pdf_and_content.py",
    "test_v104_local_webui_backend.py", "test_v591_unified_qabug_ui.py",
    "test_document_conversion_runtime_database_target.py", "test_project_switch_clears_pending_files.py",
    "test_version_compare_selector_constraints.py", "test_relation_graph_service.py",
    "test_v530_current_qdrant_sync_source_contract.py", "test_v532_qdrant_sync_recovery_source_contract.py",
    "test_v560_qdrant_governance.py", "test_v594_import_resilience.py", "test_v595_large_image_resilience.py",
    "test_pipeline.py", "test_v119_release_integrity.py",
    "test_live_acceptance_regressions.py", "test_v593_retrieval_optimization.py",
    "test_v141_index_integrity_and_body_selection.py",
    "test_testing_workflow.py",
    "test_v133_stage2_controlled_review.py", "test_v137_stage2_db_semantic_and_commit_reconcile.py",
    "test_v106_engineering_intelligence.py", "test_v106_agent_workflow.py",
    "test_v107_engineering_integrity.py", "test_v107_backend_routing.py",
]


def main():
    folder=ROOT/"output"/"core_validation"/uuid.uuid4().hex
    folder.mkdir(parents=True)
    env=dict(os.environ,HF_HUB_OFFLINE="1",TRANSFORMERS_OFFLINE="1",HF_DATASETS_OFFLINE="1")
    command=[sys.executable,"-m","pytest",*["tests/"+name for name in TESTS],"-q","--tb=short",
             "--basetemp="+str(folder/"tmp"),"--junitxml="+str(folder/"results.xml")]
    return subprocess.run(command,cwd=ROOT,env=env,check=False).returncode


if __name__=="__main__":
    raise SystemExit(main())
