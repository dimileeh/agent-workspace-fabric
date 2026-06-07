"""Focused branch-coverage tests for PR monitor runner edge behavior. (split part)"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    CheckState,
    CheckTiming,
    MergeableState,
    MergeStateStatus,
    MonitorState,
    PRStatus,
    ReviewComment,
    ReviewThread,
)
from awf.runtime.pr_monitor_runner import remote_repair as pr_monitor_runner_remote_repair
from awf.runtime.pr_monitor_runner.helpers import (
    _is_protected_manual_ready_handoff,
    _review_comment_body_state_key,
)
from awf.runtime.pr_monitor_runner.types import (
    ProtectedScopeDiffError,
)
from tests.postgres import postgres_test_engine
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


def _status_for_helpers(
    *,
    head_sha: str = "abc1234567890def",
    threads: tuple[ReviewThread, ...] = (),
    reviews: tuple[ReviewComment, ...] = (),
    blocking_reviews: tuple[ReviewComment, ...] | None = None,
    checks: tuple[CheckTiming, ...] = (),
) -> PRStatus:
    return PRStatus(
        number=42,
        head_sha=head_sha,
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=threads,
        unresolved_review_comments=reviews,
        blocking_reviews=(
            tuple(review for review in reviews if review.blocks_merge)
            if blocking_reviews is None
            else blocking_reviews
        ),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
        checks=checks,
    )


class _RaisingCreateIssueClient:
    """Minimal gh stand-in whose ``create_issue`` raises a forced error.

    Used to exercise the deferred-capture failure arms with a
    ``BitBucketClientError`` (the BitBucket forge raises this, not
    ``GitHubClientError``) so the capture path's forge-neutral downgrade is
    covered for BitBucket workspaces.
    """

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def create_issue(self, *, repo: object, title: str, body: str) -> str:
        raise self._exc


class _RaisingPostCommentClient:
    """gh stand-in whose ``create_issue`` succeeds but ``post_comment`` raises.

    Used to exercise the best-effort courtesy comment after a durable capture:
    on a BitBucket workspace ``post_comment`` raises ``BitBucketClientError``,
    which must be swallowed (the tracking issue is already filed) rather than
    escaping to terminate the monitor.
    """

    def __init__(self, *, issue_url: str, exc: BaseException) -> None:
        self._issue_url = issue_url
        self._exc = exc

    async def create_issue(self, *, repo: object, title: str, body: str) -> str:
        return self._issue_url

    async def post_comment(self, *, repo: object, pr_number: int, body: str) -> None:
        raise self._exc


@pytest.mark.unit
def test_read_worktree_text_reports_decode_and_os_errors(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.yml"
    invalid.write_bytes(b"\xff\xfe")
    with pytest.raises(ProtectedScopeDiffError, match="as UTF-8"):
        pr_monitor_runner_remote_repair._read_worktree_text(invalid, display_path="invalid.yml")  # noqa: SLF001

    directory = tmp_path / "config-dir"
    directory.mkdir()
    with pytest.raises(ProtectedScopeDiffError, match="Could not read protected worktree file"):
        pr_monitor_runner_remote_repair._read_worktree_text(directory, display_path="config-dir")  # noqa: SLF001


@pytest.mark.unit
async def test_feedback_refresh_drops_stale_review_comment_state(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    comment = ReviewComment(comment_id="review-1", body_excerpt="new feedback")
    state = MonitorState()
    state.threads_addressed_ids["review-1"] = "fix_committed"
    state.threads_addressed_ids[_review_comment_body_state_key("review-1")] = "old-body-hash"

    async def _no_remote_resolution_update(**_kwargs: object) -> bool:
        return False

    runner._apply_pr_feedback_resolution_state = _no_remote_resolution_update  # type: ignore[method-assign]

    changed = await runner._refresh_pr_feedback_resolution_state(
        workspace_id="ws_feedback",
        repo=RepoRef(owner="example", name="repo"),
        pr_number=42,
        status=_status_for_helpers(reviews=(comment,)),
        state=state,
    )

    assert changed is True
    assert "review-1" not in state.threads_addressed_ids
    assert _review_comment_body_state_key("review-1") not in state.threads_addressed_ids


@pytest.mark.unit
def test_deferred_issue_filed_marker_is_body_aware() -> None:
    # #305: the capture marker is keyed by thread id AND body hash so a same-body
    # resolve-retry stays idempotent (no duplicate issue), while new reviewer
    # replies (a changed body) re-capture into a fresh issue instead of being
    # silently resolved under the stale one.
    from awf.runtime.pr_monitor_runner.fix_cycle import _deferred_issue_filed_marker

    assert _deferred_issue_filed_marker("T1", "hashA") == _deferred_issue_filed_marker(
        "T1", "hashA"
    )
    assert _deferred_issue_filed_marker("T1", "hashA") != _deferred_issue_filed_marker(
        "T1", "hashB"
    )
    assert "T1" in _deferred_issue_filed_marker("T1", "hashA")


@pytest.mark.unit
def test_deferred_thread_conversation_includes_all_replies() -> None:
    # #305: a body-aware recapture fires because new reviewer replies changed the
    # thread, so the tracking issue must carry the whole conversation — not just
    # the truncated first-comment excerpt — or the new feedback is lost on resolve.
    from awf.runtime.pr_monitor import ReviewThread, ReviewThreadComment
    from awf.runtime.pr_monitor_runner.fix_cycle import _deferred_thread_conversation

    thread = ReviewThread(
        thread_id="T1",
        path="x",
        line=1,
        body_excerpt="first finding",
        comments=(
            ReviewThreadComment(comment_id="c1", body="first finding", author="cr"),
            ReviewThreadComment(
                comment_id="c2", body="follow-up reply\nsecond line", author="alice"
            ),
            ReviewThreadComment(comment_id="c3", body="", author="bot"),
        ),
    )
    body = _deferred_thread_conversation(thread)
    assert "first finding" in body
    assert "follow-up reply" in body and "second line" in body  # all replies, multi-line
    assert "alice" in body
    # Fallback to the excerpt when GitHub supplied no structured comments.
    bare = ReviewThread(thread_id="T2", path="x", line=1, body_excerpt="only excerpt", comments=())
    assert "only excerpt" in _deferred_thread_conversation(bare)


@pytest.mark.unit
def test_protected_manual_ready_handoff_rejects_blocking_bot_thread() -> None:
    # #305: a bot inline thread with needs_human/defer blocks in decide() gate 7
    # but is not "human deferred"; the protected-merge handoff must NOT report
    # ready-for-merge, mirroring the _notify_human_reason guard.
    base = _status_for_helpers()
    blocked = PRStatus(
        number=base.number,
        head_sha=base.head_sha,
        mergeable=base.mergeable,
        check_state=base.check_state,
        unresolved_inline_threads=(
            ReviewThread(
                thread_id="T_bot", path="x", line=1, body_excerpt="?", author="coderabbitai"
            ),
        ),
        unresolved_review_comments=(),
        base_behind_count=base.base_behind_count,
        merge_state_status=MergeStateStatus.BLOCKED,
    )
    state = MonitorState(threads_addressed_ids={"T_bot": "needs_human"})
    assert _is_protected_manual_ready_handoff(blocked, state) is False


@pytest.mark.unit
async def test_deferred_capture_transient_failure_requeues_instead_of_downgrading(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # #305: a transient `gh issue create` failure (502) must NOT permanently
    # downgrade a valid defer to needs_human. It clears the verdict so the next
    # poll re-addresses and re-attempts capture once GitHub recovers.
    from awf.runtime.pr_monitor import _mark_review_thread_addressed
    from awf.runtime.pr_monitor_runner.fix_cycle import _capture_deferred_review_thread

    ws_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=1, stderr="HTTP 502 Bad Gateway")  # gh issue create (transient)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    thread = ReviewThread(
        thread_id="T_defer", path="src/x.py", line=1, body_excerpt="nit", author="rev"
    )
    state = MonitorState()
    _mark_review_thread_addressed(state, thread, "defer")

    result = await _capture_deferred_review_thread(
        runner,
        workspace_id=ws_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        thread=thread,
        state=state,
        base_branch="development",
        remote_branch=f"awf/{ws_id}",
        operation_id=None,
        operation_type=None,
        monitor_log=None,
    )

    # None (requeue), not False (which would downgrade to needs_human); and the
    # verdict is cleared so the thread is re-addressed next poll.
    assert result is None
    assert state.threads_addressed_ids.get("T_defer") is None


@pytest.mark.unit
async def test_deferred_capture_transient_bitbucket_failure_requeues(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # A transient BitBucket fault (rate limit) during create_issue must NOT
    # downgrade a valid defer to needs_human: clear the verdict so the next poll
    # re-attempts capture, mirroring the GitHub transient path.
    from awf.common.bitbucket_client import (
        BITBUCKET_RATE_LIMITED,
        BitBucketClientError,
    )
    from awf.runtime.pr_monitor import _mark_review_thread_addressed
    from awf.runtime.pr_monitor_runner.fix_cycle import _capture_deferred_review_thread

    ws_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=_RaisingCreateIssueClient(
            BitBucketClientError(
                operation="bitbucket create_issue",
                status=429,
                body="rate limited",
                reason_code=BITBUCKET_RATE_LIMITED,
            )
        ),
    )
    thread = ReviewThread(
        thread_id="T_bb_defer", path="src/x.py", line=1, body_excerpt="nit", author="rev"
    )
    state = MonitorState()
    _mark_review_thread_addressed(state, thread, "defer")

    result = await _capture_deferred_review_thread(
        runner,
        workspace_id=ws_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        thread=thread,
        state=state,
        base_branch="development",
        remote_branch=f"awf/{ws_id}",
        operation_id=None,
        operation_type=None,
        monitor_log=None,
    )

    assert result is None
    assert state.threads_addressed_ids.get("T_bb_defer") is None


@pytest.mark.unit
async def test_deferred_capture_permanent_bitbucket_failure_downgrades(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # A permanent BitBucket fault (403, token lacks issues scope) during
    # create_issue must downgrade to needs_human (return False) rather than escape
    # to the runner's generic handler and terminate the monitor.
    from awf.common.bitbucket_client import BitBucketClientError
    from awf.runtime.pr_monitor import _mark_review_thread_addressed
    from awf.runtime.pr_monitor_runner.fix_cycle import _capture_deferred_review_thread

    ws_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=_RaisingCreateIssueClient(
            BitBucketClientError(
                operation="bitbucket create_issue",
                status=403,
                body="forbidden: missing issues scope",
            )
        ),
    )
    thread = ReviewThread(
        thread_id="T_bb_perm", path="src/x.py", line=1, body_excerpt="nit", author="rev"
    )
    state = MonitorState()
    _mark_review_thread_addressed(state, thread, "defer")

    result = await _capture_deferred_review_thread(
        runner,
        workspace_id=ws_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        thread=thread,
        state=state,
        base_branch="development",
        remote_branch=f"awf/{ws_id}",
        operation_id=None,
        operation_type=None,
        monitor_log=None,
    )

    # False -> the caller marks the thread needs_human (merge stays blocked); the
    # defer verdict is preserved on the thread, not cleared (that is the transient
    # path) and not silently resolved.
    assert result is False
    assert state.threads_addressed_ids.get("T_bb_perm") == "defer"


@pytest.mark.unit
async def test_deferred_capture_bitbucket_comment_failure_still_succeeds(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # Filing the tracking issue is the durable capture; the explanatory PR comment
    # is best-effort. On a BitBucket workspace ``post_comment`` raises
    # ``BitBucketClientError`` — it must be swallowed (not escape and terminate the
    # monitor), and capture still succeeds (returns True) with the issue recorded
    # as filed so a retry never opens a duplicate.
    from awf.common.bitbucket_client import BitBucketClientError
    from awf.runtime.pr_monitor import _mark_review_thread_addressed
    from awf.runtime.pr_monitor_runner.fix_cycle import (
        _capture_deferred_review_thread,
        _deferred_issue_filed_marker,
        _review_thread_body_hash,
    )

    ws_id = await seed_monitoring_workspace(factory)
    issue_url = "https://bitbucket.org/dimileeh/aira-web/issues/77"
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=_RaisingPostCommentClient(
            issue_url=issue_url,
            exc=BitBucketClientError(
                operation="bitbucket post_comment",
                status=403,
                body="forbidden",
            ),
        ),
    )
    thread = ReviewThread(
        thread_id="T_bb_comment", path="src/x.py", line=1, body_excerpt="nit", author="rev"
    )
    state = MonitorState()
    _mark_review_thread_addressed(state, thread, "defer")

    result = await _capture_deferred_review_thread(
        runner,
        workspace_id=ws_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        thread=thread,
        state=state,
        base_branch="development",
        remote_branch=f"awf/{ws_id}",
        operation_id=None,
        operation_type=None,
        monitor_log=None,
    )

    # True -> the comment failure was swallowed; capture is durable and the caller
    # may resolve the thread. The filed-issue marker is recorded for idempotency.
    assert result is True
    marker = _deferred_issue_filed_marker(thread.thread_id, _review_thread_body_hash(thread))
    assert state.threads_addressed_ids.get(marker) == issue_url


@pytest.mark.unit
async def test_fix_cycle_continues_to_next_thread_after_transient_capture(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # When _capture_deferred_review_thread returns None (transient), the fix
    # cycle must not downgrade the thread and must move on to the next item.
    from awf.runtime.pr_monitor_runner import fix_cycle as fix_cycle_module
    from awf.runtime.pr_monitor_runner.types import _MonitorPolicyBlockedError

    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    t1 = ReviewThread(
        thread_id="T_transient", path="src/a.py", line=1, body_excerpt="nit", author="rev"
    )
    t2 = ReviewThread(
        thread_id="T_block", path="src/b.py", line=2, body_excerpt="blocks", author="rev"
    )
    state = MonitorState()

    async def _address(**kwargs: object) -> str:
        thread = kwargs["thread"]
        assert isinstance(thread, ReviewThread)
        if thread.thread_id == t1.thread_id:
            return "defer"
        raise _MonitorPolicyBlockedError("policy blocked second thread")

    async def _capture_none(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(runner, "_address_thread", _address)
    monkeypatch.setattr(fix_cycle_module, "_capture_deferred_review_thread", _capture_none)

    result = await runner._run_fix_cycle(
        workspace_id="ws_transient_continue",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        initial_threads=(t1, t2),
        initial_reviews=(),
        state=state,
        remote_branch="awf/ws_transient_continue",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    # The cycle moved past the transient thread to the second (which policy-blocks)
    # and never downgraded the transient thread to needs_human.
    assert result.failed is True
    assert state.threads_addressed_ids.get("T_transient") != "needs_human"


@pytest.mark.unit
async def test_address_thread_stashes_agent_verdict_reasons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify thread handling stashes only actionable agent verdict reasons."""
    # _address_thread stashes the agent's DEFER reason in state for the
    # deferred-capture issue and the NEEDS_HUMAN reason for operator handoff.
    # Bare verdicts clear stale reasons from prior passes.
    from types import SimpleNamespace

    from awf.common.github_client import RepoRef
    from awf.runtime.pr_monitor_runner import comments
    from awf.runtime.pr_monitor_runner.comments import VerdictResult, _address_thread
    from awf.runtime.pr_monitor_runner.helpers import (
        _defer_reason_state_key,
        _needs_human_reason_state_key,
    )

    thread = ReviewThread(thread_id="T1", path="x", line=1, body_excerpt="?")
    reason_key = _defer_reason_state_key("T1")
    needs_human_reason_key = _needs_human_reason_state_key("T1")

    async def _empty_owned_paths(_runner: object, _workspace_id: str) -> list[str]:
        return []

    monkeypatch.setattr(comments, "_owned_paths_for_prompt", _empty_owned_paths)

    def _runner(result: VerdictResult) -> object:
        async def _invoke(**_kwargs: object) -> VerdictResult:
            return result

        return SimpleNamespace(
            _workspace_runtime_context="", _invoke_cli_for_verdict_result=_invoke
        )

    async def _call(runner: object, state: MonitorState | None) -> str:
        return await _address_thread(
            runner,  # type: ignore[arg-type]
            workspace_id="ws",
            repo=RepoRef(owner="o", name="r"),
            pr_number=1,
            thread=thread,
            compose_project="p",
            compose_file=Path("/tmp/c.yml"),
            state=state,
        )

    # defer + reason + state -> stashed.
    state = MonitorState()
    assert (
        await _call(_runner(VerdictResult(verdict="defer", reason="track refactor")), state)
        == "defer"
    )
    assert state.threads_addressed_ids[reason_key] == "track refactor"

    # state is None -> no crash, nothing stashed.
    assert await _call(_runner(VerdictResult(verdict="defer", reason="x")), None) == "defer"

    # non-defer verdict -> not stashed.
    s2 = MonitorState()
    assert (
        await _call(_runner(VerdictResult(verdict="fix_committed", reason="done")), s2)
        == "fix_committed"
    )
    assert reason_key not in s2.threads_addressed_ids

    # defer without a reason -> not stashed.
    s3 = MonitorState()
    assert await _call(_runner(VerdictResult(verdict="defer", reason=None)), s3) == "defer"
    assert reason_key not in s3.threads_addressed_ids

    # A re-triage with a bare defer CLEARS a stale reason from a prior pass.
    s4 = MonitorState()
    await _call(_runner(VerdictResult(verdict="defer", reason="old reason")), s4)
    assert s4.threads_addressed_ids[reason_key] == "old reason"
    await _call(_runner(VerdictResult(verdict="defer", reason=None)), s4)
    assert reason_key not in s4.threads_addressed_ids

    # needs_human + reason + state -> stashed for NotifyHuman/defer reporting.
    s5 = MonitorState()
    assert (
        await _call(
            _runner(VerdictResult(verdict="needs_human", reason="requires approval")),
            s5,
        )
        == "needs_human"
    )
    assert s5.threads_addressed_ids[needs_human_reason_key] == "requires approval"

    # A re-triage with a bare needs_human CLEARS a stale reason from a prior pass.
    s6 = MonitorState()
    await _call(_runner(VerdictResult(verdict="needs_human", reason="old reason")), s6)
    assert s6.threads_addressed_ids[needs_human_reason_key] == "old reason"
    await _call(_runner(VerdictResult(verdict="needs_human", reason=None)), s6)
    assert needs_human_reason_key not in s6.threads_addressed_ids

    # A later non-needs-human verdict also clears a stale needs-human reason.
    s7 = MonitorState(threads_addressed_ids={needs_human_reason_key: "old reason"})
    assert (
        await _call(_runner(VerdictResult(verdict="fix_committed", reason="done")), s7)
        == "fix_committed"
    )
    assert needs_human_reason_key not in s7.threads_addressed_ids
