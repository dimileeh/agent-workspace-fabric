"""Tests for the executor's ``enter_blocked_for_protected_violation`` helper.

The helper is the single epoch-guarded clean exit used at every pre-PR
protected-gate fail site. It must: pause into ``blocked`` while preserving the
execution claim, record the violation durably, bump ``block_epoch`` on every
entry, and refuse to clobber a row a newer claimant already holds (CAS 0-rows).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.control.executor import ExecutorConfig, WorkspaceExecutor
from awf.control.quality_gates import QualityGateViolation
from awf.db.enums import WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from tests.postgres import postgres_test_engine

_VIOLATIONS = [
    QualityGateViolation(
        path="pyproject.toml",
        protected_pattern="pyproject.toml",
        section="tool.coverage.report.fail_under",
        line=5,
        reason="coverage fail_under lowered from 99 to 80",
    )
]


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _executor(factory: async_sessionmaker[AsyncSession], tmp_path: Path) -> WorkspaceExecutor:
    return WorkspaceExecutor(
        session_factory=factory,
        runner=FakeCommandRunner(),
        compose=object(),  # type: ignore[arg-type]
        validation=object(),  # type: ignore[arg-type]
        pr_creator=object(),  # type: ignore[arg-type]
        config=ExecutorConfig(
            worktrees_root=tmp_path / "worktrees",
            compose_projects_root=tmp_path / "compose",
        ),
    )


async def _seed_running(
    factory: async_sessionmaker[AsyncSession],
    *,
    owner: str | None = "worker-a",
) -> str:
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="git@github.com:example/blocked.git",
            branch_base="main",
            task_title="blocked helper test",
            task_prompt="exercise the block helper",
            agent="codex",
            test_commands=[],
        )
        ws.status = WorkspaceStatus.running.value
        ws.execution_claimed_by = owner
        await session.commit()
        return ws.id


async def _get(factory: async_sessionmaker[AsyncSession], ws_id: str) -> Workspace:
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(ws_id)
    assert ws is not None
    return ws


@pytest.mark.unit
async def test_enter_blocked_pauses_and_records_violation(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    ws_id = await _seed_running(factory)
    executor = _executor(factory, tmp_path)

    paused = await executor.enter_blocked_for_protected_violation(
        workspace_id=ws_id,
        from_status=WorkspaceStatus.running,
        violations=_VIOLATIONS,
        resume_phase="validation_fix_cycle",
    )
    assert paused is True

    ws = await _get(factory, ws_id)
    assert ws.status == WorkspaceStatus.blocked.value
    assert ws.block_reason_code == "QUALITY_GATE_POLICY_CHANGED"
    assert ws.block_type == "protected_quality_gate"
    assert ws.block_resume_phase == "validation_fix_cycle"
    assert ws.block_epoch == 1
    assert ws.blocked_at is not None
    assert ws.block_violations[0]["path"] == "pyproject.toml"
    # The warm-stack execution claim is preserved through the block.
    assert ws.execution_claimed_by == "worker-a"


@pytest.mark.unit
async def test_reblock_bumps_block_epoch(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    ws_id = await _seed_running(factory)
    executor = _executor(factory, tmp_path)

    assert await executor.enter_blocked_for_protected_violation(
        workspace_id=ws_id,
        from_status=WorkspaceStatus.running,
        violations=_VIOLATIONS,
        resume_phase="validation_fix_cycle",
    )
    # Resume the workspace (blocked -> running) the way the worker would.
    async with factory() as session:
        await WorkspaceRepository(session).transition_if_current(
            ws_id,
            from_status=WorkspaceStatus.blocked,
            to=WorkspaceStatus.running,
            reason_code="TEST_RESUME",
        )
        await session.commit()

    assert await executor.enter_blocked_for_protected_violation(
        workspace_id=ws_id,
        from_status=WorkspaceStatus.running,
        violations=_VIOLATIONS,
        resume_phase="validation_fix_cycle",
    )
    ws = await _get(factory, ws_id)
    # A re-block bumps block_epoch, invalidating any prior operator grants.
    assert ws.block_epoch == 2


@pytest.mark.unit
async def test_late_mark_failed_does_not_clobber_blocked(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    ws_id = await _seed_running(factory)
    executor = _executor(factory, tmp_path)
    assert await executor.enter_blocked_for_protected_violation(
        workspace_id=ws_id,
        from_status=WorkspaceStatus.running,
        violations=_VIOLATIONS,
        resume_phase="post_agent_commit",
    )
    # A stale executor's late error path tries to fail from the phase it last
    # knew. The from_status CAS finds the row in ``blocked`` (not running), so it
    # is a no-op: ``blocked -> failed`` is never clobbered behind the operator.
    from awf.db.enums import FailureReason

    await executor._mark_failed(
        workspace_id=ws_id,
        from_status=WorkspaceStatus.running,
        failure_reason=FailureReason.infrastructure_failure,
        message="late error after block",
    )
    ws = await _get(factory, ws_id)
    assert ws.status == WorkspaceStatus.blocked.value


@pytest.mark.unit
async def test_enter_blocked_no_ops_on_status_mismatch(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    ws_id = await _seed_running(factory)
    executor = _executor(factory, tmp_path)
    # Row is ``running``; a stale executor believing it is ``validating`` loses
    # the CAS and must not clobber the row.
    paused = await executor.enter_blocked_for_protected_violation(
        workspace_id=ws_id,
        from_status=WorkspaceStatus.validating,
        violations=_VIOLATIONS,
        resume_phase="validation_fix_cycle",
    )
    assert paused is False
    ws = await _get(factory, ws_id)
    assert ws.status == WorkspaceStatus.running.value
    assert ws.block_epoch == 0


@pytest.mark.unit
async def test_enter_blocked_owner_fence_rejects_stale_worker(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    ws_id = await _seed_running(factory, owner="worker-a")
    executor = _executor(factory, tmp_path)
    # A stale worker (different owner) is fenced: 0 rows, no block.
    paused = await executor.enter_blocked_for_protected_violation(
        workspace_id=ws_id,
        from_status=WorkspaceStatus.running,
        violations=_VIOLATIONS,
        resume_phase="post_agent_commit",
        execution_owner_id="worker-b",
    )
    assert paused is False
    assert (await _get(factory, ws_id)).status == WorkspaceStatus.running.value

    # The live owner blocks successfully.
    paused = await executor.enter_blocked_for_protected_violation(
        workspace_id=ws_id,
        from_status=WorkspaceStatus.running,
        violations=_VIOLATIONS,
        resume_phase="post_agent_commit",
        execution_owner_id="worker-a",
    )
    assert paused is True
    assert (await _get(factory, ws_id)).status == WorkspaceStatus.blocked.value
