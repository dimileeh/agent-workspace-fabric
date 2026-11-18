"""Pair-less hosted guide/remonitor guards under the control_authority row lock."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request

from awf.hosted_node import lifecycle_operations as operations
from awf.hosted_node.api import denial_error
from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.runtime_schemas import HostedControlAuthorization, HostedWorkspace
from tests.unit.test_hosted_runtime_contract import hosted_create


def _authorization() -> HostedControlAuthorization:
    return HostedControlAuthorization(namespace="tenant", actor="caller", allow_grants=True)


def _hosted_row(*, status: str = "monitoring_pr") -> SimpleNamespace:
    stored = HostedWorkspace.model_validate(hosted_create()["hosted"])
    return SimpleNamespace(
        id=stored.workspace_id,
        node_id=stored.credentials.agent.identity.owner,
        agent="codex",
        status=status,
        execution_claimed_by=None,
        execution_claim_expires_at=None,
        monitor_claimed_by=None,
        monitor_claim_expires_at=None,
        task_policy={"hosted_node": stored.model_dump(mode="json")},
        _association=stored,
    )


def _authority(row: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(
        receiver=SimpleNamespace(identity=row._association.credentials.agent.identity),
        materials=AsyncMock(),
    )


def _session(row: SimpleNamespace, *, op_id: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        get=AsyncMock(return_value=row),
        refresh=AsyncMock(),
        scalar=AsyncMock(return_value=op_id),
        info={},
    )


async def _pairless(
    monkeypatch: pytest.MonkeyPatch,
    row: SimpleNamespace,
    *,
    control: str,
    grants: list[str] | None = None,
    op_id: str | None = None,
) -> None:
    authority = _authority(row)
    session = _session(row, op_id=op_id)
    monkeypatch.setattr(operations, "authority_for", lambda *_args: authority)
    monkeypatch.setattr(operations, "lock_materials", AsyncMock())
    await operations.control_authority(
        session,
        row.id,
        _authorization(),
        grants=grants,
        operator="caller",
        control=control,  # type: ignore[arg-type]
    )


def _snapshot(row: SimpleNamespace) -> dict[str, object]:
    return {
        "status": row.status,
        "execution_claimed_by": row.execution_claimed_by,
        "execution_claim_expires_at": row.execution_claim_expires_at,
        "monitor_claimed_by": row.monitor_claimed_by,
        "monitor_claim_expires_at": row.monitor_claim_expires_at,
        "task_policy": dict(row.task_policy),
    }


@pytest.mark.unit
async def test_pairless_guide_accepts_monitoring_pr_with_live_claim_and_running_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _hosted_row(status="monitoring_pr")
    future = datetime.now(UTC) + timedelta(minutes=10)
    row.monitor_claimed_by = "monitor-worker"
    row.monitor_claim_expires_at = future
    row.execution_claimed_by = "execution-worker"
    row.execution_claim_expires_at = future
    before = _snapshot(row)

    await _pairless(monkeypatch, row, control="guide", op_id="op_running")

    assert _snapshot(row) == before


@pytest.mark.unit
@pytest.mark.parametrize("status", ["blocked", "failed"])
async def test_pairless_guide_refuses_blocked_and_failed_without_mutation(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    row = _hosted_row(status=status)
    before = _snapshot(row)

    with pytest.raises(MaterialDeniedError, match="HOSTED_REAUTHORIZATION_REQUIRED") as denied:
        await _pairless(monkeypatch, row, control="guide")

    assert denied.value.status == 409
    assert _snapshot(row) == before


@pytest.mark.unit
@pytest.mark.parametrize("status", ["monitoring_pr", "blocked", "failed"])
async def test_pairless_guide_refuses_grants_in_every_state_without_mutation(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    row = _hosted_row(status=status)
    before = _snapshot(row)

    with pytest.raises(MaterialDeniedError, match="HOSTED_REAUTHORIZATION_REQUIRED") as denied:
        await _pairless(monkeypatch, row, control="guide", grants=["grant-a"])

    assert denied.value.status == 409
    assert _snapshot(row) == before


@pytest.mark.unit
async def test_hosted_reauthorization_required_denial_error_body() -> None:
    response = await denial_error(
        Request({"type": "http", "method": "POST", "path": "/v1/x", "headers": []}),
        MaterialDeniedError("HOSTED_REAUTHORIZATION_REQUIRED", 409),
    )

    assert response.status_code == 409
    assert json.loads(response.body) == {"error_code": "HOSTED_REAUTHORIZATION_REQUIRED"}


@pytest.mark.unit
async def test_pairless_remonitor_refuses_live_execution_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _hosted_row()
    row.execution_claimed_by = "execution-worker"
    row.execution_claim_expires_at = datetime.now(UTC) + timedelta(minutes=5)

    with pytest.raises(MaterialDeniedError, match="HOSTED_SOURCE_NOT_QUIESCENT") as denied:
        await _pairless(monkeypatch, row, control="remonitor")

    assert denied.value.status == 409


@pytest.mark.unit
async def test_pairless_remonitor_refuses_live_monitor_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _hosted_row()
    row.monitor_claimed_by = "monitor-worker"
    row.monitor_claim_expires_at = datetime.now(UTC) + timedelta(minutes=5)

    with pytest.raises(MaterialDeniedError, match="HOSTED_SOURCE_NOT_QUIESCENT") as denied:
        await _pairless(monkeypatch, row, control="remonitor")

    assert denied.value.status == 409


@pytest.mark.unit
@pytest.mark.parametrize("op_id", ["op_running", "op_pending"])
async def test_pairless_remonitor_refuses_pending_or_running_operation(
    monkeypatch: pytest.MonkeyPatch, op_id: str
) -> None:
    row = _hosted_row()

    with pytest.raises(MaterialDeniedError, match="HOSTED_SOURCE_NOT_QUIESCENT") as denied:
        await _pairless(monkeypatch, row, control="remonitor", op_id=op_id)

    assert denied.value.status == 409


@pytest.mark.unit
async def test_pairless_remonitor_accepts_expired_claim_without_active_ops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _hosted_row()
    row.monitor_claimed_by = "stale-monitor"
    row.monitor_claim_expires_at = datetime.now(UTC) - timedelta(minutes=1)
    row.execution_claimed_by = "stale-execution"
    row.execution_claim_expires_at = datetime.now(UTC) - timedelta(minutes=2)
    before = _snapshot(row)

    await _pairless(monkeypatch, row, control="remonitor", op_id=None)

    assert _snapshot(row) == before


@pytest.mark.unit
async def test_pairless_guards_skipped_when_control_kind_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Low-level callers may omit control; production guide/remonitor always pass it."""
    row = _hosted_row(status="blocked")
    authority = _authority(row)
    session = _session(row)
    monkeypatch.setattr(operations, "authority_for", lambda *_args: authority)
    monkeypatch.setattr(operations, "lock_materials", AsyncMock())

    await operations.control_authority(session, row.id, _authorization())


@pytest.mark.unit
async def test_non_hosted_guide_control_authority_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(task_policy={"other": True})),
        refresh=AsyncMock(),
        info={},
    )
    monkeypatch.setattr(operations, "authority_for", lambda *_args, **_kwargs: None)

    await operations.control_authority(
        session, "ws_local", None, control="guide", grants=["ignored"]
    )
    session.refresh.assert_not_awaited()


@pytest.mark.unit
async def test_non_hosted_remonitor_control_authority_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(task_policy=None)),
        refresh=AsyncMock(),
        info={},
    )
    monkeypatch.setattr(operations, "authority_for", lambda *_args, **_kwargs: None)

    await operations.control_authority(session, "ws_local", None, control="remonitor")
    session.refresh.assert_not_awaited()


@pytest.mark.unit
def test_pairless_claim_is_live_rejects_missing_owner_or_expiry() -> None:
    now = datetime.now(UTC)
    assert not operations._pairless_claim_is_live(None, now + timedelta(minutes=1), now=now)
    assert not operations._pairless_claim_is_live("worker", None, now=now)


@pytest.mark.unit
def test_pairless_claim_is_live_compares_naive_and_aware_expiries() -> None:
    now = datetime.now(UTC)
    naive_future = (now + timedelta(minutes=5)).replace(tzinfo=None)
    assert operations._pairless_claim_is_live("worker", naive_future, now=now)
    naive_now = now.replace(tzinfo=None)
    assert operations._pairless_claim_is_live("worker", now + timedelta(minutes=5), now=naive_now)
    assert not operations._pairless_claim_is_live(
        "worker", now - timedelta(minutes=5), now=naive_now
    )
