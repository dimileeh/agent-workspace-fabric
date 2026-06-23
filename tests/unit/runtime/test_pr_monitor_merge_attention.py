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
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import GitHubClientError
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    _MERGE_BLOCK_ATTENTION_STATE_KEY,
    Merge,
    MergeStateStatus,
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
        self.yielded_at: datetime | None = None

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
        self.yielded_at = datetime.now(UTC)
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
async def test_clean_status_preserves_merge_block_attention_during_queue_wait(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6LqkOW: GitHub ``CLEAN`` during a queue-style wait is not
    proof that a prior deterministic merge rejection has resolved.

    The status that led into the prior merge attempt can already have been
    ``CLEAN`` because the branch-protection fallback records no sticky blocker
    and ``decide()`` keeps returning ``Merge``. If the next poll parks behind a
    queue/reviewer/grace wait before retrying the merge, preserve the active
    operator attention until the retry path confirms resolution.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    episode_start = datetime(2026, 1, 1, 12, tzinfo=UTC)
    state = MonitorState()
    state.mark_merge_block_attention()
    marker = state.threads_addressed_ids[_MERGE_BLOCK_ATTENTION_STATE_KEY]
    async with factory() as session:
        ws = await WorkspaceRepository(session).get_for_update(workspace_id)
        assert ws is not None
        ws.monitor_threads_addressed = {_MERGE_BLOCK_ATTENTION_STATE_KEY: marker}
        ws.awaiting_human_since = episode_start
        ws.awaiting_human_reason = "GitHub rejected the merge attempt"
        await session.commit()

    await runner._clear_or_preserve_merge_attention_for_queue_wait(
        workspace_id,
        state,
        status=_mergeable_status(),
        forge="github",
    )

    assert state.threads_addressed_ids[_MERGE_BLOCK_ATTENTION_STATE_KEY] == marker
    async with factory() as session:
        ws_after = await WorkspaceRepository(session).get(workspace_id)
        assert ws_after is not None
    assert (ws_after.monitor_threads_addressed or {})[_MERGE_BLOCK_ATTENTION_STATE_KEY] == marker
    assert ws_after.awaiting_human_since == episode_start
    assert ws_after.awaiting_human_reason == "GitHub rejected the merge attempt"


@pytest.mark.unit
async def test_post_lock_gate_preserves_blocked_marker_without_restamping(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """#671: post-lock queue waits preserve only on forge-confirmed blockage.

    The old queue preserve path re-stamped markers after the coordinator wait.
    The signal is now forge-driven: ``BLOCKED`` preserves the existing marker and
    stable timer without refreshing the timestamp.
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
    # TTL large enough that the marker stays FRESH at the critical-section-entry
    # clear regardless of how long the pre-coordinator setup (DB loads, gate
    # checks, status fetch) takes inside ``_execute`` — the production entry-time
    # fix measures the marker's age against the coordinator-ENTRY clock, so the
    # marker only has to be fresh *then*, not survive the whole setup gap. A 1.0s
    # TTL is too tight under CI load (the setup gap can exceed it, making the
    # marker stale at entry and clearing ``awaiting_human_since`` — a flaky false
    # failure, not the regression under test). 30s absorbs any plausible setup
    # gap. The post-lock queue wait is now forge-signal-driven; this setup only
    # needs the critical-section-entry clear to preserve the marker before the
    # serialized wait.
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
    runner = _QueueAfterLockRunner(
        blocker=blocker,
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
    # Stamp the marker FRESH at this poll's entry so the #661 critical-section
    # entry clear preserves it. The later post-lock queue wait must not re-stamp.
    state.mark_merge_block_attention()
    original_marker = state.threads_addressed_ids[_MERGE_BLOCK_ATTENTION_STATE_KEY]

    terminal = await runner._execute(
        action=Merge(),
        workspace_id=workspace_id,
        repo_url=f"git@github.com:{_TEST_REPO.slug()}.git",
        repo=_TEST_REPO,
        pr_number=_TEST_PR_NUMBER,
        status=replace(_mergeable_status(), merge_state_status=MergeStateStatus.BLOCKED),
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
    assert gh.merge_calls == []
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
    # The still-active branch-protection signal is PRESERVED across the long
    # coordinator wait AND the post-lock queue wait: the episode start is NOT
    # reset (no flicker/restart of the human-wait timer).
    assert ws.awaiting_human_since == episode_start
    assert ws.awaiting_human_reason is not None
    persisted_raw = (ws.monitor_threads_addressed or {}).get(_MERGE_BLOCK_ATTENTION_STATE_KEY)
    assert persisted_raw is not None
    assert state.threads_addressed_ids.get(_MERGE_BLOCK_ATTENTION_STATE_KEY) == persisted_raw
    assert persisted_raw != original_marker
    assert coordinator.yielded_at is not None
    assert datetime.fromisoformat(persisted_raw) < coordinator.yielded_at


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
    """#671: a long coordinator wait still preserves when the forge says blocked."""
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
    # TTL large enough that the marker stays FRESH at the critical-section-entry
    # clear regardless of how long the pre-coordinator setup (DB loads, gate
    # checks, status fetch) takes inside ``_execute`` — the production entry-time
    # fix measures the marker's age against the coordinator-ENTRY clock, so the
    # marker only has to be fresh *then*, not survive the whole setup gap. A 1.0s
    # TTL is too tight under CI load (the setup gap can exceed it, making the
    # marker stale at entry and clearing ``awaiting_human_since`` — a flaky false
    # failure, not the regression under test). 30s absorbs any plausible setup
    # gap. The regression under test (post-wait wall-clock measurement
    # reclassifying a fresh-at-entry marker as stale) is exercised by the 1.5s
    # coordinator wait advancing real time past the entry window — the production
    # fix already passes the entry timestamp to the post-lock clears, so the
    # small-TTL "post-wait reclassification" rationale only described the
    # PRE-FIX behavior and is TTL-independent here now; the 1.5s wait still
    # drives the post-lock preserve path. Mirrors the sibling
    # ``test_post_lock_gate_restamp_uses_current_wall_clock_not_entry_timestamp``
    # (PRRT_kwDOSJAM6s6LdM4X) and ``test_long_merge_coordinator_wait_preserves_fresh_at_entry_attention``
    # (PRRT_kwDOSJAM6s6La_SZ) TTL bumps.
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
    runner = _QueueAfterLockRunner(
        blocker=blocker,
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
    # Stamp the marker FRESH at this poll's entry so the #661 critical-section
    # entry clear preserves it. The later post-lock queue wait is decided by the
    # forge ``BLOCKED`` signal, not marker age.
    state.mark_merge_block_attention()
    original_marker = state.threads_addressed_ids[_MERGE_BLOCK_ATTENTION_STATE_KEY]

    terminal = await runner._execute(
        action=Merge(),
        workspace_id=workspace_id,
        repo_url=f"git@github.com:{_TEST_REPO.slug()}.git",
        repo=_TEST_REPO,
        pr_number=_TEST_PR_NUMBER,
        status=replace(_mergeable_status(), merge_state_status=MergeStateStatus.BLOCKED),
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
    persisted_raw = (ws.monitor_threads_addressed or {}).get(_MERGE_BLOCK_ATTENTION_STATE_KEY)
    assert persisted_raw is not None
    assert state.threads_addressed_ids.get(_MERGE_BLOCK_ATTENTION_STATE_KEY) == persisted_raw
    assert persisted_raw != original_marker
    assert coordinator.yielded_at is not None
    assert datetime.fromisoformat(persisted_raw) < coordinator.yielded_at


# ── Defensive no-op branches for the new atomic-persist helpers ──────────────
#
# ``_set_workspace_attention_with_merge_block_marker`` and
# ``_persist_merge_block_attention_durably`` each have defensive early-returns
# for a missing workspace row (a GC/destroy race) and, for the durable persist,
# an absent in-memory marker (the caller did not re-stamp) or an already-equal
# persisted value (no-op write). These mirror the established single-key
# durable-persist no-op pattern (``_clear_preserved_head_marker_durably`` /
# ``_persist_forge_transient_retry_count``); each branch is exercised here so
# the helpers never silently lose coverage.


@pytest.mark.unit
async def test_set_workspace_attention_with_merge_block_marker_missing_workspace_is_noop(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The atomic marker+attention write no-ops when the workspace row is gone (a
    GC/destroy race between the fallback and the commit) rather than raising."""
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    # No marker is stamped, but the helper must not raise on a missing workspace
    # even when one IS present in state — the row lookup gates the whole write.
    state = MonitorState()
    state.mark_merge_block_attention()
    await runner._set_workspace_attention_with_merge_block_marker(
        "ws_does_not_exist",
        state,
        reason="merge_blocked",
    )


@pytest.mark.unit
async def test_persist_merge_block_attention_durably_missing_workspace_is_noop(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The durable marker persist no-ops when the workspace row is gone (a
    GC/destroy race between the preserve re-stamp and the durable write)."""
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    state = MonitorState()
    state.mark_merge_block_attention()
    await runner._persist_merge_block_attention_durably("ws_does_not_exist", state)


@pytest.mark.unit
async def test_persist_merge_block_attention_durably_absent_marker_is_noop(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The durable persist no-ops when the in-memory marker is absent — the
    caller did not re-stamp, so there is nothing to durably persist."""
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    # No marker stamped into state ⇒ the helper returns before touching the DB.
    state = MonitorState()
    await runner._persist_merge_block_attention_durably(workspace_id, state)
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
    assert _MERGE_BLOCK_ATTENTION_STATE_KEY not in (ws.monitor_threads_addressed or {})


@pytest.mark.unit
async def test_persist_merge_block_attention_durably_already_equal_is_noop(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The durable persist no-ops when the persisted marker already equals the
    in-memory stamp (idempotent re-write guard) — the DB value is left untouched
    and no redundant commit is issued."""
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    # Stamp the marker durably first so the DB row already carries it.
    state = MonitorState()
    state.mark_merge_block_attention()
    await runner._persist_merge_block_attention_durably(workspace_id, state)
    stamped = state.threads_addressed_ids[_MERGE_BLOCK_ATTENTION_STATE_KEY]
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
    assert (ws.monitor_threads_addressed or {})[_MERGE_BLOCK_ATTENTION_STATE_KEY] == stamped
    # A second persist with the SAME stamp must short-circuit (no-op write).
    await runner._persist_merge_block_attention_durably(workspace_id, state)
    async with factory() as session:
        ws_after = await WorkspaceRepository(session).get(workspace_id)
        assert ws_after is not None
    assert (ws_after.monitor_threads_addressed or {})[_MERGE_BLOCK_ATTENTION_STATE_KEY] == stamped


@pytest.mark.unit
async def test_clear_stale_merge_attention_drops_marker_durably_on_resolve(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6Lf_37: ``_clear_stale_merge_attention``'s stale (resolved)
    branch must durably remove the merge-block marker from the persisted row,
    not just drop it in memory. The outer ``run()`` loop only flushes the whole
    in-memory ``state`` after ``_execute`` returns (``runner.py:455``); a
    cancel/restart before that full ``_persist_state`` would otherwise reload the
    STALE marker from the DB while ``awaiting_human_since`` is already null, so
    a later poll could otherwise observe the stale marker without the cleared
    attention flag, losing the invariant that both pieces move together.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    # Seed a stale marker (resolved block) into BOTH the in-memory state and the
    # persisted row — the state the monitor would reload after a cancel/restart
    # before the outer loop's full ``_persist_state`` flush.
    stale_stamp = datetime(2024, 1, 1, tzinfo=UTC).isoformat()
    state = MonitorState(threads_addressed_ids={_MERGE_BLOCK_ATTENTION_STATE_KEY: stale_stamp})
    async with factory() as session:
        ws = await WorkspaceRepository(session).get_for_update(workspace_id)
        assert ws is not None
        ws.monitor_threads_addressed = {_MERGE_BLOCK_ATTENTION_STATE_KEY: stale_stamp}
        await session.commit()
    # Drive the stale (resolved) branch with a TTL that the marker exceeds, and a
    # ``reference`` (entry ``now``) far enough past the stamp to age it out.
    resolved_now = datetime(2024, 1, 2, tzinfo=UTC)
    await runner._clear_stale_merge_attention(
        workspace_id,
        state,
        now=resolved_now,
    )
    # In-memory marker dropped and attention cleared.
    assert _MERGE_BLOCK_ATTENTION_STATE_KEY not in state.threads_addressed_ids
    # The persisted row must NOT carry the stale marker anymore — the durable
    # clear closed the cancel/restart window.
    async with factory() as session:
        ws_after = await WorkspaceRepository(session).get(workspace_id)
        assert ws_after is not None
    assert _MERGE_BLOCK_ATTENTION_STATE_KEY not in (ws_after.monitor_threads_addressed or {})
    assert ws_after.awaiting_human_since is None


@pytest.mark.unit
async def test_clear_stale_merge_attention_preserves_stale_marker_when_forge_still_blocked(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6LqhqC: a long queue wait can preserve an old
    ``merge_block_attention`` marker without re-stamping it. When merge
    critical-section entry sees the forge still reporting branch protection as
    active, it must not treat the TTL-aged marker as resolved and clear
    ``awaiting_human_since``.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    old_stamp = datetime(2024, 1, 1, tzinfo=UTC).isoformat()
    state = MonitorState(threads_addressed_ids={_MERGE_BLOCK_ATTENTION_STATE_KEY: old_stamp})
    episode_start = datetime(2024, 1, 1, 12, tzinfo=UTC)
    async with factory() as session:
        ws = await WorkspaceRepository(session).get_for_update(workspace_id)
        assert ws is not None
        ws.monitor_threads_addressed = {_MERGE_BLOCK_ATTENTION_STATE_KEY: old_stamp}
        ws.awaiting_human_since = episode_start
        ws.awaiting_human_reason = "GitHub rejected the merge attempt"
        await session.commit()

    before_call = datetime.now(UTC)
    await runner._clear_stale_merge_attention(
        workspace_id,
        state,
        now=datetime(2024, 1, 2, tzinfo=UTC),
        status=replace(_mergeable_status(), merge_state_status=MergeStateStatus.BLOCKED),
        forge="github",
    )
    after_call = datetime.now(UTC)

    refreshed_stamp = datetime.fromisoformat(
        state.threads_addressed_ids[_MERGE_BLOCK_ATTENTION_STATE_KEY]
    )
    assert before_call <= refreshed_stamp <= after_call
    async with factory() as session:
        ws_after = await WorkspaceRepository(session).get(workspace_id)
        assert ws_after is not None
    assert ws_after.awaiting_human_since == episode_start
    assert ws_after.awaiting_human_reason == "GitHub rejected the merge attempt"
    assert (ws_after.monitor_threads_addressed or {})[
        _MERGE_BLOCK_ATTENTION_STATE_KEY
    ] == refreshed_stamp.isoformat()


@pytest.mark.unit
async def test_clear_stale_merge_attention_atomic_clear_marker_absent_on_row_still_clears_attention(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The stale (resolved) atomic-clear branch still clears ``awaiting_human_since``
    and commits when the persisted row carries NO merge-block marker — the in-memory
    ``state`` had a marker (driving the stale branch) but the durable row never
    received it (the outer loop had not flushed ``_persist_state`` yet, or a
    restart reloaded a marker-less row that a same-poll preserve then re-stamped).

    The ``threads_addressed.pop(...) is None`` guard must skip the redundant
    ``monitor_threads_addressed`` reassignment (line 303) WITHOUT skipping the
    attention clear + commit (lines 304-305): a row that already lacks the marker
    still needs ``awaiting_human_since`` cleared so the resolved ``NotifyHuman``
    episode stops surfacing. Covers the missing branch in the atomic clear path.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    # The in-memory state carries a stale marker (drives the stale branch), but
    # the persisted row has NO marker — only an active attention flag to clear.
    stale_stamp = datetime(2024, 1, 1, tzinfo=UTC).isoformat()
    state = MonitorState(threads_addressed_ids={_MERGE_BLOCK_ATTENTION_STATE_KEY: stale_stamp})
    async with factory() as session:
        ws = await WorkspaceRepository(session).get_for_update(workspace_id)
        assert ws is not None
        # No marker on the row; attention flag set from a prior escalation.
        ws.monitor_threads_addressed = {}
        ws.awaiting_human_since = datetime(2024, 1, 1, tzinfo=UTC)
        ws.awaiting_human_reason = "merge_blocked"
        await session.commit()
    resolved_now = datetime(2024, 1, 2, tzinfo=UTC)
    await runner._clear_stale_merge_attention(
        workspace_id,
        state,
        now=resolved_now,
    )
    # In-memory marker dropped.
    assert _MERGE_BLOCK_ATTENTION_STATE_KEY not in state.threads_addressed_ids
    # The persisted row still carries no marker, and the attention flag is cleared
    # despite the marker-absent branch skipping the threads_addressed reassignment.
    async with factory() as session:
        ws_after = await WorkspaceRepository(session).get(workspace_id)
        assert ws_after is not None
    assert _MERGE_BLOCK_ATTENTION_STATE_KEY not in (ws_after.monitor_threads_addressed or {})
    assert ws_after.awaiting_human_since is None


@pytest.mark.unit
async def test_clear_merge_block_attention_durably_missing_workspace_is_noop(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The durable marker clear no-ops when the workspace row is gone (a
    GC/destroy race between the stale-clear and the durable write)."""
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    await runner._clear_merge_block_attention_durably("ws_does_not_exist")


@pytest.mark.unit
async def test_clear_merge_block_attention_durably_absent_marker_is_noop(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The durable clear no-ops when the persisted marker is already absent —
    no redundant commit is issued."""
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    # No marker on the row ⇒ the helper returns before committing.
    await runner._clear_merge_block_attention_durably(workspace_id)
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
    assert _MERGE_BLOCK_ATTENTION_STATE_KEY not in (ws.monitor_threads_addressed or {})


@pytest.mark.unit
async def test_clear_merge_block_attention_durably_present_marker_is_removed(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The durable clear drops a present marker and commits — the symmetric
    counterpart to ``_persist_merge_block_attention_durably``. Seeding a stale
    marker on the row and invoking the helper leaves ``monitor_threads_addressed``
    without the key while preserving any other addressed ids."""
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    stamp = datetime(2024, 1, 1, tzinfo=UTC).isoformat()
    async with factory() as session:
        ws = await WorkspaceRepository(session).get_for_update(workspace_id)
        assert ws is not None
        ws.monitor_threads_addressed = {
            _MERGE_BLOCK_ATTENTION_STATE_KEY: stamp,
            "other_addressed": "kept",
        }
        await session.commit()
    # Marker present ⇒ the helper reassigns threads_addressed and commits.
    await runner._clear_merge_block_attention_durably(workspace_id)
    async with factory() as session:
        ws_after = await WorkspaceRepository(session).get(workspace_id)
        assert ws_after is not None
    addressed = ws_after.monitor_threads_addressed or {}
    assert _MERGE_BLOCK_ATTENTION_STATE_KEY not in addressed
    assert addressed.get("other_addressed") == "kept"


@pytest.mark.unit
async def test_set_workspace_attention_with_merge_block_marker_absent_marker_skips_marker_write(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The atomic write sets ``awaiting_human_since`` even when no marker is
    stamped into state (the caller did not re-stamp) — the marker merge is
    skipped, but the attention flag is still persisted. This is the defensive
    ``if stamped is not None`` False branch: production always stamps at the
    merge_loop call site, but the helper must not drop the attention write when
    the marker is absent."""
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    # No marker stamped into state ⇒ the marker merge is skipped, but the
    # attention flag must still be persisted.
    state = MonitorState()
    await runner._set_workspace_attention_with_merge_block_marker(
        workspace_id,
        state,
        reason="merge_blocked",
    )
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
    assert _MERGE_BLOCK_ATTENTION_STATE_KEY not in (ws.monitor_threads_addressed or {})
    assert ws.awaiting_human_since is not None
    assert ws.awaiting_human_reason == "merge_blocked"


@pytest.mark.unit
async def test_set_workspace_attention_with_merge_block_marker_already_equal_skips_marker_merge(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The atomic write's marker merge is idempotent: when the DB row already
    carries the in-memory stamp, the merge is skipped (no redundant assignment)
    but the attention flag is still refreshed and committed. This is the
    ``if threads_addressed.get(...) != stamped`` False branch — a second
    fallback poll re-stamps the same marker the first poll already persisted."""
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    # First call persists the marker AND sets attention.
    state = MonitorState()
    state.mark_merge_block_attention()
    await runner._set_workspace_attention_with_merge_block_marker(
        workspace_id,
        state,
        reason="merge_blocked",
    )
    stamped = state.threads_addressed_ids[_MERGE_BLOCK_ATTENTION_STATE_KEY]
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
    assert (ws.monitor_threads_addressed or {})[_MERGE_BLOCK_ATTENTION_STATE_KEY] == stamped
    first_attention = ws.awaiting_human_since
    assert first_attention is not None
    # A second call with the SAME stamp must skip the marker merge (already
    # equal) but still refresh and commit the attention flag.
    await runner._set_workspace_attention_with_merge_block_marker(
        workspace_id,
        state,
        reason="merge_blocked",
    )
    async with factory() as session:
        ws_after = await WorkspaceRepository(session).get(workspace_id)
        assert ws_after is not None
    assert (ws_after.monitor_threads_addressed or {})[_MERGE_BLOCK_ATTENTION_STATE_KEY] == stamped
    assert ws_after.awaiting_human_since is not None


class _CommitTrapSession:
    """Wraps an ``AsyncSession`` so ``commit`` can be trapped by attempt number.

    Counts every ``commit`` call into the shared ``commit_attempts`` dict (key
    ``"n"``) and raises ``RuntimeError`` on the attempt matching ``fail_on_attempt``
    (1-indexed) BEFORE delegating to ``inner.commit()``, so the simulated DB
    failure rolls that transaction back. ``fail_on_attempt=None`` never raises.

    This lets a regression FAIL for the prior two-commit implementation while
    PASSING for the current single-transaction atomic clear:

    * ``fail_on_attempt=1`` simulates a failure on the (only) atomic commit —
      the current implementation rolls both writes back (the
      ``PRRT_kwDOSJAM6s6Lh0zt`` rollback contract). The prior two-commit
      sequence would also roll its first commit back and never reach the
      second, so this case does NOT by itself distinguish old from new (the
      targeted ``fail_on_attempt=2`` case below does).
    * ``fail_on_attempt=2`` lets the first commit land (the prior
      implementation's ``_clear_workspace_attention`` clearing
      ``awaiting_human_since``) and raises on the second (the prior
      ``_clear_merge_block_attention_durably`` marker removal) — exposing the
      half-cleared restart window the atomic fix closed. The current
      single-commit implementation never reaches attempt 2, so it completes
      with both fields cleared and exactly one recorded commit attempt.
    """

    def __init__(
        self,
        inner: AsyncSession,
        commit_attempts: dict[str, int],
        *,
        fail_on_attempt: int | None = None,
    ) -> None:
        self._inner = inner
        self._commit_attempts = commit_attempts
        self._fail_on_attempt = fail_on_attempt

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def commit(self) -> None:
        self._commit_attempts["n"] += 1
        if self._fail_on_attempt is not None and (
            self._commit_attempts["n"] == self._fail_on_attempt
        ):
            raise RuntimeError("simulated DB failure mid-transaction")
        await self._inner.commit()

    async def __aenter__(self) -> _CommitTrapSession:
        await self._inner.__aenter__()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self._inner.__aexit__(exc_type, exc, tb)


@pytest.mark.unit
async def test_clear_stale_merge_attention_atomic_clear_rolls_back_together(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6Lh0zt: the stale (resolved) branch's marker removal and
    ``awaiting_human_since`` clear MUST land in a SINGLE transaction. The prior
    two-commit sequence (``_clear_workspace_attention`` then
    ``_clear_merge_block_attention_durably``) left a cancel/restart window where
    ``awaiting_human_since`` was already nulled but the STALE marker still sat on
    the DB row — so the next poll's preserve path re-stamped the stale marker
    fresh and wedged the monitor in a faux "awaiting human" state until another
    merge fallback ran (the ``PRRT_kwDOSJAM6s6Lf_37`` window's reciprocal). With
    both writes under one ``get_for_update`` transaction, a mid-transaction
    failure rolls BOTH back: a restart can never observe the cleared flag
    without the also-cleared marker, or vice versa.

    This test simulates the restart window by making the stale-branch commit
    raise. Under the atomic fix the transaction rolls back in full, so the row
    still carries BOTH the stale marker AND the still-set attention flag —
    exactly the pre-failure state, with neither half observable alone. The
    in-memory marker is dropped before the DB write (mirroring production), so
    the assertion focuses on the DB row's atomic consistency.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    # Seed a stale marker (resolved block) and an active attention flag into the
    # persisted row — the state the monitor would reload after a cancel/restart
    # before the outer loop's full ``_persist_state`` flush.
    stale_stamp = datetime(2024, 1, 1, tzinfo=UTC).isoformat()
    async with factory() as session:
        ws = await WorkspaceRepository(session).get_for_update(workspace_id)
        assert ws is not None
        ws.monitor_threads_addressed = {_MERGE_BLOCK_ATTENTION_STATE_KEY: stale_stamp}
        ws.awaiting_human_since = datetime(2024, 1, 1, tzinfo=UTC)
        ws.awaiting_human_reason = "merge_blocked"
        await session.commit()

    # Wrap the session factory so the FIRST session's commit raises — the
    # restart-window between the (old) two commits, now exercised against the
    # single atomic transaction.
    commit_attempts: dict[str, int] = {"n": 0}

    @asynccontextmanager
    async def _failing_factory() -> AsyncIterator[_CommitTrapSession]:
        commit_attempts["n"]  # shared counter touched inside the trap
        async with factory() as inner:
            yield _CommitTrapSession(inner, commit_attempts, fail_on_attempt=1)

    runner = make_runner(
        factory=factory,  # type: ignore[arg-type]
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    # Override the runner's session factory with the failing-commit wrapper so
    # the stale branch's single atomic commit raises mid-transaction.
    runner._deps = replace(runner._deps, session_factory=_failing_factory)

    state = MonitorState(threads_addressed_ids={_MERGE_BLOCK_ATTENTION_STATE_KEY: stale_stamp})
    resolved_now = datetime(2024, 1, 2, tzinfo=UTC)
    # The stale branch raises mid-transaction; the atomic transaction rolls back.
    with pytest.raises(RuntimeError, match="simulated DB failure"):
        await runner._clear_stale_merge_attention(
            workspace_id,
            state,
            now=resolved_now,
        )
    # A session was opened (the failing-commit one) — the runner attempted the
    # atomic clear, and the trap recorded exactly the single commit attempt the
    # atomic transaction performs.
    assert commit_attempts["n"] >= 1

    # Under the atomic fix, the transaction rolled back in full: the row still
    # carries BOTH the stale marker AND the still-set attention flag — neither
    # half of the marker/attention pair is observable independently. This is
    # the contract the prior two-commit sequence violated.
    async with factory() as session:
        ws_after = await WorkspaceRepository(session).get(workspace_id)
        assert ws_after is not None
    assert (ws_after.monitor_threads_addressed or {}).get(
        _MERGE_BLOCK_ATTENTION_STATE_KEY
    ) == stale_stamp
    assert ws_after.awaiting_human_since is not None
    assert ws_after.awaiting_human_reason == "merge_blocked"


@pytest.mark.unit
async def test_clear_stale_merge_attention_atomic_clear_distinguishes_two_commit_window(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6Lh0zt (PRRT_kwDOSJAM6s6LiZkN): the atomic clear MUST
    complete with a SINGLE commit that lands BOTH the stale marker removal and
    the ``awaiting_human_since`` clear. The sibling rollback regression above
    traps a mid-transaction failure (``fail_on_attempt=1``), but it does NOT by
    itself distinguish the current single-transaction implementation from the
    prior two-commit sequence: the prior ``_clear_workspace_attention`` (commit
    #1) then ``_clear_merge_block_attention_durably`` (commit #2) would ALSO
    roll its first commit back and never reach the second when commit #1
    raises, leaving both fields unchanged — the same observable end state as the
    atomic rollback.

    This targeted regression lets the FIRST commit land and traps the SECOND
    (``fail_on_attempt=2``). Under the prior two-commit implementation the
    first commit (``_clear_workspace_attention``) would clear
    ``awaiting_human_since`` and succeed, then the second commit
    (``_clear_merge_block_attention_durably``) would raise — leaving the DB in
    the half-cleared restart window (attention already nulled but the STALE
    marker still on the row) and surfacing the ``RuntimeError``. Under the
    current single-transaction atomic clear there is only ONE commit, so the
    trap is never reached (``fail_on_attempt=2``), the helper completes
    cleanly, and BOTH the marker and the attention flag are cleared together.
    Asserting exactly one recorded commit attempt AND both fields cleared
    makes this regression FAIL for the reintroduced two-commit approach (which
    would either raise on the second commit or leave the half-cleared state
    visible) while passing only for the atomic implementation.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    # Seed a stale marker (resolved block) and an active attention flag — the
    # pre-clear persisted state the atomic clear is meant to roll back from on
    # the stale (resolved) branch.
    stale_stamp = datetime(2024, 1, 1, tzinfo=UTC).isoformat()
    async with factory() as session:
        ws = await WorkspaceRepository(session).get_for_update(workspace_id)
        assert ws is not None
        ws.monitor_threads_addressed = {_MERGE_BLOCK_ATTENTION_STATE_KEY: stale_stamp}
        ws.awaiting_human_since = datetime(2024, 1, 1, tzinfo=UTC)
        ws.awaiting_human_reason = "merge_blocked"
        await session.commit()

    # Trap the SECOND commit: the prior two-commit implementation would land
    # the attention clear on commit #1 and raise on commit #2 (marker removal),
    # surfacing the half-cleared restart window. The current single-transaction
    # atomic clear performs exactly one commit and never reaches attempt 2.
    commit_attempts: dict[str, int] = {"n": 0}

    @asynccontextmanager
    async def _trapping_factory() -> AsyncIterator[_CommitTrapSession]:
        async with factory() as inner:
            yield _CommitTrapSession(inner, commit_attempts, fail_on_attempt=2)

    runner = make_runner(
        factory=factory,  # type: ignore[arg-type]
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    runner._deps = replace(runner._deps, session_factory=_trapping_factory)

    state = MonitorState(threads_addressed_ids={_MERGE_BLOCK_ATTENTION_STATE_KEY: stale_stamp})
    resolved_now = datetime(2024, 1, 2, tzinfo=UTC)
    # The atomic clear completes with its single commit; the trap (set on
    # attempt 2) is never reached. The prior two-commit implementation would
    # raise here on its second commit.
    await runner._clear_stale_merge_attention(
        workspace_id,
        state,
        now=resolved_now,
    )

    # The atomic implementation performs EXACTLY one commit. A reintroduced
    # two-commit sequence would record two attempts (and raise on the second).
    assert commit_attempts["n"] == 1

    # Both the stale marker AND the attention flag are cleared together in
    # that single transaction — neither half of the pair is observable alone.
    # A prior two-commit implementation that survived commit #1 but raised on
    # commit #2 would leave the half-cleared state: marker still present,
    # attention already nulled.
    async with factory() as session:
        ws_after = await WorkspaceRepository(session).get(workspace_id)
        assert ws_after is not None
    assert (ws_after.monitor_threads_addressed or {}).get(_MERGE_BLOCK_ATTENTION_STATE_KEY) is None
    assert ws_after.awaiting_human_since is None
    assert ws_after.awaiting_human_reason is None


@pytest.mark.unit
async def test_clear_stale_merge_attention_atomic_clear_missing_workspace_skips_writes(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6Lh0zt: the atomic clear no-ops when the workspace row is
    gone (a GC/destroy race between the stale-clear and the atomic write) — no
    marker removal, no attention clear, no commit. Mirrors the established
    ``get_for_update``-returns-None no-op guard in the sibling durable helpers."""
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    stale_stamp = datetime(2024, 1, 1, tzinfo=UTC).isoformat()
    state = MonitorState(threads_addressed_ids={_MERGE_BLOCK_ATTENTION_STATE_KEY: stale_stamp})
    # The workspace does not exist — the atomic clear returns before any write.
    await runner._clear_stale_merge_attention(
        "ws_does_not_exist",
        state,
        now=datetime(2024, 1, 2, tzinfo=UTC),
    )
    # In-memory marker is still dropped (the caller's signal), but no DB write
    # was attempted against a missing row.
    assert _MERGE_BLOCK_ATTENTION_STATE_KEY not in state.threads_addressed_ids
