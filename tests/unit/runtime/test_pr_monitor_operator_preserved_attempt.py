"""Operator disposition that retires a preserved correction attempt (issue #1189).

A correction attempt whose FIXED claim carries no item-scoped evidence keeps its
commit and parks the review thread ``needs_human`` with a reason naming the
preserved tip. An operator guide (or an adoption ``--hint``) saying "reject the
preserved attempt; the thread is obsolete: reply and resolve" used to run an
agent, consume the hint and land back on ``NotifyHuman`` with the identical
reason on the same head. These tests pin the explicit disposition: the
preserved-attempt record is retired with an audit event, the thread is replied
to and resolved by outdated hygiene, and the park lifts. Anything short of an
unambiguous disposition fails closed with a distinct reason code.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.forge_errors import ForgeClientError
from awf.common.github_client import GitHubClientError, RepoRef
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.feedback_policy import review_thread_body_state_key
from awf.runtime.operator_hints import (
    operator_hint_from_threads,
    operator_hint_processed_key,
    persist_operator_hint,
)
from awf.runtime.pr_monitor import (
    _PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY,
    AddressOperatorHint,
    CheckState,
    MergeableState,
    MergeStateStatus,
    MonitorAction,
    MonitorConfig,
    MonitorState,
    NotifyHuman,
    OperatorHint,
    PRStatus,
    ReviewThread,
    ReviewThreadComment,
    _mark_review_thread_addressed,
    decide,
)
from awf.runtime.pr_monitor_runner.comment_verdict_correction import (
    correction_self_citation_outcome,
    correction_unscoped_fix_outcome,
)
from awf.runtime.pr_monitor_runner.comment_verdict_protocol_types import VerdictResult
from awf.runtime.pr_monitor_runner.helpers import (
    _clear_addressed_state_by_id,
    _notify_human_reason,
    _sync_needs_human_reason,
)
from awf.runtime.pr_monitor_runner.operator_hint_loop import handle_operator_hint_action
from awf.runtime.pr_monitor_runner.operator_hint_retirement import (
    _finalize_processed_operator_hint,
)
from awf.runtime.pr_monitor_runner.operator_preserved_attempt import (
    OPERATOR_PRESERVED_ATTEMPT_DISPOSITION_UNAPPLIED,
    OPERATOR_PRESERVED_ATTEMPT_RETIRED,
    PreservedAttemptDisposition,
    preserved_attempt_disposition,
    preserved_attempt_tip,
)
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult
from awf.runtime.pr_monitor_runner.thread_disposition import (
    ThreadDisposition,
    read_item_disposition,
    record_item_disposition,
)
from awf.service.pr_monitor_adoption_seed import (
    PR_ADOPTION_OPERATOR_HINT_REASON,
    seedable_monitor_state,
)
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)
from tests.unit.runtime._thread_disposition_fixtures import OrderedForge

_REPO = RepoRef(owner="dimileeh", name="aira-web")
_HEAD = "abc1234567890def"
_TIP = "d5889e51" + "a" * 32
# The park reason cites the 12-char tip; that is what the retirement records.
_SHORT_TIP = _TIP[:12]
_THREAD = "PRRT_kwDOSJAM6s61Ix0A"
_OTHER_THREAD = "PRRT_kwDOSJAM6s61Ix0B"
_RETIRED_KEY = f"__awf_preserved_attempt_retired__:{_THREAD}"
_REASON_KEY = f"__needs_human_reason__:{_THREAD}"
_GUIDE = (
    f"The premise is gone after the scope-reset commit. Reject the preserved attempt "
    f"d5889e51; the thread {_THREAD} is obsolete: reply and resolve."
)
_SHA_ONLY_GUIDE = (
    "Premise gone after the scope reset: reply and resolve the thread, and reject the "
    "preserved attempt `d5889e51`."
)
_DISPOSITION_EVENT = "workspace.audit.operator_disposition"


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _thread(
    tid: str = _THREAD, *, body: str = "please fix this finding", outdated: bool = True
) -> ReviewThread:
    return ReviewThread(
        thread_id=tid,
        path="src/anchor.py",
        line=7,
        body_excerpt=body,
        author="greptile",
        is_resolved=False,
        is_outdated=outdated,
        comments=(ReviewThreadComment(comment_id=f"c-{tid}", body=body, author="greptile"),),
    )


def _status(
    *outdated: ReviewThread, active: tuple[ReviewThread, ...] = (), head: str = _HEAD
) -> PRStatus:
    return PRStatus(
        number=42,
        head_sha=head,
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=active,
        unresolved_review_comments=(),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
        outdated_unresolved_inline_threads=outdated,
    )


def _park(state: MonitorState, thread: ReviewThread, result: VerdictResult) -> None:
    """Record ``result`` exactly the way the comment-repair batch records it."""
    _mark_review_thread_addressed(state, thread, result.verdict)
    _sync_needs_human_reason(state, thread.thread_id, result)


def _unscoped_park(tip: str = _TIP) -> VerdictResult:
    return correction_unscoped_fix_outcome(
        workspace_id="ws_x",
        reason="moved the guard into the shared helper",
        attempt_tip=tip,
        item_path="src/anchor.py",
    )


def _parked_state(thread: ReviewThread | None = None, *, tip: str = _TIP) -> MonitorState:
    state = MonitorState()
    _park(state, thread or _thread(), _unscoped_park(tip))
    return state


def _arm(
    state: MonitorState,
    directive: str,
    *,
    operation_id: str = "op_guide_1189",
    reason_code: str = "OPERATOR_GUIDE",
) -> OperatorHint:
    hint = OperatorHint(
        reason=directive,
        directive=directive,
        operation_id=operation_id,
        reason_code=reason_code,
    )
    state.pending_operator_hint = hint
    return hint


class _HintCycle:
    """Stand-in for the agent cycle: today's processed no-op (agent FIXED, no commit)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> _GitPushResult:
        self.calls.append(kwargs)
        _finalize_processed_operator_hint(kwargs["state"], hint=kwargs["hint"])
        return _GitPushResult(pushed=False, failed=False, returncode=0)


async def _not_terminal(*_args: Any, **_kwargs: Any) -> None:
    return None


async def _runner(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> tuple[Any, str, OrderedForge, _HintCycle]:
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    gh = OrderedForge(cmd)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    cycle = _HintCycle()
    runner._run_operator_hint_cycle = cycle
    return runner, workspace_id, gh, cycle


async def _guide_poll(
    runner: Any, workspace_id: str, state: MonitorState, status: PRStatus
) -> bool:
    hint = state.pending_operator_hint
    assert hint is not None
    return await handle_operator_hint_action(
        runner,
        action=AddressOperatorHint(hint=hint),
        workspace_id=workspace_id,
        repo=_REPO,
        pr_number=42,
        status=status,
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project=f"awf_{workspace_id}",
        compose_file=Path("/tmp/compose.yml"),
        monitor_log=None,
        remote_push_url=None,
        finish_if_pr_terminal=_not_terminal,
        finish_if_pr_terminal_after_cleanup_error=_not_terminal,
    )


async def _next_poll(
    runner: Any, gh: OrderedForge, workspace_id: str, state: MonitorState, status: PRStatus
) -> tuple[MonitorAction, str | None]:
    """The next poll's pre-decision hygiene, then ``decide()`` on the same head.

    Also returns the reason the runner would attach to a ``NotifyHuman``.
    """
    gh.remember_review_threads(
        status.unresolved_inline_threads + status.outdated_unresolved_inline_threads
    )
    checked = await runner._resolve_addressed_outdated_threads(
        workspace_id=workspace_id,
        repo=_REPO,
        pr_number=42,
        status=status,
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
    )
    action = decide(checked, state, MonitorConfig(auto_merge=True))
    return action, _notify_human_reason(checked, state)


async def _disposition_events(
    factory: async_sessionmaker[AsyncSession], workspace_id: str
) -> list[Any]:
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        return [event for event in ws.events if event.event_type == _DISPOSITION_EVENT]


def _original_park_reason(state: MonitorState) -> str:
    return state.threads_addressed_ids[_REASON_KEY]


def _assert_retired_and_resolved(
    state: MonitorState,
    gh: OrderedForge,
    poll: tuple[MonitorAction, str | None],
    original_reason: str,
) -> None:
    assert state.threads_addressed_ids[_THREAD] == "false_positive"
    assert _REASON_KEY not in state.threads_addressed_ids
    assert state.threads_addressed_ids[_RETIRED_KEY] == _SHORT_TIP
    assert state.pending_operator_hint is None
    replies = gh.replies()
    assert [thread_id for thread_id, _ in replies] == [_THREAD]
    assert "d5889e51" in replies[0][1]
    assert "operator" in replies[0][1].lower()
    assert ("resolve", _THREAD) in gh.events
    assert gh.events.index(("resolve", _THREAD)) > next(
        i for i, event in enumerate(gh.events) if event[0] == "reply"
    )
    action, notify_reason = poll
    assert not isinstance(action, NotifyHuman)
    assert notify_reason != original_reason


# --- T1: the issue repro ----------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("guide", [_GUIDE, _SHA_ONLY_GUIDE], ids=["thread_and_sha", "sha_only"])
async def test_guide_disposition_retires_preserved_attempt_and_resolves_thread(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path, guide: str
) -> None:
    runner, workspace_id, gh, cycle = await _runner(factory, tmp_path)
    thread = _thread()
    state = _parked_state(thread)
    original_reason = _original_park_reason(state)
    status = _status(thread)
    _arm(state, guide)

    terminal = await _guide_poll(runner, workspace_id, state, status)
    poll = await _next_poll(runner, gh, workspace_id, state, status)

    assert terminal is False
    assert cycle.calls == []  # the agent never reinterprets "reject" as a revert
    _assert_retired_and_resolved(state, gh, poll, original_reason)
    assert state.threads_addressed_ids[operator_hint_processed_key("op_guide_1189")] == (
        "processed"
    )
    events = await _disposition_events(factory, workspace_id)
    assert [event.reason_code for event in events] == [OPERATOR_PRESERVED_ATTEMPT_RETIRED]
    evidence = events[0].payload["evidence"]
    assert evidence["thread_id"] == _THREAD
    assert evidence["attempt_tip"] == _SHORT_TIP
    assert evidence["hint_operation_id"] == "op_guide_1189"


@pytest.mark.unit
async def test_retired_disposition_is_persisted_before_the_next_poll(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    runner, workspace_id, _gh, _cycle = await _runner(factory, tmp_path)
    state = _parked_state()
    _arm(state, _GUIDE)

    await _guide_poll(runner, workspace_id, state, _status(_thread()))

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        persisted = dict(ws.monitor_threads_addressed or {})
    assert persisted[_THREAD] == "false_positive"
    assert persisted[_RETIRED_KEY] == _SHORT_TIP
    assert operator_hint_from_threads(persisted) is None


@pytest.mark.unit
async def test_disposition_retires_a_previously_posted_reply_before_recording_its_own(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """A reply AWF posted for an earlier verdict must not certify the operator's ruling."""
    runner, workspace_id, _gh, _cycle = await _runner(factory, tmp_path)
    state = _parked_state()
    record_item_disposition(
        state,
        ThreadDisposition(
            item_id=_THREAD,
            disposition_class="false_positive",
            verdict="false_positive",
            reason="earlier",
            reply_comment_id="reply-1",
            resolved=True,
        ),
    )
    _arm(state, _GUIDE)

    await _guide_poll(runner, workspace_id, state, _status(_thread()))

    disposition = read_item_disposition(state, _THREAD)
    assert disposition is not None
    assert disposition.source == "operator_disposition"
    assert disposition.reply_comment_id is None
    assert f"__awf_disposition_retired__:{_THREAD}:" in state.threads_addressed_ids


@pytest.mark.unit
async def test_disposition_leaves_other_parked_feedback_named_in_the_guide_untouched(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Only the retired thread is finalized; the agent never ran on the rest."""
    runner, workspace_id, _gh, cycle = await _runner(factory, tmp_path)
    state = _parked_state()
    comment_id = "issue:123456"
    comment_reason = "needs a product decision"
    state.mark_addressed(comment_id, "needs_human")
    state.mark_addressed(f"__review_comment_body_hash__:{comment_id}", "hash-1")
    state.mark_addressed(f"__needs_human_reason__:{comment_id}", comment_reason)
    _arm(
        state,
        f"Reject the preserved attempt d5889e51 on {_THREAD} and resolve it. "
        f"Investigate {comment_id} separately.",
    )

    await _guide_poll(runner, workspace_id, state, _status(_thread()))

    assert cycle.calls == []
    assert state.threads_addressed_ids[_THREAD] == "false_positive"
    assert state.threads_addressed_ids[comment_id] == "needs_human"
    assert state.threads_addressed_ids[f"__needs_human_reason__:{comment_id}"] == comment_reason
    assert state.pending_operator_hint is None


# --- T2: no disposition, behavior unchanged --------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "guide",
    [
        "The premise is gone; please look at this again.",
        "Reply and resolve the thread; the preserved attempt d5889e51 is fine to keep.",
        "Reject the preserved attempt d5889e51 and redo the fix properly.",
    ],
    ids=["plain", "resolve_without_reject", "reject_without_resolve"],
)
async def test_guide_without_disposition_keeps_the_park(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path, guide: str
) -> None:
    runner, workspace_id, gh, cycle = await _runner(factory, tmp_path)
    thread = _thread()
    state = _parked_state(thread)
    original_reason = _original_park_reason(state)
    status = _status(thread)
    _arm(state, guide)

    await _guide_poll(runner, workspace_id, state, status)
    action, notify_reason = await _next_poll(runner, gh, workspace_id, state, status)

    assert len(cycle.calls) == 1
    assert state.threads_addressed_ids[_THREAD] == "needs_human"
    assert state.threads_addressed_ids[_REASON_KEY] == original_reason
    assert _RETIRED_KEY not in state.threads_addressed_ids
    assert gh.events == []
    assert isinstance(action, NotifyHuman)
    assert notify_reason == original_reason
    assert await _disposition_events(factory, workspace_id) == []


@pytest.mark.unit
async def test_guide_naming_thread_without_disposition_keeps_issue_938_requeue(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    runner, workspace_id, gh, cycle = await _runner(factory, tmp_path)
    state = _parked_state()
    _arm(state, f"Look at {_THREAD} again with fresh eyes.")

    await _guide_poll(runner, workspace_id, state, _status(_thread()))

    assert len(cycle.calls) == 1
    assert _THREAD not in state.threads_addressed_ids  # #938 clears it for re-triage
    assert _RETIRED_KEY not in state.threads_addressed_ids
    assert gh.events == []


# --- T3: cancel + re-adopt with the disposition as --hint ------------------------


def _readopt(predecessor: MonitorState, hint_text: str, *, head_continuity: bool) -> MonitorState:
    seeded = seedable_monitor_state(
        predecessor.threads_addressed_ids, head_continuity=head_continuity
    )
    persist_operator_hint(
        seeded,
        OperatorHint(
            reason=hint_text,
            directive=hint_text,
            operation_id="op_adopt_1189",
            reason_code=PR_ADOPTION_OPERATOR_HINT_REASON,
        ),
    )
    successor = MonitorState(threads_addressed_ids=seeded)
    successor.pending_operator_hint = operator_hint_from_threads(seeded)
    return successor


@pytest.mark.unit
@pytest.mark.parametrize("head_continuity", [True, False])
@pytest.mark.parametrize("hint", [_GUIDE, _SHA_ONLY_GUIDE], ids=["thread_and_sha", "sha_only"])
async def test_readopt_hint_disposition_matches_the_guide(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    head_continuity: bool,
    hint: str,
) -> None:
    runner, workspace_id, gh, cycle = await _runner(factory, tmp_path)
    thread = _thread()
    predecessor = _parked_state(thread)
    original_reason = _original_park_reason(predecessor)
    state = _readopt(predecessor, hint, head_continuity=head_continuity)
    status = _status(thread)

    await _guide_poll(runner, workspace_id, state, status)
    poll = await _next_poll(runner, gh, workspace_id, state, status)

    assert cycle.calls == []
    _assert_retired_and_resolved(state, gh, poll, original_reason)
    events = await _disposition_events(factory, workspace_id)
    assert [event.reason_code for event in events] == [OPERATOR_PRESERVED_ATTEMPT_RETIRED]
    assert events[0].payload["evidence"]["hint_operation_id"] == "op_adopt_1189"


@pytest.mark.unit
@pytest.mark.parametrize("hint", [_GUIDE, _SHA_ONLY_GUIDE], ids=["thread_and_sha", "sha_only"])
async def test_readopt_after_retirement_is_an_idempotent_processed_no_op(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path, hint: str
) -> None:
    runner, workspace_id, _gh, cycle = await _runner(factory, tmp_path)
    predecessor = _parked_state()
    _arm(predecessor, _GUIDE)
    await _guide_poll(runner, workspace_id, predecessor, _status(_thread()))
    first_events = await _disposition_events(factory, workspace_id)

    state = _readopt(predecessor, hint, head_continuity=True)
    assert state.threads_addressed_ids[_RETIRED_KEY] == _SHORT_TIP
    await _guide_poll(runner, workspace_id, state, _status())

    assert cycle.calls == []
    assert state.pending_operator_hint is None
    assert state.threads_addressed_ids[operator_hint_processed_key("op_adopt_1189")] == (
        "processed"
    )
    assert [e.id for e in await _disposition_events(factory, workspace_id)] == [
        e.id for e in first_events
    ]


@pytest.mark.unit
@pytest.mark.parametrize("guide", [_GUIDE, _SHA_ONLY_GUIDE], ids=["thread_and_sha", "sha_only"])
async def test_repeated_guide_after_failed_reply_retries_the_disposition(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path, guide: str
) -> None:
    """A retirement whose reply never landed is not a no-op for the next guide."""
    runner, workspace_id, gh, cycle = await _runner(factory, tmp_path)
    thread = _thread()
    state = _parked_state(thread)
    original_reason = _original_park_reason(state)
    status = _status(thread)
    _arm(state, guide)
    await _guide_poll(runner, workspace_id, state, status)
    gh.reply_error = ForgeClientError("forge blip")
    await _next_poll(runner, gh, workspace_id, state, status)
    assert state.threads_addressed_ids[_THREAD] == "needs_human"
    assert state.threads_addressed_ids[_REASON_KEY].startswith("THREAD_DISPOSITION_REPLY_FAILED")

    gh.reply_error = None
    gh.events.clear()
    _arm(state, guide, operation_id="op_guide_1189_retry")
    await _guide_poll(runner, workspace_id, state, status)
    poll = await _next_poll(runner, gh, workspace_id, state, status)

    assert cycle.calls == []
    _assert_retired_and_resolved(state, gh, poll, original_reason)
    events = await _disposition_events(factory, workspace_id)
    assert [event.reason_code for event in events] == [OPERATOR_PRESERVED_ATTEMPT_RETIRED] * 2
    assert events[1].payload["evidence"]["hint_operation_id"] == "op_guide_1189_retry"


@pytest.mark.unit
async def test_repeated_guide_after_failed_resolve_reuses_the_posted_reply(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """A retry after a failed resolve resolves on the posted reply, never double-posts."""
    runner, workspace_id, gh, cycle = await _runner(factory, tmp_path)
    thread = _thread()
    state = _parked_state(thread)
    status = _status(thread)
    _arm(state, _GUIDE)
    await _guide_poll(runner, workspace_id, state, status)
    gh.resolve_error = GitHubClientError(
        operation="resolve_thread", returncode=1, stderr="HTTP 403: resource not accessible"
    )
    await _next_poll(runner, gh, workspace_id, state, status)
    assert state.threads_addressed_ids[_THREAD] == "needs_human"
    posted = read_item_disposition(state, _THREAD)
    assert posted is not None
    assert posted.reply_comment_id is not None
    assert len(gh.replies()) == 1

    gh.resolve_error = None
    gh.events.clear()
    _arm(state, _GUIDE, operation_id="op_guide_1189_retry")
    await _guide_poll(runner, workspace_id, state, status)
    await _next_poll(runner, gh, workspace_id, state, status)

    assert cycle.calls == []
    assert gh.replies() == []
    assert ("resolve", _THREAD) in gh.events
    assert state.threads_addressed_ids[_THREAD] == "false_positive"
    retried = read_item_disposition(state, _THREAD)
    assert retried is not None
    assert retried.reply_comment_id == posted.reply_comment_id
    assert not any(
        key.startswith(f"__awf_disposition_retired__:{_THREAD}:")
        for key in state.threads_addressed_ids
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("outdated", "fresh_reply", "detail"),
    [
        (True, True, "not_preserved_attempt"),
        (False, True, "not_preserved_attempt"),
        (True, False, "no_body_snapshot"),
        (False, False, "thread_not_outdated"),
    ],
    ids=["outdated-fresh-reply", "active-fresh-reply", "outdated-readopt", "active-readopt"],
)
async def test_repeated_guide_after_reopen_with_fresh_reply_fails_closed(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    outdated: bool,
    fresh_reply: bool,
    detail: str,
) -> None:
    """A tombstone is not proof of retirement once the thread is open with a new verdict."""
    runner, workspace_id, gh, cycle = await _runner(factory, tmp_path)
    thread = _thread()
    state = _parked_state(thread)
    status = _status(thread)
    _arm(state, _GUIDE)
    await _guide_poll(runner, workspace_id, state, status)
    await _next_poll(runner, gh, workspace_id, state, status)
    assert ("resolve", _THREAD) in gh.events
    if fresh_reply:
        # A reviewer reopens the thread with a fresh reply: that feedback supersedes
        # the operator-sourced false_positive verdict and ends the retirement.
        _clear_addressed_state_by_id(
            state, _THREAD, restore_operator_decision=False, keep_posted_reply=False
        )
        assert _RETIRED_KEY not in state.threads_addressed_ids
    else:
        # A moved-head re-adopt carries the tombstone but drops the verdict it explains.
        state.threads_addressed_ids.pop(_THREAD)
        state.threads_addressed_ids.pop(review_thread_body_state_key(_THREAD))
        assert state.threads_addressed_ids[_RETIRED_KEY] == _SHORT_TIP
    reopened = _thread(body="reopening: this is still broken", outdated=outdated)
    reopened_status = _status(reopened) if outdated else _status(active=(reopened,))
    gh.events.clear()

    _arm(state, _GUIDE, operation_id="op_guide_1189_reopened")
    await _guide_poll(runner, workspace_id, state, reopened_status)

    assert cycle.calls == []
    assert operator_hint_processed_key("op_guide_1189_reopened") not in (
        state.threads_addressed_ids
    )
    hint = state.pending_operator_hint
    assert hint is not None
    assert hint.status == "needs_human"
    assert (hint.status_reason or "").startswith(OPERATOR_PRESERVED_ATTEMPT_DISPOSITION_UNAPPLIED)
    assert detail in (hint.status_reason or "")
    assert gh.events == []


@pytest.mark.unit
async def test_repeated_guide_after_outdated_retriage_of_fresh_reply_fails_closed(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Re-triaging fresh feedback ends the retirement even without the poll-boundary clear.

    An outdated thread never reaches ``_drop_stale_review_thread_addressed_state``:
    its fresh reply re-enters repair and is re-triaged straight into an ordinary
    ``needs_human`` verdict for the new conversation. A repeated ruling must not
    read the old tombstone and close that new escalation on the old attempt SHA.
    """
    runner, workspace_id, gh, cycle = await _runner(factory, tmp_path)
    thread = _thread()
    state = _parked_state(thread)
    status = _status(thread)
    _arm(state, _GUIDE)
    await _guide_poll(runner, workspace_id, state, status)
    await _next_poll(runner, gh, workspace_id, state, status)
    assert ("resolve", _THREAD) in gh.events
    reopened = _thread(body="reopening: this is still broken")
    _mark_review_thread_addressed(state, reopened, "needs_human")
    state.mark_addressed(_REASON_KEY, "needs a product decision on the new ask")
    assert _RETIRED_KEY not in state.threads_addressed_ids
    gh.events.clear()

    _arm(state, _GUIDE, operation_id="op_guide_1189_retriaged")
    await _guide_poll(runner, workspace_id, state, _status(reopened))

    assert cycle.calls == []
    assert state.threads_addressed_ids[_THREAD] == "needs_human"
    hint = state.pending_operator_hint
    assert hint is not None
    assert hint.status == "needs_human"
    assert "not_preserved_attempt" in (hint.status_reason or "")
    assert gh.events == []


# --- T4: the disposition cannot be applied -> distinct reason, no silent re-raise --


def _two_parks_sharing_prefix() -> tuple[MonitorState, tuple[ReviewThread, ...]]:
    first, second = _thread(), _thread(_OTHER_THREAD)
    state = MonitorState()
    _park(state, first, _unscoped_park(_TIP))
    _park(state, second, _unscoped_park("d5889e51" + "b" * 32))
    return state, (first, second)


def _plain_needs_human() -> tuple[MonitorState, tuple[ReviewThread, ...]]:
    thread = _thread()
    state = MonitorState()
    _park(state, thread, VerdictResult(verdict="needs_human", reason="ambiguous ask"))
    return state, (thread,)


def _active_thread_park() -> tuple[MonitorState, tuple[ReviewThread, ...]]:
    thread = _thread(outdated=False)
    return _parked_state(thread), ()


def _conversation_changed() -> tuple[MonitorState, tuple[ReviewThread, ...]]:
    state = _parked_state(_thread())
    return state, (_thread(body="actually this is still broken"),)


def _no_body_snapshot() -> tuple[MonitorState, tuple[ReviewThread, ...]]:
    state = _parked_state(_thread())
    state.threads_addressed_ids.pop(review_thread_body_state_key(_THREAD))
    return state, (_thread(),)


def _thread_not_open() -> tuple[MonitorState, tuple[ReviewThread, ...]]:
    return _parked_state(_thread()), ()


_UNAPPLIED_CASES: list[tuple[str, Any, str]] = [
    (
        "ambiguous_item",
        _two_parks_sharing_prefix,
        "Reject the preserved attempt d5889e51 and resolve the thread as obsolete.",
    ),
    (
        "multiple_threads_named",
        _two_parks_sharing_prefix,
        f"Reject the preserved attempt on {_THREAD} and resolve it. Keep {_OTHER_THREAD} "
        "parked for investigation.",
    ),
    (
        "multiple_shas_named",
        lambda: (_parked_state(_thread()), (_thread(),)),
        f"Reject the preserved attempt 0badc0de on {_THREAD} and resolve it. Keep d5889e51 "
        "for investigation.",
    ),
    (
        "multiple_shas_named",
        lambda: (_parked_state(_thread()), (_thread(),)),
        "Reject the preserved attempt 0badc0de and resolve it. Keep d5889e51 for investigation.",
    ),
    (
        "no_matching_item",
        lambda: (_parked_state(_thread()), (_thread(),)),
        "Reject the preserved attempt 0badc0de; resolve the thread.",
    ),
    (
        "no_item_named",
        lambda: (_parked_state(_thread()), (_thread(),)),
        "Reject the preserved attempt; the thread is obsolete, resolve it.",
    ),
    (
        "sha_mismatch",
        lambda: (_parked_state(_thread()), (_thread(),)),
        f"Reject the preserved attempt 0badc0de on {_THREAD}; it is obsolete.",
    ),
    (
        "not_preserved_attempt",
        _plain_needs_human,
        f"Reject the preserved attempt on {_THREAD}; it is obsolete: resolve.",
    ),
    ("thread_not_outdated", _active_thread_park, _GUIDE),
    ("conversation_changed", _conversation_changed, _GUIDE),
    ("no_body_snapshot", _no_body_snapshot, _GUIDE),
    ("thread_not_open", _thread_not_open, _GUIDE),
]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("detail", "build", "guide"), _UNAPPLIED_CASES, ids=[case[0] for case in _UNAPPLIED_CASES]
)
async def test_unapplicable_disposition_parks_hint_with_distinct_reason(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    detail: str,
    build: Any,
    guide: str,
) -> None:
    runner, workspace_id, gh, cycle = await _runner(factory, tmp_path)
    state, outdated = build()
    active = (_thread(outdated=False),) if detail == "thread_not_outdated" else ()
    status = _status(*outdated, active=active)
    before = dict(state.threads_addressed_ids)
    _arm(state, guide)

    await _guide_poll(runner, workspace_id, state, status)

    assert cycle.calls == []
    assert state.threads_addressed_ids == before  # the record is untouched
    hint = state.pending_operator_hint
    assert hint is not None
    assert hint.status == "needs_human"
    assert (hint.status_reason or "").startswith(OPERATOR_PRESERVED_ATTEMPT_DISPOSITION_UNAPPLIED)
    assert detail in (hint.status_reason or "")
    action = decide(status, state, MonitorConfig(auto_merge=True))
    assert isinstance(action, NotifyHuman)
    assert OPERATOR_PRESERVED_ATTEMPT_DISPOSITION_UNAPPLIED in (action.message or "")
    events = await _disposition_events(factory, workspace_id)
    assert [event.reason_code for event in events] == [
        OPERATOR_PRESERVED_ATTEMPT_DISPOSITION_UNAPPLIED
    ]
    assert events[0].payload["evidence"]["detail"] == detail
    assert gh.events == []


# --- T5: protected-scope resumes are owned by the grant/directive protocol ---------


@pytest.mark.unit
async def test_protected_block_resume_ignores_the_disposition(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    runner, workspace_id, _gh, cycle = await _runner(factory, tmp_path)
    state = _parked_state()
    state.threads_addressed_ids[_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY] = "e" * 40
    _arm(state, _GUIDE)

    await _guide_poll(runner, workspace_id, state, _status(_thread()))

    assert len(cycle.calls) == 1
    assert _RETIRED_KEY not in state.threads_addressed_ids
    assert await _disposition_events(factory, workspace_id) == []


@pytest.mark.unit
async def test_remonitor_reason_without_directive_is_not_a_disposition(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """A plain remonitor's ``reason`` is audit context, never an instruction."""
    runner, workspace_id, _gh, cycle = await _runner(factory, tmp_path)
    state = _parked_state()
    state.pending_operator_hint = OperatorHint(reason=_GUIDE, operation_id="op_remonitor")

    await _guide_poll(runner, workspace_id, state, _status(_thread()))

    assert len(cycle.calls) == 1
    assert _RETIRED_KEY not in state.threads_addressed_ids


# --- Pure grammar and tip extraction ----------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (_GUIDE, PreservedAttemptDisposition(thread_ids=(_THREAD,), shas=("d5889e51",))),
        (_SHA_ONLY_GUIDE, PreservedAttemptDisposition(thread_ids=(), shas=("d5889e51",))),
        (
            "REJECTED the Preserved Correction Commit D5889E51; OBSOLETE.",
            PreservedAttemptDisposition(thread_ids=(), shas=("d5889e51",)),
        ),
        (
            "Rejecting preserved commit d5889e5 and d5889e51 d5889e51, resolve both.",
            PreservedAttemptDisposition(thread_ids=(), shas=("d5889e5", "d5889e51")),
        ),
        (
            "reject preserved attempt abc12 and resolve",
            PreservedAttemptDisposition(thread_ids=(), shas=()),
        ),
    ],
    ids=["thread_and_sha", "sha_only", "mixed_case", "dedup_two_shas", "short_sha_ignored"],
)
def test_disposition_grammar_positives(text: str, expected: PreservedAttemptDisposition) -> None:
    assert preserved_attempt_disposition(text) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "Reply and resolve; the preserved attempt d5889e51 stays.",
        "Reject the preserved attempt d5889e51 and fix it again.",
        "Reject this. The preserved attempt d5889e51 is obsolete, resolve.",
        "Do not reject the preserved attempt d5889e51; resolve the thread.",
        "Don't reject the preserved attempt d5889e51, resolve it.",
        "Never reject the preserved commit d5889e51; resolve.",
        f"Do not yet reject the preserved attempt d5889e51 on {_THREAD}; the thread is obsolete.",
        "Do not actually reject the preserved attempt d5889e51; resolve.",
        "You shouldn't ever reject the preserved commit d5889e51, resolve it.",
        "Reject the attempt d5889e51 and resolve.",
        f"Reject the preserved attempt d5889e51 on {_THREAD}, but do not resolve the thread; "
        "investigate the actual bug.",
        "Reject the preserved attempt d5889e51; it is not obsolete, resolve nothing yet.",
        "Reject the preserved attempt d5889e51 and don't resolve.",
        "Reject the preserved attempt d5889e51 and never resolve it.",
        "Reject the preserved attempt d5889e51 and resolve the thread if CI passes.",
        "Reject the preserved attempt d5889e51; only resolve once the fix lands.",
        "Reject the preserved attempt d5889e51; resolve. Do not resolve it yet.",
    ],
    ids=[
        "none",
        "empty",
        "no_reject",
        "no_resolve",
        "reject_in_other_clause",
        "do_not",
        "dont",
        "never",
        "do_not_yet",
        "do_not_actually",
        "shouldnt_ever",
        "not_preserved",
        "resolve_negated",
        "obsolete_negated",
        "resolve_dont",
        "resolve_never",
        "resolve_conditional_if",
        "resolve_conditional_once",
        "resolve_contradicted",
    ],
)
def test_disposition_grammar_negatives(text: str | None) -> None:
    assert preserved_attempt_disposition(text) is None


def _self_citation(verdict: str) -> VerdictResult:
    return correction_self_citation_outcome(
        workspace_id="ws_x",
        verdict=verdict,
        reason="already handled",
        attempt_tip=_TIP,
        has_path_evidence=False,
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "result",
    [
        _unscoped_park(),
        _self_citation("needs_human"),
        _self_citation("false_positive"),
        VerdictResult(
            verdict="needs_human",
            reason=(
                "AGENT_VERDICT_PROTOCOL_VIOLATION: x The correction attempt ended without an "
                f"accepted AWF-VERDICT; this item's commit (attempt tip {_TIP[:12]}) is "
                "preserved for human review."
            ),
            kept_commit=True,
        ),
    ],
    ids=["unscoped_fixed", "self_citation_needs_human", "self_citation_non_fix", "unaccepted"],
)
def test_preserved_tip_is_read_from_every_kept_commit_template(result: VerdictResult) -> None:
    assert result.kept_commit is True
    assert preserved_attempt_tip(result.reason) == _SHORT_TIP


@pytest.mark.unit
@pytest.mark.parametrize(
    "reason",
    [
        None,
        "ambiguous ask",
        f"Accepted as FIXED: the correction verdict cited a commit (attempt tip {_TIP[:12]}).",
        "this item's commit (attempt tip <unknown>) is preserved for human review.",
    ],
    ids=["none", "plain", "not_preserved", "unknown_tip"],
)
def test_reason_without_preserved_signature_has_no_tip(reason: str | None) -> None:
    assert preserved_attempt_tip(reason) is None


@pytest.mark.unit
@pytest.mark.parametrize("head_continuity", [True, False])
def test_adoption_seed_carries_every_real_preserved_attempt_park(head_continuity: bool) -> None:
    """The seed's reason signature accepts what the runtime templates actually write."""
    state = _parked_state()

    seeded = seedable_monitor_state(state.threads_addressed_ids, head_continuity=head_continuity)

    assert seeded[_REASON_KEY] == _original_park_reason(state)
    assert preserved_attempt_tip(seeded[_REASON_KEY]) == _SHORT_TIP
