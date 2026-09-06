"""Merge-arm terminal ownership: workspace writes fenced on the terminate sink.

The ``Merge`` success path in ``handle_merge_action`` is the third terminal sink
alongside ``loop._execute``'s ``ShortCircuitCompleted`` arm and
``loop_helpers._finish_cycle_for_terminal_pr``. All three must publish their
workspace-scoped writes (the "monitor is done" defer signal and, via ``run()``'s
post-``_execute`` persist, the monitor state) only behind
``_terminate_completed``'s owner fence.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import Merge, MonitorState
from tests.postgres import postgres_test_engine
from tests.unit.runtime._merge_methods_fixtures import (
    _TEST_DEFAULT_BASE_BRANCH,
    _TEST_PR_NUMBER,
    _TEST_REPO,
    _mergeable_status,
    _MergeMethodClient,
)
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _merge_client() -> _MergeMethodClient:
    gh = _MergeMethodClient(
        repo_methods=("merge", "squash"),
        branch_methods=("merge", "squash"),
        merge_results=["MERGESHA123"],
    )
    gh.expect_context(
        repo=_TEST_REPO,
        pr_number=_TEST_PR_NUMBER,
        base_branch=_TEST_DEFAULT_BASE_BRANCH,
    )
    return gh


def _stale_state() -> MonitorState:
    """In-memory monitor state a superseded runner must not flush."""
    state = MonitorState()
    state.mark_addressed("t-stale", "fix_committed")
    state.last_push_sha = "stalesha0000"
    return state


async def _run_merge_arm(
    *,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    workspace_id: str,
    gh: _MergeMethodClient,
    state: MonitorState,
    monitor_owner_id: str | None,
    artifacts_root: Path,
) -> bool:
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        artifacts_root=artifacts_root,
        gh=gh,
        pre_merge_settle_seconds=0,
        initial_review_grace_period_seconds=0,
    )
    runner._monitor_owner_id = monitor_owner_id
    return await runner._execute(
        action=Merge(),
        workspace_id=workspace_id,
        repo_url=f"git@github.com:{_TEST_REPO.slug()}.git",
        repo=_TEST_REPO,
        pr_number=_TEST_PR_NUMBER,
        status=_mergeable_status(),
        state=state,
        base_branch=_TEST_DEFAULT_BASE_BRANCH,
        remote_branch=f"awf/{workspace_id}",
        remote_push_url=f"git@github.com:{_TEST_REPO.slug()}.git",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )


@pytest.mark.unit
async def test_merge_arm_skips_workspace_writes_for_a_superseded_owner(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The ``Merge`` success arm fences its workspace writes on the terminate sink.

    Regression for PRRT_kwDOSJAM6s6fvGsq. The arm used to publish the defer signal
    BEFORE ``_terminate_completed`` and then discard the sink's boolean, so a
    runner whose monitor lease had been reclaimed mid-merge left a workspace-scoped
    "monitor is done" artifact and — via ``run()``'s unconditional
    post-``_execute`` persist — flushed its stale ``monitor_threads_addressed`` /
    ``monitor_last_commit_sha`` onto the live claimant's still-``monitoring_pr``
    row. The merge itself still happens (the PR is genuinely merged); only the
    workspace-scoped writes belong to the new owner, which re-derives the
    completion from the merged PR on its next poll.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    artifacts_root = tmp_path / "artifacts"
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.monitor_claimed_by = "worker-current"
        workspace.monitor_last_commit_sha = "livesha00000"
        await session.commit()
    gh = _merge_client()
    state = _stale_state()

    terminal = await _run_merge_arm(
        factory=factory,
        tmp_path=tmp_path,
        workspace_id=workspace_id,
        gh=gh,
        state=state,
        monitor_owner_id="worker-stale",  # lease lost to worker-current
        artifacts_root=artifacts_root,
    )

    # The cycle still ends for THIS runner; only its workspace writes are dropped.
    assert terminal is True
    assert gh.merge_calls == ["squash"]
    assert state.monitor_writes_suppressed is True
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
    assert workspace is not None
    assert workspace.status == WorkspaceStatus.monitoring_pr.value
    assert workspace.monitor_last_commit_sha == "livesha00000"
    assert not (artifacts_root / f"{workspace_id}.defer-signal.json").exists()


@pytest.mark.unit
async def test_merge_arm_publishes_terminal_writes_for_the_owning_runner(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The owning runner still completes and publishes its terminal artifact."""
    workspace_id = await seed_monitoring_workspace(factory)
    artifacts_root = tmp_path / "artifacts"
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.monitor_claimed_by = "worker-current"
        await session.commit()
    gh = _merge_client()
    state = MonitorState()

    terminal = await _run_merge_arm(
        factory=factory,
        tmp_path=tmp_path,
        workspace_id=workspace_id,
        gh=gh,
        state=state,
        monitor_owner_id="worker-current",
        artifacts_root=artifacts_root,
    )

    assert terminal is True
    assert gh.merge_calls == ["squash"]
    assert state.monitor_writes_suppressed is False
    signal = json.loads((artifacts_root / f"{workspace_id}.defer-signal.json").read_text())
    assert signal["terminal_action"] == "Merge"
    assert signal["merged"] is True
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
    assert workspace is not None
    assert workspace.status == WorkspaceStatus.completed.value
    assert workspace.pr_merge_sha == "MERGESHA123"
