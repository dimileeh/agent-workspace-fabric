"""Preserved-commit ``needs_human`` verdicts stay publish-dependent.

The #925 correction outcomes (``correction_unscoped_fix_outcome`` and the
no-evidence arm of ``correction_self_citation_outcome``) escalate an item to
``needs_human`` while deliberately keeping the agent's commit. That commit only
reaches the PR on a successful push, and a ``needs_human`` item is excluded from
``AddressComments`` — so treating it like an ordinary human-only verdict on push
failure strands the "preserved for human review" commit in the local worktree
forever, where a later re-address abandons it as unpublished repair history
(PRRT_kwDOSJAM6s6fpjBw).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    CheckFailure,
    CheckState,
    MergeableState,
    MergeStateStatus,
    MonitorState,
    PRStatus,
    ReviewComment,
    ReviewThread,
)
from awf.runtime.pr_monitor_runner import fix_cycle
from awf.runtime.pr_monitor_runner.comment_verdict import (
    MonitorVerdictResult,
    VerdictResult,
)
from awf.runtime.pr_monitor_runner.fix_cycle import (
    _requeue_workflow_scope_publish_dependent_items,
)
from awf.runtime.pr_monitor_runner.helpers import (
    _clear_preserved_unpublished_commit_markers,
    _has_pending_preserved_unpublished_commit,
    _has_preserved_unpublished_commit,
    _needs_human_reason_state_key,
    _preserved_unpublished_commit_retry_head,
    _preserved_unpublished_commit_state_key,
    _retain_preserved_unpublished_commit_head,
    _sync_needs_human_reason,
)
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    pr_payload,
    seed_monitoring_workspace,
)

_PRESERVED_REASON = (
    "FIXED claimed on the correction attempt, but this item's commit does not "
    "touch the reviewed path; the commit is preserved for human review."
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Yield a database session factory for PR monitor regressions."""
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


@pytest.mark.unit
def test_sync_needs_human_reason_keeps_the_preserved_commit_marker_until_published() -> None:
    """A later ordinary verdict must not drop a marker whose commit is still local.

    Fresh feedback can requeue the same item during the settle window. The
    superseding ``needs_human`` publishes nothing, so clearing the marker here
    would make the item non-publish-dependent while the earlier correction's
    commit is still at HEAD (PRRT_kwDOSJAM6s6fp2uJ).
    """
    state = MonitorState()

    _sync_needs_human_reason(
        state,
        "T_preserved",
        VerdictResult(
            verdict="needs_human",
            reason=_PRESERVED_REASON,
            preserved_unpublished_commit=True,
        ),
    )
    assert _has_preserved_unpublished_commit(state, "T_preserved")
    assert _preserved_unpublished_commit_state_key("T_preserved") in state.threads_addressed_ids

    # The superseding verdict replaces the reason but keeps the commit marker.
    _sync_needs_human_reason(
        state,
        "T_preserved",
        VerdictResult(verdict="needs_human", reason="operator decision required"),
    )
    assert _has_preserved_unpublished_commit(state, "T_preserved")
    assert (
        state.threads_addressed_ids[_needs_human_reason_state_key("T_preserved")]
        == "operator decision required"
    )

    # Publishing the branch is what retires the marker — and with it the abandon
    # exemption a failed push recorded for the same commit.
    _retain_preserved_unpublished_commit_head(state, "d" * 40)
    _clear_preserved_unpublished_commit_markers(state)
    assert not _has_preserved_unpublished_commit(state, "T_preserved")
    assert _preserved_unpublished_commit_retry_head(state) is None
    assert (
        state.threads_addressed_ids[_needs_human_reason_state_key("T_preserved")]
        == "operator decision required"
    )


@pytest.mark.unit
def test_ordinary_needs_human_never_sets_the_preserved_commit_marker() -> None:
    """A plain blocking verdict stays out of the publish-dependent set."""
    state = MonitorState()

    _sync_needs_human_reason(
        state,
        "issue:plain",
        VerdictResult(verdict="needs_human", reason="needs a product call"),
    )

    assert not _has_preserved_unpublished_commit(state, "issue:plain")


@pytest.mark.unit
def test_provider_failure_result_has_no_preserved_commit_flag() -> None:
    """``MonitorVerdictResult`` carries no protocol flag and must not crash the sync."""
    state = MonitorState()

    _sync_needs_human_reason(
        state,
        "issue:provider_failure",
        MonitorVerdictResult(verdict="agent_failed"),
    )

    assert not _has_preserved_unpublished_commit(state, "issue:provider_failure")


def _thread() -> ReviewThread:
    return ReviewThread(
        thread_id="T_preserved_commit",
        path="src/awf/runtime/pr_monitor_runner/ci_ops.py",
        line=957,
        body_excerpt="recheck terminal state before CI failure returns",
        author="reviewer",
    )


def _review_comment() -> ReviewComment:
    return ReviewComment(
        comment_id="issue:preserved_commit",
        body_excerpt="review-level concern about the CI recheck",
        body="review-level concern about the CI recheck",
        author="reviewer",
        source_kind="issue",
    )


def _runner_with_failed_push(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> object:
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=pr_payload(threads=[], reviews=[], comments=[]))
    cmd.queue_result(returncode=1, stderr="remote: pre-receive hook declined")
    return make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )


@pytest.mark.unit
async def test_push_failure_requeues_thread_whose_needs_human_preserved_a_commit(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The preserved commit is retryable: its verdict is cleared so it is re-addressed."""
    workspace_id = await seed_monitoring_workspace(factory)
    runner = _runner_with_failed_push(factory, tmp_path)
    thread = _thread()

    async def _address_thread(*, state: MonitorState, **_kwargs: object) -> str:
        _sync_needs_human_reason(
            state,
            thread.thread_id,
            VerdictResult(
                verdict="needs_human",
                reason=_PRESERVED_REASON,
                preserved_unpublished_commit=True,
            ),
        )
        return "needs_human"

    monkeypatch.setattr(runner, "_address_thread", _address_thread)

    state = MonitorState()
    result = await runner._run_fix_cycle(
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

    assert result.failed is True
    assert result.reason_code == "GIT_PUSH_FAILED"
    # Verdict + markers cleared, so the next poll re-enters AddressComments and
    # retries publishing instead of parking on NotifyHuman forever.
    assert thread.thread_id not in state.threads_addressed_ids
    assert _needs_human_reason_state_key(thread.thread_id) not in state.threads_addressed_ids
    assert not _has_preserved_unpublished_commit(state, thread.thread_id)


@pytest.mark.unit
async def test_push_failure_requeues_review_comment_whose_needs_human_preserved_a_commit(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review-level items carry the same publish dependency as inline threads."""
    workspace_id = await seed_monitoring_workspace(factory)
    runner = _runner_with_failed_push(factory, tmp_path)
    comment = _review_comment()

    async def _address_review_comment_result(**_kwargs: object) -> VerdictResult:
        return VerdictResult(
            verdict="needs_human",
            reason=_PRESERVED_REASON,
            preserved_unpublished_commit=True,
        )

    monkeypatch.setattr(runner, "_address_review_comment_result", _address_review_comment_result)

    state = MonitorState()
    result = await runner._run_fix_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        initial_threads=(),
        initial_reviews=(comment,),
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert comment.comment_id not in state.threads_addressed_ids
    assert not _has_preserved_unpublished_commit(state, comment.comment_id)


@pytest.mark.unit
async def test_push_failure_records_provenance_for_the_preserved_commit(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The requeue only retries if the next cycle can prove AWF owns the local commit.

    ``_abandon_unpublished_comment_repairs`` runs before the re-address and resets a
    local-ahead HEAD only when a prior repair operation recorded it. Without that
    provenance the next cycle fails closed on
    ``COMMENT_REPAIR_UNPUBLISHED_PROVENANCE_MISSING`` instead of retrying the push
    (PRRT_kwDOSJAM6s6fp2uF).
    """
    workspace_id = await seed_monitoring_workspace(factory)
    runner = _runner_with_failed_push(factory, tmp_path)
    thread = _thread()
    repair_head = "c" * 40

    async def _address_thread(*, state: MonitorState, **_kwargs: object) -> str:
        _sync_needs_human_reason(
            state,
            thread.thread_id,
            VerdictResult(
                verdict="needs_human",
                reason=_PRESERVED_REASON,
                preserved_unpublished_commit=True,
            ),
        )
        return "needs_human"

    async def _rev_parse_head(_path: Path) -> str:
        return repair_head

    monkeypatch.setattr(runner, "_address_thread", _address_thread)
    monkeypatch.setattr(runner, "_rev_parse_head", _rev_parse_head)

    state = MonitorState()
    result = await runner._run_fix_cycle(
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

    assert result.failed is True
    assert result.reason_code == "GIT_PUSH_FAILED"
    # Retryable, not terminal — and still fingerprinted, so the operation result
    # the monitor loop persists carries the abandon-path proof.
    assert result.terminal_monitor_failure is False
    assert result.details is not None
    assert result.details.get("local_terminal_head_sha") == repair_head
    assert result.failure_evidence().get("local_terminal_head_sha") == repair_head
    # ...and that same provenance now exempts the commit from the next cycle's
    # abandon, so the requeue retries publishing it instead of resetting the
    # commit the escalation preserved for human review (PRRT_kwDOSJAM6s6fqJVM).
    assert _preserved_unpublished_commit_retry_head(state) == repair_head


@pytest.mark.unit
async def test_push_failure_on_an_ordinary_fix_records_no_abandon_exemption(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A committed fix keeps the ordinary abandon-then-re-address contract."""
    workspace_id = await seed_monitoring_workspace(factory)
    runner = _runner_with_failed_push(factory, tmp_path)
    thread = _thread()

    async def _address_thread(**_kwargs: object) -> str:
        return "fix_committed"

    monkeypatch.setattr(runner, "_address_thread", _address_thread)

    state = MonitorState()
    result = await runner._run_fix_cycle(
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

    assert result.failed is True
    assert thread.thread_id not in state.threads_addressed_ids
    assert _preserved_unpublished_commit_retry_head(state) is None


@pytest.mark.unit
async def test_repeated_push_failure_moves_the_exemption_to_the_new_head(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exemption follows the branch while the preserved commit stays unpublished.

    The requeued cycle re-addresses the item on top of the exempt commit, so an
    ordinary verdict there advances HEAD. If a second push also fails, the
    exemption must move to the new head — it is keyed by the exact SHA, so
    leaving it on the now-ancestor commit would let the next cycle's abandon
    reset the whole local branch and delete the commit the #925 escalation
    preserved for human review (PRRT_kwDOSJAM6s6fqJVM).
    """
    workspace_id = await seed_monitoring_workspace(factory)
    runner = _runner_with_failed_push(factory, tmp_path)
    thread = _thread()
    preserved_head = "b" * 40
    advanced_head = "c" * 40

    async def _address_thread(**_kwargs: object) -> str:
        return "fix_committed"

    async def _rev_parse_head(_path: Path) -> str:
        return advanced_head

    monkeypatch.setattr(runner, "_address_thread", _address_thread)
    monkeypatch.setattr(runner, "_rev_parse_head", _rev_parse_head)

    state = MonitorState()
    # The prior cycle's failed push recorded the preserved commit as exempt.
    _retain_preserved_unpublished_commit_head(state, preserved_head)

    result = await runner._run_fix_cycle(
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

    assert result.failed is True
    assert _preserved_unpublished_commit_retry_head(state) == advanced_head


def _settle_status(*threads: ReviewThread, head_sha: str) -> PRStatus:
    return PRStatus(
        number=42,
        head_sha=head_sha,
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=threads,
        unresolved_review_comments=(),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
    )


def _preserved_then_ordinary_verdicts() -> list[VerdictResult]:
    return [
        VerdictResult(
            verdict="needs_human",
            reason=_PRESERVED_REASON,
            preserved_unpublished_commit=True,
        ),
        VerdictResult(verdict="needs_human", reason="operator decision required"),
    ]


def _settle_runner(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    workspace_id: str,
    push_result: _GitPushResult,
    settle_threads: tuple[ReviewThread, ...],
    remote_head: str,
    verdicts: list[VerdictResult],
) -> object:
    """Build a runner whose settle re-poll re-addresses ``settle_threads`` once."""
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0)  # pass-1 item cat-file
    cmd.queue_result(returncode=0)  # pass-2 item cat-file
    worktrees_root = tmp_path / "worktrees"
    (worktrees_root / workspace_id).mkdir(parents=True)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=worktrees_root,
    )
    pending = iter(verdicts)
    settle_calls = 0

    async def _start_head(**_kwargs: object) -> tuple[str, None]:
        return (remote_head, None)

    async def _no_dirty(**_kwargs: object) -> None:
        return None

    async def _rev_parse_head(_worktree_path: Path) -> str:
        return remote_head

    async def _address_thread(*, state: MonitorState, **kwargs: object) -> str:
        thread = kwargs["thread"]
        result = next(pending)
        _sync_needs_human_reason(state, thread.thread_id, result)  # type: ignore[union-attr]
        return result.verdict

    async def _settle(**_kwargs: object) -> PRStatus:
        nonlocal settle_calls
        settle_calls += 1
        if settle_calls == 1:
            return _settle_status(*settle_threads, head_sha=remote_head)
        return _settle_status(head_sha=remote_head)

    async def _no_block(**_kwargs: object) -> None:
        return None

    async def _validated(**_kwargs: object) -> _GitPushResult:
        return push_result

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head)
    monkeypatch.setattr(runner, "_rev_parse_head", _rev_parse_head)
    monkeypatch.setattr(runner, "_address_thread", _address_thread)
    monkeypatch.setattr(runner._deps.gh, "fetch_pr_status", _settle)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _no_block)
    monkeypatch.setattr(runner, "_validated_git_push_result", _validated)
    return runner


@pytest.mark.unit
async def test_settle_readdress_keeps_the_preserved_commit_publish_dependent(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fresh feedback mid-cycle must not strand the commit an earlier pass preserved.

    Pass 1 escalates with a preserved commit; a reviewer reply lands during the
    settle window and pass 2 returns an ordinary ``needs_human`` that publishes
    nothing. The commit is still at HEAD, so the item must stay publish-dependent
    and be requeued by the failed push instead of parking on ``NotifyHuman``
    with unpublished repair history (PRRT_kwDOSJAM6s6fp2uJ).
    """
    workspace_id = await seed_monitoring_workspace(factory)
    remote_head = "a" * 40
    thread = _thread()
    replied = ReviewThread(
        thread_id=thread.thread_id,
        path=thread.path,
        line=thread.line,
        body_excerpt=f"{thread.body_excerpt}\n\nreviewer reply during settle",
        author=thread.author,
    )
    runner = _settle_runner(
        factory,
        tmp_path,
        monkeypatch,
        workspace_id=workspace_id,
        push_result=_GitPushResult(
            pushed=False,
            failed=True,
            returncode=1,
            stderr="fatal: unable to access remote: connection reset",
        ),
        settle_threads=(replied,),
        remote_head=remote_head,
        verdicts=_preserved_then_ordinary_verdicts(),
    )

    state = MonitorState()
    result = await runner._run_fix_cycle(  # type: ignore[attr-defined]
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha=remote_head,
        initial_threads=(thread,),
        initial_reviews=(),
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert thread.thread_id not in state.threads_addressed_ids
    assert _needs_human_reason_state_key(thread.thread_id) not in state.threads_addressed_ids
    assert not _has_preserved_unpublished_commit(state, thread.thread_id)


@pytest.mark.unit
async def test_defer_capture_failure_keeps_the_preserved_commit_publish_dependent(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The capture-failure downgrade must carry the publish dependency too.

    Pass 1 escalates with a preserved commit; a reviewer reply during settle
    re-addresses the thread to ``defer``, and the durable capture fails
    permanently so the caller downgrades it back to ``needs_human``. That
    downgrade happens inside the ``defer`` branch, so the item must still pick up
    the preserved-commit dependency — otherwise the failed push leaves the
    verdict addressed and strands the commit locally with no retry
    (PRRT_kwDOSJAM6s6fqM4Q).
    """
    workspace_id = await seed_monitoring_workspace(factory)
    remote_head = "a" * 40
    thread = _thread()
    replied = ReviewThread(
        thread_id=thread.thread_id,
        path=thread.path,
        line=thread.line,
        body_excerpt=f"{thread.body_excerpt}\n\nreviewer asks to track this separately",
        author=thread.author,
    )
    runner = _settle_runner(
        factory,
        tmp_path,
        monkeypatch,
        workspace_id=workspace_id,
        push_result=_GitPushResult(
            pushed=False,
            failed=True,
            returncode=1,
            stderr="fatal: unable to access remote: connection reset",
        ),
        settle_threads=(replied,),
        remote_head=remote_head,
        verdicts=[
            VerdictResult(
                verdict="needs_human",
                reason=_PRESERVED_REASON,
                preserved_unpublished_commit=True,
            ),
            VerdictResult(verdict="defer", reason="track the follow-up separately"),
        ],
    )

    async def _capture_fails_permanently(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(fix_cycle, "_capture_deferred_review_thread", _capture_fails_permanently)

    state = MonitorState()
    result = await runner._run_fix_cycle(  # type: ignore[attr-defined]
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha=remote_head,
        initial_threads=(thread,),
        initial_reviews=(),
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    # Requeued: the next poll re-addresses the thread and retries publishing the
    # commit the escalation preserved, instead of parking on ``NotifyHuman``.
    assert thread.thread_id not in state.threads_addressed_ids
    assert _needs_human_reason_state_key(thread.thread_id) not in state.threads_addressed_ids
    assert not _has_preserved_unpublished_commit(state, thread.thread_id)


@pytest.mark.unit
async def test_defer_capture_failure_without_a_preserved_commit_stays_addressed(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The effective-verdict dependency stays narrow: no commit, no requeue.

    Same capture-failure downgrade as above, but nothing preserved a local
    commit. The resulting ``needs_human`` publishes nothing, so a push failure
    must leave it addressed rather than forcing a pointless re-address of
    feedback the agent already judged to need a human.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    remote_head = "a" * 40
    thread = _thread()
    runner = _settle_runner(
        factory,
        tmp_path,
        monkeypatch,
        workspace_id=workspace_id,
        push_result=_GitPushResult(
            pushed=False,
            failed=True,
            returncode=1,
            stderr="fatal: unable to access remote: connection reset",
        ),
        settle_threads=(),
        remote_head=remote_head,
        verdicts=[VerdictResult(verdict="defer", reason="track the follow-up separately")],
    )

    async def _capture_fails_permanently(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(fix_cycle, "_capture_deferred_review_thread", _capture_fails_permanently)

    state = MonitorState()
    result = await runner._run_fix_cycle(  # type: ignore[attr-defined]
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha=remote_head,
        initial_threads=(thread,),
        initial_reviews=(),
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert state.threads_addressed_ids.get(thread.thread_id) == "needs_human"


@pytest.mark.unit
async def test_successful_push_retires_the_preserved_commit_marker(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Publishing the branch ends the publish dependency; the verdict still blocks."""
    workspace_id = await seed_monitoring_workspace(factory)
    remote_head = "a" * 40
    thread = _thread()
    runner = _settle_runner(
        factory,
        tmp_path,
        monkeypatch,
        workspace_id=workspace_id,
        push_result=_GitPushResult(pushed=True, failed=False, returncode=0),
        settle_threads=(),
        remote_head=remote_head,
        verdicts=_preserved_then_ordinary_verdicts()[:1],
    )

    state = MonitorState()
    result = await runner._run_fix_cycle(  # type: ignore[attr-defined]
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha=remote_head,
        initial_threads=(thread,),
        initial_reviews=(),
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is False
    assert state.threads_addressed_ids[thread.thread_id] == "needs_human"
    assert not _has_preserved_unpublished_commit(state, thread.thread_id)


@pytest.mark.unit
async def test_push_failure_still_preserves_an_ordinary_needs_human_thread(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No commit to publish means no requeue — the existing #925 contract holds."""
    workspace_id = await seed_monitoring_workspace(factory)
    runner = _runner_with_failed_push(factory, tmp_path)
    thread = _thread()

    async def _address_thread(*, state: MonitorState, **_kwargs: object) -> str:
        _sync_needs_human_reason(
            state,
            thread.thread_id,
            VerdictResult(verdict="needs_human", reason="needs an operator decision"),
        )
        return "needs_human"

    monkeypatch.setattr(runner, "_address_thread", _address_thread)

    state = MonitorState()
    result = await runner._run_fix_cycle(
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

    assert result.failed is True
    assert state.threads_addressed_ids[thread.thread_id] == "needs_human"
    assert (
        state.threads_addressed_ids[_needs_human_reason_state_key(thread.thread_id)]
        == "needs an operator decision"
    )


@pytest.mark.unit
def test_workflow_scope_requeue_keeps_the_preserved_commit_marker() -> None:
    """The non-publishing requeue must not drop a marker whose commit is still local.

    ``awaiting_workflow_scope`` deliberately keeps the local commit, so the item
    stays publish-dependent when the next cycle re-addresses it to an ordinary
    ``needs_human`` (PRRT_kwDOSJAM6s6fqJVN).
    """
    state = MonitorState(
        threads_addressed_ids={
            "T_preserved": "needs_human",
            "__review_thread_body_hash__:T_preserved": "preserved-hash",
            _needs_human_reason_state_key("T_preserved"): _PRESERVED_REASON,
            _preserved_unpublished_commit_state_key("T_preserved"): "1",
            "issue:preserved": "needs_human",
            _preserved_unpublished_commit_state_key("issue:preserved"): "1",
        }
    )

    _requeue_workflow_scope_publish_dependent_items(
        state,
        ["T_preserved"],
        resolution_dependent_ids=["issue:preserved"],
        reason="token lacks `workflow` scope",
    )

    # Verdict + reason cleared so the next cycle re-addresses the item...
    assert "T_preserved" not in state.threads_addressed_ids
    assert "__review_thread_body_hash__:T_preserved" not in state.threads_addressed_ids
    assert _needs_human_reason_state_key("T_preserved") not in state.threads_addressed_ids
    assert "issue:preserved" not in state.threads_addressed_ids
    # ...but the unpublished commit keeps its publish dependency.
    assert _has_preserved_unpublished_commit(state, "T_preserved")
    assert _has_preserved_unpublished_commit(state, "issue:preserved")


@pytest.mark.unit
async def test_workflow_scope_push_failure_keeps_the_preserved_commit_marker(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A workflow-scope rejection requeues the item without retiring its marker."""
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=pr_payload(threads=[], reviews=[], comments=[]))
    cmd.queue_result(
        returncode=1,
        stderr=(
            "remote: refusing to allow a Personal Access Token to create or update workflow "
            "`.github/workflows/publish.yml` without `workflow` scope"
        ),
    )
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    thread = _thread()

    async def _address_thread(*, state: MonitorState, **_kwargs: object) -> str:
        _sync_needs_human_reason(
            state,
            thread.thread_id,
            VerdictResult(
                verdict="needs_human",
                reason=_PRESERVED_REASON,
                preserved_unpublished_commit=True,
            ),
        )
        return "needs_human"

    monkeypatch.setattr(runner, "_address_thread", _address_thread)

    state = MonitorState()
    result = await runner._run_fix_cycle(
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

    assert result.failed is True
    assert thread.thread_id not in state.threads_addressed_ids
    # The commit is still local and still needs publishing, so a later ordinary
    # ``needs_human`` on the re-address stays publish-dependent.
    assert _has_preserved_unpublished_commit(state, thread.thread_id)


@pytest.mark.unit
def test_pending_preserved_commit_tracks_markers_and_the_retry_head() -> None:
    """Either an item marker or a carried-forward retry head means "still local"."""
    state = MonitorState()
    assert not _has_pending_preserved_unpublished_commit(state)

    state.mark_addressed(_preserved_unpublished_commit_state_key("T_preserved"), "1")
    assert _has_pending_preserved_unpublished_commit(state)

    # The push-failure requeue clears the item's marker but keeps the retry head,
    # so the commit is still unpublished on the next cycle's push.
    state.threads_addressed_ids.pop(_preserved_unpublished_commit_state_key("T_preserved"))
    assert not _has_pending_preserved_unpublished_commit(state)
    _retain_preserved_unpublished_commit_head(state, "f" * 40)
    assert _has_pending_preserved_unpublished_commit(state)


def _resync_probe_runner(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    push_kwargs: dict[str, object],
    local_head: str,
) -> object:
    """Runner whose fix-cycle push records its kwargs and rejects non-fast-forward."""
    runner = _runner_with_failed_push(factory, tmp_path)

    async def _rev_parse_head(_path: Path) -> str:
        return local_head

    async def _no_block(**_kwargs: object) -> None:
        return None

    async def _validated(**kwargs: object) -> _GitPushResult:
        push_kwargs.update(kwargs)
        return _GitPushResult(
            pushed=False,
            failed=True,
            returncode=1,
            stderr="! [rejected] awf/ws -> awf/ws (non-fast-forward)",
            reason_code="GIT_PUSH_REJECTED_NON_FAST_FORWARD",
        )

    monkeypatch.setattr(runner, "_rev_parse_head", _rev_parse_head)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _no_block)
    monkeypatch.setattr(runner, "_validated_git_push_result", _validated)
    return runner


@pytest.mark.unit
async def test_push_suppresses_resync_while_a_preserved_commit_is_unpublished(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-fast-forward resync would delete the commit the escalation preserved.

    ``_git_push_result``'s divergence recovery fetches the advanced remote tip and
    ``reset --hard``s onto it before reporting failure. The retry head recorded
    below would then be the remote SHA, not the preserved commit, so the requeued
    cycle could never publish it. The push must therefore run with the resync
    disabled while such a commit is unpublished (PRRT_kwDOSJAM6s6fqc0l).
    """
    workspace_id = await seed_monitoring_workspace(factory)
    preserved_head = "e" * 40
    push_kwargs: dict[str, object] = {}
    runner = _resync_probe_runner(
        factory,
        tmp_path,
        monkeypatch,
        push_kwargs=push_kwargs,
        local_head=preserved_head,
    )
    thread = _thread()

    async def _address_thread(*, state: MonitorState, **_kwargs: object) -> str:
        _sync_needs_human_reason(
            state,
            thread.thread_id,
            VerdictResult(
                verdict="needs_human",
                reason=_PRESERVED_REASON,
                preserved_unpublished_commit=True,
            ),
        )
        return "needs_human"

    monkeypatch.setattr(runner, "_address_thread", _address_thread)

    state = MonitorState()
    result = await runner._run_fix_cycle(  # type: ignore[attr-defined]
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

    assert result.failed is True
    assert push_kwargs["allow_resync_on_rejection"] is False
    # The worktree still holds the preserved commit, so the exemption points at
    # it rather than at an advanced remote tip.
    assert _preserved_unpublished_commit_retry_head(state) == preserved_head


@pytest.mark.unit
async def test_push_keeps_resync_when_no_preserved_commit_is_pending(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ordinary repairs keep the divergence recovery that unblocks the next push."""
    workspace_id = await seed_monitoring_workspace(factory)
    push_kwargs: dict[str, object] = {}
    runner = _resync_probe_runner(
        factory,
        tmp_path,
        monkeypatch,
        push_kwargs=push_kwargs,
        local_head="a" * 40,
    )
    thread = _thread()

    async def _address_thread(**_kwargs: object) -> str:
        return "fix_committed"

    monkeypatch.setattr(runner, "_address_thread", _address_thread)

    state = MonitorState()
    result = await runner._run_fix_cycle(  # type: ignore[attr-defined]
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

    assert result.failed is True
    assert push_kwargs["allow_resync_on_rejection"] is True


async def _run_ci_fix_capturing_push_kwargs(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    workspace_id: str,
    state: MonitorState | None,
) -> dict[str, object]:
    """Run the CI-repair cycle with a stubbed push and return its kwargs."""
    adapter = FakeAdapter()
    adapter.queue(stdout="attempted ci fix")
    cmd = FakeCommandRunner()
    worktrees_root = tmp_path / "worktrees"
    (worktrees_root / workspace_id).mkdir(parents=True, exist_ok=True)
    cmd.queue_result(returncode=0, stdout="")  # clean worktree before repair
    cmd.queue_result(returncode=0, stdout="abc1234567890def\n")  # operation start HEAD
    cmd.queue_result(returncode=0)  # operation start HEAD exists
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=worktrees_root,
    )
    push_kwargs: dict[str, object] = {}

    async def _committed(**_kwargs: object) -> bool:
        return True

    async def _no_block(**_kwargs: object) -> None:
        return None

    async def _validated(**kwargs: object) -> _GitPushResult:
        push_kwargs.update(kwargs)
        return _GitPushResult(
            pushed=False,
            failed=True,
            returncode=1,
            stderr="! [rejected] awf/ws -> awf/ws (non-fast-forward)",
            reason_code="GIT_PUSH_REJECTED_NON_FAST_FORWARD",
        )

    monkeypatch.setattr(runner, "_commit_dirty_worktree", _committed)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _no_block)
    monkeypatch.setattr(runner, "_validated_git_push_result", _validated)

    await runner._run_ci_fix(
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        failures=(CheckFailure(name="pytest", conclusion="FAILURE", log_excerpt="assert 1 == 2"),),
        compose_project=f"awf_{workspace_id}",
        compose_file=tmp_path / "compose.yml",
        workspace_id=workspace_id,
        remote_branch=f"awf/{workspace_id}",
        state=state,
    )
    return push_kwargs


@pytest.mark.unit
async def test_ci_repair_push_suppresses_resync_while_a_preserved_commit_is_unpublished(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CI-repair push shares the worktree holding the preserved commit.

    A preserved-commit marker (or retry head) survives across monitor cycles, and
    ``decide()`` can pick ``ReportCiFailure`` on the very next cycle. That push
    would resync on a non-fast-forward rejection and ``reset --hard`` away exactly
    the commit the fix-cycle guard protects, so it must carry the same suppression
    (PRRT_kwDOSJAM6s6fqc0l).
    """
    workspace_id = await seed_monitoring_workspace(factory)
    state = MonitorState()
    _retain_preserved_unpublished_commit_head(state, "e" * 40)

    push_kwargs = await _run_ci_fix_capturing_push_kwargs(
        factory,
        tmp_path,
        monkeypatch,
        workspace_id=workspace_id,
        state=state,
    )

    assert push_kwargs["allow_resync_on_rejection"] is False


@pytest.mark.unit
async def test_ci_repair_push_keeps_resync_without_a_preserved_commit(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ordinary CI repairs — and stateless calls — keep the divergence recovery."""
    workspace_id = await seed_monitoring_workspace(factory)

    with_state = await _run_ci_fix_capturing_push_kwargs(
        factory,
        tmp_path,
        monkeypatch,
        workspace_id=workspace_id,
        state=MonitorState(),
    )
    without_state = await _run_ci_fix_capturing_push_kwargs(
        factory,
        tmp_path,
        monkeypatch,
        workspace_id=workspace_id,
        state=None,
    )

    assert with_state["allow_resync_on_rejection"] is True
    assert without_state["allow_resync_on_rejection"] is True
