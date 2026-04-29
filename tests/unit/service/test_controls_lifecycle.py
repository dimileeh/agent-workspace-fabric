"""Workspace control lifecycle behavior tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from awf.db.base import Base
from awf.db.enums import AgentRuntime, OperationStatus, OperationType, WorkspaceStatus
from awf.db.models import MergeCandidate, Operation, Workspace, WorkspaceEvent
from awf.db.repositories import (
    MergeCandidateRepository,
    OperationRepository,
    TaskAttemptRepository,
    TaskRepository,
    WorkspaceEventRepository,
    WorkspaceRepository,
)
from awf.db.session import make_engine, make_session_factory
from awf.service.controls import (
    _OPERATION_ERROR_MESSAGE_MAX_LENGTH,
    ActiveWorkspaceDestroyError,
    IdempotencyConflictError,
    VersionConflictError,
    WorkspaceControlService,
    WorkspaceNotFoundError,
    WorkspaceRebaseActiveConflictError,
    WorkspaceRebaseMissingCandidateError,
    WorkspaceRebaseMissingPrUrlError,
    WorkspaceRebaseStateError,
    WorkspaceRefreshStateError,
    WorkspaceRemonitorMissingPrUrlError,
    WorkspaceRemonitorStateError,
    WorkspaceStackStopError,
    WorkspaceValidateMissingPrUrlError,
    WorkspaceValidateStateError,
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
class FailingStopper:
    calls: list[str | None] = field(default_factory=list)

    async def __call__(self, compose_project_name: str | None) -> None:
        self.calls.append(compose_project_name)
        raise WorkspaceStackStopError(
            operation="stop",
            returncode=17,
            stdout="",
            stderr="compose stop denied",
        )


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


@dataclass
class StaleCallbackCleaner(RecordingCleaner):
    session: AsyncSession | None = None
    final_status: WorkspaceStatus = WorkspaceStatus.cancelled

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
        result = await super().cleanup(
            workspace_id=workspace_id,
            repo_url=repo_url,
            compose_project_name=compose_project_name,
            compose_file_path=compose_file_path,
            worktree_host_path=worktree_host_path,
            remove_volumes=remove_volumes,
            remove_worktree=remove_worktree,
        )
        assert self.session is not None
        await self.session.execute(
            sa_update(Workspace)
            .where(Workspace.id == workspace_id)
            .values(
                status=self.final_status.value,
                failure_reason=(
                    "operator_failure"
                    if self.final_status == WorkspaceStatus.failed
                    else None
                ),
                failure_message=(
                    "operator moved workspace"
                    if self.final_status == WorkspaceStatus.failed
                    else None
                ),
            )
            .execution_options(synchronize_session=False)
        )
        await self.session.flush()
        return result


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


async def _workspace_with_candidate(
    session: AsyncSession,
    *,
    status: WorkspaceStatus = WorkspaceStatus.monitoring_pr,
    title: str = "rebase lifecycle",
) -> tuple[Workspace, MergeCandidate]:
    workspace = await _workspace(session, status=status, title=title)
    workspace.branch_name = f"awf/{workspace.id}"
    workspace.remote_push_branch = workspace.branch_name
    workspace.base_commit = "a" * 40
    workspace.monitor_last_commit_sha = "h" * 40
    workspace.pr_url = "https://github.com/example/control-lifecycle/pull/50"
    workspace.pr_number = 50
    task = await TaskRepository(session).create_or_get(
        repo_url=workspace.repo_url,
        base_branch=workspace.branch_base,
        title=workspace.task_title,
        prompt=workspace.task_prompt,
        external_id=workspace.task_external_id,
        idempotency_key=None,
        task_class=workspace.task_class,
        owned_paths=list(workspace.owned_paths),
    )
    attempt = await TaskAttemptRepository(session).create_for_workspace(
        task=task,
        workspace=workspace,
    )
    candidate = await MergeCandidateRepository(session).create_or_update_open_for_attempt(
        task=task,
        attempt=attempt,
        workspace=workspace,
        head_sha=workspace.monitor_last_commit_sha,
        base_sha=workspace.base_commit,
    )
    await session.flush()
    return workspace, candidate


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
    audit_events = await WorkspaceEventRepository(session).list(
        workspace_id=workspace.id,
        event_type="workspace.audit.control_operation",
        limit=10,
    )

    assert response.operation_id == replay.operation_id
    assert response.message == "workspace cancellation requested"
    assert response.status == WorkspaceStatus.cancelled
    assert workspace.status == WorkspaceStatus.cancelled.value
    assert stopper.calls == [workspace.compose_project_name]
    assert [operation.type for operation in operations] == [OperationType.cancel.value]
    assert operations[0].status == "succeeded"
    assert operations[0].payload == {
        "owner": "operator_api",
        "source": "operator_api",
        "reason": "operator requested",
        "reason_code": "OPERATOR_CANCEL",
        "requested_action": "cancel",
        "stop_stack": True,
        "expected_version": 1,
    }
    assert operations[0].result == {"status": WorkspaceStatus.cancelled.value}
    assert len(audit_events) == 1
    assert audit_events[0].payload == {
        "schema": "control_audit.v1",
        "actor": "operator_api",
        "source": "operator_api",
        "action": "cancel",
        "outcome": "succeeded",
        "reason_code": "OPERATOR_CANCEL",
        "operation_id": operations[0].id,
        "operation_type": "cancel",
        "stop_stack": True,
        "expected_version": 1,
    }


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
    cancel_event = next(
        event for event in events if event.event_type == "workspace.cancel_requested"
    )
    assert cancel_event.reason_code == "OPERATOR_CANCEL"
    assert cancel_event.payload == {"reason": None, "stop_stack": False}


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
    terminal_audit = await WorkspaceEventRepository(session).list(
        workspace_id=terminal.id,
        event_type="workspace.audit.control_operation",
        limit=10,
    )

    assert active_response.status == WorkspaceStatus.cancelled
    assert terminal_response.status == WorkspaceStatus.completed
    assert active.status == WorkspaceStatus.cancelled.value
    assert terminal.status == WorkspaceStatus.completed.value
    assert stopper.calls == [active.compose_project_name, terminal.compose_project_name]
    stack_event = next(
        event for event in terminal_events if event.event_type == "workspace.stack_stopped"
    )
    assert stack_event.payload == {"reason": "already done"}
    assert len(terminal_audit) == 1
    assert terminal_audit[0].payload is not None
    assert terminal_audit[0].payload["actor"] == "operator_api"
    assert terminal_audit[0].payload["action"] == "stop"
    assert terminal_audit[0].payload["outcome"] == "succeeded"
    assert terminal_audit[0].payload["reason_code"] == "OPERATOR_STOP"


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
async def test_terminal_cancel_stop_events_include_expected_version(
    session: AsyncSession,
) -> None:
    cancel = await _workspace(
        session,
        status=WorkspaceStatus.completed,
        title="terminal cancel with expected version",
    )
    stop = await _workspace(
        session,
        status=WorkspaceStatus.completed,
        title="terminal stop with expected version",
    )
    service, _stopper, _cleaner = _service(session)

    await service.cancel_workspace(
        cancel.id,
        reason="cancel audit",
        stop_stack=False,
        expected_version=cancel.version,
    )
    await service.stop_workspace(
        stop.id,
        reason="stop audit",
        expected_version=stop.version,
    )
    cancel_event = next(
        event
        for event in await _events(session, cancel.id)
        if event.event_type == "workspace.cancel_requested"
    )
    stop_event = next(
        event
        for event in await _events(session, stop.id)
        if event.event_type == "workspace.stack_stopped"
    )

    assert cancel_event.payload == {
        "reason": "cancel audit",
        "stop_stack": False,
        "expected_version": 1,
    }
    assert stop_event.payload == {
        "reason": "stop audit",
        "expected_version": 1,
    }


@pytest.mark.unit
async def test_idempotent_replay_returns_original_operation_audit_unchanged(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.completed)
    service, _stopper, _cleaner = _service(session)

    first = await service.stop_workspace(
        workspace.id,
        reason="preserve audit",
        idempotency_key="stop-audit-replay",
    )
    operation = (await _operations(session, workspace.id))[0]
    original_payload = dict(operation.payload or {})
    original_result = dict(operation.result or {})
    original_started_at = operation.started_at
    original_finished_at = operation.finished_at
    replay = await service.stop_workspace(
        workspace.id,
        reason="preserve audit",
        idempotency_key="stop-audit-replay",
    )
    replayed = (await _operations(session, workspace.id))[0]
    audit_events = await WorkspaceEventRepository(session).list(
        workspace_id=workspace.id,
        event_type="workspace.audit.control_operation",
        limit=10,
    )

    assert replay.operation_id == first.operation_id
    assert replayed.id == operation.id
    assert replayed.payload == original_payload == {
        "owner": "operator_api",
        "source": "operator_api",
        "reason": "preserve audit",
        "reason_code": "OPERATOR_STOP",
        "requested_action": "stop",
    }
    assert replayed.result == original_result
    assert replayed.started_at == original_started_at
    assert replayed.finished_at == original_finished_at
    assert replayed.idempotency_key == "stop-audit-replay"
    assert len(audit_events) == 1
    assert audit_events[0].payload is not None
    assert audit_events[0].payload["operation_id"] == first.operation_id


@pytest.mark.unit
async def test_stop_stack_failure_finishes_operation_failed_with_audit(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.ready)
    stopper = FailingStopper()
    service, _stopper, _cleaner = _service(session, stopper=stopper)

    with pytest.raises(WorkspaceStackStopError) as exc_info:
        await service.stop_workspace(
            workspace.id,
            reason="operator stop",
            idempotency_key="stop-fails",
        )
    operations = await _operations(session, workspace.id)
    audit_events = await WorkspaceEventRepository(session).list(
        workspace_id=workspace.id,
        event_type="workspace.audit.control_operation",
        limit=10,
    )

    assert exc_info.value.error_code == "STACK_STOP_FAILED"
    assert stopper.calls == [workspace.compose_project_name]
    assert workspace.status == WorkspaceStatus.ready.value
    assert len(operations) == 1
    assert operations[0].status == OperationStatus.failed.value
    assert operations[0].error_code == "STACK_STOP_FAILED"
    assert "compose stop denied" in (operations[0].error_message or "")
    assert operations[0].payload == {
        "owner": "operator_api",
        "source": "operator_api",
        "reason": "operator stop",
        "reason_code": "OPERATOR_STOP",
        "requested_action": "stop",
    }
    assert operations[0].started_at is not None
    assert operations[0].finished_at is not None
    assert len(audit_events) == 1
    assert audit_events[0].reason_code == "STACK_STOP_FAILED"
    assert audit_events[0].payload is not None
    assert audit_events[0].payload["action"] == "stop"
    assert audit_events[0].payload["outcome"] == "failed"
    assert audit_events[0].payload["operation_id"] == operations[0].id
    assert audit_events[0].payload["evidence"]["error_message"] == (
        "docker stop failed (exit=17): compose stop denied"
    )


@pytest.mark.unit
async def test_cancel_stack_failure_finishes_operation_failed_with_audit(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.ready)
    stopper = FailingStopper()
    service, _stopper, _cleaner = _service(session, stopper=stopper)

    with pytest.raises(WorkspaceStackStopError):
        await service.cancel_workspace(
            workspace.id,
            reason="operator cancel",
            stop_stack=True,
            idempotency_key="cancel-fails",
        )
    operations = await _operations(session, workspace.id)
    audit_events = await WorkspaceEventRepository(session).list(
        workspace_id=workspace.id,
        event_type="workspace.audit.control_operation",
        limit=10,
    )

    assert stopper.calls == [workspace.compose_project_name]
    assert workspace.status == WorkspaceStatus.ready.value
    assert len(operations) == 1
    assert operations[0].status == OperationStatus.failed.value
    assert operations[0].error_code == "STACK_STOP_FAILED"
    assert "compose stop denied" in (operations[0].error_message or "")
    assert operations[0].payload == {
        "owner": "operator_api",
        "source": "operator_api",
        "reason": "operator cancel",
        "reason_code": "OPERATOR_CANCEL",
        "requested_action": "cancel",
        "stop_stack": True,
    }
    assert len(audit_events) == 1
    assert audit_events[0].payload is not None
    assert audit_events[0].payload["action"] == "cancel"
    assert audit_events[0].payload["outcome"] == "failed"
    assert audit_events[0].payload["reason_code"] == "STACK_STOP_FAILED"


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
async def test_control_require_workspace_reports_missing_workspace(
    session: AsyncSession,
) -> None:
    service, _stopper, _cleaner = _service(session)

    with pytest.raises(WorkspaceNotFoundError) as missing:
        await service._require_workspace(WorkspaceRepository(session), "ws_missing")

    assert missing.value.error_code == "NOT_FOUND"


@pytest.mark.unit
async def test_remonitor_rejects_wrong_state_and_missing_pr_before_creating_operation(
    session: AsyncSession,
) -> None:
    requested = await _workspace(session, status=WorkspaceStatus.requested)
    missing_pr = await _workspace(
        session,
        status=WorkspaceStatus.monitoring_pr,
        title="monitoring without pr",
    )
    service, _stopper, _cleaner = _service(session)

    with pytest.raises(WorkspaceRemonitorStateError) as wrong_state:
        await service.remonitor_workspace(requested.id, reason="retry monitor")
    with pytest.raises(WorkspaceRemonitorMissingPrUrlError) as missing_pr_error:
        await service.remonitor_workspace(missing_pr.id, reason="retry monitor")

    assert wrong_state.value.detail == {
        "status": WorkspaceStatus.requested.value,
        "eligible_statuses": [
            WorkspaceStatus.monitoring_pr.value,
            WorkspaceStatus.failed.value,
        ],
    }
    assert missing_pr_error.value.detail == {"status": WorkspaceStatus.monitoring_pr.value}
    assert await _operations(session, requested.id) == []
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
    workspace.base_commit = "b" * 40
    workspace.monitor_last_commit_sha = "h" * 40
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
    audit_events = await WorkspaceEventRepository(session).list(
        workspace_id=workspace.id,
        event_type="workspace.audit.control_operation",
        limit=10,
    )
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
        "pr_number": 42,
        "pr_url": "https://github.com/example/control-lifecycle/pull/42",
        "source_head_sha": "h" * 40,
        "source_base_sha": "b" * 40,
    }
    assert events[0].event_type == "workspace.remonitor_requested"
    assert events[0].payload == {
        "reason": "worker restarted",
        "operation_id": operations[0].id,
        "claims_reset": expected_snapshot,
        "expected_version": 1,
    }


@pytest.mark.unit
async def test_cancel_stop_destroy_remonitor_payloads_include_operator_audit(
    session: AsyncSession,
) -> None:
    cancel = await _workspace(session, status=WorkspaceStatus.completed, title="cancel audit")
    stop = await _workspace(session, status=WorkspaceStatus.completed, title="stop audit")
    destroy = await _workspace(session, status=WorkspaceStatus.destroyed, title="destroy audit")
    remonitor = await _workspace(session, status=WorkspaceStatus.monitoring_pr, title="remonitor audit")
    remonitor.pr_url = "https://github.com/example/control-lifecycle/pull/45"
    remonitor.pr_number = 45
    remonitor.base_commit = "b" * 40
    remonitor.monitor_last_commit_sha = "h" * 40
    await session.flush()
    service, _stopper, _cleaner = _service(session)

    await service.cancel_workspace(cancel.id, reason="cancel it", stop_stack=False)
    await service.stop_workspace(stop.id, reason="stop it")
    await service.destroy_workspace(
        destroy.id,
        force=False,
        remove_volumes=True,
        remove_worktree=False,
    )
    await service.remonitor_workspace(remonitor.id, reason="rerun monitor")

    operations_by_type = {
        operation.type: operation for operation in await OperationRepository(session).list_all(limit=20)
    }

    assert operations_by_type[OperationType.cancel.value].payload == {
        "owner": "operator_api",
        "source": "operator_api",
        "reason": "cancel it",
        "reason_code": "OPERATOR_CANCEL",
        "requested_action": "cancel",
        "stop_stack": False,
    }
    assert operations_by_type[OperationType.stop.value].payload == {
        "owner": "operator_api",
        "source": "operator_api",
        "reason": "stop it",
        "reason_code": "OPERATOR_STOP",
        "requested_action": "stop",
    }
    assert operations_by_type[OperationType.destroy.value].payload == {
        "owner": "operator_api",
        "source": "operator_api",
        "reason": None,
        "reason_code": "OPERATOR_DESTROY",
        "requested_action": "destroy",
        "force": False,
        "remove_volumes": True,
        "remove_worktree": False,
    }
    assert operations_by_type[OperationType.remonitor.value].payload == {
        "owner": "operator_api",
        "source": "operator_api",
        "reason": "rerun monitor",
        "reason_code": "OPERATOR_REMONITOR",
        "requested_action": "remonitor",
        "pr_number": 45,
        "pr_url": "https://github.com/example/control-lifecycle/pull/45",
        "source_head_sha": "h" * 40,
        "source_base_sha": "b" * 40,
    }


@pytest.mark.unit
async def test_refresh_active_workspace_creates_pending_operation_and_coalesces_by_reason(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.ready)
    service, _stopper, _cleaner = _service(session)

    operation = await service.request_refresh_workspace(
        workspace.id,
        reason="stale merge queue",
        idempotency_key="refresh-first",
        expected_version=workspace.version,
    )
    replay = await service.request_refresh_workspace(
        workspace.id,
        reason="stale merge queue",
        idempotency_key="refresh-fresh-key",
    )
    operations = await _operations(session, workspace.id)
    events = await _events(session, workspace.id)
    refresh_event = next(
        event for event in events if event.event_type == "workspace.refresh_requested"
    )

    assert replay.id == operation.id
    assert workspace.status == WorkspaceStatus.ready.value
    assert [row.id for row in operations] == [operation.id]
    assert operation.type == OperationType.refresh.value
    assert operation.status == OperationStatus.pending.value
    assert operation.idempotency_key == "refresh-first"
    assert operation.payload == {
        "owner": "operator_api",
        "source": "operator_api",
        "reason": "stale merge queue",
        "reason_code": "OPERATOR_REFRESH",
        "requested_action": "refresh",
        "expected_version": 1,
    }
    assert refresh_event.reason_code == "OPERATOR_REFRESH"
    assert refresh_event.payload == {
        "source": "operator_api",
        "reason": "stale merge queue",
        "operation_id": operation.id,
        "expected_version": 1,
    }


@pytest.mark.unit
async def test_refresh_fresh_key_with_stale_if_match_does_not_coalesce_active_operation(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.ready)
    service, _stopper, _cleaner = _service(session)

    operation = await service.request_refresh_workspace(
        workspace.id,
        reason="stale merge queue",
        idempotency_key="refresh-original",
        expected_version=workspace.version,
    )
    workspace.version += 1
    await session.flush()

    replay = await service.request_refresh_workspace(
        workspace.id,
        reason="stale merge queue",
        idempotency_key="refresh-original",
        expected_version=1,
    )
    with pytest.raises(VersionConflictError) as exc_info:
        await service.request_refresh_workspace(
            workspace.id,
            reason="stale merge queue",
            idempotency_key="refresh-fresh-stale-version",
            expected_version=1,
        )

    assert replay.id == operation.id
    assert exc_info.value.detail == {"expected_version": 1, "actual_version": 2}
    assert [row.id for row in await _operations(session, workspace.id)] == [operation.id]


@pytest.mark.unit
async def test_remonitor_failed_workspace_with_pr_reenters_monitoring(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.failed)
    workspace.pr_url = "https://github.com/example/control-lifecycle/pull/43"
    workspace.pr_number = 43
    workspace.failure_reason = "infrastructure_failure"
    workspace.failure_message = "old worker died during rebase recovery"
    workspace.monitor_iter_count = 8
    await session.flush()
    service, _stopper, _cleaner = _service(session)

    response = await service.remonitor_workspace(
        workspace.id,
        reason="reattach failed PR",
        idempotency_key="remonitor-failed-pr",
        expected_version=workspace.version,
    )
    events = await _events(session, workspace.id)

    assert response.status == WorkspaceStatus.monitoring_pr
    assert workspace.status == WorkspaceStatus.monitoring_pr.value
    assert workspace.failure_reason is None
    assert workspace.failure_message is None
    assert workspace.monitor_iter_count == 0
    assert events[0].event_type == "workspace.remonitor_requested"
    assert events[0].old_state == WorkspaceStatus.failed.value
    assert events[0].new_state == WorkspaceStatus.monitoring_pr.value
    assert events[0].payload["state_reset"] == {
        "from": WorkspaceStatus.failed.value,
        "to": WorkspaceStatus.monitoring_pr.value,
        "monitor_iter_count_reset_from": 8,
        "candidate_reopened": False,
    }


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
        if event.event_type == "workspace.state_changed"
        and event.reason_code == "OPERATOR_REBASE"
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


@pytest.mark.unit
async def test_rebase_same_key_with_different_expected_version_conflicts_without_duplicate_rows(
    session: AsyncSession,
) -> None:
    workspace, _candidate = await _workspace_with_candidate(session)
    service, _stopper, _cleaner = _service(session)
    original_version = workspace.version

    operation = await service.request_rebase_workspace(
        workspace.id,
        reason="base branch advanced",
        idempotency_key="rebase-if-match-conflict",
        expected_version=original_version,
    )
    before_operation_ids = [row.id for row in await _operations(session, workspace.id)]
    before_event_ids = [row.id for row in await _events(session, workspace.id)]

    with pytest.raises(IdempotencyConflictError):
        await service.request_rebase_workspace(
            workspace.id,
            reason="base branch advanced",
            idempotency_key="rebase-if-match-conflict",
            expected_version=original_version + 1,
        )

    assert before_operation_ids == [operation.id]
    assert [
        row.id for row in await _operations(session, workspace.id)
    ] == before_operation_ids
    assert [row.id for row in await _events(session, workspace.id)] == before_event_ids


@pytest.mark.unit
async def test_rebase_active_incompatible_payload_conflicts_without_duplicate_operation(
    session: AsyncSession,
) -> None:
    workspace, _candidate = await _workspace_with_candidate(session)
    service, _stopper, _cleaner = _service(session)

    operation = await service.request_rebase_workspace(
        workspace.id,
        reason="base branch advanced",
        idempotency_key="rebase-original",
    )

    with pytest.raises(WorkspaceRebaseActiveConflictError) as exc_info:
        await service.request_rebase_workspace(
            workspace.id,
            reason="different base branch reason",
            idempotency_key="rebase-conflicting",
        )

    assert exc_info.value.error_code == "WORKSPACE_REBASE_CONFLICT"
    assert exc_info.value.detail == {
        "operation_id": operation.id,
        "operation_type": OperationType.rebase.value,
        "operation_status": OperationStatus.pending.value,
    }
    assert [row.id for row in await _operations(session, workspace.id)] == [operation.id]


@pytest.mark.unit
async def test_rebase_rejects_missing_pr_candidate_state_and_destructive_conflicts(
    session: AsyncSession,
) -> None:
    wrong_state, _wrong_candidate = await _workspace_with_candidate(
        session,
        status=WorkspaceStatus.completed,
        title="rebase completed",
    )
    missing_pr = await _workspace(
        session,
        status=WorkspaceStatus.monitoring_pr,
        title="rebase missing pr",
    )
    missing_candidate = await _workspace(
        session,
        status=WorkspaceStatus.monitoring_pr,
        title="rebase missing candidate",
    )
    missing_candidate.pr_url = "https://github.com/example/control-lifecycle/pull/51"
    missing_candidate.pr_number = 51
    destructive, _destructive_candidate = await _workspace_with_candidate(
        session,
        title="rebase destructive conflict",
    )
    conflict = await OperationRepository(session).create(
        workspace_id=destructive.id,
        operation_type=OperationType.destroy,
        status=OperationStatus.running,
        payload={"source": "operator_api"},
    )
    service, _stopper, _cleaner = _service(session)

    with pytest.raises(WorkspaceRebaseStateError) as wrong_state_error:
        await service.request_rebase_workspace(wrong_state.id, reason="rebase")
    with pytest.raises(WorkspaceRebaseMissingPrUrlError) as missing_pr_error:
        await service.request_rebase_workspace(missing_pr.id, reason="rebase")
    with pytest.raises(WorkspaceRebaseMissingCandidateError) as missing_candidate_error:
        await service.request_rebase_workspace(missing_candidate.id, reason="rebase")
    with pytest.raises(WorkspaceRebaseActiveConflictError) as conflict_error:
        await service.request_rebase_workspace(destructive.id, reason="rebase")

    assert wrong_state_error.value.detail == {
        "status": WorkspaceStatus.completed.value,
        "eligible_statuses": [WorkspaceStatus.monitoring_pr.value],
    }
    assert missing_pr_error.value.detail == {"status": WorkspaceStatus.monitoring_pr.value}
    assert missing_candidate_error.value.detail == {
        "workspace_id": missing_candidate.id,
        "pr_url": "https://github.com/example/control-lifecycle/pull/51",
    }
    assert conflict_error.value.detail == {
        "operation_id": conflict.id,
        "operation_type": OperationType.destroy.value,
        "operation_status": OperationStatus.running.value,
    }
    assert await _operations(session, wrong_state.id) == []
    assert await _operations(session, missing_pr.id) == []
    assert await _operations(session, missing_candidate.id) == []
    assert [row.id for row in await _operations(session, destructive.id)] == [conflict.id]


@pytest.mark.unit
async def test_validate_rejects_missing_pr_url_before_creating_operation(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.monitoring_pr)
    service, _stopper, _cleaner = _service(session)

    with pytest.raises(WorkspaceValidateMissingPrUrlError) as exc_info:
        await service.request_validate_workspace(
            workspace.id,
            reason="rerun without pr",
        )

    assert exc_info.value.error_code == "WORKSPACE_PR_URL_REQUIRED"
    assert exc_info.value.detail == {"status": WorkspaceStatus.monitoring_pr.value}
    assert await _operations(session, workspace.id) == []


@pytest.mark.unit
async def test_validate_replay_rejects_workspace_that_left_replay_states(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.completed)
    payload = {
        "owner": "operator_api",
        "source": "operator_api",
        "reason": "rerun after completion",
        "reason_code": "OPERATOR_VALIDATE",
        "requested_action": OperationType.validate.value,
        "recovery_mode": "validate_only",
    }
    operation = await OperationRepository(session).create(
        workspace_id=workspace.id,
        operation_type=OperationType.validate,
        status=OperationStatus.pending,
        payload=payload,
    )
    service, _stopper, _cleaner = _service(session)

    with pytest.raises(WorkspaceValidateStateError) as exc_info:
        await service.request_validate_workspace(
            workspace.id,
            reason="rerun after completion",
        )

    assert exc_info.value.detail == {
        "status": WorkspaceStatus.completed.value,
        "eligible_statuses": [WorkspaceStatus.monitoring_pr.value],
    }
    assert [row.id for row in await _operations(session, workspace.id)] == [operation.id]


@pytest.mark.unit
async def test_refresh_rejects_destroying_or_destroyed_without_creating_operation(
    session: AsyncSession,
) -> None:
    destroying = await _workspace(
        session,
        status=WorkspaceStatus.destroying,
        title="refresh destroying",
    )
    destroyed = await _workspace(
        session,
        status=WorkspaceStatus.destroyed,
        title="refresh destroyed",
    )
    service, _stopper, _cleaner = _service(session)

    with pytest.raises(WorkspaceRefreshStateError) as destroying_error:
        await service.request_refresh_workspace(destroying.id, reason="refresh")
    with pytest.raises(WorkspaceRefreshStateError) as destroyed_error:
        await service.request_refresh_workspace(destroyed.id, reason="refresh")

    assert destroying_error.value.error_code == "WORKSPACE_STATE_NOT_REFRESHABLE"
    assert destroying_error.value.detail == {"status": WorkspaceStatus.destroying.value}
    assert destroyed_error.value.detail == {"status": WorkspaceStatus.destroyed.value}
    assert await _operations(session, destroying.id) == []
    assert await _operations(session, destroyed.id) == []


@pytest.mark.unit
async def test_validate_rejects_ineligible_state_before_creating_operation(
    session: AsyncSession,
) -> None:
    completed = await _workspace(session, status=WorkspaceStatus.completed)
    destroying = await _workspace(
        session,
        status=WorkspaceStatus.destroying,
        title="validate destroying",
    )
    service, _stopper, _cleaner = _service(session)

    with pytest.raises(WorkspaceValidateStateError) as completed_error:
        await service.request_validate_workspace(
            completed.id,
            reason="rerun after merge",
        )
    with pytest.raises(WorkspaceValidateStateError) as destroying_error:
        await service.request_validate_workspace(
            destroying.id,
            reason="rerun while deleting",
        )

    assert completed_error.value.error_code == "WORKSPACE_STATE_NOT_VALIDATABLE"
    assert completed_error.value.detail == {
        "status": WorkspaceStatus.completed.value,
        "eligible_statuses": [WorkspaceStatus.monitoring_pr.value],
    }
    assert destroying_error.value.detail == {
        "status": WorkspaceStatus.destroying.value,
        "eligible_statuses": [WorkspaceStatus.monitoring_pr.value],
    }
    assert await _operations(session, completed.id) == []
    assert await _operations(session, destroying.id) == []


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
async def test_destroy_already_cancelled_workspace_runs_cleanup_and_records_destroy_contract(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.cancelled)
    cleaner = RecordingCleaner()
    service, _stopper, _cleaner = _service(session, cleaner=cleaner)

    response = await service.destroy_workspace(
        workspace.id,
        force=False,
        remove_volumes=True,
        remove_worktree=False,
        idempotency_key="destroy-cancelled",
    )
    operations = await _operations(session, workspace.id)
    events = await _events(session, workspace.id)
    state_events = [
        event for event in events if event.event_type == "workspace.state_changed"
    ]

    assert response.operation_id == operations[0].id
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
        remove_volumes=True,
        remove_worktree=False,
    )
    assert operations[0].type == OperationType.destroy.value
    assert operations[0].status == OperationStatus.succeeded.value
    assert operations[0].payload == {
        "owner": "operator_api",
        "source": "operator_api",
        "reason": None,
        "reason_code": "OPERATOR_DESTROY",
        "requested_action": "destroy",
        "force": False,
        "remove_volumes": True,
        "remove_worktree": False,
    }
    assert operations[0].result == {
        "status": WorkspaceStatus.destroyed.value,
        "cleanup": {
            "status": "succeeded",
            "reason_code": "CLEANUP_SUCCEEDED",
            "steps": [],
            "failed_steps": [],
            "completed_steps": [],
        },
    }
    assert [event.new_state for event in state_events] == [
        WorkspaceStatus.destroyed.value,
        WorkspaceStatus.destroying.value,
    ]
    assert state_events[1].old_state == WorkspaceStatus.cancelled.value
    assert state_events[1].payload == {
        "force": False,
        "remove_volumes": True,
        "remove_worktree": False,
    }
    assert state_events[0].old_state == WorkspaceStatus.destroying.value
    assert state_events[0].payload is not None
    assert state_events[0].payload["cleanup"] == operations[0].result["cleanup"]
    assert not any(
        event.event_type == "workspace.stale_callback_ignored" for event in events
    )


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
    audit_events = await WorkspaceEventRepository(session).list(
        workspace_id=workspace.id,
        event_type="workspace.audit.control_operation",
        limit=10,
    )

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
    assert operations[0].result == {
        "status": WorkspaceStatus.destroyed.value,
        "cleanup": {
            "status": "succeeded",
            "reason_code": "CLEANUP_SUCCEEDED",
            "steps": [],
            "failed_steps": [],
            "completed_steps": [],
        },
    }
    state_events = [
        event for event in events if event.event_type == "workspace.state_changed"
    ]
    assert [event.new_state for event in state_events[:3]] == [
        WorkspaceStatus.destroyed.value,
        WorkspaceStatus.destroying.value,
        WorkspaceStatus.cancelled.value,
    ]
    assert state_events[0].payload is not None
    assert state_events[0].payload["cleanup"] == operations[0].result["cleanup"]
    assert len(audit_events) == 1
    assert audit_events[0].payload == {
        "schema": "control_audit.v1",
        "actor": "operator_api",
        "source": "operator_api",
        "action": "destroy",
        "outcome": "succeeded",
        "reason_code": "OPERATOR_DESTROY",
        "operation_id": operations[0].id,
        "operation_type": "destroy",
        "force": True,
        "remove_volumes": False,
        "remove_worktree": True,
        "evidence": {"cleanup": operations[0].result["cleanup"]},
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    "final_status",
    [
        WorkspaceStatus.cancelled,
        WorkspaceStatus.destroyed,
        WorkspaceStatus.completed,
        WorkspaceStatus.failed,
    ],
)
async def test_destroy_cleanup_callback_does_not_override_terminal_state(
    session: AsyncSession,
    final_status: WorkspaceStatus,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.completed)
    cleaner = StaleCallbackCleaner(session=session, final_status=final_status)
    service, _stopper, _cleaner = _service(session, cleaner=cleaner)

    response = await service.destroy_workspace(
        workspace.id,
        force=False,
        remove_volumes=True,
        remove_worktree=True,
    )
    operations = await _operations(session, workspace.id)
    events = await _events(session, workspace.id)

    assert response.status == final_status
    assert response.message == "workspace destroy callback ignored"
    assert workspace.status == final_status.value
    if final_status == WorkspaceStatus.failed:
        assert workspace.failure_reason == "operator_failure"
        assert workspace.failure_message == "operator moved workspace"
    assert operations[0].status == OperationStatus.cancelled.value
    assert operations[0].result == {
        "status": final_status.value,
        "cleanup": {
            "status": "succeeded",
            "reason_code": "CLEANUP_SUCCEEDED",
            "steps": [],
            "failed_steps": [],
            "completed_steps": [],
        },
        "ignored_callback": {
            "reason_code": "STALE_CALLBACK_IGNORED",
            "callback_source": "service.controls",
            "callback_action": "destroy_cleanup",
            "expected_status": WorkspaceStatus.destroying.value,
            "actual_status": final_status.value,
            "requested_status": WorkspaceStatus.destroyed.value,
            "operation_id": operations[0].id,
        },
    }
    ignored_events = [
        event for event in events if event.event_type == "workspace.stale_callback_ignored"
    ]
    assert ignored_events[-1].payload == operations[0].result["ignored_callback"]


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
    audit_events = await WorkspaceEventRepository(session).list(
        workspace_id=workspace.id,
        event_type="workspace.audit.control_operation",
        limit=10,
    )

    assert response.operation_id == replay.operation_id
    assert response.message == "workspace already destroyed"
    assert replay.message == "workspace already destroyed"
    assert response.status == WorkspaceStatus.destroyed
    assert cleaner.calls == []
    assert [operation.type for operation in operations] == [OperationType.destroy.value]
    assert operations[0].result == {
        "status": WorkspaceStatus.destroyed.value,
        "cleanup": {
            "status": "skipped",
            "reason_code": "WORKSPACE_ALREADY_DESTROYED",
            "steps": [],
            "failed_steps": [],
            "completed_steps": [],
        },
    }
    assert len(audit_events) == 1
    assert audit_events[0].payload is not None
    assert audit_events[0].payload["action"] == "destroy"
    assert audit_events[0].payload["outcome"] == "skipped"
    assert audit_events[0].payload["operation_id"] == operations[0].id
    assert audit_events[0].payload["evidence"]["cleanup"] == operations[0].result["cleanup"]


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
    audit_events = await WorkspaceEventRepository(session).list(
        workspace_id=workspace.id,
        event_type="workspace.audit.control_operation",
        limit=10,
    )

    assert response.status == WorkspaceStatus.failed
    assert response.message == "workspace cleanup failed"
    assert workspace.status == WorkspaceStatus.failed.value
    assert workspace.failure_reason == "cleanup_failure"
    assert workspace.failure_message == "compose down failed, worktree removal failed"
    assert len(cleaner.calls) == 1
    assert operations[0].status == "failed"
    assert operations[0].error_code == "CLEANUP_FAILED"
    assert operations[0].error_message == "compose down failed, worktree removal failed"
    assert operations[0].result == {
        "status": WorkspaceStatus.failed.value,
        "cleanup": {
            "status": "partial",
            "reason_code": "CLEANUP_PARTIAL",
            "steps": [
                {
                    "name": "compose down failed",
                    "status": "failed",
                    "reason_code": "CLEANUP_STEP_FAILED",
                    "error": "compose down failed",
                },
                {
                    "name": "worktree removal failed",
                    "status": "failed",
                    "reason_code": "CLEANUP_STEP_FAILED",
                    "error": "worktree removal failed",
                },
            ],
            "failed_steps": [
                {
                    "name": "compose down failed",
                    "status": "failed",
                    "reason_code": "CLEANUP_STEP_FAILED",
                    "error": "compose down failed",
                },
                {
                    "name": "worktree removal failed",
                    "status": "failed",
                    "reason_code": "CLEANUP_STEP_FAILED",
                    "error": "worktree removal failed",
                },
            ],
            "completed_steps": [],
        },
    }
    assert len(audit_events) == 1
    assert audit_events[0].reason_code == "CLEANUP_FAILED"
    assert audit_events[0].payload is not None
    assert audit_events[0].payload["action"] == "destroy"
    assert audit_events[0].payload["outcome"] == "failed"
    assert audit_events[0].payload["operation_id"] == operations[0].id
    assert audit_events[0].payload["evidence"]["cleanup"] == operations[0].result["cleanup"]
    assert (
        audit_events[0].payload["evidence"]["error_message"]
        == "compose down failed, worktree removal failed"
    )


@pytest.mark.unit
async def test_destroy_cleanup_failure_message_is_bounded(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.failed)
    cleanup_failure = "cleanup failed: " + ("x" * _OPERATION_ERROR_MESSAGE_MAX_LENGTH)
    cleaner = RecordingCleaner(failures=[cleanup_failure])
    service, _stopper, _cleaner = _service(session, cleaner=cleaner)

    await service.destroy_workspace(
        workspace.id,
        force=False,
        remove_volumes=True,
        remove_worktree=True,
    )
    operations = await _operations(session, workspace.id)

    expected = cleanup_failure[:_OPERATION_ERROR_MESSAGE_MAX_LENGTH]
    assert workspace.failure_message == expected
    assert operations[0].error_message == expected


@pytest.mark.unit
async def test_destroy_replay_uses_in_progress_message_for_non_destroyed_workspace(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.failed)
    payload = {
        "owner": "operator_api",
        "source": "operator_api",
        "reason": None,
        "reason_code": "OPERATOR_DESTROY",
        "requested_action": "destroy",
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
