"""Bounded-retry coverage for transient forge auth blips (#515).

A transient forge auth error (an ambiguous ``HTTP 401`` / ``Requires
authentication`` on GitHub, or any 401/403 on Bitbucket) must be bounded-retryable
with exponential backoff rather than terminating a healthy auto-merge on a
momentary blip. These tests pin the new backoff schedule, the per-context retry
counter (with corrupt-value recovery and clear-on-success), the exhaustion path,
and the end-to-end #515 regression.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.bitbucket_client import (
    BITBUCKET_AUTH_FAILED,
    BitbucketClientError,
)
from awf.common.commands import FakeCommandRunner
from awf.common.github_client import GITHUB_API_ERROR, GitHubClientError
from awf.db.enums import WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner.helpers import (
    _clear_transient_forge_retry_state,
    _exponential_backoff_wait_seconds,
    _increment_forge_transient_retry_count,
)
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    pr_payload,
    seed_monitoring_workspace,
)

_FORGE_COUNT_KEY = "__awf_forge_transient_retry_count:fetch_pr_status"


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _events(ws: Workspace, event_type: str) -> list:
    return [event for event in ws.events if event.event_type == event_type]


@pytest.mark.unit
def test_exponential_backoff_schedule_grows_then_caps() -> None:
    # 1-based retry number: the first retry waits the initial backoff, doubling
    # each step, capped at the maximum.
    waits = [
        _exponential_backoff_wait_seconds(
            retry_number=n,
            initial_backoff_seconds=5.0,
            max_backoff_seconds=120.0,
        )
        for n in range(1, 7)
    ]
    assert waits == [5.0, 10.0, 20.0, 40.0, 80.0, 120.0]
    # A huge retry number stays clamped at the ceiling (no overflow).
    assert (
        _exponential_backoff_wait_seconds(
            retry_number=100,
            initial_backoff_seconds=5.0,
            max_backoff_seconds=120.0,
        )
        == 120.0
    )


@pytest.mark.unit
def test_increment_forge_transient_retry_count_recovers_from_corrupt_value() -> None:
    state = MonitorState(threads_addressed_ids={_FORGE_COUNT_KEY: "not-an-integer"})

    retry_number = _increment_forge_transient_retry_count(state, "fetch_pr_status")

    assert retry_number == 1
    assert state.threads_addressed_ids[_FORGE_COUNT_KEY] == "1"
    # The counter is per-context — a distinct context starts its own budget.
    assert _increment_forge_transient_retry_count(state, "merge_pr") == 1
    assert state.threads_addressed_ids[_FORGE_COUNT_KEY] == "1"


@pytest.mark.unit
def test_clear_transient_forge_retry_state_reports_whether_present() -> None:
    state = MonitorState(threads_addressed_ids={_FORGE_COUNT_KEY: "3"})

    assert _clear_transient_forge_retry_state(state, context="fetch_pr_status") is True
    assert _FORGE_COUNT_KEY not in state.threads_addressed_ids
    # Idempotent: clearing an absent counter reports no change.
    assert _clear_transient_forge_retry_state(state, context="fetch_pr_status") is False


@pytest.mark.unit
async def test_github_transient_401_retries_with_backoff_then_exhausts(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    sleep_fn = RecordedSleep()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
    )
    # The exact #515 failure string — a bare 401 with no strong permanent marker.
    exc = GitHubClientError(
        operation="gh api graphql",
        returncode=1,
        stderr="gh: Requires authentication (HTTP 401)",
    )
    state = MonitorState()

    # The default budget is 5: the first five calls retry with the exponential
    # backoff schedule; the sixth exhausts.
    for expected_count in range(1, 6):
        retried = await runner._wait_after_transient_github_error(
            exc,
            workspace_id=workspace_id,
            pr_number=42,
            context="fetch_pr_status",
            state=state,
            monitor_log=None,
        )
        assert retried is True
        assert state.threads_addressed_ids[_FORGE_COUNT_KEY] == str(expected_count)

    assert sleep_fn.calls == [5, 10, 20, 40, 80]

    exhausted = await runner._wait_after_transient_github_error(
        exc,
        workspace_id=workspace_id,
        pr_number=42,
        context="fetch_pr_status",
        state=state,
        monitor_log=None,
    )

    # Exhaustion: no further sleep, the helper returns False so the caller
    # terminates, and a distinct exhausted event is recorded.
    assert exhausted is False
    assert sleep_fn.calls == [5, 10, 20, 40, 80]
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        retrying = _events(ws, "monitor.github_transient_error_retrying")
        assert len(retrying) == 5
        exhausted_events = _events(ws, "monitor.github_transient_error_retry_exhausted")
        assert len(exhausted_events) == 1
        assert exhausted_events[0].reason_code == "GITHUB_TRANSIENT_RETRY_EXHAUSTED"
        assert exhausted_events[0].payload["retry_number"] == 6
        assert exhausted_events[0].payload["max_retries"] == 5


@pytest.mark.unit
async def test_bitbucket_transient_403_retries_with_backoff_then_exhausts(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    sleep_fn = RecordedSleep()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
    )
    # Keep the test fast: a budget of 2 retries before exhaustion.
    object.__setattr__(runner._runner_config, "transient_forge_max_retries", 2)
    exc = BitbucketClientError(
        operation="bitbucket fetch_pr_status",
        status=403,
        body="forbidden",
        reason_code=BITBUCKET_AUTH_FAILED,
    )
    state = MonitorState()

    for _ in range(2):
        assert (
            await runner._wait_after_transient_bitbucket_error(
                exc,
                workspace_id=workspace_id,
                pr_number=42,
                context="fetch_pr_status",
                state=state,
                monitor_log=None,
            )
            is True
        )

    assert sleep_fn.calls == [5, 10]

    exhausted = await runner._wait_after_transient_bitbucket_error(
        exc,
        workspace_id=workspace_id,
        pr_number=42,
        context="fetch_pr_status",
        state=state,
        monitor_log=None,
    )

    assert exhausted is False
    assert sleep_fn.calls == [5, 10]
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert len(_events(ws, "monitor.bitbucket_transient_error_retrying")) == 2
        exhausted_events = _events(ws, "monitor.bitbucket_transient_error_retry_exhausted")
        assert len(exhausted_events) == 1
        assert exhausted_events[0].reason_code == "BITBUCKET_TRANSIENT_RETRY_EXHAUSTED"
        assert exhausted_events[0].payload["reason_code"] == BITBUCKET_AUTH_FAILED


@pytest.mark.unit
async def test_monitor_run_survives_github_401_auth_blip_and_clears_retry_count(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The exact #515 regression: a transient GraphQL 401 on the first poll must
    retry (not terminate), and the next green poll merges and clears the counter."""
    cmd = FakeCommandRunner()
    sleep_fn = RecordedSleep()
    workspace_id = await seed_monitoring_workspace(factory)
    # Poll 1: base fetch + base-behind ok, then the gh graphql 401 blip.
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="0\n")
    cmd.queue_result(returncode=1, stderr="gh: Requires authentication (HTTP 401)")
    # Poll 2: base fetch + base-behind ok, PR already merged upstream → complete.
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="0\n")
    cmd.queue_result(returncode=0, stdout=pr_payload(merged=True))
    cmd.queue_result(returncode=0)  # compose down
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
    )

    await runner.run(
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    # The auto-merge survived the blip: one bounded retry (backoff 5s) then complete.
    assert sleep_fn.calls == [5]
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.completed.value
        assert ws.failure_message is None
        retrying = _events(ws, "monitor.github_transient_error_retrying")
        assert len(retrying) == 1
        assert retrying[0].reason_code == "GITHUB_TRANSIENT_RETRY"
        # The 401 carries GitHub's base reason code (no native HTTP code).
        assert retrying[0].payload["context"] == "fetch_pr_status"
        # The clear-on-success poll removed the counter so an intermittent blip
        # never accumulates toward exhaustion.
        assert _FORGE_COUNT_KEY not in (ws.monitor_threads_addressed or {})


@pytest.mark.unit
async def test_monitor_run_terminates_after_github_401_retry_budget_exhausted(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A genuinely-persistent 401 exhausts the bounded budget and terminates with
    the forge reason code preserved — no tight unbounded loop."""
    cmd = FakeCommandRunner()
    sleep_fn = RecordedSleep()
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
    )
    # Budget of 1 retry keeps the test to two polls before termination.
    object.__setattr__(runner._runner_config, "transient_forge_max_retries", 1)
    # Every poll: base fetch + base-behind ok, then the gh graphql 401.
    for _ in range(2):
        cmd.queue_result(returncode=0)
        cmd.queue_result(returncode=0, stdout="0\n")
        cmd.queue_result(returncode=1, stderr="gh: Requires authentication (HTTP 401)")

    await runner.run(
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    # One bounded retry (5s) then terminate — not an unbounded loop.
    assert sleep_fn.calls == [5]
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.failed.value
        assert "github error" in (ws.failure_message or "")
        assert ws.events[-1].reason_code == GITHUB_API_ERROR
        assert len(_events(ws, "monitor.github_transient_error_retrying")) == 1
        assert len(_events(ws, "monitor.github_transient_error_retry_exhausted")) == 1


_RESOLVE_THREAD_COUNT_KEY = "__awf_forge_transient_retry_count:resolve_thread"


@pytest.mark.unit
async def test_clear_on_success_resets_per_context_budget_and_persists(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A recovered blip must not leak its count: once the context's operation
    succeeds the counter is cleared (and the clear is persisted), so later
    independent blips in the same context start from a fresh bounded budget
    instead of resuming from a stale count and exhausting it prematurely."""
    workspace_id = await seed_monitoring_workspace(factory)
    sleep_fn = RecordedSleep()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
    )
    object.__setattr__(runner._runner_config, "transient_forge_max_retries", 2)
    exc = GitHubClientError(
        operation="gh api graphql",
        returncode=1,
        stderr="gh: Requires authentication (HTTP 401)",
    )
    state = MonitorState()

    # First incident: two recovered blips bring ``resolve_thread`` to the budget
    # edge. The wait helper persists the running count on every blip.
    for expected_count in (1, 2):
        assert (
            await runner._wait_after_transient_github_error(
                exc,
                workspace_id=workspace_id,
                pr_number=42,
                context="resolve_thread",
                state=state,
                monitor_log=None,
            )
            is True
        )
        assert state.threads_addressed_ids[_RESOLVE_THREAD_COUNT_KEY] == str(expected_count)

    # The operation then succeeds: the counter is cleared in memory AND the clear
    # is persisted over the previously-persisted "2".
    await runner._clear_forge_transient_retry_state_on_success(
        workspace_id=workspace_id,
        state=state,
        context="resolve_thread",
    )
    assert _RESOLVE_THREAD_COUNT_KEY not in state.threads_addressed_ids
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert _RESOLVE_THREAD_COUNT_KEY not in (ws.monitor_threads_addressed or {})

    # A later, independent blip starts a fresh budget rather than tipping straight
    # into exhaustion off the stale count.
    assert (
        await runner._wait_after_transient_github_error(
            exc,
            workspace_id=workspace_id,
            pr_number=42,
            context="resolve_thread",
            state=state,
            monitor_log=None,
        )
        is True
    )
    assert state.threads_addressed_ids[_RESOLVE_THREAD_COUNT_KEY] == "1"


@pytest.mark.unit
async def test_clear_on_success_persists_only_counter_not_unconfirmed_verdict(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The success-path counter clear must persist ONLY the counter removal,
    never the whole in-memory state.

    In a fix cycle with multiple ``threads_to_resolve`` the in-memory state
    already carries addressed verdicts for *later* threads whose forge resolve
    calls have not run yet. When an earlier thread's resolve succeeds after a
    prior transient blip, clearing its counter must not flush those later
    unconfirmed verdicts: if the monitor is cancelled during a subsequent
    transient resolve wait before its rollback executes, a full-state flush here
    would reload those still-open threads as addressed and let ``decide()`` skip
    them (#305).
    """
    workspace_id = await seed_monitoring_workspace(factory)
    sleep_fn = RecordedSleep()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
    )
    object.__setattr__(runner._runner_config, "transient_forge_max_retries", 2)
    exc = GitHubClientError(
        operation="gh api graphql",
        returncode=1,
        stderr="gh: Requires authentication (HTTP 401)",
    )
    state = MonitorState()

    # A prior transient ``resolve_thread`` blip persisted the counter to the DB.
    assert (
        await runner._wait_after_transient_github_error(
            exc,
            workspace_id=workspace_id,
            pr_number=42,
            context="resolve_thread",
            state=state,
            monitor_log=None,
        )
        is True
    )
    assert state.threads_addressed_ids[_RESOLVE_THREAD_COUNT_KEY] == "1"

    # The in-memory state now also holds an addressed verdict for a *later*
    # thread whose resolve has not yet landed — exactly the unconfirmed marker
    # that must never reach the DB before its own resolve/rollback.
    state.threads_addressed_ids["PRRT_later"] = "fix_committed"

    # This thread's resolve then succeeds: the counter is cleared in memory and
    # the removal is persisted, but the later unconfirmed verdict must NOT be.
    await runner._clear_forge_transient_retry_state_on_success(
        workspace_id=workspace_id,
        state=state,
        context="resolve_thread",
    )
    assert _RESOLVE_THREAD_COUNT_KEY not in state.threads_addressed_ids
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        persisted = ws.monitor_threads_addressed or {}
        assert _RESOLVE_THREAD_COUNT_KEY not in persisted
        assert "PRRT_later" not in persisted


@pytest.mark.unit
async def test_clear_on_success_is_noop_without_a_counter(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Clearing a context that never blipped is a harmless no-op that adds no
    state mutation (and so no extra DB write on the common happy path)."""
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    state = MonitorState(threads_addressed_ids={"PRRT_x": "false_positive"})

    await runner._clear_forge_transient_retry_state_on_success(
        workspace_id=workspace_id,
        state=state,
        context="merge_pr",
    )

    # Unrelated state is untouched and no forge-retry key was introduced.
    assert state.threads_addressed_ids == {"PRRT_x": "false_positive"}


@pytest.mark.unit
async def test_transient_wait_persists_only_counter_not_unconfirmed_verdict(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A fix-cycle transient wait must persist ONLY the bounded retry counter,
    never the whole in-memory state.

    When ``resolve_thread`` (or deferred-issue capture) faults transiently, the
    in-memory state still holds the thread's unconfirmed ``fix_committed`` verdict
    — the caller only rolls it back *after* the wait returns. If the wait flushed
    the whole state, a crash/restart during the backoff would reload the thread as
    addressed even though the forge resolve failed, so ``decide()`` would skip the
    still-open thread and let auto-merge bypass live feedback (#305). The counter
    must still be persisted so the bounded budget survives across polls.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    sleep_fn = RecordedSleep()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
    )
    object.__setattr__(runner._runner_config, "transient_forge_max_retries", 2)
    exc = GitHubClientError(
        operation="gh api graphql",
        returncode=1,
        stderr="gh: Requires authentication (HTTP 401)",
    )
    # The unconfirmed addressed verdict the resolve_thread caller has not yet
    # rolled back at the moment the wait runs.
    state = MonitorState(threads_addressed_ids={"PRRT_open": "fix_committed"})

    # Transient retry branch: returns True, persists the counter, but must NOT
    # flush the unconfirmed verdict.
    assert (
        await runner._wait_after_transient_github_error(
            exc,
            workspace_id=workspace_id,
            pr_number=42,
            context="resolve_thread",
            state=state,
            monitor_log=None,
        )
        is True
    )
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        persisted = ws.monitor_threads_addressed or {}
        assert persisted.get(_RESOLVE_THREAD_COUNT_KEY) == "1"
        assert "PRRT_open" not in persisted

    # Exhaustion branch: also persists only the counter, never the unconfirmed
    # verdict, so the terminal path cannot strand an addressed-but-unresolved
    # thread in the DB either.
    assert (
        await runner._wait_after_transient_github_error(
            exc,
            workspace_id=workspace_id,
            pr_number=42,
            context="resolve_thread",
            state=state,
            monitor_log=None,
        )
        is True
    )
    exhausted = await runner._wait_after_transient_github_error(
        exc,
        workspace_id=workspace_id,
        pr_number=42,
        context="resolve_thread",
        state=state,
        monitor_log=None,
    )
    assert exhausted is False
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        persisted = ws.monitor_threads_addressed or {}
        assert persisted.get(_RESOLVE_THREAD_COUNT_KEY) == "3"
        assert "PRRT_open" not in persisted


@pytest.mark.unit
async def test_deterministic_fault_clears_stale_prior_transient_counter(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A deterministic forge fault must clear a stale counter left by a *prior*
    recovered transient blip in the same context.

    The non-terminal callers (``resolve_thread`` / ``capture_deferred_thread``)
    do not terminate on a deterministic fault — they keep polling and the runner
    later persists ``state``. If the deterministic early return left the prior
    transient counter behind, a later *unrelated* transient in the same context
    would resume from the stale count and exhaust the bounded budget immediately,
    downgrading or terminating an otherwise-healthy monitor. Only the deterministic
    case clears the counter; the exhausted-transient path keeps it (still
    transient → fail closed).
    """
    workspace_id = await seed_monitoring_workspace(factory)
    sleep_fn = RecordedSleep()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
    )
    object.__setattr__(runner._runner_config, "transient_forge_max_retries", 2)
    transient_exc = GitHubClientError(
        operation="gh api graphql",
        returncode=1,
        stderr="gh: Requires authentication (HTTP 401)",
    )
    # A strong permanent marker ("bad credentials") => deterministic, never retried,
    # even though the same string also carries the ambiguous ``HTTP 401`` token.
    deterministic_exc = GitHubClientError(
        operation="gh api graphql",
        returncode=1,
        stderr="gh: Bad credentials (HTTP 401)",
    )
    state = MonitorState()

    # A prior recovered transient blip persists the counter to the DB.
    assert (
        await runner._wait_after_transient_github_error(
            transient_exc,
            workspace_id=workspace_id,
            pr_number=42,
            context="resolve_thread",
            state=state,
            monitor_log=None,
        )
        is True
    )
    assert state.threads_addressed_ids[_RESOLVE_THREAD_COUNT_KEY] == "1"

    # A deterministic fault then arrives in the same context: it returns False (no
    # retry, no backoff) AND clears the stale counter both in memory and over the DB.
    assert (
        await runner._wait_after_transient_github_error(
            deterministic_exc,
            workspace_id=workspace_id,
            pr_number=42,
            context="resolve_thread",
            state=state,
            monitor_log=None,
        )
        is False
    )
    assert _RESOLVE_THREAD_COUNT_KEY not in state.threads_addressed_ids
    assert sleep_fn.calls == [5]
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert _RESOLVE_THREAD_COUNT_KEY not in (ws.monitor_threads_addressed or {})

    # A later, independent transient starts from a fresh budget rather than
    # resuming the stale count and exhausting prematurely.
    assert (
        await runner._wait_after_transient_github_error(
            transient_exc,
            workspace_id=workspace_id,
            pr_number=42,
            context="resolve_thread",
            state=state,
            monitor_log=None,
        )
        is True
    )
    assert state.threads_addressed_ids[_RESOLVE_THREAD_COUNT_KEY] == "1"
