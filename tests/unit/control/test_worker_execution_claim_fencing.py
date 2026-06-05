"""End-to-end execution-claim epoch fencing (issue #421).

Ties the lease-fencing mechanism together against a real Provisioner + git +
PostgreSQL:

- the normal single-worker provision path completes to ``ready`` unaffected
  (the epoch is claimed, threaded, and CAS'd without stalling);
- a stale worker whose epoch was superseded by a later claimant is fenced on
  its heartbeat/read and its release cannot clobber or steal the new claim.
"""

from __future__ import annotations

import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.control.worker import ControlWorker, WorkerConfig
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.node.git_manager import GitManager
from awf.node.provisioner import Provisioner, ProvisionerConfig
from tests.postgres import postgres_test_engine


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
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _worker(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path, *, suffix: str = ""
) -> ControlWorker:
    git = GitManager(tmp_path / f"awf-work{suffix}")
    prov = Provisioner(
        session_factory=session_factory,
        git=git,
        config=ProvisionerConfig(node_id="test-node-01"),
    )
    return ControlWorker(
        session_factory=session_factory,
        provisioner=prov,
        config=WorkerConfig(poll_interval_seconds=0.01, max_concurrent_provisions=1),
    )


async def _create_requested(
    session_factory: async_sessionmaker[AsyncSession], origin: Path, title: str
) -> str:
    async with session_factory() as s:
        ws = await WorkspaceRepository(s).create(
            repo_url=str(origin),
            branch_base="development",
            task_title=title,
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        await s.commit()
        return ws.id


@pytest.mark.unit
async def test_normal_single_worker_provision_completes_to_ready(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    origin_repo: Path,
) -> None:
    workspace_id = await _create_requested(session_factory, origin_repo, "normal-path")
    worker = _worker(session_factory, tmp_path)

    assert await worker.run_once() == 1

    async with session_factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        # No stall: the epoch was claimed (0 -> 1), threaded to the provisioner,
        # CAS'd to ready, and the claim released afterward.
        assert ws.status == WorkspaceStatus.ready.value
        assert ws.execution_claim_epoch == 1
        assert ws.execution_claimed_by is None
    # The epoch map is cleared after dispatch returns.
    assert workspace_id not in worker._execution_claim_epochs  # noqa: SLF001


@pytest.mark.unit
async def test_stale_worker_is_fenced_and_cannot_clobber_or_steal(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    origin_repo: Path,
) -> None:
    workspace_id = await _create_requested(session_factory, origin_repo, "fenced-path")
    worker_a = _worker(session_factory, tmp_path, suffix="-a")
    worker_b = _worker(session_factory, tmp_path, suffix="-b")

    # Worker A claims the workspace (epoch 0 -> 1) and starts an in-flight
    # provision, capturing its epoch.
    assert await worker_a._claim_requested_for_provisioning(workspace_id)  # noqa: SLF001
    worker_a._execution_claim_epochs[workspace_id] = 1  # noqa: SLF001

    # A later claimant (worker B) supersedes A's claim: the epoch advances and
    # ownership flips (as a recovery-clear + re-claim would produce).
    async with session_factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        ws.execution_claim_epoch = 2
        ws.execution_claimed_by = worker_b._worker_id  # noqa: SLF001
        await s.commit()

    # A is fenced: it no longer owns the claim, so the read returns None and the
    # heartbeat CAS fails.
    assert await worker_a._read_execution_claim_epoch(workspace_id) is None  # noqa: SLF001
    assert await worker_a._refresh_execution_claim(workspace_id) is False  # noqa: SLF001

    # A's release CAS (on its stale epoch 1) updates 0 rows: it cannot clobber
    # or steal the newer claimant's lease.
    await worker_a._release_execution_claim(workspace_id)  # noqa: SLF001

    async with session_factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.execution_claim_epoch == 2
        assert ws.execution_claimed_by == worker_b._worker_id  # noqa: SLF001

    # Worker B still owns its claim and reads its own epoch back.
    assert await worker_b._read_execution_claim_epoch(workspace_id) == 2  # noqa: SLF001
