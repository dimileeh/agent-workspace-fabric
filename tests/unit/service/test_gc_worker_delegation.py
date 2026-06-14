"""Pure-fold unit tests for ``gc_worker_delegation`` (#582).

These cover the summation of reclaimed counts/bytes, the ``worker_reclaim`` shape,
and the status downgrade on a delegation error or a partial worker reap.
"""

from __future__ import annotations

import pytest

from awf.service.gc_worker_delegation import (
    SERVICE_GC_WORKER_DELEGATION_TIMEOUT,
    SERVICE_GC_WORKER_RECLAIM_FAILED,
    SERVICE_GC_WORKER_RECLAIMED,
    SERVICE_GC_WORKER_UNAVAILABLE,
    WorkerReclaimOutcome,
    fold_worker_reclaim,
)

pytestmark = pytest.mark.unit


def _api_base(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "status": "succeeded",
        "reason_code": "CLEANUP_EXECUTION_SUCCEEDED",
        "dry_run": False,
        "deleted_path_count": 2,
        "total_estimated_bytes": 100,
    }
    base.update(overrides)
    return base


def test_fold_sums_worker_reclaim_into_headline() -> None:
    report = {
        "status": "succeeded",
        "reason_code": "CLEANUP_EXECUTION_SUCCEEDED",
        "deleted_path_count": 3,
        "total_estimated_bytes": 1_700_000_000,
    }
    outcome = WorkerReclaimOutcome.from_report(report)

    folded = fold_worker_reclaim(_api_base(), outcome)

    # Headline numbers are the API-side reclaim PLUS the worker's reclaim.
    assert folded["deleted_path_count"] == 5
    assert folded["total_estimated_bytes"] == 1_700_000_100
    assert folded["status"] == "succeeded"
    worker_reclaim = folded["worker_reclaim"]
    assert isinstance(worker_reclaim, dict)
    assert worker_reclaim["status"] == "completed"
    assert worker_reclaim["reason_code"] == SERVICE_GC_WORKER_RECLAIMED
    assert worker_reclaim["deleted_path_count"] == 3
    assert worker_reclaim["total_estimated_bytes"] == 1_700_000_000
    assert worker_reclaim["report"] == report


def test_fold_does_not_mutate_base() -> None:
    base = _api_base()
    outcome = WorkerReclaimOutcome.from_report({"deleted_path_count": 1})

    fold_worker_reclaim(base, outcome)

    assert base["deleted_path_count"] == 2
    assert "worker_reclaim" not in base


def test_fold_worker_partial_downgrades_status() -> None:
    report = {
        "status": "partial",
        "reason_code": "CLEANUP_EXECUTION_PARTIAL",
        "deleted_path_count": 1,
        "total_estimated_bytes": 10,
    }
    outcome = WorkerReclaimOutcome.from_report(report)

    folded = fold_worker_reclaim(_api_base(), outcome)

    assert folded["status"] == "partial"
    assert folded["reason_code"] == "CLEANUP_EXECUTION_PARTIAL"
    # Reclaimed totals still fold in even on a partial worker sweep.
    assert folded["deleted_path_count"] == 3


def test_fold_worker_unavailable_downgrades_and_preserves_api_reclaim() -> None:
    outcome = WorkerReclaimOutcome.unavailable("no fresh worker heartbeat for node node-a")

    folded = fold_worker_reclaim(_api_base(), outcome)

    assert folded["status"] == "partial"
    assert folded["reason_code"] == SERVICE_GC_WORKER_UNAVAILABLE
    # The API-side reclaim is preserved (never zeroed) and not inflated.
    assert folded["deleted_path_count"] == 2
    assert folded["total_estimated_bytes"] == 100
    worker_reclaim = folded["worker_reclaim"]
    assert isinstance(worker_reclaim, dict)
    assert worker_reclaim["status"] == "unavailable"
    assert worker_reclaim["deleted_path_count"] == 0


def test_fold_timeout_and_failed_reason_codes() -> None:
    timed_out = fold_worker_reclaim(
        _api_base(), WorkerReclaimOutcome.delegation_timeout("deadline exceeded")
    )
    assert timed_out["status"] == "partial"
    assert timed_out["reason_code"] == SERVICE_GC_WORKER_DELEGATION_TIMEOUT

    failed = fold_worker_reclaim(
        _api_base(),
        WorkerReclaimOutcome.reclaim_failed("boom", report={"status": "failed"}),
    )
    assert failed["status"] == "partial"
    assert failed["reason_code"] == SERVICE_GC_WORKER_RECLAIM_FAILED
    worker_reclaim = failed["worker_reclaim"]
    assert isinstance(worker_reclaim, dict)
    assert worker_reclaim["message"] == "boom"
    assert worker_reclaim["report"] == {"status": "failed"}


def test_from_report_coerces_missing_and_float_fields() -> None:
    outcome = WorkerReclaimOutcome.from_report(
        {"deleted_path_count": True, "total_estimated_bytes": 12.9}
    )
    assert outcome.deleted_path_count == 1
    assert outcome.total_estimated_bytes == 12

    empty = WorkerReclaimOutcome.from_report({})
    assert empty.deleted_path_count == 0
    assert empty.total_estimated_bytes == 0
    assert empty.succeeded is True


def test_fold_partial_worker_reap_without_reason_code_falls_back() -> None:
    # A partial worker report missing its reason_code still downgrades the
    # headline to the canonical partial reason code.
    outcome = WorkerReclaimOutcome.from_report({"status": "partial"})

    folded = fold_worker_reclaim(_api_base(), outcome)

    assert folded["status"] == "partial"
    assert folded["reason_code"] == "CLEANUP_EXECUTION_PARTIAL"
