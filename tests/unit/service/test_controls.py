from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import structlog
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from awf.common.config import get_settings
from awf.db.enums import OperationStatus, OperationType, WorkspaceStatus
from awf.db.repositories import OperationRepository, WorkspaceEventRepository, WorkspaceRepository
from awf.db.session import make_session_factory
from awf.service import controls
from awf.service.controls import WorkspaceStackStopError, stop_project_containers
from awf.service.terminal_runtime import (
    TERMINAL_RUNTIME_RELEASE_CLAIM_LOST_REASON_CODE,
    TERMINAL_RUNTIME_RELEASE_CLAIM_OWNER_PREFIX,
    TERMINAL_RUNTIME_RELEASE_CLAIM_REFRESH_FAILED_REASON_CODE,
)


def _mock_proc(returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> AsyncMock:
    proc = AsyncMock()
    proc.returncode = returncode
    proc.communicate.return_value = (stdout, stderr)
    return proc


class _CancellationHangingProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.communicate_started = asyncio.Event()
        self.kill_called = False
        self.wait_called = False

    async def communicate(self) -> tuple[bytes, bytes]:
        self.communicate_started.set()
        await asyncio.Event().wait()
        return b"", b""

    def kill(self) -> None:
        self.kill_called = True
        self.returncode = -9

    async def wait(self) -> int | None:
        self.wait_called = True
        return self.returncode


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
    assert mock_exec.call_args_list[0].args == (
        "docker",
        "ps",
        "-q",
        "--filter",
        "label=com.docker.compose.project=awf_ws_running",
    )
    assert mock_exec.call_args_list[1].args == ("docker", "stop", "abc123", "def456")
    all_args = [arg for call in mock_exec.call_args_list for arg in call.args]
    assert "down" not in all_args
    assert "--volumes" not in all_args


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
async def test_stop_project_containers_ignores_racy_missing_container_on_stop() -> None:
    with patch(
        "awf.service.controls.asyncio.create_subprocess_exec",
        side_effect=[
            _mock_proc(stdout=b"abc123\n"),
            _mock_proc(
                returncode=1,
                stderr=b"Error response from daemon: No such container: abc123\n",
            ),
        ],
    ) as mock_exec:
        await stop_project_containers("awf_ws_racy_stop")

    assert mock_exec.call_count == 2
    assert mock_exec.call_args_list[1].args == ("docker", "stop", "abc123")


@pytest.mark.unit
async def test_stop_project_containers_cancellation_kills_ps_subprocess() -> None:
    proc = _CancellationHangingProcess()
    with patch(
        "awf.service.controls.asyncio.create_subprocess_exec",
        return_value=proc,
    ):
        task = asyncio.create_task(stop_project_containers("awf_ws_hanging_ps"))
        await asyncio.wait_for(proc.communicate_started.wait(), timeout=1)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)

    assert proc.kill_called is True
    assert proc.wait_called is True


@pytest.mark.unit
async def test_stop_project_containers_cancellation_kills_stop_subprocess() -> None:
    stop_proc = _CancellationHangingProcess()
    with patch(
        "awf.service.controls.asyncio.create_subprocess_exec",
        side_effect=[
            _mock_proc(stdout=b"abc123\n"),
            stop_proc,
        ],
    ):
        task = asyncio.create_task(stop_project_containers("awf_ws_hanging_stop"))
        await asyncio.wait_for(stop_proc.communicate_started.wait(), timeout=1)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)

    assert stop_proc.kill_called is True
    assert stop_proc.wait_called is True


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
            session_factory=factory,
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
            session_factory=factory,
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
@pytest.mark.parametrize("action", ["cancel", "stop"])
async def test_stop_and_cancel_record_terminal_cleanup_before_version_conflict(
    engine: AsyncEngine,
    action: str,
) -> None:
    factory = make_session_factory(engine)
    cleaner = _RecordingCleaner()
    async with factory() as seed_session:
        workspace = await _create_control_workspace(
            seed_session,
            status=WorkspaceStatus.running,
            compose_project_name=f"awf_ws_{action}_conflict",
        )
        expected_version = workspace.version
        workspace_id = workspace.id
        await seed_session.commit()

    class _VersionBumpingStopper:
        def __init__(self) -> None:
            self.calls: list[str | None] = []

        async def __call__(self, compose_project_name: str | None) -> None:
            self.calls.append(compose_project_name)
            async with factory() as bump_session:
                bumped = await WorkspaceRepository(bump_session).get_for_update(workspace_id)
                assert bumped is not None
                bumped.subphase = f"{action}-external-stop"
                bumped.version += 1
                await bump_session.commit()

    stopper = _VersionBumpingStopper()
    async with factory() as session:
        service = controls.WorkspaceControlService(
            session,
            project_stopper=stopper,
            cleaner_factory=lambda: cleaner,
            session_factory=factory,
        )
        with pytest.raises(controls.VersionConflictError) as exc_info:
            if action == "cancel":
                await service.cancel_workspace(
                    workspace_id,
                    reason="operator cancel races version",
                    stop_stack=True,
                    expected_version=expected_version,
                )
            else:
                await service.stop_workspace(
                    workspace_id,
                    reason="operator stop races version",
                    expected_version=expected_version,
                )

    assert exc_info.value.detail["expected_version"] == expected_version
    assert stopper.calls == [f"awf_ws_{action}_conflict"]
    assert cleaner.calls[0]["remove_volumes"] is False
    assert cleaner.calls[0]["remove_worktree"] is False
    async with factory() as session:
        events = await WorkspaceEventRepository(session).list(workspace_id=workspace_id)
        operations = await OperationRepository(session).list_for_workspace(
            workspace_id,
            operation_type=OperationType.cancel if action == "cancel" else OperationType.stop,
        )

    release_event = next(
        event for event in events if event.event_type == "workspace.terminal_runtime_released"
    )
    assert release_event.payload["source"] == f"service.controls.{action}"
    assert operations[0].status == OperationStatus.failed.value
    assert operations[0].error_code == "VERSION_CONFLICT"


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
            session_factory=factory,
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
@pytest.mark.parametrize(
    ("method_name", "operation_type", "extra_kwargs"),
    [
        ("cancel_workspace", OperationType.cancel, {"stop_stack": True}),
        ("stop_workspace", OperationType.stop, {}),
    ],
)
async def test_control_runtime_claim_check_failure_marks_precommitted_operation_failed(
    engine: AsyncEngine,
    method_name: str,
    operation_type: OperationType,
    extra_kwargs: Mapping[str, object],
) -> None:
    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await _create_control_workspace(
            session,
            status=WorkspaceStatus.running,
            compose_project_name=f"awf_ws_{method_name}",
        )
        service = controls.WorkspaceControlService(
            session,
            project_stopper=_RecordingStopper(),
            cleaner_factory=lambda: _RecordingCleaner(),
            session_factory=factory,
        )

        async def fail_claim_check(_workspace: object) -> bool:
            raise RuntimeError("database temporarily unavailable")

        service._terminal_runtime_release_claim_active_for_control = fail_claim_check  # type: ignore[method-assign]  # noqa: SLF001
        control_method = getattr(service, method_name)
        with pytest.raises(RuntimeError, match="database temporarily unavailable"):
            await control_method(
                workspace.id,
                reason="operator requested teardown",
                idempotency_key=f"{method_name}-claim-failure",
                **extra_kwargs,
            )
        operations = await OperationRepository(session).list_for_workspace(
            workspace.id,
            operation_type=operation_type,
        )

    assert len(operations) == 1
    assert operations[0].status == OperationStatus.failed.value
    assert operations[0].error_code == "CONTROL_OPERATION_FAILED"
    assert "RuntimeError: database temporarily unavailable" in str(operations[0].error_message)


@pytest.mark.unit
@pytest.mark.parametrize("error_kind", ["runtime", "cancelled"])
async def test_cancel_workspace_records_post_cleanup_failures_after_precommit(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    error_kind: str,
) -> None:
    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await _create_control_workspace(
            session,
            status=WorkspaceStatus.running,
            compose_project_name="awf_ws_cancel_post_cleanup_failure",
        )
        workspace_id = workspace.id
        service = controls.WorkspaceControlService(
            session,
            project_stopper=_RecordingStopper(),
            cleaner_factory=lambda: _RecordingCleaner(),
            session_factory=factory,
        )

        async def fail_transition(*_args: object, **_kwargs: object) -> object:
            if error_kind == "cancelled":
                raise asyncio.CancelledError()
            raise RuntimeError("transition after cleanup failed")

        monkeypatch.setattr(controls, "_transition_workspace_for_control", fail_transition)

        if error_kind == "cancelled":
            with pytest.raises(asyncio.CancelledError):
                await service.cancel_workspace(
                    workspace_id,
                    reason="operator cancel races post-cleanup",
                    stop_stack=True,
                    idempotency_key="cancel-post-cleanup-cancelled",
                )
        else:
            with pytest.raises(RuntimeError, match="transition after cleanup failed"):
                await service.cancel_workspace(
                    workspace_id,
                    reason="operator cancel races post-cleanup",
                    stop_stack=True,
                    idempotency_key="cancel-post-cleanup-runtime",
                )

        operations = await OperationRepository(session).list_for_workspace(
            workspace_id,
            operation_type=OperationType.cancel,
        )

    assert len(operations) == 1
    assert operations[0].status == OperationStatus.failed.value
    assert operations[0].error_code == "CONTROL_OPERATION_FAILED"
    if error_kind == "cancelled":
        assert operations[0].error_message == "CancelledError: operation was cancelled"
    else:
        assert operations[0].error_message == "RuntimeError: transition after cleanup failed"


@pytest.mark.unit
async def test_teardown_operation_heartbeat_helper_handles_sessionless_and_stopped_lease(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = make_session_factory(engine)
    async with factory() as session:
        service = controls.WorkspaceControlService(
            session,
            project_stopper=_RecordingStopper(),
            cleaner_factory=lambda: _RecordingCleaner(),
            session_factory=factory,
        )

        async def _done() -> str:
            return "done"

        service._session_factory = None  # type: ignore[assignment]  # noqa: SLF001
        with pytest.raises(RuntimeError, match="session_factory"):
            await service._run_with_teardown_operation_heartbeat("op-sessionless", _done())  # noqa: SLF001
        service._session_factory = factory  # type: ignore[assignment]  # noqa: SLF001

        async def _lease_stopped(*_args: object, **_kwargs: object) -> str:
            return "operation lease is no longer active"

        monkeypatch.setattr(
            controls,
            "_renew_runtime_teardown_operation_lease_loop",
            _lease_stopped,
        )
        assert (
            await service._run_with_teardown_operation_heartbeat("op-done", _done())  # noqa: SLF001
            == "done"
        )

        async def _pending() -> str:
            await asyncio.Event().wait()
            return "unreachable"

        with pytest.raises(RuntimeError, match="operation lease is no longer active"):
            await service._run_with_teardown_operation_heartbeat("op-lost", _pending())  # noqa: SLF001


@pytest.mark.unit
async def test_teardown_operation_heartbeat_requires_explicit_session_factory(
    engine: AsyncEngine,
) -> None:
    factory = make_session_factory(engine)
    async with factory() as session:
        service = controls.WorkspaceControlService(
            session,
            project_stopper=_RecordingStopper(),
            cleaner_factory=lambda: _RecordingCleaner(),
        )

        async def _done() -> str:
            return "done"

        with pytest.raises(RuntimeError, match="session_factory"):
            await service._run_with_teardown_operation_heartbeat("op-no-factory", _done())  # noqa: SLF001


@pytest.mark.unit
async def test_terminal_runtime_release_claim_heartbeat_helper_handles_stopped_heartbeat(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = make_session_factory(engine)
    async with factory() as session:
        service = controls.WorkspaceControlService(
            session,
            project_stopper=_RecordingStopper(),
            cleaner_factory=lambda: _RecordingCleaner(),
            session_factory=factory,
        )

        async def _claim_loop_done(*_args: object, **_kwargs: object) -> object:
            return controls._ControlTerminalRuntimeReleaseClaimFailure(  # noqa: SLF001
                reason_code=TERMINAL_RUNTIME_RELEASE_CLAIM_LOST_REASON_CODE,
            )

        monkeypatch.setattr(
            controls,
            "_refresh_terminal_runtime_release_claim_loop",
            _claim_loop_done,
        )

        async def _done() -> str:
            return "done"

        assert (
            await service._run_with_terminal_runtime_release_claim_heartbeat(  # noqa: SLF001
                "ws-done",
                owner_id="owner",
                work=_done(),
            )
            == "done"
        )

        async def _pending() -> str:
            await asyncio.Event().wait()
            return "unreachable"

        with pytest.raises(
            RuntimeError,
            match="terminal runtime release claim heartbeat stopped",
        ):
            await service._run_with_terminal_runtime_release_claim_heartbeat(  # noqa: SLF001
                "ws-lost",
                owner_id="owner",
                work=_pending(),
            )


@pytest.mark.unit
async def test_terminal_runtime_release_claim_heartbeat_requires_session_factory(
    engine: AsyncEngine,
) -> None:
    factory = make_session_factory(engine)
    async with factory() as session:
        service = controls.WorkspaceControlService(
            session,
            project_stopper=_RecordingStopper(),
            cleaner_factory=lambda: _RecordingCleaner(),
            session_factory=factory,
        )

        async def _done() -> str:
            return "done"

        service._session_factory = None  # type: ignore[assignment]  # noqa: SLF001
        with pytest.raises(RuntimeError, match="session_factory"):
            await service._run_with_terminal_runtime_release_claim_heartbeat(  # noqa: SLF001
                "ws-sessionless",
                owner_id="owner",
                work=_done(),
            )


@pytest.mark.unit
async def test_terminal_runtime_release_claim_heartbeat_helper_cancels_pending_work(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = make_session_factory(engine)
    async with factory() as session:
        service = controls.WorkspaceControlService(
            session,
            project_stopper=_RecordingStopper(),
            cleaner_factory=lambda: _RecordingCleaner(),
            session_factory=factory,
        )
        work_cancelled = asyncio.Event()

        async def _claim_loop_waits(*_args: object, **_kwargs: object) -> None:
            await asyncio.Event().wait()

        async def _pending_work() -> str:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                work_cancelled.set()
                raise
            return "unreachable"

        monkeypatch.setattr(
            controls,
            "_refresh_terminal_runtime_release_claim_loop",
            _claim_loop_waits,
        )
        helper = asyncio.create_task(
            service._run_with_terminal_runtime_release_claim_heartbeat(  # noqa: SLF001
                "ws-cancel",
                owner_id="owner",
                work=_pending_work(),
            )
        )
        await asyncio.sleep(0)
        helper.cancel()
        with pytest.raises(asyncio.CancelledError):
            await helper

    assert work_cancelled.is_set()


@pytest.mark.unit
async def test_terminal_runtime_release_claim_heartbeat_helper_cancels_cleanup_on_refresh_error(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = make_session_factory(engine)
    async with factory() as session:
        service = controls.WorkspaceControlService(
            session,
            project_stopper=_RecordingStopper(),
            cleaner_factory=lambda: _RecordingCleaner(),
            session_factory=factory,
        )
        work_started = asyncio.Event()
        work_cancelled = asyncio.Event()

        async def _claim_loop_refresh_failed(*_args: object, **_kwargs: object) -> object:
            return controls._ControlTerminalRuntimeReleaseClaimFailure(  # noqa: SLF001
                reason_code=TERMINAL_RUNTIME_RELEASE_CLAIM_REFRESH_FAILED_REASON_CODE,
                error="RuntimeError: database unavailable",
            )

        async def _pending_work() -> str:
            work_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                work_cancelled.set()
                raise
            return "cleaned"

        monkeypatch.setattr(
            controls,
            "_refresh_terminal_runtime_release_claim_loop",
            _claim_loop_refresh_failed,
        )
        helper = asyncio.create_task(
            service._run_with_terminal_runtime_release_claim_heartbeat(  # noqa: SLF001
                "ws-refresh-error",
                owner_id="owner",
                work=_pending_work(),
            )
        )
        await work_started.wait()
        await asyncio.sleep(0)

        with pytest.raises(
            RuntimeError,
            match="terminal runtime release claim heartbeat stopped before cleanup completed",
        ):
            await helper
        assert work_cancelled.is_set()


@pytest.mark.unit
async def test_control_private_helpers_cover_defensive_branches() -> None:
    operation = SimpleNamespace(
        status=OperationStatus.failed.value,
        type=OperationType.cancel.value,
        started_at=None,
        lease_renewed_at=None,
        finished_at=datetime.now(UTC),
        error_code="OLD",
        error_message="old",
        result={"old": True},
    )
    controls._renew_runtime_teardown_operation(operation)  # noqa: SLF001
    assert operation.status == OperationStatus.running.value
    assert operation.started_at is not None
    assert operation.finished_at is None
    assert operation.error_code is None
    assert operation.result is None

    class _Operations:
        async def list_for_workspace(
            self,
            _workspace_id: str,
            *,
            status: OperationStatus,
            limit: int,
        ) -> list[object]:
            del limit
            if status is OperationStatus.pending:
                return [
                    SimpleNamespace(
                        created_at=datetime(2026, 1, 1, tzinfo=UTC),
                        id="old-stop",
                        type=OperationType.stop.value,
                        status=OperationStatus.running.value,
                        started_at=datetime.now(UTC) - timedelta(minutes=20),
                        lease_renewed_at=datetime.now(UTC) - timedelta(minutes=20),
                        lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
                    )
                ]
            return [
                SimpleNamespace(
                    created_at=datetime(2026, 1, 2, tzinfo=UTC),
                    id="validate",
                    type=OperationType.validate.value,
                    status=OperationStatus.running.value,
                )
            ]

    active = await controls._find_active_operation(  # noqa: SLF001
        _Operations(),  # type: ignore[arg-type]
        workspace_id="ws_1",
        operation_types={OperationType.stop.value, OperationType.validate.value},
    )
    assert active is not None
    assert active.id == "validate"

    await controls._release_active_resource_reservation_for_control(  # noqa: SLF001
        SimpleNamespace(),
        SimpleNamespace(status=WorkspaceStatus.ready.value),
    )


@pytest.mark.unit
async def test_preserve_precommitted_cancelled_operation_uses_dedicated_session(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await _create_control_workspace(session, status=WorkspaceStatus.running)
        operation = await OperationRepository(session).create(
            workspace_id=workspace.id,
            operation_type=OperationType.cancel,
            status=OperationStatus.running,
        )
        await session.commit()
        service = controls.WorkspaceControlService(
            session,
            project_stopper=_RecordingStopper(),
            cleaner_factory=lambda: _RecordingCleaner(),
            session_factory=factory,
        )
        rollback = AsyncMock(side_effect=AssertionError("parent session reused"))
        monkeypatch.setattr(session, "rollback", rollback)

        await service._preserve_precommitted_cancelled_operation(operation.id)  # noqa: SLF001

    rollback.assert_not_awaited()
    async with factory() as verify_session:
        persisted = await OperationRepository(verify_session).get(operation.id)

    assert persisted is not None
    assert persisted.status == OperationStatus.running.value
    assert persisted.lease_renewed_at is not None


@pytest.mark.unit
async def test_precommitted_cancelled_operation_helpers_use_current_session_without_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = SimpleNamespace(rollback=AsyncMock())
    service = controls.WorkspaceControlService(
        session,  # type: ignore[arg-type]
        project_stopper=lambda _compose_project_name: None,
        cleaner_factory=lambda: controls.WorkspaceCleanupResult(
            status="succeeded",
            reason_code="CLEANUP_SUCCEEDED",
        ),
    )
    service._session_factory = None  # type: ignore[attr-defined]  # noqa: SLF001
    preserved: list[tuple[str, object]] = []
    failed: list[dict[str, object]] = []

    async def preserve(operation_id: str, *, session: object) -> None:
        preserved.append((operation_id, session))

    async def finish_failed(session: object, **kwargs: object) -> None:
        failed.append({"session": session, **kwargs})

    service._preserve_precommitted_cancelled_operation_unshielded = preserve  # type: ignore[method-assign]  # noqa: SLF001
    monkeypatch.setattr(
        controls,
        "_finish_precommitted_control_operation_failed",
        finish_failed,
    )

    await service._preserve_precommitted_cancelled_operation("op_current")  # noqa: SLF001
    await service._finish_precommitted_cancelled_control_operation_failed(  # noqa: SLF001
        operation_id="op_failed",
        workspace_id="ws_failed",
        exc=asyncio.CancelledError("cancelled"),
        terminal_runtime_release_claim_owner_id="terminal-runtime-release:owner",
    )

    assert preserved == [("op_current", session)]
    assert len(failed) == 1
    assert failed[0]["session"] is session
    assert failed[0]["operation_id"] == "op_failed"
    assert failed[0]["workspace_id"] == "ws_failed"
    assert isinstance(failed[0]["exc"], asyncio.CancelledError)
    assert failed[0]["terminal_runtime_release_claim_owner_id"] == "terminal-runtime-release:owner"


@pytest.mark.unit
async def test_preserve_precommitted_cancelled_operation_unshielded_edges(
    engine: AsyncEngine,
) -> None:
    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await _create_control_workspace(session, status=WorkspaceStatus.running)
        operation = await OperationRepository(session).create(
            workspace_id=workspace.id,
            operation_type=OperationType.stop,
            status=OperationStatus.running,
        )
        operation_id = operation.id
        await session.commit()
        service = controls.WorkspaceControlService(
            session,
            project_stopper=_RecordingStopper(),
            cleaner_factory=lambda: _RecordingCleaner(),
            session_factory=None,
        )

        await service._preserve_precommitted_cancelled_operation_unshielded(  # noqa: SLF001
            operation_id,
            session=session,
        )
        service._session_factory = None  # type: ignore[assignment]  # noqa: SLF001
        await service._preserve_precommitted_cancelled_operation_unshielded(  # noqa: SLF001
            "ignored-without-session-factory",
        )

        persisted = await OperationRepository(session).get(operation_id)

    assert persisted is not None
    assert persisted.status == OperationStatus.running.value
    assert persisted.lease_renewed_at is not None


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
async def test_refresh_active_coalesce_rejects_destroyed_workspace_state(
    engine: AsyncEngine,
) -> None:
    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await _create_control_workspace(session, status=WorkspaceStatus.ready)
        service = controls.WorkspaceControlService(
            session,
            project_stopper=_RecordingStopper(),
            cleaner_factory=lambda: _RecordingCleaner(),
        )

        operation = await service.request_refresh_workspace(
            workspace.id,
            reason="retry provider check",
        )
        assert operation.status == OperationStatus.pending.value
        workspace.status = WorkspaceStatus.destroyed.value
        await session.flush()

        with pytest.raises(controls.WorkspaceRefreshStateError):
            await service.request_refresh_workspace(
                workspace.id,
                reason="retry provider check",
            )


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
            session_factory=factory,
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
async def test_destroy_workspace_renews_teardown_operation_while_cleanup_runs(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = make_session_factory(engine)
    monkeypatch.setattr(
        controls,
        "_RUNTIME_TEARDOWN_OPERATION_HEARTBEAT_INTERVAL_SECONDS",
        0.001,
    )

    class _LeaseObservingCleaner:
        def __init__(self) -> None:
            self.lease_seen_at: datetime | None = None

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
            del (
                repo_url,
                compose_project_name,
                compose_file_path,
                worktree_host_path,
                remove_volumes,
                remove_worktree,
            )
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 1
            while loop.time() < deadline:
                async with factory() as observe_session:
                    operations = await OperationRepository(observe_session).list_for_workspace(
                        workspace_id,
                        operation_type=OperationType.destroy,
                    )
                    running_destroy = next(
                        (
                            operation
                            for operation in operations
                            if operation.status == OperationStatus.running.value
                        ),
                        None,
                    )
                    if running_destroy is not None and running_destroy.lease_renewed_at is not None:
                        self.lease_seen_at = running_destroy.lease_renewed_at
                        return []
                await asyncio.sleep(0.005)
            raise AssertionError(
                "destroy cleanup never observed a renewed teardown operation lease"
            )

    cleaner = _LeaseObservingCleaner()
    async with factory() as session:
        workspace = await _create_control_workspace(session, status=WorkspaceStatus.ready)
        service = controls.WorkspaceControlService(
            session,
            project_stopper=_RecordingStopper(),
            cleaner_factory=lambda: cleaner,
            session_factory=factory,
        )

        response = await service.destroy_workspace(
            workspace.id,
            force=True,
            remove_volumes=True,
            remove_worktree=True,
            idempotency_key="destroy-renews-teardown-lease",
        )

    assert response.status == WorkspaceStatus.destroyed
    assert cleaner.lease_seen_at is not None


@pytest.mark.unit
async def test_destroy_workspace_resumes_expired_idempotent_destroy_operation(
    engine: AsyncEngine,
) -> None:
    factory = make_session_factory(engine)
    cleaner = _RecordingCleaner()
    async with factory() as session:
        workspace = await _create_control_workspace(
            session,
            status=WorkspaceStatus.destroying,
            compose_project_name="awf_ws_resume_destroy",
        )
        operation_payload = controls._operation_payload(
            controls._operator_operation_payload(
                reason=None,
                reason_code="OPERATOR_DESTROY",
                requested_action=OperationType.destroy.value,
                extra={
                    "force": False,
                    "remove_volumes": True,
                    "remove_worktree": True,
                },
            ),
            expected_version=None,
        )
        expired_at = datetime.now(UTC) - timedelta(minutes=30)
        operation = await OperationRepository(session).create(
            workspace_id=workspace.id,
            operation_type=OperationType.destroy,
            status=OperationStatus.running,
            payload=operation_payload,
            idempotency_key="destroy-expired-resume",
        )
        operation.created_at = expired_at
        operation.started_at = expired_at
        operation.lease_renewed_at = expired_at
        await session.flush()

        service = controls.WorkspaceControlService(
            session,
            project_stopper=_RecordingStopper(),
            cleaner_factory=lambda: cleaner,
            session_factory=factory,
        )

        response = await service.destroy_workspace(
            workspace.id,
            force=False,
            remove_volumes=True,
            remove_worktree=True,
            idempotency_key="destroy-expired-resume",
        )
        operations = await OperationRepository(session).list_for_workspace(
            workspace.id,
            operation_type=OperationType.destroy,
        )

    assert response.operation_id == operation.id
    assert response.status == WorkspaceStatus.destroyed
    assert response.operation_status == OperationStatus.succeeded.value
    assert len(cleaner.calls) == 1
    assert [persisted.id for persisted in operations] == [operation.id]
    assert operations[0].status == OperationStatus.succeeded.value
    assert operations[0].lease_renewed_at is not None
    assert operations[0].lease_renewed_at > expired_at


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
            session_factory=factory,
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
            session_factory=factory,
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
async def test_communicate_cancellation_kills_and_waits_for_subprocess() -> None:
    proc = _CancellationHangingProcess()
    task = asyncio.create_task(controls._communicate(proc, operation="stop"))  # noqa: SLF001
    await asyncio.wait_for(proc.communicate_started.wait(), timeout=1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)

    assert proc.kill_called is True
    assert proc.wait_called is True


@pytest.mark.unit
def test_default_cleaner_uses_configured_work_dir(tmp_path: Path) -> None:
    previous = os.environ.get("AWF_WORK_DIR")
    try:
        os.environ["AWF_WORK_DIR"] = str(tmp_path)
        get_settings.cache_clear()

        cleaner = controls.default_cleaner()
        worktrees_root = controls.default_worktrees_root()

        assert cleaner._git._work_dir == tmp_path / "git"  # noqa: SLF001
        assert cleaner._compose._projects_dir == tmp_path / "compose"  # noqa: SLF001
        assert worktrees_root == tmp_path / "git" / "worktrees"
    finally:
        if previous is None:
            os.environ.pop("AWF_WORK_DIR", None)
        else:
            os.environ["AWF_WORK_DIR"] = previous
        get_settings.cache_clear()


@pytest.mark.unit
async def test_finish_version_conflict_operation_records_failed_operation_and_audit(
    engine: AsyncEngine,
) -> None:
    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await _create_control_workspace(session, status=WorkspaceStatus.running)
        operations = OperationRepository(session)
        operation = await operations.create(
            workspace_id=workspace.id,
            operation_type=OperationType.cancel,
            status=OperationStatus.running,
            payload={"stop_stack": True, "expected_version": 12},
        )
        exc = controls.VersionConflictError(expected_version=12, actual_version=13)

        await controls._finish_version_conflict_operation(  # noqa: SLF001
            session,
            operations,
            operation,
            workspace=workspace,
            exc=exc,
        )
        events = await WorkspaceEventRepository(session).list(workspace_id=workspace.id)

    assert operation.status == OperationStatus.failed.value
    assert operation.error_code == "VERSION_CONFLICT"
    assert operation.error_message == exc.message
    audit_event = next(
        event for event in events if event.event_type == "workspace.audit.control_operation"
    )
    assert audit_event.reason_code == "VERSION_CONFLICT"
    assert audit_event.payload["stop_stack"] is True
    assert audit_event.payload["expected_version"] == 12
    assert audit_event.payload["evidence"] == exc.detail


@pytest.mark.unit
async def test_preserve_precommitted_running_operation_renews_existing_only(
    engine: AsyncEngine,
) -> None:
    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await _create_control_workspace(session, status=WorkspaceStatus.destroying)
        operations = OperationRepository(session)
        operation = await operations.create(
            workspace_id=workspace.id,
            operation_type=OperationType.cancel,
            status=OperationStatus.running,
        )
        operation_id = operation.id
        await session.commit()
        before = operation.lease_renewed_at

        await controls._preserve_precommitted_running_operation(  # noqa: SLF001
            session,
            "missing-operation",
        )
        await controls._preserve_precommitted_running_operation(  # noqa: SLF001
            session,
            operation_id,
        )
        refreshed = await OperationRepository(session).get(operation_id)

    assert refreshed is not None
    assert refreshed.lease_renewed_at is not None
    assert refreshed.lease_renewed_at != before


@pytest.mark.unit
async def test_release_terminal_runtime_claim_for_control_now_logs_release_errors(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = make_session_factory(engine)
    async with factory() as session:
        service = controls.WorkspaceControlService(
            session,
            project_stopper=_RecordingStopper(),
            cleaner_factory=lambda: _RecordingCleaner(),
        )

        async def fail_release(
            self: WorkspaceRepository,
            workspace_id: str,
            *,
            owner_id: str,
        ) -> bool:
            raise RuntimeError(f"release failed for {workspace_id}:{owner_id}")

        monkeypatch.setattr(
            WorkspaceRepository,
            "release_execution_claim",
            fail_release,
        )

        with structlog.testing.capture_logs() as logs:
            await service._release_terminal_runtime_claim_for_control_now(  # noqa: SLF001
                "ws_missing",
                owner_id="terminal_runtime_release:control:test",
            )

    assert any(
        entry.get("event") == "controls.terminal_runtime_release_claim_clear_failed"
        and entry.get("workspace_id") == "ws_missing"
        and entry.get("owner_id") == "terminal_runtime_release:control:test"
        and "release failed for ws_missing:terminal_runtime_release:control:test"
        in str(entry.get("error"))
        for entry in logs
    )


@pytest.mark.unit
async def test_terminal_runtime_claim_active_for_control_handles_missing_workspace(
    engine: AsyncEngine,
) -> None:
    factory = make_session_factory(engine)
    async with factory() as session:
        service = controls.WorkspaceControlService(
            session,
            project_stopper=_RecordingStopper(),
            cleaner_factory=lambda: _RecordingCleaner(),
        )

        with pytest.raises(controls.WorkspaceNotFoundError):
            await service._terminal_runtime_release_claim_active_for_control(  # noqa: SLF001
                "ws_missing",
            )


@pytest.mark.unit
async def test_terminal_runtime_claim_active_for_control_returns_owner_with_expiring_session(
    engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=True, class_=AsyncSession)
    async with factory() as session:
        workspace = await _create_control_workspace(session, status=WorkspaceStatus.completed)
        service = controls.WorkspaceControlService(
            session,
            project_stopper=_RecordingStopper(),
            cleaner_factory=lambda: _RecordingCleaner(),
        )

        active, claim_owner_id = await service._terminal_runtime_release_claim_active_for_control(  # noqa: SLF001
            workspace.id,
        )

    assert active is False
    assert claim_owner_id is not None
    assert claim_owner_id.startswith(f"{TERMINAL_RUNTIME_RELEASE_CLAIM_OWNER_PREFIX}control:")


@pytest.mark.unit
async def test_terminal_runtime_claim_active_for_control_preserves_active_stale_cleanup_claim(
    engine: AsyncEngine,
) -> None:
    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await _create_control_workspace(session, status=WorkspaceStatus.completed)
        workspace.execution_claimed_by = "stale-cleanup:worker-1"
        workspace.execution_claim_expires_at = datetime.now(UTC) + timedelta(minutes=5)
        service = controls.WorkspaceControlService(
            session,
            project_stopper=_RecordingStopper(),
            cleaner_factory=lambda: _RecordingCleaner(),
        )

        active, claim_owner_id = await service._terminal_runtime_release_claim_active_for_control(  # noqa: SLF001
            workspace.id,
        )

    assert active
    assert claim_owner_id is None
    assert workspace.execution_claimed_by == "stale-cleanup:worker-1"


@pytest.mark.unit
async def test_release_terminal_runtime_claim_for_control_preserves_unmatched_claim(
    engine: AsyncEngine,
) -> None:
    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await _create_control_workspace(session, status=WorkspaceStatus.completed)
        workspace.execution_claimed_by = "someone-else"
        workspace.execution_claim_expires_at = datetime.now(UTC) + timedelta(minutes=5)
        service = controls.WorkspaceControlService(
            session,
            project_stopper=_RecordingStopper(),
            cleaner_factory=lambda: _RecordingCleaner(),
        )

        await service._release_terminal_runtime_claim_for_control(  # noqa: SLF001
            workspace,
            release=controls._ControlTerminalRuntimeCleanup(  # noqa: SLF001
                cleanup=controls.WorkspaceCleanupResult(
                    status="succeeded",
                    reason_code="CLEANUP_SUCCEEDED",
                ),
                preserved_worktree_host_path=None,
                claim_owner_id="terminal_runtime_release:control:mine",
            ),
        )

    assert workspace.execution_claimed_by == "someone-else"


@pytest.mark.unit
async def test_record_terminal_runtime_release_for_control_skips_nonterminal_workspace(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def record_failure(*_args: object, **_kwargs: object) -> None:
        calls.append("recorded")

    monkeypatch.setattr(
        controls,
        "record_terminal_runtime_release_event",
        record_failure,
    )
    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await _create_control_workspace(session, status=WorkspaceStatus.running)
        service = controls.WorkspaceControlService(
            session,
            project_stopper=_RecordingStopper(),
            cleaner_factory=lambda: _RecordingCleaner(),
        )

        await service._record_terminal_runtime_release_for_control(  # noqa: SLF001
            workspace,
            release=controls._ControlTerminalRuntimeCleanup(  # noqa: SLF001
                cleanup=controls.WorkspaceCleanupResult(
                    status="succeeded",
                    reason_code="CLEANUP_SUCCEEDED",
                ),
                preserved_worktree_host_path=None,
                claim_owner_id=None,
            ),
            source="unit-test",
        )

    assert calls == []


@pytest.mark.unit
async def test_finish_precommitted_control_operation_failed_handles_audit_failure(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await _create_control_workspace(session, status=WorkspaceStatus.destroying)
        operation = await OperationRepository(session).create(
            workspace_id=workspace.id,
            operation_type=OperationType.destroy,
            status=OperationStatus.running,
            payload={"stop_stack": True, "expected_version": 5},
        )
        await session.commit()

        async def fail_audit(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("audit insert failed")

        monkeypatch.setattr(controls, "_add_control_audit_event", fail_audit)

        await controls._finish_precommitted_control_operation_failed(  # noqa: SLF001
            session,
            operation_id=operation.id,
            workspace_id=workspace.id,
            exc=RuntimeError("cleanup failed"),
        )
        await session.refresh(operation)

    assert operation.status == OperationStatus.failed.value
    assert operation.error_code == "CONTROL_OPERATION_FAILED"
    assert operation.error_message == "RuntimeError: cleanup failed"


@pytest.mark.unit
async def test_stop_precommitted_failure_preserves_terminal_runtime_release_evidence(
    engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await _create_control_workspace(
            session,
            status=WorkspaceStatus.running,
            compose_project_name="awf_terminal_release_evidence",
        )
        worktrees_root = tmp_path / "worktrees"
        preserved_worktree_path = worktrees_root / workspace.id
        preserved_worktree_path.mkdir(parents=True)
        await session.commit()

        cleaner = _SequencedCleaner(
            [
                {
                    "status": "succeeded",
                    "reason_code": "CLEANUP_SUCCEEDED",
                    "completed_steps": [
                        {
                            "name": "compose_down",
                            "status": "succeeded",
                            "reason_code": "COMPOSE_DOWN_SUCCEEDED",
                        }
                    ],
                }
            ]
        )
        service = controls.WorkspaceControlService(
            session,
            project_stopper=_RecordingStopper(),
            cleaner_factory=lambda: cleaner,
            worktrees_root=worktrees_root,
            session_factory=factory,
        )
        require_workspace_for_update = service._require_workspace_for_update  # noqa: SLF001
        require_workspace_calls = 0

        async def fail_after_terminal_runtime_cleanup(
            repo: WorkspaceRepository,
            workspace_id: str,
        ) -> object:
            nonlocal require_workspace_calls
            require_workspace_calls += 1
            if require_workspace_calls == 1:
                return await require_workspace_for_update(repo, workspace_id)
            raise RuntimeError("database failed after cleanup")

        monkeypatch.setattr(
            service,
            "_require_workspace_for_update",
            fail_after_terminal_runtime_cleanup,
        )

        with pytest.raises(RuntimeError, match="database failed after cleanup"):
            await service.stop_workspace(workspace.id, reason="operator requested")

        operations = await OperationRepository(session).list_for_workspace(workspace.id)
        operation = next(item for item in operations if item.type == OperationType.stop.value)
        audit_events = await WorkspaceEventRepository(session).list(
            workspace_id=workspace.id,
            event_type="workspace.audit.control_operation",
        )

    assert operation.status == OperationStatus.failed.value
    assert operation.result is not None
    release_result = operation.result["terminal_runtime_release"]
    assert release_result["cleanup"]["reason_code"] == "CLEANUP_SUCCEEDED"
    assert release_result["preserved"]["worktree_path"] == str(preserved_worktree_path)

    audit_payload = audit_events[0].payload
    assert audit_payload is not None
    assert audit_payload["terminal_runtime_release"]["cleanup_status"] == "succeeded"
    release_evidence = audit_payload["evidence"]["terminal_runtime_release"]
    assert release_evidence["cleanup"]["completed_steps"][0]["name"] == "compose_down"
    assert release_evidence["preserved"]["worktree_path"] == str(preserved_worktree_path)


@pytest.mark.unit
async def test_teardown_operation_heartbeat_edges(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = make_session_factory(engine)
    async with factory() as session:
        service = controls.WorkspaceControlService(
            session,
            project_stopper=_RecordingStopper(),
            cleaner_factory=lambda: _RecordingCleaner(),
            session_factory=factory,
        )

        async def immediate_heartbeat_stop(
            *_args: object,
            **_kwargs: object,
        ) -> str:
            return "lease row disappeared"

        monkeypatch.setattr(
            controls,
            "_renew_runtime_teardown_operation_lease_loop",
            immediate_heartbeat_stop,
        )

        with pytest.raises(RuntimeError, match="lease row disappeared"):
            await service._run_with_teardown_operation_heartbeat(  # noqa: SLF001
                "op_lost",
                asyncio.sleep(60, result="unused"),
            )

        service._session_factory = None  # type: ignore[assignment]  # noqa: SLF001
        with pytest.raises(RuntimeError, match="session_factory"):
            await service._run_with_teardown_operation_heartbeat(  # noqa: SLF001
                "op_inline",
                asyncio.sleep(0, result="inline-result"),
            )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("mode", "expected_reason_code"),
    [
        ("raise", TERMINAL_RUNTIME_RELEASE_CLAIM_REFRESH_FAILED_REASON_CODE),
        ("lost", TERMINAL_RUNTIME_RELEASE_CLAIM_LOST_REASON_CODE),
    ],
)
async def test_terminal_runtime_release_claim_refresh_loop_stops_after_error_grace_or_lost_claim(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    expected_reason_code: str,
) -> None:
    factory = make_session_factory(engine)
    refresh_attempts = 0

    async def refresh_claim(
        self: WorkspaceRepository,
        workspace_id: str,
        *,
        owner_id: str,
        lease_expires_at: datetime,
    ) -> bool:
        nonlocal refresh_attempts
        del self, workspace_id, owner_id, lease_expires_at
        refresh_attempts += 1
        if mode == "raise":
            raise RuntimeError("database unavailable")
        return False

    monkeypatch.setattr(
        controls,
        "_terminal_runtime_release_claim_heartbeat_interval_seconds",
        lambda: 0.005,
    )
    monkeypatch.setattr(controls, "TERMINAL_RUNTIME_RELEASE_CLAIM_TTL_SECONDS", 0.015)
    monkeypatch.setattr(
        WorkspaceRepository,
        "refresh_execution_claim",
        refresh_claim,
    )

    failure = await controls._refresh_terminal_runtime_release_claim_loop(  # noqa: SLF001
        factory,
        workspace_id="ws_refresh",
        owner_id="terminal_runtime_release:control:test",
    )
    assert failure.reason_code == expected_reason_code
    if mode == "raise":
        assert refresh_attempts >= 2
        assert failure.error == "RuntimeError: database unavailable"
    else:
        assert refresh_attempts == 1
        assert failure.error is None


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
