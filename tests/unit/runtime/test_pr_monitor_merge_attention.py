"""Regression tests for PR monitor merge-block attention-marker preservation.

Covers the ``#661`` / ``#663`` / ``PRRT_kwDOSJAM6s6La_SZ`` /
``PRRT_kwDOSJAM6s6LcfXk`` contracts: a resolved ``NotifyHuman`` attention flag
must be cleared before the pre-merge settle / merge attempt, and a
``merge_block_attention`` marker that is FRESH at merge-coordinator entry must
survive a serialized-merge wait longer than the marker TTL (the wait is not a
poll, so the branch-protection fallback cannot re-stamp during it).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import GitHubClient, GitHubClientError
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    Merge,
    MonitorConfig,
    MonitorState,
)
from awf.runtime.pr_monitor_runner.config import MonitorRunnerConfig
from awf.runtime.pr_monitor_runner.runner import PullRequestMonitorRunner
from awf.service.merge_queue import MergeQueueBlocker
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
    pr_payload,
    seed_monitoring_workspace,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Provide an isolated async session factory for merge-attention tests."""
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


class _AttentionCheckingSleep(RecordedSleep):
    """Records sleeps and asserts ``awaiting_human_since`` is cleared mid-sleep.

    Used by the #661 tests to prove the resolved ``NotifyHuman`` attention flag is
    cleared BEFORE the pre-merge settle sleep / fast-path merge attempt, not
    only after the whole poll resolves.
    """

    def __init__(
        self,
        *,
        factory: async_sessionmaker[AsyncSession],
        workspace_id: str,
    ) -> None:
        super().__init__()
        self._factory = factory
        self._workspace_id = workspace_id
        self.cleared_before_sleep: list[bool] = []

    async def __call__(self, seconds: float) -> None:
        async with self._factory() as session:
            ws = await WorkspaceRepository(session).get(self._workspace_id)
            assert ws is not None
            self.cleared_before_sleep.append(ws.awaiting_human_since is None)
        await super().__call__(seconds)


@pytest.mark.unit
async def test_resolved_human_wait_clears_attention_before_pre_merge_settle(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """#661: a resolved ``HUMAN_WAIT`` episode must not keep surfacing
    "awaiting human" while the monitor settles before merging.

    ``decide()`` returns ``Merge`` after the human block resolves, so the
    top-of-``_execute`` clear is skipped for the ``Merge`` arm. The merge loop
    must clear the stale flag at critical-section entry — BEFORE the pre-merge
    settle sleep — so console KPIs/badges do not show "awaiting human" for the
    ~90s settle window while the monitor is merely waiting to merge.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0)  # git fetch origin development
    cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
    cmd.queue_result(returncode=0, stdout=pr_payload(check_state="PENDING"))
    sleep_fn = _AttentionCheckingSleep(factory=factory, workspace_id=workspace_id)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        initial_review_grace_period_seconds=0,
        pre_merge_settle_seconds=5,
    )

    # Seed a stable episode start from an earlier, now-resolved NotifyHuman poll.
    episode_start = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    async with factory() as session:
        await WorkspaceRepository(session).set_workspace_attention(
            workspace_id, reason="prior human escalation", now=episode_start
        )
        await session.commit()

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

    # The recheck after settle returned PENDING → WaitForCI, no merge attempted.
    assert terminal is False
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        # The flag was cleared at critical-section entry, before the settle sleep.
        assert ws.awaiting_human_since is None
        assert ws.awaiting_human_reason is None
    # The settle sleep (the first recorded sleep) observed the flag already clear.
    assert sleep_fn.cleared_before_sleep
    assert all(sleep_fn.cleared_before_sleep)


@pytest.mark.unit
async def test_resolved_human_wait_clears_attention_on_fast_path_into_merge(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """#661 fast path: with ``pre_merge_settle_seconds == 0`` the critical-section
    entry clear runs right before the merge attempt, so the resolved
    ``NotifyHuman`` flag is cleared and the merge proceeds without surfacing
    "awaiting human" while actively merging.
    """
    workspace_id = await seed_monitoring_workspace(factory)
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
    sleep_fn = RecordedSleep()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
        initial_review_grace_period_seconds=0,
        pre_merge_settle_seconds=0,
    )

    # Seed a stable episode start from an earlier, now-resolved NotifyHuman poll.
    episode_start = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    async with factory() as session:
        await WorkspaceRepository(session).set_workspace_attention(
            workspace_id, reason="prior human escalation", now=episode_start
        )
        await session.commit()

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

    # The merge succeeded.
    assert terminal is True
    assert gh.merge_calls  # a merge was attempted
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        # The flag was cleared at critical-section entry before the merge attempt.
        assert ws.awaiting_human_since is None
        assert ws.awaiting_human_reason is None


class _LongWaitMergeCoordinator:
    """A merge coordinator that actually sleeps before yielding, advancing the
    wall-clock past a tiny TTL so a marker stamped fresh at entry ages out
    during the serialized wait.

    Models the real Postgres/InProcess coordinators blocking behind another
    merge in the same repo/base lane: no branch-protection fallback fires
    during that wait (no poll runs), so the marker is NOT re-stamped. Used to
    reproduce the flicker described in PRRT_kwDOSJAM6s6La_SZ.
    """

    def __init__(self, wait_seconds: float) -> None:
        self._wait_seconds = wait_seconds
        self.entries: list[tuple[str, str]] = []

    @asynccontextmanager
    async def serialized_merge(
        self,
        *,
        repo_url: str,
        base_branch: str,
    ) -> AsyncIterator[None]:
        self.entries.append((repo_url, base_branch))
        # Real sleep so datetime.now(UTC) advances past the TTL inside the wait.
        await asyncio.sleep(self._wait_seconds)
        yield


@pytest.mark.unit
async def test_long_merge_coordinator_wait_preserves_fresh_at_entry_attention(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6La_SZ: a serialized-merge wait longer than the merge-block
    TTL must NOT clear a ``merge_block_attention`` marker that was FRESH at
    coordinator entry.

    The branch-protection fallback re-stamps the marker every poll while
    blocked, but the merge coordinator can block behind another merge for
    longer than the TTL without any poll firing (no fallback runs during the
    wait). Before the fix, ``_clear_stale_merge_attention`` measured the
    marker's age against the post-wait wall-clock, so a marker fresh at entry
    aged past the TTL during the wait and was misclassified as RESOLVED —
    clearing ``awaiting_human_since`` and then letting the deterministic
    rejection re-stamp it, flickering/restarting the human-wait timer though
    the operator block never resolved.

    The fix measures marker age against the coordinator-ENTRY timestamp, so a
    marker fresh when the wait started is preserved across the wait; a marker
    already stale at entry is still cleared (block resolved before the wait).
    """
    workspace_id = await seed_monitoring_workspace(factory)
    sleep_fn = RecordedSleep()
    gh = _MergeMethodClient(
        repo_methods=("merge", "squash"),
        branch_methods=("merge", "squash"),
        # Deterministic branch-protection rejection: decide() stays on Merge and
        # the fallback re-sets attention + re-stamps the marker this poll.
        merge_results=[
            GitHubClientError(
                operation="gh pr merge",
                returncode=1,
                stderr="GraphQL: Pull request could not be merged with this method.",
            ),
        ],
    )
    gh.expect_context(
        repo=_TEST_REPO,
        pr_number=_TEST_PR_NUMBER,
        base_branch=_TEST_DEFAULT_BASE_BRANCH,
    )
    # TTL large enough that the marker stays FRESH at coordinator entry
    # regardless of how long the pre-coordinator setup (DB loads, gate checks)
    # takes inside ``_execute`` — the production entry-time fix measures the
    # marker's age against the coordinator-ENTRY clock, so the marker only has
    # to be fresh *then*, not survive the whole setup gap. A 1.0s TTL is too
    # tight under CI load (the setup gap can exceed it, making the marker stale
    # at entry and clearing ``awaiting_human_since`` — a flaky false failure,
    # not the regression under test). 30s absorbs any plausible setup gap while
    # still exercising the long-wait path; the during-wait aging only mattered
    # for the pre-fix post-wait clock behavior, which is already fixed
    # (PRRT_kwDOSJAM6s6La_SZ).
    monitor_config = MonitorConfig(
        auto_merge=True,
        poll_interval_seconds=60,
        pre_merge_settle_seconds=0,
        initial_review_grace_period_seconds=0,
        non_check_reviewer_settle_seconds=0,
        non_check_reviewer_logins=(),
        merge_block_attention_ttl_seconds=30.0,
    )
    coordinator = _LongWaitMergeCoordinator(wait_seconds=1.5)
    runner = PullRequestMonitorRunner(
        session_factory=factory,
        runner=FakeCommandRunner(),
        adapter=FakeAdapter(),
        gh=gh,
        monitor_config=monitor_config,
        runner_config=MonitorRunnerConfig(
            max_outer_iterations=20,
            max_fix_cycle_passes=3,
            pre_push_validation_fix_passes=1,
        ),
        sleep=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        merge_coordinator=coordinator,
    )

    # Seed a stable episode start from an earlier poll (COALESCE'd start). The
    # prior poll's branch-protection fallback stamped attention + a fresh
    # marker; this poll re-enters the merge loop still blocked.
    episode_start = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    async with factory() as session:
        await WorkspaceRepository(session).set_workspace_attention(
            workspace_id, reason="prior escalation", now=episode_start
        )
        await session.commit()
    state = MonitorState()
    # Stamp the marker FRESH at this poll's entry — still-blocked. The
    # coordinator wait (1.5s) is shorter than the TTL (30s), so the entry-time
    # fix preserves the marker across the wait; without that fix the
    # critical-section-entry clear would measure age against the post-wait
    # clock and (with a tight TTL) wipe the signal.
    state.mark_merge_block_attention()

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
    assert coordinator.entries == [
        (f"git@github.com:{_TEST_REPO.slug()}.git", _TEST_DEFAULT_BASE_BRANCH)
    ]
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
    # The still-active branch-protection signal is PRESERVED across the long
    # coordinator wait: the episode start is NOT reset (no flicker/restart).
    assert ws.awaiting_human_since == episode_start
    assert ws.awaiting_human_reason is not None
    assert "GitHub rejected the merge attempt" in ws.awaiting_human_reason


@pytest.mark.unit
async def test_stale_at_coordinator_entry_marker_still_cleared_after_long_wait(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6La_SZ parity: a marker that was ALREADY stale at
    coordinator entry (the block resolved BEFORE the wait) is still cleared
    after a long merge-coordinator wait.

    The entry-time reference only preserves a marker that was FRESH at entry;
    a marker already stale at entry means no fallback has fired recently (the
    block resolved before this poll), so the clear must still proceed after
    the wait so "awaiting human" does not stay up while only non-human gates
    remain (#663 contract intact).
    """
    workspace_id = await seed_monitoring_workspace(factory)
    sleep_fn = RecordedSleep()
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
    monitor_config = MonitorConfig(
        auto_merge=True,
        poll_interval_seconds=60,
        pre_merge_settle_seconds=0,
        initial_review_grace_period_seconds=0,
        non_check_reviewer_settle_seconds=0,
        non_check_reviewer_logins=(),
        merge_block_attention_ttl_seconds=1.0,
    )
    coordinator = _LongWaitMergeCoordinator(wait_seconds=1.5)
    runner = PullRequestMonitorRunner(
        session_factory=factory,
        runner=FakeCommandRunner(),
        adapter=FakeAdapter(),
        gh=gh,
        monitor_config=monitor_config,
        runner_config=MonitorRunnerConfig(
            max_outer_iterations=20,
            max_fix_cycle_passes=3,
            pre_push_validation_fix_passes=1,
        ),
        sleep=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        merge_coordinator=coordinator,
    )

    # Seed the surfaced attention from a prior, now-resolved poll. The marker is
    # STALE at entry (no fallback has fired since well before the TTL).
    episode_start = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    async with factory() as session:
        await WorkspaceRepository(session).set_workspace_attention(
            workspace_id, reason="prior escalation", now=episode_start
        )
        await session.commit()
    state = MonitorState()
    # Stamp the marker well outside the TTL → stale (resolved) at entry.
    state.mark_merge_block_attention(now=datetime(2025, 12, 31, 0, 0, tzinfo=UTC))

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
    assert coordinator.entries == [
        (f"git@github.com:{_TEST_REPO.slug()}.git", _TEST_DEFAULT_BASE_BRANCH)
    ]
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
    # The stale-at-entry marker was cleared after the wait: the resolved
    # episode does not stay "awaiting human" once the monitor is merging.
    assert ws.awaiting_human_since is None
    assert ws.awaiting_human_reason is None


class _QueueAfterLockRunner(PullRequestMonitorRunner):
    """Return no blockers pre-lock, then a blocker on the post-lock call.

    Models the real merge coordinator blocking behind another merge in the
    same repo/base lane: the pre-lock queue is clear, but by the time the
    serialized coordinator yields an older candidate has claimed the lane so
    the post-lock recheck reports a queue blocker. Used to reproduce the
    post-lock ``_clear_stale_merge_attention`` regression (PRRT_kwDOSJAM6s6LcfXk).
    """

    def __init__(self, *, blocker: MergeQueueBlocker, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._blocker = blocker
        self.blocker_calls = 0

    async def _merge_queue_blockers_for_workspace(
        self,
        workspace_id: str,
    ) -> list[MergeQueueBlocker]:
        assert workspace_id
        self.blocker_calls += 1
        return [] if self.blocker_calls == 1 else [self._blocker]


@pytest.mark.unit
async def test_long_coordinator_wait_preserves_fresh_at_entry_attention_across_post_lock_queue_wait(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6LcfXk: a serialized-merge wait longer than the merge-block
    TTL must NOT clear a ``merge_block_attention`` marker that was FRESH at
    coordinator entry when a post-lock queue blocker parks the monitor on a
    non-human gate wait.

    The critical-section-entry clear re-stamps a fresh marker to the entry
    timestamp. The serialized merge coordinator can block behind another merge
    for longer than the TTL with no branch-protection fallback firing (no poll
    runs during the wait). Before the fix, the post-lock queue-blocker clear
    measured the marker's age against the post-wait wall-clock (``now=None``),
    so a marker fresh at entry aged past the TTL during the wait and was
    misclassified as RESOLVED — clearing ``awaiting_human_since`` even though
    the branch-protection block was still active. On the next poll the
    fallback re-stamps via COALESCE, restarting the human-wait timer though
    the operator block never resolved.

    The fix passes the coordinator-ENTRY timestamp to the post-lock clears too,
    so a marker fresh when the wait started stays fresh across the post-lock
    queue wait; the attention signal is preserved.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    sleep_fn = RecordedSleep()
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
    blocker = MergeQueueBlocker(
        candidate_id="mc_after_lock",
        workspace_id="ws_older",
        attempt_id="attempt_older",
        task_id="task_older",
        title="Older candidate",
        pr_url="https://github.com/example-org/example-repo/pull/41",
        pr_number=41,
        status="open",
        blocker_state="ready",
    )
    # TTL small enough that the 1.5s coordinator wait exceeds it, so a
    # post-wait wall-clock measurement would reclassify a fresh-at-entry
    # marker as stale. The entry-time reference preserves it instead.
    monitor_config = MonitorConfig(
        auto_merge=True,
        poll_interval_seconds=60,
        pre_merge_settle_seconds=0,
        initial_review_grace_period_seconds=0,
        non_check_reviewer_settle_seconds=0,
        non_check_reviewer_logins=(),
        merge_block_attention_ttl_seconds=1.0,
    )
    coordinator = _LongWaitMergeCoordinator(wait_seconds=1.5)
    runner = _QueueAfterLockRunner(
        blocker=blocker,
        session_factory=factory,
        runner=FakeCommandRunner(),
        adapter=FakeAdapter(),
        gh=GitHubClient(FakeCommandRunner()),
        monitor_config=monitor_config,
        runner_config=MonitorRunnerConfig(
            max_outer_iterations=20,
            max_fix_cycle_passes=3,
            pre_push_validation_fix_passes=1,
        ),
        sleep=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        merge_coordinator=coordinator,
    )

    # Seed a stable episode start from an earlier poll's branch-protection
    # fallback (COALESCE'd start). This poll re-enters the merge loop still
    # blocked, so the marker is fresh at entry.
    episode_start = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    async with factory() as session:
        await WorkspaceRepository(session).set_workspace_attention(
            workspace_id, reason="prior escalation", now=episode_start
        )
        await session.commit()
    state = MonitorState()
    # Stamp the marker FRESH at this poll's entry — still-blocked. The
    # coordinator wait (1.5s) exceeds the TTL (1.0s), so without the entry-time
    # reference the post-lock queue clear would measure the marker against the
    # post-wait wall-clock, age it past the TTL, and clear the signal.
    state.mark_merge_block_attention()

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

    # The post-lock queue blocker parked the monitor on a non-human wait.
    assert terminal is False
    assert runner.blocker_calls == 2
    assert coordinator.entries == [
        (f"git@github.com:{_TEST_REPO.slug()}.git", _TEST_DEFAULT_BASE_BRANCH)
    ]
    # The merge attempt was skipped because the post-lock queue blocker was
    # present before the merge-method preflight/attempt ran.
    assert gh.merge_calls == []
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
    # The still-active branch-protection signal is PRESERVED across the long
    # coordinator wait AND the post-lock queue wait: the episode start is NOT
    # reset (no flicker/restart of the human-wait timer).
    assert ws.awaiting_human_since == episode_start
    assert ws.awaiting_human_reason is not None
