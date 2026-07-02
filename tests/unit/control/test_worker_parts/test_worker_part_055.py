"""ControlWorker monitor-recovery cancellation tests (continued from test_worker_part_053).

Split out of ``test_worker_part_053`` to keep each test module under the
first-party 1500-line maintainability guardrail.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.control.worker import (
    ControlWorker,
    WorkerConfig,
)
from awf.db.enums import OperationStatus, WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from tests.postgres import postgres_test_engine
from tests.unit.control.test_worker_parts.test_worker_part_053 import (
    _RecordingExecutor,
)


@pytest.fixture
async def session_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Yield a Postgres-backed async session factory for worker tests."""
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


@pytest.mark.unit
async def test_safely_resume_pr_monitor_cancellation_during_succeed_finalize_shielded(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Cancellation after handoff but before succeed finalize uses the shielded helper."""
    handoff = object()
    finish_calls: list[dict[str, object]] = []
    finalize_started = asyncio.Event()
    first_finalize_blocked = asyncio.Event()
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
            await first_finalize_blocked.wait()
        return True

    worker._finish_monitor_recovery_operation = (  # type: ignore[method-assign]
        _finish_monitor_recovery_operation
    )

    resume_task = asyncio.create_task(
        worker._safely_resume_pr_monitor(  # noqa: SLF001
            "ws_monitor",
            recovery_operation_id="op_during_finalize",
        )
    )
    try:
        await asyncio.wait_for(finalize_started.wait(), timeout=5.0)
        resume_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await resume_task
    finally:
        first_finalize_blocked.set()

    assert finish_attempts == 2
    assert len(finish_calls) == 2
    assert finish_calls[0]["status"] == OperationStatus.succeeded
    assert finish_calls[0]["operation_id"] == "op_during_finalize"
    assert finish_calls[1]["status"] == OperationStatus.succeeded
    assert finish_calls[1]["operation_id"] == "op_during_finalize"


@pytest.mark.unit
async def test_safely_resume_pr_monitor_cancellation_during_finalize_preserves_completed_recovery(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Cancellation during succeed finalize must not downgrade a completed workspace."""
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="https://github.com/example/repo.git",
            branch_base="main",
            task_title="monitor-recovery-cancel-completed",
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
        await repo.transition(ws, to=WorkspaceStatus.completed, reason_code="SEED")
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
            recovery_operation_id="op_completed_cancel_finalize",
        )
    )
    await asyncio.wait_for(finalize_started.wait(), timeout=5.0)
    resume_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await resume_task

    assert len(after_cancellation_calls) == 1
    assert after_cancellation_calls[0]["status"] == OperationStatus.succeeded
    assert after_cancellation_calls[0]["operation_id"] == "op_completed_cancel_finalize"
    assert after_cancellation_calls[0]["error_code"] is None
    assert after_cancellation_calls[0]["error_message"] is None
    assert len(finish_calls) == 1


@pytest.mark.unit
async def test_safely_resume_pr_monitor_cancellation_during_verify_skip_finalize_preserves_completed_recovery(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Cancellation during verify-start skip finalize must honor the handoff marker."""
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="https://github.com/example/repo.git",
            branch_base="main",
            task_title="monitor-recovery-verify-skip-cancel-completed",
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
        await repo.transition(ws, to=WorkspaceStatus.completed, reason_code="SEED")
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

        async def verify_resume_monitor_start(self, workspace_id_arg: str) -> bool:
            """Test helper for verify resume monitor start."""
            assert workspace_id_arg == workspace_id
            return False

        async def run_resumed_pr_monitor(self, workspace_id_arg: str, handoff_obj: object) -> None:
            """Test helper for run resumed pr monitor."""
            raise AssertionError("monitor run must not start when verify-start skip finalizes")

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
            recovery_operation_id="op_verify_skip_cancel_finalize",
        )
    )
    await asyncio.wait_for(finalize_started.wait(), timeout=5.0)
    resume_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await resume_task

    assert len(after_cancellation_calls) == 1
    assert after_cancellation_calls[0]["status"] == OperationStatus.succeeded
    assert after_cancellation_calls[0]["operation_id"] == "op_verify_skip_cancel_finalize"
    assert after_cancellation_calls[0]["error_code"] is None
    assert after_cancellation_calls[0]["error_message"] is None
    assert len(finish_calls) == 1


@pytest.mark.unit
async def test_safely_resume_pr_monitor_cancellation_during_verify_start_preserves_completed_recovery(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Cancellation during verify-start await must honor the pre-verify handoff marker."""
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="https://github.com/example/repo.git",
            branch_base="main",
            task_title="monitor-recovery-verify-await-cancel-completed",
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
        await repo.transition(ws, to=WorkspaceStatus.completed, reason_code="SEED")
        await session.commit()

    handoff = object()
    after_cancellation_calls: list[dict[str, object]] = []
    verify_started = asyncio.Event()

    class HandoffExecutor(_RecordingExecutor):
        """Executor stub for monitor recovery handoff tests."""

        async def resume_pr_monitor_handoff(self, workspace_id_arg: str) -> object:
            """Test helper for resume pr monitor handoff."""
            assert workspace_id_arg == workspace_id
            return handoff

        async def verify_resume_monitor_start(self, workspace_id_arg: str) -> bool:
            """Test helper for verify resume monitor start."""
            assert workspace_id_arg == workspace_id
            verify_started.set()
            await asyncio.Event().wait()
            return True

        async def run_resumed_pr_monitor(self, workspace_id_arg: str, handoff_obj: object) -> None:
            """Test helper for run resumed pr monitor."""
            raise AssertionError("monitor run must not start when verify-start is cancelled")

    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=object(),  # type: ignore[arg-type]
        executor=HandoffExecutor(),
        config=WorkerConfig(poll_interval_seconds=0.01),
    )

    async def _finish_after_cancellation(*args: object, **kwargs: object) -> None:
        """Test helper for finish after cancellation."""
        after_cancellation_calls.append(dict(kwargs))

    worker._finish_monitor_recovery_operation_after_cancellation = (  # type: ignore[method-assign]
        _finish_after_cancellation
    )

    resume_task = asyncio.create_task(
        worker._safely_resume_pr_monitor(  # noqa: SLF001
            workspace_id,
            recovery_operation_id="op_verify_await_cancel",
        )
    )
    await asyncio.wait_for(verify_started.wait(), timeout=5.0)
    resume_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await resume_task

    assert len(after_cancellation_calls) == 1
    assert after_cancellation_calls[0]["status"] == OperationStatus.succeeded
    assert after_cancellation_calls[0]["operation_id"] == "op_verify_await_cancel"
    assert after_cancellation_calls[0]["error_code"] is None
    assert after_cancellation_calls[0]["error_message"] is None


@pytest.mark.unit
async def test_safely_resume_pr_monitor_cancellation_during_verify_start_preserves_pending_recovery(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Cancellation during verify-start await must retain pending recovery while still monitoring."""
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="https://github.com/example/repo.git",
            branch_base="main",
            task_title="monitor-recovery-verify-await-cancel-monitoring",
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
        await session.commit()

    handoff = object()
    after_cancellation_calls: list[dict[str, object]] = []
    verify_started = asyncio.Event()

    class HandoffExecutor(_RecordingExecutor):
        """Executor stub for monitor recovery handoff tests."""

        async def resume_pr_monitor_handoff(self, workspace_id_arg: str) -> object:
            """Test helper for resume pr monitor handoff."""
            assert workspace_id_arg == workspace_id
            return handoff

        async def verify_resume_monitor_start(self, workspace_id_arg: str) -> bool:
            """Test helper for verify resume monitor start."""
            assert workspace_id_arg == workspace_id
            verify_started.set()
            await asyncio.Event().wait()
            return True

        async def run_resumed_pr_monitor(self, workspace_id_arg: str, handoff_obj: object) -> None:
            """Test helper for run resumed pr monitor."""
            raise AssertionError("monitor run must not start when verify-start is cancelled")

    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=object(),  # type: ignore[arg-type]
        executor=HandoffExecutor(),
        config=WorkerConfig(poll_interval_seconds=0.01),
    )
    worker._monitor_recovery_operation_ids[workspace_id] = "op_verify_await_cancel_monitoring"  # noqa: SLF001

    async def _finish_after_cancellation(*args: object, **kwargs: object) -> None:
        """Test helper for finish after cancellation."""
        after_cancellation_calls.append(dict(kwargs))

    worker._finish_monitor_recovery_operation_after_cancellation = (  # type: ignore[method-assign]
        _finish_after_cancellation
    )

    resume_task = asyncio.create_task(
        worker._safely_resume_pr_monitor(  # noqa: SLF001
            workspace_id,
            recovery_operation_id="op_verify_await_cancel_monitoring",
        )
    )
    await asyncio.wait_for(verify_started.wait(), timeout=5.0)
    resume_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await resume_task

    assert after_cancellation_calls == []
    assert workspace_id in worker._monitor_recovery_operation_ids  # noqa: SLF001
    assert workspace_id not in worker._monitor_recovery_handoff_succeeded_workspace_ids  # noqa: SLF001


@pytest.mark.unit
async def test_safely_resume_pr_monitor_cancellation_during_verify_skip_finalize_marks_still_monitoring_failed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Cancellation during verify-start skip finalize must not succeed while still monitoring."""
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="https://github.com/example/repo.git",
            branch_base="main",
            task_title="monitor-recovery-verify-skip-cancel-monitoring",
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

        async def verify_resume_monitor_start(self, workspace_id_arg: str) -> bool:
            """Test helper for verify resume monitor start."""
            assert workspace_id_arg == workspace_id
            return False

        async def run_resumed_pr_monitor(self, workspace_id_arg: str, handoff_obj: object) -> None:
            """Test helper for run resumed pr monitor."""
            raise AssertionError("monitor run must not start when verify-start skip finalizes")

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
            recovery_operation_id="op_verify_skip_cancel_monitoring",
        )
    )
    await asyncio.wait_for(finalize_started.wait(), timeout=5.0)
    resume_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await resume_task

    assert len(after_cancellation_calls) == 1
    assert after_cancellation_calls[0]["status"] == OperationStatus.failed
    assert after_cancellation_calls[0]["operation_id"] == "op_verify_skip_cancel_monitoring"
    assert after_cancellation_calls[0]["error_code"] == "MONITOR_RECOVERY_START_SKIPPED"
    assert (
        after_cancellation_calls[0]["error_message"]
        == "Monitor resume skipped before monitor loop started."
    )
    assert len(finish_calls) == 1


@pytest.mark.unit
async def test_safely_resume_pr_monitor_pre_start_exception_before_handoff_fails_recovery_op(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Pre-handoff verify failures must fail the remonitor bookkeeping operation."""
    from awf.control.executor.monitor_handoff import MonitorResumePreStartError

    handoff = object()
    finish_calls: list[dict[str, object]] = []

    class HandoffExecutor(_RecordingExecutor):
        """Executor stub that raises before handoff bookkeeping finalizes."""

        async def resume_pr_monitor_handoff(self, workspace_id: str) -> object:
            """Test helper for resume pr monitor handoff."""
            assert workspace_id == "ws_monitor"
            return handoff

        async def verify_resume_monitor_start(self, workspace_id: str) -> bool:
            """Test helper for verify resume monitor start."""
            assert workspace_id == "ws_monitor"
            raise MonitorResumePreStartError("verify failed before finalize")

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
        finish_calls.append({"workspace_id": workspace_id, **kwargs})
        return True

    worker._finish_monitor_recovery_operation = (  # type: ignore[method-assign]
        _finish_monitor_recovery_operation
    )

    result = await worker._safely_resume_pr_monitor(  # noqa: SLF001
        "ws_monitor",
        recovery_operation_id="op_pre_handoff_verify_failed",
    )

    assert result is False
    assert len(finish_calls) == 1
    assert finish_calls[0]["operation_id"] == "op_pre_handoff_verify_failed"
    assert finish_calls[0]["status"] == OperationStatus.failed
    assert finish_calls[0]["error_code"] == "MONITOR_RECOVERY_FAILED"
    assert "ws_monitor" not in worker._monitor_recovery_handoff_succeeded_workspace_ids  # noqa: SLF001
