from dataclasses import dataclass

from app.ai.health_policy import (
    SEVERITY_ERROR,
    SEVERITY_OK,
    SEVERITY_WARNING,
    classify_quality_issue,
    classify_runtime,
    is_valid_empty_scope,
    qabug_chunk_fanout,
)


def test_empty_sql_scope_without_collection_is_valid() -> None:
    assert is_valid_empty_scope(sql_count=0, collection_exists=False)
    assert not is_valid_empty_scope(sql_count=1, collection_exists=False)


def test_sync_backlog_is_warning_not_runtime_error() -> None:
    result = classify_runtime(
        dependencies_ok=True,
        sync_warnings=("21MM: SQL current chunks=100225, Qdrant points=54095",),
    )
    assert result.severity == SEVERITY_WARNING
    assert "sync" in result.message.lower()


def test_contract_failure_remains_error() -> None:
    result = classify_runtime(
        dependencies_ok=True,
        critical_errors=("vector dimension mismatch",),
    )
    assert result.severity == SEVERITY_ERROR


def test_ready_when_dependencies_and_indexes_are_clean() -> None:
    assert classify_runtime(dependencies_ok=True).severity == SEVERITY_OK


def test_validator_missing_points_are_repairable_warning() -> None:
    assert (
        classify_quality_issue(code="QDRANT_POINTS_MISSING", reported_severity="ERROR")
        == SEVERITY_WARNING
    )


def test_vector_contract_mismatch_is_hard_error() -> None:
    assert (
        classify_quality_issue(code="VECTOR_DIMENSION_MISMATCH", reported_severity="ERROR")
        == SEVERITY_ERROR
    )


def test_qabug_field_level_chunk_fanout_is_reported_as_ratio() -> None:
    value = qabug_chunk_fanout(records=79427, chunks=949679)
    assert round(value, 2) == 11.96
