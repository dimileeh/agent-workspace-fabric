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

    async def provision_claimed(
        self, workspace_id: str, execution_claim_epoch: int | None = None
    ) -> None:
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


async def _workspace_execution_claim(
    session_factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
) -> tuple[str | None, datetime | None]:
    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        return workspace.execution_claimed_by, workspace.execution_claim_expires_at


async def _workspace_execution_epoch(
    session_factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
) -> int:
    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        return workspace.execution_claim_epoch


async def _reset_to_requested(
    session_factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
) -> None:
    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.status = WorkspaceStatus.requested.value
        await session.commit()


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
async def test_live_named_provisioning_claim_is_hidden_from_sibling_stale_scan(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await _create_requested(
        session_factory,
        create_attempt=False,
        node_id=None,
    )
    claim_worker = ControlWorker(
        session_factory=session_factory,
        provisioner=_RecordingProvisioner(),  # type: ignore[arg-type]
        executor=_UnusedExecutor(),  # type: ignore[arg-type]
        config=WorkerConfig(
            max_concurrent_provisions=1,
            max_concurrent_executions=1,
            node_id="local",
        ),
    )

    assert await claim_worker._claim_requested_for_provisioning(workspace_id)  # noqa: SLF001

    claimed_by, claim_expires_at = await _workspace_execution_claim(
        session_factory,
        workspace_id,
    )
    assert claimed_by == claim_worker._worker_id  # noqa: SLF001
    assert claim_expires_at is not None
    assert claim_expires_at > datetime.now(UTC)

    sibling_worker = ControlWorker(
        session_factory=session_factory,
        provisioner=_RecordingProvisioner(),  # type: ignore[arg-type]
        executor=_UnusedExecutor(),  # type: ignore[arg-type]
        config=WorkerConfig(
            max_concurrent_provisions=1,
            max_concurrent_executions=1,
            node_id="local",
        ),
    )

    candidates = await sibling_worker._list_stale_active_execution_candidates(  # noqa: SLF001
        exclude_ids=set()
    )

    assert [candidate.workspace_id for candidate in candidates] == []


class _EpochCapturingProvisioner:
    """Records the epoch passed and the worker's in-memory epoch at call time."""

    def __init__(self, worker_box: dict[str, ControlWorker]) -> None:
        self.calls: list[tuple[str, int | None]] = []
        self.epoch_in_map_at_call: int | None = None
        self._worker_box = worker_box

    async def provision_claimed(
        self, workspace_id: str, execution_claim_epoch: int | None = None
    ) -> None:
        worker = self._worker_box["worker"]
        self.epoch_in_map_at_call = worker._execution_claim_epochs.get(workspace_id)  # noqa: SLF001
        self.calls.append((workspace_id, execution_claim_epoch))


class _RaisingProvisioner:
    async def provision_claimed(
        self, workspace_id: str, execution_claim_epoch: int | None = None
    ) -> None:
        raise RuntimeError("provision failed")


@pytest.mark.unit
async def test_safely_provision_claimed_swallows_provision_exception(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # A claimed workspace whose provision raises: the failure is swallowed (one
    # bad workspace must not abort the batch) and the epoch is cleared.
    workspace_id = await _create_requested(
        session_factory,
        create_attempt=False,
        node_id=None,
    )
    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=_RaisingProvisioner(),  # type: ignore[arg-type]
        executor=_UnusedExecutor(),  # type: ignore[arg-type]
        config=WorkerConfig(max_concurrent_provisions=1, max_concurrent_executions=1),
    )
    assert await worker._claim_requested_for_provisioning(workspace_id)  # noqa: SLF001

    await worker._safely_provision_claimed(workspace_id)  # noqa: SLF001

    assert workspace_id not in worker._execution_claim_epochs  # noqa: SLF001


@pytest.mark.unit
async def test_safely_provision_claimed_aborts_when_epoch_is_none(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # A workspace not claimed by this worker -> read returns None -> abort, no
    # provision, no stored epoch.
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
        config=WorkerConfig(max_concurrent_provisions=1, max_concurrent_executions=1),
    )

    await worker._safely_provision_claimed(workspace_id)  # noqa: SLF001

    assert provisioner.calls == []
    assert workspace_id not in worker._execution_claim_epochs  # noqa: SLF001


@pytest.mark.unit
async def test_safely_provision_claimed_stores_passes_and_clears_epoch(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await _create_requested(
        session_factory,
        create_attempt=False,
        node_id=None,
    )
    worker_box: dict[str, ControlWorker] = {}
    provisioner = _EpochCapturingProvisioner(worker_box)
    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=provisioner,  # type: ignore[arg-type]
        executor=_UnusedExecutor(),  # type: ignore[arg-type]
        config=WorkerConfig(max_concurrent_provisions=1, max_concurrent_executions=1),
    )
    worker_box["worker"] = worker

    assert await worker._claim_requested_for_provisioning(workspace_id)  # noqa: SLF001
    expected_epoch = await _workspace_execution_epoch(session_factory, workspace_id)
    assert expected_epoch == 1

    await worker._safely_provision_claimed(workspace_id)  # noqa: SLF001

    # The epoch read back at provision start is stored and threaded to the
    # provisioner, then cleared in the finally.
    assert provisioner.calls == [(workspace_id, 1)]
    assert provisioner.epoch_in_map_at_call == 1
    assert workspace_id not in worker._execution_claim_epochs  # noqa: SLF001

    # The claim was released on the stored epoch.
    claimed_by, _ = await _workspace_execution_claim(session_factory, workspace_id)
    assert claimed_by is None


class _FenceThenBlockProvisioner:
    """Advances the epoch (a later claimant) then blocks until cancelled."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self.cancelled = False
        self.started = asyncio.Event()

    async def provision_claimed(
        self, workspace_id: str, execution_claim_epoch: int | None = None
    ) -> None:
        async with self._session_factory() as session:
            ws = await WorkspaceRepository(session).get(workspace_id)
            assert ws is not None
            ws.execution_claim_epoch = ws.execution_claim_epoch + 1
            ws.execution_claimed_by = "control-worker-newer"
            await session.commit()
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


@pytest.mark.unit
async def test_safely_provision_claimed_heartbeat_fence_cancels_provision(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await _create_requested(
        session_factory,
        create_attempt=False,
        node_id=None,
    )
    provisioner = _FenceThenBlockProvisioner(session_factory)
    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=provisioner,  # type: ignore[arg-type]
        executor=_UnusedExecutor(),  # type: ignore[arg-type]
        config=WorkerConfig(
            max_concurrent_provisions=1,
            max_concurrent_executions=1,
            execution_claim_lease_seconds=3.0,
        ),
    )

    assert await worker._claim_requested_for_provisioning(workspace_id)  # noqa: SLF001

    # The provision starts at epoch 1, advances the row to epoch 2 (a later
    # claimant), then blocks; the heartbeat CAS then fences us and cancels it.
    await asyncio.wait_for(
        worker._safely_provision_claimed(workspace_id),  # noqa: SLF001
        timeout=WORKER_TEST_TIMEOUT_SECONDS,
    )

    assert provisioner.cancelled is True
    assert workspace_id not in worker._execution_claim_epochs  # noqa: SLF001
    # The fenced worker's release did not clobber the newer claimant.
    claimed_by, _ = await _workspace_execution_claim(session_factory, workspace_id)
    assert claimed_by == "control-worker-newer"


@pytest.mark.unit
async def test_refresh_execution_claim_loop_cancels_provision_on_fence(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await _create_requested(
        session_factory,
        create_attempt=False,
        node_id=None,
    )
    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=_RecordingProvisioner(),  # type: ignore[arg-type]
        executor=_UnusedExecutor(),  # type: ignore[arg-type]
        config=WorkerConfig(
            max_concurrent_provisions=1,
            max_concurrent_executions=1,
            execution_claim_lease_seconds=3.0,
        ),
    )

    async def _fenced_refresh(_workspace_id: str) -> bool:
        return False

    monkeypatch.setattr(worker, "_refresh_execution_claim", _fenced_refresh)
    cancelled = asyncio.Event()

    await worker._refresh_execution_claim_loop(  # noqa: SLF001
        workspace_id,
        on_claim_lost=cancelled.set,
    )

    assert cancelled.is_set()


@pytest.mark.unit
async def test_provisioning_claim_increments_execution_claim_epoch(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await _create_requested(
        session_factory,
        create_attempt=False,
        node_id=None,
    )
    assert await _workspace_execution_epoch(session_factory, workspace_id) == 0

    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=_RecordingProvisioner(),  # type: ignore[arg-type]
        executor=_UnusedExecutor(),  # type: ignore[arg-type]
        config=WorkerConfig(
            max_concurrent_provisions=1,
            max_concurrent_executions=1,
            node_id="local",
        ),
    )

    assert await worker._claim_requested_for_provisioning(workspace_id)  # noqa: SLF001
    claimed_by, _ = await _workspace_execution_claim(session_factory, workspace_id)
    assert claimed_by == worker._worker_id  # noqa: SLF001
    # D1/D8: the backfilled 0 row advances to 1 on claim.
    assert await _workspace_execution_epoch(session_factory, workspace_id) == 1

    # Same-worker re-dispatch still increments, fencing a previously captured epoch.
    await _reset_to_requested(session_factory, workspace_id)
    assert await worker._claim_requested_for_provisioning(workspace_id)  # noqa: SLF001
    assert await _workspace_execution_epoch(session_factory, workspace_id) == 2


@pytest.mark.unit
async def test_capacity_claim_increments_execution_claim_epoch(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await _create_requested(
        session_factory,
        create_attempt=True,
        node_id=None,
    )
    assert await _workspace_execution_epoch(session_factory, workspace_id) == 0

    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=_RecordingProvisioner(),  # type: ignore[arg-type]
        executor=_UnusedExecutor(),  # type: ignore[arg-type]
        config=WorkerConfig(
            max_concurrent_provisions=1,
            max_concurrent_executions=1,
            node_id="local",
            local_capacity_cpu_cores=100.0,
        ),
    )

    claimed = await worker._claim_requested_ids([workspace_id], limit=1)  # noqa: SLF001
    assert claimed == [workspace_id]
    claimed_by, _ = await _workspace_execution_claim(session_factory, workspace_id)
    assert claimed_by == worker._worker_id  # noqa: SLF001
    assert await _workspace_execution_epoch(session_factory, workspace_id) == 1


@pytest.mark.unit
async def test_live_named_capacity_provisioning_claim_is_hidden_from_sibling_stale_scan(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await _create_requested(
        session_factory,
        create_attempt=True,
        node_id=None,
    )
    claim_worker = ControlWorker(
        session_factory=session_factory,
        provisioner=_RecordingProvisioner(),  # type: ignore[arg-type]
        executor=_UnusedExecutor(),  # type: ignore[arg-type]
        config=WorkerConfig(
            max_concurrent_provisions=1,
            max_concurrent_executions=1,
            node_id="local",
            local_capacity_cpu_cores=100.0,
        ),
    )

    claimed = await claim_worker._claim_requested_ids(  # noqa: SLF001
        [workspace_id],
        limit=1,
    )
    assert claimed == [workspace_id]

    claimed_by, claim_expires_at = await _workspace_execution_claim(
        session_factory,
        workspace_id,
    )
    assert claimed_by == claim_worker._worker_id  # noqa: SLF001
    assert claim_expires_at is not None
    assert claim_expires_at > datetime.now(UTC)

    sibling_worker = ControlWorker(
        session_factory=session_factory,
        provisioner=_RecordingProvisioner(),  # type: ignore[arg-type]
        executor=_UnusedExecutor(),  # type: ignore[arg-type]
        config=WorkerConfig(
            max_concurrent_provisions=1,
            max_concurrent_executions=1,
            node_id="local",
        ),
    )

    candidates = await sibling_worker._list_stale_active_execution_candidates(  # noqa: SLF001
        exclude_ids=set()
    )

    assert [candidate.workspace_id for candidate in candidates] == []


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
async def test_default_worker_recovers_local_node_provisioning_rows_that_block_admission(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await _create_active_slot(
        session_factory,
        node_id="local",
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
                        container_id="default-local-agent",
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
            node_id=None,
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
