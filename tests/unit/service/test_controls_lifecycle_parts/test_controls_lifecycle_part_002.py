"""Workspace control lifecycle behavior tests."""

from __future__ import annotations

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
from awf.db.repositories.base import (
    TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
    has_terminal_runtime_released_event,
)
from awf.node.cleanup import (
    COMPOSE_DOWN_SUCCEEDED,
    WorkspaceCleanupResult,
    WorkspaceCleanupStepResult,
)
from awf.service.controls import (
    _OPERATION_ERROR_MESSAGE_MAX_LENGTH,
    ActiveWorkspaceDestroyError,
    IdempotencyConflictError,
    WorkspaceControlService,
    WorkspaceRebaseActiveConflictError,
    WorkspaceRebaseMissingCandidateError,
    WorkspaceRebaseMissingPrUrlError,
    WorkspaceRebaseStateError,
    WorkspaceRefreshStateError,
    WorkspaceStackStopError,
    WorkspaceValidateMissingPrUrlError,
    WorkspaceValidateStateError,
    _json_datetime,
    default_cleaner,
    stop_project_containers,
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
    companion_worktrees: tuple[tuple[str, str], ...]
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
        self.calls.append(
            CleanupCall(
                workspace_id=workspace_id,
                repo_url=repo_url,
                companion_worktrees=companion_worktrees,
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


@dataclass
class StructuredCleaner:
    result: WorkspaceCleanupResult
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
    ) -> WorkspaceCleanupResult:
        self.calls.append(
            CleanupCall(
                workspace_id=workspace_id,
                repo_url=repo_url,
                companion_worktrees=companion_worktrees,
                compose_project_name=compose_project_name,
                compose_file_path=compose_file_path,
                worktree_host_path=worktree_host_path,
                remove_volumes=remove_volumes,
                remove_worktree=remove_worktree,
            )
        )
        return self.result


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
    assert [row.id for row in await _operations(session, workspace.id)] == before_operation_ids
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
    workspace.task_policy = {
        "companions": [
            {
                "name": "backend",
                "repo_url": "git@github.com:example/backend.git",
            }
        ]
    }
    await session.flush()
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
    state_events = [event for event in events if event.event_type == "workspace.state_changed"]

    assert response.operation_id == operations[0].id
    assert response.status == WorkspaceStatus.destroyed
    assert response.message == "workspace destroyed"
    assert workspace.status == WorkspaceStatus.destroyed.value
    assert len(cleaner.calls) == 1
    assert cleaner.calls[0] == CleanupCall(
        workspace_id=workspace.id,
        repo_url=workspace.repo_url,
        companion_worktrees=(
            (
                f"{workspace.id}__companion__backend",
                "git@github.com:example/backend.git",
            ),
        ),
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
    assert not any(event.event_type == "workspace.stale_callback_ignored" for event in events)


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
        companion_worktrees=(),
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
    state_events = [event for event in events if event.event_type == "workspace.state_changed"]
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
async def test_destroy_workspace_revokes_active_secret_leases_before_cleanup(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.ready)
    await _issue_control_secret_lease(session, workspace)
    cleanup_seen_statuses: list[list[str]] = []

    class LeaseCheckingCleaner(RecordingCleaner):
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
            leases = await SecretLeaseRepository(session).list_for_workspace(workspace_id)
            cleanup_seen_statuses.append([lease.status for lease in leases])
            return await super().cleanup(
                workspace_id=workspace_id,
                repo_url=repo_url,
                companion_worktrees=companion_worktrees,
                compose_project_name=compose_project_name,
                compose_file_path=compose_file_path,
                worktree_host_path=worktree_host_path,
                remove_volumes=remove_volumes,
                remove_worktree=remove_worktree,
            )

    cleaner = LeaseCheckingCleaner()
    service, _stopper, _cleaner = _service(session, cleaner=cleaner)

    response = await service.destroy_workspace(
        workspace.id,
        force=True,
        remove_volumes=True,
        remove_worktree=True,
        idempotency_key="destroy-with-secret-lease",
    )
    operations = await _operations(session, workspace.id)
    leases = await SecretLeaseRepository(session).list_for_workspace(workspace.id)
    audit_events = await WorkspaceEventRepository(session).list(
        workspace_id=workspace.id,
        event_type="workspace.audit.control_operation",
        limit=10,
    )

    assert response.status == WorkspaceStatus.destroyed
    assert cleanup_seen_statuses == [["revoked"]]
    assert leases[0].status == "revoked"
    assert leases[0].revoke_reason_code == "TERMINAL_CLEANUP"
    assert operations[0].result is not None
    assert operations[0].result["secret_leases"] == {
        "revoked_count": 1,
        "reason_code": "TERMINAL_CLEANUP",
    }
    assert audit_events[0].payload is not None
    assert audit_events[0].payload["evidence"]["lease_revocations"] == {
        "revoked_count": 1,
        "reason_code": "TERMINAL_CLEANUP",
    }


@pytest.mark.unit
async def test_destroy_workspace_replay_keeps_secret_lease_revocation_idempotent(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.ready)
    await _issue_control_secret_lease(session, workspace)
    service, _stopper, _cleaner = _service(session)

    first = await service.destroy_workspace(
        workspace.id,
        force=True,
        remove_volumes=True,
        remove_worktree=True,
        idempotency_key="destroy-secret-replay",
    )
    replay = await service.destroy_workspace(
        workspace.id,
        force=True,
        remove_volumes=True,
        remove_worktree=True,
        idempotency_key="destroy-secret-replay",
    )
    leases = await SecretLeaseRepository(session).list_for_workspace(workspace.id)
    events = await _events(session, workspace.id)

    assert replay.operation_id == first.operation_id
    assert leases[0].status == "revoked"
    assert leases[0].revoke_reason_code == "TERMINAL_CLEANUP"
    assert [event.reason_code for event in events].count("SECRET_LEASE_REVOKED") == 1


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
async def test_destroy_partial_cleanup_records_runtime_released_when_compose_down_succeeded(
    session: AsyncSession,
) -> None:
    partial_result = WorkspaceCleanupResult.from_steps(
        [
            WorkspaceCleanupStepResult(
                name="compose_down",
                status="succeeded",
                reason_code=COMPOSE_DOWN_SUCCEEDED,
            ),
            WorkspaceCleanupStepResult(
                name="worktree_remove",
                status="failed",
                reason_code="CLEANUP_STEP_FAILED",
                error="worktree removal failed",
            ),
        ]
    )
    cleaner = StructuredCleaner(result=partial_result)
    service = WorkspaceControlService(
        session,
        project_stopper=RecordingStopper(),
        cleaner_factory=lambda: cleaner,
    )

    workspace = await _workspace(session, status=WorkspaceStatus.destroying)

    response = await service.destroy_workspace(
        workspace.id,
        force=False,
        remove_volumes=True,
        remove_worktree=True,
    )

    assert response.status == WorkspaceStatus.failed
    assert workspace.status == WorkspaceStatus.failed.value
    assert await has_terminal_runtime_released_event(session, workspace.id) is True


@pytest.mark.unit
async def test_destroy_compose_file_only_records_runtime_released_after_cleanup(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.destroying)
    compose_file_path = workspace.compose_file_path
    assert compose_file_path is not None
    workspace.compose_project_name = None
    await session.flush()
    cleaner = RecordingCleaner()
    service, _stopper, _cleaner = _service(session, cleaner=cleaner)

    response = await service.destroy_workspace(
        workspace.id,
        force=False,
        remove_volumes=True,
        remove_worktree=True,
    )
    events = await _events(session, workspace.id)
    release_events = [
        event for event in events if event.event_type == TERMINAL_RUNTIME_RELEASE_EVENT_TYPE
    ]

    assert response.status == WorkspaceStatus.destroyed
    assert workspace.status == WorkspaceStatus.destroyed.value
    assert cleaner.calls[0].compose_project_name is None
    assert cleaner.calls[0].compose_file_path == Path(compose_file_path)
    assert await has_terminal_runtime_released_event(session, workspace.id) is True
    assert len(release_events) == 1
    assert release_events[0].payload is not None
    assert release_events[0].payload["compose_project_name"] is None
    assert release_events[0].payload["compose_file_path"] == compose_file_path
    assert release_events[0].payload["workspace_status"] == WorkspaceStatus.destroyed.value


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
    assert [event.new_state for event in state_change_events] == [WorkspaceStatus.destroyed.value]


@pytest.mark.unit
async def test_default_stack_helpers_handle_noop_and_construct_cleaner() -> None:
    await stop_project_containers(None)

    cleaner = default_cleaner()

    assert hasattr(cleaner, "cleanup")
    assert _json_datetime(None) is None
