"""Workspace control lifecycle behavior tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from awf.db.base import Base
from awf.db.enums import AgentRuntime, OperationType, WorkspaceStatus
from awf.db.models import Operation, Workspace, WorkspaceEvent
from awf.db.repositories import OperationRepository, WorkspaceEventRepository, WorkspaceRepository
from awf.db.session import make_engine, make_session_factory
from awf.service.controls import (
    ActiveWorkspaceDestroyError,
    IdempotencyConflictError,
    VersionConflictError,
    WorkspaceControlService,
    WorkspaceNotFoundError,
    WorkspaceRemonitorMissingPrUrlError,
    WorkspaceRemonitorStateError,
    _json_datetime,
    default_cleaner,
    stop_project_containers,
)


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = make_session_factory(engine)
    async with factory() as s:
        yield s

    await engine.dispose()


@dataclass
class RecordingStopper:
    calls: list[str | None] = field(default_factory=list)

    async def __call__(self, compose_project_name: str | None) -> None:
        self.calls.append(compose_project_name)


@dataclass
class CleanupCall:
    workspace_id: str
    repo_url: str
    compose_project_name: str | None
    compose_file_path: Path | None
    worktree_host_path: Path | None
    remove_volumes: bool
    remove_worktree: bool


@dataclass
class RecordingCleaner:
    failures: list[str] = field(default_factory=list)
    calls: list[CleanupCall] = field(default_factory=list)

    async def cleanup(
        self,
        *,
        workspace_id: str,
        repo_url: str,
        compose_project_name: str | None = None,
        compose_file_path: Path | None = None,
        worktree_host_path: Path | None = None,
        remove_volumes: bool = True,
        remove_worktree: bool = True,
    ) -> list[str]:
        self.calls.append(
            CleanupCall(
                workspace_id=workspace_id,
                repo_url=repo_url,
                compose_project_name=compose_project_name,
                compose_file_path=compose_file_path,
                worktree_host_path=worktree_host_path,
                remove_volumes=remove_volumes,
                remove_worktree=remove_worktree,
            )
        )
        return list(self.failures)


async def _workspace(
    session: AsyncSession,
    *,
    status: WorkspaceStatus,
    title: str = "control lifecycle",
) -> Workspace:
    workspace = await WorkspaceRepository(session).create(
        repo_url="git@github.com:example/control-lifecycle.git",
        branch_base="development",
        task_title=title,
        task_prompt="Exercise control lifecycle behavior.",
        agent=AgentRuntime.codex.value,
        test_commands=["pytest -q"],
    )
    workspace.status = status.value
    workspace.compose_project_name = f"awf_{workspace.id}"
    workspace.compose_file_path = f"/tmp/{workspace.id}/compose.yml"
    await session.flush()
    return workspace


def _service(
    session: AsyncSession,
    *,
    stopper: RecordingStopper | None = None,
    cleaner: RecordingCleaner | None = None,
) -> tuple[WorkspaceControlService, RecordingStopper, RecordingCleaner]:
    stopper = stopper or RecordingStopper()
    cleaner = cleaner or RecordingCleaner()
    return (
        WorkspaceControlService(
            session,
            project_stopper=stopper,
            cleaner_factory=lambda: cleaner,
        ),
        stopper,
        cleaner,
    )


async def _operations(session: AsyncSession, workspace_id: str) -> list[Operation]:
    return await OperationRepository(session).list_for_workspace(workspace_id, limit=20)


async def _events(session: AsyncSession, workspace_id: str) -> list[WorkspaceEvent]:
    return await WorkspaceEventRepository(session).list(workspace_id=workspace_id, limit=20)


@pytest.mark.unit
async def test_cancel_active_workspace_stops_stack_transitions_and_replays(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.ready)
    service, stopper, _cleaner = _service(session)
    expected_version = workspace.version

    response = await service.cancel_workspace(
        workspace.id,
        reason="operator requested",
        stop_stack=True,
        idempotency_key="cancel-same-key",
        expected_version=expected_version,
    )
    replay = await service.cancel_workspace(
        workspace.id,
        reason="operator requested",
        stop_stack=True,
        idempotency_key="cancel-same-key",
        expected_version=expected_version,
    )
    operations = await _operations(session, workspace.id)

    assert response.operation_id == replay.operation_id
    assert response.message == "workspace cancellation requested"
    assert response.status == WorkspaceStatus.cancelled
    assert workspace.status == WorkspaceStatus.cancelled.value
    assert stopper.calls == [workspace.compose_project_name]
    assert [operation.type for operation in operations] == [OperationType.cancel.value]
    assert operations[0].status == "succeeded"
    assert operations[0].payload == {
        "reason": "operator requested",
        "stop_stack": True,
        "expected_version": 1,
    }
    assert operations[0].result == {"status": WorkspaceStatus.cancelled.value}


@pytest.mark.unit
async def test_cancel_terminal_workspace_records_request_event_without_transition(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.completed)
    service, stopper, _cleaner = _service(session)

    response = await service.cancel_workspace(
        workspace.id,
        reason=None,
        stop_stack=False,
    )
    events = await _events(session, workspace.id)

    assert response.status == WorkspaceStatus.completed
    assert stopper.calls == []
    assert workspace.status == WorkspaceStatus.completed.value
    assert events[0].event_type == "workspace.cancel_requested"
    assert events[0].reason_code == "OPERATOR_CANCEL"
    assert events[0].payload == {"reason": None, "stop_stack": False}


@pytest.mark.unit
async def test_stop_active_workspace_cancels_and_terminal_workspace_records_event(
    session: AsyncSession,
) -> None:
    active = await _workspace(session, status=WorkspaceStatus.running, title="active stop")
    terminal = await _workspace(
        session,
        status=WorkspaceStatus.completed,
        title="terminal stop",
    )
    service, stopper, _cleaner = _service(session)

    active_response = await service.stop_workspace(active.id, reason="halt")
    terminal_response = await service.stop_workspace(terminal.id, reason="already done")
    terminal_events = await _events(session, terminal.id)

    assert active_response.status == WorkspaceStatus.cancelled
    assert terminal_response.status == WorkspaceStatus.completed
    assert active.status == WorkspaceStatus.cancelled.value
    assert terminal.status == WorkspaceStatus.completed.value
    assert stopper.calls == [active.compose_project_name, terminal.compose_project_name]
    assert terminal_events[0].event_type == "workspace.stack_stopped"
    assert terminal_events[0].payload == {"reason": "already done"}


@pytest.mark.unit
async def test_stop_workspace_replays_existing_idempotent_operation(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.completed)
    service, stopper, _cleaner = _service(session)

    response = await service.stop_workspace(
        workspace.id,
        reason="first stop",
        idempotency_key="stop-replay",
    )
    replay = await service.stop_workspace(
        workspace.id,
        reason="first stop",
        idempotency_key="stop-replay",
    )
    operations = await _operations(session, workspace.id)

    assert response.operation_id == replay.operation_id
    assert replay.message == "workspace stack stopped"
    assert replay.status == WorkspaceStatus.completed
    assert stopper.calls == [workspace.compose_project_name]
    assert [operation.id for operation in operations] == [response.operation_id]


@pytest.mark.unit
async def test_control_prepare_operation_rejects_missing_conflicting_and_stale_requests(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.ready)
    service, stopper, _cleaner = _service(session)

    await service.cancel_workspace(
        workspace.id,
        reason="first",
        stop_stack=False,
        idempotency_key="control-conflict",
    )

    with pytest.raises(IdempotencyConflictError) as conflict:
        await service.cancel_workspace(
            workspace.id,
            reason="different",
            stop_stack=False,
            idempotency_key="control-conflict",
        )
    with pytest.raises(VersionConflictError) as version:
        await service.stop_workspace(
            workspace.id,
            reason="stale",
            expected_version=999,
        )
    with pytest.raises(WorkspaceNotFoundError) as missing:
        await service.cancel_workspace(
            "ws_missing",
            reason=None,
            stop_stack=False,
        )

    assert conflict.value.error_code == "IDEMPOTENCY_CONFLICT"
    assert version.value.detail == {"expected_version": 999, "actual_version": 2}
    assert missing.value.error_code == "NOT_FOUND"
    assert stopper.calls == []


@pytest.mark.unit
async def test_remonitor_rejects_wrong_state_and_missing_pr_before_creating_operation(
    session: AsyncSession,
) -> None:
    completed = await _workspace(session, status=WorkspaceStatus.completed)
    missing_pr = await _workspace(
        session,
        status=WorkspaceStatus.monitoring_pr,
        title="monitoring without pr",
    )
    service, _stopper, _cleaner = _service(session)

    with pytest.raises(WorkspaceRemonitorStateError) as wrong_state:
        await service.remonitor_workspace(completed.id, reason="retry monitor")
    with pytest.raises(WorkspaceRemonitorMissingPrUrlError) as missing_pr_error:
        await service.remonitor_workspace(missing_pr.id, reason="retry monitor")

    assert wrong_state.value.detail == {
        "status": WorkspaceStatus.completed.value,
        "eligible_statuses": [WorkspaceStatus.monitoring_pr.value],
    }
    assert missing_pr_error.value.detail == {"status": WorkspaceStatus.monitoring_pr.value}
    assert await _operations(session, completed.id) == []
    assert await _operations(session, missing_pr.id) == []


@pytest.mark.unit
async def test_remonitor_resets_claims_records_snapshot_and_replays(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.monitoring_pr)
    monitor_expiry = datetime(2026, 4, 27, 16, 0, tzinfo=UTC)
    execution_expiry = monitor_expiry + timedelta(minutes=10)
    workspace.pr_url = "https://github.com/example/control-lifecycle/pull/42"
    workspace.pr_number = 42
    workspace.monitor_claimed_by = "monitor-worker"
    workspace.monitor_claim_expires_at = monitor_expiry
    workspace.execution_claimed_by = "execution-worker"
    workspace.execution_claim_expires_at = execution_expiry
    await session.flush()
    service, _stopper, _cleaner = _service(session)

    response = await service.remonitor_workspace(
        workspace.id,
        reason="worker restarted",
        idempotency_key="remonitor-same-key",
        expected_version=workspace.version,
    )
    replay = await service.remonitor_workspace(
        workspace.id,
        reason="worker restarted",
        idempotency_key="remonitor-same-key",
        expected_version=workspace.version - 1,
    )
    operations = await _operations(session, workspace.id)
    events = await _events(session, workspace.id)
    expected_snapshot = {
        "monitor_claimed_by": "monitor-worker",
        "monitor_claim_expires_at": monitor_expiry.isoformat(),
        "execution_claimed_by": "execution-worker",
        "execution_claim_expires_at": execution_expiry.isoformat(),
    }

    assert response.operation_id == replay.operation_id
    assert response.status == WorkspaceStatus.monitoring_pr
    assert workspace.version == 2
    assert workspace.monitor_claimed_by is None
    assert workspace.monitor_claim_expires_at is None
    assert workspace.execution_claimed_by is None
    assert workspace.execution_claim_expires_at is None
    assert operations[0].type == OperationType.remonitor.value
    assert operations[0].status == "succeeded"
    assert operations[0].result == {
        "status": WorkspaceStatus.monitoring_pr.value,
        "claims_reset": expected_snapshot,
    }
    assert events[0].event_type == "workspace.remonitor_requested"
    assert events[0].payload == {
        "reason": "worker restarted",
        "operation_id": operations[0].id,
        "claims_reset": expected_snapshot,
    }


@pytest.mark.unit
async def test_destroy_rejects_active_workspace_without_force_before_cleanup(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.running)
    cleaner = RecordingCleaner()
    service, _stopper, _cleaner = _service(session, cleaner=cleaner)

    with pytest.raises(ActiveWorkspaceDestroyError) as exc_info:
        await service.destroy_workspace(
            workspace.id,
            force=False,
            remove_volumes=True,
            remove_worktree=True,
        )

    assert exc_info.value.error_code == "WORKSPACE_ACTIVE"
    assert cleaner.calls == []
    assert await _operations(session, workspace.id) == []


@pytest.mark.unit
async def test_force_destroy_active_workspace_runs_cleanup_and_marks_destroyed(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.ready)
    cleaner = RecordingCleaner()
    service, _stopper, _cleaner = _service(session, cleaner=cleaner)

    response = await service.destroy_workspace(
        workspace.id,
        force=True,
        remove_volumes=False,
        remove_worktree=True,
        idempotency_key="destroy-active",
    )
    operations = await _operations(session, workspace.id)
    events = await _events(session, workspace.id)

    assert response.status == WorkspaceStatus.destroyed
    assert response.message == "workspace destroyed"
    assert workspace.status == WorkspaceStatus.destroyed.value
    assert len(cleaner.calls) == 1
    assert cleaner.calls[0] == CleanupCall(
        workspace_id=workspace.id,
        repo_url=workspace.repo_url,
        compose_project_name=workspace.compose_project_name,
        compose_file_path=Path(workspace.compose_file_path),
        worktree_host_path=None,
        remove_volumes=False,
        remove_worktree=True,
    )
    assert operations[0].status == "succeeded"
    assert operations[0].result == {"status": WorkspaceStatus.destroyed.value}
    assert [event.new_state for event in events[:3]] == [
        WorkspaceStatus.destroyed.value,
        WorkspaceStatus.destroying.value,
        WorkspaceStatus.cancelled.value,
    ]


@pytest.mark.unit
async def test_destroy_already_destroyed_workspace_succeeds_without_cleanup_and_replays(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.destroyed)
    cleaner = RecordingCleaner()
    service, _stopper, _cleaner = _service(session, cleaner=cleaner)

    response = await service.destroy_workspace(
        workspace.id,
        force=False,
        remove_volumes=True,
        remove_worktree=False,
        idempotency_key="destroyed-replay",
    )
    replay = await service.destroy_workspace(
        workspace.id,
        force=False,
        remove_volumes=True,
        remove_worktree=False,
        idempotency_key="destroyed-replay",
    )
    operations = await _operations(session, workspace.id)

    assert response.operation_id == replay.operation_id
    assert response.message == "workspace already destroyed"
    assert replay.message == "workspace already destroyed"
    assert response.status == WorkspaceStatus.destroyed
    assert cleaner.calls == []
    assert [operation.type for operation in operations] == [OperationType.destroy.value]
    assert operations[0].result == {"status": WorkspaceStatus.destroyed.value}


@pytest.mark.unit
async def test_destroy_cleanup_failures_mark_operation_failed_and_workspace_failed(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.failed)
    cleaner = RecordingCleaner(failures=["compose down failed", "worktree removal failed"])
    service, _stopper, _cleaner = _service(session, cleaner=cleaner)

    response = await service.destroy_workspace(
        workspace.id,
        force=False,
        remove_volumes=True,
        remove_worktree=True,
    )
    operations = await _operations(session, workspace.id)

    assert response.status == WorkspaceStatus.failed
    assert response.message == "workspace cleanup failed"
    assert workspace.status == WorkspaceStatus.failed.value
    assert workspace.failure_reason == "cleanup_failure"
    assert workspace.failure_message == "compose down failed, worktree removal failed"
    assert len(cleaner.calls) == 1
    assert operations[0].status == "failed"
    assert operations[0].error_code == "CLEANUP_FAILED"
    assert operations[0].error_message == "compose down failed, worktree removal failed"
    assert operations[0].result == {"status": WorkspaceStatus.failed.value}


@pytest.mark.unit
async def test_destroy_replay_uses_in_progress_message_for_non_destroyed_workspace(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.failed)
    payload = {
        "force": False,
        "remove_volumes": True,
        "remove_worktree": True,
    }
    operation = await OperationRepository(session).create(
        workspace_id=workspace.id,
        operation_type=OperationType.destroy,
        status="running",
        payload=payload,
        idempotency_key="destroy-in-progress",
    )
    service, _stopper, cleaner = _service(session)

    response = await service.destroy_workspace(
        workspace.id,
        force=False,
        remove_volumes=True,
        remove_worktree=True,
        idempotency_key="destroy-in-progress",
    )

    assert response.operation_id == operation.id
    assert response.message == "workspace destroy requested"
    assert response.status == WorkspaceStatus.failed
    assert cleaner.calls == []


@pytest.mark.unit
async def test_destroy_destroying_workspace_runs_cleanup_without_retransition(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.destroying)
    cleaner = RecordingCleaner()
    service, _stopper, _cleaner = _service(session, cleaner=cleaner)

    response = await service.destroy_workspace(
        workspace.id,
        force=False,
        remove_volumes=True,
        remove_worktree=True,
    )
    events = await _events(session, workspace.id)

    assert response.status == WorkspaceStatus.destroyed
    assert workspace.status == WorkspaceStatus.destroyed.value
    assert len(cleaner.calls) == 1
    state_change_events = [
        event for event in events if event.event_type == "workspace.state_changed"
    ]
    assert [event.new_state for event in state_change_events] == [
        WorkspaceStatus.destroyed.value
    ]


@pytest.mark.unit
async def test_default_stack_helpers_handle_noop_and_construct_cleaner() -> None:
    await stop_project_containers(None)

    cleaner = default_cleaner()

    assert hasattr(cleaner, "cleanup")
    assert _json_datetime(None) is None
