"""Slot-release soak against a live local control-plane Postgres (manual; never CI).

Drives in-process ``ControlWorker`` instances with fake provisioning, fake agent
runs (short sleeps held under ``work_slot.hold("agent")``) and a fake process
probe, while N PR monitors wait (WaitForCI-style, holding no work slot) and
occasionally run an agent fix. It asserts:

* concurrent held/adopted work-slot leases never exceed ``--slots``;
* every queued (``requested``) workspace is admitted despite the waiting monitors;
* a worker restart mid-run adopts an in-flight invocation left by a crashed
  worker (holder and epoch change, phase ``adopted``) and the reconciler releases
  it once the fake process exits.

The soak creates a scratch database ``awf_slot_soak_<hex>`` on the given server,
migrates it to head, and drops it afterwards (``--keep-db`` to inspect), so the
real worker never sees soak rows. It prints a JSON summary and exits non-zero on
any violation.

Run it from the repository root against the local control plane (``awf service
bootstrap`` publishes Postgres; use the same URL as ``AWF_DATABASE_URL``):

    uv run --python 3.12 --extra dev python scripts/slot_release_soak.py \\
      --database-url "$AWF_DATABASE_URL" --slots 3 --monitors 7 --queued 3 \\
      --agent-seconds 0.5 --duration 60

This script is not collected by pytest (``testpaths = ["tests"]``) and is not
wired into any workflow.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.ids import new_work_slot_invocation_id
from awf.control.worker import ControlWorker, WorkerConfig
from awf.db.enums import WorkSlotLeasePhase, WorkspaceStatus
from awf.db.models import WorkSlotLease
from awf.db.repositories import (
    IN_USE_WORK_SLOT_LEASE_PHASES,
    WorkSlotLeaseRepository,
    WorkspaceRepository,
)
from awf.db.session import make_engine, make_session_factory

REPO_ROOT = Path(__file__).resolve().parents[1]
CRASHED_WORKER_ID = "control-worker-soak-crashed"
_SAMPLE_INTERVAL_SECONDS = 0.1


@dataclass(frozen=True)
class SoakConfig:
    database_url: str
    slots: int
    monitors: int
    queued: int
    agent_seconds: float
    duration: float
    keep_db: bool = False


@dataclass
class SoakReport:
    max_leases_in_use: int = 0
    samples: int = 0
    admitted: list[str] = field(default_factory=list)
    not_admitted: list[str] = field(default_factory=list)
    monitor_agent_runs: int = 0
    adoption: dict[str, Any] = field(default_factory=dict)
    violations: list[str] = field(default_factory=list)


def check_invariants(
    *,
    lease_samples: Sequence[int],
    slots: int,
    queued_statuses: dict[str, str],
    adoption: dict[str, Any],
) -> list[str]:
    """Return human-readable violations of the slot-release soak invariants."""
    violations: list[str] = []
    over = [sample for sample in lease_samples if sample > slots]
    if over:
        violations.append(f"leases in use exceeded {slots} slots: max {max(over)}")
    stuck = sorted(
        workspace_id
        for workspace_id, status in queued_statuses.items()
        if status == WorkspaceStatus.requested.value
    )
    if stuck:
        violations.append(f"queued workspaces never admitted: {stuck}")
    if not adoption.get("adopted"):
        violations.append("restarted worker did not adopt the in-flight invocation")
    elif not adoption.get("released_after_exit"):
        violations.append("reconciler did not release the adopted lease after its process exited")
    return violations


def scratch_database_url(database_url: str, name: str) -> str:
    return make_url(database_url).set(database=name).render_as_string(hide_password=False)


@contextlib.asynccontextmanager
async def scratch_database(config: SoakConfig) -> AsyncIterator[str]:
    """Create, migrate and (unless ``keep_db``) drop a scratch control-plane DB."""
    name = f"awf_slot_soak_{uuid.uuid4().hex[:12]}"
    admin_engine = make_engine(config.database_url).execution_options(isolation_level="AUTOCOMMIT")
    try:
        async with admin_engine.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{name}"'))
        url = scratch_database_url(config.database_url, name)
        await asyncio.to_thread(_migrate_to_head, url)
        yield url
    finally:
        if not config.keep_db:
            async with admin_engine.connect() as conn:
                await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin_engine.dispose()


def _migrate_to_head(database_url: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"],
        cwd=REPO_ROOT,
        env={**os.environ, "AWF_DATABASE_URL": database_url},
        check=True,
        capture_output=True,
    )


class FakeProcesses:
    """In-flight fake ``awf-exec`` processes, keyed by work-slot invocation."""

    def __init__(self) -> None:
        self.live: set[str] = set()

    @contextlib.contextmanager
    def running(self, invocation_id: str) -> Iterator[None]:
        self.live.add(invocation_id)
        try:
            yield
        finally:
            self.live.discard(invocation_id)

    async def probe(self, *, workspace_id: str, invocation_id: str, **_kwargs: object) -> str:
        del workspace_id
        return "present" if invocation_id in self.live else "absent"


class SoakProvisioner:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def provision_claimed(
        self, workspace_id: str, execution_claim_epoch: int | None = None
    ) -> None:
        del execution_claim_epoch
        await asyncio.sleep(0.05)
        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(workspace_id)
            if ws is None or ws.status != WorkspaceStatus.provisioning.value:
                return
            ws.compose_project_name = f"awf_{workspace_id}"
            ws.compose_file_path = f"/tmp/awf-soak/{workspace_id}/compose.yml"
            await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="SOAK_READY")
            await session.commit()

    def get_worktree_path(self, workspace_id: str) -> Path | None:
        del workspace_id
        return None


class SoakExecutor:
    """Fake executor: agent runs and monitor fixes hold the injected work slot."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        processes: FakeProcesses,
        *,
        agent_seconds: float,
        report: SoakReport,
    ) -> None:
        self._session_factory = session_factory
        self._processes = processes
        self._agent_seconds = agent_seconds
        self._report = report
        self._bound: dict[str, Any] = {}
        self.stop_monitors = asyncio.Event()

    @contextlib.contextmanager
    def work_slot_binding(self, workspace_id: str, work_slot: Any) -> Iterator[None]:
        self._bound[workspace_id] = work_slot
        try:
            yield
        finally:
            self._bound.pop(workspace_id, None)

    async def _agent_run(self, workspace_id: str) -> None:
        scope = self._bound[workspace_id]
        async with scope.hold("agent") as handle:
            with self._processes.running(handle.invocation_id):
                await asyncio.sleep(self._agent_seconds)

    async def execute(self, workspace_id: str, **_kwargs: object) -> None:
        await self._move(workspace_id, (WorkspaceStatus.running,))
        await self._agent_run(workspace_id)
        await self._move(workspace_id, (WorkspaceStatus.validating, WorkspaceStatus.completed))

    async def resume_pr_monitor(self, workspace_id: str) -> None:
        polls = 0
        while not self.stop_monitors.is_set():
            # WaitForCI: wait without a slot; every few polls address a comment.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.stop_monitors.wait(), timeout=0.2)
            polls += 1
            if polls % 5 == 0 and not self.stop_monitors.is_set():
                await self._agent_run(workspace_id)
                await self._bound[workspace_id].release_idle()
                self._report.monitor_agent_runs += 1

    async def _move(self, workspace_id: str, steps: Sequence[WorkspaceStatus]) -> None:
        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(workspace_id)
            if ws is None:
                return
            for step in steps:
                await repo.transition(ws, to=step, reason_code="SOAK")
            await session.commit()


async def seed(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    monitors: int,
    queued: int,
) -> tuple[list[str], list[str]]:
    monitor_ids: list[str] = []
    queued_ids: list[str] = []
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        for index in range(monitors + queued):
            ws = await repo.create(
                repo_url="git@example.com:soak/repo.git",
                branch_base="main",
                task_title=f"soak-{index}",
                task_prompt="soak",
                agent="codex",
                test_commands=[],
            )
            if index < monitors:
                ws.compose_project_name = f"awf_{ws.id}"
                ws.compose_file_path = f"/tmp/awf-soak/{ws.id}/compose.yml"
                ws.pr_url = f"https://github.com/soak/repo/pull/{index + 1}"
                ws.pr_number = index + 1
                for step in (
                    WorkspaceStatus.provisioning,
                    WorkspaceStatus.ready,
                    WorkspaceStatus.running,
                    WorkspaceStatus.validating,
                    WorkspaceStatus.pushing,
                    WorkspaceStatus.monitoring_pr,
                ):
                    await repo.transition(ws, to=step, reason_code="SOAK_SEED")
                monitor_ids.append(ws.id)
            else:
                queued_ids.append(ws.id)
        await session.commit()
    return monitor_ids, queued_ids


async def _leases_in_use(session_factory: async_sessionmaker[AsyncSession]) -> int:
    async with session_factory() as session:
        leases = await WorkSlotLeaseRepository(session).list_for_node(node_id=None)
    return sum(1 for lease in leases if lease.phase in IN_USE_WORK_SLOT_LEASE_PHASES)


async def _sample(
    session_factory: async_sessionmaker[AsyncSession],
    samples: list[int],
    stop: asyncio.Event,
) -> None:
    while not stop.is_set():
        samples.append(await _leases_in_use(session_factory))
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=_SAMPLE_INTERVAL_SECONDS)


def _worker(
    session_factory: async_sessionmaker[AsyncSession],
    executor: SoakExecutor,
    processes: FakeProcesses,
    config: SoakConfig,
) -> ControlWorker:
    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=SoakProvisioner(session_factory),  # type: ignore[arg-type]
        executor=executor,
        work_slot_probe=processes,  # type: ignore[arg-type]
        config=WorkerConfig(
            poll_interval_seconds=0.2,
            max_concurrent_provisions=max(1, config.queued),
            max_concurrent_executions=config.slots,
        ),
    )
    worker._next_stale_active_execution_scan_at = float("inf")  # noqa: SLF001
    return worker


async def _run_worker_until(worker: ControlWorker, deadline: float) -> None:
    loop = asyncio.get_running_loop()
    while loop.time() < deadline:
        if await worker.run_once() == 0:
            await asyncio.sleep(0.2)


async def _stop_worker(worker: ControlWorker, executor: SoakExecutor) -> None:
    executor.stop_monitors.set()
    await worker.wait_for_execution_tasks()
    executor.stop_monitors.clear()


async def _plant_crashed_invocation(
    session_factory: async_sessionmaker[AsyncSession],
    processes: FakeProcesses,
) -> str:
    """A crashed worker left an in-flight agent run holding a work slot."""
    now = datetime.now(UTC)
    invocation_id = new_work_slot_invocation_id()
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="git@example.com:soak/repo.git",
            branch_base="main",
            task_title="soak-crashed",
            task_prompt="soak",
            agent="codex",
            test_commands=[],
        )
        for step in (WorkspaceStatus.provisioning, WorkspaceStatus.ready, WorkspaceStatus.running):
            await repo.transition(ws, to=step, reason_code="SOAK_SEED")
        await WorkSlotLeaseRepository(session).add(
            WorkSlotLease(
                invocation_id=invocation_id,
                workspace_id=ws.id,
                holder_worker_id=CRASHED_WORKER_ID,
                holder_epoch=1,
                ticket_at=now,
                acquired_at=now,
                expires_at=now + timedelta(minutes=5),
                phase=WorkSlotLeasePhase.held.value,
                cleanup_unproven=False,
            )
        )
        await session.commit()
    processes.live.add(invocation_id)
    return invocation_id


async def run_soak(config: SoakConfig) -> SoakReport:
    report = SoakReport()
    async with scratch_database(config) as url:
        engine = make_engine(url)
        try:
            session_factory = make_session_factory(engine)
            _monitor_ids, queued_ids = await seed(
                session_factory, monitors=config.monitors, queued=config.queued
            )
            processes = FakeProcesses()
            executor = SoakExecutor(
                session_factory, processes, agent_seconds=config.agent_seconds, report=report
            )
            samples: list[int] = []
            stop_sampling = asyncio.Event()
            sampler = asyncio.create_task(_sample(session_factory, samples, stop_sampling))
            start = asyncio.get_running_loop().time()
            try:
                first = _worker(session_factory, executor, processes, config)
                await _run_worker_until(first, start + config.duration / 2)
                await _stop_worker(first, executor)

                # Restart: the crashed worker's agent run is still in flight.
                crashed_invocation = await _plant_crashed_invocation(session_factory, processes)
                restarted = _worker(session_factory, executor, processes, config)
                await restarted.run_once()
                report.adoption = await _adoption_state(
                    session_factory, crashed_invocation, restarted
                )
                processes.live.discard(crashed_invocation)
                await _run_worker_until(restarted, start + config.duration)
                report.adoption["released_after_exit"] = (
                    await _lease(session_factory, crashed_invocation) is None
                )
                await _stop_worker(restarted, executor)
            finally:
                stop_sampling.set()
                await sampler
            statuses = await _statuses(session_factory, queued_ids)
            report.samples = len(samples)
            report.max_leases_in_use = max(samples, default=0)
            report.admitted = sorted(
                ws for ws, status in statuses.items() if status != WorkspaceStatus.requested.value
            )
            report.not_admitted = sorted(set(queued_ids) - set(report.admitted))
            report.violations = check_invariants(
                lease_samples=samples,
                slots=config.slots,
                queued_statuses=statuses,
                adoption=report.adoption,
            )
        finally:
            await engine.dispose()
    return report


async def _lease(
    session_factory: async_sessionmaker[AsyncSession], invocation_id: str
) -> WorkSlotLease | None:
    async with session_factory() as session:
        return await WorkSlotLeaseRepository(session).get(invocation_id)


async def _adoption_state(
    session_factory: async_sessionmaker[AsyncSession],
    invocation_id: str,
    worker: ControlWorker,
) -> dict[str, Any]:
    lease = await _lease(session_factory, invocation_id)
    if lease is None:
        return {"adopted": False, "lease": None}
    adopted = (
        lease.holder_worker_id == worker._worker_id  # noqa: SLF001
        and lease.holder_epoch == 2
        and lease.phase == WorkSlotLeasePhase.adopted.value
    )
    return {
        "adopted": adopted,
        "holder_worker_id": lease.holder_worker_id,
        "holder_epoch": lease.holder_epoch,
        "phase": lease.phase,
    }


async def _statuses(
    session_factory: async_sessionmaker[AsyncSession], workspace_ids: Sequence[str]
) -> dict[str, str]:
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        statuses: dict[str, str] = {}
        for workspace_id in workspace_ids:
            ws = await repo.get(workspace_id)
            statuses[workspace_id] = ws.status if ws is not None else "missing"
        return statuses


def parse_args(argv: Sequence[str] | None = None) -> SoakConfig:
    parser = argparse.ArgumentParser(description="Slot-release soak (manual; never CI).")
    parser.add_argument("--database-url", default=os.environ.get("AWF_DATABASE_URL"))
    parser.add_argument("--slots", type=int, default=3)
    parser.add_argument("--monitors", type=int, default=7)
    parser.add_argument("--queued", type=int, default=3)
    parser.add_argument("--agent-seconds", type=float, default=0.5)
    parser.add_argument("--duration", type=float, default=60.0)
    parser.add_argument("--keep-db", action="store_true")
    args = parser.parse_args(argv)
    if not args.database_url:
        parser.error("--database-url (or AWF_DATABASE_URL) is required")
    return SoakConfig(
        database_url=args.database_url,
        slots=args.slots,
        monitors=args.monitors,
        queued=args.queued,
        agent_seconds=args.agent_seconds,
        duration=args.duration,
        keep_db=args.keep_db,
    )


def main(argv: Sequence[str] | None = None) -> int:
    config = parse_args(argv)
    report = asyncio.run(run_soak(config))
    print(json.dumps(asdict(report), indent=2, sort_keys=True, default=str))
    return 1 if report.violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
