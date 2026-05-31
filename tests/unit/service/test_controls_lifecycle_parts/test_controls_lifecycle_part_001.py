"""Workspace control lifecycle behavior tests."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from awf.db.enums import AgentRuntime, OperationStatus, OperationType, WorkspaceStatus
from awf.db.models import MergeCandidate, Operation, Workspace, WorkspaceEvent
from awf.db.repositories import (
    MergeCandidateRepository,
    OperationRepository,
    SecretLeaseIssue,
    SecretLeaseRepository,
    TaskAttemptRepository,
    TaskRepository,
    WorkspaceEventRepository,
    WorkspaceRepository,
)
from awf.runtime.operator_hints import (
    OPERATOR_HINT_STATE_KEY,
    _initial_review_grace_done_key,
    _initial_review_grace_started_key,
    _non_check_reviewer_settle_done_key,
    _non_check_reviewer_settle_started_key,
)
from awf.service import controls as controls_module
from awf.service.controls import (
    IdempotencyConflictError,
    VersionConflictError,
    WorkspaceControlService,
    WorkspaceNotFoundError,
    WorkspaceRemonitorMissingPrUrlError,
    WorkspaceRemonitorStateError,
    WorkspaceStackStopError,
)
from tests.postgres import postgres_test_session


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with postgres_test_session() as s:
        yield s


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
        companion_worktrees: tuple[tuple[str, str], ...] = (),
        compose_project_name: str | None = None,
        compose_file_path: Path | None = None,
        worktree_host_path: Path | None = None,
        remove_volumes: bool = True,
        remove_worktree: bool = True,
    ) -> list[str]:
        _ = companion_worktrees
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
        companion_worktrees: tuple[tuple[str, str], ...] = (),
        compose_project_name: str | None = None,
        compose_file_path: Path | None = None,
        worktree_host_path: Path | None = None,
        remove_volumes: bool = True,
        remove_worktree: bool = True,
    ) -> list[str]:
        result = await super().cleanup(
            workspace_id=workspace_id,
            repo_url=repo_url,
            companion_worktrees=companion_worktrees,
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
                    "operator_failure" if self.final_status == WorkspaceStatus.failed else None
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


async def _issue_control_secret_lease(
    session: AsyncSession,
    workspace: Workspace,
    *,
    now: datetime | None = None,
) -> None:
    issued_at = now or datetime(2026, 4, 29, 12, 0, tzinfo=UTC)
    await SecretLeaseRepository(session).issue_declared_leases(
        workspace,
        leases=[
            SecretLeaseIssue(
                secret_name="api-token",
                kind="env",
                target="API_TOKEN",
                mode="ro",
                required=True,
                provider="env",
                ref_digest="sha256:" + "d" * 64,
                expires_at=issued_at + timedelta(hours=1),
                issue_metadata={"profile": "control-lifecycle", "declaration_index": 0},
            )
        ],
        now=issued_at,
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
    assert (
        replayed.payload
        == original_payload
        == {
            "owner": "operator_api",
            "source": "operator_api",
            "reason": "preserve audit",
            "reason_code": "OPERATOR_STOP",
            "requested_action": "stop",
        }
    )
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
async def test_control_prepare_operation_treats_blank_idempotency_key_as_absent(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.ready)
    service, _stopper, _cleaner = _service(session)

    await service.cancel_workspace(
        workspace.id,
        reason="blank-key-should-be-absent",
        stop_stack=False,
        idempotency_key="   ",
    )

    operations = await _operations(session, workspace.id)
    assert len(operations) == 1
    assert operations[0].idempotency_key is None


@pytest.mark.unit
async def test_append_only_events_do_not_invalidate_if_match_controls(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.ready)
    expected_version = workspace.version
    repo = WorkspaceRepository(session)
    await repo.add_event(
        workspace,
        event_type="workspace.audit.control_probe",
        reason_code="AUDIT_ONLY",
        payload={"probe": True},
    )
    service, _stopper, _cleaner = _service(session)

    response = await service.cancel_workspace(
        workspace.id,
        reason="operator fetched before audit event",
        stop_stack=False,
        idempotency_key="cancel-after-audit-only-event",
        expected_version=expected_version,
    )
    events = await _events(session, workspace.id)
    reason_codes = [event.reason_code for event in events]
    audit_event = next(event for event in events if event.reason_code == "AUDIT_ONLY")
    cancel_state_event = next(
        event
        for event in events
        if event.event_type == "workspace.state_changed" and event.reason_code == "OPERATOR_CANCEL"
    )

    assert response.status == WorkspaceStatus.cancelled
    assert workspace.version == expected_version + 1
    assert workspace.event_sequence == 4
    assert reason_codes.count("AUDIT_ONLY") == 1
    assert reason_codes.count("OPERATOR_CANCEL") == 2
    assert audit_event.event_order == 2
    assert cancel_state_event.event_order == 3


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
    expected_snapshot = {
        "monitor_claimed_by": "monitor-worker",
        "monitor_claim_expires_at": monitor_expiry.isoformat(),
        "execution_claimed_by": "execution-worker",
        "execution_claim_expires_at": execution_expiry.isoformat(),
    }
    pending_hint = events[0].payload["pending_operator_hint"]

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
    assert pending_hint == {
        "operation_id": operations[0].id,
        "reason": "worker restarted",
        "reason_code": "OPERATOR_REMONITOR",
        "requested_at": pending_hint["requested_at"],
        "status": "pending",
    }
    assert events[0].payload == {
        "reason": "worker restarted",
        "operation_id": operations[0].id,
        "claims_reset": expected_snapshot,
        "expected_version": 1,
        "pending_operator_hint": pending_hint,
    }


@pytest.mark.unit
async def test_remonitor_before_settle_persists_operator_hint_without_freeze(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.monitoring_pr)
    workspace.pr_url = "https://github.com/example/control-lifecycle/pull/44"
    workspace.pr_number = 44
    workspace.base_commit = "b" * 40
    workspace.monitor_last_commit_sha = "h" * 40
    await session.flush()
    service, _stopper, _cleaner = _service(session)

    response = await service.remonitor_workspace(
        workspace.id,
        reason="please repair the reviewer note",
        idempotency_key="remonitor-pre-settle-hint",
        expected_version=workspace.version,
    )
    operations = await _operations(session, workspace.id)
    events = await _events(session, workspace.id)
    monitor_state = dict(workspace.monitor_threads_addressed or {})
    persisted_hint = json.loads(monitor_state[OPERATOR_HINT_STATE_KEY])

    assert response.warnings == []
    assert workspace.version == 2
    assert persisted_hint == {
        "operation_id": operations[0].id,
        "reason": "please repair the reviewer note",
        "reason_code": "OPERATOR_REMONITOR",
        "requested_at": persisted_hint["requested_at"],
        "status": "pending",
    }
    assert all("initial_review_grace" not in key for key in monitor_state)
    assert all("non_check_reviewer_settle" not in key for key in monitor_state)
    assert "warnings" not in operations[0].result
    assert events[0].payload["pending_operator_hint"] == persisted_hint
    assert "warnings" not in events[0].payload


@pytest.mark.unit
async def test_remonitor_requested_at_invariant_raises_clear_error(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.monitoring_pr)
    workspace.pr_url = "https://github.com/example/control-lifecycle/pull/45"
    workspace.pr_number = 45
    workspace.base_commit = "b" * 40
    workspace.monitor_last_commit_sha = "h" * 40
    await session.flush()
    service, _stopper, _cleaner = _service(session)
    monkeypatch.setattr(controls_module, "utcnow", lambda: None)

    with pytest.raises(
        RuntimeError,
        match="operator remonitor requested_at was not initialized",
    ):
        await service.remonitor_workspace(
            workspace.id,
            reason="please repair the reviewer note",
            idempotency_key="remonitor-requested-at-invariant",
            expected_version=workspace.version,
        )


@pytest.mark.unit
async def test_remonitor_no_reason_past_settle_warns_without_operator_hint(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.monitoring_pr)
    head_sha = "f" * 40
    initial_done_key = _initial_review_grace_done_key(47)
    initial_started_key = _initial_review_grace_started_key(47)
    settle_done_key = _non_check_reviewer_settle_done_key(
        pr_number=47,
        head_sha=head_sha,
    )
    settle_started_key = _non_check_reviewer_settle_started_key(
        pr_number=47,
        head_sha=head_sha,
    )
    workspace.pr_url = "https://github.com/example/control-lifecycle/pull/47"
    workspace.pr_number = 47
    workspace.base_commit = "b" * 40
    workspace.monitor_last_commit_sha = head_sha
    workspace.monitor_threads_addressed = {
        initial_done_key: "elapsed",
        settle_done_key: "elapsed",
    }
    await session.flush()
    service, _stopper, _cleaner = _service(session)

    response = await service.remonitor_workspace(
        workspace.id,
        reason=None,
        idempotency_key="remonitor-no-reason-past-settle",
        expected_version=workspace.version,
    )
    operations = await _operations(session, workspace.id)
    events = await _events(session, workspace.id)
    monitor_state = dict(workspace.monitor_threads_addressed or {})
    warnings = [
        {
            "warning_code": "REMONITOR_PAST_SETTLE",
            "message": (
                "Workspace is past reviewer-settle window; auto-merge is paused "
                "until reviewer settle is re-evaluated."
            ),
        }
    ]

    assert response.status == WorkspaceStatus.monitoring_pr
    assert [warning.model_dump() for warning in response.warnings] == warnings
    assert workspace.version == 2
    assert initial_done_key not in monitor_state
    assert settle_done_key not in monitor_state
    assert float(monitor_state[initial_started_key]) >= 1_000_000_000
    assert float(monitor_state[settle_started_key]) >= 1_000_000_000
    assert OPERATOR_HINT_STATE_KEY not in monitor_state
    assert operations[0].result is not None
    assert operations[0].result["warnings"] == warnings
    assert events[0].event_type == "workspace.remonitor_requested"
    assert "pending_operator_hint" not in events[0].payload
    assert events[0].payload["warnings"] == warnings


@pytest.mark.unit
async def test_remonitor_no_reason_past_settle_arms_current_candidate_head(
    session: AsyncSession,
) -> None:
    workspace, candidate = await _workspace_with_candidate(
        session,
        status=WorkspaceStatus.monitoring_pr,
    )
    old_head_sha = "e" * 40
    current_head_sha = "f" * 40
    initial_done_key = _initial_review_grace_done_key(50)
    initial_started_key = _initial_review_grace_started_key(50)
    current_settle_done_key = _non_check_reviewer_settle_done_key(
        pr_number=50,
        head_sha=current_head_sha,
    )
    old_settle_started_key = _non_check_reviewer_settle_started_key(
        pr_number=50,
        head_sha=old_head_sha,
    )
    current_settle_started_key = _non_check_reviewer_settle_started_key(
        pr_number=50,
        head_sha=current_head_sha,
    )
    workspace.monitor_last_commit_sha = old_head_sha
    candidate.head_sha = current_head_sha
    workspace.monitor_threads_addressed = {
        initial_done_key: "elapsed",
        current_settle_done_key: "elapsed",
    }
    await session.flush()
    service, _stopper, _cleaner = _service(session)

    response = await service.remonitor_workspace(
        workspace.id,
        reason=None,
        idempotency_key="remonitor-no-reason-current-candidate-head",
        expected_version=workspace.version,
    )
    operations = await _operations(session, workspace.id)
    monitor_state = dict(workspace.monitor_threads_addressed or {})
    warnings = [
        {
            "warning_code": "REMONITOR_PAST_SETTLE",
            "message": (
                "Workspace is past reviewer-settle window; auto-merge is paused "
                "until reviewer settle is re-evaluated."
            ),
        }
    ]

    assert response.status == WorkspaceStatus.monitoring_pr
    assert [warning.model_dump() for warning in response.warnings] == warnings
    assert initial_done_key not in monitor_state
    assert current_settle_done_key not in monitor_state
    assert float(monitor_state[initial_started_key]) >= 1_000_000_000
    assert old_settle_started_key not in monitor_state
    assert float(monitor_state[current_settle_started_key]) >= 1_000_000_000
    assert OPERATOR_HINT_STATE_KEY not in monitor_state
    assert operations[0].result is not None
    assert operations[0].result["warnings"] == warnings


@pytest.mark.unit
async def test_remonitor_no_reason_past_settle_prefers_workspace_head_when_open_candidate_stale(
    session: AsyncSession,
) -> None:
    workspace, candidate = await _workspace_with_candidate(
        session,
        status=WorkspaceStatus.monitoring_pr,
    )
    stale_candidate_head_sha = "e" * 40
    workspace_head_sha = "f" * 40
    candidate.head_sha = stale_candidate_head_sha
    candidate.updated_at = datetime(2026, 5, 31, 14, 0, tzinfo=UTC)
    await session.flush()
    initial_done_key = _initial_review_grace_done_key(50)
    initial_started_key = _initial_review_grace_started_key(50)
    workspace_settle_done_key = _non_check_reviewer_settle_done_key(
        pr_number=50,
        head_sha=workspace_head_sha,
    )
    stale_settle_started_key = _non_check_reviewer_settle_started_key(
        pr_number=50,
        head_sha=stale_candidate_head_sha,
    )
    workspace_settle_started_key = _non_check_reviewer_settle_started_key(
        pr_number=50,
        head_sha=workspace_head_sha,
    )
    workspace.monitor_last_commit_sha = workspace_head_sha
    workspace.monitor_threads_addressed = {
        initial_done_key: "elapsed",
        workspace_settle_done_key: "elapsed",
    }
    workspace.updated_at = datetime(2026, 5, 31, 14, 1, tzinfo=UTC)
    await session.flush()
    service, _stopper, _cleaner = _service(session)

    response = await service.remonitor_workspace(
        workspace.id,
        reason=None,
        idempotency_key="remonitor-no-reason-stale-open-candidate-head",
        expected_version=workspace.version,
    )
    operations = await _operations(session, workspace.id)
    monitor_state = dict(workspace.monitor_threads_addressed or {})
    warnings = [
        {
            "warning_code": "REMONITOR_PAST_SETTLE",
            "message": (
                "Workspace is past reviewer-settle window; auto-merge is paused "
                "until reviewer settle is re-evaluated."
            ),
        }
    ]

    assert response.status == WorkspaceStatus.monitoring_pr
    assert [warning.model_dump() for warning in response.warnings] == warnings
    assert candidate.head_sha == stale_candidate_head_sha
    assert initial_done_key not in monitor_state
    assert workspace_settle_done_key not in monitor_state
    assert float(monitor_state[initial_started_key]) >= 1_000_000_000
    assert stale_settle_started_key not in monitor_state
    assert float(monitor_state[workspace_settle_started_key]) >= 1_000_000_000
    assert OPERATOR_HINT_STATE_KEY not in monitor_state
    assert operations[0].result is not None
    assert operations[0].result["warnings"] == warnings


@pytest.mark.unit
async def test_remonitor_no_reason_stale_past_settle_does_not_arm_current_candidate_head(
    session: AsyncSession,
) -> None:
    workspace, candidate = await _workspace_with_candidate(
        session,
        status=WorkspaceStatus.monitoring_pr,
    )
    old_head_sha = "e" * 40
    current_head_sha = "f" * 40
    initial_done_key = _initial_review_grace_done_key(50)
    old_settle_done_key = _non_check_reviewer_settle_done_key(
        pr_number=50,
        head_sha=old_head_sha,
    )
    current_settle_started_key = _non_check_reviewer_settle_started_key(
        pr_number=50,
        head_sha=current_head_sha,
    )
    workspace.monitor_last_commit_sha = old_head_sha
    candidate.head_sha = current_head_sha
    workspace.monitor_threads_addressed = {
        initial_done_key: "elapsed",
        old_settle_done_key: "elapsed",
    }
    await session.flush()
    service, _stopper, _cleaner = _service(session)

    response = await service.remonitor_workspace(
        workspace.id,
        reason=None,
        idempotency_key="remonitor-no-reason-stale-current-candidate-head",
        expected_version=workspace.version,
    )
    operations = await _operations(session, workspace.id)
    events = await _events(session, workspace.id)
    monitor_state = dict(workspace.monitor_threads_addressed or {})

    assert response.status == WorkspaceStatus.monitoring_pr
    assert response.warnings == []
    assert monitor_state[initial_done_key] == "elapsed"
    assert monitor_state[old_settle_done_key] == "elapsed"
    assert current_settle_started_key not in monitor_state
    assert OPERATOR_HINT_STATE_KEY not in monitor_state
    assert operations[0].result is not None
    assert "warnings" not in operations[0].result
    assert "warnings" not in events[0].payload


@pytest.mark.unit
async def test_remonitor_failed_workspace_past_settle_arms_latest_closed_candidate_head(
    session: AsyncSession,
) -> None:
    workspace, candidate = await _workspace_with_candidate(
        session,
        status=WorkspaceStatus.failed,
    )
    hint = "resume reviewer-settle after monitor failure"
    old_head_sha = "e" * 40
    live_head_sha = "f" * 40
    initial_done_key = _initial_review_grace_done_key(50)
    initial_started_key = _initial_review_grace_started_key(50)
    live_settle_done_key = _non_check_reviewer_settle_done_key(
        pr_number=50,
        head_sha=live_head_sha,
    )
    old_settle_started_key = _non_check_reviewer_settle_started_key(
        pr_number=50,
        head_sha=old_head_sha,
    )
    live_settle_started_key = _non_check_reviewer_settle_started_key(
        pr_number=50,
        head_sha=live_head_sha,
    )
    workspace.monitor_last_commit_sha = old_head_sha
    workspace.failure_reason = "infrastructure_failure"
    workspace.failure_message = "old monitor failed after reviewer settle"
    workspace.monitor_iter_count = 3
    workspace.monitor_threads_addressed = {
        initial_done_key: "elapsed",
        live_settle_done_key: "elapsed",
    }
    candidate.head_sha = live_head_sha
    candidate.status = "closed"
    candidate.close_reason = "STALE_MONITOR_FAILED"
    candidate.closed_at = datetime.now(UTC)
    await session.flush()
    service, _stopper, _cleaner = _service(session)

    response = await service.remonitor_workspace(
        workspace.id,
        reason=hint,
        idempotency_key="remonitor-failed-closed-candidate-head",
        expected_version=workspace.version,
    )
    operations = await _operations(session, workspace.id)
    monitor_state = dict(workspace.monitor_threads_addressed or {})
    warnings = [
        {
            "warning_code": "REMONITOR_PAST_SETTLE",
            "message": (
                "Workspace is past reviewer-settle window; auto-merge is frozen "
                "until the operator hint is processed."
            ),
        }
    ]

    assert response.status == WorkspaceStatus.monitoring_pr
    assert [warning.model_dump() for warning in response.warnings] == warnings
    assert candidate.status == "open"
    assert candidate.head_sha == live_head_sha
    assert initial_done_key not in monitor_state
    assert live_settle_done_key not in monitor_state
    assert float(monitor_state[initial_started_key]) >= 1_000_000_000
    assert old_settle_started_key not in monitor_state
    assert float(monitor_state[live_settle_started_key]) >= 1_000_000_000
    assert operations[0].result is not None
    assert operations[0].result["warnings"] == warnings


@pytest.mark.unit
async def test_cancel_stop_destroy_remonitor_payloads_include_operator_audit(
    session: AsyncSession,
) -> None:
    cancel = await _workspace(session, status=WorkspaceStatus.completed, title="cancel audit")
    stop = await _workspace(session, status=WorkspaceStatus.completed, title="stop audit")
    destroy = await _workspace(session, status=WorkspaceStatus.destroyed, title="destroy audit")
    remonitor = await _workspace(
        session, status=WorkspaceStatus.monitoring_pr, title="remonitor audit"
    )
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
        operation.type: operation
        for operation in await OperationRepository(session).list_all(limit=20)
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
async def test_refresh_active_workspace_keeps_operation_pending_and_coalesces_new_same_payload_request(
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
    repeated = await service.request_refresh_workspace(
        workspace.id,
        reason="stale merge queue",
        idempotency_key="refresh-fresh-key",
    )
    operations = await _operations(session, workspace.id)
    events = await _events(session, workspace.id)
    refresh_events = [
        event for event in events if event.event_type == "workspace.refresh_requested"
    ]
    first_refresh_event = next(
        event for event in refresh_events if event.payload["operation_id"] == operation.id
    )

    assert repeated.id == operation.id
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
    assert operation.result is None
    assert repeated.idempotency_key == "refresh-first"
    assert repeated.result is None
    assert len(refresh_events) == 1
    assert first_refresh_event.reason_code == "OPERATOR_REFRESH"
    assert first_refresh_event.payload == {
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
    assert operation.status == OperationStatus.pending.value
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
async def test_remonitor_failed_workspace_past_settle_persists_operator_hint_and_warns(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.failed)
    hint = "fix the reviewer-requested release note before merge"
    head_sha = "f" * 40
    initial_done_key = _initial_review_grace_done_key(46)
    initial_started_key = _initial_review_grace_started_key(46)
    settle_done_key = _non_check_reviewer_settle_done_key(
        pr_number=46,
        head_sha=head_sha,
    )
    settle_started_key = _non_check_reviewer_settle_started_key(
        pr_number=46,
        head_sha=head_sha,
    )
    workspace.pr_url = "https://github.com/example/control-lifecycle/pull/46"
    workspace.pr_number = 46
    workspace.base_commit = "b" * 40
    workspace.monitor_last_commit_sha = head_sha
    workspace.failure_reason = "infrastructure_failure"
    workspace.failure_message = "old monitor failed after reviewer settle"
    workspace.monitor_iter_count = 3
    workspace.monitor_threads_addressed = {
        initial_done_key: "elapsed",
        settle_done_key: "elapsed",
    }
    await session.flush()
    service, _stopper, _cleaner = _service(session)

    response = await service.remonitor_workspace(
        workspace.id,
        reason=hint,
        idempotency_key="remonitor-failed-past-settle-hint",
        expected_version=workspace.version,
    )
    operations = await _operations(session, workspace.id)
    events = await _events(session, workspace.id)
    monitor_state = dict(workspace.monitor_threads_addressed or {})
    persisted_hint = json.loads(monitor_state[OPERATOR_HINT_STATE_KEY])
    warnings = [
        {
            "warning_code": "REMONITOR_PAST_SETTLE",
            "message": (
                "Workspace is past reviewer-settle window; auto-merge is frozen "
                "until the operator hint is processed."
            ),
        }
    ]

    assert response.status == WorkspaceStatus.monitoring_pr
    assert [warning.model_dump() for warning in response.warnings] == warnings
    assert workspace.status == WorkspaceStatus.monitoring_pr.value
    assert workspace.failure_reason is None
    assert workspace.failure_message is None
    assert workspace.monitor_iter_count == 0
    assert workspace.version == 2
    assert initial_done_key not in monitor_state
    assert settle_done_key not in monitor_state
    assert float(monitor_state[initial_started_key]) >= 1_000_000_000
    assert float(monitor_state[settle_started_key]) >= 1_000_000_000
    assert persisted_hint == {
        "operation_id": operations[0].id,
        "reason": hint,
        "reason_code": "OPERATOR_REMONITOR",
        "requested_at": persisted_hint["requested_at"],
        "status": "pending",
    }
    assert operations[0].result is not None
    assert operations[0].result["warnings"] == warnings
    assert events[0].event_type == "workspace.remonitor_requested"
    assert events[0].old_state == WorkspaceStatus.failed.value
    assert events[0].new_state == WorkspaceStatus.monitoring_pr.value
    assert events[0].payload["state_reset"] == {
        "from": WorkspaceStatus.failed.value,
        "to": WorkspaceStatus.monitoring_pr.value,
        "monitor_iter_count_reset_from": 3,
        "candidate_reopened": False,
    }
    assert events[0].payload["pending_operator_hint"] == persisted_hint
    assert events[0].payload["warnings"] == warnings


@pytest.mark.unit
async def test_remonitor_failed_workspace_past_settle_uses_elapsed_marker_when_last_sha_stale(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.failed)
    hint = "fix the reviewer-requested release note before merge"
    stale_head_sha = "e" * 40
    live_head_sha = "f" * 40
    initial_done_key = _initial_review_grace_done_key(46)
    initial_started_key = _initial_review_grace_started_key(46)
    live_settle_done_key = _non_check_reviewer_settle_done_key(
        pr_number=46,
        head_sha=live_head_sha,
    )
    live_settle_started_key = _non_check_reviewer_settle_started_key(
        pr_number=46,
        head_sha=live_head_sha,
    )
    stale_settle_started_key = _non_check_reviewer_settle_started_key(
        pr_number=46,
        head_sha=stale_head_sha,
    )
    workspace.pr_url = "https://github.com/example/control-lifecycle/pull/46"
    workspace.pr_number = 46
    workspace.base_commit = "b" * 40
    workspace.monitor_last_commit_sha = stale_head_sha
    workspace.failure_reason = "infrastructure_failure"
    workspace.failure_message = "old monitor failed after reviewer settle"
    workspace.monitor_iter_count = 3
    workspace.monitor_threads_addressed = {
        initial_done_key: "elapsed",
        live_settle_done_key: "elapsed",
    }
    await session.flush()
    service, _stopper, _cleaner = _service(session)

    response = await service.remonitor_workspace(
        workspace.id,
        reason=hint,
        idempotency_key="remonitor-failed-stale-sha-past-settle-hint",
        expected_version=workspace.version,
    )
    operations = await _operations(session, workspace.id)
    events = await _events(session, workspace.id)
    monitor_state = dict(workspace.monitor_threads_addressed or {})
    warnings = [
        {
            "warning_code": "REMONITOR_PAST_SETTLE",
            "message": (
                "Workspace is past reviewer-settle window; auto-merge is frozen "
                "until the operator hint is processed."
            ),
        }
    ]

    assert response.status == WorkspaceStatus.monitoring_pr
    assert [warning.model_dump() for warning in response.warnings] == warnings
    assert initial_done_key not in monitor_state
    assert live_settle_done_key not in monitor_state
    assert float(monitor_state[initial_started_key]) >= 1_000_000_000
    assert float(monitor_state[live_settle_started_key]) >= 1_000_000_000
    assert stale_settle_started_key not in monitor_state
    assert operations[0].result is not None
    assert operations[0].result["warnings"] == warnings
    assert events[0].payload["warnings"] == warnings
