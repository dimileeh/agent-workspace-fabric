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

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.bitbucket_client import BITBUCKET_API_ERROR, BitBucketClientError
from awf.common.commands import FakeCommandRunner
from awf.common.github_client import GitHubClientError, RepoRef
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    _CLOSED_OUTDATED_THREAD_VERDICTS,
    CheckState,
    MergeableState,
    MergeStateStatus,
    MonitorState,
    PRStatus,
    ReviewThread,
    _mark_review_thread_addressed,
)
from awf.runtime.pr_monitor_runner.fix_cycle import _RESOLVABLE_THREAD_VERDICTS
from awf.runtime.pr_monitor_runner.outdated_resolution import (
    _OUTDATED_RESOLVABLE_THREAD_VERDICTS,
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
    a single recording stub exercises both the GitHub and BitBucket paths; the
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
    """(d) Forge-neutral: a BitBucket-style addressed-outdated thread resolves via
    ``resolve_thread`` (the client-level POST/never-DELETE semantics are covered
    in the BitBucket client tests)."""
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
    assert sleep_fn.calls  # the transient handler waited before re-polling
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        requeued = _resolution_events(ws, outcome="requeued")
        assert len(requeued) == 1
        assert requeued[0].reason_code == "GITHUB_TRANSIENT_RETRY"


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
    permanent = BitBucketClientError(
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

    await _call_resolve(
        runner,
        workspace_id=workspace_id,
        status=_status_with_outdated(thread),
        state=state,
    )

    assert gh.resolved == []
    # Verdict downgraded so the next poll skips it (not in the resolvable set).
    assert state.threads_addressed_ids[bb_id] == "needs_human"
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
    permanent = BitBucketClientError(
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
    permanent = BitBucketClientError(
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
