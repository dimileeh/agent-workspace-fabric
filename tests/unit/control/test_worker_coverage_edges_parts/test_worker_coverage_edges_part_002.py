"""Focused branch-coverage tests for control worker scheduling helpers."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import BranchOpenPullRequestResolver
from awf.control.worker import (
    ControlWorker,
    WorkerConfig,
)
from awf.control.worker import dispatch_methods as worker_dispatch_methods
from awf.control.worker import helpers as worker_helpers
from awf.control.worker import recovery_cooldown as worker_recovery_cooldown
from awf.control.worker import recovery_stale as worker_recovery_stale
from awf.control.worker import resource_broker as worker_resource_broker
from awf.control.worker.types import _ActiveExecutionCandidate
from awf.db.enums import FailureReason, OperationStatus, OperationType, WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import (
    OperationRepository,
    WorkspaceEventRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
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


@pytest.mark.unit
def test_active_salvage_recovery_operation_id_cache_moves_recent_and_evicts_oldest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = SimpleNamespace(
        _active_salvage_monitor_recovery_operation_ids={"op-old": None, "op-keep": None}
    )
    monkeypatch.setattr(
        worker_recovery_cooldown,
        "_ACTIVE_SALVAGE_MONITOR_RECOVERY_OPERATION_ID_LIMIT",
        2,
    )

    worker_recovery_cooldown._remember_active_salvage_monitor_recovery_operation_id(  # noqa: SLF001
        worker,
        "op-keep",
    )
    worker_recovery_cooldown._remember_active_salvage_monitor_recovery_operation_id(  # noqa: SLF001
        worker,
        "op-new",
    )

    assert list(worker._active_salvage_monitor_recovery_operation_ids) == [
        "op-keep",
        "op-new",
    ]

    worker_recovery_cooldown._forget_active_salvage_monitor_recovery_operation_id(  # noqa: SLF001
        worker,
        "op-keep",
    )
    assert list(worker._active_salvage_monitor_recovery_operation_ids) == ["op-new"]


@pytest.mark.unit
async def test_active_salvage_resume_cooldown_blocks_claim_uses_persisted_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    worker = SimpleNamespace(
        _active_salvage_monitor_resume_cooldown_active=lambda _workspace_id: False,
    )

    async def _persisted(workspace_id: str) -> bool:
        calls.append(workspace_id)
        return True

    worker._persisted_active_salvage_monitor_resume_cooldown_active = _persisted

    assert await worker_recovery_cooldown._active_salvage_monitor_resume_cooldown_blocks_claim(  # noqa: SLF001
        worker,
        "ws_cooldown",
    )
    assert calls == ["ws_cooldown"]


@pytest.mark.unit
async def test_active_salvage_resume_cooldown_in_memory_blocks_claim() -> None:
    worker = SimpleNamespace(
        _active_salvage_monitor_resume_cooldowns={"ws_hot": 1_000_000_000_000.0},
        _evict_expired_salvage_monitor_cooldowns=lambda: None,
    )
    assert worker_recovery_cooldown._active_salvage_monitor_resume_cooldown_active(  # noqa: SLF001
        worker,
        "ws_hot",
    )
    assert not worker_recovery_cooldown._active_salvage_monitor_resume_cooldown_active(  # noqa: SLF001
        worker,
        "ws_other",
    )


@pytest.mark.unit
async def test_persisted_active_salvage_resume_cooldown_handles_zero_lease_and_event_fallback() -> (
    None
):
    disabled = SimpleNamespace(_config=SimpleNamespace(monitor_claim_lease_seconds=0.0))
    assert (
        not await worker_recovery_cooldown._persisted_active_salvage_monitor_resume_cooldown_active(  # noqa: SLF001
            disabled,
            "ws",
        )
    )

    class _Result:
        def scalar_one_or_none(self) -> object:
            return SimpleNamespace(
                payload="not-a-mapping",
                occurred_at=datetime.now(UTC) - timedelta(seconds=1),
            )

    class _Session:
        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def execute(self, _stmt: object) -> _Result:
            return _Result()

    active = SimpleNamespace(
        _config=SimpleNamespace(monitor_claim_lease_seconds=60.0),
        _session_factory=lambda: _Session(),
    )
    assert await worker_recovery_cooldown._persisted_active_salvage_monitor_resume_cooldown_active(  # noqa: SLF001
        active,
        "ws",
    )


@pytest.mark.unit
async def test_persisted_active_salvage_resume_cooldown_handles_missing_and_expired_events() -> (
    None
):
    class _Result:
        def __init__(self, event: object | None) -> None:
            self.event = event

        def scalar_one_or_none(self) -> object | None:
            return self.event

    class _Session:
        def __init__(self, event: object | None) -> None:
            self.event = event

        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def execute(self, _stmt: object) -> _Result:
            return _Result(self.event)

    missing = SimpleNamespace(
        _config=SimpleNamespace(monitor_claim_lease_seconds=60.0),
        _session_factory=lambda: _Session(None),
    )
    assert (
        not await worker_recovery_cooldown._persisted_active_salvage_monitor_resume_cooldown_active(  # noqa: SLF001
            missing,
            "ws_missing",
        )
    )

    expired_event = SimpleNamespace(
        payload={"cooldown_until": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()},
        occurred_at=datetime.now(UTC),
    )
    expired = SimpleNamespace(
        _config=SimpleNamespace(monitor_claim_lease_seconds=60.0),
        _session_factory=lambda: _Session(expired_event),
    )
    assert (
        not await worker_recovery_cooldown._persisted_active_salvage_monitor_resume_cooldown_active(  # noqa: SLF001
            expired,
            "ws_expired",
        )
    )


@pytest.mark.unit
async def test_active_salvage_resume_cooldown_record_swallows_session_failures() -> None:
    class _FailingSession:
        async def __aenter__(self) -> object:
            raise RuntimeError("database offline")

        async def __aexit__(self, *_args: object) -> None:
            return None

    worker = SimpleNamespace(
        _session_factory=lambda: _FailingSession(),
        _worker_id="worker-1",
    )

    await worker_recovery_cooldown._record_active_salvage_monitor_resume_cooldown(  # noqa: SLF001
        worker,
        "ws",
        recovery_operation_id="op",
        cooldown_until=datetime.now(UTC),
    )


@pytest.mark.unit
async def test_active_salvage_resume_cooldown_record_skips_missing_or_wrong_status_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Session:
        committed = False

        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def commit(self) -> None:
            self.committed = True

    class _Repo:
        workspace: object | None = None
        events: list[object] = []

        def __init__(self, session: object) -> None:
            self.session = session

        async def get(self, _workspace_id: str) -> object | None:
            return self.workspace

        async def add_event(self, *_args: object, **_kwargs: object) -> None:
            self.events.append((_args, _kwargs))

    monkeypatch.setattr(worker_recovery_cooldown, "WorkspaceRepository", _Repo)
    worker = SimpleNamespace(_session_factory=lambda: _Session(), _worker_id="worker-1")

    await worker_recovery_cooldown._record_active_salvage_monitor_resume_cooldown(  # noqa: SLF001
        worker,
        "ws_missing",
        recovery_operation_id="op",
        cooldown_until=datetime.now(UTC),
    )
    assert _Repo.events == []

    _Repo.workspace = SimpleNamespace(status=WorkspaceStatus.running.value)
    await worker_recovery_cooldown._record_active_salvage_monitor_resume_cooldown(  # noqa: SLF001
        worker,
        "ws_running",
        recovery_operation_id="op",
        cooldown_until=datetime.now(UTC),
    )
    assert _Repo.events == []


@pytest.mark.unit
def test_active_salvage_resume_cooldown_expired_entry_is_evicted() -> None:
    worker = SimpleNamespace(
        _active_salvage_monitor_resume_cooldowns={"ws_expired": -1.0},
        _evict_expired_salvage_monitor_cooldowns=lambda: None,
    )

    assert not worker_recovery_cooldown._active_salvage_monitor_resume_cooldown_active(  # noqa: SLF001
        worker,
        "ws_expired",
    )
    assert worker._active_salvage_monitor_resume_cooldowns == {}


@pytest.mark.unit
def test_active_salvage_resume_cooldown_remember_evicts_expired_and_over_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = worker_recovery_cooldown.monotonic()
    worker = SimpleNamespace(
        _active_salvage_monitor_resume_cooldowns={
            "ws_expired": now - 1,
            "ws_old": now + 100,
            "ws_keep": now + 100,
        },
    )
    worker._evict_expired_salvage_monitor_cooldowns = (  # type: ignore[attr-defined]
        lambda: worker_recovery_cooldown._evict_expired_salvage_monitor_cooldowns(worker)  # noqa: SLF001
    )
    monkeypatch.setattr(
        worker_recovery_cooldown,
        "_ACTIVE_SALVAGE_MONITOR_RESUME_COOLDOWN_LIMIT",
        2,
    )

    worker_recovery_cooldown._remember_active_salvage_monitor_resume_cooldown(  # noqa: SLF001
        worker,
        "ws_new",
        now + 100,
    )

    assert list(worker._active_salvage_monitor_resume_cooldowns) == ["ws_keep", "ws_new"]


@pytest.mark.unit
def test_capacity_previous_resource_summary_strips_nested_previous() -> None:
    assert worker_resource_broker._capacity_previous_resource_summary(  # noqa: SLF001
        {"previous": {"stale": True}, "dind_slots": {"limit": 2}},
    ) == {"dind_slots": {"limit": 2}}


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
async def test_fail_stale_active_execution_restores_primary_failure_row_fields(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await _seed_status(
        factory,
        WorkspaceStatus.running,
        title="stale active execution restores primary failure fields",
    )
    primary_failure = {
        "failure_reason": FailureReason.validation_failure.value,
        "message": "pytest failed before remonitor",
        "reason_code": "PYTEST_TEST_FAILURE",
    }

    async def _load_preserved_primary(
        session: AsyncSession,
        workspace: object,
    ) -> SimpleNamespace:
        del session, workspace
        return SimpleNamespace(primary_failure=primary_failure, secondary_failures=())

    monkeypatch.setattr(
        worker_recovery_stale,
        "load_failure_causality_snapshot",
        _load_preserved_primary,
    )
    worker = _worker(factory)

    await worker._fail_stale_active_execution(  # noqa: SLF001
        _ActiveExecutionCandidate(
            workspace_id=workspace_id,
            status=WorkspaceStatus.running,
            compose_project_name=f"awf_{workspace_id}",
            repo_url="git@example.com:repo/app.git",
        ),
        RuntimeSnapshot(stack_state="stopped", reason="worker restarted"),
    )

    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        state_events = await WorkspaceEventRepository(session).list(
            workspace_id=workspace_id,
            event_type="workspace.state_changed",
        )

    assert ws is not None
    assert ws.status == WorkspaceStatus.failed.value
    assert ws.failure_reason == FailureReason.validation_failure.value
    assert ws.failure_message == "pytest failed before remonitor"
    latest_failed = next(
        event for event in state_events if event.new_state == WorkspaceStatus.failed.value
    )
    assert latest_failed.reason_code == "PYTEST_TEST_FAILURE"
    assert latest_failed.payload is not None
    assert latest_failed.payload["primary_failure"] == primary_failure
    assert latest_failed.payload["secondary_failure"]["reason_code"] == ("STALE_ACTIVE_EXECUTION")


@pytest.mark.unit
def test_open_pull_request_summary_helpers_cover_invalid_and_fallback_edges() -> None:
    summary = worker_helpers._open_pull_request_summary(  # noqa: SLF001
        {
            "url": " https://github.com/example/repo/pull/12 ",
            "number": "12",
            "headRefOid": "h" * 40,
            "headRepositoryNameWithOwner": "example/repo",
        },
        branch_name="feature/fallback",
    )

    assert summary.pr_url == "https://github.com/example/repo/pull/12"
    assert summary.pr_number == 12
    assert summary.head_ref == "feature/fallback"
    assert summary.head_sha == "h" * 40
    assert summary.head_repo_slug == "example/repo"

    object_summary = worker_helpers._open_pull_request_summary(  # noqa: SLF001
        SimpleNamespace(
            pr_url="https://github.com/example/repo/pull/13",
            pr_number=13,
            head_ref="feature/object",
        ),
        branch_name="feature/fallback",
    )
    assert object_summary.pr_number == 13
    assert object_summary.head_ref == "feature/object"

    for metadata, match in (
        ({"number": 12}, "missing pr_url"),
        ({"url": "https://github.com/example/repo/pull/12", "number": object()}, "pr_number"),
        ({"url": "https://github.com/example/repo/pull/12", "number": "not-int"}, "pr_number"),
        ({"url": "https://github.com/example/repo/pull/12", "number": 0}, "invalid"),
    ):
        with pytest.raises(ValueError, match=match):
            worker_helpers._open_pull_request_summary(  # noqa: SLF001
                metadata,
                branch_name="feature/fallback",
            )


@pytest.mark.unit
def test_pr_adoption_and_salvage_payload_helpers_cover_edges() -> None:
    assert (
        worker_helpers._expected_open_pr_head_repo_slug(  # noqa: SLF001
            "https://github.com/example/repo.git"
        )
        == "example/repo"
    )
    assert worker_helpers._expected_open_pr_head_repo_slug("not a github repo") is None  # noqa: SLF001

    workspace = Workspace(id="ws_policy")
    assert worker_helpers._pr_adoption_expected_head_repo_slug(workspace) is None  # noqa: SLF001
    workspace.task_policy = {"pr_adoption": {"head_repo_slug": " example/fork "}}
    assert (
        worker_helpers._pr_adoption_expected_head_repo_slug(workspace)  # noqa: SLF001
        == "example/fork"
    )
    workspace.task_policy = {"pr_adoption": {"head_repo_slug": "  "}}
    assert worker_helpers._pr_adoption_expected_head_repo_slug(workspace) is None  # noqa: SLF001

    assert worker_helpers._extract_pr_number("https://github.com/example/repo/pull/42") == 42  # noqa: SLF001
    assert worker_helpers._extract_pr_number("https://github.com/example/repo/issues/42") is None  # noqa: SLF001
    assert worker_helpers._extract_pr_number("https://github.com/example/repo/pull/0") is None  # noqa: SLF001
    assert worker_helpers._metadata_value({"number": 1}, "number") == 1  # noqa: SLF001
    assert worker_helpers._metadata_value(SimpleNamespace(number=2), "number") == 2  # noqa: SLF001
    assert worker_helpers._metadata_nonempty_str({"head": " value "}, "head") == "value"  # noqa: SLF001
    assert worker_helpers._metadata_nonempty_str({"head": " "}, "head") is None  # noqa: SLF001
    assert (
        worker_helpers._active_execution_salvage_idempotency_key(  # noqa: SLF001
            "validate",
            "ws_policy",
            "event-1",
        )
        == "active-salvage-validate:ws_policy:event-1"
    )

    candidate = _ActiveExecutionCandidate(
        workspace_id="ws_policy",
        status=WorkspaceStatus.validating,
        compose_project_name="awf_ws_policy",
        compose_file_path="/tmp/ws_policy/compose.yml",
    )
    preserved_event = SimpleNamespace(
        id="event-1",
        occurred_at=datetime(2026, 5, 21, 12, 0, tzinfo=UTC),
        event_type=ACTIVE_EXECUTION_PRESERVED_EVENT_TYPE,
        reason_code=ACTIVE_EXECUTION_PRESERVED_REASON_CODE,
        payload={"operation_id": "op-1"},
    )
    classification = worker_helpers._PreservedWorktreeClassification(  # noqa: SLF001
        state="salvageable",
        reason="clean branch",
        branch_name="feature/ws",
        base_commit="b" * 40,
        head_sha="h" * 40,
    )

    payload = worker_helpers._active_execution_salvage_payload(  # noqa: SLF001
        candidate,
        preserved_event=preserved_event,
        worker_id="worker-1",
        reason_code="ACTIVE_EXECUTION_SALVAGE_VALIDATION_REQUESTED",
        decision="validate",
        attempt_id="attempt-1",
        task_id="task-1",
        previous_claim={"execution_claimed_by": "old-worker"},
        claim_cleanup={"action": "cleared_stale"},
        classification=classification,
        extra={"recovery_mode": "validate_only"},
    )

    assert payload["preservation_event_id"] == "event-1"
    assert payload["classification"]["state"] == "salvageable"
    assert payload["base_commit"] == "b" * 40
    assert payload["head_sha"] == "h" * 40
    assert payload["recovery_mode"] == "validate_only"
    assert worker_helpers._is_active_execution_salvage_validation_payload(payload)  # noqa: SLF001
    assert not worker_helpers._is_active_execution_salvage_validation_payload({})  # noqa: SLF001
    assert worker_helpers._payload_preservation_event_id(payload) == "event-1"  # noqa: SLF001
    assert (
        worker_helpers._payload_preservation_event_id(  # noqa: SLF001
            {"preservation_event": {"id": " nested-event "}}
        )
        == "nested-event"
    )
    assert worker_helpers._payload_preservation_event_id({"preservation_event": {}}) is None  # noqa: SLF001


@pytest.mark.unit
async def test_preserved_active_branch_lookup_reports_resolver_failures(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    worker = _worker(factory)

    class _FailingResolver:
        async def resolve(self, **_kwargs: object) -> list[object]:
            raise RuntimeError("github unavailable")

    worker._open_pr_resolver = _FailingResolver()  # type: ignore[assignment]

    lookup = await worker._resolve_preserved_active_branch_open_pr(  # noqa: SLF001
        repo_url="https://github.com/example/repo.git",
        branch_name=" feature/retry ",
        base_branch="main",
    )

    assert lookup is not None
    assert lookup.state == "failed"
    assert lookup.branch_name == "feature/retry"
    assert lookup.ambiguity_reason == "open_pr_lookup_failed"
    assert lookup.payload["failure"] == "resolver_exception"
    assert lookup.payload["error_type"] == "RuntimeError"


@pytest.mark.unit
async def test_preserved_active_branch_lookup_treats_invalid_repo_url_as_failure(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    worker = _worker(factory)
    fake = FakeCommandRunner()
    worker._open_pr_resolver = BranchOpenPullRequestResolver(fake)  # type: ignore[assignment]

    lookup = await worker._resolve_preserved_active_branch_open_pr(  # noqa: SLF001
        repo_url="https://x-access-token:secret-token@github.com/example",
        branch_name="feature/retry",
        base_branch="main",
    )

    assert lookup is not None
    assert lookup.state == "failed"
    assert lookup.branch_name == "feature/retry"
    assert lookup.ambiguity_reason == "open_pr_lookup_failed"
    assert lookup.payload["failure"] == "resolver_exception"
    assert lookup.payload["error_type"] == "PullRequestMetadataError"
    assert fake.calls == []


@pytest.mark.unit
async def test_preserved_active_branch_lookup_covers_invalid_empty_and_multiple_results(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    worker = _worker(factory)

    class _StaticResolver:
        def __init__(self, matches: list[object]) -> None:
            self.matches = matches

        async def resolve(self, **_kwargs: object) -> list[object]:
            return self.matches

    worker._open_pr_resolver = _StaticResolver([{"number": "bad"}])  # type: ignore[assignment]
    invalid = await worker._resolve_preserved_active_branch_open_pr(  # noqa: SLF001
        repo_url="https://github.com/example/repo.git",
        branch_name="feature/invalid",
        base_branch="main",
    )
    assert invalid is not None
    assert invalid.state == "failed"
    assert invalid.ambiguity_reason == "open_pr_lookup_invalid"
    assert invalid.payload["source"] == "open_pr_resolver"

    worker._open_pr_resolver = _StaticResolver([])  # type: ignore[assignment]
    empty = await worker._resolve_preserved_active_branch_open_pr(  # noqa: SLF001
        repo_url="https://github.com/example/repo.git",
        branch_name="feature/none",
        base_branch="main",
    )
    assert empty is not None
    assert empty.state == "none"
    assert empty.payload["match_count"] == 0

    worker._open_pr_resolver = _StaticResolver(  # type: ignore[assignment]
        [
            {
                "url": "https://github.com/example/repo/pull/1",
                "number": 1,
                "headRefName": "feature/many",
            },
            {
                "url": "https://github.com/example/repo/pull/2",
                "number": 2,
                "headRefName": "feature/many",
            },
        ]
    )
    multiple = await worker._resolve_preserved_active_branch_open_pr(  # noqa: SLF001
        repo_url="not a github url",
        branch_name="feature/many",
        base_branch="main",
    )
    assert multiple is not None
    assert multiple.state == "ambiguous"
    assert multiple.ambiguity_reason == "multiple_open_prs_for_branch"
    assert multiple.payload["match_count"] == 2


@pytest.mark.unit
async def test_preserved_active_worktree_classification_covers_mismatch_and_count_edges(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = ControlWorker(
        session_factory=factory,
        provisioner=_PublicWorktreePathProvisioner(tmp_path),  # type: ignore[arg-type]
        config=WorkerConfig(),
    )
    (tmp_path / "ws_mismatch").mkdir()
    (tmp_path / "ws_invalid_count").mkdir()

    async def _branch_mismatch_git(_path: Path, *args: str) -> tuple[bool, str, str]:
        assert args == ("branch", "--show-current")
        return (True, "actual-branch\n", "")

    monkeypatch.setattr(worker, "_run_preserved_active_git", _branch_mismatch_git)

    mismatch = await worker._classify_preserved_active_worktree(  # noqa: SLF001
        workspace_id="ws_mismatch",
        expected_branch_name="expected-branch",
        base_commit="b" * 40,
    )

    assert mismatch.state == "ambiguous"
    assert mismatch.reason == "branch_mismatch"
    assert mismatch.branch_name == "actual-branch"

    responses = iter(
        [
            (True, "expected-branch\n", ""),
            (True, "h" * 40 + "\n", ""),
            (True, "", ""),
            (True, "not-a-number\n", ""),
        ]
    )

    async def _invalid_count_git(_path: Path, *args: str) -> tuple[bool, str, str]:
        del args
        return next(responses)

    monkeypatch.setattr(worker, "_run_preserved_active_git", _invalid_count_git)

    invalid_count = await worker._classify_preserved_active_worktree(  # noqa: SLF001
        workspace_id="ws_invalid_count",
        expected_branch_name="expected-branch",
        base_commit="b" * 40,
    )

    assert invalid_count.state == "failed"
    assert invalid_count.reason == "ahead_count_invalid"
    assert invalid_count.error == "not-a-number\n"

    missing_base = await worker._classify_preserved_active_worktree(  # noqa: SLF001
        workspace_id="ws_mismatch",
        expected_branch_name="expected-branch",
        base_commit=" ",
    )
    assert missing_base.state == "ambiguous"
    assert missing_base.reason == "missing_base_commit"

    async def _branch_unavailable_git(_path: Path, *args: str) -> tuple[bool, str, str]:
        assert args == ("branch", "--show-current")
        return (False, "", "fatal: branch unavailable")

    monkeypatch.setattr(worker, "_run_preserved_active_git", _branch_unavailable_git)
    branch_unavailable = await worker._classify_preserved_active_worktree(  # noqa: SLF001
        workspace_id="ws_mismatch",
        expected_branch_name="expected-branch",
        base_commit="b" * 40,
    )
    assert branch_unavailable.state == "failed"
    assert branch_unavailable.reason == "branch_unavailable"

    async def _detached_head_git(_path: Path, *args: str) -> tuple[bool, str, str]:
        assert args == ("branch", "--show-current")
        return (True, "\n", "")

    monkeypatch.setattr(worker, "_run_preserved_active_git", _detached_head_git)
    detached = await worker._classify_preserved_active_worktree(  # noqa: SLF001
        workspace_id="ws_mismatch",
        expected_branch_name="expected-branch",
        base_commit="b" * 40,
    )
    assert detached.state == "ambiguous"
    assert detached.reason == "detached_head"

    count_failure_responses = iter(
        [
            (True, "expected-branch\n", ""),
            (True, "h" * 40 + "\n", ""),
            (True, "", ""),
            (False, "", "rev-list failed"),
        ]
    )

    async def _count_failure_git(_path: Path, *args: str) -> tuple[bool, str, str]:
        del args
        return next(count_failure_responses)

    monkeypatch.setattr(worker, "_run_preserved_active_git", _count_failure_git)
    count_failure = await worker._classify_preserved_active_worktree(  # noqa: SLF001
        workspace_id="ws_invalid_count",
        expected_branch_name="expected-branch",
        base_commit="b" * 40,
    )
    assert count_failure.state == "failed"
    assert count_failure.reason == "ahead_count_unavailable"


@pytest.mark.unit
async def test_provider_recovery_candidate_blocker_edges(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    assert not await worker_recovery_stale._monitor_provider_recovery_resume_pending(  # noqa: SLF001
        factory,
        _ActiveExecutionCandidate(
            workspace_id="ws_running",
            status=WorkspaceStatus.running,
            compose_project_name="awf_ws_running",
            task_policy={},
        ),
    )
    assert not await worker_recovery_stale._monitor_provider_recovery_resume_pending(  # noqa: SLF001
        factory,
        _ActiveExecutionCandidate(
            workspace_id="ws_unknown_action",
            status=WorkspaceStatus.monitoring_pr,
            compose_project_name="awf_ws_unknown_action",
            task_policy={worker_recovery_stale.PROVIDER_RECOVERY_STATE_KEY: {"action": "other"}},
        ),
    )
    assert not await worker_recovery_stale._monitor_provider_recovery_resume_pending(  # noqa: SLF001
        factory,
        _ActiveExecutionCandidate(
            workspace_id="ws_missing_agent",
            status=WorkspaceStatus.monitoring_pr,
            compose_project_name="awf_ws_missing_agent",
            task_policy={worker_recovery_stale.PROVIDER_RECOVERY_STATE_KEY: {"action": "retry"}},
            agent=None,
        ),
    )
    assert not await worker_recovery_stale._monitor_provider_recovery_resume_pending(  # noqa: SLF001
        factory,
        _ActiveExecutionCandidate(
            workspace_id="ws_unknown_provider",
            status=WorkspaceStatus.monitoring_pr,
            compose_project_name="awf_ws_unknown_provider",
            task_policy={worker_recovery_stale.PROVIDER_RECOVERY_STATE_KEY: {"action": "retry"}},
            agent="custom-agent",
        ),
    )


@pytest.mark.unit
async def test_salvage_monitor_cooldown_active_evicts_expired_entries(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    worker = _worker(factory)
    worker._active_salvage_monitor_resume_cooldowns["ws_expired"] = (  # noqa: SLF001
        worker_recovery_cooldown.monotonic() - 1
    )

    assert not worker._active_salvage_monitor_resume_cooldown_active("ws_expired")  # noqa: SLF001
    assert "ws_expired" not in worker._active_salvage_monitor_resume_cooldowns  # noqa: SLF001


@pytest.mark.unit
async def test_forget_execution_task_ignores_replaced_task(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    worker = _worker(factory)

    async def _pending() -> None:
        await asyncio.Event().wait()

    tracked = asyncio.create_task(_pending())
    stale = asyncio.create_task(_pending())
    worker._execution_tasks["ws_replaced"] = tracked  # noqa: SLF001
    worker._execution_task_kinds["ws_replaced"] = (  # noqa: SLF001
        worker_dispatch_methods._ExecutionTaskKind.READY
    )
    try:
        # A late done-callback from a previously cancelled task must not evict the
        # task that currently owns the slot (identity guard).
        worker._forget_execution_task("ws_replaced", stale)  # noqa: SLF001
        assert worker._execution_tasks["ws_replaced"] is tracked  # noqa: SLF001
        assert (
            worker._execution_task_kinds["ws_replaced"]  # noqa: SLF001
            is worker_dispatch_methods._ExecutionTaskKind.READY
        )

        # The matching task clears both slot-accounting maps together.
        worker._forget_execution_task("ws_replaced", tracked)  # noqa: SLF001
        assert "ws_replaced" not in worker._execution_tasks  # noqa: SLF001
        assert "ws_replaced" not in worker._execution_task_kinds  # noqa: SLF001
    finally:
        for task in (tracked, stale):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


@pytest.mark.unit
async def test_reconcile_drops_monitor_kind_when_task_already_gone(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    worker = _worker(factory)
    # Simulate a monitor resume that finished concurrently during the status load:
    # the kind entry lingers while its task is already gone and the workspace row
    # is absent (so it is no longer monitoring_pr). Reconcile should drop the stale
    # kind entry without logging a cancellation or raising.
    worker._execution_task_kinds["ws_vanished"] = (  # noqa: SLF001
        worker_dispatch_methods._ExecutionTaskKind.MONITOR_RESUME
    )

    with structlog.testing.capture_logs() as captured:
        await worker._reconcile_stale_monitor_execution_tasks()  # noqa: SLF001

    assert "ws_vanished" not in worker._execution_task_kinds  # noqa: SLF001
    assert not any(
        event.get("event") == "worker.stale_monitor_execution_task_cancelled" for event in captured
    )
