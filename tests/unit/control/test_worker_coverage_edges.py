"""Focused branch-coverage tests for control worker scheduling helpers."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import structlog
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.control.worker import (
    _STALE_ACTIVE_EXECUTION_EVENT_TYPE,
    _STALE_ACTIVE_EXECUTION_REASON_CODE,
    ACTIVE_EXECUTION_PRESERVED_EVENT_TYPE,
    ACTIVE_EXECUTION_PRESERVED_REASON_CODE,
    ControlWorker,
    WorkerConfig,
    _active_execution_preservation_claim_cleanup_payload,
    _ActiveExecutionCandidate,
    _exception_chain_has_sqlalchemy_error,
    _execution_claim_is_stale,
    _has_running_agent_runtime,
    _json_datetime,
    _monitor_claim_is_stale,
    _record_scheduler_queue_decision,
    _stale_active_execution_failure_message,
    _utc_datetime,
    _worker_exception_is_transient_db_connection,
)
from awf.db.enums import OperationStatus, OperationType, WorkspaceStatus
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
from awf.runtime.inspection import RuntimeService, RuntimeSnapshot
from tests.postgres import postgres_test_engine


class _NoopProvisioner:
    async def provision(self, workspace_id: str) -> None:
        del workspace_id

    async def provision_claimed(self, workspace_id: str) -> None:
        del workspace_id


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

    async def _refresh_execution_claim(
        self,
        workspace_id: str,
        *,
        owner_id: str | None = None,
    ) -> bool:
        assert workspace_id == "ws_loop"
        assert owner_id is None or isinstance(owner_id, str)
        self.execution_refresh_calls += 1
        self.refreshed_once.set()
        if self.raises:
            raise RuntimeError("execution refresh failed")
        return self.refreshed


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
async def test_await_stale_cleanup_returns_none_when_heartbeat_finishes_first(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    worker = _worker(factory)
    cleanup = asyncio.create_task(
        asyncio.sleep(
            60,
            result=SimpleNamespace(ok=True),  # type: ignore[arg-type]
        )
    )
    heartbeat = asyncio.create_task(asyncio.sleep(0))

    result = await worker._await_stale_cleanup_or_claim_loss(  # noqa: SLF001
        cleanup,  # type: ignore[arg-type]
        heartbeat,
    )

    assert result is None
    assert cleanup.cancelled()


@pytest.mark.unit
async def test_finish_monitor_recovery_operation_edges(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    worker = _worker(factory)
    workspace_id = await _seed_status(
        factory,
        WorkspaceStatus.monitoring_pr,
        title="monitor recovery finish",
    )

    await worker._finish_monitor_recovery_operation(  # noqa: SLF001
        workspace_id,
        operation_id=None,
        status=OperationStatus.succeeded,
    )
    await worker._finish_monitor_recovery_operation(  # noqa: SLF001
        workspace_id,
        operation_id="missing-operation",
        status=OperationStatus.failed,
        error_code="MONITOR_FAILED",
        error_message="monitor failed",
    )
    async with factory() as session:
        operation = await OperationRepository(session).create(
            workspace_id=workspace_id,
            operation_type=OperationType.remonitor,
            status=OperationStatus.running,
            payload={"requested_action": OperationType.remonitor.value},
        )
        await session.commit()
        operation_id = operation.id

    await worker._finish_monitor_recovery_operation(  # noqa: SLF001
        workspace_id,
        operation_id=operation_id,
        status=OperationStatus.failed,
        error_code="MONITOR_FAILED",
        error_message="monitor failed",
    )

    async with factory() as session:
        finished = await OperationRepository(session).get(operation_id)
    assert finished is not None
    assert finished.status == OperationStatus.failed.value
    assert finished.error_code == "MONITOR_FAILED"
    assert finished.result["requested_action"] == OperationType.remonitor.value
    assert finished.result["pr_number"] == 123


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
async def test_provider_recovery_filter_ignores_unrecognized_scheduler_items(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    worker = _worker(factory)

    async with factory() as session:
        filtered = await worker._filter_provider_recovery_suppressed(  # noqa: SLF001
            session,
            [object()],  # type: ignore[list-item]
        )

    assert filtered == []


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
async def test_stale_active_execution_claim_rejects_preserved_runtime(
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

    assert not await worker._claim_stale_active_execution_cleanup(  # noqa: SLF001
        _ActiveExecutionCandidate(
            workspace_id=workspace_id,
            status=WorkspaceStatus.running,
            compose_project_name=f"awf_{workspace_id}",
        )
    )


@pytest.mark.unit
def test_monitor_claim_staleness_and_json_datetime_handle_naive_datetimes() -> None:
    cutoff = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)

    assert _monitor_claim_is_stale(
        SimpleNamespace(
            monitor_claimed_by="worker",
            monitor_claim_expires_at=datetime(2026, 4, 27, 11, 59),
        ),
        cutoff,
    )
    assert _json_datetime(datetime(2026, 4, 27, 12, 1)) == "2026-04-27T12:01:00+00:00"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("loop_name", "raises", "expected_event"),
    [
        ("monitor", True, "worker.monitor_claim_refresh_failed"),
        ("monitor", False, "worker.monitor_claim_lost"),
        ("execution", True, "worker.execution_claim_refresh_failed"),
        ("execution", False, "worker.execution_claim_lost"),
    ],
)
async def test_claim_refresh_loops_stop_after_refresh_failure_or_lost_claim(
    loop_name: str,
    raises: bool,
    expected_event: str,
) -> None:
    worker = _RefreshLoopWorker(raises=raises, refreshed=False)
    loop = (
        worker._refresh_monitoring_pr_claim_loop
        if loop_name == "monitor"
        else worker._refresh_execution_claim_loop
    )

    with structlog.testing.capture_logs() as captured:
        await asyncio.wait_for(loop("ws_loop"), timeout=2)

    assert any(event.get("event") == expected_event for event in captured)
    if loop_name == "monitor":
        assert worker.monitor_refresh_calls == 1
        assert worker.execution_refresh_calls == 0
    else:
        assert worker.execution_refresh_calls == 1
        assert worker.monitor_refresh_calls == 0


@pytest.mark.unit
@pytest.mark.parametrize("loop_name", ["monitor", "execution"])
async def test_claim_refresh_loops_continue_after_successful_refresh(loop_name: str) -> None:
    worker = _RefreshLoopWorker(raises=False, refreshed=True)
    loop = (
        worker._refresh_monitoring_pr_claim_loop
        if loop_name == "monitor"
        else worker._refresh_execution_claim_loop
    )

    task = asyncio.create_task(loop("ws_loop"))
    await asyncio.wait_for(worker.refreshed_once.wait(), timeout=2)
    await asyncio.sleep(0)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    if loop_name == "monitor":
        assert worker.monitor_refresh_calls == 1
        assert worker.execution_refresh_calls == 0
    else:
        assert worker.execution_refresh_calls == 1
        assert worker.monitor_refresh_calls == 0


@pytest.mark.unit
def test_stale_active_execution_failure_message_includes_runtime_reason() -> None:
    message = _stale_active_execution_failure_message(
        _ActiveExecutionCandidate(
            workspace_id="ws_runtime",
            status=WorkspaceStatus.running,
            compose_project_name="awf_ws_runtime",
        ),
        RuntimeSnapshot(stack_state="unavailable", reason=" docker unavailable \n", services=[]),
    )

    assert "compose runtime state is unavailable: docker unavailable" in message
    no_reason_message = _stale_active_execution_failure_message(
        _ActiveExecutionCandidate(
            workspace_id="ws_runtime",
            status=WorkspaceStatus.validating,
            compose_project_name="awf_ws_runtime",
        ),
        RuntimeSnapshot(stack_state="stopped", services=[]),
    )
    assert "compose runtime state is stopped." in no_reason_message


@pytest.mark.unit
def test_runtime_snapshot_requires_running_stack_before_agent_detection() -> None:
    assert not _has_running_agent_runtime(
        RuntimeSnapshot(
            stack_state="stopped",
            services=[
                RuntimeService(
                    name="agent",
                    container_id="agent-1",
                    image="awf-agent-runtime",
                    state="running",
                )
            ],
        )
    )


@pytest.mark.unit
def test_worker_utc_datetime_normalizes_naive_values() -> None:
    assert _utc_datetime(datetime(2026, 4, 27, 12, 0)) == datetime(
        2026,
        4,
        27,
        12,
        0,
        tzinfo=UTC,
    )


@pytest.mark.unit
async def test_wait_for_execution_tasks_removes_completed_tasks(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    worker = _worker(factory)
    worker._execution_tasks["ws_done"] = asyncio.create_task(asyncio.sleep(0))  # noqa: SLF001

    await worker.wait_for_execution_tasks()

    assert worker._execution_tasks == {}  # noqa: SLF001


@pytest.mark.unit
async def test_execution_and_monitor_claim_helpers_skip_already_running_task(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    worker = _worker(factory)
    task = asyncio.create_task(asyncio.sleep(30))
    worker._execution_tasks["ws_busy"] = task  # noqa: SLF001

    try:
        assert worker._dispatchable_execution_ids(  # noqa: SLF001
            ["ws_busy", "ws_next"],
            limit=2,
        ) == ["ws_next"]
        assert await worker._claim_monitoring_pr_ids(["ws_busy"], limit=1) == []  # noqa: SLF001
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.unit
async def test_dispatchable_execution_ids_stops_after_limit(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    worker = _worker(factory)

    assert worker._dispatchable_execution_ids(["ws_one", "ws_two"], limit=1) == ["ws_one"]  # noqa: SLF001


@pytest.mark.unit
async def test_finish_monitor_recovery_operation_skips_wrong_workspace(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await _seed_status(factory, WorkspaceStatus.monitoring_pr, title="operation")
    worker = _worker(factory)
    async with factory() as session:
        operation = await OperationRepository(session).create(
            workspace_id=workspace_id,
            operation_type=OperationType.remonitor,
            status=OperationStatus.running,
            payload={"requested_action": OperationType.remonitor.value},
        )
        operation_id = operation.id
        await session.commit()

    await worker._finish_monitor_recovery_operation(  # noqa: SLF001
        "ws_other",
        operation_id=operation_id,
        status=OperationStatus.succeeded,
    )

    async with factory() as session:
        operation = await OperationRepository(session).get(operation_id)

    assert operation is not None
    assert operation.status == OperationStatus.running.value


@pytest.mark.unit
async def test_stale_active_execution_without_runtime_cleaner_records_cleanup_failure(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await _seed_status(
        factory,
        WorkspaceStatus.running,
        title="stale active execution without cleaner",
    )
    worker = _worker(factory)
    candidate = _ActiveExecutionCandidate(
        workspace_id=workspace_id,
        status=WorkspaceStatus.running,
        compose_project_name=f"awf_{workspace_id}",
        repo_url="git@example.com:repo/app.git",
    )
    snapshot = RuntimeSnapshot(stack_state="running", reason="control worker restarted")
    assert await worker._record_stale_active_execution_detected(candidate, snapshot)  # noqa: SLF001

    await worker._cleanup_and_fail_stale_active_execution(candidate, snapshot)  # noqa: SLF001

    async with factory() as session:
        events = await WorkspaceEventRepository(session).list(
            workspace_id=workspace_id,
            event_type="workspace.stale_active_execution_cleanup_failed",
        )

    assert len(events) == 1
    assert events[0].reason_code == "STALE_ACTIVE_EXECUTION_CLEANUP_FAILED"
    assert events[0].payload["message"] == "runtime cleanup is not configured"


@pytest.mark.unit
async def test_fail_stale_active_execution_skips_status_mismatch(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await _seed_status(
        factory,
        WorkspaceStatus.running,
        title="stale active execution status mismatch",
    )
    worker = _worker(factory)

    await worker._fail_stale_active_execution(  # noqa: SLF001
        _ActiveExecutionCandidate(
            workspace_id=workspace_id,
            status=WorkspaceStatus.validating,
            compose_project_name=f"awf_{workspace_id}",
        ),
        RuntimeSnapshot(stack_state="running", reason="worker restarted"),
    )

    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)

    assert ws is not None
    assert ws.status == WorkspaceStatus.running.value


@pytest.mark.unit
async def test_cleanup_claim_allows_failure_clears_or_preserves_claim_by_state(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    worker = _worker(factory)
    owner = worker._stale_active_execution_cleanup_owner()  # noqa: SLF001
    changed_id = await _seed_status(factory, WorkspaceStatus.running, title="changed")
    wrong_owner_id = await _seed_status(factory, WorkspaceStatus.running, title="wrong owner")
    evidence_id = await _seed_status(factory, WorkspaceStatus.running, title="has evidence")
    no_evidence_id = await _seed_status(factory, WorkspaceStatus.running, title="no evidence")

    async with factory() as session:
        repo = WorkspaceRepository(session)
        changed = await repo.get(changed_id)
        wrong_owner = await repo.get(wrong_owner_id)
        evidence = await repo.get(evidence_id)
        no_evidence = await repo.get(no_evidence_id)
        assert changed and wrong_owner and evidence and no_evidence
        changed.status = WorkspaceStatus.ready.value
        changed.execution_claimed_by = owner
        changed.execution_claim_expires_at = datetime.now(UTC) + timedelta(minutes=5)
        wrong_owner.execution_claimed_by = "other-worker"
        wrong_owner.execution_claim_expires_at = datetime.now(UTC) + timedelta(minutes=5)
        evidence.execution_claimed_by = owner
        evidence.execution_claim_expires_at = datetime.now(UTC) + timedelta(minutes=5)
        no_evidence.execution_claimed_by = owner
        no_evidence.execution_claim_expires_at = datetime.now(UTC) + timedelta(minutes=5)
        await session.commit()

    evidence_calls = 0

    async def _has_evidence(
        _session: AsyncSession,
        workspace: object,
        _status: WorkspaceStatus,
        *,
        event_floor: datetime | None = None,
    ) -> bool:
        del _session, _status, event_floor
        nonlocal evidence_calls
        evidence_calls += 1
        return workspace.id == evidence_id

    worker._has_current_stale_active_execution_failure_evidence = _has_evidence  # type: ignore[method-assign]  # noqa: SLF001

    async with factory() as session:
        repo = WorkspaceRepository(session)
        assert not await worker._cleanup_claim_still_allows_stale_active_execution_failure(  # noqa: SLF001
            session,
            repo,
            _ActiveExecutionCandidate(
                workspace_id=changed_id,
                status=WorkspaceStatus.running,
                compose_project_name="awf_changed",
            ),
        )
        assert not await worker._cleanup_claim_still_allows_stale_active_execution_failure(  # noqa: SLF001
            session,
            repo,
            _ActiveExecutionCandidate(
                workspace_id=wrong_owner_id,
                status=WorkspaceStatus.running,
                compose_project_name="awf_wrong",
            ),
        )
        assert await worker._cleanup_claim_still_allows_stale_active_execution_failure(  # noqa: SLF001
            session,
            repo,
            _ActiveExecutionCandidate(
                workspace_id=evidence_id,
                status=WorkspaceStatus.running,
                compose_project_name="awf_evidence",
            ),
        )
        assert not await worker._cleanup_claim_still_allows_stale_active_execution_failure(  # noqa: SLF001
            session,
            repo,
            _ActiveExecutionCandidate(
                workspace_id=no_evidence_id,
                status=WorkspaceStatus.running,
                compose_project_name="awf_no_evidence",
            ),
        )
        await session.commit()

    async with factory() as session:
        changed = await WorkspaceRepository(session).get(changed_id)
        no_evidence = await WorkspaceRepository(session).get(no_evidence_id)
        evidence = await WorkspaceRepository(session).get(evidence_id)

    assert evidence_calls == 2
    assert changed is not None and changed.execution_claimed_by is None
    assert no_evidence is not None and no_evidence.execution_claimed_by is None
    assert evidence is not None and evidence.execution_claimed_by == owner


@pytest.mark.unit
async def test_record_ignored_stale_callback_logs_audit_failure_and_releases_claim(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await _seed_status(
        factory,
        WorkspaceStatus.running,
        title="blocked transition stale callback",
    )
    now = datetime.now(UTC)
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        ws.execution_claimed_by = "owner-1"
        ws.execution_claim_expires_at = now - timedelta(seconds=1)
        ws.monitor_claimed_by = "monitor-1"
        ws.monitor_claim_expires_at = now - timedelta(seconds=1)
        await session.commit()

    async def _raise_record_failure(self: object, *_args: object, **_kwargs: object) -> None:
        del self
        raise RuntimeError("audit sink unavailable")

    monkeypatch.setattr(
        WorkspaceRepository,
        "record_ignored_stale_callback",
        _raise_record_failure,
    )
    control_worker = _worker(factory)

    with structlog.testing.capture_logs() as captured:
        async with factory() as session:
            await control_worker._record_active_operation_blocked_transition_in_session(  # noqa: SLF001
                session,
                workspace_id=workspace_id,
                action="execute",
                expected=WorkspaceStatus.running,
                requested=WorkspaceStatus.failed,
                reason_code="STALE_CALLBACK",
                operation_id="op_1",
                release_execution_claim_owner_id="owner-1",
                clear_stale_claims_for_status=WorkspaceStatus.monitoring_pr,
            )
            await session.commit()

    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)

    assert ws is not None
    assert ws.execution_claimed_by is None
    assert ws.execution_claim_expires_at is None
    assert ws.monitor_claimed_by is None
    assert ws.monitor_claim_expires_at is None
    assert any(
        entry.get("event") == "worker.ignored_stale_callback_record_failed"
        and entry.get("workspace_id") == workspace_id
        and "audit sink unavailable" in str(entry.get("error"))
        for entry in captured
    )
