"""Resolve-hygiene for addressed review threads that became OUTDATED (#473).

Part 3 of 3 — the #484 branch-evidence seeding cases and the pure-helper unit
tests. Part 1 holds the #473 resolve-hygiene cases; part 2 holds the #547 / #548
comment-keyed reconcile cases. Shared builders live in ``._helpers``; the
PostgreSQL ``factory`` fixture lives in the package ``conftest``.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.db.repositories import WorkspaceRepository
from awf.runtime.pr_monitor import (
    _CLOSED_OUTDATED_THREAD_VERDICTS,
    MonitorConfig,
    MonitorState,
    NotifyHuman,
    ReviewThread,
    ReviewThreadComment,
    _mark_review_thread_addressed,
    _review_thread_body_hash,
    decide,
)
from awf.runtime.pr_monitor_runner.fix_cycle import (
    _RESOLVABLE_THREAD_VERDICTS,
    _deferred_issue_filed_marker,
)
from awf.runtime.pr_monitor_runner.outdated_resolution import (
    _OUTDATED_RESOLVABLE_THREAD_VERDICTS,
    _addressed_comment_created_at,
    _grep_id_pattern,
    _latest_reviewer_comment_at,
    _outdated_thread_is_resolvable,
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
def test_addressed_comment_created_at_returns_none_when_comment_missing() -> None:
    """Post-fix activity guard keeps the promote-baseline when the addressed
    comment id is absent from the thread feed (ordering is then unprovable)."""
    created = datetime(2026, 6, 10, 8, 0, tzinfo=UTC)
    thread = ReviewThread(
        thread_id="PRRT_missing",
        path="src/anchor.py",
        line=7,
        body_excerpt="finding",
        is_outdated=True,
        comments=(ReviewThreadComment(comment_id="present", body="x", created_at=created),),
    )
    assert _addressed_comment_created_at(thread, "present") == created
    assert _addressed_comment_created_at(thread, "absent") is None


@pytest.mark.unit
def test_parse_commit_iso() -> None:
    """``%aI`` offset dates parse to UTC-aware datetimes; junk yields None."""
    assert _parse_commit_iso("2026-06-10T09:00:00+02:00") == datetime(2026, 6, 10, 7, 0, tzinfo=UTC)
    assert _parse_commit_iso("not-a-timestamp") is None


@pytest.mark.unit
def test_outdated_resolvable_verdicts_exclude_defer() -> None:
    """The closed outdated-resolvable *set* still excludes ``defer``; durable
    capture is checked separately by ``_outdated_thread_is_resolvable`` so an
    uncaptured outdated defer stays open while a captured one can resolve."""
    assert "defer" in _RESOLVABLE_THREAD_VERDICTS
    assert "defer" not in _OUTDATED_RESOLVABLE_THREAD_VERDICTS
    assert frozenset({"fix_committed", "false_positive"}) == (_OUTDATED_RESOLVABLE_THREAD_VERDICTS)
    assert _OUTDATED_RESOLVABLE_THREAD_VERDICTS < _RESOLVABLE_THREAD_VERDICTS
    # Guard the hand-written literal in pr_monitor.py against future drift: the two
    # constants are documented to mirror each other (same verdicts, different
    # derivation paths), so any new verdict added to one must reach the other.
    assert _CLOSED_OUTDATED_THREAD_VERDICTS == _OUTDATED_RESOLVABLE_THREAD_VERDICTS
    uncaptured = _outdated_thread("T_uncaptured")
    state = MonitorState()
    state.mark_addressed(uncaptured.thread_id, "defer")
    assert not _outdated_thread_is_resolvable(state, uncaptured)
    captured = _outdated_thread("T_captured")
    _mark_review_thread_addressed(state, captured, "defer")
    state.mark_addressed(
        _deferred_issue_filed_marker(captured.thread_id, _review_thread_body_hash(captured)),
        "https://github.example/issues/1",
    )
    assert _outdated_thread_is_resolvable(state, captured)


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
