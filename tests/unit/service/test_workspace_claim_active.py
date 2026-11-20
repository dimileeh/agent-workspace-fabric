"""Unit tests for ``workspace_claim_active`` read-time claim liveness."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from awf.service.workspaces_response import workspace_claim_active


def _row(
    *,
    execution_claimed_by: str | None = None,
    execution_claim_expires_at: datetime | None = None,
    monitor_claimed_by: str | None = None,
    monitor_claim_expires_at: datetime | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        execution_claimed_by=execution_claimed_by,
        execution_claim_expires_at=execution_claim_expires_at,
        monitor_claimed_by=monitor_claimed_by,
        monitor_claim_expires_at=monitor_claim_expires_at,
    )


@pytest.mark.unit
def test_workspace_claim_active_false_with_no_claim() -> None:
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)

    assert workspace_claim_active(_row(), now=now) is False


@pytest.mark.unit
def test_workspace_claim_active_true_for_live_execution_claim() -> None:
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)

    assert (
        workspace_claim_active(
            _row(
                execution_claimed_by="worker-1",
                execution_claim_expires_at=now + timedelta(minutes=5),
            ),
            now=now,
        )
        is True
    )


@pytest.mark.unit
def test_workspace_claim_active_true_for_live_monitor_claim() -> None:
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)

    assert (
        workspace_claim_active(
            _row(
                monitor_claimed_by="monitor-1",
                monitor_claim_expires_at=now + timedelta(minutes=5),
            ),
            now=now,
        )
        is True
    )


@pytest.mark.unit
def test_workspace_claim_active_false_for_expired_execution_claim() -> None:
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)

    assert (
        workspace_claim_active(
            _row(
                execution_claimed_by="worker-1",
                execution_claim_expires_at=now - timedelta(seconds=1),
            ),
            now=now,
        )
        is False
    )


@pytest.mark.unit
def test_workspace_claim_active_false_when_owner_set_with_null_expiry() -> None:
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)

    assert (
        workspace_claim_active(
            _row(execution_claimed_by="worker-1", execution_claim_expires_at=None),
            now=now,
        )
        is False
    )
    assert (
        workspace_claim_active(
            _row(monitor_claimed_by="monitor-1", monitor_claim_expires_at=None),
            now=now,
        )
        is False
    )


@pytest.mark.unit
def test_workspace_claim_active_naive_expiry_against_aware_now() -> None:
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    naive_future = now.replace(tzinfo=None) + timedelta(minutes=5)

    assert (
        workspace_claim_active(
            _row(
                execution_claimed_by="worker-1",
                execution_claim_expires_at=naive_future,
            ),
            now=now,
        )
        is True
    )


@pytest.mark.unit
def test_workspace_claim_active_aware_expiry_against_naive_now() -> None:
    aware_now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    naive_now = aware_now.replace(tzinfo=None)

    assert (
        workspace_claim_active(
            _row(
                monitor_claimed_by="monitor-1",
                monitor_claim_expires_at=aware_now + timedelta(minutes=5),
            ),
            now=naive_now,
        )
        is True
    )
    assert (
        workspace_claim_active(
            _row(
                monitor_claimed_by="monitor-1",
                monitor_claim_expires_at=aware_now - timedelta(minutes=5),
            ),
            now=naive_now,
        )
        is False
    )


@pytest.mark.unit
def test_workspace_claim_active_non_utc_aware_expiry_converts_before_compare() -> None:
    plus_530 = timezone(timedelta(hours=5, minutes=30))
    aware_now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    naive_now = aware_now.replace(tzinfo=None)
    expired_other_zone = (aware_now - timedelta(minutes=5)).astimezone(plus_530)
    live_other_zone = (aware_now + timedelta(minutes=5)).astimezone(plus_530)

    assert (
        workspace_claim_active(
            _row(
                execution_claimed_by="worker-1",
                execution_claim_expires_at=expired_other_zone,
            ),
            now=naive_now,
        )
        is False
    )
    assert (
        workspace_claim_active(
            _row(
                execution_claimed_by="worker-1",
                execution_claim_expires_at=live_other_zone,
            ),
            now=naive_now,
        )
        is True
    )
