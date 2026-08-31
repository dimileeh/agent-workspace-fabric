"""Zero-settle pre-merge recheck must still fetch and block on late feedback."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    Merge,
    MonitorState,
    ReviewThread,
    _mark_review_thread_addressed,
)
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


@pytest.mark.unit
async def test_zero_settle_pre_merge_recheck_blocks_on_late_outdated_feedback(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """M6: even with ``pre_merge_settle_seconds == 0``, the merge critical section
    must fetch fresh forge status. Late never-addressed outdated feedback must
    prevent the merge attempt and release the lock."""
    workspace_id = await seed_monitoring_workspace(factory)
    late_outdated = ReviewThread(
        thread_id="T_late",
        path="src/x.py",
        line=1,
        body_excerpt="never addressed",
        author="reviewer",
        is_outdated=True,
    )
    recheck = replace(
        _mergeable_status(),
        outdated_unresolved_inline_threads=(late_outdated,),
    )
    gh = _MergeMethodClient(
        repo_methods=("merge", "squash"),
        branch_methods=("merge", "squash"),
        merge_results=["MERGESHA123"],
        recheck_status=recheck,
    )
    gh.expect_context(
        repo=_TEST_REPO,
        pr_number=_TEST_PR_NUMBER,
        base_branch=_TEST_DEFAULT_BASE_BRANCH,
    )
    adapter = FakeAdapter()
    # Recheck dispatches AddressComments after releasing the merge lock.
    adapter.queue(stdout="AWF-VERDICT: NEEDS_HUMAN: late outdated feedback")
    cmd = FakeCommandRunner()
    # Fix-cycle settle re-poll after AddressComments triage.
    cmd.queue_result(returncode=0, stdout="")  # may be unused depending on path
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
        pre_merge_settle_seconds=0,
        initial_review_grace_period_seconds=0,
    )

    terminal = await runner._execute(
        action=Merge(),
        workspace_id=workspace_id,
        repo_url=f"git@github.com:{_TEST_REPO.slug()}.git",
        repo=_TEST_REPO,
        pr_number=_TEST_PR_NUMBER,
        status=_mergeable_status(),
        state=MonitorState(),
        base_branch=_TEST_DEFAULT_BASE_BRANCH,
        remote_branch=f"awf/{workspace_id}",
        remote_push_url=f"git@github.com:{_TEST_REPO.slug()}.git",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is False
    assert gh.fetch_pr_status_calls >= 1
    assert gh.merge_calls == []
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
    assert workspace is not None
    assert workspace.status == WorkspaceStatus.monitoring_pr.value


@pytest.mark.unit
async def test_pre_merge_recheck_resolves_late_outdated_addressed_thread(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6dcrzo: when an already-addressed thread appears in the
    fresh pre-merge ``outdated_unresolved_inline_threads`` snapshot only (forge
    flipped it after the outer poll), run outdated hygiene before ``decide`` so
    ``resolve_thread`` lands before merge accepts the recheck."""
    workspace_id = await seed_monitoring_workspace(factory)
    late_addressed = ReviewThread(
        thread_id="T_late_addressed",
        path="src/x.py",
        line=1,
        body_excerpt="already fixed elsewhere",
        author="reviewer",
        is_outdated=True,
    )
    state = MonitorState()
    _mark_review_thread_addressed(state, late_addressed, "fix_committed")
    recheck = replace(
        _mergeable_status(),
        outdated_unresolved_inline_threads=(late_addressed,),
    )
    gh = _MergeMethodClient(
        repo_methods=("merge", "squash"),
        branch_methods=("merge", "squash"),
        merge_results=["MERGESHA123"],
        recheck_status=recheck,
    )
    gh.expect_context(
        repo=_TEST_REPO,
        pr_number=_TEST_PR_NUMBER,
        base_branch=_TEST_DEFAULT_BASE_BRANCH,
    )
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
        pre_merge_settle_seconds=0,
        initial_review_grace_period_seconds=0,
    )

    terminal = await runner._execute(
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

    assert terminal is True
    assert gh.fetch_pr_status_calls >= 1
    assert gh.resolved_threads == ["T_late_addressed"]
    assert gh.merge_calls == ["squash"]
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
    assert workspace is not None
    assert workspace.status == WorkspaceStatus.completed.value


@pytest.mark.unit
async def test_pre_merge_outdated_resolve_transient_backs_off_outside_lock(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6dfBzK: transient resolve under pre-merge hygiene must not
    sleep while holding the merge lock. Record the requeue blocker in-lock,
    backoff after release, and do not merge."""
    from awf.common.github_client import GitHubClientError
    from awf.runtime.monitor_state_keys import _outdated_resolve_requeued_key

    workspace_id = await seed_monitoring_workspace(factory)
    late_addressed = ReviewThread(
        thread_id="T_late_transient",
        path="src/x.py",
        line=1,
        body_excerpt="already fixed elsewhere",
        author="reviewer",
        is_outdated=True,
    )
    state = MonitorState()
    _mark_review_thread_addressed(state, late_addressed, "fix_committed")
    recheck = replace(
        _mergeable_status(),
        outdated_unresolved_inline_threads=(late_addressed,),
    )
    gh = _MergeMethodClient(
        repo_methods=("merge", "squash"),
        branch_methods=("merge", "squash"),
        merge_results=["MERGESHA123"],
        recheck_status=recheck,
        resolve_error=GitHubClientError(
            operation="resolve_thread",
            returncode=1,
            stderr="HTTP 503: service unavailable",
        ),
    )
    gh.expect_context(
        repo=_TEST_REPO,
        pr_number=_TEST_PR_NUMBER,
        base_branch=_TEST_DEFAULT_BASE_BRANCH,
    )
    sleep_fn = RecordedSleep()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
        pre_merge_settle_seconds=0,
        initial_review_grace_period_seconds=0,
    )

    terminal = await runner._execute(
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

    assert terminal is False
    assert gh.resolved_threads == ["T_late_transient"]
    assert gh.merge_calls == []
    assert state.threads_addressed_ids[_outdated_resolve_requeued_key("T_late_transient")] == (
        "requeued"
    )
    assert sleep_fn.calls  # backoff after the merge lock released
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
    assert workspace is not None
    assert workspace.status == WorkspaceStatus.monitoring_pr.value
