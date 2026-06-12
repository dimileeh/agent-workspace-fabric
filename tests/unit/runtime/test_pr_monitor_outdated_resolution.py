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
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.bitbucket_client import BITBUCKET_API_ERROR, BitbucketClientError
from awf.common.commands import FakeCommandRunner
from awf.common.github_client import GitHubClientError, RepoRef
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
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
from tests.postgres import postgres_test_engine
from tests.shared.monitor_runner import DefaultMergeMethodGitHubClient
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


class _RecordingGitHub(DefaultMergeMethodGitHubClient):
    """Forge stub that records ``resolve_thread`` calls and optionally raises.

    The runner step is forge-neutral — it only calls ``gh.resolve_thread`` — so
    a single recording stub exercises both the GitHub and Bitbucket paths; the
    POST-not-DELETE resolve semantics are covered by the client-level tests.
    """

    def __init__(self, inner: FakeCommandRunner, *, error: Exception | None = None) -> None:
        super().__init__(inner)
        self.resolved: list[str] = []
        self.attempts: list[str] = []
        self._error = error

    async def resolve_thread(self, *, thread_id: str) -> None:
        self.attempts.append(thread_id)
        if self._error is not None:
            raise self._error
        self.resolved.append(thread_id)


def _outdated_thread(
    tid: str,
    *,
    path: str = "src/anchor.py",
    body_excerpt: str = "please fix this finding",
) -> ReviewThread:
    return ReviewThread(
        thread_id=tid,
        path=path,
        line=7,
        body_excerpt=body_excerpt,
        author="greptile",
        is_resolved=False,
        is_outdated=True,
    )


def _outdated_thread_with_comment(
    tid: str,
    *,
    comment_at: datetime,
    viewer_did_author: bool = False,
) -> ReviewThread:
    """An outdated thread carrying one reviewer comment stamped ``comment_at``.

    Used to exercise the post-fix-activity guard: the seed compares this
    timestamp against the matching fix commit's author time.
    """
    return ReviewThread(
        thread_id=tid,
        path="src/anchor.py",
        line=7,
        body_excerpt="please fix this finding",
        author="greptile",
        is_resolved=False,
        is_outdated=True,
        comments=(
            ReviewThreadComment(
                comment_id="c1",
                body="please fix this finding",
                author="greptile",
                created_at=comment_at,
                updated_at=comment_at,
                viewer_did_author=viewer_did_author,
            ),
        ),
    )


def _outdated_thread_with_distinct_comment(
    tid: str,
    *,
    comment_id: str,
    comment_at: datetime | None = None,
) -> ReviewThread:
    """An outdated thread whose head comment carries a databaseId distinct from
    ``thread_id`` — the #547 shape.

    The fix-cycle COMMENT path records the verdict under ``comment_id`` (the
    comment's GraphQL ``databaseId``, e.g. ``4688598838``) and the commit reads
    ``fix: address review comment issue:<comment_id> — …``, while the outdated
    thread surfaces under its node ``thread_id`` (``PRRT_…``). Reader-side
    reconciliation must bridge the two.
    """
    return ReviewThread(
        thread_id=tid,
        path="src/anchor.py",
        line=7,
        body_excerpt="please fix this finding",
        author="greptile",
        is_resolved=False,
        is_outdated=True,
        comments=(
            ReviewThreadComment(
                comment_id=comment_id,
                body="please fix this finding",
                author="greptile",
                created_at=comment_at,
                updated_at=comment_at,
            ),
        ),
    )


def _outdated_thread_with_two_comments(
    tid: str,
    *,
    comment_ids: tuple[str, str],
) -> ReviewThread:
    """An outdated thread carrying two distinct-databaseId comments (#548).

    A reply comment can be addressed comment-by-comment via the fix-cycle COMMENT
    path, so a single thread can hold a verdict under each ``comment_id`` — the
    mixed-verdict shape (one resolvable, one blocking) the reconcile/seed guards
    must not resolve over.
    """
    first, second = comment_ids
    return ReviewThread(
        thread_id=tid,
        path="src/anchor.py",
        line=7,
        body_excerpt="please fix this finding",
        author="greptile",
        is_resolved=False,
        is_outdated=True,
        comments=(
            ReviewThreadComment(
                comment_id=first,
                body="please fix this finding",
                author="greptile",
            ),
            ReviewThreadComment(
                comment_id=second,
                body="and also consider this",
                author="greptile",
            ),
        ),
    )


def _status_with_outdated(*outdated: ReviewThread) -> PRStatus:
    return PRStatus(
        number=42,
        head_sha="abc1234567890def",
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=(),
        unresolved_review_comments=(),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
        outdated_unresolved_inline_threads=outdated,
    )


def _resolution_events(ws: object, *, outcome: str | None = None) -> list:
    return [
        event
        for event in ws.events  # type: ignore[attr-defined]
        if event.event_type == "workspace.audit.comment_resolution"
        and (event.payload or {}).get("action") == "resolve_outdated_thread"
        and (outcome is None or (event.payload or {}).get("outcome") == outcome)
    ]


async def _call_resolve(
    runner: object,
    *,
    workspace_id: str,
    status: PRStatus,
    state: MonitorState,
) -> None:
    await runner._resolve_addressed_outdated_threads(  # type: ignore[attr-defined]
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=status,
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
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


def _grep_argv(worktree_path: Path, *ids: str) -> list[str]:
    """The bounded ``git log`` evidence grep the seeding helper issues (#484/#547).

    The id alternation OR-es every identifier the outdated thread could have been
    fixed under — the ``thread_id`` plus any review-comment databaseIds (#547) —
    each run through ``_grep_id_pattern`` (numeric ids get a non-digit boundary —
    #548) and sorted for a deterministic argv, under ``-E`` AND-ed with the literal
    ``fix: address`` prefix via ``--all-match``.
    """
    alternation = "(" + "|".join(_grep_id_pattern(i) for i in sorted(ids)) + ")"
    return [
        "git",
        "-c",
        f"safe.directory={worktree_path}",
        "-C",
        str(worktree_path),
        "log",
        "-n",
        "1",
        "--format=%aI",
        "-E",
        "--all-match",
        "--grep",
        "fix: address",
        "--grep",
        alternation,
        "HEAD",
    ]


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


def _outdated_thread_with_reply(
    tid: str,
    *,
    addressed_comment_id: str,
    addressed_at: datetime,
    reply_at: datetime,
) -> ReviewThread:
    """An outdated thread whose head comment was addressed comment-by-comment and
    which then gained a LATER untriaged reviewer reply (#548 / PRRT_kwDOSJAM6s6JHeA2).

    The head comment carries ``addressed_comment_id`` (the fix-cycle COMMENT path
    keys its verdict on it); the reply carries no verdict and is stamped after the
    addressed comment, so the reconcile's post-fix activity guard must treat it as
    fresh feedback.
    """
    return ReviewThread(
        thread_id=tid,
        path="src/anchor.py",
        line=7,
        body_excerpt="please fix this finding",
        author="greptile",
        is_resolved=False,
        is_outdated=True,
        comments=(
            ReviewThreadComment(
                comment_id=addressed_comment_id,
                body="please fix this finding",
                author="greptile",
                created_at=addressed_at,
                updated_at=addressed_at,
            ),
            ReviewThreadComment(
                comment_id="reply-99",
                body="actually this is still broken",
                author="greptile",
                created_at=reply_at,
                updated_at=reply_at,
            ),
        ),
    )


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


def _outdated_thread_with_edited_comment(
    tid: str,
    *,
    addressed_comment_id: str,
    created_at: datetime,
    edited_at: datetime,
) -> ReviewThread:
    """An outdated thread whose SOLE addressed comment was edited after the fix
    (#548 / PRRT_kwDOSJAM6s6JH9Zx).

    The fix-cycle COMMENT path keyed its verdict on ``addressed_comment_id``. The
    reviewer then edited that very comment (its ``updated_at`` advances past its
    ``created_at`` and its body changes), so the edit is untriaged feedback. The
    edited comment is itself the newest reviewer activity, so the post-fix guard
    must anchor on the comment's stable ``created_at`` — not its moving
    ``updated_at`` — to detect the edit.
    """
    return ReviewThread(
        thread_id=tid,
        path="src/anchor.py",
        line=7,
        body_excerpt="please fix this finding",
        author="greptile",
        is_resolved=False,
        is_outdated=True,
        comments=(
            ReviewThreadComment(
                comment_id=addressed_comment_id,
                body="actually, also handle the edited edge case",
                author="greptile",
                created_at=created_at,
                updated_at=edited_at,
            ),
        ),
    )


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
