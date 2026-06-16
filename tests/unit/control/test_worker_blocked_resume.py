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
from awf.control.worker.constants import ORDERED_BLOCKED_RESUME_REASON
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
    # task must restore the paused ``blocked`` state and release the claim
    # instead of stranding the claimed ``running`` row (which stale-active
    # recovery would later FAIL, dropping the operator-visible paused state).
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
        assert ws.status == WorkspaceStatus.running.value

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
        # The paused state was restored instead of being stranded in running.
        assert ws.status == WorkspaceStatus.blocked.value
        # The operator directive is preserved so the next cycle can resume it.
        assert ws.pending_operator_hint is not None
        assert ws.pending_operator_hint["directive"] == "revert the change"
        # Cleanup ran: the execution claim was released rather than stranded.
        assert ws.execution_claimed_by is None
        assert ws.execution_claim_expires_at is None


@pytest.mark.unit
async def test_run_once_restores_blocked_when_ordered_decision_write_fails(
    session_factory: async_sessionmaker[AsyncSession],
    origin_repo: Path,
) -> None:
    # ``run_once`` commits the ``blocked -> running`` claim in
    # ``_claim_blocked_resume_ids`` *before* the fallible
    # ``_record_ordered_decisions`` queue-decision write and before dispatch
    # starts a resume task. If that write fails after retries, no resume task
    # runs and the row would be stranded in ``running`` with the operator hint
    # still pending — stale-active recovery would then FAIL it as an abandoned
    # active execution, dropping the operator-visible paused state. ``run_once``
    # must restore the claimed-but-undispatched workspace to ``blocked``.
    blocked_id = await _create_blocked(
        session_factory,
        origin_repo,
        "ordered-decision-fails",
        directive="revert the change",
    )
    executor = _RecordingBlockedExecutor()
    worker = _worker(session_factory, executor)

    # Fail only the blocked-resume ordered-decision write — the empty monitor
    # and ready writes must still succeed so the blocked claim is reached first.
    original_record = worker._record_ordered_decisions  # noqa: SLF001

    async def _record_with_blocked_failure(workspace_ids: list[str], *, reason_code: str) -> None:
        if reason_code == ORDERED_BLOCKED_RESUME_REASON:
            raise RuntimeError("ordered decision write failed")
        await original_record(workspace_ids, reason_code=reason_code)

    worker._record_ordered_decisions = _record_with_blocked_failure  # type: ignore[method-assign]  # noqa: SLF001

    with pytest.raises(RuntimeError, match="ordered decision write failed"):
        await asyncio.wait_for(worker.run_once(), timeout=WORKER_TEST_TIMEOUT_SECONDS)

    # No resume task ran because dispatch was never reached.
    assert executor.resume_blocked_calls == []
    async with session_factory() as s:
        ws = await WorkspaceRepository(s).get(blocked_id)
        assert ws is not None
        # The paused state was restored instead of being stranded in running.
        assert ws.status == WorkspaceStatus.blocked.value
        # The operator directive is preserved so the next cycle can resume it.
        assert ws.pending_operator_hint is not None
        assert ws.pending_operator_hint["directive"] == "revert the change"
        # The execution claim won by the resume CAS was released, not stranded.
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


@pytest.mark.unit
async def test_claim_blocked_for_resume_returns_false_when_not_blocked(
    session_factory: async_sessionmaker[AsyncSession],
    origin_repo: Path,
) -> None:
    # The resume CAS only wins a row still in ``blocked``. A workspace that
    # already left ``blocked`` (e.g. a competing worker resumed it) yields no
    # row, so the claim must report failure without touching the state.
    ready_id = await _create_ready(session_factory, origin_repo, "not-blocked")
    worker = _worker(session_factory, _RecordingBlockedExecutor())

    assert await worker._claim_blocked_for_resume(ready_id) is False  # noqa: SLF001
    async with session_factory() as s:
        ws = await WorkspaceRepository(s).get(ready_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.ready.value


@pytest.mark.unit
async def test_list_resumable_blocked_returns_empty_when_limit_non_positive(
    session_factory: async_sessionmaker[AsyncSession],
    origin_repo: Path,
) -> None:
    # A non-positive row limit short-circuits before any DB query.
    await _create_blocked(session_factory, origin_repo, "blocked", directive="go")
    worker = _worker(session_factory, _RecordingBlockedExecutor())

    assert await worker._list_resumable_blocked(limit=0) == []  # noqa: SLF001


@pytest.mark.unit
async def test_claim_blocked_resume_ids_skips_in_flight_and_honors_limit(
    session_factory: async_sessionmaker[AsyncSession],
    origin_repo: Path,
) -> None:
    # An already-tracked workspace is skipped (it is being resumed), and the
    # per-cycle limit caps how many fresh rows are claimed.
    in_flight = await _create_blocked(session_factory, origin_repo, "in-flight", directive="go")
    claimable = await _create_blocked(session_factory, origin_repo, "claimable", directive="go")
    overflow = await _create_blocked(session_factory, origin_repo, "overflow", directive="go")
    worker = _worker(session_factory, _RecordingBlockedExecutor())

    async def _noop() -> None:
        return None

    worker._execution_tasks[in_flight] = asyncio.create_task(_noop())  # noqa: SLF001

    claimed = await worker._claim_blocked_resume_ids(  # noqa: SLF001
        [in_flight, claimable, overflow], limit=1
    )

    assert claimed == [claimable]
    async with session_factory() as s:
        repo = WorkspaceRepository(s)
        # The in-flight row was skipped, not re-claimed.
        in_flight_ws = await repo.get(in_flight)
        assert in_flight_ws is not None and in_flight_ws.status == WorkspaceStatus.blocked.value
        # The overflow row was left for a later cycle (limit reached).
        overflow_ws = await repo.get(overflow)
        assert overflow_ws is not None and overflow_ws.status == WorkspaceStatus.blocked.value
    await asyncio.wait_for(worker.wait_for_execution_tasks(), timeout=WORKER_TEST_TIMEOUT_SECONDS)


@pytest.mark.unit
async def test_dispatch_blocked_resumes_skips_in_flight_and_honors_limit(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Pure dispatch-loop wiring: an already-tracked workspace is skipped and the
    # limit caps how many resume tasks are spawned this cycle.
    worker = _worker(session_factory, _RecordingBlockedExecutor())
    resumed: list[str] = []

    async def _fake_resume(workspace_id: str) -> None:
        resumed.append(workspace_id)

    worker._safely_resume_blocked_claimed = _fake_resume  # type: ignore[method-assign]  # noqa: SLF001

    async def _noop() -> None:
        return None

    worker._execution_tasks["in-flight"] = asyncio.create_task(_noop())  # noqa: SLF001

    dispatched = worker._dispatch_blocked_resumes(  # noqa: SLF001
        ["in-flight", "fresh", "overflow"], limit=1
    )

    assert dispatched == {"fresh"}
    await asyncio.wait_for(worker.wait_for_execution_tasks(), timeout=WORKER_TEST_TIMEOUT_SECONDS)
    assert resumed == ["fresh"]


@pytest.mark.unit
async def test_safely_resume_blocked_claimed_swallows_executor_error(
    session_factory: async_sessionmaker[AsyncSession],
    origin_repo: Path,
) -> None:
    # A resume that raises inside the executor must not escape: the error is
    # logged and the ``finally`` still releases the execution claim so the row
    # is not left wedged with a stranded claim.
    blocked_id = await _create_blocked(
        session_factory, origin_repo, "executor-raises", directive="go"
    )

    class _RaisingExecutor(_RecordingBlockedExecutor):
        async def resume_blocked_execution(self, workspace_id: str, **_kwargs: object) -> None:
            raise RuntimeError("resume boom")

    worker = _worker(session_factory, _RaisingExecutor())
    assert await worker._claim_blocked_for_resume(blocked_id)  # noqa: SLF001

    # Must not raise despite the executor blowing up.
    await asyncio.wait_for(
        worker._safely_resume_blocked_claimed(blocked_id),  # noqa: SLF001
        timeout=WORKER_TEST_TIMEOUT_SECONDS,
    )

    async with session_factory() as s:
        ws = await WorkspaceRepository(s).get(blocked_id)
        assert ws is not None
        # The claim was released by the ``finally``, not stranded.
        assert ws.execution_claimed_by is None
        assert ws.execution_claim_expires_at is None


@pytest.mark.unit
async def test_restore_blocked_resume_claim_noop_when_row_not_running(
    session_factory: async_sessionmaker[AsyncSession],
    origin_repo: Path,
) -> None:
    # The restore CAS is gated on ``running`` + owner. A row no longer in
    # ``running`` (or owned by another claimant) yields no row, so the restore
    # is a silent no-op rather than clobbering the current state.
    blocked_id = await _create_blocked(
        session_factory, origin_repo, "already-blocked", directive="go"
    )
    worker = _worker(session_factory, _RecordingBlockedExecutor())

    await worker._restore_blocked_resume_claim(  # noqa: SLF001
        blocked_id, reason_code="EXECUTOR_BLOCKED_RESUME_ABORTED"
    )

    async with session_factory() as s:
        ws = await WorkspaceRepository(s).get(blocked_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.blocked.value


@pytest.mark.unit
async def test_restore_blocked_resume_claim_swallows_session_failure(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # A DB failure during restore is logged and swallowed — the surrounding
    # cleanup path must not propagate it.
    worker = _worker(session_factory, _RecordingBlockedExecutor())

    def _boom() -> AsyncSession:
        raise RuntimeError("session unavailable")

    worker._session_factory = _boom  # type: ignore[method-assign]  # noqa: SLF001

    # Must return without raising.
    await worker._restore_blocked_resume_claim(  # noqa: SLF001
        "ws_missing", reason_code="EXECUTOR_BLOCKED_RESUME_ABORTED"
    )
