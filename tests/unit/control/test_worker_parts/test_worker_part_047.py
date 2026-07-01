"""ControlWorker tests (continued from test_worker_part_038).

Split out of ``test_worker_part_038`` to keep each test module under the
first-party 1500-line maintainability guardrail. These exercise the worker's
DB-closed event handling, dispatch limit helpers, active-salvage bookkeeping
bounds, and the ``_safely_*`` failure-isolation paths.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import InterfaceError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.control.worker import (
    ControlWorker,
    WorkerConfig,
)
from awf.control.worker import dispatch_methods as worker_dispatch_methods
from awf.control.worker import recovery_cooldown as worker_recovery_cooldown
from awf.control.worker.types import (
    _ActiveExecutionCandidate,
)
from awf.db.enums import OperationStatus, WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
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


def _closed_connection_error() -> InterfaceError:
    return InterfaceError(
        "SELECT 1",
        {},
        RuntimeError("connection is closed"),
        connection_invalidated=True,
    )


@pytest.mark.unit
async def test_db_connection_closed_event_skips_stale_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RecordingSession:
        committed = False
        closed = False

        async def commit(self) -> None:
            self.committed = True

        async def rollback(self) -> None:
            raise AssertionError("stale event skip should not roll back")

        async def close(self) -> None:
            self.closed = True

    class StaleWorkspaceRepository:
        def __init__(self, session: RecordingSession) -> None:
            assert session is recording_session

        async def get(self, workspace_id: str) -> SimpleNamespace:
            assert workspace_id == "ws_stale_event"
            return SimpleNamespace(status=WorkspaceStatus.ready.value)

        async def add_event(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("stale workspace should not receive an event")

    recording_session = RecordingSession()
    worker = ControlWorker(
        session_factory=lambda: recording_session,  # type: ignore[arg-type]
        provisioner=object(),  # type: ignore[arg-type]
        config=WorkerConfig(poll_interval_seconds=0.01),
    )
    monkeypatch.setattr(
        "awf.control.worker.recovery_stale.WorkspaceRepository",
        StaleWorkspaceRepository,
    )

    await worker._record_db_connection_closed_event(  # noqa: SLF001
        _ActiveExecutionCandidate(
            workspace_id="ws_stale_event",
            status=WorkspaceStatus.running,
            compose_project_name="awf_ws_stale_event",
        ),
        _closed_connection_error(),
    )

    assert recording_session.committed is True
    assert recording_session.closed is True


@pytest.mark.unit
async def test_dispatch_helpers_respect_limits_and_existing_tasks(
    worker: ControlWorker,
) -> None:
    existing_task = asyncio.create_task(_pending_execution_task())
    try:
        worker._execution_tasks["existing"] = existing_task  # noqa: SLF001

        assert worker._dispatchable_execution_ids(["new"], limit=0) == []  # noqa: SLF001
        assert (
            worker._dispatchable_execution_ids(["existing", "new"], limit=2)  # noqa: SLF001
            == ["new"]
        )
        assert worker._dispatch_ready_executions(["new"], limit=0) == set()  # noqa: SLF001
        assert (
            worker._dispatch_ready_executions(["existing"], limit=1)  # noqa: SLF001
            == set()
        )
        assert worker._dispatch_monitor_resumes(["new"], limit=0) == set()  # noqa: SLF001
        assert (
            worker._dispatch_monitor_resumes(["existing"], limit=1)  # noqa: SLF001
            == set()
        )
    finally:
        existing_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await existing_task
        worker._execution_tasks.clear()  # noqa: SLF001


@pytest.mark.unit
def test_active_salvage_monitor_recovery_operation_ids_are_bounded(
    worker: ControlWorker,
) -> None:
    limit = worker_recovery_cooldown._ACTIVE_SALVAGE_MONITOR_RECOVERY_OPERATION_ID_LIMIT  # noqa: SLF001

    for index in range(limit + 2):
        worker._remember_active_salvage_monitor_recovery_operation_id(  # noqa: SLF001
            f"operation-{index:04d}"
        )

    tracked = worker._active_salvage_monitor_recovery_operation_ids  # noqa: SLF001
    assert len(tracked) == limit
    assert "operation-0000" not in tracked
    assert "operation-0001" not in tracked
    assert f"operation-{limit + 1:04d}" in tracked


@pytest.mark.unit
def test_active_salvage_monitor_resume_cooldowns_are_bounded_and_expired_entries_are_evicted(
    worker: ControlWorker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_time = 1_000.0
    monkeypatch.setattr("awf.control.worker.recovery_cooldown.monotonic", lambda: current_time)
    limit = worker_recovery_cooldown._ACTIVE_SALVAGE_MONITOR_RESUME_COOLDOWN_LIMIT  # noqa: SLF001
    worker._active_salvage_monitor_resume_cooldowns["expired-workspace"] = (  # noqa: SLF001
        current_time - 1.0
    )

    for index in range(limit + 2):
        worker._remember_active_salvage_monitor_resume_cooldown(  # noqa: SLF001
            f"workspace-{index:04d}",
            current_time + 60.0,
        )

    tracked = worker._active_salvage_monitor_resume_cooldowns  # noqa: SLF001
    assert len(tracked) == limit
    assert "expired-workspace" not in tracked
    assert "workspace-0000" not in tracked
    assert "workspace-0001" not in tracked
    assert f"workspace-{limit + 1:04d}" in tracked
    assert worker._active_salvage_monitor_resume_cooldown_active(  # noqa: SLF001
        f"workspace-{limit + 1:04d}"
    )

    current_time += 61.0

    assert not worker._active_salvage_monitor_resume_cooldown_active(  # noqa: SLF001
        f"workspace-{limit + 1:04d}"
    )
    assert f"workspace-{limit + 1:04d}" not in tracked


@pytest.mark.unit
async def test_safe_worker_paths_swallow_runtime_failures(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    class RaisingProvisioner:
        async def provision_claimed(
            self, workspace_id: str, execution_claim_epoch: int | None = None
        ) -> None:
            assert workspace_id == "ws_provision"
            raise RuntimeError("provision failed")

    class RaisingExecutor(_RecordingExecutor):
        async def execute(self, workspace_id: str, **_kwargs: object) -> None:
            assert workspace_id == "ws_execute"
            raise RuntimeError("execute failed")

    class RaisingMonitorExecutor(_RecordingExecutor):
        async def run_resumed_pr_monitor(self, workspace_id: str, handoff: object) -> None:
            del handoff
            assert workspace_id == "ws_monitor"
            raise RuntimeError("resume failed")

    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=RaisingProvisioner(),  # type: ignore[arg-type]
        config=WorkerConfig(poll_interval_seconds=0.01),
    )
    finish_calls: list[dict[str, object]] = []

    async def _finish_monitor_recovery_operation(
        workspace_id: str,
        **kwargs: object,
    ) -> bool:
        finish_calls.append({"workspace_id": workspace_id, **kwargs})
        return True

    worker._finish_monitor_recovery_operation = (  # type: ignore[method-assign]
        _finish_monitor_recovery_operation
    )

    await worker._safely_provision_claimed("ws_provision")  # noqa: SLF001
    await worker._safely_resume_pr_monitor(  # noqa: SLF001
        "ws_monitor",
        recovery_operation_id="op_no_executor",
    )

    assert finish_calls == [
        {
            "workspace_id": "ws_monitor",
            "operation_id": "op_no_executor",
            "status": OperationStatus.failed,
            "error_code": "MONITOR_RECOVERY_NO_EXECUTOR",
            "error_message": "Worker has no executor configured.",
        }
    ]

    execute_worker = ControlWorker(
        session_factory=session_factory,
        provisioner=object(),  # type: ignore[arg-type]
        executor=RaisingExecutor(),
        config=WorkerConfig(poll_interval_seconds=0.01),
    )
    await execute_worker._safely_execute("ws_execute")  # noqa: SLF001

    raising_worker = ControlWorker(
        session_factory=session_factory,
        provisioner=object(),  # type: ignore[arg-type]
        executor=RaisingMonitorExecutor(),
        config=WorkerConfig(poll_interval_seconds=0.01),
    )
    finish_calls.clear()
    raising_worker._finish_monitor_recovery_operation = (  # type: ignore[method-assign]
        _finish_monitor_recovery_operation
    )

    await raising_worker._safely_resume_pr_monitor(  # noqa: SLF001
        "ws_monitor",
        recovery_operation_id="op_resume_failed",
    )

    assert len(finish_calls) == 1
    assert finish_calls[0]["workspace_id"] == "ws_monitor"
    assert finish_calls[0]["operation_id"] == "op_resume_failed"
    assert finish_calls[0]["status"] == OperationStatus.succeeded

    class _FailingHandoffExecutor(_RecordingExecutor):
        async def resume_pr_monitor_handoff(self, workspace_id: str) -> object | None:
            del workspace_id
            return None

    failing_handoff_worker = ControlWorker(
        session_factory=session_factory,
        provisioner=object(),  # type: ignore[arg-type]
        executor=_FailingHandoffExecutor(),
        config=WorkerConfig(poll_interval_seconds=0.01),
    )
    finish_calls.clear()
    failing_handoff_worker._finish_monitor_recovery_operation = (  # type: ignore[method-assign]
        _finish_monitor_recovery_operation
    )
    result = await failing_handoff_worker._safely_resume_pr_monitor(  # noqa: SLF001
        "ws_monitor",
        recovery_operation_id="op_handoff_failed",
    )
    assert result is False
    assert len(finish_calls) == 1
    assert finish_calls[0]["operation_id"] == "op_handoff_failed"
    assert finish_calls[0]["status"] == OperationStatus.failed
    assert finish_calls[0]["error_code"] == "MONITOR_RECOVERY_FAILED"


@pytest.mark.unit
async def test_safely_resume_pr_monitor_cancelled_handoff_skips_classifies_operation_cancelled(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """When handoff returns None because the workspace was cancelled, finalize cancelled."""
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="https://github.com/example/repo.git",
            branch_base="main",
            task_title="monitor-recovery-cancelled-handoff",
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        workspace_id = ws.id
        await repo.transition(
            ws,
            to=WorkspaceStatus.cancelled,
            reason_code="TEST_OPERATOR",
        )
        await session.commit()

    finish_calls: list[dict[str, object]] = []

    class _CancelledHandoffExecutor(_RecordingExecutor):
        async def resume_pr_monitor_handoff(self, handoff_workspace_id: str) -> object | None:
            assert handoff_workspace_id == workspace_id
            return None

    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=object(),  # type: ignore[arg-type]
        executor=_CancelledHandoffExecutor(),
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
        recovery_operation_id="op_cancelled_handoff",
    )

    assert result is True
    assert len(finish_calls) == 1
    assert finish_calls[0]["operation_id"] == "op_cancelled_handoff"
    assert finish_calls[0]["status"] == OperationStatus.cancelled
    assert finish_calls[0]["error_code"] == "MONITOR_RECOVERY_CANCELLED"


@pytest.mark.unit
async def test_safely_resume_pr_monitor_retries_succeed_finalize_after_handoff_write_failure(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    handoff = object()
    finish_calls: list[dict[str, object]] = []
    succeed_attempt = 0
    monitor_ran_after_finalize = False

    class HandoffExecutor(_RecordingExecutor):
        async def resume_pr_monitor_handoff(self, workspace_id: str) -> object:
            assert workspace_id == "ws_monitor"
            return handoff

        async def run_resumed_pr_monitor(self, workspace_id: str, handoff_obj: object) -> bool:
            nonlocal monitor_ran_after_finalize
            assert handoff_obj is handoff
            assert workspace_id == "ws_monitor"
            monitor_ran_after_finalize = succeed_attempt >= 2
            return True

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
        if kwargs.get("status") == OperationStatus.succeeded:
            nonlocal succeed_attempt
            succeed_attempt += 1
            return succeed_attempt > 1
        return True

    worker._finish_monitor_recovery_operation = (  # type: ignore[method-assign]
        _finish_monitor_recovery_operation
    )

    result = await worker._safely_resume_pr_monitor(  # noqa: SLF001
        "ws_monitor",
        recovery_operation_id="op_retry_finalize",
    )

    assert result is True
    assert monitor_ran_after_finalize is True
    assert len(finish_calls) == 2
    assert all(call["status"] == OperationStatus.succeeded for call in finish_calls)
    assert finish_calls[0]["operation_id"] == "op_retry_finalize"
    assert finish_calls[1]["operation_id"] == "op_retry_finalize"


@pytest.mark.unit
async def test_safely_resume_pr_monitor_post_handoff_runtime_error_does_not_fail_recovery_op(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    handoff = object()
    finish_calls: list[dict[str, object]] = []

    class HandoffExecutor(_RecordingExecutor):
        async def resume_pr_monitor_handoff(self, workspace_id: str) -> object:
            assert workspace_id == "ws_monitor"
            return handoff

        async def run_resumed_pr_monitor(self, workspace_id: str, handoff_obj: object) -> None:
            assert handoff_obj is handoff
            assert workspace_id == "ws_monitor"
            raise RuntimeError("monitor run failed")

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
        recovery_operation_id="op_post_handoff_runtime_error",
    )

    assert result is True
    assert len(finish_calls) == 1
    assert finish_calls[0]["status"] == OperationStatus.succeeded
    assert finish_calls[0]["operation_id"] == "op_post_handoff_runtime_error"


@pytest.mark.unit
async def test_safely_resume_pr_monitor_skips_monitor_when_finalize_never_succeeds(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    handoff = object()
    finish_calls: list[dict[str, object]] = []
    monitor_ran = False

    class HandoffExecutor(_RecordingExecutor):
        async def resume_pr_monitor_handoff(self, workspace_id: str) -> object:
            assert workspace_id == "ws_monitor"
            return handoff

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
        return kwargs.get("status") != OperationStatus.succeeded

    worker._finish_monitor_recovery_operation = (  # type: ignore[method-assign]
        _finish_monitor_recovery_operation
    )
    worker._monitor_recovery_operation_ids["ws_monitor"] = "op_finalize_never_succeeds"  # noqa: SLF001

    result = await worker._safely_resume_pr_monitor(  # noqa: SLF001
        "ws_monitor",
        recovery_operation_id="op_finalize_never_succeeds",
    )

    assert result is False
    assert monitor_ran is False
    assert len(finish_calls) == 2
    assert all(call["status"] == OperationStatus.succeeded for call in finish_calls)
    assert "ws_monitor" in worker._monitor_recovery_operation_ids  # noqa: SLF001


@pytest.mark.unit
async def test_safely_resume_claimed_pr_monitor_retains_claim_when_finalize_pending(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="https://github.com/example/repo.git",
            branch_base="main",
            task_title="monitor-recovery-finalize-pending",
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

    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=object(),  # type: ignore[arg-type]
        executor=object(),  # type: ignore[arg-type]
        config=WorkerConfig(poll_interval_seconds=0.01),
    )
    claim_released = False
    prompt_released = False

    async def _resume(
        resume_workspace_id: str,
        *,
        recovery_operation_id: str | None = None,
    ) -> bool:
        assert resume_workspace_id == workspace_id
        assert recovery_operation_id == "op_finalize_pending"
        return False

    async def _release_monitor_claim(released_workspace_id: str) -> None:
        nonlocal claim_released
        assert released_workspace_id == workspace_id
        claim_released = True

    async def _prompt_release(released_workspace_id: str) -> None:
        nonlocal prompt_released
        assert released_workspace_id == workspace_id
        prompt_released = True

    worker._monitor_recovery_operation_ids[workspace_id] = "op_finalize_pending"  # noqa: SLF001
    worker._safely_resume_pr_monitor = _resume  # type: ignore[method-assign]
    worker._release_monitoring_pr_claim = _release_monitor_claim  # type: ignore[method-assign]
    worker._release_terminal_runtime_promptly = _prompt_release  # type: ignore[method-assign]

    await worker._safely_resume_claimed_pr_monitor(  # noqa: SLF001
        workspace_id,
        recovery_operation_id="op_finalize_pending",
    )

    assert claim_released is False
    assert prompt_released is False
    assert worker._monitor_recovery_operation_ids[workspace_id] == "op_finalize_pending"  # noqa: SLF001
    retained_heartbeat = worker._monitor_claim_heartbeat_tasks.get(workspace_id)  # noqa: SLF001
    assert retained_heartbeat is not None
    assert not retained_heartbeat.done()
    assert not retained_heartbeat.cancelled()
    retained_heartbeat.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await retained_heartbeat
    worker._monitor_claim_heartbeat_tasks.pop(workspace_id, None)  # noqa: SLF001


@pytest.mark.unit
async def test_safely_resume_claimed_pr_monitor_releases_claim_when_finalize_pending_but_terminal(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Terminal workspaces must not retain a monitor claim with no retry path."""
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="https://github.com/example/repo.git",
            branch_base="main",
            task_title="monitor-recovery-terminal-finalize-pending",
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
        await repo.transition(ws, to=WorkspaceStatus.failed, reason_code="SEED")
        await session.commit()

    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=object(),  # type: ignore[arg-type]
        executor=object(),  # type: ignore[arg-type]
        config=WorkerConfig(poll_interval_seconds=0.01),
    )
    claim_released = False
    prompt_released = False
    finalize_calls: list[dict[str, object]] = []

    async def _resume(
        resume_workspace_id: str,
        *,
        recovery_operation_id: str | None = None,
    ) -> bool:
        assert resume_workspace_id == workspace_id
        assert recovery_operation_id == "op_terminal_finalize_pending"
        return False

    async def _release_monitor_claim(released_workspace_id: str) -> None:
        nonlocal claim_released
        assert released_workspace_id == workspace_id
        claim_released = True

    async def _prompt_release(released_workspace_id: str) -> None:
        nonlocal prompt_released
        assert released_workspace_id == workspace_id
        prompt_released = True

    async def _finish_monitor_recovery_operation(
        finish_workspace_id: str,
        **kwargs: object,
    ) -> bool:
        finalize_calls.append({"workspace_id": finish_workspace_id, **kwargs})
        return False

    worker._monitor_recovery_operation_ids[workspace_id] = "op_terminal_finalize_pending"  # noqa: SLF001
    worker._safely_resume_pr_monitor = _resume  # type: ignore[method-assign]
    worker._release_monitoring_pr_claim = _release_monitor_claim  # type: ignore[method-assign]
    worker._release_terminal_runtime_promptly = _prompt_release  # type: ignore[method-assign]
    worker._finish_monitor_recovery_operation = (  # type: ignore[method-assign]
        _finish_monitor_recovery_operation
    )

    await worker._safely_resume_claimed_pr_monitor(  # noqa: SLF001
        workspace_id,
        recovery_operation_id="op_terminal_finalize_pending",
    )

    assert claim_released is True
    assert prompt_released is True
    assert workspace_id not in worker._monitor_recovery_operation_ids  # noqa: SLF001
    assert worker._monitor_claim_heartbeat_tasks.get(workspace_id) is None  # noqa: SLF001
    assert len(finalize_calls) == 1
    assert finalize_calls[0]["status"] == OperationStatus.failed
    assert finalize_calls[0]["operation_id"] == "op_terminal_finalize_pending"


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
async def test_safely_resume_pr_monitor_corrects_operation_when_post_finalize_start_recheck_bails(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Post-handoff start recheck must not leave a succeeded remonitor op with no monitor."""
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
    assert len(finish_calls) == 2
    assert finish_calls[0]["operation_id"] == "op_post_finalize_recheck_bailed"
    assert finish_calls[0]["status"] == OperationStatus.succeeded
    assert finish_calls[1]["operation_id"] == "op_post_finalize_recheck_bailed"
    assert finish_calls[1]["status"] == OperationStatus.failed
    assert finish_calls[1]["error_code"] == "MONITOR_RECOVERY_FAILED"


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
    after_cancellation_calls: list[dict[str, object]] = []
    finalize_started = asyncio.Event()

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
        finish_calls.append({"workspace_id": workspace_id, **kwargs})
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
            "ws_monitor",
            recovery_operation_id="op_during_finalize",
        )
    )
    await asyncio.wait_for(finalize_started.wait(), timeout=5.0)
    resume_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await resume_task

    assert len(after_cancellation_calls) == 1
    assert after_cancellation_calls[0]["status"] == OperationStatus.succeeded
    assert after_cancellation_calls[0]["operation_id"] == "op_during_finalize"
    assert len(finish_calls) == 1


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
