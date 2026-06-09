"""ControlWorker capacity queue decision tests split from part 003."""

from __future__ import annotations

import subprocess
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.control.worker import ControlWorker, WorkerConfig
from awf.control.worker import claims as worker_claims
from awf.control.worker import resource_broker as worker_resource_broker
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import (
    QueueDecisionRepository,
    ResourceReservationRepository,
    TaskAttemptRepository,
    TaskRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from tests.postgres import postgres_test_engine

pytestmark = pytest.mark.unit


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def origin_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "origin"
    repo.mkdir()
    _git(["init", "-q", "-b", "development"], repo)
    _git(["config", "user.name", "T"], repo)
    _git(["config", "user.email", "t@t"], repo)
    (repo / "README.md").write_text("hello\n")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "init"], repo)
    return repo


@pytest.fixture
async def session_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    del tmp_path
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


async def _create_requested(
    session_factory: async_sessionmaker[AsyncSession],
    origin: Path,
    title: str,
    *,
    create_task_attempt: bool = False,
) -> str:
    async with session_factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.create(
            repo_url=str(origin),
            branch_base="development",
            task_title=title,
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        if create_task_attempt:
            task = await TaskRepository(s).create_or_get(
                repo_url=ws.repo_url,
                base_branch=ws.branch_base,
                title=ws.task_title,
                prompt=ws.task_prompt,
                external_id=None,
                idempotency_key=None,
                task_class=ws.task_class,
                owned_paths=list(ws.owned_paths),
            )
            await TaskAttemptRepository(s).create_for_workspace(task=task, workspace=ws)
        await s.commit()
        return ws.id


async def _reserve_workspace(
    session_factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    *,
    steady_cpu: float = 1.0,
    steady_memory_gb: float = 1.0,
    peak_cpu: float = 1.0,
    peak_memory_gb: float = 1.0,
    disk_mb: int | None = None,
    dind_slots: int = 0,
    node_id: str = "local",
) -> None:
    async with session_factory() as s:
        attempt = await TaskAttemptRepository(s).get_by_workspace_id(workspace_id)
        assert attempt is not None
        await ResourceReservationRepository(s).create(
            workspace_id=workspace_id,
            attempt_id=attempt.id,
            node_id=node_id,
            steady_cpu=steady_cpu,
            steady_memory_gb=steady_memory_gb,
            peak_cpu=peak_cpu,
            peak_memory_gb=peak_memory_gb,
            disk_mb=disk_mb,
            dind_slots=dind_slots,
            phase="workspace_lifecycle",
        )
        await s.commit()


async def _create_ready(
    session_factory: async_sessionmaker[AsyncSession],
    origin: Path,
    title: str,
    *,
    create_task_attempt: bool = False,
) -> str:
    async with session_factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.create(
            repo_url=str(origin),
            branch_base="development",
            task_title=title,
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        if create_task_attempt:
            task = await TaskRepository(s).create_or_get(
                repo_url=ws.repo_url,
                base_branch=ws.branch_base,
                title=ws.task_title,
                prompt=ws.task_prompt,
                external_id=None,
                idempotency_key=None,
                task_class=ws.task_class,
                owned_paths=list(ws.owned_paths),
            )
            await TaskAttemptRepository(s).create_for_workspace(task=task, workspace=ws)
        await repo.transition(ws, to=WorkspaceStatus.provisioning, reason_code="SEED")
        ws.branch_name = f"awf/{ws.id}"
        ws.base_commit = "a" * 40
        ws.compose_project_name = f"awf_{ws.id}"
        ws.compose_file_path = f"/tmp/awf/{ws.id}/compose.yml"
        await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="SEED")
        await s.commit()
        return ws.id


class _UnexpectedProvisioner:
    async def provision(self, workspace_id: str) -> None:
        raise AssertionError(f"workspace {workspace_id} should remain queued")

    async def provision_claimed(
        self, workspace_id: str, execution_claim_epoch: int | None = None
    ) -> None:
        raise AssertionError(f"workspace {workspace_id} should remain queued")


class _TransitioningProvisioner:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self.calls: list[str] = []

    async def provision(self, workspace_id: str) -> None:
        await self.provision_claimed(workspace_id)

    async def provision_claimed(
        self, workspace_id: str, execution_claim_epoch: int | None = None
    ) -> None:
        self.calls.append(workspace_id)
        async with self._session_factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.get(workspace_id)
            assert ws is not None
            if ws.status == WorkspaceStatus.requested.value:
                await repo.transition(ws, to=WorkspaceStatus.provisioning, reason_code="TEST")
            elif ws.status != WorkspaceStatus.provisioning.value:
                return
            ws.branch_name = f"awf/{workspace_id}"
            ws.base_commit = "b" * 40
            ws.compose_project_name = f"awf_{workspace_id}"
            ws.compose_file_path = f"/tmp/awf/{workspace_id}/compose.yml"
            await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="TEST_READY")
            await s.commit()


class TestRunOnceCapacityDecisionsPart043:
    async def test_capacity_queue_decision_warns_when_attempt_is_missing(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        requested_id = await _create_requested(
            session_factory,
            origin_repo,
            "missing-attempt-capacity-decision",
            create_task_attempt=False,
        )
        decided_at = datetime(2026, 5, 20, 12, 0, tzinfo=UTC)

        async with session_factory() as s:
            workspace = await WorkspaceRepository(s).get(requested_id)
            assert workspace is not None
            with structlog.testing.capture_logs() as captured:
                await worker_claims._record_capacity_queue_decision(
                    s,
                    workspace,
                    decision="deferred",
                    reason_code="LOCAL_CAPACITY_DEFERRED",
                    decided_at=decided_at,
                    allocated=worker_claims._AllocatedReservationTotals(),
                    demand=worker_resource_broker._ReservationDemand(
                        workspace_id=requested_id,
                        steady_cpu=1.0,
                        steady_memory_gb=1.0,
                        peak_cpu=1.0,
                        peak_memory_gb=1.0,
                        disk_mb=0,
                        dind_slots=0,
                    ),
                    blockers=[],
                )

        assert any(
            event.get("event") == "worker.capacity_queue_decision_missing_attempt"
            and event.get("log_level") == "warning"
            and event.get("workspace_id") == requested_id
            for event in captured
        )

    async def test_requested_capacity_gate_skips_repeated_unchanged_capacity_deferral(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        active_id = await _create_ready(
            session_factory,
            origin_repo,
            "stable-capacity-holder",
            create_task_attempt=True,
        )
        await _reserve_workspace(
            session_factory,
            active_id,
            steady_cpu=3.0,
            steady_memory_gb=8.0,
            peak_cpu=6.0,
            peak_memory_gb=16.0,
            dind_slots=1,
        )
        requested_id = await _create_requested(
            session_factory,
            origin_repo,
            "stable-capacity-deferred",
            create_task_attempt=True,
        )
        await _reserve_workspace(
            session_factory,
            requested_id,
            steady_cpu=3.0,
            steady_memory_gb=8.0,
            peak_cpu=6.0,
            peak_memory_gb=16.0,
            dind_slots=1,
        )
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_UnexpectedProvisioner(),  # type: ignore[arg-type]
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_provisions=2,
                local_capacity_cpu_cores=6.0,
                local_capacity_memory_gb=16.0,
                local_capacity_dind_slots=1,
            ),
        )

        assert await worker.run_once() == 0
        assert await worker.run_once() == 0

        async with session_factory() as s:
            decisions = await QueueDecisionRepository(s).list_for_workspace(requested_id)

        deferred_decisions = [
            decision for decision in decisions if decision.reason_code == "LOCAL_CAPACITY_DEFERRED"
        ]
        assert len(deferred_decisions) == 1

    async def test_requested_capacity_gate_scans_only_workspaces_for_worker_node(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        remote_id = await _create_requested(
            session_factory,
            origin_repo,
            "remote-capacity-request",
            create_task_attempt=True,
        )
        local_id = await _create_requested(
            session_factory,
            origin_repo,
            "local-capacity-request",
            create_task_attempt=True,
        )
        async with session_factory() as s:
            remote = await WorkspaceRepository(s).get(remote_id)
            local = await WorkspaceRepository(s).get(local_id)
            assert remote is not None
            assert local is not None
            remote.node_id = "worker-node-b"
            local.node_id = "worker-node-a"
            await s.commit()
        await _reserve_workspace(
            session_factory,
            remote_id,
            node_id="worker-node-b",
            steady_cpu=2.0,
            steady_memory_gb=1.0,
            peak_cpu=2.0,
            peak_memory_gb=1.0,
        )
        await _reserve_workspace(
            session_factory,
            local_id,
            node_id="worker-node-a",
            steady_cpu=1.0,
            steady_memory_gb=1.0,
            peak_cpu=1.0,
            peak_memory_gb=1.0,
        )
        provisioner = _TransitioningProvisioner(session_factory)
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=provisioner,  # type: ignore[arg-type]
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_provisions=1,
                node_id="worker-node-a",
                local_capacity_cpu_cores=1.0,
            ),
        )

        assert await worker.run_once() == 1

        async with session_factory() as s:
            remote = await WorkspaceRepository(s).get(remote_id)
            local = await WorkspaceRepository(s).get(local_id)
            remote_decisions = await QueueDecisionRepository(s).list_for_workspace(remote_id)

        assert provisioner.calls == [local_id]
        assert remote is not None
        assert remote.status == WorkspaceStatus.requested.value
        assert local is not None
        assert local.status == WorkspaceStatus.ready.value
        assert remote_decisions == []
