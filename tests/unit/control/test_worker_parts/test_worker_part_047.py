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
from awf.control.worker import recovery_cooldown as worker_recovery_cooldown
from awf.control.worker.types import (
    _ActiveExecutionCandidate,
)
from awf.db.enums import OperationStatus, WorkspaceStatus
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

    async def resume_pr_monitor(self, workspace_id: str) -> None:
        self.resume_calls.append(workspace_id)


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
        async def resume_pr_monitor(self, workspace_id: str) -> None:
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
    ) -> None:
        finish_calls.append({"workspace_id": workspace_id, **kwargs})

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

    assert finish_calls[0]["workspace_id"] == "ws_monitor"
    assert finish_calls[0]["operation_id"] == "op_resume_failed"
    assert finish_calls[0]["status"] == OperationStatus.failed
    assert finish_calls[0]["error_code"] == "MONITOR_RECOVERY_FAILED"
    assert "resume failed" in str(finish_calls[0]["error_message"])


@pytest.mark.unit
async def test_safely_provision_isolates_epoch_read_failure(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A transient failure on the fencing epoch read must not abort the batch.

    ``run_once`` gathers provision tasks with ``return_exceptions=False``, so an
    exception escaping ``_safely_provision_claimed`` would propagate and wedge
    the rest of the cycle. The epoch read (D2) sits outside the inner provision
    try/except, so it must be isolated like a provision failure — logged and
    swallowed — and the claim released so the next poll re-claims and retries.
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

    # The claim was still released so the next poll re-claims and retries.
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
