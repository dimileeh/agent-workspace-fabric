"""Resolve-hygiene for addressed review threads that became OUTDATED (#473).

When the monitor addresses a review thread by changing code ELSEWHERE (a
different file/line than the comment anchor), the forge marks the original
thread ``isOutdated=true`` and both forge clients drop it from
``PRStatus.unresolved_inline_threads`` (outdated threads are non-blocking for
merge). The fix-cycle resolve loop only iterates that actionable feed, so the
addressed thread is never resolved and lingers as "unresolved" on a merged PR.

``_resolve_addressed_outdated_threads`` closes that gap forge-neutrally: it
iterates ``PRStatus.outdated_unresolved_inline_threads`` and resolves closed
verdicts (``fix_committed`` / ``false_positive``) plus durably captured
``defer``. Uncaptured ``defer`` / ``needs_human`` / ``agent_failed`` stay open.

Part 1 of 3 — the #473 resolve-hygiene cases. Part 2 holds the #547 / #548
comment-keyed reconcile cases; part 3 holds the #484 branch-evidence seeding
cases and the pure-helper unit tests. Shared builders live in ``._helpers``; the
PostgreSQL ``factory`` fixture lives in the package ``conftest``.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.bitbucket_client import (
    BITBUCKET_API_ERROR,
    BITBUCKET_RATE_LIMITED,
    BitbucketClientError,
)
from awf.common.commands import FakeCommandRunner
from awf.common.forge_errors import ForgeClientError
from awf.common.github_client import GitHubClientError
from awf.db.repositories import WorkspaceRepository
from awf.runtime.monitor_state_keys import _outdated_resolve_requeued_key
from awf.runtime.pr_monitor import (
    CheckState,
    Merge,
    MergeableState,
    MergeStateStatus,
    MonitorConfig,
    MonitorState,
    NotifyHuman,
    PRStatus,
    ReviewThread,
    ReviewThreadComment,
    WaitForCI,
    _mark_review_thread_addressed,
    _review_thread_body_hash,
    decide,
)
from awf.runtime.pr_monitor_runner.fix_cycle import _deferred_issue_filed_marker
from awf.runtime.pr_monitor_runner.outdated_resolution import (
    _OUTDATED_RESOLVABLE_THREAD_VERDICTS,
    _outdated_thread_is_resolvable,
)
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)
from tests.unit.runtime.test_pr_monitor_outdated_resolution_parts._helpers import (
    _call_resolve,
    _outdated_thread,
    _RecordingGitHub,
    _resolution_events,
    _status_with_outdated,
)


@pytest.mark.unit
async def test_outdated_addressed_thread_is_resolved(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """(a) #473 regression — a ``fix_committed`` thread that became OUTDATED (only
    in ``outdated_unresolved_inline_threads``) IS resolved. Pins the PR #470
    scenario: feedback addressed by an edit elsewhere, thread went outdated."""
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    gh = _RecordingGitHub(cmd)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    state = MonitorState()
    thread = _outdated_thread("T_outdated")
    _mark_review_thread_addressed(state, thread, "fix_committed")

    await _call_resolve(
        runner,
        workspace_id=workspace_id,
        status=_status_with_outdated(thread),
        state=state,
    )

    assert gh.resolved == ["T_outdated"]
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        succeeded = _resolution_events(ws, outcome="succeeded")
        assert len(succeeded) == 1
        assert succeeded[0].reason_code == "COMMENT_REPAIR"


@pytest.mark.unit
async def test_successful_resolve_clears_prior_transient_requeue_flag(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A resolve that lands after an earlier poll's transient fault must clear the
    requeue flag so ``decide`` stops holding the thread at ``NotifyHuman`` — the
    block exists only until the resolve succeeds."""
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    gh = _RecordingGitHub(cmd)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    state = MonitorState()
    thread = _outdated_thread("T_outdated")
    _mark_review_thread_addressed(state, thread, "fix_committed")
    # Simulate a requeue flag left by a prior poll's transient fault.
    state.threads_addressed_ids[_outdated_resolve_requeued_key("T_outdated")] = "requeued"

    await _call_resolve(
        runner,
        workspace_id=workspace_id,
        status=_status_with_outdated(thread),
        state=state,
    )

    assert gh.resolved == ["T_outdated"]
    assert _outdated_resolve_requeued_key("T_outdated") not in state.threads_addressed_ids


@pytest.mark.unit
async def test_false_positive_outdated_thread_is_resolved(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """``false_positive`` is resolvable like ``fix_committed`` — both mean the
    thread should close with no human follow-up."""
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    gh = _RecordingGitHub(cmd)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    state = MonitorState()
    thread = _outdated_thread("T_fp")
    _mark_review_thread_addressed(state, thread, "false_positive")

    await _call_resolve(
        runner,
        workspace_id=workspace_id,
        status=_status_with_outdated(thread),
        state=state,
    )

    assert gh.resolved == ["T_fp"]


@pytest.mark.unit
async def test_in_place_thread_is_not_double_resolved(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """(b) An in-place ``fix_committed`` thread (still in the actionable feed, not
    the outdated feed) is resolved by the existing fix-cycle path and is NOT
    touched by this step — the step only reads the outdated feed."""
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    gh = _RecordingGitHub(cmd)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    state = MonitorState()
    state.mark_addressed("T_inplace", "fix_committed")

    # An in-place thread stays in ``unresolved_inline_threads`` and is absent
    # from the outdated feed, so this step finds nothing to do.
    status = PRStatus(
        number=42,
        head_sha="abc1234567890def",
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=(
            ReviewThread(
                thread_id="T_inplace",
                path="src/anchor.py",
                line=7,
                body_excerpt="please fix this finding",
                author="greptile",
            ),
        ),
        unresolved_review_comments=(),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
    )

    await _call_resolve(runner, workspace_id=workspace_id, status=status, state=state)

    assert gh.attempts == []
    assert gh.resolved == []


@pytest.mark.unit
@pytest.mark.parametrize("verdict", ["defer", "needs_human", "agent_failed"])
async def test_keep_open_verdicts_are_not_resolved(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    verdict: str,
) -> None:
    """(c) Outdated threads recorded ``needs_human`` / ``agent_failed``, or
    ``defer`` without durable capture, are NOT resolved here — they stay open.
    Captured outdated defer is covered by
    ``test_durably_captured_defer_outdated_thread_is_resolved``."""
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    gh = _RecordingGitHub(cmd)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    state = MonitorState()
    state.mark_addressed("T_keep_open", verdict)

    await _call_resolve(
        runner,
        workspace_id=workspace_id,
        status=_status_with_outdated(_outdated_thread("T_keep_open")),
        state=state,
    )

    assert gh.attempts == []


@pytest.mark.unit
async def test_durably_captured_defer_outdated_thread_is_resolved(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """R5 ownership handoff: already-outdated captured ``defer`` has no in-cycle
    resolve owner, so hygiene must close it once the tracking-issue marker is
    present — otherwise decide permanently NotifyHuman-wedges the PR."""
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    gh = _RecordingGitHub(cmd)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    state = MonitorState()
    thread = _outdated_thread("T_defer_captured")
    _mark_review_thread_addressed(state, thread, "defer")
    marker = _deferred_issue_filed_marker(
        thread.thread_id,
        _review_thread_body_hash(thread),
    )
    state.mark_addressed(marker, "https://github.example/issues/901")

    status = _status_with_outdated(thread)
    assert _outdated_thread_is_resolvable(state, thread)
    filtered = await _call_resolve(
        runner,
        workspace_id=workspace_id,
        status=status,
        state=state,
    )

    assert gh.resolved == ["T_defer_captured"]
    # Same-poll decide must not see the forge-resolved defer still sitting in
    # the pre-resolution snapshot (PRRT_kwDOSJAM6s6dcnGv).
    assert filtered.outdated_unresolved_inline_threads == ()
    action = decide(status=filtered, state=state, config=MonitorConfig(auto_merge=True))
    assert isinstance(action, Merge)


@pytest.mark.unit
async def test_successful_outdated_resolve_under_blocked_defers_stale_merge_state(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Required-conversation BLOCKED must not survive a successful resolve.

    Filtering resolved IDs alone leaves the pre-mutation ``merge_state_status``
    authoritative; same-poll ``decide`` then hits gate 9 and pages a human for a
    blocker AWF just cleared (PRRT_kwDOSJAM6s6dfH8j). Invalidate mergeability so
    the poll defers quietly until the next fetch.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    gh = _RecordingGitHub(cmd)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    state = MonitorState()
    thread = _outdated_thread("T_blocked_resolve")
    _mark_review_thread_addressed(state, thread, "fix_committed")

    status = replace(
        _status_with_outdated(thread),
        merge_state_status=MergeStateStatus.BLOCKED,
    )
    filtered = await _call_resolve(
        runner,
        workspace_id=workspace_id,
        status=status,
        state=state,
    )

    assert gh.resolved == ["T_blocked_resolve"]
    assert filtered.outdated_unresolved_inline_threads == ()
    assert filtered.merge_state_status is MergeStateStatus.UNKNOWN
    action = decide(status=filtered, state=state, config=MonitorConfig(auto_merge=True))
    assert isinstance(action, WaitForCI)
    assert not isinstance(action, NotifyHuman)


@pytest.mark.unit
async def test_duplicate_outdated_captured_defer_resolves_once(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Transport may repeat the same outdated thread ID; hygiene must resolve once.

    Canonical policy tolerates duplicate nodes. Without a seen-ID guard, each
    copy that passes the captured-defer predicate would call ``resolve_thread``
    again — wasting forge calls and risking a post-success permanent fault that
    downgrades the verdict to ``needs_human``.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    gh = _RecordingGitHub(cmd)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    state = MonitorState()
    first = _outdated_thread("T_defer_dup")
    # Identical second transport node for the same conversation (same body hash
    # so the captured-defer predicate passes for every copy).
    second = _outdated_thread("T_defer_dup")
    _mark_review_thread_addressed(state, first, "defer")
    marker = _deferred_issue_filed_marker(
        first.thread_id,
        _review_thread_body_hash(first),
    )
    state.mark_addressed(marker, "https://github.example/issues/902")

    assert _outdated_thread_is_resolvable(state, first)
    assert _outdated_thread_is_resolvable(state, second)

    await _call_resolve(
        runner,
        workspace_id=workspace_id,
        status=_status_with_outdated(first, second),
        state=state,
    )

    assert gh.attempts == ["T_defer_dup"]
    assert gh.resolved == ["T_defer_dup"]


@pytest.mark.unit
async def test_duplicate_outdated_refuses_resolve_when_any_copy_needs_attention(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Within-outdated transport duplicates must not resolve on a stale match.

    When the feed repeats a thread ID and a richer node carries a newer reviewer
    reply, resolving from an earlier body-hash match would close the shared
    forge conversation and drop the fresh reply on the next poll. Hygiene must
    inspect the preferred representation and refuse if it needs attention.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    gh = _RecordingGitHub(cmd)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    state = MonitorState()
    stale = _outdated_thread("T_dup_fresh", body_excerpt="addressed body")
    _mark_review_thread_addressed(state, stale, "fix_committed")
    fresher = ReviewThread(
        thread_id="T_dup_fresh",
        path="src/anchor.py",
        line=7,
        body_excerpt="new feedback after address",
        author="greptile",
        is_resolved=False,
        is_outdated=True,
        comments=(
            ReviewThreadComment(
                comment_id="1",
                body="addressed body",
                author="bot",
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
            ),
            ReviewThreadComment(
                comment_id="2",
                body="new feedback after address",
                author="reviewer",
                created_at=datetime(2026, 1, 2, tzinfo=UTC),
            ),
        ),
    )

    await _call_resolve(
        runner,
        workspace_id=workspace_id,
        status=_status_with_outdated(stale, fresher),
        state=state,
    )

    assert gh.attempts == []


@pytest.mark.unit
async def test_duplicate_outdated_resolves_when_freshest_copy_is_settled(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A stale ghost must not block resolve after the preferred body is settled.

    Once the fresher hash is recorded, walking every transport copy for
    needs-attention would refuse forever on the mismatched older sibling.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    gh = _RecordingGitHub(cmd)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    state = MonitorState()
    stale = _outdated_thread("T_dup_settled", body_excerpt="addressed body")
    fresher = ReviewThread(
        thread_id="T_dup_settled",
        path="src/anchor.py",
        line=7,
        body_excerpt="new feedback after address",
        author="greptile",
        is_resolved=False,
        is_outdated=True,
        comments=(
            ReviewThreadComment(
                comment_id="1",
                body="addressed body",
                author="bot",
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
            ),
            ReviewThreadComment(
                comment_id="2",
                body="new feedback after address",
                author="reviewer",
                created_at=datetime(2026, 1, 2, tzinfo=UTC),
            ),
        ),
    )
    _mark_review_thread_addressed(state, fresher, "fix_committed")

    await _call_resolve(
        runner,
        workspace_id=workspace_id,
        status=_status_with_outdated(stale, fresher),
        state=state,
    )

    assert gh.attempts == ["T_dup_settled"]
    assert gh.resolved == ["T_dup_settled"]


@pytest.mark.unit
async def test_unaddressed_outdated_thread_is_not_resolved(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """An outdated thread the monitor never recorded a verdict for is left alone —
    only threads the monitor itself addressed are resolved."""
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    gh = _RecordingGitHub(cmd)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )

    await _call_resolve(
        runner,
        workspace_id=workspace_id,
        status=_status_with_outdated(_outdated_thread("T_unknown")),
        state=MonitorState(),
    )

    assert gh.attempts == []


@pytest.mark.unit
async def test_outdated_thread_with_fresh_reply_is_not_resolved(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A ``fix_committed`` outdated thread that gained a NEW reviewer reply after
    the verdict (its body hash changed) is left open — mirroring the fix-cycle's
    stale-thread guard. Resolving it would close feedback the monitor never
    re-handled: outdated threads are dropped from the actionable feed, so the fix
    cycle never re-addresses them either."""
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    gh = _RecordingGitHub(cmd)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    state = MonitorState()
    addressed = _outdated_thread("T_reply")
    _mark_review_thread_addressed(state, addressed, "fix_committed")
    # A new reviewer reply lands on the now-outdated thread: same id + verdict,
    # but a changed body, so the recorded body hash no longer matches.
    with_reply = _outdated_thread("T_reply", body_excerpt="actually this is still broken")

    await _call_resolve(
        runner,
        workspace_id=workspace_id,
        status=_status_with_outdated(with_reply),
        state=state,
    )

    assert gh.attempts == []


@pytest.mark.unit
async def test_outdated_resolve_skips_ids_still_active(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Active-wins must protect hygiene, not only ``decide``.

    Same thread ID in both feeds: the stale outdated copy still matches the
    recorded body hash (so ``_outdated_thread_is_resolvable`` + the changed-body
    guard would accept it), while the active copy carries new feedback.
    Resolving the outdated copy closes the shared conversation before
    ``decide()`` can route the canonical active copy to AddressComments.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    gh = _RecordingGitHub(cmd)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    state = MonitorState()
    stale_outdated = _outdated_thread("T_dup", body_excerpt="addressed body")
    _mark_review_thread_addressed(state, stale_outdated, "fix_committed")
    active = ReviewThread(
        thread_id="T_dup",
        path="src/anchor.py",
        line=7,
        body_excerpt="new feedback after address",
        author="greptile",
        is_resolved=False,
        is_outdated=False,
    )
    status = PRStatus(
        number=42,
        head_sha="abc1234567890def",
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=(active,),
        unresolved_review_comments=(),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
        outdated_unresolved_inline_threads=(stale_outdated,),
    )

    await _call_resolve(
        runner,
        workspace_id=workspace_id,
        status=status,
        state=state,
    )

    assert gh.attempts == []


@pytest.mark.unit
async def test_bitbucket_outdated_thread_resolves_via_resolve_thread(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """(d) Forge-neutral: a Bitbucket-style addressed-outdated thread resolves via
    ``resolve_thread`` (the client-level POST/never-DELETE semantics are covered
    in the Bitbucket client tests)."""
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    gh = _RecordingGitHub(cmd)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    state = MonitorState()
    bb_id = "bb:workspace/repo#42:100"
    thread = _outdated_thread(bb_id)
    _mark_review_thread_addressed(state, thread, "fix_committed")

    await _call_resolve(
        runner,
        workspace_id=workspace_id,
        status=_status_with_outdated(thread),
        state=state,
    )

    assert gh.resolved == [bb_id]


@pytest.mark.unit
async def test_transient_resolve_error_is_requeued_not_failed(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """(e) A transient resolve fault waits and is left for the next poll (the
    thread stays in the outdated set); the monitor does not crash and records a
    ``requeued`` audit event with the forge-native retry reason code."""
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    transient = GitHubClientError(
        operation="resolve_thread",
        returncode=1,
        stderr="HTTP 503: service unavailable",
    )
    gh = _RecordingGitHub(cmd, error=transient)
    sleep_fn = RecordedSleep()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    state = MonitorState()
    thread = _outdated_thread("T_transient")
    _mark_review_thread_addressed(state, thread, "fix_committed")

    await _call_resolve(
        runner,
        workspace_id=workspace_id,
        status=_status_with_outdated(thread),
        state=state,
    )

    assert gh.resolved == []
    # The addressed marker is preserved so the next poll retries the resolve.
    assert state.threads_addressed_ids["T_transient"] == "fix_committed"
    # The thread is flagged requeued so decide() blocks merge this iteration.
    assert state.threads_addressed_ids[_outdated_resolve_requeued_key("T_transient")] == "requeued"
    assert sleep_fn.calls  # the transient handler waited before re-polling
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        requeued = _resolution_events(ws, outcome="requeued")
        assert len(requeued) == 1
        assert requeued[0].reason_code == "GITHUB_TRANSIENT_RETRY"


@pytest.mark.unit
async def test_transient_resolve_without_wait_still_sets_requeue(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Merge-lock hygiene must record the requeue blocker without sleeping.

    Pre-merge outdated resolve runs under ``serialized_merge``; sleeping there
    stalls every other PR for the same base (PRRT_kwDOSJAM6s6dfBzK).
    """
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    transient = GitHubClientError(
        operation="resolve_thread",
        returncode=1,
        stderr="HTTP 503: service unavailable",
    )
    gh = _RecordingGitHub(cmd, error=transient)
    sleep_fn = RecordedSleep()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    state = MonitorState()
    thread = _outdated_thread("T_no_wait")
    _mark_review_thread_addressed(state, thread, "fix_committed")

    await _call_resolve(
        runner,
        workspace_id=workspace_id,
        status=_status_with_outdated(thread),
        state=state,
        wait_on_transient=False,
    )

    assert gh.resolved == []
    assert state.threads_addressed_ids["T_no_wait"] == "fix_committed"
    assert state.threads_addressed_ids[_outdated_resolve_requeued_key("T_no_wait")] == "requeued"
    assert sleep_fn.calls == []
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        requeued = _resolution_events(ws, outcome="requeued")
        assert len(requeued) == 1
        assert requeued[0].reason_code == "GITHUB_TRANSIENT_RETRY"


@pytest.mark.unit
async def test_bitbucket_transient_resolve_without_wait_still_sets_requeue(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Bitbucket transient + wait_on_transient=False classifies without sleeping.

    Pre-merge hygiene must use the Bitbucket transient classifier (not the
    wait-and-backoff path) so a rate-limit under ``serialized_merge`` still
    records the requeue blocker and releases the merge lock promptly.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    transient = BitbucketClientError(
        operation="bitbucket resolve_thread",
        status=429,
        body="rate limited",
        reason_code=BITBUCKET_RATE_LIMITED,
    )
    gh = _RecordingGitHub(cmd, error=transient)
    sleep_fn = RecordedSleep()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    state = MonitorState()
    bb_id = "bb:workspace/repo#42:101"
    thread = _outdated_thread(bb_id)
    _mark_review_thread_addressed(state, thread, "fix_committed")

    await _call_resolve(
        runner,
        workspace_id=workspace_id,
        status=_status_with_outdated(thread),
        state=state,
        wait_on_transient=False,
    )

    assert gh.resolved == []
    assert state.threads_addressed_ids[bb_id] == "fix_committed"
    assert state.threads_addressed_ids[_outdated_resolve_requeued_key(bb_id)] == "requeued"
    assert sleep_fn.calls == []
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        requeued = _resolution_events(ws, outcome="requeued")
        assert len(requeued) == 1
        assert requeued[0].reason_code == "BITBUCKET_TRANSIENT_RETRY"


@pytest.mark.unit
async def test_unknown_forge_error_without_wait_downgrades_to_needs_human(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Non-GitHub/Bitbucket ForgeClientError with no-wait is treated as permanent.

    The no-wait classifier only knows GH/BB subclasses; any other forge fault
    must not be requeued as transient (that would spin forever without a
    forge-specific recovery path).
    """
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    unknown = ForgeClientError("unexpected forge transport fault")
    gh = _RecordingGitHub(cmd, error=unknown)
    sleep_fn = RecordedSleep()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    state = MonitorState()
    thread = _outdated_thread("T_unknown_forge")
    _mark_review_thread_addressed(state, thread, "fix_committed")

    await _call_resolve(
        runner,
        workspace_id=workspace_id,
        status=_status_with_outdated(thread),
        state=state,
        wait_on_transient=False,
    )

    assert gh.resolved == []
    assert state.threads_addressed_ids["T_unknown_forge"] == "needs_human"
    assert _outdated_resolve_requeued_key("T_unknown_forge") not in state.threads_addressed_ids
    assert sleep_fn.calls == []
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        downgraded = _resolution_events(ws, outcome="needs_human")
        assert len(downgraded) == 1
        assert downgraded[0].reason_code == "FORGE_CLIENT_ERROR"


@pytest.mark.unit
async def test_transient_resolve_error_blocks_merge_same_iteration(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """(e1) The promised next-poll retry is only meaningful if merge does not race
    it. ``_resolve_addressed_outdated_threads`` runs in the same iteration right
    before ``decide``; after a transient resolve fault the fix verdict survives
    (non-blocking) but the requeue flag must make ``decide`` hold the merge-ready
    PR at ``NotifyHuman`` so the addressed-but-unresolved outdated thread is not
    merged over before the retry runs."""
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    transient = GitHubClientError(
        operation="resolve_thread",
        returncode=1,
        stderr="HTTP 503: service unavailable",
    )
    gh = _RecordingGitHub(cmd, error=transient)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    state = MonitorState()
    thread = _outdated_thread("T_transient")
    _mark_review_thread_addressed(state, thread, "fix_committed")
    status = _status_with_outdated(thread)

    await _call_resolve(runner, workspace_id=workspace_id, status=status, state=state)

    # The merge-ready snapshot would merge but for the requeue flag.
    action = decide(status=status, state=state, config=MonitorConfig(auto_merge=True))
    assert isinstance(action, NotifyHuman)


@pytest.mark.unit
async def test_permanent_resolve_error_downgrades_to_needs_human(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """(e) A permanent resolve fault does not crash the monitor. Rather than
    preserving the resolvable verdict (which would re-issue the same failing
    resolve every poll — a retry storm), the verdict is downgraded to
    ``needs_human`` and a ``needs_human`` audit event carrying the forge-native
    reason code is recorded. The thread is non-blocking, so this never wedges
    auto-merge; it simply stops the pointless retries."""
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    permanent = BitbucketClientError(
        operation="bitbucket resolve_thread",
        status=403,
        body="thread resolution is not permitted for this token",
        reason_code=BITBUCKET_API_ERROR,
    )
    gh = _RecordingGitHub(cmd, error=permanent)
    sleep_fn = RecordedSleep()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    state = MonitorState()
    bb_id = "bb:workspace/repo#42:100"
    thread = _outdated_thread(bb_id)
    _mark_review_thread_addressed(state, thread, "fix_committed")
    # A prior poll's transient fault left a requeue flag; the permanent downgrade
    # must clear it (``needs_human`` blocks on its own — leaving the flag would be
    # redundant stale state).
    state.threads_addressed_ids[_outdated_resolve_requeued_key(bb_id)] = "requeued"

    await _call_resolve(
        runner,
        workspace_id=workspace_id,
        status=_status_with_outdated(thread),
        state=state,
    )

    assert gh.resolved == []
    # Verdict downgraded so the next poll skips it (not in the resolvable set).
    assert state.threads_addressed_ids[bb_id] == "needs_human"
    assert _outdated_resolve_requeued_key(bb_id) not in state.threads_addressed_ids
    assert "needs_human" not in _OUTDATED_RESOLVABLE_THREAD_VERDICTS
    assert sleep_fn.calls == []  # permanent fault does not wait/retry
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        downgraded = _resolution_events(ws, outcome="needs_human")
        assert len(downgraded) == 1
        assert downgraded[0].reason_code == BITBUCKET_API_ERROR
        assert (downgraded[0].payload or {}).get("evidence", {}).get(
            "needs_human_thread_count"
        ) == 1


@pytest.mark.unit
async def test_permanent_resolve_error_is_not_retried_next_poll(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """(e2) After a permanent resolve fault downgrades the verdict to
    ``needs_human``, a subsequent poll that still surfaces the outdated thread
    must NOT re-issue the resolve — otherwise a non-fixable fault would spam the
    forge API and logs on every cycle until the (non-blocking) PR merges."""
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    permanent = BitbucketClientError(
        operation="bitbucket resolve_thread",
        status=403,
        body="thread resolution is not permitted for this token",
        reason_code=BITBUCKET_API_ERROR,
    )
    gh = _RecordingGitHub(cmd, error=permanent)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    state = MonitorState()
    bb_id = "bb:workspace/repo#42:100"
    thread = _outdated_thread(bb_id)
    _mark_review_thread_addressed(state, thread, "fix_committed")
    status = _status_with_outdated(thread)

    # First poll: permanent fault, one attempt, downgrade to needs_human.
    await _call_resolve(runner, workspace_id=workspace_id, status=status, state=state)
    # Second poll: the thread is still outdated/unresolved, but the verdict is now
    # needs_human, so the step must skip it without a second forge call.
    await _call_resolve(runner, workspace_id=workspace_id, status=status, state=state)

    assert gh.attempts == [bb_id]  # exactly one resolve attempt across both polls


@pytest.mark.unit
async def test_permanent_resolve_downgrade_survives_state_reload(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """(e3) The ``needs_human`` downgrade must be PERSISTED, not just held in
    memory. This step runs before ``_execute``, which skips ``_persist_state`` on
    a transient fault and reloads clean state from the DB next poll. If the
    downgrade lived only in memory it would be lost on that path, and the next
    poll would re-issue the same known-permanent resolve — the retry storm the
    downgrade exists to prevent. Unlike (e2), which reused one in-memory ``state``
    across both polls, this reloads state from the DB between polls to mirror the
    real loop (``state = self._load_state(ws)``)."""
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    permanent = BitbucketClientError(
        operation="bitbucket resolve_thread",
        status=403,
        body="thread resolution is not permitted for this token",
        reason_code=BITBUCKET_API_ERROR,
    )
    gh = _RecordingGitHub(cmd, error=permanent)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    state = MonitorState()
    bb_id = "bb:workspace/repo#42:100"
    thread = _outdated_thread(bb_id)
    _mark_review_thread_addressed(state, thread, "fix_committed")
    status = _status_with_outdated(thread)

    # First poll: permanent fault, one attempt, downgrade to needs_human (persisted).
    await _call_resolve(runner, workspace_id=workspace_id, status=status, state=state)

    # Next poll mirrors the loop: reload state from the DB rather than reusing the
    # in-memory object. The persisted downgrade must come back as needs_human.
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        reloaded = runner._load_state(ws)  # type: ignore[attr-defined]
    assert reloaded.threads_addressed_ids[bb_id] == "needs_human"

    # Second poll with the reloaded state: the verdict is needs_human, so the step
    # skips it without a second forge call (no retry storm).
    await _call_resolve(runner, workspace_id=workspace_id, status=status, state=reloaded)

    assert gh.attempts == [bb_id]  # exactly one resolve attempt across both polls
