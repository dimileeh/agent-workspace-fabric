"""ControlWorker tests (continued from test_worker_part_038).

Split out of ``test_worker_part_038`` to keep each test module under the
first-party 1500-line maintainability guardrail. Continued in
``test_worker_part_053``. These exercise the worker's DB-closed event handling,
dispatch limit helpers, active-salvage bookkeeping bounds, and the ``_safely_*``
failure-isolation paths.
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
from awf.control.worker import claims as worker_claims
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

    assert result is False
    assert len(finish_calls) == 1
    assert finish_calls[0]["operation_id"] == "op_cancelled_handoff"
    assert finish_calls[0]["status"] == OperationStatus.cancelled
    assert finish_calls[0]["error_code"] == "MONITOR_RECOVERY_CANCELLED"


@pytest.mark.unit
async def test_safely_resume_claimed_pr_monitor_skips_cooldown_when_cancelled_handoff(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Cancelled pre-handoff skip must not apply active-salvage monitor cooldown."""
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="https://github.com/example/repo.git",
            branch_base="main",
            task_title="monitor-recovery-cancelled-cooldown",
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

    cooldown_recorded = False

    class _CancelledHandoffExecutor(_RecordingExecutor):
        async def resume_pr_monitor_handoff(self, handoff_workspace_id: str) -> object | None:
            assert handoff_workspace_id == workspace_id
            return None

    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=object(),  # type: ignore[arg-type]
        executor=_CancelledHandoffExecutor(),
        config=WorkerConfig(poll_interval_seconds=0.01, monitor_claim_lease_seconds=30.0),
    )
    worker._remember_active_salvage_monitor_recovery_operation_id(  # noqa: SLF001
        "op_cancelled_handoff"
    )

    async def _finish_monitor_recovery_operation(
        finish_workspace_id: str,
        **kwargs: object,
    ) -> bool:
        assert finish_workspace_id == workspace_id
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

    async def _release_monitor_claim(release_workspace_id: str) -> None:
        assert release_workspace_id == workspace_id

    async def _prompt_release(release_workspace_id: str) -> None:
        assert release_workspace_id == workspace_id

    worker._release_monitoring_pr_claim = _release_monitor_claim  # type: ignore[method-assign]
    worker._release_terminal_runtime_promptly = _prompt_release  # type: ignore[method-assign]

    await worker._safely_resume_claimed_pr_monitor(  # noqa: SLF001
        workspace_id,
        recovery_operation_id="op_cancelled_handoff",
    )

    assert cooldown_recorded is False
    assert workspace_id not in worker._active_salvage_monitor_resume_cooldowns  # noqa: SLF001


@pytest.mark.unit
async def test_safely_resume_pr_monitor_completed_handoff_skips_classifies_operation_succeeded(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """When handoff returns None because the workspace completed, finalize succeeded."""
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="https://github.com/example/repo.git",
            branch_base="main",
            task_title="monitor-recovery-completed-handoff",
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

    finish_calls: list[dict[str, object]] = []

    class _CompletedHandoffExecutor(_RecordingExecutor):
        async def resume_pr_monitor_handoff(self, handoff_workspace_id: str) -> object | None:
            assert handoff_workspace_id == workspace_id
            return None

    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=object(),  # type: ignore[arg-type]
        executor=_CompletedHandoffExecutor(),
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
        recovery_operation_id="op_completed_handoff",
    )

    assert result is True
    assert len(finish_calls) == 1
    assert finish_calls[0]["operation_id"] == "op_completed_handoff"
    assert finish_calls[0]["status"] == OperationStatus.succeeded
    assert finish_calls[0].get("error_code") is None
    assert finish_calls[0].get("error_message") is None


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
async def test_safely_resume_claimed_pr_monitor_releases_claim_when_finalize_pending(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Pending finalize on a still-monitoring workspace must drop the claim for retry."""
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

    assert claim_released is True
    assert prompt_released is False
    assert worker._monitor_recovery_operation_ids[workspace_id] == "op_finalize_pending"  # noqa: SLF001
    assert worker._monitor_claim_heartbeat_tasks.get(workspace_id) is None  # noqa: SLF001


@pytest.mark.unit
async def test_workspace_is_monitoring_pr_returns_false_on_db_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transient DB errors must not skip terminal finalize eligibility."""

    class FakeSession:
        async def __aenter__(self) -> FakeSession:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    class FailingWorkspaceRepository:
        def __init__(self, _session: FakeSession) -> None:
            pass

        async def get(self, workspace_id: str) -> None:
            assert workspace_id == "ws_monitor"
            raise _closed_connection_error()

    monkeypatch.setattr(
        "awf.control.worker.claims.WorkspaceRepository",
        FailingWorkspaceRepository,
    )
    worker = ControlWorker(
        session_factory=lambda: FakeSession(),  # type: ignore[arg-type]
        provisioner=object(),  # type: ignore[arg-type]
        executor=object(),  # type: ignore[arg-type]
        config=WorkerConfig(poll_interval_seconds=0.01),
    )

    result = await worker_claims._workspace_is_monitoring_pr(worker, "ws_monitor")  # noqa: SLF001

    assert result is False


@pytest.mark.unit
async def test_safely_resume_claimed_pr_monitor_releases_claim_when_finalize_pending_db_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pending finalize must attempt terminal finalize when status lookup fails."""

    class FakeSession:
        async def __aenter__(self) -> FakeSession:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    class FailingWorkspaceRepository:
        def __init__(self, _session: FakeSession) -> None:
            pass

        async def get(self, workspace_id: str) -> None:
            assert workspace_id == "ws_monitor"
            raise _closed_connection_error()

    monkeypatch.setattr(
        "awf.control.worker.claims.WorkspaceRepository",
        FailingWorkspaceRepository,
    )
    worker = ControlWorker(
        session_factory=lambda: FakeSession(),  # type: ignore[arg-type]
        provisioner=object(),  # type: ignore[arg-type]
        executor=object(),  # type: ignore[arg-type]
        config=WorkerConfig(poll_interval_seconds=0.01),
    )
    workspace_id = "ws_monitor"
    claim_released = False
    finalize_called = False

    async def _resume(
        resume_workspace_id: str,
        *,
        recovery_operation_id: str | None = None,
    ) -> bool:
        assert resume_workspace_id == workspace_id
        assert recovery_operation_id == "op_finalize_pending_db_error"
        return False

    async def _release_monitor_claim(released_workspace_id: str) -> None:
        nonlocal claim_released
        assert released_workspace_id == workspace_id
        claim_released = True

    async def _finish_monitor_recovery_operation(
        *_args: object,
        **_kwargs: object,
    ) -> bool:
        nonlocal finalize_called
        finalize_called = True
        return True

    worker._monitor_recovery_operation_ids[workspace_id] = "op_finalize_pending_db_error"  # noqa: SLF001
    worker._safely_resume_pr_monitor = _resume  # type: ignore[method-assign]
    worker._release_monitoring_pr_claim = _release_monitor_claim  # type: ignore[method-assign]
    worker._finish_monitor_recovery_operation = (  # type: ignore[method-assign]
        _finish_monitor_recovery_operation
    )

    await worker._safely_resume_claimed_pr_monitor(  # noqa: SLF001
        workspace_id,
        recovery_operation_id="op_finalize_pending_db_error",
    )

    assert claim_released is True
    assert finalize_called is True
    assert workspace_id not in worker._monitor_recovery_operation_ids  # noqa: SLF001
    assert worker._monitor_claim_heartbeat_tasks.get(workspace_id) is None  # noqa: SLF001


@pytest.mark.unit
async def test_safely_resume_claimed_pr_monitor_releases_claim_when_finalize_pending_retry_lookup_fails(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Still-monitoring workspace must retry when only retry-eligibility lookup fails."""
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="https://github.com/example/repo.git",
            branch_base="main",
            task_title="monitor-recovery-finalize-pending-retry-lookup-fails",
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

    lookup_calls = 0

    class IntermittentWorkspaceRepository:
        def __init__(self, session: AsyncSession) -> None:
            self._session = session

        async def get(self, workspace_id: str) -> object:
            nonlocal lookup_calls
            lookup_calls += 1
            if lookup_calls == 1:
                raise _closed_connection_error()
            return await WorkspaceRepository(self._session).get(workspace_id)

    monkeypatch.setattr(
        "awf.control.worker.claims.WorkspaceRepository",
        IntermittentWorkspaceRepository,
    )
    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=object(),  # type: ignore[arg-type]
        executor=object(),  # type: ignore[arg-type]
        config=WorkerConfig(poll_interval_seconds=0.01),
    )
    claim_released = False
    finalize_called = False

    async def _resume(
        resume_workspace_id: str,
        *,
        recovery_operation_id: str | None = None,
    ) -> bool:
        assert resume_workspace_id == workspace_id
        assert recovery_operation_id == "op_retry_lookup_failed"
        return False

    async def _release_monitor_claim(released_workspace_id: str) -> None:
        nonlocal claim_released
        assert released_workspace_id == workspace_id
        claim_released = True

    async def _finish_monitor_recovery_operation(
        *_args: object,
        **_kwargs: object,
    ) -> bool:
        nonlocal finalize_called
        finalize_called = True
        return True

    worker._monitor_recovery_operation_ids[workspace_id] = "op_retry_lookup_failed"  # noqa: SLF001
    worker._safely_resume_pr_monitor = _resume  # type: ignore[method-assign]
    worker._release_monitoring_pr_claim = _release_monitor_claim  # type: ignore[method-assign]
    worker._finish_monitor_recovery_operation = (  # type: ignore[method-assign]
        _finish_monitor_recovery_operation
    )

    await worker._safely_resume_claimed_pr_monitor(  # noqa: SLF001
        workspace_id,
        recovery_operation_id="op_retry_lookup_failed",
    )

    assert claim_released is True
    assert finalize_called is False
    assert worker._monitor_recovery_operation_ids[workspace_id] == "op_retry_lookup_failed"  # noqa: SLF001
    assert worker._monitor_claim_heartbeat_tasks.get(workspace_id) is None  # noqa: SLF001


@pytest.mark.unit
async def test_safely_resume_claimed_pr_monitor_propagates_cancellation_when_still_monitoring(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelled resume must propagate after claim release on still-monitoring retry."""
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="https://github.com/example/repo.git",
            branch_base="main",
            task_title="monitor-recovery-cancel-still-monitoring",
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

    lookup_calls = 0
    resume_started = asyncio.Event()
    claim_released = False

    class IntermittentWorkspaceRepository:
        def __init__(self, session: AsyncSession) -> None:
            self._session = session

        async def get(self, workspace_id: str) -> object:
            nonlocal lookup_calls
            lookup_calls += 1
            if lookup_calls == 1:
                raise _closed_connection_error()
            return await WorkspaceRepository(self._session).get(workspace_id)

    monkeypatch.setattr(
        "awf.control.worker.claims.WorkspaceRepository",
        IntermittentWorkspaceRepository,
    )
    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=object(),  # type: ignore[arg-type]
        executor=object(),  # type: ignore[arg-type]
        config=WorkerConfig(poll_interval_seconds=0.01),
    )

    async def _resume(
        resume_workspace_id: str,
        *,
        recovery_operation_id: str | None = None,
    ) -> bool:
        assert resume_workspace_id == workspace_id
        assert recovery_operation_id == "op_cancel_still_monitoring"
        resume_started.set()
        await asyncio.Event().wait()
        return False

    async def _release_monitor_claim(released_workspace_id: str) -> None:
        nonlocal claim_released
        assert released_workspace_id == workspace_id
        claim_released = True

    worker._monitor_recovery_operation_ids[workspace_id] = "op_cancel_still_monitoring"  # noqa: SLF001
    worker._safely_resume_pr_monitor = _resume  # type: ignore[method-assign]
    worker._release_monitoring_pr_claim = _release_monitor_claim  # type: ignore[method-assign]

    resume_task = asyncio.create_task(
        worker._safely_resume_claimed_pr_monitor(  # noqa: SLF001
            workspace_id,
            recovery_operation_id="op_cancel_still_monitoring",
        )
    )
    await asyncio.wait_for(resume_started.wait(), timeout=5.0)
    resume_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await resume_task

    assert claim_released is True
    assert worker._monitor_recovery_operation_ids[workspace_id] == "op_cancel_still_monitoring"  # noqa: SLF001
    assert worker._monitor_claim_heartbeat_tasks.get(workspace_id) is None  # noqa: SLF001


@pytest.mark.unit
async def test_safely_resume_claimed_pr_monitor_finalizes_when_finalize_pending_db_error_but_terminal(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Terminal workspace must finalize even when retry-eligibility lookup fails."""
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="https://github.com/example/repo.git",
            branch_base="main",
            task_title="monitor-recovery-terminal-db-error",
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
        await repo.transition(ws, to=WorkspaceStatus.completed, reason_code="SEED")
        await session.commit()

    lookup_calls = 0

    class IntermittentWorkspaceRepository:
        def __init__(self, session: AsyncSession) -> None:
            self._session = session

        async def get(self, workspace_id: str) -> object:
            nonlocal lookup_calls
            lookup_calls += 1
            if lookup_calls == 1:
                raise _closed_connection_error()
            return await WorkspaceRepository(self._session).get(workspace_id)

    monkeypatch.setattr(
        "awf.control.worker.claims.WorkspaceRepository",
        IntermittentWorkspaceRepository,
    )
    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=object(),  # type: ignore[arg-type]
        executor=object(),  # type: ignore[arg-type]
        config=WorkerConfig(poll_interval_seconds=0.01),
    )
    claim_released = False
    finalize_calls: list[dict[str, object]] = []

    async def _resume(
        resume_workspace_id: str,
        *,
        recovery_operation_id: str | None = None,
    ) -> bool:
        assert resume_workspace_id == workspace_id
        assert recovery_operation_id == "op_terminal_db_error"
        return False

    async def _release_monitor_claim(released_workspace_id: str) -> None:
        nonlocal claim_released
        assert released_workspace_id == workspace_id
        claim_released = True

    async def _finish_monitor_recovery_operation(
        finish_workspace_id: str,
        **kwargs: object,
    ) -> bool:
        finalize_calls.append({"workspace_id": finish_workspace_id, **kwargs})
        return True

    worker._monitor_recovery_operation_ids[workspace_id] = "op_terminal_db_error"  # noqa: SLF001
    worker._safely_resume_pr_monitor = _resume  # type: ignore[method-assign]
    worker._release_monitoring_pr_claim = _release_monitor_claim  # type: ignore[method-assign]
    worker._finish_monitor_recovery_operation = (  # type: ignore[method-assign]
        _finish_monitor_recovery_operation
    )

    await worker._safely_resume_claimed_pr_monitor(  # noqa: SLF001
        workspace_id,
        recovery_operation_id="op_terminal_db_error",
    )

    assert claim_released is True
    assert workspace_id not in worker._monitor_recovery_operation_ids  # noqa: SLF001
    assert worker._monitor_claim_heartbeat_tasks.get(workspace_id) is None  # noqa: SLF001
    assert len(finalize_calls) == 1
    assert finalize_calls[0]["status"] == OperationStatus.succeeded
    assert finalize_calls[0]["operation_id"] == "op_terminal_db_error"


@pytest.mark.unit
async def test_safely_resume_claimed_pr_monitor_releases_claim_when_finalize_pending_but_terminal(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Terminal finalize success drops the claim and clears the recovery handle."""
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
        await repo.add_event(
            ws,
            event_type="workspace.monitor_runtime_restart_failed",
            reason_code="MONITOR_RECOVERY_METADATA_MISSING",
            payload={"reason_code": "MONITOR_RECOVERY_METADATA_MISSING"},
        )
        ws.failure_message = "Monitor recovery metadata missing after handoff."
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
        return True

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
    assert finalize_calls[0]["error_code"] == "MONITOR_RECOVERY_METADATA_MISSING"
    assert finalize_calls[0]["error_message"] == "Monitor recovery metadata missing after handoff."


@pytest.mark.unit
async def test_safely_resume_claimed_pr_monitor_preserves_succeeded_finalize_after_handoff_failed_race(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Pending finalize retry after successful handoff must not downgrade on failed races."""
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="https://github.com/example/repo.git",
            branch_base="main",
            task_title="monitor-recovery-handoff-success-finalize-race",
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
        executor=object(),  # type: ignore[arg-type]
        config=WorkerConfig(poll_interval_seconds=0.01),
    )
    claim_released = False
    finalize_calls: list[dict[str, object]] = []

    async def _resume(
        resume_workspace_id: str,
        *,
        recovery_operation_id: str | None = None,
    ) -> bool:
        assert resume_workspace_id == workspace_id
        assert recovery_operation_id == "op_handoff_success_finalize_race"
        worker._monitor_recovery_handoff_succeeded_workspace_ids.add(workspace_id)  # noqa: SLF001
        return False

    async def _release_monitor_claim(released_workspace_id: str) -> None:
        nonlocal claim_released
        assert released_workspace_id == workspace_id
        claim_released = True

    async def _finish_monitor_recovery_operation(
        finish_workspace_id: str,
        **kwargs: object,
    ) -> bool:
        finalize_calls.append({"workspace_id": finish_workspace_id, **kwargs})
        return True

    worker._monitor_recovery_operation_ids[workspace_id] = "op_handoff_success_finalize_race"  # noqa: SLF001
    worker._safely_resume_pr_monitor = _resume  # type: ignore[method-assign]
    worker._release_monitoring_pr_claim = _release_monitor_claim  # type: ignore[method-assign]
    worker._finish_monitor_recovery_operation = (  # type: ignore[method-assign]
        _finish_monitor_recovery_operation
    )

    await worker._safely_resume_claimed_pr_monitor(  # noqa: SLF001
        workspace_id,
        recovery_operation_id="op_handoff_success_finalize_race",
    )

    assert claim_released is True
    assert workspace_id not in worker._monitor_recovery_operation_ids  # noqa: SLF001
    assert workspace_id not in worker._monitor_recovery_handoff_succeeded_workspace_ids  # noqa: SLF001
    assert len(finalize_calls) == 1
    assert finalize_calls[0]["status"] == OperationStatus.succeeded
    assert finalize_calls[0]["operation_id"] == "op_handoff_success_finalize_race"
    assert finalize_calls[0]["error_code"] is None
    assert finalize_calls[0]["error_message"] is None


@pytest.mark.unit
async def test_safely_resume_claimed_pr_monitor_retains_handle_when_terminal_finalize_fails(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Failed terminal finalize must drop the claim but keep the recovery handle."""
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="https://github.com/example/repo.git",
            branch_base="main",
            task_title="monitor-recovery-terminal-finalize-failed",
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
        await repo.add_event(
            ws,
            event_type="workspace.monitor_runtime_restart_failed",
            reason_code="MONITOR_RECOVERY_METADATA_MISSING",
            payload={"reason_code": "MONITOR_RECOVERY_METADATA_MISSING"},
        )
        ws.failure_message = "Monitor recovery metadata missing after handoff."
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

    async def _resume(
        resume_workspace_id: str,
        *,
        recovery_operation_id: str | None = None,
    ) -> bool:
        assert resume_workspace_id == workspace_id
        assert recovery_operation_id == "op_terminal_finalize_failed"
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
        assert finish_workspace_id == workspace_id
        return False

    worker._monitor_recovery_operation_ids[workspace_id] = "op_terminal_finalize_failed"  # noqa: SLF001
    worker._safely_resume_pr_monitor = _resume  # type: ignore[method-assign]
    worker._release_monitoring_pr_claim = _release_monitor_claim  # type: ignore[method-assign]
    worker._release_terminal_runtime_promptly = _prompt_release  # type: ignore[method-assign]
    worker._finish_monitor_recovery_operation = (  # type: ignore[method-assign]
        _finish_monitor_recovery_operation
    )

    await worker._safely_resume_claimed_pr_monitor(  # noqa: SLF001
        workspace_id,
        recovery_operation_id="op_terminal_finalize_failed",
    )

    assert claim_released is True
    assert prompt_released is False
    assert worker._monitor_recovery_operation_ids[workspace_id] == "op_terminal_finalize_failed"  # noqa: SLF001
    assert worker._monitor_claim_heartbeat_tasks.get(workspace_id) is None  # noqa: SLF001
