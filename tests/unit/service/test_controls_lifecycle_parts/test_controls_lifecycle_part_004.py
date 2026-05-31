"""Workspace control lifecycle recovery and replay tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from awf.db.enums import OperationStatus, OperationType, WorkspaceStatus
from awf.db.models import Workspace, WorkspaceEvent
from awf.db.repositories import OperationRepository, WorkspaceRepository
from awf.service.controls import (
    IdempotencyConflictError,
    VersionConflictError,
    WorkspaceRebaseStateError,
)
from tests.postgres import postgres_test_session
from tests.unit.service.test_controls_lifecycle_parts.test_controls_lifecycle_part_001 import (
    _events,
    _operations,
    _service,
    _workspace,
    _workspace_with_candidate,
)


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with postgres_test_session() as s:
        yield s


@pytest.mark.unit
async def test_remonitor_failed_workspace_reserves_state_reset_event_order(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.failed)
    workspace.pr_url = "https://github.com/example/control-lifecycle/pull/43"
    workspace.pr_number = 43
    await session.flush()
    service, _stopper, _cleaner = _service(session)
    calls: list[tuple[str, str, str, str]] = []
    original_add_event_with_states = WorkspaceRepository.add_event_with_states

    async def _recording_add_event_with_states(
        self: WorkspaceRepository,
        event_workspace: Workspace,
        *,
        event_type: str,
        old_state: WorkspaceStatus | str,
        new_state: WorkspaceStatus | str,
        reason_code: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> WorkspaceEvent:
        calls.append((event_workspace.id, event_type, str(old_state), str(new_state)))
        return await original_add_event_with_states(
            self,
            event_workspace,
            event_type=event_type,
            old_state=old_state,
            new_state=new_state,
            reason_code=reason_code,
            payload=payload,
        )

    monkeypatch.setattr(
        WorkspaceRepository,
        "add_event_with_states",
        _recording_add_event_with_states,
    )

    await service.remonitor_workspace(
        workspace.id,
        reason="reattach failed PR",
        idempotency_key="remonitor-failed-pr-reserve-order",
        expected_version=workspace.version,
    )
    events = await _events(session, workspace.id)

    assert calls == [
        (
            workspace.id,
            "workspace.remonitor_requested",
            WorkspaceStatus.failed.value,
            WorkspaceStatus.monitoring_pr.value,
        )
    ]
    assert workspace.version == 2
    assert events[0].event_order == 2
    assert events[0].old_state == WorkspaceStatus.failed.value
    assert events[0].new_state == WorkspaceStatus.monitoring_pr.value


@pytest.mark.unit
async def test_remonitor_failed_workspace_cancels_stale_pr_monitor_recovery_ops(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.failed)
    workspace.pr_url = "https://github.com/example/control-lifecycle/pull/44"
    workspace.pr_number = 44
    workspace.failure_reason = "infrastructure_failure"
    workspace.failure_message = "monitor process died during validation recovery"
    operation_repo = OperationRepository(session)
    stale_validate = await operation_repo.create(
        workspace_id=workspace.id,
        operation_type=OperationType.validate,
        status=OperationStatus.running,
        payload={
            "owner": "pr_monitor",
            "source": "pr_monitor",
            "recovery_mode": "validate_only",
            "reason_code": "VALIDATION_INSUFFICIENT_TIER",
        },
    )
    operator_validate = await operation_repo.create(
        workspace_id=workspace.id,
        operation_type=OperationType.validate,
        status=OperationStatus.running,
        payload={
            "owner": "operator",
            "source": "operator_api",
            "recovery_mode": "validate_only",
            "reason_code": "OPERATOR_VALIDATE",
        },
    )
    await session.flush()
    service, _stopper, _cleaner = _service(session)

    response = await service.remonitor_workspace(
        workspace.id,
        reason="reattach after stale validation op",
        idempotency_key="remonitor-cancel-stale-recovery",
        expected_version=workspace.version,
    )
    operations = {op.id: op for op in await _operations(session, workspace.id)}
    events = await _events(session, workspace.id)

    assert response.status == WorkspaceStatus.monitoring_pr
    assert operations[stale_validate.id].status == OperationStatus.cancelled.value
    assert operations[stale_validate.id].error_code == "OPERATOR_REMONITOR"
    assert operations[stale_validate.id].result == {
        "status": "cancelled",
        "reason_code": "OPERATOR_REMONITOR",
        "requested_action": "remonitor",
    }
    assert operations[operator_validate.id].status == OperationStatus.running.value
    remonitor_event = next(
        event for event in events if event.event_type == "workspace.remonitor_requested"
    )
    assert remonitor_event.payload["cancelled_recovery_operations"] == [
        {
            "operation_id": stale_validate.id,
            "operation_type": OperationType.validate.value,
            "operation_status": OperationStatus.running.value,
        }
    ]


@pytest.mark.unit
async def test_refresh_replays_same_idempotency_key_after_destroying_state(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.ready)
    service, _stopper, _cleaner = _service(session)

    operation = await service.request_refresh_workspace(
        workspace.id,
        reason="stale merge queue",
        idempotency_key="refresh-before-destroy",
    )
    workspace.status = WorkspaceStatus.destroying.value
    await session.flush()

    replay = await service.request_refresh_workspace(
        workspace.id,
        reason="stale merge queue",
        idempotency_key="refresh-before-destroy",
    )
    operations = await _operations(session, workspace.id)

    assert replay.id == operation.id
    assert replay.status == OperationStatus.pending.value
    assert [row.id for row in operations] == [operation.id]


@pytest.mark.unit
async def test_validate_monitoring_pr_creates_validate_only_operation_and_coalesces(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.monitoring_pr)
    workspace.pr_url = "https://github.com/example/control-lifecycle/pull/44"
    workspace.pr_number = 44
    await session.flush()
    service, _stopper, _cleaner = _service(session)

    operation = await service.request_validate_workspace(
        workspace.id,
        reason="rerun required validation",
        requested_tier=2,
        idempotency_key="validate-first",
        expected_version=workspace.version,
    )
    replay = await service.request_validate_workspace(
        workspace.id,
        reason="rerun required validation",
        requested_tier=2,
        idempotency_key="validate-fresh-key",
    )
    operations = await _operations(session, workspace.id)
    events = await _events(session, workspace.id)
    validate_event = next(
        event for event in events if event.event_type == "workspace.validate_requested"
    )
    state_event = next(
        event
        for event in events
        if event.event_type == "workspace.state_changed"
        and event.reason_code == "OPERATOR_VALIDATE"
    )

    assert replay.id == operation.id
    assert workspace.status == WorkspaceStatus.ready.value
    assert workspace.version == 2
    assert [row.id for row in operations] == [operation.id]
    assert operation.type == OperationType.validate.value
    assert operation.status == OperationStatus.pending.value
    assert operation.idempotency_key == "validate-first"
    assert operation.payload == {
        "owner": "operator_api",
        "source": "operator_api",
        "reason": "rerun required validation",
        "reason_code": "OPERATOR_VALIDATE",
        "requested_action": "validate",
        "recovery_mode": "validate_only",
        "requested_tier": 2,
        "expected_version": 1,
    }
    assert validate_event.reason_code == "OPERATOR_VALIDATE"
    assert validate_event.payload == {
        "source": "operator_api",
        "reason": "rerun required validation",
        "operation_id": operation.id,
        "recovery_mode": "validate_only",
        "requested_tier": 2,
        "expected_version": 1,
    }
    assert state_event.old_state == WorkspaceStatus.monitoring_pr.value
    assert state_event.new_state == WorkspaceStatus.ready.value


@pytest.mark.unit
async def test_validate_fresh_key_with_stale_if_match_does_not_coalesce_active_operation(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.monitoring_pr)
    workspace.pr_url = "https://github.com/example/control-lifecycle/pull/48"
    workspace.pr_number = 48
    await session.flush()
    service, _stopper, _cleaner = _service(session)

    operation = await service.request_validate_workspace(
        workspace.id,
        reason="rerun required validation",
        requested_tier=2,
        idempotency_key="validate-original",
        expected_version=workspace.version,
    )

    replay = await service.request_validate_workspace(
        workspace.id,
        reason="rerun required validation",
        requested_tier=2,
        idempotency_key="validate-original",
        expected_version=1,
    )
    with pytest.raises(VersionConflictError) as exc_info:
        await service.request_validate_workspace(
            workspace.id,
            reason="rerun required validation",
            requested_tier=2,
            idempotency_key="validate-fresh-stale-version",
            expected_version=1,
        )

    assert replay.id == operation.id
    assert workspace.version == 2
    assert exc_info.value.detail == {"expected_version": 1, "actual_version": 2}
    assert [row.id for row in await _operations(session, workspace.id)] == [operation.id]


@pytest.mark.unit
@pytest.mark.parametrize(
    "transient_status",
    [WorkspaceStatus.running, WorkspaceStatus.validating],
)
async def test_validate_replay_coalesces_during_executor_transient_states(
    session: AsyncSession,
    transient_status: WorkspaceStatus,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.monitoring_pr)
    workspace.pr_url = "https://github.com/example/control-lifecycle/pull/47"
    workspace.pr_number = 47
    await session.flush()
    service, _stopper, _cleaner = _service(session)

    operation = await service.request_validate_workspace(
        workspace.id,
        reason="rerun required validation",
        requested_tier=2,
    )
    operation.status = OperationStatus.running.value
    workspace.status = transient_status.value
    await session.flush()

    replay = await service.request_validate_workspace(
        workspace.id,
        reason="rerun required validation",
        requested_tier=2,
    )

    assert replay.id == operation.id
    assert [row.id for row in await _operations(session, workspace.id)] == [operation.id]


@pytest.mark.unit
async def test_validate_without_requested_tier_omits_tier_from_payload(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.monitoring_pr)
    workspace.pr_url = "https://github.com/example/control-lifecycle/pull/46"
    workspace.pr_number = 46
    await session.flush()
    service, _stopper, _cleaner = _service(session)

    operation = await service.request_validate_workspace(
        workspace.id,
        reason="rerun default validation",
    )
    events = await _events(session, workspace.id)
    validate_event = next(
        event for event in events if event.event_type == "workspace.validate_requested"
    )

    assert operation.payload is not None
    assert "requested_tier" not in operation.payload
    assert validate_event.payload is not None
    assert "requested_tier" not in validate_event.payload


@pytest.mark.unit
async def test_validate_same_key_with_different_requested_tier_conflicts(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.monitoring_pr)
    workspace.pr_url = "https://github.com/example/control-lifecycle/pull/45"
    workspace.pr_number = 45
    await session.flush()
    service, _stopper, _cleaner = _service(session)

    await service.request_validate_workspace(
        workspace.id,
        reason="rerun required validation",
        requested_tier=2,
        idempotency_key="validate-tier-conflict",
    )

    with pytest.raises(IdempotencyConflictError):
        await service.request_validate_workspace(
            workspace.id,
            reason="rerun required validation",
            requested_tier=3,
            idempotency_key="validate-tier-conflict",
        )


@pytest.mark.unit
async def test_rebase_monitoring_pr_creates_rebase_operation_and_replays_exact_key(
    session: AsyncSession,
) -> None:
    workspace, candidate = await _workspace_with_candidate(session)
    service, _stopper, _cleaner = _service(session)

    operation = await service.request_rebase_workspace(
        workspace.id,
        reason="base branch advanced",
        idempotency_key="rebase-first",
        expected_version=workspace.version,
    )
    replay = await service.request_rebase_workspace(
        workspace.id,
        reason="base branch advanced",
        idempotency_key="rebase-first",
        expected_version=1,
    )
    with pytest.raises(WorkspaceRebaseStateError) as fresh_key_error:
        await service.request_rebase_workspace(
            workspace.id,
            reason="base branch advanced",
            idempotency_key="rebase-fresh-key",
        )
    operations = await _operations(session, workspace.id)
    events = await _events(session, workspace.id)
    rebase_event = next(
        event for event in events if event.event_type == "workspace.rebase_requested"
    )
    state_event = next(
        event
        for event in events
        if event.event_type == "workspace.state_changed" and event.reason_code == "OPERATOR_REBASE"
    )

    assert replay.id == operation.id
    assert fresh_key_error.value.detail == {
        "status": WorkspaceStatus.ready.value,
        "eligible_statuses": [WorkspaceStatus.monitoring_pr.value],
    }
    assert workspace.status == WorkspaceStatus.ready.value
    assert workspace.version == 2
    assert [row.id for row in operations] == [operation.id]
    assert operation.type == OperationType.rebase.value
    assert operation.status == OperationStatus.pending.value
    assert operation.idempotency_key == "rebase-first"
    assert operation.payload == {
        "owner": "operator_api",
        "source": "operator_api",
        "reason": "base branch advanced",
        "reason_code": "OPERATOR_REBASE",
        "requested_action": "rebase",
        "recovery_mode": "rebase_only",
        "candidate_id": candidate.id,
        "attempt_id": candidate.attempt_id,
        "task_id": candidate.task_id,
        "pr_number": 50,
        "pr_url": "https://github.com/example/control-lifecycle/pull/50",
        "source_head_sha": "h" * 40,
        "source_base_sha": "a" * 40,
        "target_branch": "development",
        "remote_branch": f"awf/{workspace.id}",
        "expected_version": 1,
    }
    assert rebase_event.reason_code == "OPERATOR_REBASE"
    assert rebase_event.payload == {
        "source": "operator_api",
        "reason": "base branch advanced",
        "operation_id": operation.id,
        "recovery_mode": "rebase_only",
        "candidate_id": candidate.id,
        "expected_version": 1,
    }
    assert state_event.old_state == WorkspaceStatus.monitoring_pr.value
    assert state_event.new_state == WorkspaceStatus.ready.value


@pytest.mark.unit
async def test_rebase_fresh_key_with_stale_if_match_does_not_coalesce_active_operation(
    session: AsyncSession,
) -> None:
    workspace, _candidate = await _workspace_with_candidate(session)
    service, _stopper, _cleaner = _service(session)

    operation = await service.request_rebase_workspace(
        workspace.id,
        reason="base branch advanced",
        idempotency_key="rebase-original",
        expected_version=workspace.version,
    )

    replay = await service.request_rebase_workspace(
        workspace.id,
        reason="base branch advanced",
        idempotency_key="rebase-original",
        expected_version=1,
    )
    with pytest.raises(VersionConflictError) as exc_info:
        await service.request_rebase_workspace(
            workspace.id,
            reason="base branch advanced",
            idempotency_key="rebase-fresh-stale-version",
            expected_version=1,
        )

    assert replay.id == operation.id
    assert workspace.version == 2
    assert exc_info.value.detail == {"expected_version": 1, "actual_version": 2}
    assert [row.id for row in await _operations(session, workspace.id)] == [operation.id]


@pytest.mark.unit
async def test_rebase_same_key_with_different_reason_conflicts(
    session: AsyncSession,
) -> None:
    workspace, _candidate = await _workspace_with_candidate(session)
    service, _stopper, _cleaner = _service(session)

    await service.request_rebase_workspace(
        workspace.id,
        reason="base branch advanced",
        idempotency_key="rebase-reason-conflict",
    )

    with pytest.raises(IdempotencyConflictError):
        await service.request_rebase_workspace(
            workspace.id,
            reason="different base branch reason",
            idempotency_key="rebase-reason-conflict",
        )
