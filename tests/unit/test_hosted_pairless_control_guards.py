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
async def test_pairless_remonitor_refuses_live_execution_claim() -> None:
    row = _hosted_row()
    row.execution_claimed_by = "execution-worker"
    row.execution_claim_expires_at = datetime.now(UTC) + timedelta(minutes=5)
    session = _session(row)

    with pytest.raises(MaterialDeniedError, match="HOSTED_SOURCE_NOT_QUIESCENT") as denied:
        await operations.enforce_pairless_control_guards(session, row, control="remonitor")

    assert denied.value.status == 409


@pytest.mark.unit
async def test_pairless_remonitor_refuses_live_monitor_claim() -> None:
    row = _hosted_row()
    row.monitor_claimed_by = "monitor-worker"
    row.monitor_claim_expires_at = datetime.now(UTC) + timedelta(minutes=5)
    session = _session(row)

    with pytest.raises(MaterialDeniedError, match="HOSTED_SOURCE_NOT_QUIESCENT") as denied:
        await operations.enforce_pairless_control_guards(session, row, control="remonitor")

    assert denied.value.status == 409


@pytest.mark.unit
@pytest.mark.parametrize("op_id", ["op_running", "op_pending"])
async def test_pairless_remonitor_refuses_pending_or_running_operation(op_id: str) -> None:
    row = _hosted_row()
    session = _session(row, op_id=op_id)

    with pytest.raises(MaterialDeniedError, match="HOSTED_SOURCE_NOT_QUIESCENT") as denied:
        await operations.enforce_pairless_control_guards(session, row, control="remonitor")

    assert denied.value.status == 409


@pytest.mark.unit
async def test_pairless_remonitor_accepts_expired_claim_without_active_ops() -> None:
    row = _hosted_row()
    row.monitor_claimed_by = "stale-monitor"
    row.monitor_claim_expires_at = datetime.now(UTC) - timedelta(minutes=1)
    row.execution_claimed_by = "stale-execution"
    row.execution_claim_expires_at = datetime.now(UTC) - timedelta(minutes=2)
    before = _snapshot(row)
    session = _session(row, op_id=None)

    await operations.enforce_pairless_control_guards(session, row, control="remonitor")

    assert _snapshot(row) == before


@pytest.mark.unit
async def test_control_authority_defers_remonitor_quiescence_to_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auth stays in control_authority; remonitor quiescence runs after exact replay."""
    row = _hosted_row()
    row.monitor_claimed_by = "monitor-worker"
    row.monitor_claim_expires_at = datetime.now(UTC) + timedelta(minutes=5)
    before = _snapshot(row)

    # Live claim would fail the remonitor guard, but control_authority must not
    # raise here — remonitor_workspace applies the guard only after prepare.
    await _pairless(monkeypatch, row, control="remonitor")

    assert _snapshot(row) == before


@pytest.mark.unit
async def test_remonitor_exact_replay_skips_quiescence_after_monitor_reclaim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same-key remonitor retry must return the stored op even if a worker reclaimed."""
    from awf.service.controls import WorkspaceControlService
    from awf.service.controls_types import _PreparedOperation, _PreparedOperationKind

    row = _hosted_row(status="monitoring_pr")
    row.monitor_claimed_by = "monitor-worker"
    row.monitor_claim_expires_at = datetime.now(UTC) + timedelta(minutes=5)
    row.pr_url = "https://github.com/example/repo/pull/1"
    row.version = 1
    stored_op = SimpleNamespace(
        id="op_remonitor_done",
        workspace_id=row.id,
        type="remonitor",
        status="succeeded",
        payload={},
        result=None,
    )
    session = SimpleNamespace(refresh=AsyncMock())
    service = WorkspaceControlService(
        session,  # type: ignore[arg-type]
        project_stopper=AsyncMock(),
        cleaner_factory=lambda *_a, **_k: AsyncMock(),
    )
    prepared = _PreparedOperation(
        workspace=row,  # type: ignore[arg-type]
        replay=stored_op,  # type: ignore[arg-type]
        kind=_PreparedOperationKind.exact_replay,
        idempotency_key="remonitor-retry",
    )
    guard = AsyncMock(side_effect=MaterialDeniedError("HOSTED_SOURCE_NOT_QUIESCENT", 409))

    monkeypatch.setattr("awf.service.controls.control_authority", AsyncMock())
    monkeypatch.setattr(
        "awf.service.controls.WorkspaceRepository",
        lambda _s: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "awf.service.controls.OperationRepository",
        lambda _s: SimpleNamespace(),
    )
    monkeypatch.setattr(service, "_require_workspace", AsyncMock(return_value=row))
    monkeypatch.setattr(
        "awf.service.controls.remonitor_payload",
        lambda *_a, **_k: ({"reason": "retry"}, {"reason": "retry"}),
    )
    monkeypatch.setattr(service, "_prepare_operation", AsyncMock(return_value=prepared))
    monkeypatch.setattr("awf.service.controls.authority_for", lambda _s: object())
    monkeypatch.setattr("awf.service.controls.enforce_pairless_control_guards", guard)

    response = await service.remonitor_workspace(
        row.id,
        reason="retry",
        idempotency_key="remonitor-retry",
        hosted_authorization=_authorization(),
    )

    assert response.operation_id == "op_remonitor_done"
    guard.assert_not_awaited()
    session.refresh.assert_not_awaited()


@pytest.mark.unit
async def test_remonitor_fresh_pairless_applies_quiescence_after_prepare(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fresh hosted remonitor still refuses live claims once exact replay is ruled out."""
    from awf.service.controls import WorkspaceControlService
    from awf.service.controls_types import _PreparedOperation

    row = _hosted_row(status="monitoring_pr")
    row.monitor_claimed_by = "monitor-worker"
    row.monitor_claim_expires_at = datetime.now(UTC) + timedelta(minutes=5)
    row.pr_url = "https://github.com/example/repo/pull/1"
    row.version = 1
    session = SimpleNamespace(refresh=AsyncMock())
    service = WorkspaceControlService(
        session,  # type: ignore[arg-type]
        project_stopper=AsyncMock(),
        cleaner_factory=lambda *_a, **_k: AsyncMock(),
    )
    prepared = _PreparedOperation(
        workspace=row,  # type: ignore[arg-type]
        idempotency_key="remonitor-fresh",
    )
    guard = AsyncMock(side_effect=MaterialDeniedError("HOSTED_SOURCE_NOT_QUIESCENT", 409))

    monkeypatch.setattr("awf.service.controls.control_authority", AsyncMock())
    monkeypatch.setattr(
        "awf.service.controls.WorkspaceRepository",
        lambda _s: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "awf.service.controls.OperationRepository",
        lambda _s: SimpleNamespace(),
    )
    monkeypatch.setattr(service, "_require_workspace", AsyncMock(return_value=row))
    monkeypatch.setattr(
        "awf.service.controls.remonitor_payload",
        lambda *_a, **_k: ({"reason": "fresh"}, {"reason": "fresh"}),
    )
    monkeypatch.setattr(service, "_prepare_operation", AsyncMock(return_value=prepared))
    monkeypatch.setattr("awf.service.controls.authority_for", lambda _s: object())
    monkeypatch.setattr("awf.service.controls.enforce_pairless_control_guards", guard)

    with pytest.raises(MaterialDeniedError, match="HOSTED_SOURCE_NOT_QUIESCENT"):
        await service.remonitor_workspace(
            row.id,
            reason="fresh",
            idempotency_key="remonitor-fresh",
            hosted_authorization=_authorization(),
        )

    session.refresh.assert_awaited_once_with(row, with_for_update=True)
    guard.assert_awaited_once()
    assert guard.await_args.args[1] is row
    assert guard.await_args.kwargs["control"] == "remonitor"


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
