"""ControlWorker pre-PR ``blocked``-resume dispatch.

A workspace that paused at a protected quality-gate violation sits in
``blocked`` until an operator resolves it via ``guide`` (a directive armed in
``pending_operator_hint`` or a grant active for the current ``block_epoch``).
The worker loop must then re-claim it (``blocked -> running``) and drive the
executor's ``resume_blocked_execution``. These tests exercise that wiring end to
end against real git + PostgreSQL and guard against re-dispatching a workspace
that is still awaiting an operator decision (which would spin
``blocked -> running -> blocked`` every cycle).
"""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.control.worker import ControlWorker, WorkerConfig
from awf.db.enums import WorkspaceStatus
from awf.db.models import OperatorGrantAuditRecord
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from tests.postgres import postgres_test_engine

WORKER_TEST_TIMEOUT_SECONDS = 60.0


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
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


class _TransitioningProvisioner:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def provision_claimed(
        self, workspace_id: str, execution_claim_epoch: int | None = None
    ) -> None:  # pragma: no cover - no requested workspaces in these tests
        del workspace_id, execution_claim_epoch

    def get_worktree_path(self, workspace_id: str) -> Path | None:
        del workspace_id
        return None


class _RecordingBlockedExecutor:
    def __init__(self) -> None:
        self.resume_blocked_calls: list[str] = []

    async def execute(self, workspace_id: str, **_kwargs: object) -> None:  # pragma: no cover
        del workspace_id

    async def resume_pr_monitor(self, workspace_id: str) -> None:  # pragma: no cover
        del workspace_id

    async def resume_blocked_execution(self, workspace_id: str, **_kwargs: object) -> None:
        self.resume_blocked_calls.append(workspace_id)


async def _create_blocked(
    session_factory: async_sessionmaker[AsyncSession],
    origin: Path,
    title: str,
    *,
    directive: str | None = None,
    grant_path: str | None = None,
    block_epoch: int = 1,
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
        await repo.transition(ws, to=WorkspaceStatus.provisioning, reason_code="SEED")
        ws.branch_name = f"awf/{ws.id}"
        ws.base_commit = "a" * 40
        ws.compose_project_name = f"awf_{ws.id}"
        ws.compose_file_path = f"/tmp/awf/{ws.id}/compose.yml"
        await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="SEED")
        await repo.transition(ws, to=WorkspaceStatus.running, reason_code="SEED")
        await repo.transition(ws, to=WorkspaceStatus.blocked, reason_code="SEED")
        ws.block_epoch = block_epoch
        if directive is not None:
            ws.pending_operator_hint = {
                "status": "pending",
                "directive": directive,
                "reason": "r",
            }
        if grant_path is not None:
            s.add(
                OperatorGrantAuditRecord(
                    id=f"grant_{ws.id}",
                    workspace_id=ws.id,
                    operator="op@example.com",
                    reason="approved",
                    normalized_path=grant_path,
                    block_epoch=block_epoch,
                )
            )
        await s.commit()
        return ws.id


def _worker(
    session_factory: async_sessionmaker[AsyncSession],
    executor: _RecordingBlockedExecutor,
) -> ControlWorker:
    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
        executor=executor,
        config=WorkerConfig(
            poll_interval_seconds=0.01,
            max_concurrent_provisions=1,
            max_concurrent_executions=1,
        ),
    )
    worker._next_stale_active_execution_scan_at = float("inf")  # noqa: SLF001
    return worker


@pytest.mark.unit
@pytest.mark.parametrize(
    "directive,grant_path",
    [("revert the change", None), (None, "pyproject.toml")],
)
async def test_run_once_resumes_operator_cleared_blocked_workspace(
    session_factory: async_sessionmaker[AsyncSession],
    origin_repo: Path,
    directive: str | None,
    grant_path: str | None,
) -> None:
    blocked_id = await _create_blocked(
        session_factory,
        origin_repo,
        "cleared",
        directive=directive,
        grant_path=grant_path,
    )
    executor = _RecordingBlockedExecutor()
    worker = _worker(session_factory, executor)

    dispatched = await asyncio.wait_for(worker.run_once(), timeout=WORKER_TEST_TIMEOUT_SECONDS)
    await asyncio.wait_for(worker.wait_for_execution_tasks(), timeout=WORKER_TEST_TIMEOUT_SECONDS)

    assert dispatched == 1
    assert executor.resume_blocked_calls == [blocked_id]
    # The resume CAS performed the blocked -> running transition.
    async with session_factory() as s:
        ws = await WorkspaceRepository(s).get(blocked_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.running.value


@pytest.mark.unit
async def test_resume_blocked_claimed_releases_claim_when_executor_missing(
    session_factory: async_sessionmaker[AsyncSession],
    origin_repo: Path,
) -> None:
    # The resume CAS transitions ``blocked -> running`` and stamps the execution
    # claim *before* dispatch. If the worker then has no executor, the resume
    # task must still release the claim instead of stranding the claimed
    # ``running`` row until lease expiry.
    blocked_id = await _create_blocked(
        session_factory,
        origin_repo,
        "no-executor",
        directive="revert the change",
    )
    executor = _RecordingBlockedExecutor()
    worker = _worker(session_factory, executor)

    # Simulate the pre-dispatch claim CAS acquiring the execution claim.
    assert await worker._claim_blocked_for_resume(blocked_id)  # noqa: SLF001
    async with session_factory() as s:
        ws = await WorkspaceRepository(s).get(blocked_id)
        assert ws is not None
        assert ws.execution_claimed_by == worker._worker_id  # noqa: SLF001

    # The executor went away after the claim was acquired.
    worker._executor = None  # noqa: SLF001
    await asyncio.wait_for(
        worker._safely_resume_blocked_claimed(blocked_id),  # noqa: SLF001
        timeout=WORKER_TEST_TIMEOUT_SECONDS,
    )

    assert executor.resume_blocked_calls == []
    async with session_factory() as s:
        ws = await WorkspaceRepository(s).get(blocked_id)
        assert ws is not None
        # Cleanup ran: the execution claim was released rather than stranded.
        assert ws.execution_claimed_by is None
        assert ws.execution_claim_expires_at is None


async def _create_ready(
    session_factory: async_sessionmaker[AsyncSession],
    origin: Path,
    title: str,
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
        await repo.transition(ws, to=WorkspaceStatus.provisioning, reason_code="SEED")
        ws.branch_name = f"awf/{ws.id}"
        ws.base_commit = "b" * 40
        ws.compose_project_name = f"awf_{ws.id}"
        ws.compose_file_path = f"/tmp/awf/{ws.id}/compose.yml"
        await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="SEED")
        await s.commit()
        return ws.id


@pytest.mark.unit
async def test_run_once_prioritizes_blocked_resume_over_ready_when_slots_limited(
    session_factory: async_sessionmaker[AsyncSession],
    origin_repo: Path,
) -> None:
    # With only one execution slot, an operator-cleared blocked workspace must
    # take the slot ahead of fresh ready work: in-flight paused executions take
    # priority over starting brand-new ones. Guards against silent regression of
    # the dispatch ordering in ``run_once`` (blocked-resume before ready).
    blocked_id = await _create_blocked(
        session_factory,
        origin_repo,
        "blocked-first",
        directive="revert the change",
    )
    ready_id = await _create_ready(session_factory, origin_repo, "ready-work")

    executor = _RecordingBlockedExecutor()
    worker = _worker(session_factory, executor)

    dispatched = await asyncio.wait_for(worker.run_once(), timeout=WORKER_TEST_TIMEOUT_SECONDS)
    await asyncio.wait_for(worker.wait_for_execution_tasks(), timeout=WORKER_TEST_TIMEOUT_SECONDS)

    assert dispatched == 1
    assert executor.resume_blocked_calls == [blocked_id]
    # The single slot went to the blocked resume; the ready workspace was left
    # untouched for a later cycle.
    async with session_factory() as s:
        ready_ws = await WorkspaceRepository(s).get(ready_id)
        assert ready_ws is not None
        assert ready_ws.status == WorkspaceStatus.ready.value


@pytest.mark.unit
async def test_run_once_leaves_undecided_blocked_workspace_untouched(
    session_factory: async_sessionmaker[AsyncSession],
    origin_repo: Path,
) -> None:
    # No directive and no grant: the operator has not acted, so the worker must
    # not re-dispatch it (otherwise it would spin blocked -> running -> blocked).
    blocked_id = await _create_blocked(session_factory, origin_repo, "awaiting")
    executor = _RecordingBlockedExecutor()
    worker = _worker(session_factory, executor)

    dispatched = await asyncio.wait_for(worker.run_once(), timeout=WORKER_TEST_TIMEOUT_SECONDS)
    await asyncio.wait_for(worker.wait_for_execution_tasks(), timeout=WORKER_TEST_TIMEOUT_SECONDS)

    assert dispatched == 0
    assert executor.resume_blocked_calls == []
    async with session_factory() as s:
        ws = await WorkspaceRepository(s).get(blocked_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.blocked.value
