"""ControlWorker tests (continued from test_worker_part_047).

Split out of ``test_worker_part_047`` to keep each test module under the
first-party 1500-line maintainability guardrail.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
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
from awf.db.enums import OperationStatus, OperationType, WorkspaceStatus
from awf.db.repositories import OperationRepository, WorkspaceRepository
from awf.db.session import make_session_factory
from awf.node.git_manager import GitManager
from awf.node.provisioner import Provisioner, ProvisionerConfig
from tests.postgres import postgres_test_engine


async def _pending_execution_task() -> None:
    await asyncio.Event().wait()


@pytest.fixture
async def session_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


@pytest.fixture
def worker(session_factory: async_sessionmaker[AsyncSession], tmp_path: Path) -> ControlWorker:
    git = GitManager(tmp_path / "awf-work")
    prov = Provisioner(
        session_factory=session_factory,
        git=git,
        config=ProvisionerConfig(node_id="test-node-01"),
    )
    return ControlWorker(
        session_factory=session_factory,
        provisioner=prov,
        config=WorkerConfig(poll_interval_seconds=0.01, max_concurrent_provisions=3),
    )


class _RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.resume_calls: list[str] = []

    async def execute(self, workspace_id: str, **_kwargs: object) -> None:
        self.calls.append(workspace_id)

    async def resume_pr_monitor_handoff(self, workspace_id: str) -> object | None:
        del workspace_id
        return object()

    async def run_resumed_pr_monitor(self, workspace_id: str, handoff: object) -> bool:
        del handoff
        self.resume_calls.append(workspace_id)
        return True

    async def resume_pr_monitor(self, workspace_id: str) -> None:
        handoff = await self.resume_pr_monitor_handoff(workspace_id)
        if handoff is None:
            return
        await self.run_resumed_pr_monitor(workspace_id, handoff)


@pytest.mark.unit
async def test_monitor_recovery_terminal_finalize_status_preserves_handoff_failure_reason(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Terminal finalize must not downgrade a specific handoff abort to generic failure."""
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="https://github.com/example/repo.git",
            branch_base="main",
            task_title="monitor-recovery-terminal-handoff-reason",
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
        await repo.add_event(
            ws,
            event_type="workspace.monitor_runtime_restart_failed",
            reason_code="UNSUPPORTED_TASK_KIND",
            payload={"reason_code": "UNSUPPORTED_TASK_KIND"},
        )
        ws.failure_message = "Task kind is not supported for monitor recovery."
        await repo.transition(ws, to=WorkspaceStatus.failed, reason_code="SEED")
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
    assert status == OperationStatus.failed
    assert error_code == "UNSUPPORTED_TASK_KIND"
    assert error_message == "Task kind is not supported for monitor recovery."


@pytest.mark.unit
async def test_monitor_recovery_terminal_finalize_status_preserves_success_after_handoff_failed_race(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Terminal finalize retry must not downgrade after a successful handoff."""
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="https://github.com/example/repo.git",
            branch_base="main",
            task_title="monitor-recovery-terminal-handoff-success-race",
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
        after_successful_handoff=True,
    )
    assert status == OperationStatus.succeeded
    assert error_code is None
    assert error_message is None


@pytest.mark.unit
async def test_monitor_recovery_start_skipped_operation_status_preserves_success_after_handoff_failed_race(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="https://github.com/example/repo.git",
            branch_base="main",
            task_title="monitor-recovery-pre-finalize-failed-race",
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

    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=object(),  # type: ignore[arg-type]
        config=WorkerConfig(poll_interval_seconds=0.01),
    )
    (
        status,
        error_code,
        error_message,
    ) = await worker_dispatch_methods._monitor_recovery_start_skipped_operation_status(  # noqa: SLF001
        worker,
        workspace_id,
        after_successful_handoff=True,
    )
    assert status == OperationStatus.succeeded
    assert error_code is None
    assert error_message is None


@pytest.mark.unit
async def test_safely_resume_pr_monitor_preserves_succeeded_operation_when_pre_finalize_start_recheck_bails_after_failed_race(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Pre-finalize verify failure after handoff must not downgrade on terminal failed races."""
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="https://github.com/example/repo.git",
            branch_base="main",
            task_title="monitor-recovery-pre-finalize-failed-race",
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
    monitor_ran = False

    class HandoffExecutor(_RecordingExecutor):
        async def resume_pr_monitor_handoff(self, handoff_workspace_id: str) -> object:
            assert handoff_workspace_id == workspace_id
            return handoff

        async def verify_resume_monitor_start(self, handoff_workspace_id: str) -> bool:
            assert handoff_workspace_id == workspace_id
            return False

        async def run_resumed_pr_monitor(
            self,
            handoff_workspace_id: str,
            handoff_obj: object,
        ) -> None:
            nonlocal monitor_ran
            del handoff_obj
            assert handoff_workspace_id == workspace_id
            monitor_ran = True

    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=object(),  # type: ignore[arg-type]
        executor=HandoffExecutor(),
        config=WorkerConfig(poll_interval_seconds=0.01),
    )

    async def _finish_monitor_recovery_operation(
        finish_workspace_id: str,
        **kwargs: object,
    ) -> bool:
        finish_calls.append({"workspace_id": finish_workspace_id, **kwargs})
        return True

    worker._finish_monitor_recovery_operation = (  # type: ignore[method-assign]
        _finish_monitor_recovery_operation
    )

    result = await worker._safely_resume_pr_monitor(  # noqa: SLF001
        workspace_id,
        recovery_operation_id="op_pre_finalize_failed_race",
    )

    assert result is True
    assert monitor_ran is False
    assert len(finish_calls) == 1
    assert finish_calls[0]["operation_id"] == "op_pre_finalize_failed_race"
    assert finish_calls[0]["status"] == OperationStatus.succeeded


@pytest.mark.unit
async def test_safely_resume_pr_monitor_fails_operation_when_start_recheck_bails(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    handoff = object()
    finish_calls: list[dict[str, object]] = []
    monitor_ran = False

    class HandoffExecutor(_RecordingExecutor):
        async def resume_pr_monitor_handoff(self, workspace_id: str) -> object:
            assert workspace_id == "ws_monitor"
            return handoff

        async def verify_resume_monitor_start(self, workspace_id: str) -> bool:
            assert workspace_id == "ws_monitor"
            return False

        async def run_resumed_pr_monitor(self, workspace_id: str, handoff_obj: object) -> None:
            nonlocal monitor_ran
            del handoff_obj
            assert workspace_id == "ws_monitor"
            monitor_ran = True

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
        finish_calls.append({"workspace_id": workspace_id, **kwargs})
        return True

    worker._finish_monitor_recovery_operation = (  # type: ignore[method-assign]
        _finish_monitor_recovery_operation
    )

    result = await worker._safely_resume_pr_monitor(  # noqa: SLF001
        "ws_monitor",
        recovery_operation_id="op_start_recheck_bailed",
    )

    assert result is False
    assert monitor_ran is False
    assert len(finish_calls) == 1
    assert finish_calls[0]["operation_id"] == "op_start_recheck_bailed"
    assert finish_calls[0]["status"] == OperationStatus.failed
    assert finish_calls[0]["error_code"] == "MONITOR_RECOVERY_FAILED"


@pytest.mark.unit
async def test_safely_resume_claimed_pr_monitor_skips_cooldown_when_start_recheck_bails(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Pre-finalize verify failure must not apply active-salvage monitor cooldown."""
    handoff = object()
    cooldown_recorded = False

    class HandoffExecutor(_RecordingExecutor):
        async def resume_pr_monitor_handoff(self, workspace_id: str) -> object:
            assert workspace_id == "ws_monitor"
            return handoff

        async def verify_resume_monitor_start(self, workspace_id: str) -> bool:
            assert workspace_id == "ws_monitor"
            return False

    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=object(),  # type: ignore[arg-type]
        executor=HandoffExecutor(),
        config=WorkerConfig(poll_interval_seconds=0.01, monitor_claim_lease_seconds=30.0),
    )
    worker._remember_active_salvage_monitor_recovery_operation_id(  # noqa: SLF001
        "op_start_recheck_bailed"
    )

    async def _finish_monitor_recovery_operation(
        workspace_id: str,
        **kwargs: object,
    ) -> bool:
        assert workspace_id == "ws_monitor"
        return True

    async def _record_cooldown(**kwargs: object) -> None:
        nonlocal cooldown_recorded
        cooldown_recorded = True

    worker._finish_monitor_recovery_operation = (  # type: ignore[method-assign]
        _finish_monitor_recovery_operation
    )
    worker._record_active_salvage_monitor_resume_cooldown = (  # type: ignore[method-assign]
        _record_cooldown
    )

    async def _release_monitor_claim(workspace_id: str) -> None:
        assert workspace_id == "ws_monitor"

    async def _prompt_release(workspace_id: str) -> None:
        assert workspace_id == "ws_monitor"

    worker._release_monitoring_pr_claim = _release_monitor_claim  # type: ignore[method-assign]
    worker._release_terminal_runtime_promptly = _prompt_release  # type: ignore[method-assign]

    await worker._safely_resume_claimed_pr_monitor(  # noqa: SLF001
        "ws_monitor",
        recovery_operation_id="op_start_recheck_bailed",
    )

    assert cooldown_recorded is False
    assert "ws_monitor" not in worker._active_salvage_monitor_resume_cooldowns  # noqa: SLF001


@pytest.mark.unit
async def test_safely_resume_claimed_pr_monitor_applies_cooldown_when_handoff_aborts_while_monitorable(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Non-terminal handoff aborts that leave monitoring_pr must cool down active salvage."""
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="https://github.com/example/repo.git",
            branch_base="main",
            task_title="monitor-recovery-handoff-abort",
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
        await repo.transition(
            ws,
            to=WorkspaceStatus.monitoring_pr,
            reason_code="SEED",
        )
        await session.commit()

    cooldown_recorded = False

    class _AbortingHandoffExecutor(_RecordingExecutor):
        async def resume_pr_monitor_handoff(self, handoff_workspace_id: str) -> object | None:
            assert handoff_workspace_id == workspace_id
            return None

    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=object(),  # type: ignore[arg-type]
        executor=_AbortingHandoffExecutor(),
        config=WorkerConfig(poll_interval_seconds=0.01, monitor_claim_lease_seconds=30.0),
    )
    worker._remember_active_salvage_monitor_recovery_operation_id(  # noqa: SLF001
        "op_handoff_aborted"
    )

    async def _finish_monitor_recovery_operation(
        finish_workspace_id: str,
        **kwargs: object,
    ) -> bool:
        assert finish_workspace_id == workspace_id
        assert kwargs["status"] == OperationStatus.failed
        return True

    async def _record_cooldown(_record_workspace_id: str, **kwargs: object) -> None:
        nonlocal cooldown_recorded
        assert _record_workspace_id == workspace_id
        assert kwargs["recovery_operation_id"] == "op_handoff_aborted"
        cooldown_recorded = True

    worker._finish_monitor_recovery_operation = (  # type: ignore[method-assign]
        _finish_monitor_recovery_operation
    )
    worker._record_active_salvage_monitor_resume_cooldown = (  # type: ignore[method-assign]
        _record_cooldown
    )

    async def _release_monitor_claim(released_workspace_id: str) -> None:
        assert released_workspace_id == workspace_id

    async def _prompt_release(released_workspace_id: str) -> None:
        assert released_workspace_id == workspace_id

    worker._release_monitoring_pr_claim = _release_monitor_claim  # type: ignore[method-assign]
    worker._release_terminal_runtime_promptly = _prompt_release  # type: ignore[method-assign]

    await worker._safely_resume_claimed_pr_monitor(  # noqa: SLF001
        workspace_id,
        recovery_operation_id="op_handoff_aborted",
    )

    assert cooldown_recorded is True
    assert worker._active_salvage_monitor_resume_cooldown_active(workspace_id)  # noqa: SLF001


@pytest.mark.unit
async def test_safely_resume_claimed_pr_monitor_skips_cooldown_when_cancelled_after_succeeded_handoff(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Post-handoff cancellation must not apply failed-recovery salvage cooldown."""
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="https://github.com/example/repo.git",
            branch_base="main",
            task_title="monitor-recovery-post-handoff-cancel",
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
        await repo.transition(
            ws,
            to=WorkspaceStatus.monitoring_pr,
            reason_code="SEED",
        )
        operation = await OperationRepository(session).create(
            workspace_id=workspace_id,
            operation_type=OperationType.remonitor,
            status=OperationStatus.running,
            payload={"source": "worker_restart"},
        )
        operation_id = operation.id
        await session.commit()

    handoff = object()
    run_started = asyncio.Event()
    cooldown_recorded = False

    class BlockingRunExecutor(_RecordingExecutor):
        async def resume_pr_monitor_handoff(self, handoff_workspace_id: str) -> object:
            assert handoff_workspace_id == workspace_id
            return handoff

        async def run_resumed_pr_monitor(
            self,
            handoff_workspace_id: str,
            handoff_obj: object,
        ) -> None:
            assert handoff_obj is handoff
            assert handoff_workspace_id == workspace_id
            run_started.set()
            await asyncio.Event().wait()

    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=object(),  # type: ignore[arg-type]
        executor=BlockingRunExecutor(),
        config=WorkerConfig(poll_interval_seconds=0.01, monitor_claim_lease_seconds=30.0),
    )
    worker._remember_active_salvage_monitor_recovery_operation_id(  # noqa: SLF001
        operation_id
    )

    async def _record_cooldown(_record_workspace_id: str, **kwargs: object) -> None:
        nonlocal cooldown_recorded
        assert _record_workspace_id == workspace_id
        cooldown_recorded = True

    async def _release_monitor_claim(released_workspace_id: str) -> None:
        assert released_workspace_id == workspace_id

    async def _prompt_release(released_workspace_id: str) -> None:
        assert released_workspace_id == workspace_id

    worker._record_active_salvage_monitor_resume_cooldown = (  # type: ignore[method-assign]
        _record_cooldown
    )
    worker._release_monitoring_pr_claim = _release_monitor_claim  # type: ignore[method-assign]
    worker._release_terminal_runtime_promptly = _prompt_release  # type: ignore[method-assign]

    resume_task = asyncio.create_task(
        worker._safely_resume_claimed_pr_monitor(  # noqa: SLF001
            workspace_id,
            recovery_operation_id=operation_id,
        )
    )
    await asyncio.wait_for(run_started.wait(), timeout=5.0)
    resume_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await resume_task

    assert cooldown_recorded is False
    assert workspace_id not in worker._active_salvage_monitor_resume_cooldowns  # noqa: SLF001
    async with session_factory() as session:
        finished = await OperationRepository(session).get(operation_id)
        assert finished is not None
        assert finished.status == OperationStatus.succeeded.value


@pytest.mark.unit
async def test_safely_resume_pr_monitor_none_return_does_not_refinish_succeeded_operation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Legacy void-return executors must not trigger the skipped-start correction path."""
    handoff = object()
    finish_calls: list[dict[str, object]] = []
    monitor_ran = False

    class LegacyVoidRunExecutor(_RecordingExecutor):
        async def resume_pr_monitor_handoff(self, workspace_id: str) -> object:
            assert workspace_id == "ws_monitor"
            return handoff

        async def run_resumed_pr_monitor(self, workspace_id: str, handoff_obj: object) -> None:
            nonlocal monitor_ran
            assert handoff_obj is handoff
            assert workspace_id == "ws_monitor"
            monitor_ran = True

    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=object(),  # type: ignore[arg-type]
        executor=LegacyVoidRunExecutor(),
        config=WorkerConfig(poll_interval_seconds=0.01),
    )

    async def _finish_monitor_recovery_operation(
        workspace_id: str,
        **kwargs: object,
    ) -> bool:
        finish_calls.append({"workspace_id": workspace_id, **kwargs})
        return True

    worker._finish_monitor_recovery_operation = (  # type: ignore[method-assign]
        _finish_monitor_recovery_operation
    )

    result = await worker._safely_resume_pr_monitor(  # noqa: SLF001
        "ws_monitor",
        recovery_operation_id="op_legacy_void_return",
    )

    assert result is True
    assert monitor_ran is True
    assert len(finish_calls) == 1
    assert finish_calls[0]["operation_id"] == "op_legacy_void_return"
    assert finish_calls[0]["status"] == OperationStatus.succeeded


@pytest.mark.unit
async def test_safely_resume_pr_monitor_preserves_succeeded_operation_when_post_finalize_start_recheck_bails(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Post-handoff start recheck bail must not downgrade a succeeded remonitor op."""
    handoff = object()
    finish_calls: list[dict[str, object]] = []
    verify_calls = 0

    class HandoffExecutor(_RecordingExecutor):
        async def resume_pr_monitor_handoff(self, workspace_id: str) -> object:
            assert workspace_id == "ws_monitor"
            return handoff

        async def verify_resume_monitor_start(self, workspace_id: str) -> bool:
            nonlocal verify_calls
            assert workspace_id == "ws_monitor"
            verify_calls += 1
            return verify_calls == 1

        async def run_resumed_pr_monitor(self, workspace_id: str, handoff_obj: object) -> bool:
            assert handoff_obj is handoff
            assert workspace_id == "ws_monitor"
            if not await self.verify_resume_monitor_start(workspace_id):
                return False
            raise AssertionError("monitor run must not start when post-finalize recheck bails")

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
        finish_calls.append({"workspace_id": workspace_id, **kwargs})
        return True

    worker._finish_monitor_recovery_operation = (  # type: ignore[method-assign]
        _finish_monitor_recovery_operation
    )

    result = await worker._safely_resume_pr_monitor(  # noqa: SLF001
        "ws_monitor",
        recovery_operation_id="op_post_finalize_recheck_bailed",
    )

    assert result is True
    assert verify_calls == 2
    assert len(finish_calls) == 1
    assert finish_calls[0]["operation_id"] == "op_post_finalize_recheck_bailed"
    assert finish_calls[0]["status"] == OperationStatus.succeeded


@pytest.mark.unit
async def test_safely_resume_pr_monitor_post_handoff_cancellation_does_not_cancel_recovery_op(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    handoff = object()
    finish_calls: list[dict[str, object]] = []
    run_started = asyncio.Event()
    after_cancellation_called = False

    class BlockingRunExecutor(_RecordingExecutor):
        async def resume_pr_monitor_handoff(self, workspace_id: str) -> object:
            assert workspace_id == "ws_monitor"
            return handoff

        async def run_resumed_pr_monitor(self, workspace_id: str, handoff_obj: object) -> None:
            assert handoff_obj is handoff
            assert workspace_id == "ws_monitor"
            run_started.set()
            await asyncio.Event().wait()

    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=object(),  # type: ignore[arg-type]
        executor=BlockingRunExecutor(),
        config=WorkerConfig(poll_interval_seconds=0.01),
    )

    async def _finish_monitor_recovery_operation(
        workspace_id: str,
        **kwargs: object,
    ) -> bool:
        finish_calls.append({"workspace_id": workspace_id, **kwargs})
        return True

    async def _finish_after_cancellation(*args: object, **kwargs: object) -> None:
        nonlocal after_cancellation_called
        after_cancellation_called = True

    worker._finish_monitor_recovery_operation = (  # type: ignore[method-assign]
        _finish_monitor_recovery_operation
    )
    worker._finish_monitor_recovery_operation_after_cancellation = (  # type: ignore[method-assign]
        _finish_after_cancellation
    )

    resume_task = asyncio.create_task(
        worker._safely_resume_pr_monitor(  # noqa: SLF001
            "ws_monitor",
            recovery_operation_id="op_post_handoff_cancel",
        )
    )
    await asyncio.wait_for(run_started.wait(), timeout=5.0)
    resume_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await resume_task

    assert not after_cancellation_called
    assert len(finish_calls) == 1
    assert finish_calls[0]["status"] == OperationStatus.succeeded
    assert finish_calls[0]["operation_id"] == "op_post_handoff_cancel"


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
        async def resume_pr_monitor_handoff(self, workspace_id: str) -> object:
            assert workspace_id == "ws_monitor"
            return handoff

        async def run_resumed_pr_monitor(self, workspace_id: str, handoff_obj: object) -> None:
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
        async def resume_pr_monitor_handoff(self, workspace_id_arg: str) -> object:
            assert workspace_id_arg == workspace_id
            return handoff

        async def run_resumed_pr_monitor(self, workspace_id_arg: str, handoff_obj: object) -> None:
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
        finish_calls.append({"workspace_id": workspace_id_arg, **kwargs})
        finalize_started.set()
        await asyncio.Event().wait()
        return True

    async def _finish_after_cancellation(*args: object, **kwargs: object) -> None:
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
        async def resume_pr_monitor_handoff(self, workspace_id_arg: str) -> object:
            assert workspace_id_arg == workspace_id
            return handoff

        async def verify_resume_monitor_start(self, workspace_id_arg: str) -> bool:
            assert workspace_id_arg == workspace_id
            return False

        async def run_resumed_pr_monitor(self, workspace_id_arg: str, handoff_obj: object) -> None:
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
        finish_calls.append({"workspace_id": workspace_id_arg, **kwargs})
        finalize_started.set()
        await asyncio.Event().wait()
        return True

    async def _finish_after_cancellation(*args: object, **kwargs: object) -> None:
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
        async def resume_pr_monitor_handoff(self, workspace_id_arg: str) -> object:
            assert workspace_id_arg == workspace_id
            return handoff

        async def run_resumed_pr_monitor(self, workspace_id_arg: str, handoff_obj: object) -> None:
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
        finish_calls.append({"workspace_id": workspace_id_arg, **kwargs})
        finalize_started.set()
        await asyncio.Event().wait()
        return True

    async def _finish_after_cancellation(*args: object, **kwargs: object) -> None:
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
        async def resume_pr_monitor_handoff(self, workspace_id: str) -> object:
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
        async def resume_pr_monitor_handoff(self, workspace_id: str) -> object:
            assert workspace_id == "ws_monitor"
            return handoff

        async def run_resumed_pr_monitor(self, workspace_id: str, handoff_obj: object) -> None:
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
        nonlocal finish_attempts
        finish_attempts += 1
        finish_calls.append({"workspace_id": workspace_id, **kwargs})
        if finish_attempts == 1:
            finalize_started.set()
            await asyncio.Event().wait()
        return True

    async def _release_monitor_claim(workspace_id: str) -> None:
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
        async def resume_pr_monitor(self, workspace_id: str) -> None:
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
        finish_calls.append({"workspace_id": workspace_id, **kwargs})
        return True

    async def _release_monitor_claim(workspace_id: str) -> None:
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
        def __init__(self) -> None:
            self.resume_calls: list[str] = []

        async def execute(self, workspace_id: str, **_kwargs: object) -> None:
            del workspace_id

        async def resume_pr_monitor(self, workspace_id: str) -> None:
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
        def __init__(self) -> None:
            self.handoff_calls: list[str] = []
            self.resume_calls: list[str] = []

        async def execute(self, workspace_id: str, **_kwargs: object) -> None:
            del workspace_id

        async def resume_pr_monitor_handoff(self, workspace_id: str) -> object:
            self.handoff_calls.append(workspace_id)
            return object()

        async def resume_pr_monitor(self, workspace_id: str) -> None:
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
def test_monitor_recovery_handoff_failure_message_prefers_payload_message() -> None:
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
    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=object(),  # type: ignore[arg-type]
        config=WorkerConfig(poll_interval_seconds=0.01),
    )

    class _BrokenRepo:
        async def get(self, workspace_id: str) -> None:
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
    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=object(),  # type: ignore[arg-type]
        config=WorkerConfig(poll_interval_seconds=0.01),
    )

    class _BrokenRepo:
        async def get(self, workspace_id: str) -> None:
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
    finish_calls: list[dict[str, object]] = []

    class _RaisingLegacyExecutor:
        async def resume_pr_monitor(self, workspace_id: str) -> None:
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
    class _RaisingLegacyExecutor:
        async def resume_pr_monitor(self, workspace_id: str) -> None:
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
    finish_calls: list[dict[str, object]] = []

    class _RaisingHandoffExecutor(_RecordingExecutor):
        async def resume_pr_monitor_handoff(self, workspace_id: str) -> object:
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
    class _RaisingHandoffExecutor(_RecordingExecutor):
        async def resume_pr_monitor_handoff(self, workspace_id: str) -> object:
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
        assert workspace_id == "ws_epoch"
        raise RuntimeError("transient db disconnect")

    released: list[str] = []

    async def _release(workspace_id: str) -> None:
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
        raise asyncio.CancelledError

    released: list[str] = []

    async def _release(workspace_id: str) -> None:
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
    claim_calls: list[str] = []

    async def _claim_monitoring_pr(workspace_id: str) -> bool:
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
