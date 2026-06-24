"""Regression tests for PR monitor merge-block attention-marker preservation.

Covers the ``#661`` / ``#663`` / ``PRRT_kwDOSJAM6s6La_SZ`` /
``PRRT_kwDOSJAM6s6LcfXk`` contracts: a resolved ``NotifyHuman`` attention flag
must be cleared before the pre-merge settle / merge attempt, and a
``merge_block_attention`` marker that is FRESH at merge-coordinator entry must
survive a serialized-merge wait longer than the marker TTL (the wait is not a
poll, so the branch-protection fallback cannot re-stamp during it).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import GitHubClientError
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    _MERGE_BLOCK_ATTENTION_ORIGIN_MERGE_REJECTION,
    _MERGE_BLOCK_ATTENTION_ORIGIN_STATE_KEY,
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


class _FakeClock:
    """Deterministic wall clock for merge-attention TTL regressions."""

    def __init__(self, current: datetime, *, tick_seconds: float = 0.0) -> None:
        self._current = current
        self._tick = timedelta(seconds=tick_seconds)

    def now(self) -> datetime:
        current = self._current
        self._current += self._tick
        return current

    def advance(self, seconds: float) -> datetime:
        self._current += timedelta(seconds=seconds)
        return self._current


class _LongWaitMergeCoordinator:
    """A merge coordinator that advances a fake clock before yielding.

    The clock moves past a tiny TTL so a marker stamped fresh at entry ages out
    during the serialized wait without depending on scheduler timing.

    Models the real Postgres/InProcess coordinators blocking behind another
    merge in the same repo/base lane: no branch-protection fallback fires
    during that wait (no poll runs), so the marker is NOT re-stamped. Used to
    reproduce the flicker described in PRRT_kwDOSJAM6s6La_SZ.
    """

    def __init__(self, *, wait_seconds: float, clock: _FakeClock) -> None:
        self._wait_seconds = wait_seconds
        self._clock = clock
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
        self.yielded_at = self._clock.advance(self._wait_seconds)
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
    clock = _FakeClock(datetime(2026, 1, 1, 12, 0, 1, tzinfo=UTC), tick_seconds=0.01)
    # The fake clock makes the marker fresh at coordinator entry, then stale
    # after the coordinator advances past the tight TTL. This fails if the
    # preserve decision accidentally uses the post-wait wall clock.
    monitor_config = MonitorConfig(
        auto_merge=True,
        poll_interval_seconds=60,
        pre_merge_settle_seconds=0,
        initial_review_grace_period_seconds=0,
        non_check_reviewer_settle_seconds=0,
        non_check_reviewer_logins=(),
        merge_block_attention_ttl_seconds=1.0,
    )
    coordinator = _LongWaitMergeCoordinator(wait_seconds=1.5, clock=clock)
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
        now=clock.now,
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
    state.mark_merge_block_attention(now=clock.now())

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
async def test_bitbucket_clean_status_preserves_merge_block_attention_during_queue_wait(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Bitbucket ``CLEAN`` during a queue-style wait is not proof that a prior
    deterministic merge rejection has resolved.

    Bitbucket maps open PRs to ``CLEAN`` because it does not expose GitHub's
    branch-protection merge-state signal. Preserve active operator attention for
    that forge while GitHub ``CLEAN`` remains allowed to clear ordinary
    non-rejection markers as a confirmed resolution.
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
        ws.awaiting_human_reason = "Bitbucket rejected the merge attempt"
        await session.commit()

    await runner._clear_or_preserve_merge_attention_for_queue_wait(
        workspace_id,
        state,
        status=_mergeable_status(),
        forge="bitbucket",
    )

    assert state.threads_addressed_ids[_MERGE_BLOCK_ATTENTION_STATE_KEY] == marker
    async with factory() as session:
        ws_after = await WorkspaceRepository(session).get(workspace_id)
        assert ws_after is not None
    assert (ws_after.monitor_threads_addressed or {})[_MERGE_BLOCK_ATTENTION_STATE_KEY] == marker
    assert ws_after.awaiting_human_since == episode_start
    assert ws_after.awaiting_human_reason == "Bitbucket rejected the merge attempt"


@pytest.mark.unit
async def test_github_clean_status_preserves_merge_rejection_attention_during_queue_wait(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """#677: GitHub ``CLEAN`` is not proof that an actor/push restriction which
    rejected the previous merge attempt has resolved.

    Actor and push restrictions are invisible to ``mergeStateStatus``. A queue,
    reviewer-settle, or grace wait does not retry the merge, so it must preserve
    rejection-origin attention until a later merge attempt confirms success or
    re-stamps the rejection.
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
    state.mark_merge_block_attention(
        now=datetime(2026, 1, 1, 12, 0, 1, tzinfo=UTC),
        originated_from_merge_rejection=True,
    )
    marker_state = dict(state.threads_addressed_ids)
    marker = marker_state[_MERGE_BLOCK_ATTENTION_STATE_KEY]
    async with factory() as session:
        ws = await WorkspaceRepository(session).get_for_update(workspace_id)
        assert ws is not None
        ws.monitor_threads_addressed = marker_state
        ws.awaiting_human_since = episode_start
        ws.awaiting_human_reason = "GitHub merge was denied by branch actor restrictions"
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
    assert (ws_after.monitor_threads_addressed or {}) == marker_state
    assert ws_after.awaiting_human_since == episode_start
    assert ws_after.awaiting_human_reason == "GitHub merge was denied by branch actor restrictions"


@pytest.mark.unit
async def test_github_clean_structured_merge_rejection_preserve_uses_state_not_db(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Structured merge-rejection origin is already in ``MonitorState``.

    A GitHub ``CLEAN`` queue wait must preserve from that in-memory marker
    without opening a second workspace session. Rows without structured origin
    are intentionally not preserved from ambiguous reason text.
    """
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    state = MonitorState()
    state.mark_merge_block_attention(
        now=datetime(2026, 1, 1, 12, 0, 1, tzinfo=UTC),
        originated_from_merge_rejection=True,
    )
    marker = state.threads_addressed_ids[_MERGE_BLOCK_ATTENTION_STATE_KEY]

    def _forbidden_session_factory() -> None:
        raise AssertionError("structured merge-rejection origin should not read the DB")

    monkeypatch.setattr(runner._deps, "session_factory", _forbidden_session_factory)

    await runner._clear_or_preserve_merge_attention_for_queue_wait(
        "unused-workspace-id",
        state,
        status=_mergeable_status(),
        forge="github",
    )

    assert state.threads_addressed_ids[_MERGE_BLOCK_ATTENTION_STATE_KEY] == marker


@pytest.mark.unit
async def test_github_clean_status_preserves_stale_merge_rejection_attention_at_critical_section(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6L1FjH: critical-section entry must not clear a TTL-stale
    marker when the surfaced attention came from a prior merge rejection.

    Actor/push restrictions can reject the merge attempt while remaining
    invisible to GitHub's ``mergeStateStatus``. Queue waits preserve that marker
    without re-stamping, so it can be TTL-stale by the next critical-section
    entry even though the operator block has not been confirmed resolved.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    stale_stamp = datetime(2024, 1, 1, tzinfo=UTC).isoformat()
    state = MonitorState()
    state.mark_merge_block_attention(
        now=datetime(2024, 1, 1, tzinfo=UTC),
        originated_from_merge_rejection=True,
    )
    state.threads_addressed_ids[_MERGE_BLOCK_ATTENTION_STATE_KEY] = stale_stamp
    marker_state = dict(state.threads_addressed_ids)
    episode_start = datetime(2024, 1, 1, 12, tzinfo=UTC)
    async with factory() as session:
        ws = await WorkspaceRepository(session).get_for_update(workspace_id)
        assert ws is not None
        ws.monitor_threads_addressed = marker_state
        ws.awaiting_human_since = episode_start
        ws.awaiting_human_reason = "GitHub merge was denied by branch actor restrictions"
        await session.commit()

    before_call = datetime.now(UTC)
    await runner._clear_stale_merge_attention(
        workspace_id,
        state,
        now=datetime(2024, 1, 2, tzinfo=UTC),
        status=_mergeable_status(),
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
    assert ws_after.awaiting_human_reason == "GitHub merge was denied by branch actor restrictions"
    assert (ws_after.monitor_threads_addressed or {})[
        _MERGE_BLOCK_ATTENTION_STATE_KEY
    ] == refreshed_stamp.isoformat()
    assert state.threads_addressed_ids[_MERGE_BLOCK_ATTENTION_ORIGIN_STATE_KEY] == (
        _MERGE_BLOCK_ATTENTION_ORIGIN_MERGE_REJECTION
    )
    assert (ws_after.monitor_threads_addressed or {})[
        _MERGE_BLOCK_ATTENTION_ORIGIN_STATE_KEY
    ] == _MERGE_BLOCK_ATTENTION_ORIGIN_MERGE_REJECTION


@pytest.mark.unit
async def test_github_clean_status_clears_non_rejection_attention_during_queue_wait(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """#671: GitHub ``CLEAN`` still clears an ordinary merge-block marker when no
    prior merge rejection is the source of the surfaced attention.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    state = MonitorState()
    state.mark_merge_block_attention(now=datetime(2026, 1, 1, 12, 0, 1, tzinfo=UTC))
    marker = state.threads_addressed_ids[_MERGE_BLOCK_ATTENTION_STATE_KEY]
    async with factory() as session:
        ws = await WorkspaceRepository(session).get_for_update(workspace_id)
        assert ws is not None
        ws.monitor_threads_addressed = {_MERGE_BLOCK_ATTENTION_STATE_KEY: marker}
        ws.awaiting_human_since = datetime(2026, 1, 1, 12, tzinfo=UTC)
        ws.awaiting_human_reason = "prior non-rejection escalation"
        await session.commit()

    await runner._clear_or_preserve_merge_attention_for_queue_wait(
        workspace_id,
        state,
        status=_mergeable_status(),
        forge="github",
    )

    assert _MERGE_BLOCK_ATTENTION_STATE_KEY not in state.threads_addressed_ids
    async with factory() as session:
        ws_after = await WorkspaceRepository(session).get(workspace_id)
        assert ws_after is not None
    assert _MERGE_BLOCK_ATTENTION_STATE_KEY not in (ws_after.monitor_threads_addressed or {})
    assert ws_after.awaiting_human_since is None
    assert ws_after.awaiting_human_reason is None


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
    clock = _FakeClock(datetime(2026, 1, 1, 12, 0, 1, tzinfo=UTC), tick_seconds=0.01)
    # The fake clock keeps the setup gap out of the assertion while still making
    # the marker stale by the time the post-lock queue wait runs.
    monitor_config = MonitorConfig(
        auto_merge=True,
        poll_interval_seconds=60,
        pre_merge_settle_seconds=0,
        initial_review_grace_period_seconds=0,
        non_check_reviewer_settle_seconds=0,
        non_check_reviewer_logins=(),
        merge_block_attention_ttl_seconds=1.0,
    )
    coordinator = _LongWaitMergeCoordinator(wait_seconds=1.5, clock=clock)
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
        now=clock.now,
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
    state.mark_merge_block_attention(now=clock.now())
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
    clock = _FakeClock(datetime(2026, 1, 1, 12, 0, 1, tzinfo=UTC), tick_seconds=0.01)
    coordinator = _LongWaitMergeCoordinator(wait_seconds=1.5, clock=clock)
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
        now=clock.now,
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
    clock = _FakeClock(datetime(2026, 1, 1, 12, 0, 1, tzinfo=UTC), tick_seconds=0.01)
    # The fake clock makes the marker fresh at entry and stale after the
    # serialized wait, without depending on real elapsed time in CI.
    monitor_config = MonitorConfig(
        auto_merge=True,
        poll_interval_seconds=60,
        pre_merge_settle_seconds=0,
        initial_review_grace_period_seconds=0,
        non_check_reviewer_settle_seconds=0,
        non_check_reviewer_logins=(),
        merge_block_attention_ttl_seconds=1.0,
    )
    coordinator = _LongWaitMergeCoordinator(wait_seconds=1.5, clock=clock)
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
        now=clock.now,
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
    state.mark_merge_block_attention(now=clock.now())
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
