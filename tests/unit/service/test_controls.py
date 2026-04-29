from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from awf.common.config import get_settings
from awf.db.enums import OperationStatus, OperationType, WorkspaceStatus
from awf.db.repositories import OperationRepository, WorkspaceEventRepository, WorkspaceRepository
from awf.db.session import make_session_factory
from awf.service import controls
from awf.service.controls import WorkspaceStackStopError, stop_project_containers


def _mock_proc(returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> AsyncMock:
    proc = AsyncMock()
    proc.returncode = returncode
    proc.communicate.return_value = (stdout, stderr)
    return proc


@pytest.mark.unit
def test_idempotency_identity_matching_ignores_identity_keys_absent_from_identity() -> None:
    assert controls._payload_matches_idempotency_identity(
        {
            "reason": "base branch advanced",
            "reason_code": "operator_rebase_requested",
            "expected_version": 7,
        },
        identity={"reason": "base branch advanced"},
        identity_keys=frozenset({"reason", "reason_code", "expected_version"}),
    )
    assert not controls._payload_matches_idempotency_identity(
        {"reason_code": "operator_rebase_requested", "expected_version": 7},
        identity={"reason": "base branch advanced"},
        identity_keys=frozenset({"reason", "reason_code", "expected_version"}),
    )


@pytest.mark.unit
async def test_stop_project_containers_is_noop_without_project_name() -> None:
    with patch("awf.service.controls.asyncio.create_subprocess_exec") as mock_exec:
        await stop_project_containers(None)

    mock_exec.assert_not_called()


@pytest.mark.unit
async def test_stop_project_containers_returns_when_no_containers_match() -> None:
    with patch(
        "awf.service.controls.asyncio.create_subprocess_exec",
        return_value=_mock_proc(),
    ) as mock_exec:
        await stop_project_containers("awf_ws_empty")

    assert mock_exec.call_count == 1


@pytest.mark.unit
async def test_stop_project_containers_stops_matching_container_ids() -> None:
    with patch(
        "awf.service.controls.asyncio.create_subprocess_exec",
        side_effect=[
            _mock_proc(stdout=b"abc123\n def456 \n"),
            _mock_proc(stdout=b"abc123\ndef456\n"),
        ],
    ) as mock_exec:
        await stop_project_containers("awf_ws_running")

    assert mock_exec.call_count == 2
    assert mock_exec.call_args_list[1].args[:4] == ("docker", "stop", "abc123", "def456")


@pytest.mark.unit
async def test_stop_project_containers_raises_when_ps_fails() -> None:
    with (
        patch(
            "awf.service.controls.asyncio.create_subprocess_exec",
            return_value=_mock_proc(returncode=1, stderr=b"daemon unavailable"),
        ),
        pytest.raises(WorkspaceStackStopError) as exc_info,
    ):
        await stop_project_containers("awf_ws_fail")

    assert exc_info.value.error_code == "STACK_STOP_FAILED"
    assert exc_info.value.operation == "ps"
    assert exc_info.value.returncode == 1
    assert "daemon unavailable" in exc_info.value.message


@pytest.mark.unit
async def test_stop_project_containers_raises_when_stop_fails() -> None:
    with (
        patch(
            "awf.service.controls.asyncio.create_subprocess_exec",
            side_effect=[
                _mock_proc(stdout=b"abc123\n"),
                _mock_proc(returncode=17, stderr=b"permission denied"),
            ],
        ),
        pytest.raises(WorkspaceStackStopError) as exc_info,
    ):
        await stop_project_containers("awf_ws_fail")

    assert exc_info.value.error_code == "STACK_STOP_FAILED"
    assert exc_info.value.operation == "stop"
    assert exc_info.value.returncode == 17
    assert "permission denied" in exc_info.value.message


@pytest.mark.unit
async def test_cancel_workspace_stops_stack_transitions_and_replays_operation(
    engine: AsyncEngine,
) -> None:
    stopper = _RecordingStopper()
    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await _create_control_workspace(
            session,
            status=WorkspaceStatus.running,
            compose_project_name="awf_ws_cancel",
        )
        service = controls.WorkspaceControlService(
            session,
            project_stopper=stopper,
            cleaner_factory=lambda: _RecordingCleaner(),
        )

        response = await service.cancel_workspace(
            workspace.id,
            reason="operator cancelled",
            stop_stack=True,
            idempotency_key="cancel-once",
        )
        replay = await service.cancel_workspace(
            workspace.id,
            reason="operator cancelled",
            stop_stack=True,
            idempotency_key="cancel-once",
        )

    assert response.workspace_id == workspace.id
    assert response.operation_id == replay.operation_id
    assert response.operation_status == OperationStatus.succeeded.value
    assert replay.operation_status == OperationStatus.succeeded.value
    assert response.status == WorkspaceStatus.cancelled
    assert stopper.calls == ["awf_ws_cancel"]


@pytest.mark.unit
async def test_cancel_workspace_records_event_when_already_cancelled(
    engine: AsyncEngine,
) -> None:
    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await _create_control_workspace(session, status=WorkspaceStatus.cancelled)
        service = controls.WorkspaceControlService(
            session,
            project_stopper=_RecordingStopper(),
            cleaner_factory=lambda: _RecordingCleaner(),
        )

        response = await service.cancel_workspace(
            workspace.id,
            reason="second cancel",
            stop_stack=False,
            idempotency_key="cancel-again",
        )
        events = await WorkspaceEventRepository(session).list(workspace_id=workspace.id)

    assert response.status == WorkspaceStatus.cancelled
    assert response.operation_status == OperationStatus.succeeded.value
    assert events[0].event_type == "workspace.cancel_requested"
    assert events[0].payload == {"reason": "second cancel", "stop_stack": False}


@pytest.mark.unit
async def test_stop_workspace_transitions_active_workspace_and_replays(
    engine: AsyncEngine,
) -> None:
    stopper = _RecordingStopper()
    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await _create_control_workspace(
            session,
            status=WorkspaceStatus.ready,
            compose_project_name="awf_ws_stop",
        )
        service = controls.WorkspaceControlService(
            session,
            project_stopper=stopper,
            cleaner_factory=lambda: _RecordingCleaner(),
        )

        response = await service.stop_workspace(
            workspace.id,
            reason="pause it",
            idempotency_key="stop-once",
        )
        replay = await service.stop_workspace(
            workspace.id,
            reason="pause it",
            idempotency_key="stop-once",
        )

    assert response.status == WorkspaceStatus.cancelled
    assert replay.operation_id == response.operation_id
    assert response.operation_status == OperationStatus.succeeded.value
    assert replay.operation_status == OperationStatus.succeeded.value
    assert stopper.calls == ["awf_ws_stop"]


@pytest.mark.unit
async def test_stop_workspace_records_event_for_inactive_workspace(
    engine: AsyncEngine,
) -> None:
    stopper = _RecordingStopper()
    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await _create_control_workspace(
            session,
            status=WorkspaceStatus.completed,
            compose_project_name="awf_ws_completed",
        )
        service = controls.WorkspaceControlService(
            session,
            project_stopper=stopper,
            cleaner_factory=lambda: _RecordingCleaner(),
        )

        response = await service.stop_workspace(
            workspace.id,
            reason="containers only",
            idempotency_key="stop-completed",
        )
        events = await WorkspaceEventRepository(session).list(workspace_id=workspace.id)

    assert response.status == WorkspaceStatus.completed
    assert response.operation_status == OperationStatus.succeeded.value
    assert stopper.calls == ["awf_ws_completed"]
    assert events[0].event_type == "workspace.stack_stopped"


@pytest.mark.unit
async def test_remonitor_workspace_resets_claims_records_snapshot_and_replays(
    engine: AsyncEngine,
) -> None:
    now = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await _create_control_workspace(
            session,
            status=WorkspaceStatus.monitoring_pr,
            pr_url="https://github.com/example/app/pull/7",
        )
        workspace.monitor_claimed_by = "monitor-worker"
        workspace.monitor_claim_expires_at = now + timedelta(minutes=5)
        workspace.execution_claimed_by = "executor-worker"
        workspace.execution_claim_expires_at = now + timedelta(minutes=10)
        service = controls.WorkspaceControlService(
            session,
            project_stopper=_RecordingStopper(),
            cleaner_factory=lambda: _RecordingCleaner(),
        )

        response = await service.remonitor_workspace(
            workspace.id,
            reason="lost monitor",
            idempotency_key="remonitor-once",
        )
        replay = await service.remonitor_workspace(
            workspace.id,
            reason="lost monitor",
            idempotency_key="remonitor-once",
        )
        events = await WorkspaceEventRepository(session).list(workspace_id=workspace.id)

    assert response.status == WorkspaceStatus.monitoring_pr
    assert replay.operation_id == response.operation_id
    assert response.operation_status == OperationStatus.succeeded.value
    assert replay.operation_status == OperationStatus.succeeded.value
    assert workspace.monitor_claimed_by is None
    assert workspace.monitor_claim_expires_at is None
    assert workspace.execution_claimed_by is None
    assert workspace.execution_claim_expires_at is None
    assert events[0].event_type == "workspace.remonitor_requested"
    assert events[0].payload["claims_reset"]["monitor_claimed_by"] == "monitor-worker"
    assert events[0].payload["claims_reset"]["execution_claimed_by"] == "executor-worker"


@pytest.mark.unit
async def test_remonitor_workspace_rejects_wrong_state_and_missing_pr_url(
    engine: AsyncEngine,
) -> None:
    factory = make_session_factory(engine)
    async with factory() as session:
        requested = await _create_control_workspace(session, status=WorkspaceStatus.requested)
        no_pr = await _create_control_workspace(session, status=WorkspaceStatus.monitoring_pr)
        service = controls.WorkspaceControlService(
            session,
            project_stopper=_RecordingStopper(),
            cleaner_factory=lambda: _RecordingCleaner(),
        )

        with pytest.raises(controls.WorkspaceRemonitorStateError) as state_error:
            await service.remonitor_workspace(
                requested.id,
                reason=None,
                idempotency_key="remonitor-requested",
            )
        with pytest.raises(controls.WorkspaceRemonitorMissingPrUrlError) as pr_error:
            await service.remonitor_workspace(
                no_pr.id,
                reason=None,
                idempotency_key="remonitor-no-pr",
            )

    assert state_error.value.detail == {
        "status": WorkspaceStatus.requested.value,
        "eligible_statuses": [
            WorkspaceStatus.monitoring_pr.value,
            WorkspaceStatus.failed.value,
        ],
    }
    assert pr_error.value.detail == {"status": WorkspaceStatus.monitoring_pr.value}


@pytest.mark.unit
async def test_destroy_workspace_rejects_active_without_force_and_replays_destroyed(
    engine: AsyncEngine,
) -> None:
    factory = make_session_factory(engine)
    async with factory() as session:
        active = await _create_control_workspace(session, status=WorkspaceStatus.running)
        destroyed = await _create_control_workspace(session, status=WorkspaceStatus.destroyed)
        service = controls.WorkspaceControlService(
            session,
            project_stopper=_RecordingStopper(),
            cleaner_factory=lambda: _RecordingCleaner(),
        )

        with pytest.raises(controls.ActiveWorkspaceDestroyError):
            await service.destroy_workspace(
                active.id,
                force=False,
                remove_volumes=True,
                remove_worktree=True,
                idempotency_key="destroy-active",
            )
        first = await service.destroy_workspace(
            destroyed.id,
            force=False,
            remove_volumes=True,
            remove_worktree=True,
            idempotency_key="destroyed-once",
        )
        replay = await service.destroy_workspace(
            destroyed.id,
            force=False,
            remove_volumes=True,
            remove_worktree=True,
            idempotency_key="destroyed-once",
        )

    assert first.status == WorkspaceStatus.destroyed
    assert first.message == "workspace already destroyed"
    assert replay.operation_id == first.operation_id
    assert first.operation_status == OperationStatus.succeeded.value
    assert replay.operation_status == OperationStatus.succeeded.value


@pytest.mark.unit
async def test_destroy_workspace_force_cleans_resources_and_marks_destroyed(
    engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    cleaner = _RecordingCleaner()
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await _create_control_workspace(
            session,
            status=WorkspaceStatus.ready,
            compose_project_name="awf_ws_destroy",
            compose_file_path=str(compose_file),
        )
        service = controls.WorkspaceControlService(
            session,
            project_stopper=_RecordingStopper(),
            cleaner_factory=lambda: cleaner,
        )

        response = await service.destroy_workspace(
            workspace.id,
            force=True,
            remove_volumes=False,
            remove_worktree=True,
            idempotency_key="destroy-ready",
        )

    assert response.status == WorkspaceStatus.destroyed
    assert response.message == "workspace destroyed"
    assert response.operation_status == OperationStatus.succeeded.value
    assert cleaner.calls == [
        {
            "workspace_id": workspace.id,
            "repo_url": workspace.repo_url,
            "compose_project_name": "awf_ws_destroy",
            "compose_file_path": compose_file,
            "worktree_host_path": None,
            "remove_volumes": False,
            "remove_worktree": True,
        }
    ]


@pytest.mark.unit
async def test_destroy_workspace_records_cleanup_failures(
    engine: AsyncEngine,
) -> None:
    cleaner = _RecordingCleaner(failures=["volume busy", "worktree busy"])
    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await _create_control_workspace(session, status=WorkspaceStatus.failed)
        service = controls.WorkspaceControlService(
            session,
            project_stopper=_RecordingStopper(),
            cleaner_factory=lambda: cleaner,
        )

        response = await service.destroy_workspace(
            workspace.id,
            force=False,
            remove_volumes=True,
            remove_worktree=False,
            idempotency_key="destroy-failed-cleanup",
        )
        operations = await OperationRepository(session).list_for_workspace(
            workspace.id,
            operation_type=OperationType.destroy,
        )

    assert response.status == WorkspaceStatus.failed
    assert response.message == "workspace cleanup failed"
    assert response.operation_status == OperationStatus.failed.value
    assert workspace.failure_reason == "cleanup_failure"
    assert workspace.failure_message == "volume busy, worktree busy"
    assert operations[0].status == OperationStatus.failed.value
    assert operations[0].error_code == "CLEANUP_FAILED"


@pytest.mark.unit
async def test_destroy_workspace_records_structured_partial_cleanup_and_retry(
    engine: AsyncEngine,
) -> None:
    partial_cleanup = {
        "status": "partial",
        "reason_code": "CLEANUP_PARTIAL",
        "steps": [
            {
                "name": "compose_down",
                "status": "failed",
                "reason_code": "COMPOSE_COMMAND_FAILED",
                "error": "network still in use",
            },
            {
                "name": "worktree_remove",
                "status": "succeeded",
                "reason_code": "WORKTREE_REMOVE_SUCCEEDED",
            },
        ],
        "failed_steps": [
            {
                "name": "compose_down",
                "status": "failed",
                "reason_code": "COMPOSE_COMMAND_FAILED",
                "error": "network still in use",
            }
        ],
        "completed_steps": [
            {
                "name": "worktree_remove",
                "status": "succeeded",
                "reason_code": "WORKTREE_REMOVE_SUCCEEDED",
            }
        ],
    }
    successful_cleanup = {
        "status": "succeeded",
        "reason_code": "CLEANUP_SUCCEEDED",
        "steps": [
            {
                "name": "compose_down",
                "status": "succeeded",
                "reason_code": "COMPOSE_DOWN_SUCCEEDED",
            },
            {
                "name": "worktree_remove",
                "status": "succeeded",
                "reason_code": "WORKTREE_REMOVE_SUCCEEDED",
            },
        ],
        "failed_steps": [],
        "completed_steps": [
            {
                "name": "compose_down",
                "status": "succeeded",
                "reason_code": "COMPOSE_DOWN_SUCCEEDED",
            },
            {
                "name": "worktree_remove",
                "status": "succeeded",
                "reason_code": "WORKTREE_REMOVE_SUCCEEDED",
            },
        ],
    }
    cleaner = _SequencedCleaner([partial_cleanup, successful_cleanup])
    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await _create_control_workspace(session, status=WorkspaceStatus.failed)
        service = controls.WorkspaceControlService(
            session,
            project_stopper=_RecordingStopper(),
            cleaner_factory=lambda: cleaner,
        )

        failed_response = await service.destroy_workspace(
            workspace.id,
            force=False,
            remove_volumes=True,
            remove_worktree=True,
            idempotency_key="destroy-partial",
        )
        failed_events = await WorkspaceEventRepository(session).list(workspace_id=workspace.id)
        failed_operations = await OperationRepository(session).list_for_workspace(
            workspace.id,
            operation_type=OperationType.destroy,
        )

        retry_response = await service.destroy_workspace(
            workspace.id,
            force=False,
            remove_volumes=True,
            remove_worktree=True,
            idempotency_key="destroy-retry",
        )
        retry_events = await WorkspaceEventRepository(session).list(workspace_id=workspace.id)
        retry_operations = await OperationRepository(session).list_for_workspace(
            workspace.id,
            operation_type=OperationType.destroy,
        )

    assert failed_response.status == WorkspaceStatus.failed
    failed_operation = failed_operations[0]
    assert failed_operation.status == OperationStatus.failed.value
    assert failed_operation.error_code == "CLEANUP_FAILED"
    assert failed_operation.error_message == "network still in use"
    assert failed_operation.result == {
        "status": WorkspaceStatus.failed.value,
        "cleanup": partial_cleanup,
    }
    cleanup_event = next(event for event in failed_events if event.reason_code == "CLEANUP_FAILED")
    assert cleanup_event.payload is not None
    assert cleanup_event.payload["cleanup"] == partial_cleanup

    assert retry_response.status == WorkspaceStatus.destroyed
    retry_operation = retry_operations[0]
    assert retry_operation.status == OperationStatus.succeeded.value
    assert retry_operation.result == {
        "status": WorkspaceStatus.destroyed.value,
        "cleanup": successful_cleanup,
    }
    retry_cleanup_event = next(event for event in retry_events if event.reason_code == "DESTROYED")
    assert retry_cleanup_event.payload is not None
    assert retry_cleanup_event.payload["cleanup"] == retry_operation.result["cleanup"]
    assert len(cleaner.calls) == 2


@pytest.mark.unit
async def test_destroy_already_destroyed_workspace_records_skipped_cleanup_detail(
    engine: AsyncEngine,
) -> None:
    cleaner = _RecordingCleaner()
    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await _create_control_workspace(session, status=WorkspaceStatus.destroyed)
        service = controls.WorkspaceControlService(
            session,
            project_stopper=_RecordingStopper(),
            cleaner_factory=lambda: cleaner,
        )

        response = await service.destroy_workspace(
            workspace.id,
            force=False,
            remove_volumes=True,
            remove_worktree=True,
            idempotency_key="destroy-already-clean",
        )
        operations = await OperationRepository(session).list_for_workspace(
            workspace.id,
            operation_type=OperationType.destroy,
        )

    assert response.status == WorkspaceStatus.destroyed
    assert response.message == "workspace already destroyed"
    assert cleaner.calls == []
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


@pytest.mark.unit
async def test_validate_exact_idempotency_replay_survives_terminal_workspace_movement(
    engine: AsyncEngine,
) -> None:
    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await _create_control_workspace(
            session,
            status=WorkspaceStatus.monitoring_pr,
            pr_url="https://github.com/example/controls/pull/7",
        )
        service = controls.WorkspaceControlService(
            session,
            project_stopper=_RecordingStopper(),
            cleaner_factory=lambda: _RecordingCleaner(),
        )

        operation = await service.request_validate_workspace(
            workspace.id,
            reason="rerun required validation",
            requested_tier=2,
            idempotency_key="validate-terminal-replay",
        )
        await OperationRepository(session).finish(
            operation,
            status=OperationStatus.succeeded,
            result={"status": WorkspaceStatus.ready.value},
        )
        workspace.status = WorkspaceStatus.completed.value
        await session.flush()
        before_operation_ids = [
            row.id
            for row in await OperationRepository(session).list_for_workspace(
                workspace.id,
                operation_type=OperationType.validate,
            )
        ]
        before_event_ids = [
            row.id for row in await WorkspaceEventRepository(session).list(workspace_id=workspace.id)
        ]

        replay = await service.request_validate_workspace(
            workspace.id,
            reason="rerun required validation",
            requested_tier=2,
            idempotency_key="validate-terminal-replay",
        )
        with pytest.raises(controls.WorkspaceValidateStateError) as fresh_error:
            await service.request_validate_workspace(
                workspace.id,
                reason="rerun required validation",
                requested_tier=2,
                idempotency_key="validate-fresh-terminal",
            )

        after_operation_ids = [
            row.id
            for row in await OperationRepository(session).list_for_workspace(
                workspace.id,
                operation_type=OperationType.validate,
            )
        ]
        after_event_ids = [
            row.id for row in await WorkspaceEventRepository(session).list(workspace_id=workspace.id)
        ]

    assert replay.id == operation.id
    assert replay.status == OperationStatus.succeeded.value
    assert fresh_error.value.detail == {
        "status": WorkspaceStatus.completed.value,
        "eligible_statuses": [WorkspaceStatus.monitoring_pr.value],
    }
    assert after_operation_ids == before_operation_ids
    assert after_event_ids == before_event_ids


@pytest.mark.unit
async def test_control_service_rejects_idempotency_payload_and_version_conflicts(
    engine: AsyncEngine,
) -> None:
    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await _create_control_workspace(session, status=WorkspaceStatus.requested)
        service = controls.WorkspaceControlService(
            session,
            project_stopper=_RecordingStopper(),
            cleaner_factory=lambda: _RecordingCleaner(),
        )

        await service.cancel_workspace(
            workspace.id,
            reason="same key",
            stop_stack=False,
            idempotency_key="control-conflict",
        )
        with pytest.raises(controls.IdempotencyConflictError):
            await service.stop_workspace(
                workspace.id,
                reason="same key",
                idempotency_key="control-conflict",
            )
        with pytest.raises(controls.VersionConflictError) as version_error:
            await service.destroy_workspace(
                workspace.id,
                force=True,
                remove_volumes=True,
                remove_worktree=True,
                idempotency_key="version-conflict",
                expected_version=workspace.version + 1,
            )

    assert version_error.value.detail == {
        "expected_version": 3,
        "actual_version": 2,
    }


@pytest.mark.unit
async def test_communicate_reports_no_output_failure() -> None:
    proc = _mock_proc(returncode=2)

    with pytest.raises(WorkspaceStackStopError) as exc_info:
        await controls._communicate(proc, operation="stop")  # noqa: SLF001

    assert exc_info.value.message == "docker stop failed (exit=2): <no output>"
    assert exc_info.value.stdout == ""
    assert exc_info.value.stderr == ""


@pytest.mark.unit
def test_default_cleaner_uses_configured_work_dir(tmp_path: Path) -> None:
    previous = os.environ.get("AWF_WORK_DIR")
    try:
        os.environ["AWF_WORK_DIR"] = str(tmp_path)
        get_settings.cache_clear()

        cleaner = controls.default_cleaner()

        assert cleaner._git._work_dir == tmp_path / "git"  # noqa: SLF001
        assert cleaner._compose._projects_dir == tmp_path / "compose"  # noqa: SLF001
    finally:
        if previous is None:
            os.environ.pop("AWF_WORK_DIR", None)
        else:
            os.environ["AWF_WORK_DIR"] = previous
        get_settings.cache_clear()


async def _create_control_workspace(
    session: AsyncSession,
    *,
    status: WorkspaceStatus,
    compose_project_name: str | None = None,
    compose_file_path: str | None = None,
    pr_url: str | None = None,
) -> object:
    repo = WorkspaceRepository(session)
    workspace = await repo.create(
        repo_url="git@github.com:example/controls.git",
        branch_base="main",
        task_title=f"Control {status.value}",
        task_prompt="Exercise workspace control behavior.",
        agent="codex",
        test_commands=["pytest -q"],
    )
    workspace.status = status.value
    workspace.compose_project_name = compose_project_name
    workspace.compose_file_path = compose_file_path
    workspace.pr_url = pr_url
    await session.flush()
    return workspace


class _RecordingStopper:
    def __init__(self) -> None:
        self.calls: list[str | None] = []

    async def __call__(self, compose_project_name: str | None) -> None:
        self.calls.append(compose_project_name)


class _RecordingCleaner:
    def __init__(self, failures: Sequence[str] = ()) -> None:
        self.failures = list(failures)
        self.calls: list[dict[str, object]] = []

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
            {
                "workspace_id": workspace_id,
                "repo_url": repo_url,
                "compose_project_name": compose_project_name,
                "compose_file_path": compose_file_path,
                "worktree_host_path": worktree_host_path,
                "remove_volumes": remove_volumes,
                "remove_worktree": remove_worktree,
            }
        )
        return list(self.failures)


class _SequencedCleaner:
    def __init__(self, results: Sequence[Mapping[str, object]]) -> None:
        self.results = list(results)
        self.calls: list[dict[str, object]] = []

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
    ) -> Mapping[str, object]:
        self.calls.append(
            {
                "workspace_id": workspace_id,
                "repo_url": repo_url,
                "compose_project_name": compose_project_name,
                "compose_file_path": compose_file_path,
                "worktree_host_path": worktree_host_path,
                "remove_volumes": remove_volumes,
                "remove_worktree": remove_worktree,
            }
        )
        return self.results.pop(0)
