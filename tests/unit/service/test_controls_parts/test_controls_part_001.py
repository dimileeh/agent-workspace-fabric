from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from awf.db.enums import FailureReason, OperationStatus, OperationType, WorkspaceStatus
from awf.db.repositories import (
    OperationRepository,
    ValidationRunRepository,
    WorkspaceEventRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.service import controls
from awf.service.controls import WorkspaceStackStopError, stop_project_containers
from awf.service.failure_causality import load_failure_causality_snapshot


def _mock_proc(returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> AsyncMock:
    proc = AsyncMock()
    proc.returncode = returncode
    proc.communicate.return_value = (stdout, stderr)
    return proc


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


async def _seed_validation_failure_evidence(
    session: AsyncSession,
    workspace: object,
    *,
    failure_message: str,
) -> str:
    workspace.failure_reason = FailureReason.validation_failure.value
    workspace.failure_message = failure_message
    repo = WorkspaceRepository(session)
    event = await repo.add_event(
        workspace,
        event_type="workspace.state_changed",
        reason_code="PYTEST_TEST_FAILURE",
        payload={
            "reason_code": "PYTEST_TEST_FAILURE",
            "message": failure_message,
            "details": {
                "recommended_action": "fix tests before cleanup recovery",
                "recovery_strategy": "retry_after_fix",
            },
        },
    )
    event.new_state = WorkspaceStatus.failed.value
    validation_repo = ValidationRunRepository(session)
    run = await validation_repo.start(
        workspace_id=workspace.id,
        attempt_id=None,
        tier=0,
        commands=[
            {
                "command": "uv run pytest tests/unit/test_controls.py::test_destroy_cleanup",
                "phase": "validation",
            }
        ],
        base_commit="a" * 40,
        target_branch="main",
        target_head_sha="b" * 40,
        workspace_head_sha="c" * 40,
        log_stream_refs={"validation": "logs/control-validation.log"},
    )
    await validation_repo.finish(
        run.id,
        status="failed",
        reason_code="PYTEST_TEST_FAILURE",
        coverage={
            "percent": 94.0,
            "minimum_percent": 99.0,
            "threshold": 99.0,
            "failing_test_node_ids": [
                "tests/unit/test_controls.py::test_destroy_cleanup",
            ],
        },
    )
    await session.flush()
    return run.id


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
        companion_worktrees: tuple[tuple[str, str], ...] = (),
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
                "companion_worktrees": companion_worktrees,
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
        companion_worktrees: tuple[tuple[str, str], ...] = (),
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
                "companion_worktrees": companion_worktrees,
                "compose_project_name": compose_project_name,
                "compose_file_path": compose_file_path,
                "worktree_host_path": worktree_host_path,
                "remove_volumes": remove_volumes,
                "remove_worktree": remove_worktree,
            }
        )
        return self.results.pop(0)


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
def test_idempotency_identity_matching_accepts_missing_identity_and_rejects_non_mapping() -> None:
    assert controls._payload_matches_idempotency_identity(
        "not-a-payload",
        identity=None,
        identity_keys=frozenset({"reason"}),
    )
    assert not controls._payload_matches_idempotency_identity(
        "not-a-payload",
        identity={"reason": "operator requested"},
        identity_keys=None,
    )


@pytest.mark.unit
def test_pr_monitor_recovery_operation_rejects_non_mapping_payload() -> None:
    operation = SimpleNamespace(type=OperationType.validate.value, payload=["not", "mapping"])

    assert not controls._is_pr_monitor_recovery_operation(operation)  # noqa: SLF001


@pytest.mark.unit
def test_cleanup_result_normalization_preserves_structured_result() -> None:
    cleanup = controls.WorkspaceCleanupResult(
        status="succeeded",
        reason_code="CLEANUP_SUCCEEDED",
    )

    assert controls._normalize_cleanup_result(cleanup) is cleanup


@pytest.mark.unit
def test_cleanup_result_mapping_falls_back_to_unknown_legacy_step_lists() -> None:
    cleanup = controls._normalize_cleanup_result(
        {
            "status": "unknown",
            "completed_steps": [
                {
                    "name": "compose_down",
                    "status": "succeeded",
                    "reason_code": "COMPOSE_DOWN_SUCCEEDED",
                },
                "ignored legacy entry",
            ],
            "failed_steps": [
                {
                    "status": "unknown",
                    "error": "",
                },
            ],
        }
    )

    assert cleanup.status == "partial"
    assert cleanup.reason_code == "CLEANUP_PARTIAL"
    assert [step.name for step in cleanup.steps] == ["compose_down", "cleanup_step_3"]
    assert [step.status for step in cleanup.steps] == ["succeeded", "failed"]
    assert cleanup.steps[1].reason_code == "CLEANUP_STEP_FAILED"
    assert cleanup.steps[1].error is None


@pytest.mark.unit
def test_normalize_cleanup_result_accepts_legacy_step_lists() -> None:
    result = controls._normalize_cleanup_result(
        {
            "status": "succeeded",
            "steps": "legacy",
            "completed_steps": [
                {"name": "", "status": "succeeded", "reason_code": "", "error": ""},
                "ignored",
            ],
            "failed_steps": [
                {"status": "unknown"},
            ],
        }
    )

    assert result.status == "succeeded"
    assert result.reason_code == "CLEANUP_SUCCEEDED"
    assert [step.to_dict() for step in result.steps] == [
        {
            "name": "cleanup_step_1",
            "status": "succeeded",
            "reason_code": "CLEANUP_STEP_SUCCEEDED",
        },
        {
            "name": "cleanup_step_3",
            "status": "failed",
            "reason_code": "CLEANUP_STEP_FAILED",
        },
    ]
    assert controls._cleanup_failure_message(result) == "cleanup_step_3"


@pytest.mark.unit
def test_normalize_cleanup_result_handles_sequence_and_empty_cleanup_shapes() -> None:
    existing = controls.WorkspaceCleanupResult.skipped(reason_code="NOTHING_TO_CLEAN")
    failed = controls._normalize_cleanup_result(["compose down failed"])
    empty = controls._normalize_cleanup_result([])
    skipped = controls._normalize_cleanup_result({"status": "skipped", "steps": []})
    partial = controls._normalize_cleanup_result({"status": "unexpected", "steps": []})

    assert controls._normalize_cleanup_result(existing) is existing
    assert failed.status == "partial"
    assert failed.reason_code == "CLEANUP_PARTIAL"
    assert failed.failure_messages == ("compose down failed",)
    assert empty.status == "succeeded"
    assert empty.reason_code == "CLEANUP_SUCCEEDED"
    assert controls._cleanup_failure_message(skipped) == "CLEANUP_SKIPPED"
    assert controls._cleanup_failure_message(partial) == "CLEANUP_PARTIAL"


@pytest.mark.unit
def test_cleanup_reason_code_defaults_for_terminal_statuses() -> None:
    assert controls._cleanup_reason_code("", status="succeeded") == "CLEANUP_SUCCEEDED"
    assert controls._cleanup_reason_code("", status="skipped") == "CLEANUP_SKIPPED"
    assert controls._cleanup_reason_code("", status="partial") == "CLEANUP_PARTIAL"


@pytest.mark.unit
def test_cleanup_failure_message_falls_back_to_result_reason_code() -> None:
    cleanup = controls.WorkspaceCleanupResult(
        status="partial",
        reason_code="CLEANUP_FAILED_WITHOUT_STEP_DETAIL",
    )

    assert controls._cleanup_failure_message(cleanup) == "CLEANUP_FAILED_WITHOUT_STEP_DETAIL"


@pytest.mark.unit
async def test_stop_project_containers_is_noop_without_project_name() -> None:
    with patch("awf.service.controls_helpers.asyncio.create_subprocess_exec") as mock_exec:
        await stop_project_containers(None)

    mock_exec.assert_not_called()


@pytest.mark.unit
async def test_stop_project_containers_returns_when_no_containers_match() -> None:
    with patch(
        "awf.service.controls_helpers.asyncio.create_subprocess_exec",
        return_value=_mock_proc(),
    ) as mock_exec:
        await stop_project_containers("awf_ws_empty")

    assert mock_exec.call_count == 1


@pytest.mark.unit
async def test_stop_project_containers_stops_matching_container_ids() -> None:
    with patch(
        "awf.service.controls_helpers.asyncio.create_subprocess_exec",
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
            "awf.service.controls_helpers.asyncio.create_subprocess_exec",
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
            "awf.service.controls_helpers.asyncio.create_subprocess_exec",
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
    cancel_event = next(
        event for event in events if event.event_type == "workspace.cancel_requested"
    )
    assert cancel_event.payload == {"reason": "second cancel", "stop_stack": False}


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
    assert any(event.event_type == "workspace.stack_stopped" for event in events)


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
        workspace.task_policy = {
            "companions": [
                {
                    "name": "backend",
                    "repo_url": "git@github.com:example/backend.git",
                }
            ]
        }
        await session.flush()
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
            "companion_worktrees": (
                (
                    f"{workspace.id}__companion__backend",
                    "git@github.com:example/backend.git",
                ),
            ),
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
async def test_destroy_cleanup_failure_without_primary_evidence_records_secondary_event(
    engine: AsyncEngine,
) -> None:
    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await _create_control_workspace(session, status=WorkspaceStatus.failed)

        class _FailingWithoutPrimaryEvidenceCleaner(_RecordingCleaner):
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
                failures = await super().cleanup(
                    workspace_id=workspace_id,
                    repo_url=repo_url,
                    companion_worktrees=companion_worktrees,
                    compose_project_name=compose_project_name,
                    compose_file_path=compose_file_path,
                    worktree_host_path=worktree_host_path,
                    remove_volumes=remove_volumes,
                    remove_worktree=remove_worktree,
                )
                await WorkspaceRepository(session).transition(
                    workspace,
                    to=WorkspaceStatus.failed,
                    reason_code="RUNTIME_FAILED_WITHOUT_PRIMARY",
                    payload={
                        "reason_code": "RUNTIME_FAILED_WITHOUT_PRIMARY",
                        "message": "runtime failed without durable row evidence",
                    },
                )
                await session.flush()
                return failures

        cleaner = _FailingWithoutPrimaryEvidenceCleaner(failures=["volume busy"])
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
            idempotency_key="destroy-failed-cleanup-without-primary",
        )
        operations = await OperationRepository(session).list_for_workspace(
            workspace.id,
            operation_type=OperationType.destroy,
        )
        events = await WorkspaceEventRepository(session).list(workspace_id=workspace.id)
        snapshot = await load_failure_causality_snapshot(session, workspace)

    secondary_failure_events = [
        event for event in events if event.event_type == "workspace.secondary_failure_recorded"
    ]
    assert len(secondary_failure_events) == 1
    secondary_failure_event = secondary_failure_events[0]
    assert secondary_failure_event.old_state == WorkspaceStatus.failed.value
    assert secondary_failure_event.new_state == WorkspaceStatus.failed.value
    assert secondary_failure_event.reason_code == "CLEANUP_FAILED"
    assert secondary_failure_event.payload is not None
    assert secondary_failure_event.payload["synthetic"] is True
    assert "primary_failure" not in secondary_failure_event.payload
    assert secondary_failure_event.payload["secondary_failure"]["reason_code"] == "CLEANUP_FAILED"
    assert secondary_failure_event.payload["secondary_failure"]["message"] == "volume busy"
    assert secondary_failure_event.payload["secondary_failures"] == [
        secondary_failure_event.payload["secondary_failure"]
    ]
    assert response.status == WorkspaceStatus.failed
    assert response.operation_status == OperationStatus.failed.value
    assert workspace.failure_reason == "cleanup_failure"
    assert workspace.failure_message == "volume busy"
    assert operations[0].result is not None
    assert "primary_failure" not in operations[0].result
    assert "secondary_failure" not in operations[0].result
    assert snapshot is not None
    assert snapshot.secondary_failures[-1]["reason_code"] == "CLEANUP_FAILED"


@pytest.mark.unit
async def test_destroy_cleanup_failure_preserves_existing_validation_failure(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleaner = _RecordingCleaner(failures=["volume busy"])
    original_builder = controls.build_preserved_failure_payload

    def builder_with_serialization_probe(
        primary_failure: Mapping[str, Any],
        *,
        secondary_failure: Mapping[str, Any],
        extra: Mapping[str, Any] | None = None,
        previous_secondary_failures: Sequence[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        payload = original_builder(
            primary_failure,
            secondary_failure=secondary_failure,
            extra=extra,
            previous_secondary_failures=previous_secondary_failures,
        )
        payload["secondary_failure"] = {
            **payload["secondary_failure"],
            "helper_serialization_probe": "current",
        }
        payload["secondary_failures"] = [
            {
                **secondary,
                "helper_serialization_probe": index,
            }
            for index, secondary in enumerate(payload["secondary_failures"])
        ]
        return payload

    monkeypatch.setattr(
        controls,
        "build_preserved_failure_payload",
        builder_with_serialization_probe,
    )
    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await _create_control_workspace(
            session,
            status=WorkspaceStatus.destroying,
        )
        validation_run_id = await _seed_validation_failure_evidence(
            session,
            workspace,
            failure_message="pytest failed before destroy cleanup",
        )
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
            idempotency_key="destroy-preserve-validation",
        )
        operations = await OperationRepository(session).list_for_workspace(
            workspace.id,
            operation_type=OperationType.destroy,
        )
        events = await WorkspaceEventRepository(session).list(workspace_id=workspace.id)
        validation_run = await ValidationRunRepository(session).get(validation_run_id)

    assert response.status == WorkspaceStatus.failed
    assert response.message == "workspace cleanup failed"
    assert response.operation_status == OperationStatus.failed.value
    assert workspace.failure_reason == FailureReason.validation_failure.value
    assert workspace.failure_message == "pytest failed before destroy cleanup"
    assert validation_run is not None
    assert validation_run.reason_code == "PYTEST_TEST_FAILURE"
    assert validation_run.coverage is not None
    assert validation_run.coverage["failing_test_node_ids"] == [
        "tests/unit/test_controls.py::test_destroy_cleanup"
    ]
    assert operations[0].status == OperationStatus.failed.value
    assert operations[0].error_code == "CLEANUP_FAILED"
    assert operations[0].error_message == "volume busy"
    latest_failed = next(
        event
        for event in events
        if event.event_type == "workspace.state_changed"
        and event.new_state == WorkspaceStatus.failed.value
    )
    assert latest_failed.reason_code == "PYTEST_TEST_FAILURE"
    assert latest_failed.payload is not None
    assert latest_failed.payload["primary_failure"]["validation_run"]["id"] == validation_run_id
    assert latest_failed.payload["secondary_failure"]["reason_code"] == "CLEANUP_FAILED"
    assert latest_failed.payload["secondary_failures"][-1]["reason_code"] == "CLEANUP_FAILED"
    assert latest_failed.payload["secondary_failure"]["helper_serialization_probe"] == "current"
    assert latest_failed.payload["secondary_failures"][-1]["helper_serialization_probe"] == 0
    assert latest_failed.payload["secondary_failure"]["cleanup"]["failed_steps"][0]["error"] == (
        "volume busy"
    )
    assert operations[0].result is not None
    assert operations[0].result["secondary_failure"] == latest_failed.payload["secondary_failure"]
    assert operations[0].result["secondary_failures"] == latest_failed.payload["secondary_failures"]
    audit_event = next(
        event
        for event in events
        if event.event_type == "workspace.audit.control_operation"
        and event.reason_code == "CLEANUP_FAILED"
    )
    assert audit_event.payload is not None
    assert (
        audit_event.payload["evidence"]["secondary_failure"]
        == latest_failed.payload["secondary_failure"]
    )
    assert (
        audit_event.payload["evidence"]["secondary_failures"]
        == latest_failed.payload["secondary_failures"]
    )


@pytest.mark.unit
async def test_destroy_cleanup_failure_restores_primary_fields_from_embedded_payload(
    engine: AsyncEngine,
) -> None:
    cleaner = _RecordingCleaner(failures=["volume busy"])
    factory = make_session_factory(engine)
    primary_failure = {
        "failure_reason": FailureReason.validation_failure.value,
        "reason_code": "PYTEST_TEST_FAILURE",
        "message": "pytest failed before cleanup",
    }
    stale_secondary = {
        "failure_reason": "cleanup_failure",
        "reason_code": "CLEANUP_FAILED",
        "message": "first cleanup failed after validation",
    }

    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await _create_control_workspace(
            session,
            status=WorkspaceStatus.destroying,
        )
        prior_failed_event = await repo.add_event(
            workspace,
            event_type="workspace.state_changed",
            reason_code="CLEANUP_FAILED",
            payload=controls.build_preserved_failure_payload(
                primary_failure,
                secondary_failure=stale_secondary,
            ),
        )
        prior_failed_event.old_state = WorkspaceStatus.destroying.value
        prior_failed_event.new_state = WorkspaceStatus.failed.value
        workspace.failure_reason = "cleanup_failure"
        workspace.failure_message = "first cleanup failed after validation"
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
            idempotency_key="destroy-restore-primary-row-fields",
        )
        events = await WorkspaceEventRepository(session).list(workspace_id=workspace.id)

    assert response.status == WorkspaceStatus.failed
    assert response.message == "workspace cleanup failed"
    assert workspace.failure_reason == FailureReason.validation_failure.value
    assert workspace.failure_message == "pytest failed before cleanup"
    failed_transition = next(
        event
        for event in events
        if event.event_type == "workspace.state_changed"
        and event.new_state == WorkspaceStatus.failed.value
        and event.reason_code == "PYTEST_TEST_FAILURE"
    )
    assert failed_transition.payload is not None
    assert failed_transition.payload["primary_failure"] == primary_failure
    assert failed_transition.payload["secondary_failure"]["reason_code"] == "CLEANUP_FAILED"


@pytest.mark.unit
async def test_destroy_cleanup_failure_records_secondary_when_workspace_already_failed(
    engine: AsyncEngine,
) -> None:
    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await _create_control_workspace(
            session,
            status=WorkspaceStatus.failed,
        )
        validation_run_id = await _seed_validation_failure_evidence(
            session,
            workspace,
            failure_message="pytest failed before destroy cleanup",
        )

        class _FailingAfterPrimaryEventCleaner(_RecordingCleaner):
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
                failures = await super().cleanup(
                    workspace_id=workspace_id,
                    repo_url=repo_url,
                    companion_worktrees=companion_worktrees,
                    compose_project_name=compose_project_name,
                    compose_file_path=compose_file_path,
                    worktree_host_path=worktree_host_path,
                    remove_volumes=remove_volumes,
                    remove_worktree=remove_worktree,
                )
                await WorkspaceRepository(session).transition(
                    workspace,
                    to=WorkspaceStatus.failed,
                    reason_code="PYTEST_TEST_FAILURE",
                    payload={
                        "reason_code": "PYTEST_TEST_FAILURE",
                        "message": "pytest failed before destroy cleanup",
                    },
                )
                workspace.failure_reason = FailureReason.validation_failure.value
                workspace.failure_message = "pytest failed before destroy cleanup"
                await session.flush()
                return failures

        cleaner = _FailingAfterPrimaryEventCleaner(failures=["volume busy"])
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
            idempotency_key="destroy-already-failed-cleanup",
        )
        operations = await OperationRepository(session).list_for_workspace(
            workspace.id,
            operation_type=OperationType.destroy,
        )
        events = await WorkspaceEventRepository(session).list(workspace_id=workspace.id)
        snapshot = await load_failure_causality_snapshot(session, workspace)

    assert response.status == WorkspaceStatus.failed
    assert response.message == "workspace cleanup failed"
    assert response.operation_status == OperationStatus.failed.value
    assert operations[0].status == OperationStatus.failed.value
    assert operations[0].error_code == "CLEANUP_FAILED"
    assert workspace.failure_reason == FailureReason.validation_failure.value
    assert workspace.failure_message == "pytest failed before destroy cleanup"
    ignored_callbacks = [
        event
        for event in events
        if event.event_type == "workspace.stale_callback_ignored"
        and event.reason_code == "STALE_CALLBACK_IGNORED"
    ]
    assert ignored_callbacks == []
    state_failed_events = [
        event
        for event in events
        if event.event_type == "workspace.state_changed"
        and event.new_state == WorkspaceStatus.failed.value
    ]
    assert all((event.payload or {}).get("synthetic") is not True for event in state_failed_events)
    secondary_failure_events = [
        event for event in events if event.event_type == "workspace.secondary_failure_recorded"
    ]
    assert len(secondary_failure_events) == 1
    secondary_failure_event = secondary_failure_events[0]
    assert secondary_failure_event.old_state == WorkspaceStatus.failed.value
    assert secondary_failure_event.new_state == WorkspaceStatus.failed.value
    assert secondary_failure_event.reason_code == "PYTEST_TEST_FAILURE"
    failed_event_orders = [
        event.event_order for event in state_failed_events if event.event_order is not None
    ]
    assert secondary_failure_event.event_order is not None
    assert failed_event_orders
    assert secondary_failure_event.event_order > max(failed_event_orders)
    assert secondary_failure_event.payload is not None
    assert secondary_failure_event.payload["synthetic"] is True
    assert secondary_failure_event.payload["primary_failure"]["validation_run"]["id"] == (
        validation_run_id
    )
    assert secondary_failure_event.payload["secondary_failure"]["reason_code"] == "CLEANUP_FAILED"
    assert (
        secondary_failure_event.payload["secondary_failures"][-1]["reason_code"] == "CLEANUP_FAILED"
    )
    assert operations[0].result is not None
    assert (
        operations[0].result["secondary_failures"]
        == secondary_failure_event.payload["secondary_failures"]
    )
    assert snapshot is not None
    assert snapshot.secondary_failures[-1]["reason_code"] == "CLEANUP_FAILED"


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
    cleanup_event = next(
        event
        for event in failed_events
        if event.event_type == "workspace.state_changed" and event.reason_code == "CLEANUP_FAILED"
    )
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
    assert [call["companion_worktrees"] for call in cleaner.calls] == [(), ()]


@pytest.mark.unit
async def test_destroy_workspace_remains_authoritative_after_terminal_release_event(
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
            status=WorkspaceStatus.failed,
            compose_project_name="awf_ws_destroy_after_release",
            compose_file_path=str(compose_file),
        )
        workspace.failure_reason = "agent_failure"
        workspace.failure_message = "agent crashed mid-run"
        await WorkspaceRepository(session).add_event(
            workspace,
            event_type="workspace.terminal_runtime_released",
            reason_code="TERMINAL_RUNTIME_RELEASED",
            payload={
                "cleanup": {
                    "status": "succeeded",
                    "reason_code": "CLEANUP_SUCCEEDED",
                    "steps": [],
                    "failed_steps": [],
                    "completed_steps": [],
                },
            },
        )
        await session.flush()
        service = controls.WorkspaceControlService(
            session,
            project_stopper=_RecordingStopper(),
            cleaner_factory=lambda: cleaner,
        )

        response = await service.destroy_workspace(
            workspace.id,
            force=True,
            remove_volumes=True,
            remove_worktree=True,
            idempotency_key="destroy-after-release",
        )

    assert response.status == WorkspaceStatus.destroyed
    assert response.operation_status == OperationStatus.succeeded.value
    assert cleaner.calls == [
        {
            "workspace_id": workspace.id,
            "repo_url": workspace.repo_url,
            "companion_worktrees": (),
            "compose_project_name": "awf_ws_destroy_after_release",
            "compose_file_path": compose_file,
            "worktree_host_path": None,
            "remove_volumes": True,
            "remove_worktree": True,
        }
    ]


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
            row.id
            for row in await WorkspaceEventRepository(session).list(workspace_id=workspace.id)
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
            row.id
            for row in await WorkspaceEventRepository(session).list(workspace_id=workspace.id)
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
async def test_destroy_workspace_records_terminal_runtime_released_after_cleanup(
    engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    """After successful destroy cleanup, the terminal_runtime_released event
    is recorded so the destroyed workspace no longer blocks host ports."""
    cleaner = _RecordingCleaner()
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await _create_control_workspace(
            session,
            status=WorkspaceStatus.ready,
            compose_project_name="awf_ws_destroy_release",
            compose_file_path=str(compose_file),
        )
        workspace.task_policy = {
            "companions": [
                {
                    "name": "web",
                    "repo_url": "git@github.com:example/web.git",
                    "ports": [[80, 8080]],
                }
            ]
        }
        await session.flush()
        service = controls.WorkspaceControlService(
            session,
            project_stopper=_RecordingStopper(),
            cleaner_factory=lambda: cleaner,
        )

        response = await service.destroy_workspace(
            workspace.id,
            force=True,
            remove_volumes=True,
            remove_worktree=True,
            idempotency_key="destroy-release-event",
        )

        assert response.status == WorkspaceStatus.destroyed
        assert response.message == "workspace destroyed"
        repo = WorkspaceRepository(session)
        events = await WorkspaceEventRepository(session).list(workspace_id=workspace.id)
        release_events = [
            e for e in events if e.event_type == "workspace.terminal_runtime_released"
        ]
        assert len(release_events) == 1
        assert release_events[0].reason_code == "TERMINAL_RUNTIME_RELEASED"
        conflicts = await repo.find_host_port_conflicts(
            host_ports=[8080],
            excluding_workspace_id=None,
        )
        assert len(conflicts) == 0


@pytest.mark.unit
async def test_stop_workspace_records_terminal_runtime_released(
    engine: AsyncEngine,
) -> None:
    """After a successful stop, the terminal_runtime_released event is
    recorded so the cancelled workspace no longer blocks host ports."""
    from awf.db.repositories.base import (
        PROVISIONING_LAUNCHING_EVENT_TYPE,
        PROVISIONING_LAUNCHING_REASON_CODE,
    )

    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await _create_control_workspace(
            session,
            status=WorkspaceStatus.running,
            compose_project_name="awf_ws_stop_release",
        )
        workspace.task_policy = {
            "companions": [
                {
                    "name": "web",
                    "repo_url": "git@github.com:example/web.git",
                    "ports": [[80, 8081]],
                }
            ]
        }
        await session.flush()
        repo = WorkspaceRepository(session)
        await repo.add_event(
            workspace,
            event_type=PROVISIONING_LAUNCHING_EVENT_TYPE,
            reason_code=PROVISIONING_LAUNCHING_REASON_CODE,
            payload={"workspace_id": workspace.id},
        )
        await session.flush()
        service = controls.WorkspaceControlService(
            session,
            project_stopper=_RecordingStopper(),
            cleaner_factory=lambda: _RecordingCleaner(),
        )

        response = await service.stop_workspace(
            workspace.id,
            reason="stop for port release",
            idempotency_key="stop-release-event",
        )

        assert response.status == WorkspaceStatus.cancelled
        repo = WorkspaceRepository(session)
        events = await WorkspaceEventRepository(session).list(workspace_id=workspace.id)
        release_events = [
            e for e in events if e.event_type == "workspace.terminal_runtime_released"
        ]
        assert len(release_events) == 1
        assert release_events[0].reason_code == "TERMINAL_RUNTIME_RELEASED"
        conflicts = await repo.find_host_port_conflicts(
            host_ports=[8081],
            excluding_workspace_id=None,
        )
        assert len(conflicts) == 0


@pytest.mark.unit
async def test_cancel_workspace_records_terminal_runtime_released(
    engine: AsyncEngine,
) -> None:
    """After a successful cancel with stop_stack=True, the terminal_runtime_released
    event is recorded so the cancelled workspace no longer blocks host ports."""
    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await _create_control_workspace(
            session,
            status=WorkspaceStatus.running,
            compose_project_name="awf_ws_cancel_release",
        )
        workspace.task_policy = {
            "companions": [
                {
                    "name": "web",
                    "repo_url": "git@github.com:example/web.git",
                    "ports": [[80, 8082]],
                }
            ]
        }
        await session.flush()
        service = controls.WorkspaceControlService(
            session,
            project_stopper=_RecordingStopper(),
            cleaner_factory=lambda: _RecordingCleaner(),
        )

        response = await service.cancel_workspace(
            workspace.id,
            reason="cancel for port release",
            stop_stack=True,
            idempotency_key="cancel-release-event",
        )

        assert response.status == WorkspaceStatus.cancelled
        repo = WorkspaceRepository(session)
        events = await WorkspaceEventRepository(session).list(workspace_id=workspace.id)
        release_events = [
            e for e in events if e.event_type == "workspace.terminal_runtime_released"
        ]
        assert len(release_events) == 1
        assert release_events[0].reason_code == "TERMINAL_RUNTIME_RELEASED"
        conflicts = await repo.find_host_port_conflicts(
            host_ports=[8082],
            excluding_workspace_id=None,
        )
        assert len(conflicts) == 0


@pytest.mark.unit
async def test_cancel_workspace_without_stop_stack_skips_terminal_runtime_released(
    engine: AsyncEngine,
) -> None:
    """When cancel is called with stop_stack=False, the terminal_runtime_released
    event is not recorded because the compose stack may still be running."""
    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await _create_control_workspace(
            session,
            status=WorkspaceStatus.cancelled,
            compose_project_name="awf_ws_cancel_no_stop",
        )
        await session.flush()
        service = controls.WorkspaceControlService(
            session,
            project_stopper=_RecordingStopper(),
            cleaner_factory=lambda: _RecordingCleaner(),
        )

        await service.cancel_workspace(
            workspace.id,
            reason="no stack stop",
            stop_stack=False,
            idempotency_key="cancel-no-stop",
        )

        events = await WorkspaceEventRepository(session).list(workspace_id=workspace.id)
        release_events = [
            e for e in events if e.event_type == "workspace.terminal_runtime_released"
        ]
        assert len(release_events) == 0


@pytest.mark.unit
async def test_cancel_workspace_records_terminal_runtime_released_when_launching(
    engine: AsyncEngine,
) -> None:
    """When a provisioning_launching guard event exists and stop_stack=True
    successfully stops the stack, cancel must record terminal_runtime_released
    because the containers are gone and the ports should be released for reuse."""
    from awf.db.repositories.base import (
        PROVISIONING_LAUNCHING_EVENT_TYPE,
        PROVISIONING_LAUNCHING_REASON_CODE,
    )

    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await _create_control_workspace(
            session,
            status=WorkspaceStatus.provisioning,
            compose_project_name="awf_ws_cancel_launching",
        )
        await session.flush()
        repo = WorkspaceRepository(session)
        await repo.add_event(
            workspace,
            event_type=PROVISIONING_LAUNCHING_EVENT_TYPE,
            reason_code=PROVISIONING_LAUNCHING_REASON_CODE,
            payload={"workspace_id": workspace.id},
        )
        await session.flush()
        service = controls.WorkspaceControlService(
            session,
            project_stopper=_RecordingStopper(),
            cleaner_factory=lambda: _RecordingCleaner(),
        )

        await service.cancel_workspace(
            workspace.id,
            reason="cancel while launching",
            stop_stack=True,
            idempotency_key="cancel-launching-guard",
        )

        events = await WorkspaceEventRepository(session).list(workspace_id=workspace.id)
        release_events = [
            e for e in events if e.event_type == "workspace.terminal_runtime_released"
        ]
        assert len(release_events) == 1, (
            "terminal_runtime_released must be recorded after stop_stack=True "
            "even when provisioning_launching guard exists"
        )


@pytest.mark.unit
async def test_stop_workspace_records_terminal_runtime_released_when_compose_project_name(
    engine: AsyncEngine,
) -> None:
    """When a workspace has a compose_project_name, stop_workspace must record
    terminal_runtime_released even without a provisioning_launching event,
    because _source_runtime_not_yet_released treats compose_project_name as
    proof that ports were claimed and blocks retries until release."""
    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await _create_control_workspace(
            session,
            status=WorkspaceStatus.provisioning,
            compose_project_name="awf_ws_stop_no_launching",
        )
        workspace.task_policy = {
            "companions": [
                {
                    "name": "web",
                    "repo_url": "git@github.com:example/web.git",
                    "ports": [[80, 8091]],
                }
            ]
        }
        await session.flush()
        service = controls.WorkspaceControlService(
            session,
            project_stopper=_RecordingStopper(),
            cleaner_factory=lambda: _RecordingCleaner(),
        )

        await service.stop_workspace(
            workspace.id,
            reason="stop before compose-up",
            idempotency_key="stop-no-launching",
        )

        events = await WorkspaceEventRepository(session).list(workspace_id=workspace.id)
        release_events = [
            e for e in events if e.event_type == "workspace.terminal_runtime_released"
        ]
        assert len(release_events) == 1, (
            "terminal_runtime_released must be recorded when "
            "compose_project_name is set, so _source_runtime_not_yet_released "
            "does not block retries"
        )


@pytest.mark.unit
async def test_stop_workspace_records_terminal_runtime_released_when_launching(
    engine: AsyncEngine,
) -> None:
    """When provisioning_launching event exists, stop_workspace records
    terminal_runtime_released because the compose project was at least
    partially started and the stop actually freed the ports."""
    from awf.db.repositories.base import (
        PROVISIONING_LAUNCHING_EVENT_TYPE,
        PROVISIONING_LAUNCHING_REASON_CODE,
    )

    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await _create_control_workspace(
            session,
            status=WorkspaceStatus.provisioning,
            compose_project_name="awf_ws_stop_with_launching",
        )
        workspace.task_policy = {
            "companions": [
                {
                    "name": "web",
                    "repo_url": "git@github.com:example/web.git",
                    "ports": [[80, 8092]],
                }
            ]
        }
        await session.flush()
        repo = WorkspaceRepository(session)
        await repo.add_event(
            workspace,
            event_type=PROVISIONING_LAUNCHING_EVENT_TYPE,
            reason_code=PROVISIONING_LAUNCHING_REASON_CODE,
            payload={"workspace_id": workspace.id},
        )
        await session.flush()
        service = controls.WorkspaceControlService(
            session,
            project_stopper=_RecordingStopper(),
            cleaner_factory=lambda: _RecordingCleaner(),
        )

        await service.stop_workspace(
            workspace.id,
            reason="stop while launching",
            idempotency_key="stop-launching-guard",
        )

        events = await WorkspaceEventRepository(session).list(workspace_id=workspace.id)
        release_events = [
            e for e in events if e.event_type == "workspace.terminal_runtime_released"
        ]
        assert len(release_events) == 1, (
            "terminal_runtime_released must be recorded when provisioning_launching guard exists"
        )
