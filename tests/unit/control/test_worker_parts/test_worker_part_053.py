"""ControlWorker tests (continued from test_worker_part_047).

Split out of ``test_worker_part_047`` to keep each test module under the
first-party 1500-line maintainability guardrail. Continued in
``test_worker_part_054``.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
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
    """Test helper for pending execution task."""
    await asyncio.Event().wait()


@pytest.fixture
async def session_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Test helper for session factory."""
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


@pytest.fixture
def worker(session_factory: async_sessionmaker[AsyncSession], tmp_path: Path) -> ControlWorker:
    """Test helper for worker."""
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
    """Executor double that records execute and resume calls."""

    def __init__(self) -> None:
        """Test helper for __init__."""
        self.calls: list[str] = []
        self.resume_calls: list[str] = []

    async def execute(self, workspace_id: str, **_kwargs: object) -> None:
        """Test helper for execute."""
        self.calls.append(workspace_id)

    async def resume_pr_monitor_handoff(self, workspace_id: str) -> object | None:
        """Test helper for resume pr monitor handoff."""
        del workspace_id
        return object()

    async def run_resumed_pr_monitor(self, workspace_id: str, handoff: object) -> bool:
        """Test helper for run resumed pr monitor."""
        del handoff
        self.resume_calls.append(workspace_id)
        return True

    async def resume_pr_monitor(self, workspace_id: str) -> None:
        """Test helper for resume pr monitor."""
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
    """Verify monitor recovery start skipped operation status preserves success after handoff failed race."""
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
async def test_monitor_recovery_start_skipped_operation_status_marks_monitoring_pr_skip(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Post-handoff verify bail while still monitoring_pr must not look like handoff failure."""
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="https://github.com/example/repo.git",
            branch_base="main",
            task_title="monitor-recovery-start-skipped-monitoring",
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
    assert status == OperationStatus.failed
    assert error_code == "MONITOR_RECOVERY_START_SKIPPED"
    assert error_message == "Monitor resume skipped before monitor loop started."


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
        """Executor stub for monitor recovery handoff tests."""

        async def resume_pr_monitor_handoff(self, handoff_workspace_id: str) -> object:
            """Test helper for resume pr monitor handoff."""
            assert handoff_workspace_id == workspace_id
            return handoff

        async def verify_resume_monitor_start(self, handoff_workspace_id: str) -> bool:
            """Test helper for verify resume monitor start."""
            assert handoff_workspace_id == workspace_id
            return False

        async def run_resumed_pr_monitor(
            self,
            handoff_workspace_id: str,
            handoff_obj: object,
        ) -> None:
            """Test helper for run resumed pr monitor."""
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
        """Test helper for finish monitor recovery operation."""
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
async def test_safely_resume_pr_monitor_pre_finalize_verify_passes_handoff_monitor_owner_id(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Pre-finalize verify must fence on the handoff monitor_owner_id, not status alone."""
    from awf.control.executor.monitor_handoff import ResumeHandoff

    handoff = ResumeHandoff(
        monitor=object(),  # type: ignore[arg-type]
        compose_project="proj",
        compose_file=Path("/tmp/compose.yml"),
        run_kwargs={"monitor_owner_id": "worker-monitor-1"},
    )
    verify_kwargs: dict[str, object] = {}

    class HandoffExecutor(_RecordingExecutor):
        """Executor stub for monitor recovery handoff tests."""

        async def resume_pr_monitor_handoff(self, workspace_id: str) -> object:
            """Test helper for resume pr monitor handoff."""
            assert workspace_id == "ws_monitor"
            return handoff

        async def verify_resume_monitor_start(
            self,
            workspace_id: str,
            *,
            monitor_owner_id: str | None = None,
        ) -> bool:
            """Test helper for verify resume monitor start."""
            verify_kwargs["workspace_id"] = workspace_id
            verify_kwargs["monitor_owner_id"] = monitor_owner_id
            return True

        async def run_resumed_pr_monitor(self, workspace_id: str, handoff_obj: object) -> bool:
            """Test helper for run resumed pr monitor."""
            assert workspace_id == "ws_monitor"
            assert handoff_obj is handoff
            return True

    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=object(),  # type: ignore[arg-type]
        executor=HandoffExecutor(),
        config=WorkerConfig(poll_interval_seconds=0.01),
    )
    worker._finish_monitor_recovery_operation = AsyncMock(return_value=True)  # type: ignore[method-assign]

    result = await worker._safely_resume_pr_monitor(  # noqa: SLF001
        "ws_monitor",
        recovery_operation_id="op_owner_fence",
    )

    assert result is True
    assert verify_kwargs == {
        "workspace_id": "ws_monitor",
        "monitor_owner_id": "worker-monitor-1",
    }


@pytest.mark.unit
async def test_safely_resume_pr_monitor_fails_operation_when_start_recheck_bails(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Verify safely resume pr monitor fails operation when start recheck bails."""
    handoff = object()
    finish_calls: list[dict[str, object]] = []
    monitor_ran = False

    class HandoffExecutor(_RecordingExecutor):
        """Executor stub for monitor recovery handoff tests."""

        async def resume_pr_monitor_handoff(self, workspace_id: str) -> object:
            """Test helper for resume pr monitor handoff."""
            assert workspace_id == "ws_monitor"
            return handoff

        async def verify_resume_monitor_start(self, workspace_id: str) -> bool:
            """Test helper for verify resume monitor start."""
            assert workspace_id == "ws_monitor"
            return False

        async def run_resumed_pr_monitor(self, workspace_id: str, handoff_obj: object) -> None:
            """Test helper for run resumed pr monitor."""
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
        """Test helper for finish monitor recovery operation."""
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
async def test_safely_resume_pr_monitor_clears_handoff_marker_when_verify_skip_finalize_fails(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Verify-start skip must not leave a stale handoff marker when finalize fails."""
    handoff = object()

    class HandoffExecutor(_RecordingExecutor):
        """Executor stub for monitor recovery handoff tests."""

        async def resume_pr_monitor_handoff(self, workspace_id: str) -> object:
            """Test helper for resume pr monitor handoff."""
            assert workspace_id == "ws_monitor"
            return handoff

        async def verify_resume_monitor_start(self, workspace_id: str) -> bool:
            """Test helper for verify resume monitor start."""
            assert workspace_id == "ws_monitor"
            return False

    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=object(),  # type: ignore[arg-type]
        executor=HandoffExecutor(),
        config=WorkerConfig(poll_interval_seconds=0.01),
    )
    worker._monitor_recovery_operation_ids["ws_monitor"] = "op_verify_skip_finalize_failed"  # noqa: SLF001
    worker._finish_monitor_recovery_operation = AsyncMock(return_value=False)  # type: ignore[method-assign]

    result = await worker._safely_resume_pr_monitor(  # noqa: SLF001
        "ws_monitor",
        recovery_operation_id="op_verify_skip_finalize_failed",
    )

    assert result is False
    assert "ws_monitor" not in worker._monitor_recovery_handoff_succeeded_workspace_ids  # noqa: SLF001


@pytest.mark.unit
async def test_safely_resume_claimed_pr_monitor_skips_cooldown_when_start_recheck_bails(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Pre-finalize verify failure must not apply active-salvage monitor cooldown."""
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="https://github.com/example/repo.git",
            branch_base="main",
            task_title="monitor-recovery-start-recheck-bail",
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
    cooldown_recorded = False

    class HandoffExecutor(_RecordingExecutor):
        """Executor stub for monitor recovery handoff tests."""

        async def resume_pr_monitor_handoff(self, handoff_workspace_id: str) -> object:
            """Test helper for resume pr monitor handoff."""
            assert handoff_workspace_id == workspace_id
            return handoff

        async def verify_resume_monitor_start(self, handoff_workspace_id: str) -> bool:
            """Test helper for verify resume monitor start."""
            assert handoff_workspace_id == workspace_id
            return False

    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=object(),  # type: ignore[arg-type]
        executor=HandoffExecutor(),
        config=WorkerConfig(poll_interval_seconds=0.01, monitor_claim_lease_seconds=30.0),
    )
    worker._remember_active_salvage_monitor_recovery_operation_id(  # noqa: SLF001
        operation_id
    )

    async def _record_cooldown(_record_workspace_id: str, **kwargs: object) -> None:
        """Test helper for record cooldown."""
        nonlocal cooldown_recorded
        assert _record_workspace_id == workspace_id
        assert kwargs["recovery_operation_id"] == operation_id
        cooldown_recorded = True

    worker._record_active_salvage_monitor_resume_cooldown = (  # type: ignore[method-assign]
        _record_cooldown
    )

    async def _release_monitor_claim(released_workspace_id: str) -> None:
        """Test helper for release monitor claim."""
        assert released_workspace_id == workspace_id

    async def _prompt_release(released_workspace_id: str) -> None:
        """Test helper for prompt release."""
        assert released_workspace_id == workspace_id

    worker._release_monitoring_pr_claim = _release_monitor_claim  # type: ignore[method-assign]
    worker._release_terminal_runtime_promptly = _prompt_release  # type: ignore[method-assign]

    await worker._safely_resume_claimed_pr_monitor(  # noqa: SLF001
        workspace_id,
        recovery_operation_id=operation_id,
    )

    assert cooldown_recorded is False
    assert workspace_id not in worker._active_salvage_monitor_resume_cooldowns  # noqa: SLF001


@pytest.mark.unit
async def test_safely_resume_claimed_pr_monitor_skips_cooldown_when_post_finalize_start_recheck_bails(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Post-finalize verify failure must not apply active-salvage monitor cooldown."""
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="https://github.com/example/repo.git",
            branch_base="main",
            task_title="monitor-recovery-post-finalize-start-recheck-bail",
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
    verify_calls = 0
    cooldown_recorded = False

    class HandoffExecutor(_RecordingExecutor):
        """Executor stub for post-finalize monitor start recheck bail."""

        async def resume_pr_monitor_handoff(self, handoff_workspace_id: str) -> object:
            """Test helper for resume pr monitor handoff."""
            assert handoff_workspace_id == workspace_id
            return handoff

        async def verify_resume_monitor_start(self, handoff_workspace_id: str) -> bool:
            """Test helper for verify resume monitor start."""
            nonlocal verify_calls
            assert handoff_workspace_id == workspace_id
            verify_calls += 1
            return verify_calls == 1

        async def run_resumed_pr_monitor(
            self,
            handoff_workspace_id: str,
            handoff_obj: object,
        ) -> bool:
            """Test helper for run resumed pr monitor."""
            assert handoff_obj is handoff
            assert handoff_workspace_id == workspace_id
            if not await self.verify_resume_monitor_start(handoff_workspace_id):
                return False
            raise AssertionError("monitor run must not start when post-finalize recheck bails")

    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=object(),  # type: ignore[arg-type]
        executor=HandoffExecutor(),
        config=WorkerConfig(poll_interval_seconds=0.01, monitor_claim_lease_seconds=30.0),
    )
    worker._remember_active_salvage_monitor_recovery_operation_id(  # noqa: SLF001
        operation_id
    )

    async def _record_cooldown(_record_workspace_id: str, **kwargs: object) -> None:
        """Test helper for record cooldown."""
        nonlocal cooldown_recorded
        assert _record_workspace_id == workspace_id
        assert kwargs["recovery_operation_id"] == operation_id
        cooldown_recorded = True

    worker._record_active_salvage_monitor_resume_cooldown = (  # type: ignore[method-assign]
        _record_cooldown
    )

    async def _release_monitor_claim(released_workspace_id: str) -> None:
        """Test helper for release monitor claim."""
        assert released_workspace_id == workspace_id

    async def _prompt_release(released_workspace_id: str) -> None:
        """Test helper for prompt release."""
        assert released_workspace_id == workspace_id

    worker._release_monitoring_pr_claim = _release_monitor_claim  # type: ignore[method-assign]
    worker._release_terminal_runtime_promptly = _prompt_release  # type: ignore[method-assign]

    await worker._safely_resume_claimed_pr_monitor(  # noqa: SLF001
        workspace_id,
        recovery_operation_id=operation_id,
    )

    assert verify_calls == 2
    assert cooldown_recorded is False
    assert workspace_id not in worker._active_salvage_monitor_resume_cooldowns  # noqa: SLF001


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
        """Executor whose handoff raises to exercise abort paths."""

        async def resume_pr_monitor_handoff(self, handoff_workspace_id: str) -> object | None:
            """Test helper for resume pr monitor handoff."""
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
        """Test helper for finish monitor recovery operation."""
        assert finish_workspace_id == workspace_id
        assert kwargs["status"] == OperationStatus.failed
        return True

    async def _record_cooldown(_record_workspace_id: str, **kwargs: object) -> None:
        """Test helper for record cooldown."""
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
        """Test helper for release monitor claim."""
        assert released_workspace_id == workspace_id

    async def _prompt_release(released_workspace_id: str) -> None:
        """Test helper for prompt release."""
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
        """Executor that blocks in run_resumed_pr_monitor."""

        async def resume_pr_monitor_handoff(self, handoff_workspace_id: str) -> object:
            """Test helper for resume pr monitor handoff."""
            assert handoff_workspace_id == workspace_id
            return handoff

        async def run_resumed_pr_monitor(
            self,
            handoff_workspace_id: str,
            handoff_obj: object,
        ) -> None:
            """Test helper for run resumed pr monitor."""
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
        """Test helper for record cooldown."""
        nonlocal cooldown_recorded
        assert _record_workspace_id == workspace_id
        cooldown_recorded = True

    async def _release_monitor_claim(released_workspace_id: str) -> None:
        """Test helper for release monitor claim."""
        assert released_workspace_id == workspace_id

    async def _prompt_release(released_workspace_id: str) -> None:
        """Test helper for prompt release."""
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
        """Legacy executor stub with void run_resumed_pr_monitor."""

        async def resume_pr_monitor_handoff(self, workspace_id: str) -> object:
            """Test helper for resume pr monitor handoff."""
            assert workspace_id == "ws_monitor"
            return handoff

        async def run_resumed_pr_monitor(self, workspace_id: str, handoff_obj: object) -> None:
            """Test helper for run resumed pr monitor."""
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
        """Test helper for finish monitor recovery operation."""
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
        """Executor stub for monitor recovery handoff tests."""

        async def resume_pr_monitor_handoff(self, workspace_id: str) -> object:
            """Test helper for resume pr monitor handoff."""
            assert workspace_id == "ws_monitor"
            return handoff

        async def verify_resume_monitor_start(self, workspace_id: str) -> bool:
            """Test helper for verify resume monitor start."""
            nonlocal verify_calls
            assert workspace_id == "ws_monitor"
            verify_calls += 1
            return verify_calls == 1

        async def run_resumed_pr_monitor(self, workspace_id: str, handoff_obj: object) -> bool:
            """Test helper for run resumed pr monitor."""
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
        """Test helper for finish monitor recovery operation."""
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
    """Verify safely resume pr monitor post handoff cancellation does not cancel recovery op."""
    handoff = object()
    finish_calls: list[dict[str, object]] = []
    run_started = asyncio.Event()
    after_cancellation_called = False

    class BlockingRunExecutor(_RecordingExecutor):
        """Executor that blocks in run_resumed_pr_monitor."""

        async def resume_pr_monitor_handoff(self, workspace_id: str) -> object:
            """Test helper for resume pr monitor handoff."""
            assert workspace_id == "ws_monitor"
            return handoff

        async def run_resumed_pr_monitor(self, workspace_id: str, handoff_obj: object) -> None:
            """Test helper for run resumed pr monitor."""
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
        """Test helper for finish monitor recovery operation."""
        finish_calls.append({"workspace_id": workspace_id, **kwargs})
        return True

    async def _finish_after_cancellation(*args: object, **kwargs: object) -> None:
        """Test helper for finish after cancellation."""
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
