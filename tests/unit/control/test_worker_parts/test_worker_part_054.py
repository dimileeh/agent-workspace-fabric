"""ControlWorker tests (continued from test_worker_part_053).

Split out of ``test_worker_part_053`` to keep each test module under the
first-party 1500-line maintainability guardrail.
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.control.worker import (
    ControlWorker,
    WorkerConfig,
)
from awf.control.worker import claims as worker_claims
from awf.control.worker import dispatch_methods as worker_dispatch_methods
from awf.db.enums import OperationStatus, WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from tests.unit.control.test_worker_parts.test_worker_part_053 import (
    _pending_execution_task,
    _RecordingExecutor,
)


@pytest.mark.unit
async def test_safely_resume_pr_monitor_cancellation_during_finalize_preserves_failed_recovery(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Cancellation during succeed finalize must not downgrade a failed terminal race."""
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="https://github.com/example/repo.git",
            branch_base="main",
            task_title="monitor-recovery-cancel-failed",
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        workspace_id = ws.id
        await repo.transition(ws, to=WorkspaceStatus.provisioning, reason_code="SEED")
        await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="SEED")
        await repo.transition(ws, to=WorkspaceStatus.running, reason_code="SEED")
        await repo.transition(ws, to=WorkspaceStatus.validating, reason_code="SEED")
        await repo.transition(ws, to=WorkspaceStatus.pushing, reason_code="SEED")
        await repo.transition(ws, to=WorkspaceStatus.monitoring_pr, reason_code="SEED")
        await repo.transition(ws, to=WorkspaceStatus.failed, reason_code="SEED")
        await session.commit()

    handoff = object()
    finish_calls: list[dict[str, object]] = []
    after_cancellation_calls: list[dict[str, object]] = []
    finalize_started = asyncio.Event()

    class HandoffExecutor(_RecordingExecutor):
        """Executor stub for monitor recovery handoff tests."""

        async def resume_pr_monitor_handoff(self, workspace_id_arg: str) -> object:
            """Test helper for resume pr monitor handoff."""
            assert workspace_id_arg == workspace_id
            return handoff

        async def run_resumed_pr_monitor(self, workspace_id_arg: str, handoff_obj: object) -> None:
            """Test helper for run resumed pr monitor."""
            raise AssertionError("monitor run must not start when finalize is cancelled")

    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=object(),  # type: ignore[arg-type]
        executor=HandoffExecutor(),
        config=WorkerConfig(poll_interval_seconds=0.01),
    )

    async def _finish_monitor_recovery_operation(
        workspace_id_arg: str,
        **kwargs: object,
    ) -> bool:
        """Test helper for finish monitor recovery operation."""
        finish_calls.append({"workspace_id": workspace_id_arg, **kwargs})
        finalize_started.set()
        await asyncio.Event().wait()
        return True

    async def _finish_after_cancellation(*args: object, **kwargs: object) -> None:
        """Test helper for finish after cancellation."""
        after_cancellation_calls.append(dict(kwargs))

    worker._finish_monitor_recovery_operation = (  # type: ignore[method-assign]
        _finish_monitor_recovery_operation
    )
    worker._finish_monitor_recovery_operation_after_cancellation = (  # type: ignore[method-assign]
        _finish_after_cancellation
    )

    resume_task = asyncio.create_task(
        worker._safely_resume_pr_monitor(  # noqa: SLF001
            workspace_id,
            recovery_operation_id="op_failed_cancel_finalize",
        )
    )
    await asyncio.wait_for(finalize_started.wait(), timeout=5.0)
    resume_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await resume_task

    assert len(after_cancellation_calls) == 1
    assert after_cancellation_calls[0]["status"] == OperationStatus.succeeded
    assert after_cancellation_calls[0]["operation_id"] == "op_failed_cancel_finalize"
    assert after_cancellation_calls[0]["error_code"] is None
    assert after_cancellation_calls[0]["error_message"] is None
    assert len(finish_calls) == 1


@pytest.mark.unit
async def test_safely_resume_pr_monitor_retains_handle_when_cancellation_finalize_fails(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Failed shielded cancellation finalize must keep the recovery handle for finally."""
    handoff_started = asyncio.Event()

    class BlockingHandoffExecutor(_RecordingExecutor):
        """Executor that blocks during handoff finalization."""

        async def resume_pr_monitor_handoff(self, workspace_id: str) -> object:
            """Test helper for resume pr monitor handoff."""
            assert workspace_id == "ws_monitor"
            handoff_started.set()
            await asyncio.Event().wait()
            return object()

    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=object(),  # type: ignore[arg-type]
        executor=BlockingHandoffExecutor(),
        config=WorkerConfig(poll_interval_seconds=0.01),
    )

    async def _finish_after_cancellation(*args: object, **kwargs: object) -> bool:
        """Test helper for finish after cancellation."""
        del args, kwargs
        return False

    worker._finish_monitor_recovery_operation_after_cancellation = (  # type: ignore[method-assign]
        _finish_after_cancellation
    )
    worker._monitor_recovery_operation_ids["ws_monitor"] = "op_cancel_finalize_failed"  # noqa: SLF001

    resume_task = asyncio.create_task(
        worker._safely_resume_pr_monitor(  # noqa: SLF001
            "ws_monitor",
            recovery_operation_id="op_cancel_finalize_failed",
        )
    )
    await asyncio.wait_for(handoff_started.wait(), timeout=5.0)
    resume_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await resume_task

    assert worker._monitor_recovery_operation_ids["ws_monitor"] == "op_cancel_finalize_failed"  # noqa: SLF001


@pytest.mark.unit
async def test_safely_resume_claimed_pr_monitor_releases_claim_after_cancellation_finalize(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Shielded cancellation finalize must clear recovery IDs so the claim is released."""
    handoff = object()
    finish_calls: list[dict[str, object]] = []
    finalize_started = asyncio.Event()
    claim_released = False
    finish_attempts = 0

    class HandoffExecutor(_RecordingExecutor):
        """Executor stub for monitor recovery handoff tests."""

        async def resume_pr_monitor_handoff(self, workspace_id: str) -> object:
            """Test helper for resume pr monitor handoff."""
            assert workspace_id == "ws_monitor"
            return handoff

        async def run_resumed_pr_monitor(self, workspace_id: str, handoff_obj: object) -> None:
            """Test helper for run resumed pr monitor."""
            raise AssertionError("monitor run must not start when finalize is cancelled")

    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=object(),  # type: ignore[arg-type]
        executor=HandoffExecutor(),
        config=WorkerConfig(poll_interval_seconds=0.01),
    )

    async def _finish_monitor_recovery_operation(
        workspace_id: str,
        **kwargs: object,
    ) -> bool:
        """Test helper for finish monitor recovery operation."""
        nonlocal finish_attempts
        finish_attempts += 1
        finish_calls.append({"workspace_id": workspace_id, **kwargs})
        if finish_attempts == 1:
            finalize_started.set()
            await asyncio.Event().wait()
        return True

    async def _release_monitor_claim(workspace_id: str) -> None:
        """Test helper for release monitor claim."""
        nonlocal claim_released
        assert workspace_id == "ws_monitor"
        claim_released = True

    worker._finish_monitor_recovery_operation = (  # type: ignore[method-assign]
        _finish_monitor_recovery_operation
    )
    worker._release_monitoring_pr_claim = _release_monitor_claim  # type: ignore[method-assign]
    worker._release_terminal_runtime_promptly = (  # type: ignore[method-assign]
        lambda _workspace_id: asyncio.sleep(0)
    )
    worker._monitor_recovery_operation_ids["ws_monitor"] = "op_cancel_finalize"  # noqa: SLF001

    resume_task = asyncio.create_task(
        worker._safely_resume_claimed_pr_monitor(  # noqa: SLF001
            "ws_monitor",
            recovery_operation_id="op_cancel_finalize",
        )
    )
    await asyncio.wait_for(finalize_started.wait(), timeout=5.0)
    resume_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await resume_task

    assert claim_released is True
    assert "ws_monitor" not in worker._monitor_recovery_operation_ids  # noqa: SLF001
    assert finish_attempts == 2
    assert finish_calls[0]["status"] == OperationStatus.succeeded
    assert finish_calls[1]["status"] == OperationStatus.succeeded


@pytest.mark.unit
async def test_safely_resume_claimed_pr_monitor_legacy_releases_claim_after_cancellation_finalize(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Protocol-only legacy resume must clear recovery IDs after cancelled finalize."""
    finish_calls: list[dict[str, object]] = []
    resume_started = asyncio.Event()
    claim_released = False

    class BlockingLegacyExecutor:
        """Legacy executor that blocks during resume_pr_monitor."""

        async def resume_pr_monitor(self, workspace_id: str) -> None:
            """Test helper for resume pr monitor."""
            assert workspace_id == "ws_monitor"
            resume_started.set()
            await asyncio.Event().wait()

    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=object(),  # type: ignore[arg-type]
        executor=BlockingLegacyExecutor(),  # type: ignore[arg-type]
        config=WorkerConfig(poll_interval_seconds=0.01),
    )

    async def _finish_monitor_recovery_operation(
        workspace_id: str,
        **kwargs: object,
    ) -> bool:
        """Test helper for finish monitor recovery operation."""
        finish_calls.append({"workspace_id": workspace_id, **kwargs})
        return True

    async def _release_monitor_claim(workspace_id: str) -> None:
        """Test helper for release monitor claim."""
        nonlocal claim_released
        assert workspace_id == "ws_monitor"
        claim_released = True

    worker._finish_monitor_recovery_operation = (  # type: ignore[method-assign]
        _finish_monitor_recovery_operation
    )
    worker._release_monitoring_pr_claim = _release_monitor_claim  # type: ignore[method-assign]
    worker._release_terminal_runtime_promptly = (  # type: ignore[method-assign]
        lambda _workspace_id: asyncio.sleep(0)
    )
    worker._monitor_recovery_operation_ids["ws_monitor"] = "op_legacy_cancel_finalize"  # noqa: SLF001

    resume_task = asyncio.create_task(
        worker._safely_resume_claimed_pr_monitor(  # noqa: SLF001
            "ws_monitor",
            recovery_operation_id="op_legacy_cancel_finalize",
        )
    )
    await asyncio.wait_for(resume_started.wait(), timeout=5.0)
    resume_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await resume_task

    assert claim_released is True
    assert "ws_monitor" not in worker._monitor_recovery_operation_ids  # noqa: SLF001
    assert len(finish_calls) == 1
    assert finish_calls[0]["status"] == OperationStatus.cancelled
    assert finish_calls[0]["error_code"] == "MONITOR_RECOVERY_CANCELLED"


@pytest.mark.unit
async def test_safely_resume_pr_monitor_falls_back_to_protocol_resume(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Protocol-only executors without handoff helpers still resume via resume_pr_monitor."""
    finish_calls: list[dict[str, object]] = []

    class _ProtocolOnlyExecutor:
        """Minimal executor exposing only the resume protocol."""

        def __init__(self) -> None:
            """Test helper for __init__."""
            self.resume_calls: list[str] = []

        async def execute(self, workspace_id: str, **_kwargs: object) -> None:
            """Test helper for execute."""
            del workspace_id

        async def resume_pr_monitor(self, workspace_id: str) -> None:
            """Test helper for resume pr monitor."""
            self.resume_calls.append(workspace_id)

    executor = _ProtocolOnlyExecutor()
    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=object(),  # type: ignore[arg-type]
        executor=executor,  # type: ignore[arg-type]
        config=WorkerConfig(poll_interval_seconds=0.01),
    )

    async def _finish_monitor_recovery_operation(
        workspace_id: str,
        **kwargs: object,
    ) -> bool:
        """Test helper for finish monitor recovery operation."""
        finish_calls.append({"workspace_id": workspace_id, **kwargs})
        return True

    worker._finish_monitor_recovery_operation = (  # type: ignore[method-assign]
        _finish_monitor_recovery_operation
    )

    result = await worker._safely_resume_pr_monitor(  # noqa: SLF001
        "ws_protocol",
        recovery_operation_id="op_protocol_resume",
    )

    assert result is True
    assert executor.resume_calls == ["ws_protocol"]
    assert finish_calls == [
        {
            "workspace_id": "ws_protocol",
            "operation_id": "op_protocol_resume",
            "status": OperationStatus.succeeded,
        }
    ]


@pytest.mark.unit
async def test_safely_resume_pr_monitor_falls_back_when_handoff_api_incomplete(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Executors with only handoff (no run helper) resume via resume_pr_monitor."""
    finish_calls: list[dict[str, object]] = []

    class _PartialHandoffExecutor:
        """Executor implementing only the handoff half of resume."""

        def __init__(self) -> None:
            """Test helper for __init__."""
            self.handoff_calls: list[str] = []
            self.resume_calls: list[str] = []

        async def execute(self, workspace_id: str, **_kwargs: object) -> None:
            """Test helper for execute."""
            del workspace_id

        async def resume_pr_monitor_handoff(self, workspace_id: str) -> object:
            """Test helper for resume pr monitor handoff."""
            self.handoff_calls.append(workspace_id)
            return object()

        async def resume_pr_monitor(self, workspace_id: str) -> None:
            """Test helper for resume pr monitor."""
            self.resume_calls.append(workspace_id)

    executor = _PartialHandoffExecutor()
    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=object(),  # type: ignore[arg-type]
        executor=executor,  # type: ignore[arg-type]
        config=WorkerConfig(poll_interval_seconds=0.01),
    )

    async def _finish_monitor_recovery_operation(
        workspace_id: str,
        **kwargs: object,
    ) -> bool:
        """Test helper for finish monitor recovery operation."""
        finish_calls.append({"workspace_id": workspace_id, **kwargs})
        return True

    worker._finish_monitor_recovery_operation = (  # type: ignore[method-assign]
        _finish_monitor_recovery_operation
    )

    result = await worker._safely_resume_pr_monitor(  # noqa: SLF001
        "ws_partial_handoff",
        recovery_operation_id="op_partial_handoff",
    )

    assert result is True
    assert executor.handoff_calls == []
    assert executor.resume_calls == ["ws_partial_handoff"]
    assert finish_calls == [
        {
            "workspace_id": "ws_partial_handoff",
            "operation_id": "op_partial_handoff",
            "status": OperationStatus.succeeded,
        }
    ]


@pytest.mark.unit
async def test_monitor_recovery_handoff_failure_error_skips_restart_start_event(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Recovery start events must not mask the real handoff abort reason."""
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="https://github.com/example/repo.git",
            branch_base="main",
            task_title="monitor-recovery-handoff-failure",
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        workspace_id = ws.id
        await repo.add_event(
            ws,
            event_type="workspace.monitor_recovery_started",
            reason_code="MONITOR_RECOVERY_AFTER_RESTART",
            payload={"operation_id": "op-recovery"},
        )
        await session.commit()

    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=object(),  # type: ignore[arg-type]
        config=WorkerConfig(poll_interval_seconds=0.01),
    )
    (
        error_code,
        error_message,
    ) = await worker_dispatch_methods._monitor_recovery_handoff_failure_error(  # noqa: SLF001
        worker,
        workspace_id,
    )
    assert error_code == "MONITOR_RECOVERY_FAILED"
    assert "failed" in error_message.lower()

    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get(workspace_id)
        assert ws is not None
        await repo.add_event(
            ws,
            event_type="workspace.monitor_runtime_restart_failed",
            reason_code="MONITOR_RECOVERY_COMPOSE_FAILED",
            payload={"reason_code": "MONITOR_RECOVERY_COMPOSE_FAILED"},
        )
        await session.commit()

    error_code, _ = await worker_dispatch_methods._monitor_recovery_handoff_failure_error(  # noqa: SLF001
        worker,
        workspace_id,
    )
    assert error_code == "MONITOR_RECOVERY_COMPOSE_FAILED"


@pytest.mark.unit
async def test_monitor_recovery_handoff_failure_error_prefers_latest_failure_event(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The newest handoff failure reason must win over stale prior attempts."""
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="https://github.com/example/repo.git",
            branch_base="main",
            task_title="monitor-recovery-handoff-failure-latest",
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        workspace_id = ws.id
        await repo.add_event(
            ws,
            event_type="workspace.monitor_runtime_restart_failed",
            reason_code="MONITOR_RECOVERY_COMPOSE_FAILED",
            payload={"reason_code": "MONITOR_RECOVERY_COMPOSE_FAILED"},
        )
        await repo.add_event(
            ws,
            event_type="workspace.monitor_recovery_started",
            reason_code="MONITOR_RECOVERY_AFTER_RESTART",
            payload={"operation_id": "op-recovery-retry"},
        )
        await repo.add_event(
            ws,
            event_type="workspace.monitor_runtime_restart_failed",
            reason_code="MONITOR_RECOVERY_METADATA_MISSING",
            payload={"reason_code": "MONITOR_RECOVERY_METADATA_MISSING"},
        )
        await session.commit()

    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=object(),  # type: ignore[arg-type]
        config=WorkerConfig(poll_interval_seconds=0.01),
    )
    error_code, _ = await worker_dispatch_methods._monitor_recovery_handoff_failure_error(  # noqa: SLF001
        worker,
        workspace_id,
    )
    assert error_code == "MONITOR_RECOVERY_METADATA_MISSING"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("reason_code",),
    [
        ("DEPRECATED_TASK_KIND",),
        ("UNSUPPORTED_TASK_KIND",),
    ],
)
async def test_monitor_recovery_handoff_failure_error_preserves_policy_task_kind_codes(
    session_factory: async_sessionmaker[AsyncSession],
    reason_code: str,
) -> None:
    """Policy rejections during monitor handoff must surface on the remonitor op."""
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="https://github.com/example/repo.git",
            branch_base="main",
            task_title="monitor-recovery-handoff-policy-failure",
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        workspace_id = ws.id
        await repo.add_event(
            ws,
            event_type="workspace.monitor_recovery_started",
            reason_code="MONITOR_RECOVERY_AFTER_RESTART",
            payload={"operation_id": "op-recovery"},
        )
        await repo.add_event(
            ws,
            event_type="workspace.failed",
            reason_code=reason_code,
            payload={"reason_code": reason_code},
        )
        await session.commit()

    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=object(),  # type: ignore[arg-type]
        config=WorkerConfig(poll_interval_seconds=0.01),
    )
    error_code, _ = await worker_dispatch_methods._monitor_recovery_handoff_failure_error(  # noqa: SLF001
        worker,
        workspace_id,
    )
    assert error_code == reason_code


@pytest.mark.unit
async def test_monitor_recovery_handoff_failure_error_uses_event_message_not_stale_workspace_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Audit-only handoff failures must not reuse an unrelated workspace failure_message."""
    stale_message = "Stale failure from an earlier unrelated terminal failure."
    compose_stderr = "compose up failed: service postgres unhealthy"
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="https://github.com/example/repo.git",
            branch_base="main",
            task_title="monitor-recovery-handoff-failure-message",
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        workspace_id = ws.id
        ws.failure_message = stale_message
        await repo.add_event(
            ws,
            event_type="workspace.monitor_runtime_restart_failed",
            reason_code="MONITOR_RECOVERY_COMPOSE_FAILED",
            payload={
                "reason_code": "MONITOR_RECOVERY_COMPOSE_FAILED",
                "operation": "up",
                "stderr": compose_stderr,
            },
        )
        await session.commit()

    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=object(),  # type: ignore[arg-type]
        config=WorkerConfig(poll_interval_seconds=0.01),
    )
    (
        error_code,
        error_message,
    ) = await worker_dispatch_methods._monitor_recovery_handoff_failure_error(  # noqa: SLF001
        worker,
        workspace_id,
    )
    assert error_code == "MONITOR_RECOVERY_COMPOSE_FAILED"
    assert error_message == compose_stderr
    assert stale_message not in error_message


@pytest.mark.unit
@pytest.mark.parametrize(
    ("event_reason_code", "payload_reason_code"),
    [
        ("MONITOR_RECOVERY_COMPOSE_FAILED", "DOCKER_UNAVAILABLE"),
        ("MONITOR_RECOVERY_PRECHECK_FAILED", "COMPANION_ENV_SECRET_SOURCE_MISSING"),
    ],
)
async def test_monitor_recovery_handoff_failure_error_prefers_payload_reason_code(
    session_factory: async_sessionmaker[AsyncSession],
    event_reason_code: str,
    payload_reason_code: str,
) -> None:
    """Concrete compose/precheck failure codes must surface on the remonitor op."""
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="https://github.com/example/repo.git",
            branch_base="main",
            task_title="monitor-recovery-handoff-payload-reason",
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        workspace_id = ws.id
        await repo.add_event(
            ws,
            event_type="workspace.monitor_runtime_restart_failed",
            reason_code=event_reason_code,
            payload={
                "reason_code": payload_reason_code,
                "operation": "up",
                "stderr": "compose handoff failed",
            },
        )
        await session.commit()

    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=object(),  # type: ignore[arg-type]
        config=WorkerConfig(poll_interval_seconds=0.01),
    )
    error_code, _ = await worker_dispatch_methods._monitor_recovery_handoff_failure_error(  # noqa: SLF001
        worker,
        workspace_id,
    )
    assert error_code == payload_reason_code


@pytest.mark.unit
def test_monitor_recovery_handoff_failure_error_code_prefers_payload() -> None:
    """Verify monitor recovery handoff failure error code prefers payload."""
    event = SimpleNamespace(
        reason_code="MONITOR_RECOVERY_COMPOSE_FAILED",
        payload={"reason_code": "DOCKER_UNAVAILABLE"},
    )
    assert (
        worker_dispatch_methods._monitor_recovery_handoff_failure_error_code(event)  # noqa: SLF001
        == "DOCKER_UNAVAILABLE"
    )


@pytest.mark.unit
def test_monitor_recovery_handoff_failure_error_code_falls_back_to_event_reason() -> None:
    """Verify monitor recovery handoff failure error code falls back to event reason."""
    event = SimpleNamespace(
        reason_code="MONITOR_RECOVERY_METADATA_MISSING",
        payload={},
    )
    assert (
        worker_dispatch_methods._monitor_recovery_handoff_failure_error_code(event)  # noqa: SLF001
        == "MONITOR_RECOVERY_METADATA_MISSING"
    )


@pytest.mark.unit
def test_monitor_recovery_handoff_failure_message_prefers_payload_message() -> None:
    """Verify monitor recovery handoff failure message prefers payload message."""
    event = SimpleNamespace(payload={"message": "  compose handoff rejected  "})
    workspace = SimpleNamespace(
        status=WorkspaceStatus.monitoring_pr.value,
        failure_message="ignored stale row",
    )
    message = worker_dispatch_methods._monitor_recovery_handoff_failure_message(  # noqa: SLF001
        event,
        workspace=workspace,
        default_message="default",
    )
    assert message == "  compose handoff rejected  "


@pytest.mark.unit
def test_monitor_recovery_handoff_failure_message_uses_operation_when_no_text_fields() -> None:
    """Verify monitor recovery handoff failure message uses operation when no text fields."""
    event = SimpleNamespace(payload={"operation": "compose up"})
    workspace = SimpleNamespace(status=WorkspaceStatus.monitoring_pr.value, failure_message=None)
    message = worker_dispatch_methods._monitor_recovery_handoff_failure_message(  # noqa: SLF001
        event,
        workspace=workspace,
        default_message="default",
    )
    assert message == "Monitor recovery handoff failed during compose up."


@pytest.mark.unit
async def test_monitor_recovery_handoff_failure_error_skips_null_reason_events(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Verify monitor recovery handoff failure error skips null reason events."""
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="https://github.com/example/repo.git",
            branch_base="main",
            task_title="monitor-recovery-null-reason-event",
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        workspace_id = ws.id
        await repo.add_event(
            ws,
            event_type="workspace.monitor_runtime_restart_failed",
            reason_code=None,
            payload={"stderr": "ignored because reason_code is null"},
        )
        await repo.add_event(
            ws,
            event_type="workspace.monitor_runtime_restart_failed",
            reason_code="MONITOR_RECOVERY_COMPOSE_FAILED",
            payload={"message": "compose up failed"},
        )
        await session.commit()

    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=object(),  # type: ignore[arg-type]
        config=WorkerConfig(poll_interval_seconds=0.01),
    )
    (
        error_code,
        error_message,
    ) = await worker_dispatch_methods._monitor_recovery_handoff_failure_error(  # noqa: SLF001
        worker,
        workspace_id,
    )
    assert error_code == "MONITOR_RECOVERY_COMPOSE_FAILED"
    assert error_message == "compose up failed"


@pytest.mark.unit
async def test_monitor_recovery_handoff_failure_error_uses_workspace_failure_message(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Verify monitor recovery handoff failure error uses workspace failure message."""
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="https://github.com/example/repo.git",
            branch_base="main",
            task_title="monitor-recovery-failed-workspace-message",
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        workspace_id = ws.id
        ws.failure_message = "Monitor handoff aborted after validation failure."
        await repo.transition(ws, to=WorkspaceStatus.failed, reason_code="SEED")
        await session.commit()

    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=object(),  # type: ignore[arg-type]
        config=WorkerConfig(poll_interval_seconds=0.01),
    )
    (
        error_code,
        error_message,
    ) = await worker_dispatch_methods._monitor_recovery_handoff_failure_error(  # noqa: SLF001
        worker,
        workspace_id,
    )
    assert error_code == "MONITOR_RECOVERY_FAILED"
    assert error_message == "Monitor handoff aborted after validation failure."


@pytest.mark.unit
async def test_monitor_recovery_handoff_failure_error_lookup_exception_returns_default(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify monitor recovery handoff failure error lookup exception returns default."""
    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=object(),  # type: ignore[arg-type]
        config=WorkerConfig(poll_interval_seconds=0.01),
    )

    class _BrokenRepo:
        """Repository stub that raises on get for finalize error paths."""

        async def get(self, workspace_id: str) -> None:
            """Test helper for get."""
            del workspace_id
            raise RuntimeError("lookup failed")

    monkeypatch.setattr(
        worker_dispatch_methods,
        "WorkspaceRepository",
        lambda _session: _BrokenRepo(),
    )

    (
        error_code,
        error_message,
    ) = await worker_dispatch_methods._monitor_recovery_handoff_failure_error(  # noqa: SLF001
        worker,
        "ws_missing",
    )
    assert error_code == "MONITOR_RECOVERY_FAILED"
    assert error_message == "Monitor recovery handoff failed."


@pytest.mark.unit
async def test_monitor_recovery_start_skipped_operation_status_lookup_exception_returns_failed(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify monitor recovery start skipped operation status lookup exception returns failed."""
    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=object(),  # type: ignore[arg-type]
        config=WorkerConfig(poll_interval_seconds=0.01),
    )

    class _BrokenRepo:
        """Repository stub that raises on get for finalize error paths."""

        async def get(self, workspace_id: str) -> None:
            """Test helper for get."""
            del workspace_id
            raise RuntimeError("lookup failed")

    monkeypatch.setattr(
        worker_dispatch_methods,
        "WorkspaceRepository",
        lambda _session: _BrokenRepo(),
    )
    monkeypatch.setattr(
        worker_dispatch_methods,
        "_monitor_recovery_handoff_failure_error",
        AsyncMock(return_value=("MONITOR_RECOVERY_FAILED", "derived failure")),
    )

    (
        status,
        error_code,
        error_message,
    ) = await worker_dispatch_methods._monitor_recovery_start_skipped_operation_status(  # noqa: SLF001
        worker,
        "ws_missing",
    )
    assert status == OperationStatus.failed
    assert error_code == "MONITOR_RECOVERY_FAILED"
    assert error_message == "derived failure"


@pytest.mark.unit
async def test_monitor_recovery_terminal_finalize_status_handles_missing_and_cancelled(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Verify monitor recovery terminal finalize status handles missing and cancelled."""
    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=object(),  # type: ignore[arg-type]
        config=WorkerConfig(poll_interval_seconds=0.01),
    )
    (
        missing_status,
        missing_code,
        missing_message,
    ) = await worker_claims._monitor_recovery_terminal_finalize_status(  # noqa: SLF001
        worker,
        "ws_missing",
    )
    assert missing_status == OperationStatus.failed
    assert missing_code == "MONITOR_RECOVERY_FAILED"
    assert "could not be finalized" in (missing_message or "")

    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="https://github.com/example/repo.git",
            branch_base="main",
            task_title="monitor-recovery-terminal-cancelled",
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        cancelled_workspace_id = ws.id
        await repo.transition(ws, to=WorkspaceStatus.cancelled, reason_code="TEST_OPERATOR")
        await session.commit()

    (
        cancelled_status,
        cancelled_code,
        cancelled_message,
    ) = await worker_claims._monitor_recovery_terminal_finalize_status(  # noqa: SLF001
        worker,
        cancelled_workspace_id,
    )
    assert cancelled_status == OperationStatus.cancelled
    assert cancelled_code == "MONITOR_RECOVERY_CANCELLED"
    assert cancelled_message == "Monitor recovery abandoned after workspace cancellation."


@pytest.mark.unit
async def test_monitor_recovery_terminal_finalize_status_marks_left_monitoring_pr_cancelled(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Verify monitor recovery terminal finalize status marks left monitoring pr cancelled."""
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="https://github.com/example/repo.git",
            branch_base="main",
            task_title="monitor-recovery-terminal-ready",
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        workspace_id = ws.id
        await repo.transition(ws, to=WorkspaceStatus.provisioning, reason_code="SEED")
        await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="SEED")
        await session.commit()

    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=object(),  # type: ignore[arg-type]
        config=WorkerConfig(poll_interval_seconds=0.01),
    )
    (
        status,
        error_code,
        error_message,
    ) = await worker_claims._monitor_recovery_terminal_finalize_status(  # noqa: SLF001
        worker,
        workspace_id,
    )
    assert status == OperationStatus.cancelled
    assert error_code == "MONITOR_RECOVERY_CANCELLED"
    assert error_message == "Monitor resume cancelled after workspace left monitoring_pr."


@pytest.mark.unit
async def test_safely_resume_pr_monitor_legacy_failure_clears_recovery_handle(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Verify safely resume pr monitor legacy failure clears recovery handle."""
    finish_calls: list[dict[str, object]] = []

    class _RaisingLegacyExecutor:
        """Legacy executor whose resume_pr_monitor raises."""

        async def resume_pr_monitor(self, workspace_id: str) -> None:
            """Test helper for resume pr monitor."""
            del workspace_id
            raise RuntimeError("legacy resume failed")

    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=object(),  # type: ignore[arg-type]
        executor=_RaisingLegacyExecutor(),  # type: ignore[arg-type]
        config=WorkerConfig(poll_interval_seconds=0.01),
    )

    async def _finish_monitor_recovery_operation(
        workspace_id: str,
        **kwargs: object,
    ) -> bool:
        """Test helper for finish monitor recovery operation."""
        finish_calls.append({"workspace_id": workspace_id, **kwargs})
        return True

    worker._finish_monitor_recovery_operation = (  # type: ignore[method-assign]
        _finish_monitor_recovery_operation
    )
    worker._monitor_recovery_operation_ids["ws_legacy"] = "op_legacy_fail"  # noqa: SLF001

    result = await worker._safely_resume_pr_monitor(  # noqa: SLF001
        "ws_legacy",
        recovery_operation_id="op_legacy_fail",
    )

    assert result is False
    assert "ws_legacy" not in worker._monitor_recovery_operation_ids  # noqa: SLF001
    assert finish_calls == [
        {
            "workspace_id": "ws_legacy",
            "operation_id": "op_legacy_fail",
            "status": OperationStatus.failed,
            "error_code": "MONITOR_RECOVERY_FAILED",
            "error_message": "RuntimeError('legacy resume failed')",
        }
    ]


@pytest.mark.unit
async def test_safely_resume_pr_monitor_legacy_failure_retains_handle_when_finalize_fails(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Verify safely resume pr monitor legacy failure retains handle when finalize fails."""

    class _RaisingLegacyExecutor:
        """Legacy executor whose resume_pr_monitor raises."""

        async def resume_pr_monitor(self, workspace_id: str) -> None:
            """Test helper for resume pr monitor."""
            del workspace_id
            raise RuntimeError("legacy resume failed")

    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=object(),  # type: ignore[arg-type]
        executor=_RaisingLegacyExecutor(),  # type: ignore[arg-type]
        config=WorkerConfig(poll_interval_seconds=0.01),
    )

    async def _finish_monitor_recovery_operation(
        workspace_id: str,
        **kwargs: object,
    ) -> bool:
        """Test helper for finish monitor recovery operation."""
        del workspace_id, kwargs
        return False

    worker._finish_monitor_recovery_operation = (  # type: ignore[method-assign]
        _finish_monitor_recovery_operation
    )
    worker._monitor_recovery_operation_ids["ws_legacy"] = "op_legacy_fail"  # noqa: SLF001

    result = await worker._safely_resume_pr_monitor(  # noqa: SLF001
        "ws_legacy",
        recovery_operation_id="op_legacy_fail",
    )

    assert result is False
    assert worker._monitor_recovery_operation_ids["ws_legacy"] == "op_legacy_fail"  # noqa: SLF001


@pytest.mark.unit
async def test_safely_resume_pr_monitor_handoff_exception_clears_recovery_handle(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Verify safely resume pr monitor handoff exception clears recovery handle."""
    finish_calls: list[dict[str, object]] = []

    class _RaisingHandoffExecutor(_RecordingExecutor):
        """Executor whose handoff raises during recovery."""

        async def resume_pr_monitor_handoff(self, workspace_id: str) -> object:
            """Test helper for resume pr monitor handoff."""
            del workspace_id
            raise RuntimeError("handoff blew up")

    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=object(),  # type: ignore[arg-type]
        executor=_RaisingHandoffExecutor(),
        config=WorkerConfig(poll_interval_seconds=0.01),
    )

    async def _finish_monitor_recovery_operation(
        workspace_id: str,
        **kwargs: object,
    ) -> bool:
        """Test helper for finish monitor recovery operation."""
        finish_calls.append({"workspace_id": workspace_id, **kwargs})
        return True

    worker._finish_monitor_recovery_operation = (  # type: ignore[method-assign]
        _finish_monitor_recovery_operation
    )
    worker._monitor_recovery_operation_ids["ws_handoff"] = "op_handoff_exc"  # noqa: SLF001

    result = await worker._safely_resume_pr_monitor(  # noqa: SLF001
        "ws_handoff",
        recovery_operation_id="op_handoff_exc",
    )

    assert result is False
    assert "ws_handoff" not in worker._monitor_recovery_operation_ids  # noqa: SLF001
    assert finish_calls == [
        {
            "workspace_id": "ws_handoff",
            "operation_id": "op_handoff_exc",
            "status": OperationStatus.failed,
            "error_code": "MONITOR_RECOVERY_FAILED",
            "error_message": "RuntimeError('handoff blew up')",
        }
    ]


@pytest.mark.unit
async def test_safely_resume_pr_monitor_handoff_exception_retains_handle_when_finalize_fails(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Verify safely resume pr monitor handoff exception retains handle when finalize fails."""

    class _RaisingHandoffExecutor(_RecordingExecutor):
        """Executor whose handoff raises during recovery."""

        async def resume_pr_monitor_handoff(self, workspace_id: str) -> object:
            """Test helper for resume pr monitor handoff."""
            del workspace_id
            raise RuntimeError("handoff blew up")

    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=object(),  # type: ignore[arg-type]
        executor=_RaisingHandoffExecutor(),
        config=WorkerConfig(poll_interval_seconds=0.01),
    )

    async def _finish_monitor_recovery_operation(
        workspace_id: str,
        **kwargs: object,
    ) -> bool:
        """Test helper for finish monitor recovery operation."""
        del workspace_id, kwargs
        return False

    worker._finish_monitor_recovery_operation = (  # type: ignore[method-assign]
        _finish_monitor_recovery_operation
    )
    worker._monitor_recovery_operation_ids["ws_handoff"] = "op_handoff_exc"  # noqa: SLF001

    result = await worker._safely_resume_pr_monitor(  # noqa: SLF001
        "ws_handoff",
        recovery_operation_id="op_handoff_exc",
    )

    assert result is False
    assert worker._monitor_recovery_operation_ids["ws_handoff"] == "op_handoff_exc"  # noqa: SLF001


@pytest.mark.unit
async def test_safely_provision_isolates_epoch_read_failure(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A transient failure on the fencing epoch read must not abort the batch.

    ``run_once`` gathers provision tasks with ``return_exceptions=False``, so an
    exception escaping ``_safely_provision_claimed`` would propagate and wedge
    the rest of the cycle. The epoch read (D2) sits outside the inner provision
    try/except, so it must be isolated like a provision failure — logged and
    swallowed — and the claim released so the stale-active execution recovery
    scan can pick up the released ``provisioning`` row (the normal poll only
    claims ``requested`` rows).
    """
    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=object(),  # type: ignore[arg-type]
        config=WorkerConfig(poll_interval_seconds=0.01),
    )

    async def _raising_read(workspace_id: str) -> int | None:
        """Test helper for raising read."""
        assert workspace_id == "ws_epoch"
        raise RuntimeError("transient db disconnect")

    released: list[str] = []

    async def _release(workspace_id: str) -> None:
        """Test helper for release."""
        released.append(workspace_id)

    worker._read_execution_claim_epoch = _raising_read  # type: ignore[method-assign]
    worker._release_execution_claim_after_cancellation = _release  # type: ignore[method-assign]

    # Must not raise: the failure is isolated, not propagated to the gather().
    await worker._safely_provision_claimed("ws_epoch")  # noqa: SLF001

    # The claim was still released so stale-active recovery can reclaim the row.
    assert released == ["ws_epoch"]
    assert "ws_epoch" not in worker._execution_claim_epochs  # noqa: SLF001


@pytest.mark.unit
async def test_safely_provision_propagates_cancel_on_epoch_read(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An external cancel during the epoch read must still propagate.

    Only non-cancellation failures are isolated; cooperative cancellation (e.g.
    worker shutdown cancelling ``run_once``'s gather) must never be suppressed,
    and the claim is still released via the outer ``finally``.
    """
    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=object(),  # type: ignore[arg-type]
        config=WorkerConfig(poll_interval_seconds=0.01),
    )

    async def _cancelled_read(workspace_id: str) -> int | None:
        """Test helper for cancelled read."""
        raise asyncio.CancelledError

    released: list[str] = []

    async def _release(workspace_id: str) -> None:
        """Test helper for release."""
        released.append(workspace_id)

    worker._read_execution_claim_epoch = _cancelled_read  # type: ignore[method-assign]
    worker._release_execution_claim_after_cancellation = _release  # type: ignore[method-assign]

    with pytest.raises(asyncio.CancelledError):
        await worker._safely_provision_claimed("ws_cancel")  # noqa: SLF001

    assert released == ["ws_cancel"]


@pytest.mark.unit
async def test_claim_monitoring_pr_ids_respects_limit_and_existing_tasks(
    worker: ControlWorker,
) -> None:
    """Verify claim monitoring pr ids respects limit and existing tasks."""
    claim_calls: list[str] = []

    async def _claim_monitoring_pr(workspace_id: str) -> bool:
        """Test helper for claim monitoring pr."""
        claim_calls.append(workspace_id)
        return True

    worker._claim_monitoring_pr = _claim_monitoring_pr  # type: ignore[method-assign]  # noqa: SLF001

    assert (
        await worker._claim_monitoring_pr_ids(["first", "second"], limit=1)  # noqa: SLF001
        == ["first"]
    )
    assert claim_calls == ["first"]

    existing_task = asyncio.create_task(_pending_execution_task())
    try:
        worker._execution_tasks["existing"] = existing_task  # noqa: SLF001
        claim_calls.clear()

        assert (
            await worker._claim_monitoring_pr_ids(["existing", "next"], limit=2)  # noqa: SLF001
            == ["next"]
        )
        assert claim_calls == ["next"]
    finally:
        existing_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await existing_task
        worker._execution_tasks.clear()  # noqa: SLF001
