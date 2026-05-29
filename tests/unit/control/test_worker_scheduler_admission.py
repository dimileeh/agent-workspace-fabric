"""Focused regressions for worker admission vs execution-slot capacity."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.control.worker import ControlWorker, WorkerConfig
from awf.control.worker import claims as worker_claims
from awf.control.worker.admission import (
    _acquire_requested_admission_lock,
    _requested_admission_row_slots,
)
from awf.control.worker.claims import _requested_claim_admission_slots
from awf.control.worker.types import _ExecutionTaskKind
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import (
    ResourceReservationRepository,
    TaskAttemptRepository,
    TaskRepository,
    WorkspaceEventRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.runtime.inspection import RuntimeService, RuntimeSnapshot
from tests.postgres import postgres_test_engine

WORKER_TEST_TIMEOUT_SECONDS = 30.0


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


class _RecordingProvisioner:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def provision_claimed(self, workspace_id: str) -> None:
        self.calls.append(workspace_id)


class _UnusedExecutor:
    async def execute(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("saturated worker must not dispatch execution")

    async def resume_pr_monitor(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("saturated worker must not resume monitors")


class _RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute(self, workspace_id: str, *_args: object, **_kwargs: object) -> None:
        self.calls.append(workspace_id)

    async def resume_pr_monitor(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("ready redispatch must not resume monitors")


class _RecordingRuntimeInspector:
    def __init__(self, snapshots: dict[str | None, RuntimeSnapshot]) -> None:
        self._snapshots = snapshots
        self.calls: list[str | None] = []

    async def inspect(self, compose_project_name: str | None) -> RuntimeSnapshot:
        self.calls.append(compose_project_name)
        return self._snapshots[compose_project_name]


class _NonPostgresSession:
    def __init__(self) -> None:
        self.executed = False

    def get_bind(self) -> SimpleNamespace:
        return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

    async def execute(self, *_args: object, **_kwargs: object) -> None:
        self.executed = True


async def _never_finishes() -> None:
    await asyncio.Event().wait()


async def _raises_execution_failure() -> None:
    raise RuntimeError("execution failed")


async def _create_requested(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    create_attempt: bool,
    node_id: str | None = None,
) -> str:
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.create(
            repo_url="git@example.com:org/repo.git",
            branch_base="development",
            task_title="queued when saturated",
            task_prompt="do the narrow thing",
            agent="codex",
            test_commands=[],
        )
        workspace.node_id = node_id
        if create_attempt:
            task = await TaskRepository(session).create_or_get(
                repo_url=workspace.repo_url,
                base_branch=workspace.branch_base,
                title=workspace.task_title,
                prompt=workspace.task_prompt,
                external_id=None,
                idempotency_key=None,
                task_class=workspace.task_class,
                owned_paths=list(workspace.owned_paths),
            )
            attempt = await TaskAttemptRepository(session).create_for_workspace(
                task=task,
                workspace=workspace,
            )
            await ResourceReservationRepository(session).create(
                workspace_id=workspace.id,
                attempt_id=attempt.id,
                node_id="local",
                steady_cpu=1.0,
                steady_memory_gb=1.0,
                peak_cpu=1.0,
                peak_memory_gb=1.0,
                disk_mb=None,
                dind_slots=0,
                phase="workspace_lifecycle",
            )
        await session.commit()
        return workspace.id


async def _create_active_slot(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    node_id: str | None,
    status: WorkspaceStatus = WorkspaceStatus.running,
) -> str:
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.create(
            repo_url="git@example.com:org/repo.git",
            branch_base="development",
            task_title="active slot consumer",
            task_prompt="already active",
            agent="codex",
            test_commands=[],
        )
        workspace.node_id = node_id
        workspace.compose_project_name = f"awf_{workspace.id}"
        workspace.compose_file_path = f"/tmp/{workspace.id}/compose.yml"
        await repo.transition(
            workspace,
            to=WorkspaceStatus.provisioning,
            reason_code="TEST_PROVISIONING",
        )
        if status != WorkspaceStatus.provisioning:
            await repo.transition(
                workspace,
                to=WorkspaceStatus.ready,
                reason_code="TEST_READY",
            )
        if status not in {WorkspaceStatus.provisioning, WorkspaceStatus.ready}:
            await repo.transition(workspace, to=status, reason_code="TEST_ACTIVE")
        await session.commit()
        return workspace.id


async def _create_ready_with_runtime_metadata(
    session_factory: async_sessionmaker[AsyncSession],
) -> str:
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.create(
            repo_url="git@example.com:org/repo.git",
            branch_base="development",
            task_title="ready and waiting",
            task_prompt="do the narrow thing",
            agent="codex",
            test_commands=[],
        )
        workspace.compose_project_name = f"awf_{workspace.id}"
        workspace.compose_file_path = f"/tmp/{workspace.id}/compose.yml"
        await repo.transition(
            workspace,
            to=WorkspaceStatus.provisioning,
            reason_code="TEST_PROVISIONING",
        )
        await repo.transition(
            workspace,
            to=WorkspaceStatus.ready,
            reason_code="TEST_READY",
        )
        await session.commit()
        return workspace.id


async def _workspace_status(
    session_factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
) -> str:
    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        return workspace.status


async def _workspace_node_id(
    session_factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
) -> str | None:
    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        return workspace.node_id


async def _stale_events(
    session_factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
) -> list[object]:
    async with session_factory() as session:
        return await WorkspaceEventRepository(session).list(
            workspace_id=workspace_id,
            event_type="workspace.stale_active_execution_detected",
        )


def _gate_admission_prechecks(
    monkeypatch: pytest.MonkeyPatch,
    workers: list[ControlWorker],
) -> tuple[asyncio.Event, asyncio.Event]:
    observed = 0
    all_observed = asyncio.Event()
    release = asyncio.Event()

    for worker in workers:
        original: Callable[[], Awaitable[int]] = worker._requested_admission_row_slots  # noqa: SLF001

        async def _gated(original: Callable[[], Awaitable[int]] = original) -> int:
            nonlocal observed
            slots = await original()
            observed += 1
            if observed == len(workers):
                all_observed.set()
            await release.wait()
            return slots

        monkeypatch.setattr(worker, "_requested_admission_row_slots", _gated)

    return all_observed, release


@pytest.mark.unit
async def test_requested_admission_lock_is_noop_for_non_postgres() -> None:
    session = _NonPostgresSession()

    await _acquire_requested_admission_lock(session, node_id="local")  # type: ignore[arg-type]

    assert not session.executed


@pytest.mark.unit
async def test_requested_admission_row_slots_zero_when_execution_limit_disabled(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        slots = await _requested_admission_row_slots(
            session,
            config=WorkerConfig(max_concurrent_executions=0),
        )

    assert slots == 0


@pytest.mark.unit
async def test_requested_workspace_stays_queued_when_execution_slots_are_saturated(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await _create_requested(session_factory, create_attempt=False)
    provisioner = _RecordingProvisioner()
    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=provisioner,  # type: ignore[arg-type]
        executor=_UnusedExecutor(),  # type: ignore[arg-type]
        config=WorkerConfig(max_concurrent_provisions=5, max_concurrent_executions=1),
    )
    worker._next_stale_active_execution_scan_at = float("inf")  # noqa: SLF001
    existing_task = asyncio.create_task(_never_finishes())
    worker._track_execution_task(  # noqa: SLF001
        "ws_existing",
        existing_task,
        kind=_ExecutionTaskKind.READY,
    )

    try:
        assert await worker.run_once() == 0
        assert provisioner.calls == []
        assert (
            await _workspace_status(session_factory, workspace_id)
            == WorkspaceStatus.requested.value
        )
    finally:
        existing_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await existing_task


@pytest.mark.unit
async def test_provision_only_worker_claims_requested_without_execution_row_slots(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await _create_requested(session_factory, create_attempt=False)
    provisioner = _RecordingProvisioner()
    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=provisioner,  # type: ignore[arg-type]
        executor=None,
        config=WorkerConfig(max_concurrent_provisions=1, max_concurrent_executions=0),
    )

    assert await worker.run_once() == 1

    assert provisioner.calls == [workspace_id]
    assert await _workspace_status(session_factory, workspace_id) == (
        WorkspaceStatus.provisioning.value
    )


@pytest.mark.unit
async def test_named_worker_stamps_node_id_when_claiming_requested_for_provisioning(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await _create_requested(
        session_factory,
        create_attempt=False,
        node_id=None,
    )
    provisioner = _RecordingProvisioner()
    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=provisioner,  # type: ignore[arg-type]
        executor=_UnusedExecutor(),  # type: ignore[arg-type]
        config=WorkerConfig(
            max_concurrent_provisions=1,
            max_concurrent_executions=1,
            node_id="local",
        ),
    )
    worker._next_stale_active_execution_scan_at = float("inf")  # noqa: SLF001

    assert await worker.run_once() == 1

    assert provisioner.calls == [workspace_id]
    assert await _workspace_status(session_factory, workspace_id) == (
        WorkspaceStatus.provisioning.value
    )
    assert await _workspace_node_id(session_factory, workspace_id) == "local"


@pytest.mark.unit
async def test_provision_only_local_capacity_worker_claims_requested_without_execution_row_slots(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await _create_requested(session_factory, create_attempt=True)
    provisioner = _RecordingProvisioner()
    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=provisioner,  # type: ignore[arg-type]
        executor=None,
        config=WorkerConfig(
            max_concurrent_provisions=1,
            max_concurrent_executions=0,
            local_capacity_cpu_cores=100.0,
        ),
    )

    assert await worker.run_once() == 1

    assert provisioner.calls == [workspace_id]
    assert await _workspace_status(session_factory, workspace_id) == (
        WorkspaceStatus.provisioning.value
    )


@pytest.mark.unit
async def test_named_local_capacity_worker_stamps_node_id_when_claiming_requested(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await _create_requested(
        session_factory,
        create_attempt=True,
        node_id=None,
    )
    provisioner = _RecordingProvisioner()
    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=provisioner,  # type: ignore[arg-type]
        executor=_UnusedExecutor(),  # type: ignore[arg-type]
        config=WorkerConfig(
            max_concurrent_provisions=1,
            max_concurrent_executions=1,
            node_id="local",
            local_capacity_cpu_cores=100.0,
        ),
    )
    worker._next_stale_active_execution_scan_at = float("inf")  # noqa: SLF001

    assert await worker.run_once() == 1

    assert provisioner.calls == [workspace_id]
    assert await _workspace_status(session_factory, workspace_id) == (
        WorkspaceStatus.provisioning.value
    )
    assert await _workspace_node_id(session_factory, workspace_id) == "local"


@pytest.mark.unit
async def test_named_worker_admission_waits_for_null_node_lock_before_claiming(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await _create_requested(
        session_factory,
        create_attempt=False,
        node_id="worker-a",
    )
    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=_RecordingProvisioner(),  # type: ignore[arg-type]
        executor=_UnusedExecutor(),  # type: ignore[arg-type]
        config=WorkerConfig(
            max_concurrent_provisions=1,
            max_concurrent_executions=1,
            node_id="worker-a",
        ),
    )

    async with session_factory() as lock_session:
        await _acquire_requested_admission_lock(lock_session, node_id="local")
        claim_task = asyncio.create_task(
            worker._claim_requested_for_provisioning(workspace_id)  # noqa: SLF001
        )
        done, _ = await asyncio.wait({claim_task}, timeout=0.2)

        assert done == set()
        assert (
            await _workspace_status(session_factory, workspace_id)
            == WorkspaceStatus.requested.value
        )
        await lock_session.rollback()

    assert await asyncio.wait_for(
        claim_task,
        timeout=WORKER_TEST_TIMEOUT_SECONDS,
    )
    assert await _workspace_status(session_factory, workspace_id) == (
        WorkspaceStatus.provisioning.value
    )
    assert await _workspace_node_id(session_factory, workspace_id) == "worker-a"


@pytest.mark.unit
async def test_concurrent_requested_claims_recheck_admission_slots_atomically(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_id = await _create_requested(
        session_factory,
        create_attempt=False,
        node_id="local",
    )
    second_id = await _create_requested(
        session_factory,
        create_attempt=False,
        node_id="local",
    )
    provisioner = _RecordingProvisioner()
    worker_a = ControlWorker(
        session_factory=session_factory,
        provisioner=provisioner,  # type: ignore[arg-type]
        executor=_UnusedExecutor(),  # type: ignore[arg-type]
        config=WorkerConfig(
            max_concurrent_provisions=1,
            max_concurrent_executions=1,
            node_id="local",
        ),
    )
    worker_b = ControlWorker(
        session_factory=session_factory,
        provisioner=provisioner,  # type: ignore[arg-type]
        executor=_UnusedExecutor(),  # type: ignore[arg-type]
        config=WorkerConfig(
            max_concurrent_provisions=1,
            max_concurrent_executions=1,
            node_id="local",
        ),
    )
    worker_a._next_stale_active_execution_scan_at = float("inf")  # noqa: SLF001
    worker_b._next_stale_active_execution_scan_at = float("inf")  # noqa: SLF001
    all_observed, release = _gate_admission_prechecks(
        monkeypatch,
        [worker_a, worker_b],
    )

    async def _list_first() -> list[str]:
        return [first_id]

    async def _list_second() -> list[str]:
        return [second_id]

    monkeypatch.setattr(worker_a, "_list_requested", _list_first)
    monkeypatch.setattr(worker_b, "_list_requested", _list_second)

    runs = [
        asyncio.create_task(worker_a.run_once()),
        asyncio.create_task(worker_b.run_once()),
    ]
    await asyncio.wait_for(
        all_observed.wait(),
        timeout=WORKER_TEST_TIMEOUT_SECONDS,
    )
    release.set()
    dispatched = await asyncio.wait_for(
        asyncio.gather(*runs),
        timeout=WORKER_TEST_TIMEOUT_SECONDS,
    )

    statuses = [
        await _workspace_status(session_factory, first_id),
        await _workspace_status(session_factory, second_id),
    ]
    assert sorted(dispatched) == [0, 1]
    assert len(provisioner.calls) == 1
    assert statuses.count(WorkspaceStatus.provisioning.value) == 1
    assert statuses.count(WorkspaceStatus.requested.value) == 1


@pytest.mark.unit
async def test_concurrent_local_capacity_claims_recheck_admission_slots_atomically(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_id = await _create_requested(session_factory, create_attempt=True)
    second_id = await _create_requested(session_factory, create_attempt=True)
    provisioner = _RecordingProvisioner()
    worker_a = ControlWorker(
        session_factory=session_factory,
        provisioner=provisioner,  # type: ignore[arg-type]
        executor=_UnusedExecutor(),  # type: ignore[arg-type]
        config=WorkerConfig(
            max_concurrent_provisions=1,
            max_concurrent_executions=1,
            local_capacity_cpu_cores=100.0,
        ),
    )
    worker_b = ControlWorker(
        session_factory=session_factory,
        provisioner=provisioner,  # type: ignore[arg-type]
        executor=_UnusedExecutor(),  # type: ignore[arg-type]
        config=WorkerConfig(
            max_concurrent_provisions=1,
            max_concurrent_executions=1,
            local_capacity_cpu_cores=100.0,
        ),
    )
    worker_a._next_stale_active_execution_scan_at = float("inf")  # noqa: SLF001
    worker_b._next_stale_active_execution_scan_at = float("inf")  # noqa: SLF001
    all_observed, release = _gate_admission_prechecks(
        monkeypatch,
        [worker_a, worker_b],
    )

    runs = [
        asyncio.create_task(worker_a.run_once()),
        asyncio.create_task(worker_b.run_once()),
    ]
    await asyncio.wait_for(
        all_observed.wait(),
        timeout=WORKER_TEST_TIMEOUT_SECONDS,
    )
    release.set()
    dispatched = await asyncio.wait_for(
        asyncio.gather(*runs),
        timeout=WORKER_TEST_TIMEOUT_SECONDS,
    )

    statuses = [
        await _workspace_status(session_factory, first_id),
        await _workspace_status(session_factory, second_id),
    ]
    assert sorted(dispatched) == [0, 1]
    assert len(provisioner.calls) == 1
    assert statuses.count(WorkspaceStatus.provisioning.value) == 1
    assert statuses.count(WorkspaceStatus.requested.value) == 1


@pytest.mark.unit
async def test_requested_provisioning_claim_stops_when_admission_rows_are_full(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await _create_requested(session_factory, create_attempt=False)
    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=_RecordingProvisioner(),  # type: ignore[arg-type]
        executor=_UnusedExecutor(),  # type: ignore[arg-type]
        config=WorkerConfig(
            max_concurrent_provisions=1,
            max_concurrent_executions=1,
        ),
    )

    async def _no_row_slots(*_args: object, **_kwargs: object) -> int:
        return 0

    monkeypatch.setattr(worker_claims, "_requested_claim_admission_slots", _no_row_slots)

    assert not await worker._claim_requested_for_provisioning(workspace_id)  # noqa: SLF001
    assert await _workspace_status(session_factory, workspace_id) == WorkspaceStatus.requested.value


@pytest.mark.unit
async def test_local_capacity_claims_stop_when_admission_rows_are_full(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await _create_requested(session_factory, create_attempt=True)
    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=_RecordingProvisioner(),  # type: ignore[arg-type]
        executor=_UnusedExecutor(),  # type: ignore[arg-type]
        config=WorkerConfig(
            max_concurrent_provisions=1,
            max_concurrent_executions=1,
            local_capacity_cpu_cores=100.0,
        ),
    )

    async def _no_row_slots(*_args: object, **_kwargs: object) -> int:
        return 0

    async def _fail_capacity_claim(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("capacity claim must not run when admission rows are full")

    monkeypatch.setattr(worker_claims, "_requested_claim_admission_slots", _no_row_slots)
    monkeypatch.setattr(worker, "_claim_requested_ids_with_capacity", _fail_capacity_claim)

    assert await worker._claim_requested_ids([workspace_id], limit=1) == []  # noqa: SLF001
    assert await _workspace_status(session_factory, workspace_id) == WorkspaceStatus.requested.value


@pytest.mark.unit
async def test_requested_capacity_claim_zero_effective_limit_returns_empty_result(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=_RecordingProvisioner(),  # type: ignore[arg-type]
        executor=_UnusedExecutor(),  # type: ignore[arg-type]
        config=WorkerConfig(
            max_concurrent_provisions=1,
            max_concurrent_executions=1,
            local_capacity_cpu_cores=100.0,
        ),
    )

    async with session_factory() as session:
        result = await worker._claim_requested_ids_with_capacity(  # noqa: SLF001
            session,
            resume_after=None,
            resume_allocated_signature=None,
            resume_requested_queue_signature=None,
            resume_provider_suppression_expires_at=None,
            claim_limit=0,
        )

    assert result.workspace_ids == []
    assert result.resume_after is None
    assert result.allocated_signature is None
    assert result.requested_queue_signature is None
    assert result.provider_suppression_resume_expires_at is None


@pytest.mark.unit
async def test_requested_capacity_candidates_empty_batch_returns_no_claims(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=_RecordingProvisioner(),  # type: ignore[arg-type]
        executor=_UnusedExecutor(),  # type: ignore[arg-type]
        config=WorkerConfig(
            max_concurrent_provisions=1,
            max_concurrent_executions=1,
            local_capacity_cpu_cores=100.0,
        ),
    )

    async with session_factory() as session:
        claimed = await worker._claim_requested_capacity_candidates(  # noqa: SLF001
            session,
            repo=WorkspaceRepository(session),
            reservation_repo=ResourceReservationRepository(session),
            candidates=[],
            allocated=worker_claims._AllocatedReservationTotals(),  # noqa: SLF001
            claim_slots=1,
            decided_at=datetime.now(UTC),
        )

    assert claimed == []


@pytest.mark.unit
async def test_local_capacity_claims_also_wait_for_execution_slot_capacity(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await _create_requested(session_factory, create_attempt=True)
    provisioner = _RecordingProvisioner()
    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=provisioner,  # type: ignore[arg-type]
        executor=_UnusedExecutor(),  # type: ignore[arg-type]
        config=WorkerConfig(
            max_concurrent_provisions=5,
            max_concurrent_executions=1,
            local_capacity_cpu_cores=100.0,
        ),
    )
    worker._next_stale_active_execution_scan_at = float("inf")  # noqa: SLF001
    existing_task = asyncio.create_task(_never_finishes())
    worker._track_execution_task(  # noqa: SLF001
        "ws_existing",
        existing_task,
        kind=_ExecutionTaskKind.READY,
    )

    try:
        assert await worker.run_once() == 0
        assert provisioner.calls == []
        assert (
            await _workspace_status(session_factory, workspace_id)
            == WorkspaceStatus.requested.value
        )
    finally:
        existing_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await existing_task


@pytest.mark.unit
async def test_requested_workspace_stays_queued_when_node_active_rows_fill_slots(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _create_active_slot(session_factory, node_id="local")
    workspace_id = await _create_requested(
        session_factory,
        create_attempt=False,
        node_id="local",
    )
    provisioner = _RecordingProvisioner()
    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=provisioner,  # type: ignore[arg-type]
        executor=_UnusedExecutor(),  # type: ignore[arg-type]
        config=WorkerConfig(
            max_concurrent_provisions=5,
            max_concurrent_executions=1,
            node_id="local",
        ),
    )
    worker._next_stale_active_execution_scan_at = float("inf")  # noqa: SLF001

    assert await worker.run_once() == 0

    assert provisioner.calls == []
    assert await _workspace_status(session_factory, workspace_id) == WorkspaceStatus.requested.value


@pytest.mark.unit
async def test_default_worker_counts_local_node_active_rows_as_occupied(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _create_active_slot(session_factory, node_id="local")
    workspace_id = await _create_requested(
        session_factory,
        create_attempt=False,
        node_id="local",
    )
    provisioner = _RecordingProvisioner()
    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=provisioner,  # type: ignore[arg-type]
        executor=_UnusedExecutor(),  # type: ignore[arg-type]
        config=WorkerConfig(
            max_concurrent_provisions=5,
            max_concurrent_executions=1,
            node_id=None,
        ),
    )
    worker._next_stale_active_execution_scan_at = float("inf")  # noqa: SLF001

    assert await worker.run_once() == 0

    assert provisioner.calls == []
    assert await _workspace_status(session_factory, workspace_id) == WorkspaceStatus.requested.value


@pytest.mark.unit
async def test_named_node_worker_counts_null_node_provisioning_rows_as_occupied(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _create_active_slot(
        session_factory,
        node_id=None,
        status=WorkspaceStatus.provisioning,
    )
    workspace_id = await _create_requested(
        session_factory,
        create_attempt=False,
        node_id="local",
    )
    provisioner = _RecordingProvisioner()
    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=provisioner,  # type: ignore[arg-type]
        executor=_UnusedExecutor(),  # type: ignore[arg-type]
        config=WorkerConfig(
            max_concurrent_provisions=5,
            max_concurrent_executions=1,
            node_id="local",
        ),
    )
    worker._next_stale_active_execution_scan_at = float("inf")  # noqa: SLF001

    assert await worker.run_once() == 0

    assert provisioner.calls == []
    assert await _workspace_status(session_factory, workspace_id) == WorkspaceStatus.requested.value


@pytest.mark.unit
async def test_named_worker_recovers_null_node_provisioning_rows_that_block_admission(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await _create_active_slot(
        session_factory,
        node_id=None,
        status=WorkspaceStatus.provisioning,
    )
    compose_project_name = f"awf_{workspace_id}"
    inspector = _RecordingRuntimeInspector(
        {
            compose_project_name: RuntimeSnapshot(
                stack_state="running",
                services=[
                    RuntimeService(
                        name="agent",
                        container_id="legacy-null-agent",
                        image="awf-agent-runtime:latest",
                        state="running",
                    )
                ],
            )
        }
    )
    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=_RecordingProvisioner(),  # type: ignore[arg-type]
        executor=_UnusedExecutor(),  # type: ignore[arg-type]
        runtime_inspector=inspector,
        config=WorkerConfig(
            max_concurrent_provisions=5,
            max_concurrent_executions=1,
            node_id="worker-a",
        ),
    )

    await worker._recover_stale_active_executions()  # noqa: SLF001

    stale_events = await _stale_events(session_factory, workspace_id)
    assert inspector.calls == [compose_project_name]
    assert len(stale_events) == 1
    assert await _workspace_status(session_factory, workspace_id) == (
        WorkspaceStatus.provisioning.value
    )


@pytest.mark.unit
async def test_null_node_worker_admission_ignores_active_rows_on_named_nodes(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _create_active_slot(session_factory, node_id="remote")
    workspace_id = await _create_requested(
        session_factory,
        create_attempt=False,
        node_id=None,
    )
    provisioner = _RecordingProvisioner()
    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=provisioner,  # type: ignore[arg-type]
        executor=_UnusedExecutor(),  # type: ignore[arg-type]
        config=WorkerConfig(
            max_concurrent_provisions=5,
            max_concurrent_executions=1,
            node_id=None,
        ),
    )
    worker._next_stale_active_execution_scan_at = float("inf")  # noqa: SLF001

    assert await worker.run_once() == 1

    assert provisioner.calls == [workspace_id]
    assert await _workspace_status(session_factory, workspace_id) == (
        WorkspaceStatus.provisioning.value
    )


@pytest.mark.unit
async def test_healthy_ready_workspace_waiting_for_slot_is_not_stale_execution(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await _create_ready_with_runtime_metadata(session_factory)
    compose_project_name = f"awf_{workspace_id}"
    inspector = _RecordingRuntimeInspector(
        {
            compose_project_name: RuntimeSnapshot(
                stack_state="running",
                services=[
                    RuntimeService(
                        name="agent",
                        container_id="agent-1",
                        image="awf-agent-runtime:latest",
                        state="running",
                    )
                ],
            )
        }
    )
    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=_RecordingProvisioner(),  # type: ignore[arg-type]
        executor=_UnusedExecutor(),  # type: ignore[arg-type]
        runtime_inspector=inspector,
        config=WorkerConfig(max_concurrent_executions=0),
    )

    await worker._recover_stale_active_executions()  # noqa: SLF001

    assert inspector.calls == [compose_project_name]
    assert await _workspace_status(session_factory, workspace_id) == WorkspaceStatus.ready.value
    assert await _stale_events(session_factory, workspace_id) == []


@pytest.mark.unit
async def test_run_once_redispatches_healthy_ready_workspace_after_recovery_scan(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await _create_ready_with_runtime_metadata(session_factory)
    compose_project_name = f"awf_{workspace_id}"
    inspector = _RecordingRuntimeInspector(
        {
            compose_project_name: RuntimeSnapshot(
                stack_state="running",
                services=[
                    RuntimeService(
                        name="agent",
                        container_id="agent-1",
                        image="awf-agent-runtime:latest",
                        state="running",
                    )
                ],
            )
        }
    )
    executor = _RecordingExecutor()
    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=_RecordingProvisioner(),  # type: ignore[arg-type]
        executor=executor,  # type: ignore[arg-type]
        runtime_inspector=inspector,
        config=WorkerConfig(
            max_concurrent_provisions=0,
            max_concurrent_executions=1,
        ),
    )

    assert await worker.run_once() == 1
    await asyncio.wait_for(
        worker.wait_for_execution_tasks(),
        timeout=WORKER_TEST_TIMEOUT_SECONDS,
    )

    assert inspector.calls == [compose_project_name]
    assert executor.calls == [workspace_id]
    assert await _workspace_status(session_factory, workspace_id) == WorkspaceStatus.ready.value
    assert await _stale_events(session_factory, workspace_id) == []


@pytest.mark.unit
async def test_wait_for_execution_tasks_drops_cancelled_task(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=_RecordingProvisioner(),  # type: ignore[arg-type]
        executor=_UnusedExecutor(),  # type: ignore[arg-type]
        config=WorkerConfig(max_concurrent_executions=1),
    )
    task = asyncio.create_task(_never_finishes())
    worker._execution_tasks["ws_cancelled"] = task  # noqa: SLF001
    worker._execution_task_kinds["ws_cancelled"] = _ExecutionTaskKind.READY  # noqa: SLF001
    task.cancel()

    await worker.wait_for_execution_tasks()

    assert worker._execution_tasks == {}  # noqa: SLF001
    assert worker._execution_task_kinds == {}  # noqa: SLF001


@pytest.mark.unit
async def test_wait_for_execution_tasks_raises_completed_task_exception(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=_RecordingProvisioner(),  # type: ignore[arg-type]
        executor=_UnusedExecutor(),  # type: ignore[arg-type]
        config=WorkerConfig(max_concurrent_executions=1),
    )
    worker._execution_tasks["ws_failed"] = asyncio.create_task(  # noqa: SLF001
        _raises_execution_failure()
    )
    worker._execution_task_kinds["ws_failed"] = _ExecutionTaskKind.READY  # noqa: SLF001

    with pytest.raises(RuntimeError, match="execution failed"):
        await worker.wait_for_execution_tasks()

    assert worker._execution_tasks == {}  # noqa: SLF001
    assert worker._execution_task_kinds == {}  # noqa: SLF001


@pytest.mark.unit
async def test_requested_claim_admission_slots_honor_claim_limit_for_executor_worker(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=_RecordingProvisioner(),  # type: ignore[arg-type]
        executor=_UnusedExecutor(),  # type: ignore[arg-type]
        config=WorkerConfig(
            max_concurrent_provisions=5,
            max_concurrent_executions=5,
        ),
    )

    async with session_factory() as session:
        assert (
            await _requested_claim_admission_slots(
                worker,
                session,
                claim_limit=1,
            )
            == 1
        )
