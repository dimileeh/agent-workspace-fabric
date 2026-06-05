"""Focused branch-coverage tests for control worker scheduling helpers."""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import structlog
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.control.worker import (
    ControlWorker,
    WorkerConfig,
)
from awf.control.worker import cleanup as worker_cleanup
from awf.control.worker import helpers as worker_helpers
from awf.control.worker import recovery_preserved_queries as worker_recovery_preserved_queries
from awf.control.worker import recovery_stale as worker_recovery_stale
from awf.control.worker.constants import (
    _STALE_ACTIVE_EXECUTION_EVENT_TYPE,
    _STALE_ACTIVE_EXECUTION_REASON_CODE,
)
from awf.control.worker.helpers import (
    _active_execution_preservation_claim_cleanup_payload,
    _exception_chain_has_sqlalchemy_error,
    _execution_claim_is_stale,
    _worker_exception_is_transient_db_connection,
)
from awf.control.worker.scheduling import _record_scheduler_queue_decision
from awf.control.worker.types import _ActiveExecutionCandidate
from awf.db.enums import OperationStatus, OperationType, WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import (
    OperationRepository,
    QueueDecisionRepository,
    SecretLeaseIssue,
    SecretLeaseRepository,
    TaskAttemptRepository,
    TaskRepository,
    WorkspaceEventRepository,
    WorkspaceRepository,
)
from awf.db.resilience import (
    DB_CONNECTION_TRANSIENT_ATTEMPT_REASON,
    DB_CONNECTION_TRANSIENT_RECOVERED_REASON,
)
from awf.db.session import make_session_factory
from awf.node.cleanup import WorkspaceCleanupResult, WorkspaceCleanupStepResult
from awf.runtime.inspection import RuntimeSnapshot
from awf.service.workspace_runtime_health import (
    ACTIVE_EXECUTION_PRESERVED_EVENT_TYPE,
    ACTIVE_EXECUTION_PRESERVED_REASON_CODE,
)
from tests.postgres import postgres_test_engine


class _NoopProvisioner:
    async def provision(self, workspace_id: str) -> None:
        del workspace_id

    async def provision_claimed(
        self, workspace_id: str, execution_claim_epoch: int | None = None
    ) -> None:
        del workspace_id

    def get_worktree_path(self, workspace_id: str) -> Path | None:
        del workspace_id
        return None


class _RecordingExecutor:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.executed: list[str] = []
        self.resumed: list[str] = []

    async def execute(
        self,
        workspace_id: str,
        *,
        execution_owner_id: str | None = None,
        execution_lease_expires_at: datetime | None = None,
    ) -> None:
        del execution_owner_id, execution_lease_expires_at
        self.executed.append(workspace_id)
        if self.fail:
            raise RuntimeError("executor crashed")

    async def resume_pr_monitor(self, workspace_id: str) -> None:
        self.resumed.append(workspace_id)
        if self.fail:
            raise RuntimeError("monitor crashed")


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _worker(
    factory: async_sessionmaker[AsyncSession],
    *,
    executor: _RecordingExecutor | None = None,
    max_concurrent_executions: int = 2,
) -> ControlWorker:
    return ControlWorker(
        session_factory=factory,
        provisioner=_NoopProvisioner(),  # type: ignore[arg-type]
        executor=executor,
        config=WorkerConfig(
            poll_interval_seconds=0.01,
            max_concurrent_executions=max_concurrent_executions,
            monitor_claim_lease_seconds=30,
            execution_claim_lease_seconds=30,
            node_id="node-1",
        ),
    )


async def _seed_status(
    factory: async_sessionmaker[AsyncSession],
    status: WorkspaceStatus,
    *,
    title: str,
) -> str:
    async with factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.create(
            repo_url="git@example.com:repo/app.git",
            branch_base="main",
            task_title=title,
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        await repo.transition(ws, to=WorkspaceStatus.provisioning, reason_code="SEED")
        ws.branch_name = f"awf/{ws.id}"
        ws.remote_push_branch = ws.branch_name
        ws.base_commit = "a" * 40
        ws.compose_project_name = f"awf_{ws.id}"
        ws.compose_file_path = f"/tmp/{ws.id}/compose.yml"
        if status in {
            WorkspaceStatus.ready,
            WorkspaceStatus.running,
            WorkspaceStatus.validating,
            WorkspaceStatus.pushing,
            WorkspaceStatus.monitoring_pr,
        }:
            await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="SEED")
        if status in {
            WorkspaceStatus.running,
            WorkspaceStatus.validating,
            WorkspaceStatus.pushing,
            WorkspaceStatus.monitoring_pr,
        }:
            await repo.transition(ws, to=WorkspaceStatus.running, reason_code="SEED")
        if status in {
            WorkspaceStatus.validating,
            WorkspaceStatus.pushing,
            WorkspaceStatus.monitoring_pr,
        }:
            await repo.transition(ws, to=WorkspaceStatus.validating, reason_code="SEED")
        if status in {WorkspaceStatus.pushing, WorkspaceStatus.monitoring_pr}:
            await repo.transition(ws, to=WorkspaceStatus.pushing, reason_code="SEED")
        if status == WorkspaceStatus.monitoring_pr:
            ws.pr_number = 123
            ws.pr_url = "https://github.com/example/repo/pull/123"
            await repo.transition(ws, to=WorkspaceStatus.monitoring_pr, reason_code="SEED")
        await s.commit()
        return ws.id


class _ExplodingSessionFactory:
    calls = 0

    def __call__(self) -> object:
        self.calls += 1
        raise AssertionError("session factory should not be opened for empty limits")


class _PublicWorktreePathProvisioner(_NoopProvisioner):
    def __init__(self, root: Path) -> None:
        self.root = root
        self.requests: list[str] = []

    def get_worktree_path(self, workspace_id: str) -> Path:
        self.requests.append(workspace_id)
        return self.root / workspace_id


class _RefreshLoopWorker(ControlWorker):
    def __init__(self, *, raises: bool, refreshed: bool) -> None:
        super().__init__(
            session_factory=_ExplodingSessionFactory(),  # type: ignore[arg-type]
            provisioner=_NoopProvisioner(),  # type: ignore[arg-type]
            config=WorkerConfig(
                monitor_claim_lease_seconds=3,
                execution_claim_lease_seconds=3,
            ),
        )
        self.raises = raises
        self.refreshed = refreshed
        self.refreshed_once = asyncio.Event()
        self.monitor_refresh_calls = 0
        self.execution_refresh_calls = 0

    async def _refresh_monitoring_pr_claim(self, workspace_id: str) -> bool:
        assert workspace_id == "ws_loop"
        self.monitor_refresh_calls += 1
        self.refreshed_once.set()
        if self.raises:
            raise RuntimeError("monitor refresh failed")
        return self.refreshed

    async def _refresh_execution_claim(self, workspace_id: str) -> bool:
        assert workspace_id == "ws_loop"
        self.execution_refresh_calls += 1
        self.refreshed_once.set()
        if self.raises:
            raise RuntimeError("execution refresh failed")
        return self.refreshed


def _terminal_candidate(
    workspace_id: str = "ws_terminal",
    *,
    status: WorkspaceStatus = WorkspaceStatus.completed,
) -> worker_cleanup._TerminalRuntimeCandidate:  # noqa: SLF001
    return worker_cleanup._TerminalRuntimeCandidate(  # noqa: SLF001
        workspace_id=workspace_id,
        status=status,
        compose_project_name=f"awf_{workspace_id}",
        compose_file_path=None,
        repo_url="git@example.com:repo/app.git",
    )


@pytest.mark.unit
def test_preserved_active_worktree_path_uses_public_provisioner_method(tmp_path: Path) -> None:
    provisioner = _PublicWorktreePathProvisioner(tmp_path)
    worker = ControlWorker(
        session_factory=_ExplodingSessionFactory(),  # type: ignore[arg-type]
        provisioner=provisioner,  # type: ignore[arg-type]
        config=WorkerConfig(),
    )

    assert worker._preserved_active_worktree_path("ws_public") == tmp_path / "ws_public"  # noqa: SLF001
    assert provisioner.requests == ["ws_public"]
    assert not hasattr(provisioner, "_git")


@pytest.mark.unit
async def test_release_terminal_runtime_resources_propagates_single_candidate_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _terminal_candidate()

    async def _candidates(*, limit: int | None = None) -> list[object]:
        assert limit == 5
        return [candidate]

    async def _release(_candidate: object) -> None:
        raise RuntimeError("cleanup failed")

    async def _resume_pending(*, limit: int | None = None) -> None:
        assert limit == 5

    worker = SimpleNamespace(
        _runtime_cleaner=object(),
        _config=SimpleNamespace(terminal_runtime_release_max_per_scan=5),
        _resume_pending_planning_scope_auto_retries_after_terminal_release=_resume_pending,
        _list_terminal_runtime_candidates=_candidates,
        _release_terminal_runtime_for_candidate=_release,
    )
    monkeypatch.setattr(
        worker_cleanup,
        "_worker_exception_is_transient_db_connection",
        lambda _exc: False,
    )

    with pytest.raises(RuntimeError, match="cleanup failed"):
        await worker_cleanup._release_terminal_runtime_resources(worker)  # noqa: SLF001


@pytest.mark.unit
async def test_release_terminal_runtime_resources_groups_multiple_candidate_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidates = [_terminal_candidate("ws_one"), _terminal_candidate("ws_two")]

    async def _list_candidates(*, limit: int | None = None) -> list[object]:
        del limit
        return candidates

    async def _release(candidate: object) -> None:
        raise RuntimeError(f"cleanup failed for {candidate.workspace_id}")  # type: ignore[attr-defined]

    async def _resume_pending(*, limit: int | None = None) -> None:
        assert limit == 10

    worker = SimpleNamespace(
        _runtime_cleaner=object(),
        _config=SimpleNamespace(terminal_runtime_release_max_per_scan=10),
        _resume_pending_planning_scope_auto_retries_after_terminal_release=_resume_pending,
        _list_terminal_runtime_candidates=_list_candidates,
        _release_terminal_runtime_for_candidate=_release,
    )
    monkeypatch.setattr(
        worker_cleanup,
        "_worker_exception_is_transient_db_connection",
        lambda _exc: False,
    )

    with pytest.raises(ExceptionGroup) as exc_info:
        await worker_cleanup._release_terminal_runtime_resources(worker)  # noqa: SLF001

    assert len(exc_info.value.exceptions) == 2


@pytest.mark.unit
async def test_release_terminal_runtime_candidate_handles_missing_cleaner() -> None:
    worker = SimpleNamespace(_runtime_cleaner=None)

    await worker_cleanup._release_terminal_runtime_for_candidate(  # noqa: SLF001
        worker,
        _terminal_candidate(),
    )


@pytest.mark.unit
async def test_release_terminal_runtime_resources_skips_without_runtime_cleaner() -> None:
    session_factory = _ExplodingSessionFactory()

    async def _unexpected_list_candidates(*, limit: int | None = None) -> list[object]:
        raise AssertionError(f"candidate list should not run for limit {limit}")

    async def _unexpected_resume_pending(*, limit: int | None = None) -> None:
        raise AssertionError(f"retry resume should not run for limit {limit}")

    worker = SimpleNamespace(
        _runtime_cleaner=None,
        _config=SimpleNamespace(terminal_runtime_release_max_per_scan=5),
        _session_factory=session_factory,
        _resume_pending_planning_scope_auto_retries_after_terminal_release=_unexpected_resume_pending,
        _list_terminal_runtime_candidates=_unexpected_list_candidates,
        _release_terminal_runtime_for_candidate=lambda _candidate: None,
    )

    await worker_cleanup._release_terminal_runtime_resources(worker)  # noqa: SLF001

    assert session_factory.calls == 0


@pytest.mark.unit
async def test_release_terminal_runtime_candidate_records_cleanup_raise_and_swallows_record_error() -> (
    None
):
    class _Cleaner:
        async def cleanup(self, **_kwargs: object) -> WorkspaceCleanupResult:
            raise RuntimeError("docker failed")

    async def _record(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("record failed")

    worker = SimpleNamespace(
        _runtime_cleaner=_Cleaner(),
        _record_terminal_runtime_release_failed=_record,
    )

    await worker_cleanup._release_terminal_runtime_for_candidate(  # noqa: SLF001
        worker,
        _terminal_candidate(),
    )


@pytest.mark.unit
async def test_release_terminal_runtime_candidate_records_partial_cleanup_and_swallows_record_error() -> (
    None
):
    partial_cleanup = WorkspaceCleanupResult.from_steps(
        [
            WorkspaceCleanupStepResult(
                name="compose",
                status="failed",
                reason_code="COMPOSE_DOWN_FAILED",
                error="still running",
            )
        ]
    )

    class _Cleaner:
        async def cleanup(self, **_kwargs: object) -> WorkspaceCleanupResult:
            return partial_cleanup

    async def _record(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("record failed")

    worker = SimpleNamespace(
        _runtime_cleaner=_Cleaner(),
        _record_terminal_runtime_release_failed=_record,
    )

    await worker_cleanup._release_terminal_runtime_for_candidate(  # noqa: SLF001
        worker,
        _terminal_candidate(),
    )


@pytest.mark.unit
async def test_preserved_active_git_timeout_returns_failed_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    worker = ControlWorker(
        session_factory=_ExplodingSessionFactory(),  # type: ignore[arg-type]
        provisioner=_NoopProvisioner(),  # type: ignore[arg-type]
        config=WorkerConfig(),
    )
    recorded_kwargs: dict[str, object] = {}

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        recorded_kwargs.update(kwargs)
        raise subprocess.TimeoutExpired(
            cmd=args,
            timeout=kwargs["timeout"],
            output=b"partial stdout",
            stderr=b"partial stderr",
        )

    monkeypatch.setattr(worker_recovery_preserved_queries.subprocess, "run", _run)

    ok, stdout, stderr = await worker._run_preserved_active_git(  # noqa: SLF001
        tmp_path,
        "status",
        "--porcelain=v1",
    )

    assert not ok
    assert (
        recorded_kwargs["timeout"]
        == worker_recovery_preserved_queries._PRESERVED_ACTIVE_GIT_TIMEOUT_SECONDS
    )
    assert stdout == "partial stdout"
    assert "partial stderr" in stderr
    assert "git status --porcelain=v1 timed out" in stderr


@pytest.mark.unit
async def test_preserved_active_git_timeout_handles_missing_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    worker = ControlWorker(
        session_factory=_ExplodingSessionFactory(),  # type: ignore[arg-type]
        provisioner=_NoopProvisioner(),  # type: ignore[arg-type]
        config=WorkerConfig(),
    )

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(
            cmd=args,
            timeout=kwargs["timeout"],
            output=None,
            stderr=None,
        )

    monkeypatch.setattr(worker_recovery_preserved_queries.subprocess, "run", _run)

    ok, stdout, stderr = await worker._run_preserved_active_git(  # noqa: SLF001
        tmp_path,
        "rev-parse",
        "HEAD",
    )

    assert not ok
    assert stdout == ""
    assert "git rev-parse HEAD timed out" in stderr


@pytest.mark.unit
async def test_list_by_status_returns_empty_for_non_positive_limits() -> None:
    worker = ControlWorker(
        session_factory=_ExplodingSessionFactory(),  # type: ignore[arg-type]
        provisioner=_NoopProvisioner(),  # type: ignore[arg-type]
        config=WorkerConfig(),
    )

    assert await worker._list_by_status(WorkspaceStatus.ready, limit=0) == []
    assert await worker._list_by_status(WorkspaceStatus.ready, limit=-1) == []


@pytest.mark.unit
async def test_terminal_runtime_candidate_listing_returns_empty_for_non_positive_limits() -> None:
    worker = ControlWorker(
        session_factory=_ExplodingSessionFactory(),  # type: ignore[arg-type]
        provisioner=_NoopProvisioner(),  # type: ignore[arg-type]
        config=WorkerConfig(),
    )

    assert await worker._list_terminal_runtime_candidates(limit=0) == []  # noqa: SLF001
    assert await worker._list_terminal_runtime_candidates(limit=-1) == []  # noqa: SLF001


@pytest.mark.unit
async def test_scheduler_filter_helpers_return_empty_for_empty_or_unknown_inputs(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    worker = _worker(factory)
    async with factory() as session:
        assert (
            await worker._filter_scheduler_candidate_workspaces(  # noqa: SLF001
                session,
                [],
                limit=1,
                scoring_at=datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
            )
            == []
        )
        assert await worker._filter_provider_recovery_suppressed(session, []) == []  # noqa: SLF001
        assert (
            await worker._filter_provider_recovery_suppressed(  # noqa: SLF001
                session,
                [object()],  # type: ignore[list-item]
            )
            == []
        )


@pytest.mark.unit
def test_worker_claim_recheck_helpers_allow_non_claimed_statuses() -> None:
    ws = Workspace(
        id="ws_claim_recheck",
        status=WorkspaceStatus.completed.value,
        repo_url="git@example.com:repo/app.git",
        branch_base="main",
        task_title="claim recheck",
        task_prompt="prompt",
        agent="codex",
    )

    assert (
        worker_recovery_stale._claim_recheck_conditions(  # noqa: SLF001
            WorkspaceStatus.completed,
            datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
        )
        == ()
    )
    assert worker_helpers._workspace_claim_recheck_passes(  # noqa: SLF001
        ws,
        WorkspaceStatus.completed,
        datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
    )


@pytest.mark.unit
async def test_terminal_runtime_release_groups_multiple_candidate_failures() -> None:
    worker = ControlWorker(
        session_factory=_ExplodingSessionFactory(),  # type: ignore[arg-type]
        provisioner=_NoopProvisioner(),  # type: ignore[arg-type]
        runtime_cleaner=object(),  # type: ignore[arg-type]
        config=WorkerConfig(),
    )
    candidates = [
        worker_cleanup._TerminalRuntimeCandidate(  # noqa: SLF001
            workspace_id="ws_one",
            status=WorkspaceStatus.failed,
            repo_url="git@example.com:repo/app.git",
            compose_project_name="awf_ws_one",
            compose_file_path=None,
        ),
        worker_cleanup._TerminalRuntimeCandidate(  # noqa: SLF001
            workspace_id="ws_two",
            status=WorkspaceStatus.cancelled,
            repo_url="git@example.com:repo/app.git",
            compose_project_name="awf_ws_two",
            compose_file_path=None,
        ),
    ]

    async def list_candidates(*, limit: int | None = None) -> list[object]:
        assert limit is not None
        return candidates

    async def fail_candidate(candidate: object) -> None:
        assert candidate in candidates
        raise RuntimeError("release failed")

    worker._list_terminal_runtime_candidates = list_candidates  # type: ignore[method-assign]  # noqa: SLF001
    worker._release_terminal_runtime_for_candidate = fail_candidate  # type: ignore[method-assign]  # noqa: SLF001

    with pytest.raises(ExceptionGroup, match="terminal runtime release failed") as exc_info:
        await worker._release_terminal_runtime_resources()  # noqa: SLF001

    assert len(exc_info.value.exceptions) == 2


@pytest.mark.unit
async def test_release_terminal_runtime_resources_skips_empty_limit() -> None:
    session_factory = _ExplodingSessionFactory()

    async def _unexpected_list_candidates(*, limit: int | None = None) -> list[object]:
        raise AssertionError(f"candidate list should not run for limit {limit}")

    async def _unexpected_resume_pending(*, limit: int | None = None) -> None:
        raise AssertionError(f"retry resume should not run for limit {limit}")

    worker = SimpleNamespace(
        _runtime_cleaner=object(),
        _config=SimpleNamespace(terminal_runtime_release_max_per_scan=0),
        _session_factory=session_factory,
        _resume_pending_planning_scope_auto_retries_after_terminal_release=_unexpected_resume_pending,
        _list_terminal_runtime_candidates=_unexpected_list_candidates,
        _release_terminal_runtime_for_candidate=lambda _candidate: None,
    )

    await worker_cleanup._release_terminal_runtime_resources(worker)  # noqa: SLF001

    assert session_factory.calls == 0


@pytest.mark.unit
async def test_maybe_release_terminal_runtime_records_scan_after_non_transient_failure() -> None:
    worker = SimpleNamespace(
        _next_terminal_runtime_release_scan_at=0.0,
        _config=SimpleNamespace(terminal_runtime_release_scan_interval_seconds=5.0),
    )

    async def _release_resources() -> None:
        raise RuntimeError("release exploded")

    worker._release_terminal_runtime_resources = _release_resources

    await worker_cleanup._maybe_release_terminal_runtime(worker)  # noqa: SLF001

    assert worker._next_terminal_runtime_release_scan_at > 0


@pytest.mark.unit
async def test_terminal_runtime_release_candidate_records_cleanup_raise_failures() -> None:
    candidate = worker_cleanup._TerminalRuntimeCandidate(  # noqa: SLF001
        workspace_id="ws_cleanup_raised",
        status=WorkspaceStatus.failed,
        repo_url="git@example.com:repo/app.git",
        compose_project_name="awf_ws_cleanup_raised",
        compose_file_path="/tmp/compose.yml",
    )
    recorded: list[tuple[str, object | None, str]] = []

    class _FailingCleaner:
        async def cleanup(self, **_kwargs: object) -> WorkspaceCleanupResult:
            raise RuntimeError("compose gone")

    async def _record_failed(
        failed_candidate: object,
        *,
        cleanup: WorkspaceCleanupResult | None,
        message: str,
    ) -> None:
        recorded.append((failed_candidate.workspace_id, cleanup, message))  # type: ignore[attr-defined]

    worker = SimpleNamespace(
        _runtime_cleaner=_FailingCleaner(),
        _record_terminal_runtime_release_failed=_record_failed,
    )

    await worker_cleanup._release_terminal_runtime_for_candidate(worker, candidate)  # noqa: SLF001

    assert recorded == [
        ("ws_cleanup_raised", None, "runtime cleanup raised RuntimeError: compose gone")
    ]


@pytest.mark.unit
async def test_terminal_runtime_release_candidate_logs_record_failures() -> None:
    candidate = worker_cleanup._TerminalRuntimeCandidate(  # noqa: SLF001
        workspace_id="ws_record_failed",
        status=WorkspaceStatus.cancelled,
        repo_url="git@example.com:repo/app.git",
        compose_project_name="awf_ws_record_failed",
        compose_file_path=None,
    )
    cleanup = WorkspaceCleanupResult.from_steps(
        [
            WorkspaceCleanupStepResult(
                name="compose_down",
                status="failed",
                reason_code="COMPOSE_DOWN_FAILED",
                error="boom",
            )
        ]
    )

    class _Cleaner:
        async def cleanup(self, **_kwargs: object) -> WorkspaceCleanupResult:
            return cleanup

    async def _record_failed(
        _candidate: object,
        *,
        cleanup: WorkspaceCleanupResult | None,
        message: str,
    ) -> None:
        assert cleanup is not None
        assert message == "failed to stop or remove terminal workspace runtime"
        raise RuntimeError("record failed")

    worker = SimpleNamespace(
        _runtime_cleaner=_Cleaner(),
        _record_terminal_runtime_release_failed=_record_failed,
    )

    await worker_cleanup._release_terminal_runtime_for_candidate(worker, candidate)  # noqa: SLF001


@pytest.mark.unit
async def test_list_pending_delegates_to_requested_query_without_extra_filter() -> None:
    class _PendingAliasWorker(ControlWorker):
        def __init__(self) -> None:
            super().__init__(
                session_factory=_ExplodingSessionFactory(),  # type: ignore[arg-type]
                provisioner=_NoopProvisioner(),  # type: ignore[arg-type]
                config=WorkerConfig(),
            )
            self.requested_calls = 0

        async def _list_requested(self) -> list[str]:
            self.requested_calls += 1
            return ["ws_requested"]

    worker = _PendingAliasWorker()

    assert await worker._list_pending() == ["ws_requested"]
    assert worker.requested_calls == 1


@pytest.mark.unit
async def test_transient_db_retry_log_uses_attempt_reason_code() -> None:
    worker = ControlWorker(
        session_factory=_ExplodingSessionFactory(),  # type: ignore[arg-type]
        provisioner=_NoopProvisioner(),  # type: ignore[arg-type]
        config=WorkerConfig(node_id="node-1"),
    )

    with structlog.testing.capture_logs() as captured:
        await worker._log_transient_db_retry(
            RuntimeError("connection is closed"),
            attempt=1,
        )

    assert len(captured) == 1
    event = captured[0]
    assert event["event"] == "worker.db_connection_retry"
    assert event["log_level"] == "warning"
    assert event["reason_code"] == DB_CONNECTION_TRANSIENT_ATTEMPT_REASON
    assert event["reason_code"] != DB_CONNECTION_TRANSIENT_RECOVERED_REASON
    assert event["worker_id"].startswith("control-worker-")
    assert event["attempt"] == 1
    assert event["error_type"] == "RuntimeError"
    assert event["error"] == "connection is closed"


@pytest.mark.unit
async def test_stale_active_execution_recovery_continues_after_candidate_error() -> None:
    candidates = [
        _ActiveExecutionCandidate(
            workspace_id="ws_bad",
            status=WorkspaceStatus.running,
            compose_project_name="awf_bad",
        ),
        _ActiveExecutionCandidate(
            workspace_id="ws_good",
            status=WorkspaceStatus.validating,
            compose_project_name="awf_good",
        ),
    ]
    recovered: list[str] = []
    worker = ControlWorker(
        session_factory=_ExplodingSessionFactory(),  # type: ignore[arg-type]
        provisioner=_NoopProvisioner(),  # type: ignore[arg-type]
        config=WorkerConfig(node_id="node-1"),
    )

    async def _list_candidates(
        *,
        exclude_ids: set[str],
    ) -> list[_ActiveExecutionCandidate]:
        assert exclude_ids == set()
        return candidates

    async def _recover(candidate: _ActiveExecutionCandidate) -> None:
        recovered.append(candidate.workspace_id)
        if candidate.workspace_id == "ws_bad":
            raise RuntimeError("candidate recovery failed")

    worker._list_stale_active_execution_candidates = _list_candidates  # type: ignore[method-assign]
    worker._recover_stale_active_execution = _recover  # type: ignore[method-assign]

    with (
        structlog.testing.capture_logs() as captured,
        pytest.raises(RuntimeError, match="candidate recovery failed"),
    ):
        await worker._recover_stale_active_executions()

    assert recovered == ["ws_bad", "ws_good"]
    assert any(
        event.get("event") == "worker.stale_active_execution_recovery_failed"
        and event.get("log_level") == "error"
        and event.get("workspace_id") == "ws_bad"
        and event.get("status") == WorkspaceStatus.running.value
        and event.get("error_type") == "RuntimeError"
        and event.get("error") == "candidate recovery failed"
        for event in captured
    )


@pytest.mark.unit
async def test_provider_recovery_filter_skips_stale_scheduler_ids(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await _seed_status(factory, WorkspaceStatus.ready, title="claimable")
    worker = _worker(factory)

    async with factory() as session:
        filtered = await worker._filter_provider_recovery_suppressed(
            session,
            ["ws_deleted_before_claim", workspace_id],
        )

    assert filtered == [workspace_id]


@pytest.mark.unit
async def test_provider_recovery_filter_rejects_mixed_scheduler_inputs(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    worker = _worker(factory)

    async with factory() as session:
        filtered = await worker._filter_provider_recovery_suppressed(
            session,
            ["ws_ready", object()],  # type: ignore[list-item]
        )

    assert filtered == []


@pytest.mark.unit
async def test_record_scheduler_queue_decision_skips_workspace_without_attempt(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await _seed_status(factory, WorkspaceStatus.ready, title="without attempt")

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None

        await _record_scheduler_queue_decision(
            session,
            workspace,
            decision="deferred",
            reason_code="NO_ATTEMPT",
            decided_at=datetime(2026, 5, 9, 12, 0, tzinfo=UTC),
        )

        decisions = await QueueDecisionRepository(session).list_for_workspace(workspace_id)

    assert decisions == []


@pytest.mark.unit
async def test_record_scheduler_queue_decision_carries_latest_summaries(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    decided_at = datetime(2026, 5, 9, 12, 0, tzinfo=UTC)
    workspace_id = await _seed_status(factory, WorkspaceStatus.ready, title="with attempt")

    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.get(workspace_id)
        assert workspace is not None
        task = await TaskRepository(session).create_or_get(
            repo_url=workspace.repo_url,
            base_branch=workspace.branch_base,
            title=workspace.task_title,
            prompt=workspace.task_prompt,
            external_id=None,
            idempotency_key=f"queue-decision:{workspace.id}",
            task_class=workspace.task_class,
            owned_paths=list(workspace.owned_paths),
        )
        attempt = await TaskAttemptRepository(session).create_for_workspace(
            task=task,
            workspace=workspace,
        )
        queue_repo = QueueDecisionRepository(session)
        await queue_repo.create(
            workspace_id=workspace.id,
            task_id=task.id,
            attempt_id=attempt.id,
            decision="ordered",
            reason_code="PREVIOUS",
            class_priority=1,
            computed_priority=2.0,
            age_boost=0.5,
            retry_bonus=0.0,
            resource_summary={"cpu": 1},
            overlap_risk_summary={"paths": 2},
            score_summary={"previous": True},
            decided_at=decided_at - timedelta(minutes=1),
        )

        await _record_scheduler_queue_decision(
            session,
            workspace,
            decision="deferred",
            reason_code="TEST_DEFERRED",
            decided_at=decided_at,
        )

        decisions = await queue_repo.list_for_workspace(workspace_id, limit=2)

    assert decisions[0].decision == "deferred"
    assert decisions[0].resource_summary == {"cpu": 1}
    assert decisions[0].overlap_risk_summary == {"paths": 2}
    assert decisions[0].reason_code == "TEST_DEFERRED"
    assert decisions[0].score_summary["suppression"] == {"suppressed": False}
    assert decisions[1].reason_code == "PREVIOUS"


@pytest.mark.unit
def test_exception_chain_sqlalchemy_detection_handles_cause_context_and_groups() -> None:
    try:
        raise SQLAlchemyError("db")
    except SQLAlchemyError as cause:
        caused = RuntimeError("outer")
        caused.__cause__ = cause

    try:
        raise SQLAlchemyError("db")
    except SQLAlchemyError:
        try:
            raise RuntimeError("outer")
        except RuntimeError as context:
            contextual = context

    duplicate = RuntimeError("plain")
    duplicate_group = ExceptionGroup("duplicates", [duplicate, duplicate])

    assert _exception_chain_has_sqlalchemy_error(caused)
    assert _exception_chain_has_sqlalchemy_error(contextual)
    assert not _exception_chain_has_sqlalchemy_error(duplicate_group)


@pytest.mark.unit
def test_worker_transient_db_classifier_handles_implicit_context() -> None:
    try:
        raise SQLAlchemyError("connection is closed")
    except SQLAlchemyError:
        try:
            raise RuntimeError("scan wrapper failed")
        except RuntimeError as exc:
            wrapped = exc

    assert _worker_exception_is_transient_db_connection(wrapped)


@pytest.mark.unit
async def test_dispatch_ready_executions_respects_limit_and_existing_tasks(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    worker = _worker(factory)
    existing = asyncio.create_task(asyncio.sleep(0))
    worker._execution_tasks["ws_existing"] = existing

    dispatched = worker._dispatch_ready_executions(
        ["ws_existing", "ws_new", "ws_extra"],
        limit=1,
    )
    await worker.wait_for_execution_tasks()

    assert dispatched == {"ws_new"}
    assert worker._execution_tasks == {}


@pytest.mark.unit
async def test_dispatch_monitor_resumes_respects_limit_and_existing_tasks(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    worker = _worker(factory)
    existing = asyncio.create_task(asyncio.sleep(0))
    worker._execution_tasks["ws_existing"] = existing

    dispatched = worker._dispatch_monitor_resumes(
        ["ws_existing", "ws_monitor", "ws_extra"],
        limit=1,
    )
    await worker.wait_for_execution_tasks()

    assert dispatched == {"ws_monitor"}
    assert worker._execution_tasks == {}


@pytest.mark.unit
async def test_claim_monitoring_pr_ids_respects_limit_and_running_tasks(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    running_id = await _seed_status(factory, WorkspaceStatus.monitoring_pr, title="running")
    claimable_id = await _seed_status(factory, WorkspaceStatus.monitoring_pr, title="claimable")
    extra_id = await _seed_status(factory, WorkspaceStatus.monitoring_pr, title="extra")
    worker = _worker(factory)
    worker._execution_tasks[running_id] = asyncio.create_task(asyncio.sleep(0))

    claimed = await worker._claim_monitoring_pr_ids(
        [running_id, claimable_id, extra_id],
        limit=1,
    )
    await worker.wait_for_execution_tasks()

    assert claimed == [claimable_id]
    async with factory() as s:
        repo = WorkspaceRepository(s)
        running = await repo.get(running_id)
        claimable = await repo.get(claimable_id)
        extra = await repo.get(extra_id)
        assert running is not None and running.monitor_claimed_by is None
        assert claimable is not None and claimable.monitor_claimed_by == worker._worker_id
        assert extra is not None and extra.monitor_claimed_by is None


@pytest.mark.unit
async def test_claimed_execution_releases_claim_after_executor_exception(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await _seed_status(factory, WorkspaceStatus.running, title="claimed")
    executor = _RecordingExecutor(fail=True)
    worker = _worker(factory, executor=executor)
    async with factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.get(workspace_id)
        assert ws is not None
        ws.execution_claimed_by = worker._worker_id
        ws.execution_claim_expires_at = datetime.now(UTC) + timedelta(seconds=30)
        await s.commit()

    await worker._safely_execute_claimed(workspace_id)

    assert executor.executed == [workspace_id]
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.execution_claimed_by is None
        assert ws.execution_claim_expires_at is None


@pytest.mark.unit
async def test_claimed_monitor_resume_releases_claim_after_executor_exception(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await _seed_status(factory, WorkspaceStatus.monitoring_pr, title="monitor")
    executor = _RecordingExecutor(fail=True)
    worker = _worker(factory, executor=executor)
    async with factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.get(workspace_id)
        assert ws is not None
        ws.monitor_claimed_by = worker._worker_id
        ws.monitor_claim_expires_at = datetime.now(UTC) + timedelta(seconds=30)
        await s.commit()

    await worker._safely_resume_claimed_pr_monitor(workspace_id)

    assert executor.resumed == [workspace_id]
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.monitor_claimed_by is None
        assert ws.monitor_claim_expires_at is None


@pytest.mark.unit
async def test_refresh_helpers_extend_worker_claims(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    execution_id = await _seed_status(factory, WorkspaceStatus.running, title="execution")
    monitor_id = await _seed_status(factory, WorkspaceStatus.monitoring_pr, title="monitor")
    worker = _worker(factory)
    async with factory() as s:
        repo = WorkspaceRepository(s)
        execution = await repo.get(execution_id)
        monitor = await repo.get(monitor_id)
        assert execution is not None
        assert monitor is not None
        execution.execution_claimed_by = worker._worker_id
        execution.execution_claim_expires_at = datetime.now(UTC) + timedelta(seconds=1)
        monitor.monitor_claimed_by = worker._worker_id
        monitor.monitor_claim_expires_at = datetime.now(UTC) + timedelta(seconds=1)
        await s.commit()

    assert await worker._refresh_execution_claim(execution_id) is True
    assert await worker._refresh_monitoring_pr_claim(monitor_id) is True

    async with factory() as s:
        execution = await WorkspaceRepository(s).get(execution_id)
        monitor = await WorkspaceRepository(s).get(monitor_id)
        assert execution is not None and execution.execution_claimed_by == worker._worker_id
        assert monitor is not None and monitor.monitor_claimed_by == worker._worker_id
        assert execution.execution_claim_expires_at is not None
        assert monitor.monitor_claim_expires_at is not None


@pytest.mark.unit
async def test_release_helpers_swallow_session_failures() -> None:
    worker = ControlWorker(
        session_factory=_ExplodingSessionFactory(),  # type: ignore[arg-type]
        provisioner=_NoopProvisioner(),  # type: ignore[arg-type]
        config=WorkerConfig(),
    )

    await worker._release_execution_claim("ws_missing")
    await worker._release_monitoring_pr_claim("ws_missing")


@pytest.mark.unit
async def test_safely_execute_and_resume_noop_without_executor(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    worker = _worker(factory)

    await worker._safely_execute("ws_missing")
    await worker._safely_resume_pr_monitor("ws_missing")

    assert worker._execution_tasks == {}


@pytest.mark.unit
async def test_run_once_expires_due_secret_leases(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await _seed_status(factory, WorkspaceStatus.running, title="secret-expiry")
    issued_at = datetime.now(UTC) - timedelta(hours=2)
    async with factory() as s:
        workspace = await WorkspaceRepository(s).get(workspace_id)
        assert workspace is not None
        await SecretLeaseRepository(s).issue_declared_leases(
            workspace,
            leases=[
                SecretLeaseIssue(
                    secret_name="api-token",
                    kind="env",
                    target="API_TOKEN",
                    mode="ro",
                    required=True,
                    provider="env",
                    ref_digest="sha256:" + "a" * 64,
                    expires_at=issued_at + timedelta(hours=1),
                    issue_metadata={"profile": "local"},
                )
            ],
            now=issued_at,
        )
        await s.commit()

    worker = _worker(factory)

    assert await worker.run_once() == 0

    async with factory() as s:
        leases = await SecretLeaseRepository(s).list_for_workspace(workspace_id)

    assert [lease.status for lease in leases] == ["expired"]


@pytest.mark.unit
async def test_safely_resume_marks_recovery_operation_failed_without_executor(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await _seed_status(
        factory,
        WorkspaceStatus.monitoring_pr,
        title="missing-executor-monitor-recovery",
    )
    async with factory() as s:
        operation = await OperationRepository(s).create(
            workspace_id=workspace_id,
            operation_type=OperationType.remonitor,
            status=OperationStatus.running,
            payload={"requested_action": OperationType.remonitor.value},
        )
        operation_id = operation.id
        await s.commit()

    worker = _worker(factory)

    await worker._safely_resume_pr_monitor(
        workspace_id,
        recovery_operation_id=operation_id,
    )

    async with factory() as s:
        operation = await OperationRepository(s).get(operation_id)
        assert operation is not None
        assert operation.status == OperationStatus.failed.value
        assert operation.error_code == "MONITOR_RECOVERY_NO_EXECUTOR"
        assert operation.finished_at is not None


@pytest.mark.unit
async def test_record_stale_active_execution_detected_skips_diverged_or_fresh_claims(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    diverged_id = await _seed_status(factory, WorkspaceStatus.running, title="diverged")
    claimed_id = await _seed_status(factory, WorkspaceStatus.running, title="claimed")
    worker = _worker(factory)
    snapshot = RuntimeSnapshot(stack_state="running", services=[])
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(claimed_id)
        assert ws is not None
        ws.execution_claimed_by = "worker-a"
        ws.execution_claim_expires_at = datetime.now(UTC) + timedelta(seconds=30)
        await s.commit()

    await worker._record_stale_active_execution_detected(
        _ActiveExecutionCandidate(
            workspace_id=diverged_id,
            status=WorkspaceStatus.validating,
            compose_project_name="awf_diverged",
        ),
        snapshot,
    )
    await worker._record_stale_active_execution_detected(
        _ActiveExecutionCandidate(
            workspace_id=claimed_id,
            status=WorkspaceStatus.running,
            compose_project_name="awf_claimed",
        ),
        snapshot,
    )

    async with factory() as s:
        diverged = await WorkspaceRepository(s).get(diverged_id)
        claimed = await WorkspaceRepository(s).get(claimed_id)
        assert diverged is not None
        assert claimed is not None
        assert all(
            event.event_type != "workspace.stale_active_execution_detected"
            for event in diverged.events
        )
        assert all(
            event.event_type != "workspace.stale_active_execution_detected"
            for event in claimed.events
        )


@pytest.mark.unit
def test_execution_claim_is_stale_handles_missing_and_naive_datetimes() -> None:
    cutoff = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)

    assert _execution_claim_is_stale(
        SimpleNamespace(execution_claimed_by=None, execution_claim_expires_at=cutoff),
        cutoff,
    )
    assert _execution_claim_is_stale(
        SimpleNamespace(execution_claimed_by="worker", execution_claim_expires_at=None),
        cutoff,
    )
    assert _execution_claim_is_stale(
        SimpleNamespace(
            execution_claimed_by="worker",
            execution_claim_expires_at=datetime(2026, 4, 27, 11, 59),
        ),
        cutoff,
    )
    assert not _execution_claim_is_stale(
        SimpleNamespace(
            execution_claimed_by="worker",
            execution_claim_expires_at=datetime(2026, 4, 27, 12, 1),
        ),
        cutoff,
    )
    assert not _execution_claim_is_stale(
        SimpleNamespace(
            execution_claimed_by="worker",
            execution_claim_expires_at=datetime(2026, 4, 27, 12, 1, tzinfo=UTC),
        ),
        cutoff,
    )


@pytest.mark.unit
def test_active_execution_preservation_claim_cleanup_preserves_unexpired_claim() -> None:
    cutoff = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
    workspace = SimpleNamespace(
        execution_claimed_by="live-worker",
        execution_claim_expires_at=cutoff + timedelta(minutes=5),
    )

    assert _active_execution_preservation_claim_cleanup_payload(
        workspace,
        claim_cutoff=cutoff,
    ) == {
        "action": "preserved_unexpired",
        "reason_code": "UNEXPIRED_EXECUTION_CLAIM_PRESERVED_DURING_ACTIVE_EXECUTION_PRESERVATION",
        "previous_claimed_by": "live-worker",
        "previous_expires_at": "2026-04-27T12:05:00+00:00",
    }


@pytest.mark.unit
async def test_active_execution_preservation_checks_skip_missing_workspaces(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    worker = _worker(factory)
    candidate = _ActiveExecutionCandidate(
        workspace_id="ws_missing",
        status=WorkspaceStatus.running,
        compose_project_name="awf_missing",
    )

    assert not await worker._has_operator_refresh_after_latest_preservation(candidate)  # noqa: SLF001
    assert not await worker._has_current_preserved_active_execution(candidate)  # noqa: SLF001


@pytest.mark.unit
async def test_active_execution_event_queries_accept_event_floor(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    worker = _worker(factory)
    event_floor = datetime.now(UTC)

    async with factory() as session:
        assert not await worker._has_stale_active_execution_event(  # noqa: SLF001
            session,
            "ws_missing",
            event_floor=event_floor,
        )
        assert not await worker._has_preserved_active_execution_event(  # noqa: SLF001
            session,
            "ws_missing",
            WorkspaceStatus.running,
            event_floor=event_floor,
        )
        assert (
            await worker._latest_preserved_active_execution_at(  # noqa: SLF001
                session,
                "ws_missing",
                WorkspaceStatus.running,
                event_floor=event_floor,
            )
            is None
        )
        assert not await worker._has_stale_active_execution_event(  # noqa: SLF001
            session,
            "ws_missing",
        )
        assert not await worker._has_preserved_active_execution_event(  # noqa: SLF001
            session,
            "ws_missing",
            WorkspaceStatus.running,
        )
        assert (
            await worker._latest_preserved_active_execution_at(  # noqa: SLF001
                session,
                "ws_missing",
                WorkspaceStatus.running,
            )
            is None
        )


@pytest.mark.unit
async def test_record_preserved_active_execution_skips_missing_workspace(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    worker = _worker(factory)

    await worker._record_preserved_active_execution_after_restart(  # noqa: SLF001
        _ActiveExecutionCandidate(
            workspace_id="ws_missing",
            status=WorkspaceStatus.running,
            compose_project_name="awf_missing",
        ),
        RuntimeSnapshot(stack_state="running", services=[]),
    )


@pytest.mark.unit
async def test_stale_active_execution_can_fail_rejects_preserved_runtime(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await _seed_status(factory, WorkspaceStatus.running, title="preserved")
    worker = _worker(factory)
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get(workspace_id)
        assert ws is not None
        ws.execution_claimed_by = "stale-worker"
        ws.execution_claim_expires_at = datetime.now(UTC) - timedelta(seconds=30)
        await repo.add_event(
            ws,
            event_type=_STALE_ACTIVE_EXECUTION_EVENT_TYPE,
            reason_code=_STALE_ACTIVE_EXECUTION_REASON_CODE,
            payload={"workspace_status": WorkspaceStatus.running.value},
        )
        await repo.add_event(
            ws,
            event_type=ACTIVE_EXECUTION_PRESERVED_EVENT_TYPE,
            reason_code=ACTIVE_EXECUTION_PRESERVED_REASON_CODE,
            payload={"workspace_status": WorkspaceStatus.running.value},
        )
        await session.commit()

    assert not await worker._stale_active_execution_can_fail(  # noqa: SLF001
        _ActiveExecutionCandidate(
            workspace_id=workspace_id,
            status=WorkspaceStatus.running,
            compose_project_name=f"awf_{workspace_id}",
        )
    )


@pytest.mark.unit
async def test_stale_active_execution_can_fail_ignores_salvage_for_other_status(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await _seed_status(
        factory, WorkspaceStatus.running, title="status-scoped-salvage"
    )
    now = datetime.now(UTC)
    status_started_at = now - timedelta(minutes=10)
    claim_expires_at = now - timedelta(minutes=5)
    preserved_at = now - timedelta(minutes=4)
    mismatched_salvage_at = now - timedelta(minutes=3)
    stale_at = now - timedelta(minutes=2)

    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get(workspace_id)
        assert ws is not None
        ws.execution_claimed_by = "stale-worker"
        ws.execution_claim_expires_at = claim_expires_at
        state_events = await WorkspaceEventRepository(session).list(
            workspace_id=workspace_id,
            event_type="workspace.state_changed",
        )
        running_started = next(
            event for event in state_events if event.new_state == WorkspaceStatus.running.value
        )
        running_started.occurred_at = status_started_at
        preserved = await repo.add_event(
            ws,
            event_type=ACTIVE_EXECUTION_PRESERVED_EVENT_TYPE,
            reason_code=ACTIVE_EXECUTION_PRESERVED_REASON_CODE,
            payload={"workspace_status": WorkspaceStatus.running.value},
        )
        preserved.occurred_at = preserved_at
        salvage = await repo.add_event(
            ws,
            event_type="workspace.active_execution_salvage_operator_required",
            reason_code="ACTIVE_EXECUTION_SALVAGE_OPERATOR_REQUIRED",
            payload={
                "reason_code": "ACTIVE_EXECUTION_SALVAGE_OPERATOR_REQUIRED",
                "workspace_status": WorkspaceStatus.validating.value,
            },
        )
        salvage.occurred_at = mismatched_salvage_at
        stale = await repo.add_event(
            ws,
            event_type=_STALE_ACTIVE_EXECUTION_EVENT_TYPE,
            reason_code=_STALE_ACTIVE_EXECUTION_REASON_CODE,
            payload={"workspace_status": WorkspaceStatus.running.value},
        )
        stale.occurred_at = stale_at
        await session.commit()

    worker = ControlWorker(
        session_factory=factory,
        provisioner=_NoopProvisioner(),  # type: ignore[arg-type]
        config=WorkerConfig(active_execution_preservation_grace_seconds=0.0),
    )

    assert await worker._stale_active_execution_can_fail(  # noqa: SLF001
        _ActiveExecutionCandidate(
            workspace_id=workspace_id,
            status=WorkspaceStatus.running,
            compose_project_name=f"awf_{workspace_id}",
        )
    )
