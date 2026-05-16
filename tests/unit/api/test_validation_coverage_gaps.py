"""API-layer tests for coverage gap fields in validation responses."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from awf.api.schemas import (
    ValidationProvenanceItemResponse,
    ValidationRunSummaryResponse,
)
from awf.api.validation_runs import (
    validation_coverage_fields,
    validation_run_summary,
)
from awf.db.models import ValidationRun


def _run(**overrides: object) -> ValidationRun:
    values: dict[str, object] = {
        "id": "vr_00000000000001",
        "workspace_id": "ws_gaps",
        "attempt_id": "ta_gaps",
        "tier": 1,
        "command_set_hash": "a" * 64,
        "commands": [],
        "base_commit": "base-sha",
        "target_branch": "main",
        "target_head_sha": "target-sha",
        "status": "failed",
        "reason_code": "COVERAGE_BELOW_THRESHOLD",
        "started_at": datetime(2026, 4, 26, 12, 0, tzinfo=UTC),
        "finished_at": datetime(2026, 4, 26, 12, 5, tzinfo=UTC),
        "log_stream_refs": {},
        "retry_count": 0,
    }
    values.update(overrides)
    return ValidationRun(**values)


@pytest.mark.unit
def test_validation_coverage_fields_extracts_gaps() -> None:
    gaps: list[dict[str, object]] = [
        {"file": "src/a.py", "missing_lines": ["10-20", "50"]},
        {"file": "src/b.py", "missing_lines": ["30-45"]},
    ]
    run = _run(
        log_stream_refs={
            "coverage": {
                "percent": 88.0,
                "minimum_percent": 99.0,
                "status": "failed",
                "reason_code": "COVERAGE_BELOW_THRESHOLD",
                "gaps": gaps,
            }
        }
    )

    fields = validation_coverage_fields(run)

    assert fields == {
        "coverage_percent": 88.0,
        "coverage_minimum_percent": 99.0,
        "coverage_status": "failed",
        "coverage_reason_code": "COVERAGE_BELOW_THRESHOLD",
        "coverage_gaps": gaps,
        "failing_test_node_ids": [],
        "failing_test_evidence": [],
    }


@pytest.mark.unit
def test_validation_coverage_fields_prefers_coverage_column_over_log_refs() -> None:
    run = _run(
        coverage={
            "percent": 99.4,
            "minimum_percent": 99.0,
            "status": "passed",
            "reason_code": "COVERAGE_OK",
        },
        log_stream_refs={
            "coverage": {
                "percent": 72.0,
                "minimum_percent": 99.0,
                "status": "failed",
                "reason_code": "COVERAGE_BELOW_THRESHOLD",
            }
        },
    )

    fields = validation_coverage_fields(run)

    assert fields["coverage_percent"] == 99.4
    assert fields["coverage_status"] == "passed"
    assert fields["coverage_reason_code"] == "COVERAGE_OK"


@pytest.mark.unit
def test_validation_coverage_fields_handles_missing_gaps() -> None:
    run = _run(
        log_stream_refs={
            "coverage": {
                "percent": 95.0,
                "minimum_percent": 90.0,
                "status": "passed",
                "reason_code": "COVERAGE_OK",
            }
        }
    )

    fields = validation_coverage_fields(run)

    assert fields["coverage_gaps"] == []


@pytest.mark.unit
def test_validation_coverage_fields_handles_non_list_gaps() -> None:
    run = _run(
        log_stream_refs={
            "coverage": {
                "percent": 88.0,
                "minimum_percent": 99.0,
                "status": "failed",
                "reason_code": "COVERAGE_BELOW_THRESHOLD",
                "gaps": "not a list",
            }
        }
    )

    fields = validation_coverage_fields(run)

    assert fields["coverage_gaps"] == []


@pytest.mark.unit
def test_validation_coverage_fields_handles_none_log_stream_refs() -> None:
    run = _run(log_stream_refs=None)

    fields = validation_coverage_fields(run)

    assert fields == {
        "coverage_percent": None,
        "coverage_minimum_percent": None,
        "coverage_status": None,
        "coverage_reason_code": None,
        "coverage_gaps": [],
        "failing_test_node_ids": [],
        "failing_test_evidence": [],
    }


@pytest.mark.unit
def test_validation_coverage_fields_extracts_failing_test_evidence() -> None:
    node_ids = ["tests/unit/test_widget.py::test_handles_edges"]
    evidence = [
        "FAILED tests/unit/test_widget.py::test_handles_edges - AssertionError",
        "E   assert 1 == 2",
    ]
    run = _run(
        log_stream_refs={
            "coverage": {
                "percent": 99.2,
                "minimum_percent": 99.0,
                "status": "passed",
                "reason_code": "COVERAGE_OK",
                "failing_test_node_ids": node_ids,
                "failing_test_evidence": evidence,
            }
        }
    )

    fields = validation_coverage_fields(run)

    assert fields["failing_test_node_ids"] == node_ids
    assert fields["failing_test_evidence"] == evidence


@pytest.mark.unit
def test_validation_coverage_fields_handles_malformed_failing_test_evidence() -> None:
    run = _run(
        log_stream_refs={
            "coverage": {
                "failing_test_node_ids": "tests/unit/test_widget.py::test_handles_edges",
                "failing_test_evidence": [
                    123,
                    "FAILED tests/unit/test_widget.py::test_handles_edges",
                ],
            }
        }
    )

    fields = validation_coverage_fields(run)

    assert fields["failing_test_node_ids"] == []
    assert fields["failing_test_evidence"] == [
        "FAILED tests/unit/test_widget.py::test_handles_edges"
    ]


@pytest.mark.unit
def test_validation_run_summary_includes_gaps_field() -> None:
    gaps: list[dict[str, object]] = [
        {"file": "src/x.py", "missing_lines": ["100-200"]},
    ]
    run = _run(
        log_stream_refs={
            "coverage": {
                "percent": 88.0,
                "minimum_percent": 99.0,
                "status": "failed",
                "reason_code": "COVERAGE_BELOW_THRESHOLD",
                "gaps": gaps,
            }
        }
    )

    summary = validation_run_summary(run, current_target_head_sha="target-sha")

    assert summary.coverage_gaps == gaps
    assert summary.coverage_percent == 88.0
    assert summary.coverage_reason_code == "COVERAGE_BELOW_THRESHOLD"


@pytest.mark.unit
def test_validation_run_summary_exposes_failing_test_evidence() -> None:
    node_ids = ["tests/unit/test_widget.py::test_handles_edges"]
    evidence = ["FAILED tests/unit/test_widget.py::test_handles_edges - AssertionError"]
    run = _run(
        log_stream_refs={
            "coverage": {
                "percent": 99.2,
                "minimum_percent": 99.0,
                "status": "passed",
                "reason_code": "COVERAGE_OK",
                "failing_test_node_ids": node_ids,
                "failing_test_evidence": evidence,
            }
        }
    )

    summary = validation_run_summary(run, current_target_head_sha="target-sha")

    assert summary.failing_test_node_ids == node_ids
    assert summary.failing_test_evidence == evidence
    assert summary.coverage_percent == 99.2


@pytest.mark.unit
def test_validation_run_summary_response_has_coverage_gaps_field() -> None:
    response = ValidationRunSummaryResponse(
        validation_run_id="vr_x",
        tier=1,
        command_set_hash="a" * 64,
        status="failed",
        started_at=datetime(2026, 4, 26, 12, 0, tzinfo=UTC),
        coverage_gaps=[{"file": "a.py", "missing_lines": ["1"]}],
        failing_test_node_ids=["tests/unit/test_widget.py::test_handles_edges"],
        failing_test_evidence=["FAILED tests/unit/test_widget.py::test_handles_edges"],
    )

    assert response.coverage_gaps == [{"file": "a.py", "missing_lines": ["1"]}]
    assert response.failing_test_node_ids == ["tests/unit/test_widget.py::test_handles_edges"]
    assert response.failing_test_evidence == [
        "FAILED tests/unit/test_widget.py::test_handles_edges"
    ]


@pytest.mark.unit
def test_validation_provenance_item_response_has_coverage_gaps_field() -> None:
    response = ValidationProvenanceItemResponse(
        workspace_id="ws_x",
        phase="validate",
        command_index=0,
        command="pytest",
        stream_ids={},
        stdout_byte_count=0,
        stdout_line_count=0,
        stderr_byte_count=0,
        stderr_line_count=0,
        opened_at=datetime(2026, 4, 26, 12, 0, tzinfo=UTC),
        closed_at=datetime(2026, 4, 26, 12, 0, tzinfo=UTC),
        base_commit="abc",
        branch_name="branch",
        coverage_gaps=[{"file": "b.py", "missing_lines": ["5"]}],
        failing_test_node_ids=["tests/unit/test_widget.py::test_handles_edges"],
        failing_test_evidence=["FAILED tests/unit/test_widget.py::test_handles_edges"],
        status="failed",
        started_at=datetime(2026, 4, 26, 12, 0, tzinfo=UTC),
    )

    assert response.coverage_gaps == [{"file": "b.py", "missing_lines": ["5"]}]
    assert response.failing_test_node_ids == ["tests/unit/test_widget.py::test_handles_edges"]
    assert response.failing_test_evidence == [
        "FAILED tests/unit/test_widget.py::test_handles_edges"
    ]
