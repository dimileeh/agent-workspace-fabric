"""Authority-grace defer must retry across all claimed consumer entry paths.

Provisioning already keeps the claim and retries on ``WorkspaceAuthorityDeferredError``.
Execute, paused/monitor resume, and preserved-branch salvage must do the same so a
brief material lapse cannot fail or strand those workspaces during grace.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.control.worker import ControlWorker, WorkerConfig
from awf.control.worker import dispatch_methods as worker_dispatch
from awf.control.worker import recovery_preserved_flow as recovery
from awf.control.worker.types import _ActiveExecutionCandidate
from awf.db.enums import OperationStatus, WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.hosted_node.identity import (
    MaterialLapsedError,
    WorkspaceAuthorityDeferredError,
)
from tests.postgres import postgres_test_engine
from tests.unit.control._worker_admission_support import (
    _create_ready_with_runtime_metadata,
    _RecordingProvisioner,
    _workspace_status,
)

WORKER_TEST_TIMEOUT_SECONDS = 30.0


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _deferred() -> WorkspaceAuthorityDeferredError:
    return WorkspaceAuthorityDeferredError(MaterialLapsedError(expires_at=None))


class _DeferThenSucceedExecutor:
    """Raises WorkspaceAuthorityDeferredError once from execute, then succeeds."""

    def __init__(self) -> None:
        self.execute_calls = 0

    async def execute(self, workspace_id: str, **_kwargs: object) -> None:
        del workspace_id
        self.execute_calls += 1
        if self.execute_calls == 1:
            raise _deferred() from None

    async def resume_pr_monitor(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("execute defer test must not resume monitors")


class _DeferThenSucceedBlockedExecutor:
    """Raises deferred once from blocked resume, then succeeds."""

    def __init__(self) -> None:
        self.resume_calls = 0

    async def execute(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("blocked resume defer test must not execute")

    async def resume_pr_monitor(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("blocked resume defer test must not resume monitors")

    async def resume_blocked_execution(self, workspace_id: str, **_kwargs: object) -> None:
        del workspace_id
        self.resume_calls += 1
        if self.resume_calls == 1:
            raise _deferred() from None


class _DeferThenSucceedMonitorExecutor:
    """Raises deferred once from monitor handoff, then completes handoff + run."""

    def __init__(self) -> None:
        self.handoff_calls = 0
        self.run_calls = 0

    async def execute(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("monitor defer test must not execute")

    async def resume_pr_monitor(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("monitor defer test uses the handoff API")

    async def resume_pr_monitor_handoff(self, workspace_id: str) -> object:
        del workspace_id
        self.handoff_calls += 1
        if self.handoff_calls == 1:
            raise _deferred() from None
        return SimpleNamespace(run_kwargs={})

    async def verify_resume_monitor_start(self, workspace_id: str, **_kwargs: object) -> bool:
        del workspace_id, _kwargs
        return True

    async def run_resumed_pr_monitor(self, workspace_id: str, handoff: object) -> bool:
        del workspace_id, handoff
        self.run_calls += 1
        return True


@pytest.mark.unit
async def test_safely_execute_claimed_retries_authority_defer(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scope-entry defer at execute must keep the attempt and retry, not log-fail."""
    monkeypatch.setattr(worker_dispatch, "_AUTHORITY_DEFER_SECONDS", 0.0)
    workspace_id = await _create_ready_with_runtime_metadata(session_factory)
    executor = _DeferThenSucceedExecutor()
    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=_RecordingProvisioner(),  # type: ignore[arg-type]
        executor=executor,  # type: ignore[arg-type]
        config=WorkerConfig(max_concurrent_provisions=0, max_concurrent_executions=1),
    )

    await asyncio.wait_for(
        worker._safely_execute_claimed(workspace_id),  # noqa: SLF001
        timeout=WORKER_TEST_TIMEOUT_SECONDS,
    )

    assert executor.execute_calls == 2
    assert await _workspace_status(session_factory, workspace_id) == WorkspaceStatus.ready.value


@pytest.mark.unit
async def test_safely_resume_blocked_claimed_retries_authority_defer(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Blocked resume must retry defer instead of restoring pause + releasing claim."""
    monkeypatch.setattr(worker_dispatch, "_AUTHORITY_DEFER_SECONDS", 0.0)
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.create(
            repo_url="git@example.com:org/repo.git",
            branch_base="development",
            task_title="blocked defer resume",
            task_prompt="resume after authority defer",
            agent="codex",
            test_commands=[],
        )
        await repo.transition(workspace, to=WorkspaceStatus.provisioning, reason_code="SEED")
        workspace.compose_project_name = f"awf_{workspace.id}"
        workspace.compose_file_path = f"/tmp/{workspace.id}/compose.yml"
        await repo.transition(workspace, to=WorkspaceStatus.ready, reason_code="SEED")
        await repo.transition(workspace, to=WorkspaceStatus.running, reason_code="SEED")
        await repo.transition(workspace, to=WorkspaceStatus.blocked, reason_code="SEED")
        workspace.block_reason_code = "PROTECTED_PATH_VIOLATION"
        workspace.block_type = "protected_path"
        workspace.block_epoch = 1
        workspace.execution_claimed_by = "control-worker"
        workspace.execution_claim_expires_at = datetime.now(UTC) + timedelta(minutes=5)
        await session.commit()
        workspace_id = workspace.id

    executor = _DeferThenSucceedBlockedExecutor()
    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=_RecordingProvisioner(),  # type: ignore[arg-type]
        executor=executor,  # type: ignore[arg-type]
        config=WorkerConfig(max_concurrent_provisions=0, max_concurrent_executions=1),
    )
    assert await worker._claim_blocked_for_resume(workspace_id)  # noqa: SLF001

    await asyncio.wait_for(
        worker._safely_resume_blocked_claimed(workspace_id),  # noqa: SLF001
        timeout=WORKER_TEST_TIMEOUT_SECONDS,
    )

    assert executor.resume_calls == 2
    # Successful resume leaves the row wherever the executor left it (still
    # ``running`` for this stub) rather than reverting to ``blocked`` on defer.
    assert await _workspace_status(session_factory, workspace_id) == WorkspaceStatus.running.value


@pytest.mark.unit
async def test_safely_resume_pr_monitor_retries_authority_defer(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Monitor handoff defer must retry instead of failing the remonitor operation."""
    monkeypatch.setattr(worker_dispatch, "_AUTHORITY_DEFER_SECONDS", 0.0)
    executor = _DeferThenSucceedMonitorExecutor()
    finish_calls: list[dict[str, object]] = []

    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=_RecordingProvisioner(),  # type: ignore[arg-type]
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

    result = await asyncio.wait_for(
        worker._safely_resume_pr_monitor(  # noqa: SLF001
            "ws_monitor",
            recovery_operation_id="op_authority_defer",
        ),
        timeout=WORKER_TEST_TIMEOUT_SECONDS,
    )

    assert result is True
    assert executor.handoff_calls == 2
    assert executor.run_calls == 1
    assert len(finish_calls) == 1
    assert finish_calls[0]["status"] == OperationStatus.succeeded


@pytest.mark.unit
async def test_preserved_branch_salvage_defers_authority_without_failing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preserved-branch scope defer must return True so stale recovery skips fail paths."""

    @asynccontextmanager
    async def _deferred_scope(_workspace_id: str) -> AsyncIterator[None]:
        raise _deferred()
        yield  # pragma: no cover - unreachable after raise

    blocked_calls: list[str] = []

    class _WorkspaceRepo:
        def __init__(self, _session: object) -> None:
            pass

        async def get_for_update(self, workspace_id: str) -> object:
            return SimpleNamespace(
                id=workspace_id,
                status=WorkspaceStatus.running.value,
                pr_url=None,
                pr_number=None,
                repo_url="https://example.test/repo.git",
                branch_name="awf/ws_recover",
                remote_push_branch=None,
                base_commit="abc123",
                resolved_profile={},
                task_attempt=None,
                task_policy={},
            )

    monkeypatch.setattr(recovery, "WorkspaceRepository", _WorkspaceRepo)

    async def _event_floor(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def _latest_preserved(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(id="evt-preserve", occurred_at=datetime(2026, 1, 1, tzinfo=UTC))

    async def _has_current_salvage(*_args: Any, **_kwargs: Any) -> bool:
        return False

    def _preservation_expired(_occurred_at: object) -> bool:
        return False

    async def _record_blocked(*_args: Any, **_kwargs: Any) -> None:
        blocked_calls.append("blocked")

    async def _has_active_validation(*_args: Any, **_kwargs: Any) -> bool:
        return False

    worker = SimpleNamespace(
        _session_factory=lambda: _AsyncSession(),
        _workspace_scope=_deferred_scope,
        _execution_tasks={},
        _has_active_preserved_validation_recovery=_has_active_validation,
        _active_execution_preservation_event_floor=_event_floor,
        _latest_preserved_active_execution_event=_latest_preserved,
        _has_current_salvage_event=_has_current_salvage,
        _active_execution_preservation_is_expired=_preservation_expired,
        _record_preserved_active_salvage_blocked=_record_blocked,
        _resolve_preserved_active_branch_open_pr=lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("branch lookup must not run while authority is deferred")
        ),
    )

    result = await recovery._recover_preserved_active_execution(  # noqa: SLF001
        worker,
        _ActiveExecutionCandidate(
            workspace_id="ws_recover",
            status=WorkspaceStatus.running,
            compose_project_name="awf_ws_recover",
            repo_url="https://example.test/repo.git",
        ),
    )

    assert result is True
    assert blocked_calls == []


class _AsyncSession:
    async def __aenter__(self) -> _AsyncSession:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def commit(self) -> None:
        return None
