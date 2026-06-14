"""Destroy/stop/refresh control branches that earlier suites left uncovered."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from awf.db.enums import OperationStatus, OperationType, WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import (
    OperationRepository,
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
from awf.service.controls import WorkspaceControlService, WorkspaceRefreshStateError
from awf.service.failure_causality import (
    PRIMARY_FAILURE_KEY,
    SECONDARY_FAILURE_KEY,
    SECONDARY_FAILURE_RECORDED_EVENT_TYPE,
    SECONDARY_FAILURES_KEY,
)
from tests.postgres import postgres_test_session
from tests.unit.service.test_controls_lifecycle_parts.controls_lifecycle_helpers import (
    CleanupCall,
    RecordingCleaner,
    RecordingStopper,
    _events,
    _operations,
    _service,
    _workspace,
    compose_down_succeeded_result,
)


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with postgres_test_session() as s:
        yield s


@dataclass
class StatusMutatingCleaner:
    """Cleaner that rewrites the workspace row mid-cleanup, like a stale callback.

    Used to drive the post-refresh status into a non-terminal state so the
    destroy success path skips both the ``destroyed`` transition and (optionally)
    the terminal runtime-release event.
    """

    session: AsyncSession
    final_status: WorkspaceStatus
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
                compose_project_name=compose_project_name,
                compose_file_path=compose_file_path,
                worktree_host_path=worktree_host_path,
                remove_volumes=remove_volumes,
                remove_worktree=remove_worktree,
            )
        )
        await self.session.execute(
            sa_update(Workspace)
            .where(Workspace.id == workspace_id)
            .values(status=self.final_status.value)
            .execution_options(synchronize_session=False)
        )
        await self.session.flush()
        return self.result


@dataclass
class FailingStructuredCleaner:
    """Cleaner returning a failed result and mutating the row to a chosen status.

    When ``set_failure_fields`` is False the row is moved to ``failed`` without
    populating ``failure_reason``/``failure_message`` so no durable primary
    failure is discoverable.
    """

    session: AsyncSession
    final_status: WorkspaceStatus
    result: WorkspaceCleanupResult
    set_failure_fields: bool = True
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
                compose_project_name=compose_project_name,
                compose_file_path=compose_file_path,
                worktree_host_path=worktree_host_path,
                remove_volumes=remove_volumes,
                remove_worktree=remove_worktree,
            )
        )
        values: dict[str, object | None] = {"status": self.final_status.value}
        if self.set_failure_fields:
            values["failure_reason"] = "operator_failure"
            values["failure_message"] = "operator moved workspace to failed"
        else:
            values["failure_reason"] = None
            values["failure_message"] = None
        await self.session.execute(
            sa_update(Workspace)
            .where(Workspace.id == workspace_id)
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        await self.session.flush()
        return self.result


async def _seed_durable_primary_failure(
    session: AsyncSession,
    *,
    reason_code: str,
    message: str,
) -> Workspace:
    """Drive a workspace to ``failed`` with a durable primary-failure event."""
    repo = WorkspaceRepository(session)
    workspace = await _workspace(session, status=WorkspaceStatus.ready, title="primary failure")
    await repo.transition(workspace, to=WorkspaceStatus.running, reason_code="SEED")
    await repo.transition(workspace, to=WorkspaceStatus.validating, reason_code="SEED")
    workspace.failure_reason = "validation_failure"
    workspace.failure_message = message
    await repo.transition(
        workspace,
        to=WorkspaceStatus.failed,
        reason_code=reason_code,
        payload={
            "reason_code": reason_code,
            "message": message,
            PRIMARY_FAILURE_KEY: {
                "failure_reason": "validation_failure",
                "reason_code": reason_code,
                "message": message,
            },
        },
    )
    await session.flush()
    return workspace


# ---------------------------------------------------------------------------
# destroy: cleanup failure preserving a durable primary failure
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_destroy_cleanup_failure_preserves_primary_failure_and_records_secondary(
    session: AsyncSession,
) -> None:
    workspace = await _seed_durable_primary_failure(
        session,
        reason_code="VALIDATION_INSUFFICIENT_TIER",
        message="required tier failed",
    )
    cleaner = RecordingCleaner(failures=["compose down failed"])
    service, _stopper, _cleaner = _service(session, cleaner=cleaner)

    response = await service.destroy_workspace(
        workspace.id,
        force=False,
        remove_volumes=True,
        remove_worktree=True,
    )
    operations = await _operations(session, workspace.id)
    destroy_op = next(op for op in operations if op.type == OperationType.destroy.value)
    audit_events = await WorkspaceEventRepository(session).list(
        workspace_id=workspace.id,
        event_type="workspace.audit.control_operation",
        limit=10,
    )
    # The destroying -> failed transition is keyed on the preserved primary
    # reason code (not the generic CLEANUP_FAILED fallback).
    failed_transitions = [
        event
        for event in await _events(session, workspace.id)
        if event.event_type == "workspace.state_changed"
        and event.new_state == WorkspaceStatus.failed.value
        and event.old_state == WorkspaceStatus.destroying.value
    ]

    assert response.status == WorkspaceStatus.failed
    assert response.message == "workspace cleanup failed"
    assert workspace.status == WorkspaceStatus.failed.value
    assert len(failed_transitions) == 1
    assert failed_transitions[0].reason_code == "VALIDATION_INSUFFICIENT_TIER"
    # The original validation failure is restored onto the live row, not the
    # generic cleanup_failure reason.
    assert workspace.failure_reason == "validation_failure"
    assert workspace.failure_message == "required tier failed"
    # Operation/audit fail with the preserved primary reason code, and the
    # cleanup fault is recorded as the secondary failure.
    assert destroy_op.status == OperationStatus.failed.value
    assert destroy_op.error_code == "CLEANUP_FAILED"
    assert destroy_op.result[PRIMARY_FAILURE_KEY]["reason_code"] == "VALIDATION_INSUFFICIENT_TIER"
    assert destroy_op.result[SECONDARY_FAILURE_KEY]["reason_code"] == "CLEANUP_FAILED"
    assert destroy_op.result[SECONDARY_FAILURES_KEY][-1]["failure_reason"] == "cleanup_failure"
    # The audit event is keyed on the cleanup fault, but carries the preserved
    # primary failure (and the cleanup secondary) in its evidence.
    assert audit_events[0].reason_code == "CLEANUP_FAILED"
    assert audit_events[0].payload is not None
    assert (
        audit_events[0].payload["evidence"][PRIMARY_FAILURE_KEY]["reason_code"]
        == "VALIDATION_INSUFFICIENT_TIER"
    )
    assert (
        audit_events[0].payload["evidence"][SECONDARY_FAILURE_KEY]["reason_code"]
        == "CLEANUP_FAILED"
    )


# ---------------------------------------------------------------------------
# destroy: cleanup failure when the row is already ``failed`` (no failed->failed edge)
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_destroy_cleanup_failure_on_already_failed_row_records_secondary_event(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.completed)
    failed_result = WorkspaceCleanupResult.from_steps(
        [
            WorkspaceCleanupStepResult(
                name="worktree_remove",
                status="failed",
                reason_code="CLEANUP_STEP_FAILED",
                error="worktree removal failed",
            )
        ]
    )
    cleaner = FailingStructuredCleaner(
        session=session,
        final_status=WorkspaceStatus.failed,
        result=failed_result,
        set_failure_fields=False,
    )
    service = WorkspaceControlService(
        session,
        project_stopper=RecordingStopper(),
        cleaner_factory=lambda: cleaner,
    )

    response = await service.destroy_workspace(
        workspace.id,
        force=False,
        remove_volumes=True,
        remove_worktree=True,
    )
    operations = await _operations(session, workspace.id)
    destroy_op = next(op for op in operations if op.type == OperationType.destroy.value)
    secondary_events = await WorkspaceEventRepository(session).list(
        workspace_id=workspace.id,
        event_type=SECONDARY_FAILURE_RECORDED_EVENT_TYPE,
        limit=10,
    )

    # There is no failed -> failed state-machine edge, so instead of a state
    # transition the service emits a synthetic secondary-failure event.
    assert response.status == WorkspaceStatus.failed
    assert response.message == "workspace cleanup failed"
    assert workspace.status == WorkspaceStatus.failed.value
    assert destroy_op.status == OperationStatus.failed.value
    assert destroy_op.error_code == "CLEANUP_FAILED"
    assert len(secondary_events) == 1
    payload = secondary_events[0].payload
    assert payload is not None
    assert payload["synthetic"] is True
    assert payload[SECONDARY_FAILURE_KEY]["reason_code"] == "CLEANUP_FAILED"
    assert payload[SECONDARY_FAILURE_KEY]["failure_reason"] == "cleanup_failure"
    assert payload[SECONDARY_FAILURES_KEY][-1]["reason_code"] == "CLEANUP_FAILED"
    # No durable primary failure existed for this row, so the synthetic event
    # does not embed primary evidence.
    assert PRIMARY_FAILURE_KEY not in payload


@pytest.mark.unit
async def test_destroy_cleanup_failure_on_already_failed_row_embeds_primary_evidence(
    session: AsyncSession,
) -> None:
    workspace = await _seed_durable_primary_failure(
        session,
        reason_code="VALIDATION_INSUFFICIENT_TIER",
        message="required tier failed",
    )
    failed_result = WorkspaceCleanupResult.from_steps(
        [
            WorkspaceCleanupStepResult(
                name="worktree_remove",
                status="failed",
                reason_code="CLEANUP_STEP_FAILED",
                error="worktree removal failed",
            )
        ]
    )
    # The cleaner moves the row back to ``failed`` (mid-destroy) without clearing
    # the durable primary-failure event, so the service has a primary to embed.
    cleaner = FailingStructuredCleaner(
        session=session,
        final_status=WorkspaceStatus.failed,
        result=failed_result,
    )
    service = WorkspaceControlService(
        session,
        project_stopper=RecordingStopper(),
        cleaner_factory=lambda: cleaner,
    )

    response = await service.destroy_workspace(
        workspace.id,
        force=False,
        remove_volumes=True,
        remove_worktree=True,
    )
    secondary_events = await WorkspaceEventRepository(session).list(
        workspace_id=workspace.id,
        event_type=SECONDARY_FAILURE_RECORDED_EVENT_TYPE,
        limit=10,
    )

    assert response.status == WorkspaceStatus.failed
    assert workspace.status == WorkspaceStatus.failed.value
    assert len(secondary_events) == 1
    payload = secondary_events[0].payload
    assert payload is not None
    # With a durable primary, the synthetic secondary-failure event embeds the
    # preserved primary-failure evidence alongside the cleanup secondary.
    assert payload["synthetic"] is True
    assert payload[PRIMARY_FAILURE_KEY]["reason_code"] == "VALIDATION_INSUFFICIENT_TIER"
    assert payload[SECONDARY_FAILURE_KEY]["reason_code"] == "CLEANUP_FAILED"


# ---------------------------------------------------------------------------
# destroy: success path where the row was mutated out of ``destroying``
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_destroy_success_skips_destroyed_transition_when_row_left_destroying(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.destroying)
    succeeded_result = WorkspaceCleanupResult.from_steps(
        [
            WorkspaceCleanupStepResult(
                name="compose_down",
                status="succeeded",
                reason_code=COMPOSE_DOWN_SUCCEEDED,
            )
        ]
    )
    cleaner = StatusMutatingCleaner(
        session=session,
        final_status=WorkspaceStatus.ready,
        result=succeeded_result,
    )
    service = WorkspaceControlService(
        session,
        project_stopper=RecordingStopper(),
        cleaner_factory=lambda: cleaner,
    )

    response = await service.destroy_workspace(
        workspace.id,
        force=False,
        remove_volumes=True,
        remove_worktree=True,
    )
    operations = await _operations(session, workspace.id)
    destroy_op = next(op for op in operations if op.type == OperationType.destroy.value)

    # ``ready`` cannot transition to ``destroyed`` so the row keeps the status
    # the cleaner left behind, but cleanup succeeded so the operation succeeds
    # and the runtime-release event is still emitted.
    assert response.status == WorkspaceStatus.ready
    assert workspace.status == WorkspaceStatus.ready.value
    assert destroy_op.status == OperationStatus.succeeded.value
    assert await has_terminal_runtime_released_event(session, workspace.id) is True


@pytest.mark.unit
async def test_destroy_success_skips_runtime_release_when_cleanup_only_skipped(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.destroying)
    skipped_result = WorkspaceCleanupResult.skipped(reason_code="CLEANUP_SKIPPED")
    cleaner = StatusMutatingCleaner(
        session=session,
        final_status=WorkspaceStatus.ready,
        result=skipped_result,
    )
    service = WorkspaceControlService(
        session,
        project_stopper=RecordingStopper(),
        cleaner_factory=lambda: cleaner,
    )

    response = await service.destroy_workspace(
        workspace.id,
        force=False,
        remove_volumes=True,
        remove_worktree=True,
    )
    operations = await _operations(session, workspace.id)
    destroy_op = next(op for op in operations if op.type == OperationType.destroy.value)
    release_events = [
        event
        for event in await _events(session, workspace.id)
        if event.event_type == TERMINAL_RUNTIME_RELEASE_EVENT_TYPE
    ]

    # ``skipped`` cleanup is ``ok`` but did not prove the runtime stopped, so no
    # terminal runtime-release event is recorded; cleanup owns that later.
    assert response.status == WorkspaceStatus.ready
    assert destroy_op.status == OperationStatus.succeeded.value
    assert release_events == []
    assert await has_terminal_runtime_released_event(session, workspace.id) is False


# ---------------------------------------------------------------------------
# stop_workspace: no terminal runtime-release event when one already exists
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_stop_workspace_skips_runtime_release_when_already_released(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.cancelled)
    repo = WorkspaceRepository(session)
    # A prior terminal runtime-release event already proved the runtime stopped.
    await repo.add_event(
        workspace,
        event_type=TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
        reason_code="TERMINAL_RUNTIME_RELEASED",
        payload={"compose_project_name": workspace.compose_project_name},
    )
    await session.flush()
    cleaner = RecordingCleaner(result=compose_down_succeeded_result())
    service, stopper, _cleaner = _service(session, cleaner=cleaner)

    response = await service.stop_workspace(workspace.id, reason="re-stop already cancelled")
    release_events = [
        event
        for event in await _events(session, workspace.id)
        if event.event_type == TERMINAL_RUNTIME_RELEASE_EVENT_TYPE
    ]

    # The full compose down still runs via the cleaner, but the release event is
    # not duplicated since one already proved the runtime stopped.
    assert response.status == WorkspaceStatus.cancelled
    assert stopper.calls == []
    assert len(cleaner.calls) == 1
    assert len(release_events) == 1


# ---------------------------------------------------------------------------
# refresh: active-coalesce replay rejected once destruction has begun
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_refresh_active_coalesce_replay_rejected_after_destroying(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.ready)
    service, _stopper, _cleaner = _service(session)

    operation = await service.request_refresh_workspace(
        workspace.id,
        reason="stale merge queue",
    )
    # Destruction begins after the refresh op is enqueued; a fresh (keyless)
    # request coalesces onto the active op but must observe the new state.
    workspace.status = WorkspaceStatus.destroying.value
    await session.flush()

    with pytest.raises(WorkspaceRefreshStateError) as exc_info:
        await service.request_refresh_workspace(
            workspace.id,
            reason="stale merge queue",
        )

    assert exc_info.value.detail == {"status": WorkspaceStatus.destroying.value}
    assert [row.id for row in await _operations(session, workspace.id)] == [operation.id]


# ---------------------------------------------------------------------------
# remonitor: failed workspace with an attempt but no merge candidate
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_remonitor_failed_workspace_without_candidate_resets_without_reopen(
    session: AsyncSession,
) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.failed)
    workspace.pr_url = "https://github.com/example/control-lifecycle/pull/77"
    workspace.pr_number = 77
    workspace.failure_reason = "infrastructure_failure"
    workspace.failure_message = "monitor process died"
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
    # An attempt exists, but no merge candidate was ever opened for it.
    await TaskAttemptRepository(session).create_for_workspace(task=task, workspace=workspace)
    await session.flush()
    service, _stopper, _cleaner = _service(session)

    response = await service.remonitor_workspace(
        workspace.id,
        reason="reattach failed monitor",
        expected_version=workspace.version,
    )
    events = await _events(session, workspace.id)
    remonitor_event = next(
        event for event in events if event.event_type == "workspace.remonitor_requested"
    )

    assert response.status == WorkspaceStatus.monitoring_pr
    assert workspace.status == WorkspaceStatus.monitoring_pr.value
    state_reset = remonitor_event.payload["state_reset"]
    assert state_reset["from"] == WorkspaceStatus.failed.value
    assert state_reset["to"] == WorkspaceStatus.monitoring_pr.value
    # No open candidate existed, so the reset records that nothing was reopened.
    assert state_reset["candidate_reopened"] is False
    candidate_ops = await OperationRepository(session).list_for_workspace(workspace.id, limit=20)
    assert any(op.type == OperationType.remonitor.value for op in candidate_ops)
