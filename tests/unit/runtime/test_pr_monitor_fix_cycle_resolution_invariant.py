"""#925: a fix cycle must never end with a dispositioned thread nobody owns.

A thread that ends the cycle with a resolvable verdict (``fix_committed`` /
``false_positive`` / ``defer``) whose recorded body hash still matches the live
conversation is not waiting on more agent work — it is waiting on
``resolve_thread``. On PR #922 such threads were excluded from in-cycle
resolution by ``already_outdated_at_batch_entry`` (their outdatedness came from
the item's own, subsequently rolled-back commit), and the next poll's
outdated-hygiene path never saw them because they were active again. The verdict
plus matching hash then made ``thread_needs_attention`` False forever: a silent
merge blocker with no ``NotifyHuman`` and no reason code.

These tests pin the invariant: resolved in this cycle, demonstrably owned by
another path (outdated hygiene / AddressComments), or escalated to
``needs_human`` with ``THREAD_RESOLUTION_OWNER_MISSING``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import GitHubClientError, RepoRef
from awf.db.session import make_session_factory
from awf.runtime.feedback_policy import (
    review_thread_body_hash,
    review_thread_body_state_key,
    thread_enters_address_comments,
    thread_resolution_pending,
)
from awf.runtime.pr_monitor import (
    CheckState,
    MergeableState,
    MergeStateStatus,
    MonitorState,
    PRStatus,
    ReviewThread,
)
from awf.runtime.pr_monitor_runner.fix_cycle import _deferred_issue_filed_marker
from awf.runtime.pr_monitor_runner.fix_cycle_resolution_invariant import (
    RESOLUTION_OWNER_MISSING_REASON,
    stranded_resolvable_thread_ids,
)
from tests.postgres import postgres_test_engine
from tests.shared.monitor_runner import DefaultMergeMethodGitHubClient
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)

_THREAD_ID = "PRRT_stranded"
_PRIOR_THREAD_ID = "PRRT_stranded_by_earlier_cycle"


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


class _ScriptedSettleClient(DefaultMergeMethodGitHubClient):
    """gh double with scripted settle statuses and a recording ``resolve_thread``."""

    def __init__(
        self,
        runner: FakeCommandRunner,
        *,
        statuses: Sequence[PRStatus] = (),
        status_error: Exception | None = None,
        status_error_on_call: int | None = None,
    ) -> None:
        super().__init__(runner)
        self._statuses = list(statuses)
        self._status_error = status_error
        self._status_error_on_call = status_error_on_call
        self.resolved: list[str] = []
        self.status_calls = 0

    async def fetch_pr_status(
        self, *, repo: RepoRef, pr_number: int, base_behind_count: int, retry: bool = True
    ) -> PRStatus:
        del repo, pr_number, base_behind_count, retry
        self.status_calls += 1
        if self._status_error is not None and self._status_error_on_call in (
            None,
            self.status_calls,
        ):
            raise self._status_error
        index = min(self.status_calls - 1, len(self._statuses) - 1)
        return self._statuses[index]

    async def resolve_thread(self, *, thread_id: str) -> None:
        self.resolved.append(thread_id)


def _thread(
    *,
    thread_id: str = _THREAD_ID,
    body: str = "please adjust this",
    is_outdated: bool = False,
) -> ReviewThread:
    return ReviewThread(
        thread_id=thread_id,
        path="src/foo.py",
        line=12,
        body_excerpt=body,
        author="review-bot",
        is_outdated=is_outdated,
    )


def _status(
    *,
    active: Sequence[ReviewThread] = (),
    outdated: Sequence[ReviewThread] = (),
) -> PRStatus:
    return PRStatus(
        number=42,
        head_sha="abc1234567890def",
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=tuple(active),
        unresolved_review_comments=(),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
        outdated_unresolved_inline_threads=tuple(outdated),
    )


async def _run_cycle(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    *,
    gh_statuses: Sequence[PRStatus] = (),
    status_error: Exception | None = None,
    status_error_on_call: int | None = None,
    verdicts: Sequence[str] = ("AWF-VERDICT: FIXED: committed locally",),
    initial_threads: Sequence[ReviewThread],
    state: MonitorState | None = None,
) -> tuple[_ScriptedSettleClient, MonitorState]:
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0)  # push
    cmd.queue_result(returncode=0, stdout="newsha\n")  # rev-parse HEAD
    adapter = FakeAdapter()
    for verdict in verdicts:
        adapter.queue(stdout=verdict)
    gh = _ScriptedSettleClient(
        cmd,
        statuses=gh_statuses,
        status_error=status_error,
        status_error_on_call=status_error_on_call,
    )
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )

    async def _commit_dirty(**_kwargs: object) -> bool:
        return True

    runner._commit_dirty_worktree = _commit_dirty  # type: ignore[method-assign]
    state = MonitorState() if state is None else state

    await runner._run_fix_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        initial_threads=tuple(initial_threads),
        initial_reviews=(),
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )
    return gh, state


@pytest.mark.unit
async def test_thread_outdated_at_batch_entry_but_active_at_settle_is_resolved_in_cycle(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """#925 D3: a rolled-back commit un-outdates the thread — nobody else owns it."""
    entry_thread = _thread(is_outdated=True)
    settle_thread = _thread(is_outdated=False)

    gh, state = await _run_cycle(
        factory,
        tmp_path,
        gh_statuses=[_status(active=[settle_thread])],
        initial_threads=[entry_thread],
    )

    assert gh.resolved == [_THREAD_ID]
    assert state.threads_addressed_ids[_THREAD_ID] == "fix_committed"


@pytest.mark.unit
async def test_thread_still_outdated_at_settle_is_left_to_outdated_hygiene(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Guard (#484): outdated hygiene on the next poll keeps owning resolution."""
    entry_thread = _thread(is_outdated=True)
    settle_thread = _thread(is_outdated=True)

    gh, state = await _run_cycle(
        factory,
        tmp_path,
        gh_statuses=[_status(outdated=[settle_thread])],
        initial_threads=[entry_thread],
    )

    assert gh.resolved == []
    assert state.threads_addressed_ids[_THREAD_ID] == "fix_committed"


@pytest.mark.unit
async def test_thread_with_changed_body_at_settle_is_not_resolved_and_re_enters_repair(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Guard: fresh reviewer feedback keeps the thread in AddressComments."""
    entry_thread = _thread(is_outdated=True)
    replies = [_status(active=[_thread(body=f"reviewer reply {index}")]) for index in range(1, 5)]

    gh, state = await _run_cycle(
        factory,
        tmp_path,
        gh_statuses=replies,
        verdicts=["AWF-VERDICT: FIXED: committed locally"] * 4,
        initial_threads=[entry_thread],
    )

    assert gh.resolved == []
    latest = _thread(body="reviewer reply 4")
    assert thread_enters_address_comments(state.threads_addressed_ids, latest)


@pytest.mark.unit
async def test_unownable_resolvable_thread_is_escalated_to_needs_human_with_reason_code(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """No settle evidence at all: escalate rather than silently strand (#925)."""
    entry_thread = _thread(is_outdated=True)

    gh, state = await _run_cycle(
        factory,
        tmp_path,
        status_error=GitHubClientError(
            operation="fetch_pr_status",
            returncode=1,
            stderr="HTTP 503: service unavailable",
        ),
        initial_threads=[entry_thread],
    )

    assert gh.resolved == []
    assert state.threads_addressed_ids[_THREAD_ID] == "needs_human"
    reason = state.threads_addressed_ids["__needs_human_reason__:" + _THREAD_ID]
    assert RESOLUTION_OWNER_MISSING_REASON in reason


@pytest.mark.unit
async def test_failed_later_settle_poll_escalates_instead_of_resolving_on_stale_feed(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6fm4VR: a stale settle feed is not proof of no fresh reply.

    Pass 1's settle poll succeeds and shows a reviewer reply, so pass 2 re-addresses
    the thread; pass 2's settle poll then fails transiently and breaks the loop. The
    surviving ``status`` is pass 1's feed, in which the thread is active with a body
    hash that now matches the recorded verdict — enough to look sweepable. A reply
    may have landed during the failed poll window, so the sweep must treat the feed
    as missing and escalate rather than resolve past feedback it never saw.
    """
    entry_thread = _thread(is_outdated=True)

    gh, state = await _run_cycle(
        factory,
        tmp_path,
        gh_statuses=[_status(active=[_thread(body="reviewer reply 1")])],
        status_error=GitHubClientError(
            operation="fetch_pr_status",
            returncode=1,
            stderr="HTTP 503: service unavailable",
        ),
        status_error_on_call=2,
        verdicts=["AWF-VERDICT: FIXED: committed locally"] * 2,
        initial_threads=[entry_thread],
    )

    assert gh.status_calls == 2
    assert gh.resolved == []
    assert state.threads_addressed_ids[_THREAD_ID] == "needs_human"
    reason = state.threads_addressed_ids["__needs_human_reason__:" + _THREAD_ID]
    assert RESOLUTION_OWNER_MISSING_REASON in reason


@pytest.mark.unit
async def test_failed_later_settle_poll_escalates_prior_cycle_orphan_from_stale_feed(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6fm7wj: a discarded feed must not take its orphans with it.

    Pass 1's settle poll exposes a thread stranded by an EARLIER cycle (resolvable
    verdict + matching hash, so it never re-enters AddressComments and is absent
    from this cycle's deferred candidates). Pass 2's poll then fails transiently,
    so the feed is no longer fresh enough to license a resolve. It is still proof
    the conversation is open, so the orphan must escalate to ``needs_human``
    rather than end the cycle unresolved *and* unescalated — the silent permanent
    merge blocker of #925.
    """
    prior = _thread(thread_id=_PRIOR_THREAD_ID, body="earlier cycle feedback")
    state = MonitorState()
    state.threads_addressed_ids.update(_state_map("fix_committed", prior))

    gh, state = await _run_cycle(
        factory,
        tmp_path,
        gh_statuses=[_status(active=[_thread(body="reviewer reply 1"), prior])],
        status_error=GitHubClientError(
            operation="fetch_pr_status",
            returncode=1,
            stderr="HTTP 503: service unavailable",
        ),
        status_error_on_call=2,
        verdicts=["AWF-VERDICT: FIXED: committed locally"] * 2,
        initial_threads=[_thread(is_outdated=True)],
        state=state,
    )

    assert gh.status_calls == 2
    assert gh.resolved == []
    assert state.threads_addressed_ids[_PRIOR_THREAD_ID] == "needs_human"
    reason = state.threads_addressed_ids["__needs_human_reason__:" + _PRIOR_THREAD_ID]
    assert RESOLUTION_OWNER_MISSING_REASON in reason


@pytest.mark.unit
async def test_failed_later_settle_poll_ignores_stale_outdated_only_ownership(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6fqagY: stale outdatedness must not bypass needs_human.

    Pass 1's settle shows a prior-cycle orphan as outdated-only (hygiene would
    own it *if* that feed were fresh). Pass 2's poll fails, so ``settle_threads``
    is discarded but ``outdated_only_thread_ids`` would still be derived from the
    superseded status. Trusting that set would skip escalation; if the thread
    re-activated during the failed window without a body change, the next outer
    poll's hygiene path skips it and the matching resolvable hash keeps it out of
    AddressComments — a silent wedge. Escalate instead.
    """
    prior = _thread(
        thread_id=_PRIOR_THREAD_ID,
        body="earlier cycle feedback",
        is_outdated=True,
    )
    state = MonitorState()
    state.threads_addressed_ids.update(_state_map("fix_committed", prior))

    gh, state = await _run_cycle(
        factory,
        tmp_path,
        gh_statuses=[
            _status(
                active=[_thread(body="reviewer reply 1")],
                outdated=[prior],
            )
        ],
        status_error=GitHubClientError(
            operation="fetch_pr_status",
            returncode=1,
            stderr="HTTP 503: service unavailable",
        ),
        status_error_on_call=2,
        verdicts=["AWF-VERDICT: FIXED: committed locally"] * 2,
        initial_threads=[_thread(is_outdated=True)],
        state=state,
    )

    assert gh.status_calls == 2
    assert gh.resolved == []
    assert state.threads_addressed_ids[_PRIOR_THREAD_ID] == "needs_human"
    reason = state.threads_addressed_ids["__needs_human_reason__:" + _PRIOR_THREAD_ID]
    assert RESOLUTION_OWNER_MISSING_REASON in reason


@pytest.mark.unit
@pytest.mark.parametrize(
    ("entry_outdated", "settle"),
    [
        (True, "active"),
        (True, "outdated"),
        (True, "changed_body"),
        (False, "active"),
        (False, "outdated"),
    ],
)
async def test_no_thread_left_with_resolvable_verdict_and_matching_hash_unresolved(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    entry_outdated: bool,
    settle: str,
) -> None:
    """The #925 invariant across the batch-entry / settle matrix."""
    entry_thread = _thread(is_outdated=entry_outdated)
    if settle == "active":
        statuses = [_status(active=[_thread()])]
    elif settle == "outdated":
        statuses = [_status(outdated=[_thread(is_outdated=True)])]
    else:
        statuses = [
            _status(active=[_thread(body=f"reviewer reply {index}")]) for index in range(1, 5)
        ]

    gh, state = await _run_cycle(
        factory,
        tmp_path,
        gh_statuses=statuses,
        verdicts=["AWF-VERDICT: FIXED: committed locally"] * 4,
        initial_threads=[entry_thread],
    )

    final_status = statuses[-1]
    live_threads = (
        final_status.unresolved_inline_threads + final_status.outdated_unresolved_inline_threads
    )
    for thread in live_threads:
        if thread.thread_id in gh.resolved:
            continue
        if not thread_resolution_pending(state.threads_addressed_ids, thread):
            continue
        # Still pending resolution and not resolved in-cycle: an owner must exist.
        assert thread_enters_address_comments(state.threads_addressed_ids, thread) or (
            thread.is_outdated
        ), f"thread {thread.thread_id} left stranded for settle={settle}"


@pytest.mark.unit
async def test_thread_stranded_by_an_earlier_cycle_is_swept_when_another_comment_runs(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6fmmKc: the sweep must cover the whole settle feed.

    A thread stranded by an EARLIER cycle carries a resolvable verdict and a
    matching body hash, so it never re-enters AddressComments and is absent from
    this cycle's deferred candidates. It is still unresolved in the settle feed
    with no other owner — hygiene skips it (it is active again), so the fix cycle
    must adopt it rather than leave it stranded for another cycle.
    """
    prior = _thread(thread_id=_PRIOR_THREAD_ID, body="earlier cycle feedback")
    state = MonitorState()
    state.threads_addressed_ids.update(_state_map("fix_committed", prior))

    gh, state = await _run_cycle(
        factory,
        tmp_path,
        gh_statuses=[_status(active=[_thread(), prior])],
        initial_threads=[_thread()],
        state=state,
    )

    assert gh.resolved == [_THREAD_ID, _PRIOR_THREAD_ID]
    assert state.threads_addressed_ids[_PRIOR_THREAD_ID] == "fix_committed"


@pytest.mark.unit
async def test_thread_stranded_by_an_earlier_cycle_is_left_to_hygiene_while_outdated(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Guard (#484): the broadened sweep must not steal hygiene's outdated-only threads."""
    prior = _thread(thread_id=_PRIOR_THREAD_ID, body="earlier cycle feedback", is_outdated=True)
    state = MonitorState()
    state.threads_addressed_ids.update(_state_map("fix_committed", prior))

    gh, _ = await _run_cycle(
        factory,
        tmp_path,
        gh_statuses=[_status(active=[_thread()], outdated=[prior])],
        initial_threads=[_thread()],
        state=state,
    )

    assert gh.resolved == [_THREAD_ID]


@pytest.mark.unit
async def test_swept_defer_without_capture_marker_is_escalated_not_resolved(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6fmywf: never resolve a defer whose follow-up was not captured.

    Incomplete or legacy ``defer`` state (no ``__deferred_issue_filed__`` marker
    for the current conversation) has no durable tracking issue. Resolving it
    from the whole-feed sweep would let the PR merge with the deferred work lost,
    so it stays on the ``NotifyHuman`` gate like every other unownable thread.
    """
    prior = _thread(thread_id=_PRIOR_THREAD_ID, body="earlier cycle defer")
    state = MonitorState()
    state.threads_addressed_ids.update(_state_map("defer", prior))

    gh, state = await _run_cycle(
        factory,
        tmp_path,
        gh_statuses=[_status(active=[_thread(), prior])],
        initial_threads=[_thread()],
        state=state,
    )

    assert gh.resolved == [_THREAD_ID]
    assert state.threads_addressed_ids[_PRIOR_THREAD_ID] == "needs_human"
    reason = state.threads_addressed_ids["__needs_human_reason__:" + _PRIOR_THREAD_ID]
    assert RESOLUTION_OWNER_MISSING_REASON in reason


@pytest.mark.unit
async def test_swept_defer_with_capture_marker_is_resolved(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The captured counterpart still resolves: a tracking issue already holds the work."""
    prior = _thread(thread_id=_PRIOR_THREAD_ID, body="earlier cycle defer")
    state = MonitorState()
    state.threads_addressed_ids.update(_state_map("defer", prior))
    state.threads_addressed_ids[
        _deferred_issue_filed_marker(prior.thread_id, review_thread_body_hash(prior))
    ] = "dimileeh/aira-web#7"

    gh, state = await _run_cycle(
        factory,
        tmp_path,
        gh_statuses=[_status(active=[_thread(), prior])],
        initial_threads=[_thread()],
        state=state,
    )

    assert gh.resolved == [_THREAD_ID, _PRIOR_THREAD_ID]
    assert state.threads_addressed_ids[_PRIOR_THREAD_ID] == "defer"


def _state_map(verdict: str, thread: ReviewThread) -> dict[str, str]:
    return {
        thread.thread_id: verdict,
        review_thread_body_state_key(thread.thread_id): review_thread_body_hash(thread),
    }


@pytest.mark.unit
def test_stranded_resolvable_thread_ids_owner_matrix() -> None:
    """Every owner branch of the pure helper, including the unreachable-by-cycle ones."""
    thread = _thread()
    state_map = _state_map("fix_committed", thread)
    common = {
        "candidate_ids": [_THREAD_ID],
        "state_map": state_map,
        "stale_thread_ids": frozenset[str](),
        "outdated_only_thread_ids": frozenset[str](),
    }

    # Active at settle with a current disposition: nobody else owns it.
    assert stranded_resolvable_thread_ids(settle_threads=[thread], **common) == (
        (_THREAD_ID,),
        (),
    )
    # AddressComments owns a thread that needs attention.
    assert stranded_resolvable_thread_ids(
        settle_threads=[thread],
        **{**common, "stale_thread_ids": frozenset({_THREAD_ID})},
    ) == ((), ())
    # Outdated hygiene owns an outdated-only thread.
    assert stranded_resolvable_thread_ids(
        settle_threads=[thread],
        **{**common, "outdated_only_thread_ids": frozenset({_THREAD_ID})},
    ) == ((), ())
    # Gone from both unresolved feeds: the forge already resolved it.
    assert stranded_resolvable_thread_ids(settle_threads=[], **common) == ((), ())
    # Present but with a changed body: not this cycle's business.
    assert stranded_resolvable_thread_ids(settle_threads=[_thread(body="new reply")], **common) == (
        (),
        (),
    )
    # No settle status at all: escalate a resolvable verdict, ignore the rest.
    assert stranded_resolvable_thread_ids(settle_threads=None, **common) == (
        (),
        (_THREAD_ID,),
    )
    assert stranded_resolvable_thread_ids(
        settle_threads=None,
        **{**common, "state_map": _state_map("needs_human", thread)},
    ) == ((), ())
    # Duplicate candidate ids collapse to one decision.
    assert stranded_resolvable_thread_ids(
        settle_threads=[thread],
        **{**common, "candidate_ids": [_THREAD_ID, _THREAD_ID]},
    ) == ((_THREAD_ID,), ())
    # A resolution-pending settle thread that is not a candidate id is swept too
    # (stranded by an earlier cycle — PRRT_kwDOSJAM6s6fmmKc).
    assert stranded_resolvable_thread_ids(
        settle_threads=[thread],
        **{**common, "candidate_ids": []},
    ) == ((_THREAD_ID,), ())
    # ...unless this cycle's own resolve queue already owns it.
    assert stranded_resolvable_thread_ids(
        settle_threads=[thread],
        queued_resolution_ids=frozenset({_THREAD_ID}),
        **{**common, "candidate_ids": []},
    ) == ((), ())
    # Without settle evidence there is no feed to sweep: only candidates escalate.
    assert stranded_resolvable_thread_ids(
        settle_threads=None, **{**common, "candidate_ids": []}
    ) == ((), ())
    # A superseded feed cannot license a resolve, but the ids it reported open are
    # still swept, so an earlier cycle's orphan escalates instead of being dropped
    # with the discarded feed (PRRT_kwDOSJAM6s6fm7wj).
    assert stranded_resolvable_thread_ids(
        settle_threads=None,
        prior_feed_thread_ids=frozenset({_THREAD_ID}),
        **{**common, "candidate_ids": []},
    ) == ((), (_THREAD_ID,))
    # Stale outdated-only ids from a superseded poll are NOT proof hygiene still
    # owns the thread — outdatedness cannot be confirmed without a fresh settle
    # (PRRT_kwDOSJAM6s6fqagY). Escalate instead of trusting the discarded feed.
    assert stranded_resolvable_thread_ids(
        settle_threads=None,
        prior_feed_thread_ids=frozenset({_THREAD_ID}),
        **{
            **common,
            "candidate_ids": [],
            "outdated_only_thread_ids": frozenset({_THREAD_ID}),
        },
    ) == ((), (_THREAD_ID,))
    # AddressComments ownership (stale_thread_ids) still wins over that escalation.
    assert stranded_resolvable_thread_ids(
        settle_threads=None,
        prior_feed_thread_ids=frozenset({_THREAD_ID}),
        **{
            **common,
            "candidate_ids": [],
            "stale_thread_ids": frozenset({_THREAD_ID}),
            "outdated_only_thread_ids": frozenset({_THREAD_ID}),
        },
    ) == ((), ())
    # A ``defer`` with no durable capture marker escalates instead of resolving
    # (PRRT_kwDOSJAM6s6fmywf) — resolving it would lose the deferred follow-up.
    defer_map = _state_map("defer", thread)
    assert stranded_resolvable_thread_ids(
        settle_threads=[thread], **{**common, "state_map": defer_map}
    ) == ((), (_THREAD_ID,))
    # ...and resolves once the tracking issue is recorded for this conversation.
    captured_map = {
        **defer_map,
        _deferred_issue_filed_marker(thread.thread_id, review_thread_body_hash(thread)): (
            "dimileeh/aira-web#7"
        ),
    }
    assert stranded_resolvable_thread_ids(
        settle_threads=[thread], **{**common, "state_map": captured_map}
    ) == ((_THREAD_ID,), ())
