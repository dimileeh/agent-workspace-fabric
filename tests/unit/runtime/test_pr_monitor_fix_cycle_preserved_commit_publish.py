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
from awf.runtime.pr_monitor import MonitorState, ReviewComment, ReviewThread
from awf.runtime.pr_monitor_runner.comment_verdict import (
    MonitorVerdictResult,
    VerdictResult,
)
from awf.runtime.pr_monitor_runner.helpers import (
    _has_preserved_unpublished_commit,
    _needs_human_reason_state_key,
    _preserved_unpublished_commit_state_key,
    _sync_needs_human_reason,
)
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
def test_sync_needs_human_reason_records_and_clears_the_preserved_commit_marker() -> None:
    """Only a preserved-commit ``needs_human`` leaves the publish-dependency marker."""
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

    # An ordinary human-only verdict on the same item clears the marker again.
    _sync_needs_human_reason(
        state,
        "T_preserved",
        VerdictResult(verdict="needs_human", reason="operator decision required"),
    )
    assert not _has_preserved_unpublished_commit(state, "T_preserved")
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
