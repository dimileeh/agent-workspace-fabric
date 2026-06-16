"""ControlWorker POST-PR ``blocked``-resume routing back into the PR monitor.

A workspace that paused at a protected quality-gate violation *during the PR
monitor* (post-PR) carries the monitor resume-phase sentinel in
``block_resume_phase``. When an operator resolves it via ``guide``, the worker
must claim it ``blocked -> monitoring_pr`` (NOT ``blocked -> running``), bridge
the operator decision into the monitor-state hint map, and dispatch the monitor
resume path so ``decide()`` re-runs its gates. These tests exercise that wiring
against real PostgreSQL.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.control.worker import ControlWorker, WorkerConfig
from awf.db.enums import WorkspaceStatus
from awf.db.models import OperatorGrantAuditRecord
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.operator_hints import operator_hint_from_threads
from awf.runtime.pr_monitor_runner.constants import MONITOR_PR_PROTECTED_PUSH_RESUME_PHASE
from tests.postgres import postgres_test_engine

WORKER_TEST_TIMEOUT_SECONDS = 60.0


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


class _RecordingExecutor:
    def __init__(self) -> None:
        self.resume_blocked_calls: list[str] = []
        self.resume_pr_monitor_calls: list[str] = []

    async def execute(self, workspace_id: str, **_kwargs: object) -> None:  # pragma: no cover
        del workspace_id

    async def resume_pr_monitor(self, workspace_id: str) -> None:
        self.resume_pr_monitor_calls.append(workspace_id)

    async def resume_blocked_execution(
        self, workspace_id: str, **_kwargs: object
    ) -> None:  # pragma: no cover - post-PR blocks never route here
        self.resume_blocked_calls.append(workspace_id)


async def _create_blocked(
    session_factory: async_sessionmaker[AsyncSession],
    title: str,
    *,
    resume_phase: str,
    directive: str | None = None,
    grant_path: str | None = None,
    block_epoch: int = 1,
) -> str:
    async with session_factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.create(
            repo_url="git@github.com:o/r.git",
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
        ws.pr_url = "https://github.com/o/r/pull/7"
        ws.pr_number = 7
        await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="SEED")
        await repo.transition(ws, to=WorkspaceStatus.running, reason_code="SEED")
        await repo.transition(ws, to=WorkspaceStatus.monitoring_pr, reason_code="SEED")
        await repo.transition(ws, to=WorkspaceStatus.blocked, reason_code="SEED")
        ws.block_epoch = block_epoch
        ws.block_resume_phase = resume_phase
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
                    approve_policy_downgrade=True,
                )
            )
        await s.commit()
        return ws.id


def _worker(
    session_factory: async_sessionmaker[AsyncSession],
    executor: _RecordingExecutor,
) -> ControlWorker:
    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
        executor=executor,  # type: ignore[arg-type]
        config=WorkerConfig(
            poll_interval_seconds=0.01,
            max_concurrent_provisions=1,
            max_concurrent_executions=2,
        ),
    )
    worker._next_stale_active_execution_scan_at = float("inf")  # noqa: SLF001
    return worker


@pytest.mark.unit
async def test_partition_blocked_by_resume_phase_splits_pre_and_post_pr(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    post_pr = await _create_blocked(
        session_factory,
        "post-pr",
        resume_phase=MONITOR_PR_PROTECTED_PUSH_RESUME_PHASE,
        directive="revert it",
    )
    pre_pr = await _create_blocked(
        session_factory,
        "pre-pr",
        resume_phase="running",
        directive="revert it",
    )
    worker = _worker(session_factory, _RecordingExecutor())

    pre_ids, post_ids = await worker._partition_blocked_by_resume_phase(  # noqa: SLF001
        [post_pr, pre_pr, "ws_missing"]
    )

    assert post_ids == [post_pr]
    assert pre_ids == [pre_pr]


@pytest.mark.unit
async def test_claim_blocked_for_monitor_resume_bridges_directive(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    blocked_id = await _create_blocked(
        session_factory,
        "directive",
        resume_phase=MONITOR_PR_PROTECTED_PUSH_RESUME_PHASE,
        directive="revert the workflow edit",
    )
    worker = _worker(session_factory, _RecordingExecutor())

    assert await worker._claim_blocked_for_monitor_resume(blocked_id)  # noqa: SLF001

    async with session_factory() as s:
        ws = await WorkspaceRepository(s).get(blocked_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.monitoring_pr.value
        assert ws.monitor_claimed_by == worker._worker_id  # noqa: SLF001
        assert ws.monitor_claim_expires_at is not None
        # The directive is bridged into the monitor-state hint map for decide().
        hint = operator_hint_from_threads(dict(ws.monitor_threads_addressed or {}))
        assert hint is not None
        assert hint.directive == "revert the workflow edit"
        # Left intact so a restore-to-blocked keeps the row resumable.
        assert ws.pending_operator_hint is not None


@pytest.mark.unit
async def test_claim_blocked_for_monitor_resume_bridges_grant_only_hint(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    blocked_id = await _create_blocked(
        session_factory,
        "grant-only",
        resume_phase=MONITOR_PR_PROTECTED_PUSH_RESUME_PHASE,
        grant_path=".github/workflows/ci.yml",
    )
    worker = _worker(session_factory, _RecordingExecutor())

    assert await worker._claim_blocked_for_monitor_resume(blocked_id)  # noqa: SLF001

    async with session_factory() as s:
        ws = await WorkspaceRepository(s).get(blocked_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.monitoring_pr.value
        # A grant-only resolution synthesizes a directive-less pending hint so the
        # operator-hint cycle's grant-only branch re-checks and pushes.
        hint = operator_hint_from_threads(dict(ws.monitor_threads_addressed or {}))
        assert hint is not None
        assert hint.directive is None
        assert hint.status == "pending"


@pytest.mark.unit
async def test_claim_blocked_for_monitor_resume_loses_when_not_blocked(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    blocked_id = await _create_blocked(
        session_factory,
        "raced",
        resume_phase=MONITOR_PR_PROTECTED_PUSH_RESUME_PHASE,
        directive="revert it",
    )
    worker = _worker(session_factory, _RecordingExecutor())
    # First claim wins; a second claim must lose the CAS (already monitoring_pr).
    assert await worker._claim_blocked_for_monitor_resume(blocked_id)  # noqa: SLF001
    assert not await worker._claim_blocked_for_monitor_resume(blocked_id)  # noqa: SLF001


@pytest.mark.unit
async def test_restore_monitor_blocked_resume_claim_reverts_to_blocked(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    blocked_id = await _create_blocked(
        session_factory,
        "restore",
        resume_phase=MONITOR_PR_PROTECTED_PUSH_RESUME_PHASE,
        directive="revert it",
    )
    worker = _worker(session_factory, _RecordingExecutor())
    assert await worker._claim_blocked_for_monitor_resume(blocked_id)  # noqa: SLF001

    await worker._restore_monitor_blocked_resume_claim(  # noqa: SLF001
        blocked_id, reason_code="TEST_ABORT"
    )

    async with session_factory() as s:
        ws = await WorkspaceRepository(s).get(blocked_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.blocked.value
        # The operator decision survives so the next cycle re-resumes it.
        assert ws.pending_operator_hint is not None


@pytest.mark.unit
async def test_run_once_routes_post_pr_block_to_monitor_resume(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    blocked_id = await _create_blocked(
        session_factory,
        "routed",
        resume_phase=MONITOR_PR_PROTECTED_PUSH_RESUME_PHASE,
        directive="revert it",
    )
    executor = _RecordingExecutor()
    worker = _worker(session_factory, executor)

    dispatched = await asyncio.wait_for(worker.run_once(), timeout=WORKER_TEST_TIMEOUT_SECONDS)
    await asyncio.wait_for(worker.wait_for_execution_tasks(), timeout=WORKER_TEST_TIMEOUT_SECONDS)

    assert dispatched == 1
    # Routed to the monitor resume path, NOT the executor blocked-resume path.
    assert executor.resume_pr_monitor_calls == [blocked_id]
    assert executor.resume_blocked_calls == []
