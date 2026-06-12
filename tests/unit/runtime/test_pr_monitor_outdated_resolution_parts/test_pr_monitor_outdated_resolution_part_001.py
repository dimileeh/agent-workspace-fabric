"""Resolve-hygiene for addressed review threads that became OUTDATED (#473).

When the monitor addresses a review thread by changing code ELSEWHERE (a
different file/line than the comment anchor), the forge marks the original
thread ``isOutdated=true`` and both forge clients drop it from
``PRStatus.unresolved_inline_threads`` (outdated threads are non-blocking for
merge). The fix-cycle resolve loop only iterates that actionable feed, so the
addressed thread is never resolved and lingers as "unresolved" on a merged PR.

``_resolve_addressed_outdated_threads`` closes that gap forge-neutrally: it
iterates ``PRStatus.outdated_unresolved_inline_threads`` and resolves only the
threads the monitor already recorded with a fix verdict
(``fix_committed`` / ``false_positive``). ``defer`` / ``needs_human`` /
``agent_failed`` threads legitimately stay open.

Part 1 of 2 — the #473 resolve-hygiene cases, the #484 branch-evidence seeding
cases, and the pure-helper unit tests. Part 2 holds the #547 / #548
comment-keyed reconcile cases. Shared builders live in ``._helpers``; the
PostgreSQL ``factory`` fixture lives in the package ``conftest``.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.bitbucket_client import BITBUCKET_API_ERROR, BitbucketClientError
from awf.common.commands import FakeCommandRunner
from awf.common.github_client import GitHubClientError
from awf.db.repositories import WorkspaceRepository
from awf.runtime.monitor_state_keys import _outdated_resolve_requeued_key
from awf.runtime.pr_monitor import (
    _CLOSED_OUTDATED_THREAD_VERDICTS,
    CheckState,
    MergeableState,
    MergeStateStatus,
    MonitorConfig,
    MonitorState,
    NotifyHuman,
    PRStatus,
    ReviewThread,
    ReviewThreadComment,
    _mark_review_thread_addressed,
    decide,
)
from awf.runtime.pr_monitor_runner.fix_cycle import _RESOLVABLE_THREAD_VERDICTS
from awf.runtime.pr_monitor_runner.outdated_resolution import (
    _OUTDATED_RESOLVABLE_THREAD_VERDICTS,
    _grep_id_pattern,
    _latest_reviewer_comment_at,
    _parse_commit_iso,
    _thread_identifier_set,
)
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)
from tests.unit.runtime.test_pr_monitor_outdated_resolution_parts._helpers import (
    _call_resolve,
    _grep_argv,
    _outdated_thread,
    _outdated_thread_with_comment,
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
    """(c) Outdated threads recorded ``defer`` / ``needs_human`` / ``agent_failed``
    are NOT resolved here — they legitimately stay open (defer is capture-gated,
    the others need a human)."""
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


@pytest.mark.unit
async def test_outdated_thread_seeded_from_branch_evidence_is_resolved(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """(a) #484 regression — after a re-adoption / instance handoff, an outdated
    thread whose ``fix: address PRRT_…`` commit is already on the branch head but
    which has NO verdict in this instance's ``threads_addressed_ids`` is seeded
    from that durable branch evidence and resolved THIS iteration — not looped on
    ``NotifyHuman`` forever."""
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
    # Empty state — the prior instance addressed+outdated the thread, this one has
    # no record of it.
    state = MonitorState()
    thread = _outdated_thread("PRRT_kwDOSJAM6s6IMBmJ")
    # The branch head carries the prior instance's fix: address commit; ``%aI`` emits
    # its author time (non-empty => a match). The thread has no reviewer comments,
    # so there is no post-fix activity to compare against and the seed is fix_committed.
    cmd.queue_result(returncode=0, stdout="2026-06-10T09:00:00+00:00\n")

    await _call_resolve(
        runner,
        workspace_id=workspace_id,
        status=_status_with_outdated(thread),
        state=state,
    )

    assert gh.resolved == ["PRRT_kwDOSJAM6s6IMBmJ"]
    assert state.threads_addressed_ids["PRRT_kwDOSJAM6s6IMBmJ"] == "fix_committed"
    worktree = tmp_path / "worktrees" / workspace_id
    assert [c.args for c in cmd.calls] == [_grep_argv(worktree, "PRRT_kwDOSJAM6s6IMBmJ")]
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        succeeded = _resolution_events(ws, outcome="succeeded")
        assert len(succeeded) == 1
        assert succeeded[0].reason_code == "COMMENT_REPAIR"


@pytest.mark.unit
async def test_outdated_thread_without_branch_evidence_is_left_open(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """No ``fix: address`` commit references the thread → no verdict is seeded and
    the thread is left open (the monitor never claims a fix the branch lacks)."""
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
    thread = _outdated_thread("PRRT_unknown")
    # Empty stdout: no commit on HEAD matches both the thread id and ``fix: address``.
    cmd.queue_result(returncode=0, stdout="")

    await _call_resolve(
        runner,
        workspace_id=workspace_id,
        status=_status_with_outdated(thread),
        state=state,
    )

    assert gh.attempts == []
    assert "PRRT_unknown" not in state.threads_addressed_ids
    worktree = tmp_path / "worktrees" / workspace_id
    assert [c.args for c in cmd.calls] == [_grep_argv(worktree, "PRRT_unknown")]


@pytest.mark.unit
async def test_outdated_thread_seeding_git_failure_is_best_effort(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A git read failure during seeding is best-effort: no crash, no verdict, no
    resolve. The thread stays non-blocking via the existing path and a later poll
    can re-seed once the worktree read succeeds."""
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
    thread = _outdated_thread("PRRT_giterr")
    # Non-zero return: the worktree read failed (e.g. transient lock / missing ref).
    cmd.queue_result(returncode=1, stdout="", stderr="fatal: not a git repository")

    await _call_resolve(
        runner,
        workspace_id=workspace_id,
        status=_status_with_outdated(thread),
        state=state,
    )

    assert gh.attempts == []
    assert "PRRT_giterr" not in state.threads_addressed_ids


@pytest.mark.unit
async def test_already_seeded_outdated_thread_skips_branch_evidence_grep(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """An outdated thread that already carries a ``fix_committed`` verdict (recorded
    by THIS instance) resolves through the existing #473 path WITHOUT issuing the
    seeding grep — the git read runs only for unseeded outdated threads, so it does
    not recur in steady state."""
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
    thread = _outdated_thread("PRRT_seeded")
    _mark_review_thread_addressed(state, thread, "fix_committed")

    await _call_resolve(
        runner,
        workspace_id=workspace_id,
        status=_status_with_outdated(thread),
        state=state,
    )

    assert gh.resolved == ["PRRT_seeded"]
    # No git grep was issued — the thread already had a verdict.
    assert cmd.calls == []


@pytest.mark.unit
async def test_outdated_thread_with_post_fix_reply_is_not_resolved(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """(PRRT_kwDOSJAM6s6IbIvo) A reviewer reply that postdates the matching fix
    commit — landing AFTER the prior instance's fix but BEFORE this re-adoption —
    must NOT be silently resolved/merged over. The seed compares the fix commit
    time (``%aI``) against the newest reviewer comment and, when the comment is
    newer, seeds ``needs_human`` instead of ``fix_committed``: the resolve loop
    leaves the thread open and ``decide`` blocks merge on it."""
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
    thread = _outdated_thread_with_comment(
        "PRRT_postfix",
        comment_at=datetime(2026, 6, 10, 9, 30, tzinfo=UTC),
    )
    # The matching fix commit was AUTHORED before the reviewer's reply.
    cmd.queue_result(returncode=0, stdout="2026-06-10T09:00:00+00:00\n")

    status = _status_with_outdated(thread)
    await _call_resolve(runner, workspace_id=workspace_id, status=status, state=state)

    # Not resolved — the fresh reply was never re-triaged.
    assert gh.attempts == []
    assert state.threads_addressed_ids["PRRT_postfix"] == "needs_human"
    # ``decide`` holds the merge-ready PR at NotifyHuman so a human sees the reply.
    action = decide(status=status, state=state, config=MonitorConfig(auto_merge=True))
    assert isinstance(action, NotifyHuman)


@pytest.mark.unit
async def test_outdated_thread_with_pre_fix_comment_is_seeded_and_resolved(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A reviewer comment that PREDATES the matching fix commit is exactly the
    feedback the fix addressed, so the seed stays ``fix_committed`` and the thread
    is resolved — the #484 unblock is preserved for the common case."""
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
    thread = _outdated_thread_with_comment(
        "PRRT_prefix",
        comment_at=datetime(2026, 6, 10, 8, 0, tzinfo=UTC),
    )
    # The matching fix commit was AUTHORED after the reviewer's original finding.
    cmd.queue_result(returncode=0, stdout="2026-06-10T09:00:00+00:00\n")

    await _call_resolve(
        runner,
        workspace_id=workspace_id,
        status=_status_with_outdated(thread),
        state=state,
    )

    assert gh.resolved == ["PRRT_prefix"]
    assert state.threads_addressed_ids["PRRT_prefix"] == "fix_committed"


@pytest.mark.unit
async def test_outdated_thread_post_fix_guard_uses_author_date_across_rebase(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """(PRRT_kwDOSJAM6s6IbdAc) The post-fix guard must order on the commit's AUTHOR
    date, not its committer date, so AWF's rebase recovery cannot defeat it. In the
    sequence fix commit → reviewer follow-up → ``git rebase`` recovery → re-adoption,
    the rebase rewrites the committer date to AFTER the follow-up while preserving
    the author date at the original fix time. A ``%cI`` ordering would seed
    ``fix_committed`` over the untriaged reply; ``%aI`` keeps the reply newer than
    the fix and seeds ``needs_human``. This test pins the format flag and asserts the
    rebase-rewritten-committer scenario still blocks merge."""
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
    thread = _outdated_thread_with_comment(
        "PRRT_rebased",
        comment_at=datetime(2026, 6, 10, 9, 30, tzinfo=UTC),
    )
    # git emits the AUTHOR date (``%aI``) — anchored to the original fix at 09:00,
    # BEFORE the 09:30 reply — even though rebase recovery later rewrote the
    # committer date to 10:00 (after the reply). The guard must see 09:00.
    cmd.queue_result(returncode=0, stdout="2026-06-10T09:00:00+00:00\n")

    status = _status_with_outdated(thread)
    await _call_resolve(runner, workspace_id=workspace_id, status=status, state=state)

    # The evidence grep requested the author date — the flag that makes the guard
    # rebase-proof. (A regression to ``%cI`` would surface the committer date and
    # silently resolve over the reply.)
    worktree = tmp_path / "worktrees" / workspace_id
    assert cmd.calls[0].args == _grep_argv(worktree, "PRRT_rebased", "c1")
    assert "--format=%aI" in cmd.calls[0].args
    # Reply postdates the (author-dated) fix → not resolved, merge blocked.
    assert gh.attempts == []
    assert state.threads_addressed_ids["PRRT_rebased"] == "needs_human"
    action = decide(status=status, state=state, config=MonitorConfig(auto_merge=True))
    assert isinstance(action, NotifyHuman)


@pytest.mark.unit
async def test_outdated_thread_unparseable_fix_commit_time_keeps_seed_baseline(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """When the matching commit's ``%aI`` is unparseable we cannot prove post-fix
    ordering, so the seed keeps the ``fix_committed`` baseline (the alternative —
    never seeding — leaves the PR permanently blocked) and the thread resolves."""
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
    thread = _outdated_thread_with_comment(
        "PRRT_badtime",
        comment_at=datetime(2026, 6, 10, 9, 30, tzinfo=UTC),
    )
    # A non-empty but unparseable author date: a match, but no usable ordering.
    cmd.queue_result(returncode=0, stdout="not-a-timestamp\n")

    await _call_resolve(
        runner,
        workspace_id=workspace_id,
        status=_status_with_outdated(thread),
        state=state,
    )

    assert gh.resolved == ["PRRT_badtime"]
    assert state.threads_addressed_ids["PRRT_badtime"] == "fix_committed"


@pytest.mark.unit
def test_latest_reviewer_comment_at_ignores_viewer_and_missing_stamps() -> None:
    """The post-fix ordering input considers only non-viewer comments with a usable
    timestamp: AWF's own (``viewer_did_author``) replies and stamp-less comments are
    skipped, and the newest of ``created_at`` / ``updated_at`` wins."""
    older = datetime(2026, 6, 10, 8, 0, tzinfo=UTC)
    newer = datetime(2026, 6, 10, 9, 0, tzinfo=UTC)
    viewer_newest = datetime(2026, 6, 10, 10, 0, tzinfo=UTC)
    thread = ReviewThread(
        thread_id="PRRT_mix",
        path="src/anchor.py",
        line=7,
        body_excerpt="finding",
        is_outdated=True,
        comments=(
            ReviewThreadComment(comment_id="a", body="x", created_at=older, updated_at=newer),
            ReviewThreadComment(comment_id="b", body="y", created_at=None, updated_at=None),
            # AWF's own reply is newest but must be ignored.
            ReviewThreadComment(
                comment_id="c",
                body="z",
                created_at=viewer_newest,
                updated_at=viewer_newest,
                viewer_did_author=True,
            ),
        ),
    )
    assert _latest_reviewer_comment_at(thread) == newer
    # A thread with no usable reviewer timestamps yields None (seed baseline).
    assert _latest_reviewer_comment_at(_outdated_thread("PRRT_empty")) is None


@pytest.mark.unit
def test_parse_commit_iso() -> None:
    """``%aI`` offset dates parse to UTC-aware datetimes; junk yields None."""
    assert _parse_commit_iso("2026-06-10T09:00:00+02:00") == datetime(2026, 6, 10, 7, 0, tzinfo=UTC)
    assert _parse_commit_iso("not-a-timestamp") is None


@pytest.mark.unit
def test_outdated_resolvable_verdicts_exclude_defer() -> None:
    """The outdated-resolution verdict set is the fix-cycle's resolvable set MINUS
    ``defer`` — defer's resolution is gated on durable capture this hygiene step
    cannot re-verify, so an outdated defer thread stays open."""
    assert "defer" in _RESOLVABLE_THREAD_VERDICTS
    assert "defer" not in _OUTDATED_RESOLVABLE_THREAD_VERDICTS
    assert frozenset({"fix_committed", "false_positive"}) == (_OUTDATED_RESOLVABLE_THREAD_VERDICTS)
    assert _OUTDATED_RESOLVABLE_THREAD_VERDICTS < _RESOLVABLE_THREAD_VERDICTS
    # Guard the hand-written literal in pr_monitor.py against future drift: the two
    # constants are documented to mirror each other (same verdicts, different
    # derivation paths), so any new verdict added to one must reach the other.
    assert _CLOSED_OUTDATED_THREAD_VERDICTS == _OUTDATED_RESOLVABLE_THREAD_VERDICTS


@pytest.mark.unit
def test_thread_identifier_set_unions_thread_and_comment_ids() -> None:
    """(#547) The identifier set is ``{thread_id}`` ∪ non-``None`` comment ids,
    de-duped. A thread with no comments / only ``None`` comment ids falls back to
    ``{thread_id}`` (existing behavior; no crash)."""
    thread = ReviewThread(
        thread_id="PRRT_abc",
        path="src/anchor.py",
        line=7,
        body_excerpt="finding",
        is_outdated=True,
        comments=(
            ReviewThreadComment(comment_id="4688598838", body="x"),
            ReviewThreadComment(comment_id=None, body="y"),  # filtered out
            ReviewThreadComment(comment_id="4688598838", body="dup"),  # de-duped
        ),
    )
    assert _thread_identifier_set(thread) == {"PRRT_abc", "4688598838"}
    # No comments → thread_id only.
    assert _thread_identifier_set(_outdated_thread("PRRT_lonely")) == {"PRRT_lonely"}
    # Only a None-id comment → thread_id only (no crash).
    none_only = ReviewThread(
        thread_id="PRRT_noneonly",
        path="src/anchor.py",
        line=7,
        body_excerpt="finding",
        is_outdated=True,
        comments=(ReviewThreadComment(comment_id=None, body="x"),),
    )
    assert _thread_identifier_set(none_only) == {"PRRT_noneonly"}


@pytest.mark.unit
def test_grep_id_pattern_anchors_numeric_ids_but_not_node_ids() -> None:
    """(#548) A numeric databaseId is wrapped in non-digit boundaries so it cannot
    match as a substring of a longer id, while an alnum node id stays bare."""
    # Numeric ids get the non-digit (or message edge) boundary on both sides.
    assert _grep_id_pattern("123") == "(^|[^0-9])123([^0-9]|$)"
    # Node ids carry letters/``_`` and cannot collide by substring → left bare.
    assert _grep_id_pattern("PRRT_abc") == "PRRT_abc"
    # The boundaried pattern matches the id as a whole token but NOT inside a
    # longer overlapping numeric id — the exact wrong-thread match #548 closes.
    pattern = _grep_id_pattern("123")
    assert re.search(pattern, "fix: address review comment issue:123 — done")
    assert re.search(pattern, "fix: address 123") is not None  # id at message end
    assert re.search(pattern, "fix: address review comment issue:12345 — other") is None
    assert re.search(pattern, "fix: address review comment issue:5123 — other") is None
