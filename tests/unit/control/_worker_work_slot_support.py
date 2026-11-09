"""Fakes and seed helpers for the worker work-slot (slot-release) suites."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.ids import new_work_slot_invocation_id
from awf.control.worker import ControlWorker, WorkerConfig
from awf.db.enums import WorkSlotLeasePhase, WorkspaceStatus
from awf.db.models import WorkSlotLease
from awf.db.repositories import WorkSlotLeaseRepository, WorkspaceRepository
from awf.runtime.inspection import RuntimeService, RuntimeSnapshot

REPO_URL = "git@example.com:org/repo.git"


class HealthyRuntimeInspector:
    async def inspect(self, compose_project_name: str | None) -> RuntimeSnapshot:
        del compose_project_name
        return RuntimeSnapshot(
            stack_state="running",
            services=[
                RuntimeService(
                    name="agent", container_id="agent", image="awf-agent:latest", state="running"
                )
            ],
        )


class RecordingProvisioner:
    """Provisioning that ends ``ready`` (``to_ready``) or stays ``provisioning``."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        to_ready: bool = True,
    ) -> None:
        self._session_factory = session_factory
        self._to_ready = to_ready
        self.calls: list[str] = []
        self.leases_seen: dict[str, list[WorkSlotLease]] = {}

    async def provision(self, workspace_id: str) -> None:  # pragma: no cover - unused
        await self.provision_claimed(workspace_id)

    async def provision_claimed(
        self, workspace_id: str, execution_claim_epoch: int | None = None
    ) -> None:
        del execution_claim_epoch
        self.calls.append(workspace_id)
        async with self._session_factory() as session:
            self.leases_seen[workspace_id] = await WorkSlotLeaseRepository(
                session
            ).list_for_workspace(workspace_id)
            if not self._to_ready:
                return
            repo = WorkspaceRepository(session)
            ws = await repo.get(workspace_id)
            assert ws is not None
            ws.branch_name = f"awf/{workspace_id}"
            ws.base_commit = "b" * 40
            ws.compose_project_name = f"awf_{workspace_id}"
            ws.compose_file_path = f"/tmp/awf/{workspace_id}/compose.yml"
            await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="TEST_READY")
            await session.commit()

    def get_worktree_path(self, workspace_id: str) -> Path | None:
        del workspace_id
        return None


class SlotAwareExecutor:
    """Executor fake that records the bound work slot and can hold it like a hook.

    ``hold_agent`` makes ``execute``/resume calls hold the slot (an agent run);
    ``release`` gates when those runs finish. Monitor resumes wait on
    ``monitor_release`` without holding a slot (a pure WaitForCI monitor).
    """

    def __init__(
        self,
        *,
        hold_agent: bool = False,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self.hold_agent = hold_agent
        self._session_factory = session_factory
        self.release = asyncio.Event()
        self.monitor_release = asyncio.Event()
        self.bound: dict[str, Any] = {}
        self.execute_calls: list[str] = []
        self.leases_at_start: dict[str, Any] = {}
        self.leases_in_hold: dict[str, Any] = {}
        self.resume_calls: list[str] = []
        self.monitor_calls: list[str] = []
        self.started = asyncio.Event()

    def work_slot_binding(self, workspace_id: str, work_slot: Any) -> Any:
        @contextmanager
        def _binding() -> Iterator[None]:
            self.bound[workspace_id] = work_slot
            try:
                yield
            finally:
                self.bound.pop(workspace_id, None)

        return _binding()

    async def _run(self, workspace_id: str) -> None:
        scope = self.bound[workspace_id]
        self.leases_at_start[workspace_id] = scope.lease
        self.started.set()
        if self.hold_agent:
            async with scope.hold("agent"):
                self.leases_in_hold[workspace_id] = scope.lease
                await self.release.wait()

    async def execute(self, workspace_id: str, **_kwargs: object) -> None:
        self.execute_calls.append(workspace_id)
        if self._session_factory is not None:
            # Like the real executor, leave ``ready`` before running the agent.
            async with self._session_factory() as session:
                await WorkspaceRepository(session).transition_if_current(
                    workspace_id,
                    from_status=WorkspaceStatus.ready,
                    to=WorkspaceStatus.running,
                    reason_code="TEST_EXECUTING",
                )
                await session.commit()
        await self._run(workspace_id)

    async def resume_blocked_execution(self, workspace_id: str, **_kwargs: object) -> None:
        self.resume_calls.append(workspace_id)
        await self._run(workspace_id)

    async def resume_recovering_execution(self, workspace_id: str, **_kwargs: object) -> None:
        self.resume_calls.append(workspace_id)
        await self._run(workspace_id)

    async def resume_pr_monitor(self, workspace_id: str) -> None:
        self.monitor_calls.append(workspace_id)
        await self.monitor_release.wait()


def make_worker(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    executor: Any,
    provisioner: Any | None = None,
    max_concurrent_executions: int = 1,
    max_concurrent_provisions: int = 1,
    work_slot_probe: Any | None = None,
    **config: Any,
) -> ControlWorker:
    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=provisioner or RecordingProvisioner(session_factory),  # type: ignore[arg-type]
        executor=executor,
        runtime_inspector=HealthyRuntimeInspector(),
        work_slot_probe=work_slot_probe,
        config=WorkerConfig(
            poll_interval_seconds=0.01,
            max_concurrent_provisions=max_concurrent_provisions,
            max_concurrent_executions=max_concurrent_executions,
            **config,
        ),
    )
    worker._next_stale_active_execution_scan_at = float("inf")  # noqa: SLF001
    return worker


async def create_requested(
    session_factory: async_sessionmaker[AsyncSession], title: str = "queued"
) -> str:
    async with session_factory() as session:
        ws = await WorkspaceRepository(session).create(
            repo_url=REPO_URL,
            branch_base="development",
            task_title=title,
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        await session.commit()
        return ws.id


async def create_in_status(
    session_factory: async_sessionmaker[AsyncSession],
    status: WorkspaceStatus,
    title: str = "active",
    *,
    directive: str | None = None,
) -> str:
    """Seed a workspace walked through the legal transitions up to ``status``."""
    path = {
        WorkspaceStatus.ready: [WorkspaceStatus.provisioning, WorkspaceStatus.ready],
        WorkspaceStatus.running: [
            WorkspaceStatus.provisioning,
            WorkspaceStatus.ready,
            WorkspaceStatus.running,
        ],
        WorkspaceStatus.monitoring_pr: [
            WorkspaceStatus.provisioning,
            WorkspaceStatus.ready,
            WorkspaceStatus.running,
            WorkspaceStatus.validating,
            WorkspaceStatus.pushing,
            WorkspaceStatus.monitoring_pr,
        ],
        WorkspaceStatus.blocked: [
            WorkspaceStatus.provisioning,
            WorkspaceStatus.ready,
            WorkspaceStatus.running,
            WorkspaceStatus.blocked,
        ],
    }[status]
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url=REPO_URL,
            branch_base="development",
            task_title=title,
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        ws.branch_name = f"awf/{ws.id}"
        ws.remote_push_branch = ws.branch_name
        ws.base_commit = "a" * 40
        ws.compose_project_name = f"awf_{ws.id}"
        ws.compose_file_path = f"/tmp/awf/{ws.id}/compose.yml"
        for step in path:
            if step is WorkspaceStatus.monitoring_pr:
                ws.pr_url = "https://github.com/example/repo/pull/7"
                ws.pr_number = 7
            await repo.transition(ws, to=step, reason_code="SEED")
        if status is WorkspaceStatus.blocked:
            ws.block_epoch = 1
            ws.pending_operator_hint = {
                "status": "pending",
                "directive": directive or "revert the change",
                "reason": "r",
            }
        await session.commit()
        return ws.id


async def add_lease(
    session_factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    *,
    holder: str,
    phase: WorkSlotLeasePhase = WorkSlotLeasePhase.held,
    cleanup_unproven: bool = False,
    expires_in: float = 600.0,
    ticket_offset: float = 0.0,
) -> str:
    now = datetime.now(UTC)
    held = phase in {WorkSlotLeasePhase.held, WorkSlotLeasePhase.adopted}
    async with session_factory() as session:
        lease = await WorkSlotLeaseRepository(session).add(
            WorkSlotLease(
                invocation_id=new_work_slot_invocation_id(),
                workspace_id=workspace_id,
                holder_worker_id=holder,
                holder_epoch=1,
                ticket_at=now + timedelta(seconds=ticket_offset),
                acquired_at=now if held else None,
                expires_at=now + timedelta(seconds=expires_in),
                phase=phase.value,
                cleanup_unproven=cleanup_unproven,
            )
        )
        await session.commit()
        return lease.invocation_id


async def held_leases(
    session_factory: async_sessionmaker[AsyncSession],
) -> list[WorkSlotLease]:
    async with session_factory() as session:
        leases = await WorkSlotLeaseRepository(session).list_for_node(node_id=None)
    return [
        lease
        for lease in leases
        if lease.phase in {WorkSlotLeasePhase.held.value, WorkSlotLeasePhase.adopted.value}
    ]


async def status_of(session_factory: async_sessionmaker[AsyncSession], workspace_id: str) -> str:
    async with session_factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        return ws.status


async def wait_until(predicate: Any, *, timeout: float = 15.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        result = predicate()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached before timeout")
        await asyncio.sleep(0.02)


class GatedSlotExecutor(SlotAwareExecutor):
    """``SlotAwareExecutor`` with per-workspace gates and a hold-order log.

    Monitor resumes optionally run an agent action (``monitor_agent``): they wait
    on ``monitor_release`` holding nothing, then hold a slot like AddressComments.
    ``wedge`` makes that held agent run swallow cancellation until ``unwedge``.
    """

    def __init__(
        self,
        *,
        monitor_agent: bool = False,
        wedge: bool = False,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        super().__init__(hold_agent=True, session_factory=session_factory)
        self.monitor_agent = monitor_agent
        self.wedge = wedge
        self.unwedge = asyncio.Event()
        self.gates: dict[str, asyncio.Event] = {}
        self.hold_order: list[str] = []
        self.cancellations = 0

    def gate(self, workspace_id: str) -> asyncio.Event:
        return self.gates.setdefault(workspace_id, asyncio.Event())

    async def _run(self, workspace_id: str) -> None:
        scope = self.bound[workspace_id]
        self.leases_at_start[workspace_id] = scope.lease
        self.started.set()
        async with scope.hold("agent"):
            self.hold_order.append(workspace_id)
            self.leases_in_hold[workspace_id] = scope.lease
            await self._wait_in_hold(workspace_id)

    async def _wait_in_hold(self, workspace_id: str) -> None:
        if not self.wedge:
            await self.gate(workspace_id).wait()
            return
        while not self.unwedge.is_set():
            try:
                await asyncio.sleep(0.02)
            except asyncio.CancelledError:
                self.cancellations += 1

    async def resume_pr_monitor(self, workspace_id: str) -> None:
        self.monitor_calls.append(workspace_id)
        await self.monitor_release.wait()
        if self.monitor_agent:
            await self._run(workspace_id)
