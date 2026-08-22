"""Resolve-hygiene for addressed review threads that became OUTDATED (#473).

Part 2 of 2 — the #547 / #548 comment-keyed reconcile cases: an outdated thread
whose verdict was recorded under a head/reply ``comment_id`` (the fix-cycle
COMMENT path) rather than its node ``thread_id``. The reader bridges the two via
branch-evidence grep and the in-memory reconcile, guarding against resolving over
post-fix replies, edits, mixed verdicts, and overlapping numeric ids.

Part 1 holds the #473 resolve-hygiene cases, the #484 branch-evidence seeding
cases, and the pure-helper unit tests. Shared builders live in ``._helpers``; the
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
    MonitorConfig,
    MonitorState,
    NotifyHuman,
    ReviewThread,
    ReviewThreadComment,
    decide,
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
    _outdated_thread_with_distinct_comment,
    _outdated_thread_with_edited_comment,
    _outdated_thread_with_reply,
    _outdated_thread_with_two_comments,
    _outdated_thread_with_two_handled_comments,
    _RecordingGitHub,
    _resolution_events,
    _status_with_outdated,
)


@pytest.mark.unit
async def test_branch_evidence_grep_does_not_cross_match_overlapping_numeric_ids(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """(#548 regression) The branch-evidence grep for a thread whose comment
    databaseId is ``123`` must NOT be satisfied by a commit that only addressed a
    longer overlapping id (``12345``). Pins the unanchored-alternation false match:
    the recorded grep pattern matches ``…issue:123 —`` but not ``…issue:12345 —``."""
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
    thread = _outdated_thread_with_distinct_comment("PRRT_overlap", comment_id="123")
    # The seed helper issues exactly one bounded ``git log`` grep; its result is
    # irrelevant here (we assert on the emitted pattern, not the match outcome).
    cmd.queue_result(returncode=0, stdout="")

    await _call_resolve(
        runner,
        workspace_id=workspace_id,
        status=_status_with_outdated(thread),
        state=state,
    )

    # The alternation pattern is the argument right before the ``HEAD`` revision.
    argv = cmd.calls[0].args
    alternation = argv[argv.index("HEAD") - 1]
    # The real comment-path fix commit for THIS thread matches…
    assert re.search(alternation, "fix: address review comment issue:123 — done")
    # …but a sibling thread's longer overlapping id does NOT, so the seed cannot
    # mark/resolve the wrong outdated thread.
    assert re.search(alternation, "fix: address review comment issue:12345 — other") is None


@pytest.mark.unit
async def test_comment_path_outdated_thread_resolved_via_branch_evidence(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """(#547 / #540 regression — branch-evidence path) A greptile-style outdated
    thread whose head comment databaseId (``4688598838``) was addressed by a
    COMMENT-path commit (``fix: address review comment issue:4688598838 — …``) is
    auto-resolved even though ``thread_id`` ≠ ``comment_id``: the OR-grep matches
    the comment databaseId substring. Pins the #540 PR scenario."""
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
    # Empty state — the comment-path verdict never reached THIS instance; the only
    # durable trace is the fix commit on the branch head.
    state = MonitorState()
    thread = _outdated_thread_with_distinct_comment(
        "PRRT_kwDOSJAM6s6IMBmJ",
        comment_id="4688598838",
        comment_at=datetime(2026, 6, 10, 8, 0, tzinfo=UTC),
    )
    # The branch head carries ``fix: address review comment issue:4688598838 — …``;
    # ``%aI`` emits its author time (09:00, AFTER the 08:00 finding) => fix_committed.
    cmd.queue_result(returncode=0, stdout="2026-06-10T09:00:00+00:00\n")

    await _call_resolve(
        runner,
        workspace_id=workspace_id,
        status=_status_with_outdated(thread),
        state=state,
    )

    assert gh.resolved == ["PRRT_kwDOSJAM6s6IMBmJ"]
    assert state.threads_addressed_ids["PRRT_kwDOSJAM6s6IMBmJ"] == "fix_committed"
    # The evidence grep OR-ed both the thread node id and the comment databaseId
    # under ``-E`` AND-ed with ``fix: address``.
    worktree = tmp_path / "worktrees" / workspace_id
    assert [c.args for c in cmd.calls] == [
        _grep_argv(worktree, "PRRT_kwDOSJAM6s6IMBmJ", "4688598838")
    ]
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        succeeded = _resolution_events(ws, outcome="succeeded")
        assert len(succeeded) == 1
        assert succeeded[0].reason_code == "COMMENT_REPAIR"


@pytest.mark.unit
async def test_comment_keyed_in_memory_verdict_resolves_outdated_thread(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """(#547 in-memory path — the #540 continuous-monitor case) A resolvable
    verdict recorded under the head ``comment_id`` (no ``thread_id`` verdict) is
    reconciled onto the outdated thread IN MEMORY and resolved — with NO git call,
    because the reconcile promotes the verdict before the branch-evidence seed,
    whose ``unseeded`` filter then skips the now-seeded thread."""
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
    thread = _outdated_thread_with_distinct_comment("PRRT_inmem", comment_id="4688598838")
    # The fix-cycle COMMENT path recorded the verdict under the comment databaseId,
    # NOT the thread node id — the exact #540 shape the thread-keyed lookup missed.
    state.mark_addressed("4688598838", "fix_committed")

    await _call_resolve(
        runner,
        workspace_id=workspace_id,
        status=_status_with_outdated(thread),
        state=state,
    )

    assert gh.resolved == ["PRRT_inmem"]
    # Promotion recorded the thread-keyed verdict so ``decide``'s outdated gate is
    # consistent too.
    assert state.threads_addressed_ids["PRRT_inmem"] == "fix_committed"
    # No git grep: the in-memory reconcile resolved it before the seed step.
    assert cmd.calls == []


@pytest.mark.unit
async def test_comment_path_thread_never_addressed_is_not_resolved(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """(#547 specificity) An outdated thread the branch never addressed — no
    ``fix: address <any id>`` commit and no in-memory verdict under the thread OR
    comment id — is NOT resolved and no verdict is seeded. Broadening the id set
    must not falsely seed/resolve a thread the branch never touched."""
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
    thread = _outdated_thread_with_distinct_comment("PRRT_untouched", comment_id="4688598838")
    # No commit on HEAD matches both ``fix: address`` and any of the thread's ids.
    cmd.queue_result(returncode=0, stdout="")

    await _call_resolve(
        runner,
        workspace_id=workspace_id,
        status=_status_with_outdated(thread),
        state=state,
    )

    assert gh.attempts == []
    assert "PRRT_untouched" not in state.threads_addressed_ids
    assert "4688598838" not in state.threads_addressed_ids


@pytest.mark.unit
async def test_comment_keyed_defer_outdated_thread_stays_open(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """(#547 / PRRT_kwDOSJAM6s6JIGQB) A ``defer`` verdict recorded under the head
    ``comment_id`` must NOT be promoted as a *resolvable* verdict — a deferred thread
    stays open with its tracking issue (``defer`` is excluded from
    ``_OUTDATED_RESOLVABLE_THREAD_VERDICTS``). But the thread must still block the
    merge: a bare skip would leave only the comment-keyed ``defer`` in state, which
    ``decide``'s outdated gate never consults, so the PR could auto-merge over the
    deferred feedback. The reconcile promotes ``needs_human`` onto the ``thread_id``
    so the gate blocks while the thread stays open."""
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
    thread = _outdated_thread_with_distinct_comment("PRRT_defer", comment_id="4688598838")
    state.mark_addressed("4688598838", "defer")
    # A defer posts a tracking comment, not a code fix, so there is no branch
    # evidence either: the seed grep finds nothing.
    cmd.queue_result(returncode=0, stdout="")

    status = _status_with_outdated(thread)
    await _call_resolve(
        runner,
        workspace_id=workspace_id,
        status=status,
        state=state,
    )

    assert gh.attempts == []
    # The ``defer`` verdict was NOT promoted as resolvable; instead ``needs_human`` is
    # promoted onto the thread so ``decide`` blocks the merge while the thread stays
    # open. The comment-keyed ``defer`` is preserved.
    assert state.threads_addressed_ids["PRRT_defer"] == "needs_human"
    assert state.threads_addressed_ids["4688598838"] == "defer"
    # End-to-end: the deferred outdated thread blocks auto-merge.
    action = decide(status=status, state=state, config=MonitorConfig(auto_merge=True))
    assert isinstance(action, NotifyHuman)


@pytest.mark.unit
async def test_mixed_verdict_outdated_thread_stays_open(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """(#548 / PRRT_kwDOSJAM6s6JIGQB) A thread holding BOTH a resolvable comment
    verdict (``fix_committed``) and a blocking sibling (``needs_human``) must NOT be
    resolved, and must keep blocking the merge.

    The resolvable comment id (``1000``) sorts BEFORE the blocking one (``9000``),
    so the old break-on-first-resolvable loop would have promoted ``fix_committed``
    and resolved over the ``needs_human``. The guard keeps the thread open — and
    promotes ``needs_human`` onto the ``thread_id`` so ``decide``'s outdated gate
    (which consults only the ``thread_id`` verdict, never the comment ids) actually
    blocks the merge instead of auto-merging over the blocking sibling."""
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
    thread = _outdated_thread_with_two_comments("PRRT_mixed", comment_ids=("1000", "9000"))
    state.mark_addressed("1000", "fix_committed")
    state.mark_addressed("9000", "needs_human")

    status = _status_with_outdated(thread)
    await _call_resolve(
        runner,
        workspace_id=workspace_id,
        status=status,
        state=state,
    )

    # Thread not resolved; the blocking sibling is promoted onto the thread_id as
    # ``needs_human`` so ``decide`` keeps blocking the merge (the resolvable
    # ``fix_committed`` is NOT promoted, which would have resolved over the sibling).
    assert gh.attempts == []
    assert state.threads_addressed_ids["PRRT_mixed"] == "needs_human"
    assert state.threads_addressed_ids["9000"] == "needs_human"
    # End-to-end: ``decide`` actually blocks the merge over the blocking sibling.
    # A bare ``continue`` left only the comment-keyed verdict in state, which the
    # outdated gate never consults, so the PR would have auto-merged (the bug).
    action = decide(status=status, state=state, config=MonitorConfig(auto_merge=True))
    assert isinstance(action, NotifyHuman)


@pytest.mark.unit
async def test_mixed_verdict_outdated_thread_not_seeded_from_branch_evidence(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """(#548) The branch-evidence seed must not re-introduce the bypass: even with a
    matching ``fix: address`` commit on HEAD for the resolved sibling, a thread that
    holds a blocking sibling verdict is excluded from the seed (no git grep), so the
    thread stays open instead of being seeded ``fix_committed`` and resolved."""
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
    thread = _outdated_thread_with_two_comments("PRRT_mixed_seed", comment_ids=("1000", "9000"))
    state.mark_addressed("1000", "fix_committed")
    state.mark_addressed("9000", "needs_human")
    # A matching fix commit IS on HEAD — but the blocking sibling must keep the seed
    # from ever consulting it. If the guard regressed, this queued result would be
    # popped by the grep and the thread seeded + resolved.
    cmd.queue_result(returncode=0, stdout="2026-06-10T09:00:00+00:00\n")

    await _call_resolve(
        runner,
        workspace_id=workspace_id,
        status=_status_with_outdated(thread),
        state=state,
    )

    assert gh.attempts == []
    # The reconcile step promotes ``needs_human`` onto the thread_id for the blocking
    # sibling, so it is never seeded ``fix_committed`` from branch evidence.
    assert state.threads_addressed_ids["PRRT_mixed_seed"] == "needs_human"
    # No git call: the seed's ``unseeded`` filter excludes the thread (it now carries
    # a thread_id verdict), so the queued fix-commit result is never popped.
    assert cmd.calls == []


@pytest.mark.unit
async def test_comment_keyed_post_fix_reply_blocks_resolve(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """(PRRT_kwDOSJAM6s6JHeA2) A reviewer reply that postdates a comment-keyed fix
    must NOT be silently resolved over by the in-memory reconcile.

    ``_mark_review_thread_addressed`` snapshots the thread's CURRENT body, so a reply
    that landed after the comment-path ``fix_committed`` verdict was recorded would be
    baked into the snapshot and ``_review_thread_needs_attention`` would see a matching
    hash and resolve — unlike the thread-keyed path, whose snapshot predates the reply
    and blocks. The reconcile mirrors the branch-evidence seed's post-fix activity
    guard: a reviewer comment newer than the addressed comment seeds ``needs_human``
    instead, so the resolve loop leaves the thread open and ``decide`` blocks merge."""
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
    thread = _outdated_thread_with_reply(
        "PRRT_postfix_reply",
        addressed_comment_id="4688598838",
        addressed_at=datetime(2026, 6, 10, 9, 0, tzinfo=UTC),
        reply_at=datetime(2026, 6, 10, 9, 30, tzinfo=UTC),
    )
    # Only the original comment was addressed via the COMMENT path; the later reply
    # carries no verdict and was never re-triaged.
    state.mark_addressed("4688598838", "fix_committed")

    status = _status_with_outdated(thread)
    await _call_resolve(runner, workspace_id=workspace_id, status=status, state=state)

    # Not resolved — the fresh reply postdates the fix.
    assert gh.attempts == []
    assert state.threads_addressed_ids["PRRT_postfix_reply"] == "needs_human"
    # No git call: the in-memory reconcile decided before the branch-evidence seed.
    assert cmd.calls == []
    # ``decide`` holds the merge-ready PR at NotifyHuman so a human sees the reply.
    action = decide(status=status, state=state, config=MonitorConfig(auto_merge=True))
    assert isinstance(action, NotifyHuman)


@pytest.mark.unit
async def test_comment_keyed_pre_fix_reply_still_resolves(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A reviewer comment that PREDATES the addressed (newest) comment is feedback the
    fix already covers, so the post-fix guard does not fire: the resolvable verdict is
    promoted and the outdated thread is resolved — the #540 unblock is preserved."""
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
    # The addressed comment is the NEWEST reviewer activity (the earlier comment was
    # superseded), so no reviewer comment postdates the fix.
    thread = _outdated_thread_with_reply(
        "PRRT_prefix_reply",
        addressed_comment_id="4688598838",
        addressed_at=datetime(2026, 6, 10, 9, 30, tzinfo=UTC),
        reply_at=datetime(2026, 6, 10, 9, 0, tzinfo=UTC),
    )
    state.mark_addressed("4688598838", "fix_committed")

    await _call_resolve(
        runner,
        workspace_id=workspace_id,
        status=_status_with_outdated(thread),
        state=state,
    )

    assert gh.resolved == ["PRRT_prefix_reply"]
    assert state.threads_addressed_ids["PRRT_prefix_reply"] == "fix_committed"
    assert cmd.calls == []


@pytest.mark.unit
async def test_comment_keyed_later_handled_sibling_still_resolves(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """(PRRT_kwDOSJAM6s6JISCM) A thread with multiple comment-keyed resolvable verdicts
    must still resolve when a HANDLED sibling was created later than the promoted one.

    The reconcile picks the first sorted resolvable comment to promote, but the
    post-fix activity guard must compare the newest reviewer activity against the
    NEWEST handled comment — not just the promoted one. Otherwise an already-handled
    later sibling (whose ``created_at`` is the latest reviewer activity) would satisfy
    ``latest_comment_at > addressed_at`` and falsely seed ``needs_human``, leaving a
    fully-addressed thread open and blocking auto-merge."""
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
    # "a" sorts first but is the OLDER comment; "b" was handled and created later.
    thread = _outdated_thread_with_two_handled_comments(
        "PRRT_two_handled",
        first_comment_id="a",
        first_at=datetime(2026, 6, 10, 9, 0, tzinfo=UTC),
        second_comment_id="b",
        second_at=datetime(2026, 6, 10, 9, 30, tzinfo=UTC),
    )
    state.mark_addressed("a", "fix_committed")
    state.mark_addressed("b", "fix_committed")

    await _call_resolve(
        runner,
        workspace_id=workspace_id,
        status=_status_with_outdated(thread),
        state=state,
    )

    # Both siblings are handled — the thread is fully addressed and resolves.
    assert gh.resolved == ["PRRT_two_handled"]
    assert state.threads_addressed_ids["PRRT_two_handled"] == "fix_committed"
    assert cmd.calls == []


@pytest.mark.unit
async def test_comment_keyed_untriaged_reply_after_handled_sibling_blocks(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A genuinely untriaged reply newer than every handled comment still blocks.

    Anchoring the guard on the newest HANDLED comment must not mask a fresh reviewer
    reply that carries no verdict and postdates all handled siblings — that feedback
    is still surfaced via ``needs_human``."""
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
    thread = ReviewThread(
        thread_id="PRRT_two_plus_reply",
        path="src/anchor.py",
        line=7,
        body_excerpt="please fix this finding",
        author="greptile",
        is_resolved=False,
        is_outdated=True,
        comments=(
            ReviewThreadComment(
                comment_id="a",
                body="please fix this finding",
                author="greptile",
                created_at=datetime(2026, 6, 10, 9, 0, tzinfo=UTC),
                updated_at=datetime(2026, 6, 10, 9, 0, tzinfo=UTC),
            ),
            ReviewThreadComment(
                comment_id="b",
                body="and this related one too",
                author="greptile",
                created_at=datetime(2026, 6, 10, 9, 30, tzinfo=UTC),
                updated_at=datetime(2026, 6, 10, 9, 30, tzinfo=UTC),
            ),
            ReviewThreadComment(
                comment_id="reply-99",
                body="actually this is still broken",
                author="greptile",
                created_at=datetime(2026, 6, 10, 10, 0, tzinfo=UTC),
                updated_at=datetime(2026, 6, 10, 10, 0, tzinfo=UTC),
            ),
        ),
    )
    state.mark_addressed("a", "fix_committed")
    state.mark_addressed("b", "fix_committed")

    status = _status_with_outdated(thread)
    await _call_resolve(runner, workspace_id=workspace_id, status=status, state=state)

    assert gh.attempts == []
    assert state.threads_addressed_ids["PRRT_two_plus_reply"] == "needs_human"
    assert cmd.calls == []
    action = decide(status=status, state=state, config=MonitorConfig(auto_merge=True))
    assert isinstance(action, NotifyHuman)


@pytest.mark.unit
async def test_comment_keyed_edited_addressed_comment_blocks_resolve(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """(PRRT_kwDOSJAM6s6JH9Zx) An EDIT to the addressed comment itself must not be
    silently resolved over by the in-memory reconcile.

    When the addressed comment is edited after the comment-path ``fix_committed``
    verdict, its ``updated_at`` advances and is simultaneously the newest reviewer
    activity. A baseline of ``max(created_at, updated_at)`` would move in lockstep
    with that activity, so ``latest_comment_at > addressed_at`` could never fire and
    the edited body would be snapshotted as handled. Anchoring the baseline on the
    comment's ``created_at`` (a stable lower bound on fix time that an edit cannot
    move) lets the guard seed ``needs_human`` so the resolve loop leaves the thread
    open and ``decide`` blocks merge."""
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
    thread = _outdated_thread_with_edited_comment(
        "PRRT_edited_comment",
        addressed_comment_id="4688598838",
        created_at=datetime(2026, 6, 10, 9, 0, tzinfo=UTC),
        edited_at=datetime(2026, 6, 10, 9, 30, tzinfo=UTC),
    )
    state.mark_addressed("4688598838", "fix_committed")

    status = _status_with_outdated(thread)
    await _call_resolve(runner, workspace_id=workspace_id, status=status, state=state)

    # Not resolved — the edit postdates the addressed comment's creation.
    assert gh.attempts == []
    assert state.threads_addressed_ids["PRRT_edited_comment"] == "needs_human"
    # No git call: the in-memory reconcile decided before the branch-evidence seed.
    assert cmd.calls == []
    # ``decide`` holds the merge-ready PR at NotifyHuman so a human sees the edit.
    action = decide(status=status, state=state, config=MonitorConfig(auto_merge=True))
    assert isinstance(action, NotifyHuman)
