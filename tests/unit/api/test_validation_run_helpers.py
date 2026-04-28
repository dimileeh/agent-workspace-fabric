"""Helper-level tests for validation run summary normalization."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from awf.api.validation_runs import (
    fresh_for_target,
    validation_coverage_fields,
    validation_run_summary,
)
from awf.db.models import ValidationRun


def _run(**overrides: object) -> ValidationRun:
    values = {
        "id": "vr_helper_00000000000001",
        "workspace_id": "ws_helper",
        "attempt_id": "ta_helper",
        "tier": 1,
        "command_set_hash": "a" * 64,
        "commands": [],
        "base_commit": "base-sha",
        "target_branch": "main",
        "target_head_sha": "target-sha",
        "status": "succeeded",
        "reason_code": "VALIDATION_OK",
        "started_at": datetime(2026, 4, 26, 12, 0, tzinfo=UTC),
        "finished_at": datetime(2026, 4, 26, 12, 5, tzinfo=UTC),
        "log_stream_refs": {},
        "retry_count": 0,
    }
    values.update(overrides)
    return ValidationRun(**values)


@pytest.mark.unit
def test_validation_run_summary_normalizes_unknown_status_and_naive_datetimes() -> None:
    summary = validation_run_summary(
        _run(
            status="queued",
            tier=99,
            started_at=datetime(2026, 4, 26, 12, 0),
            finished_at=datetime(2026, 4, 26, 8, 30, tzinfo=timezone(timedelta(hours=-4))),
            log_stream_refs=["not", "a", "mapping"],
        ),
        current_target_head_sha="target-sha",
    )

    assert summary.status == "unknown"
    assert summary.tier == 1
    assert summary.started_at == datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    assert summary.finished_at == datetime(2026, 4, 26, 12, 30, tzinfo=UTC)
    assert summary.log_stream_refs == {}
    assert summary.fresh_for_target is True


@pytest.mark.unit
def test_validation_run_summary_preserves_running_failed_and_tier_three_states() -> None:
    running = validation_run_summary(
        _run(status="running", tier=3, finished_at=None),
        current_target_head_sha="different-sha",
    )
    failed = validation_run_summary(
        _run(status="failed", tier=2, target_head_sha=None),
        current_target_head_sha="target-sha",
    )

    assert running.status == "running"
    assert running.tier == 3
    assert running.finished_at is None
    assert running.fresh_for_target is False
    assert failed.status == "failed"
    assert failed.tier == 2
    assert failed.fresh_for_target is None


@pytest.mark.unit
def test_validation_coverage_fields_ignore_non_contract_value_types() -> None:
    fields = validation_coverage_fields(
        _run(
            log_stream_refs={
                "coverage": {
                    "percent": True,
                    "minimum_percent": "99.0",
                    "status": False,
                    "reason_code": 123,
                }
            }
        )
    )

    assert fields == {
        "coverage_percent": None,
        "coverage_minimum_percent": None,
        "coverage_status": None,
        "coverage_reason_code": None,
        "coverage_gaps": [],
    }


@pytest.mark.unit
def test_fresh_for_target_handles_true_false_and_unknown_cases() -> None:
    assert (
        fresh_for_target(validation_target_head_sha="abc", current_target_head_sha="abc")
        is True
    )
    assert (
        fresh_for_target(validation_target_head_sha="abc", current_target_head_sha="def")
        is False
    )
    assert (
        fresh_for_target(validation_target_head_sha="", current_target_head_sha="def")
        is None
    )
