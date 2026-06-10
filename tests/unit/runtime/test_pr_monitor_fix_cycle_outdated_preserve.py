"""Preserve an outdated thread's fix verdict on an in-cycle resolve fault (#484).

The fix cycle's in-cycle resolve loop normally CLEARS the addressed marker on a
``resolve_thread`` fault so the next poll re-addresses the still-open thread. But
if an in-place fix already made the thread ``isOutdated`` on the forge, clearing
the verdict strands it: outdated threads are dropped from the actionable feed, so
the fix cycle never re-addresses them, and the next poll's outdated-resolution
step skips a thread with no recorded verdict — leaving the PR permanently BLOCKED
(the second trigger in #484).

These tests pin that the verdict is PRESERVED (``fix_committed``) for a thread the
settle re-poll reports as outdated, on both the transient and permanent resolve
arms, while a non-outdated thread still clears (regression guard).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.bitbucket_client import BITBUCKET_API_ERROR, BitbucketClientError
from awf.common.commands import FakeCommandRunner
from awf.common.github_client import GitHubClientError, RepoRef
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceEventRepository, WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    CheckState,
    MergeableState,
    MergeStateStatus,
    MonitorState,
    PRStatus,
    ReviewThread,
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


class _SettleAndResolveClient(DefaultMergeMethodGitHubClient):
    """Command-based gh double with a scripted settle status + raising resolve.

    Overriding ``fetch_pr_status`` lets the settle re-poll return a status whose
    ``outdated_unresolved_inline_threads`` carries the addressed thread (so the fix
    cycle's ``outdated_thread_ids`` set includes it), while ``resolve_thread``
    raises the configured forge fault. The rest of the flow (push, rev-parse) stays
    command-based.
    """

    def __init__(
        self,
        runner: FakeCommandRunner,
        *,
        settle_status: PRStatus,
        resolve_exc: Exception,
    ) -> None:
        super().__init__(runner)
        self._settle_status = settle_status
        self._resolve_exc = resolve_exc
        self.attempts: list[str] = []

    async def fetch_pr_status(
        self, *, repo: RepoRef, pr_number: int, base_behind_count: int
    ) -> PRStatus:
        del repo, pr_number, base_behind_count
        return self._settle_status

    async def resolve_thread(self, *, thread_id: str) -> None:
        self.attempts.append(thread_id)
        raise self._resolve_exc


def _outdated_settle_status(thread: ReviewThread) -> PRStatus:
    """A merge-ready settle status that reports ``thread`` as outdated-unresolved."""
    return PRStatus(
        number=42,
        head_sha="abc1234567890def",
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=(),
        unresolved_review_comments=(),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
        outdated_unresolved_inline_threads=(thread,),
    )


def _resolution_events(events: list, *, outcome: str) -> list:
    return [
        event
        for event in events
        if event.payload is not None
        and event.payload.get("action") == "resolve_thread"
        and event.payload.get("outcome") == outcome
    ]


@pytest.mark.unit
async def test_permanent_resolve_fault_on_outdated_thread_preserves_verdict(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """(b) A PERMANENT resolve fault on a thread the settle re-poll reports as
    outdated must NOT clear the ``fix_committed`` verdict — clearing strands the
    thread (#484). The verdict is preserved so the next poll's outdated-resolution
    step retries it (or escalates to ``needs_human`` on its own permanent path),
    and a ``failed`` resolve audit event is still recorded."""
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0)  # push
    cmd.queue_result(returncode=0, stdout="newsha\n")  # rev-parse HEAD
    adapter = FakeAdapter()
    adapter.queue(stdout="Committed fix locally.")
    thread = ReviewThread(
        thread_id="PRRT_outdated",
        path="src/foo.py",
        line=12,
        body_excerpt="please adjust this",
        author="review-bot",
    )
    gh = _SettleAndResolveClient(
        cmd,
        settle_status=_outdated_settle_status(thread),
        resolve_exc=BitbucketClientError(
            operation="bitbucket resolve_thread",
            status=403,
            body="forbidden: missing scope",
            reason_code=BITBUCKET_API_ERROR,
        ),
    )
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    state = MonitorState()

    await runner._run_fix_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        initial_threads=(thread,),
        initial_reviews=(),
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert gh.attempts == ["PRRT_outdated"]
    # Verdict preserved (NOT dropped to None) so the outdated-resolution step can
    # retry/escalate it next poll.
    assert state.threads_addressed_ids.get("PRRT_outdated") == "fix_committed"
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.monitoring_pr.value
        resolution_events = await WorkspaceEventRepository(s).list(
            workspace_id=workspace_id,
            event_type="workspace.audit.comment_resolution",
            limit=10,
        )
    failed = _resolution_events(resolution_events, outcome="failed")
    assert len(failed) == 1
    assert failed[0].payload is not None
    assert failed[0].payload["reason_code"] == BITBUCKET_API_ERROR


@pytest.mark.unit
async def test_transient_resolve_fault_on_outdated_thread_preserves_verdict(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A TRANSIENT resolve fault on a now-outdated thread also preserves the
    ``fix_committed`` verdict (rather than clearing it) so the next poll retries —
    covering the transient ``tid in outdated_thread_ids`` branch."""
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0)  # push
    cmd.queue_result(returncode=0, stdout="newsha\n")  # rev-parse HEAD
    adapter = FakeAdapter()
    adapter.queue(stdout="Committed fix locally.")
    thread = ReviewThread(
        thread_id="PRRT_outdated",
        path="src/foo.py",
        line=12,
        body_excerpt="please adjust this",
        author="review-bot",
    )
    gh = _SettleAndResolveClient(
        cmd,
        settle_status=_outdated_settle_status(thread),
        resolve_exc=GitHubClientError(
            operation="resolve_thread",
            returncode=1,
            stderr="HTTP 503: service unavailable",
        ),
    )
    sleep_fn = RecordedSleep()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    state = MonitorState()

    await runner._run_fix_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        initial_threads=(thread,),
        initial_reviews=(),
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert gh.attempts == ["PRRT_outdated"]
    assert state.threads_addressed_ids.get("PRRT_outdated") == "fix_committed"
    assert sleep_fn.calls  # the transient handler waited
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        resolution_events = await WorkspaceEventRepository(s).list(
            workspace_id=workspace_id,
            event_type="workspace.audit.comment_resolution",
            limit=10,
        )
    requeued = _resolution_events(resolution_events, outcome="requeued")
    assert len(requeued) == 1


@pytest.mark.unit
async def test_permanent_resolve_fault_on_non_outdated_thread_still_clears_verdict(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Regression guard: a permanent resolve fault on a thread that is NOT outdated
    still CLEARS the addressed marker (the pre-#484 behavior) so the next poll
    re-addresses the still-open thread — locking the ``tid not in
    outdated_thread_ids`` branch."""
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0)  # push
    cmd.queue_result(returncode=0, stdout="newsha\n")  # rev-parse HEAD
    adapter = FakeAdapter()
    adapter.queue(stdout="Committed fix locally.")
    thread = ReviewThread(
        thread_id="PRRT_active",
        path="src/foo.py",
        line=12,
        body_excerpt="please adjust this",
        author="review-bot",
    )
    # Settle status with NO outdated threads: the addressed thread is not outdated,
    # so a permanent resolve fault must clear its verdict.
    settle_status = PRStatus(
        number=42,
        head_sha="abc1234567890def",
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=(),
        unresolved_review_comments=(),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
    )
    gh = _SettleAndResolveClient(
        cmd,
        settle_status=settle_status,
        resolve_exc=BitbucketClientError(
            operation="bitbucket resolve_thread",
            status=403,
            body="forbidden: missing scope",
            reason_code=BITBUCKET_API_ERROR,
        ),
    )
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    state = MonitorState()

    await runner._run_fix_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        initial_threads=(thread,),
        initial_reviews=(),
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert gh.attempts == ["PRRT_active"]
    # Not outdated → verdict cleared so the next poll re-addresses it.
    assert "PRRT_active" not in state.threads_addressed_ids
