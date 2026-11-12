"""Operator ruling that closes an ``AGENT_NON_FIXED_WITH_MUTATION`` park (issue #1213).

The production repro: three threads were parked on refused correction attempts,
later commits fixed them, and the operator guided "make NO code changes; record
dispositions only: thread A FIXED by commit Y". The agent ran, did nothing, the
hint was consumed, and the monitor returned to ``NotifyHuman`` on every poll.
These tests pin the explicit ruling: it is applied without the agent, the
FIXED claim is checked against the PR head, and the thread is replied to and
resolved with an operator-sourced disposition. Anything short of that fails
closed with a distinct reason code.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import GitHubClientError, RepoRef
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.operator_hints import operator_hint_processed_key
from awf.runtime.pr_monitor import (
    _PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY,
    AddressOperatorHint,
    CheckState,
    MergeableState,
    MergeStateStatus,
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
from awf.runtime.pr_monitor_runner.comment_verdict_protocol_types import (
    AGENT_NON_FIXED_WITH_MUTATION,
    VerdictResult,
)
from awf.runtime.pr_monitor_runner.helpers import _sync_needs_human_reason
from awf.runtime.pr_monitor_runner.operator_hint_loop import handle_operator_hint_action
from awf.runtime.pr_monitor_runner.operator_hint_retirement import (
    _finalize_processed_operator_hint,
)
from awf.runtime.pr_monitor_runner.operator_park_disposition import (
    OPERATOR_PARK_DISPOSITION_APPLIED,
    OPERATOR_PARK_DISPOSITION_RESOLVE_FAILED,
    OPERATOR_PARK_DISPOSITION_UNAPPLIED,
    ParkRuling,
    apply_park_disposition,
    park_dispositions,
)
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult
from awf.runtime.pr_monitor_runner.thread_disposition import (
    ThreadDisposition,
    read_item_disposition,
    record_item_disposition,
)
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)
from tests.unit.runtime._thread_disposition_fixtures import OrderedForge

_REPO = RepoRef(owner="acme", name="widgets")
_ROLLBACK = "0a1b2c3d4e5f" + "0" * 28
_HEAD = "9f8e7d6c5b4a" + "1" * 28
_FIX = "c0ffee12" + "3" * 32
_THREAD = "PRRT_kwDOparkdisposition0A"
_OTHER = "PRRT_kwDOparkdisposition0B"
_DISPOSITION_EVENT = "workspace.audit.operator_disposition"


def _reason_key(thread_id: str = _THREAD) -> str:
    return f"__needs_human_reason__:{thread_id}"


def _park_key(thread_id: str = _THREAD) -> str:
    return f"__awf_mutation_park__:{thread_id}"


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _thread(
    tid: str = _THREAD, *, body: str = "guard the None payload", outdated: bool = False
) -> ReviewThread:
    return ReviewThread(
        thread_id=tid,
        path="src/app.py",
        line=12,
        body_excerpt=body,
        author="review-bot",
        is_resolved=False,
        is_outdated=outdated,
        comments=(ReviewThreadComment(comment_id=f"c-{tid}", body=body, author="review-bot"),),
    )


def _status(*threads: ReviewThread, head: str = _HEAD) -> PRStatus:
    return PRStatus(
        number=42,
        head_sha=head,
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=tuple(t for t in threads if not t.is_outdated),
        unresolved_review_comments=(),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
        outdated_unresolved_inline_threads=tuple(t for t in threads if t.is_outdated),
    )


def _mutation_park(state: MonitorState, thread: ReviewThread) -> None:
    """Record the park exactly the way ``_address_thread`` records it."""
    result = VerdictResult(
        verdict="needs_human",
        reason=(
            f"{AGENT_NON_FIXED_WITH_MUTATION}: Correction attempt mutated the worktree then "
            f"reported a non-FIXED verdict. The correction attempt's edits were rolled back "
            f"to {_ROLLBACK[:12]}; the item is parked for human review."
        ),
        reason_code=AGENT_NON_FIXED_WITH_MUTATION,
        rollback_head=_ROLLBACK,
    )
    _mark_review_thread_addressed(state, thread, result.verdict)
    _sync_needs_human_reason(state, thread.thread_id, result, is_outdated=thread.is_outdated)


def _parked(*threads: ReviewThread) -> MonitorState:
    state = MonitorState()
    for thread in threads or (_thread(),):
        _mutation_park(state, thread)
    return state


def _arm(state: MonitorState, directive: str, *, operation_id: str = "op_guide_1213") -> None:
    state.pending_operator_hint = OperatorHint(
        reason=directive,
        directive=directive,
        operation_id=operation_id,
        reason_code="OPERATOR_GUIDE",
    )


class _HintCycle:
    """Stand-in for the agent cycle: the production no-op (agent ran, changed nothing)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> _GitPushResult:
        self.calls.append(kwargs)
        _finalize_processed_operator_hint(kwargs["state"], hint=kwargs["hint"])
        return _GitPushResult(pushed=False, failed=False, returncode=0)


async def _not_terminal(*_args: Any, **_kwargs: Any) -> None:
    return None


def _git(
    cmd: FakeCommandRunner,
    *,
    rev_parse: tuple[int, str] = (0, f"{_FIX}\n"),
    ancestor: int = 0,
) -> None:
    cmd.respond_when(
        lambda args: "rev-parse" in args and "--verify" in args,
        returncode=rev_parse[0],
        stdout=rev_parse[1],
    )
    cmd.respond_when(lambda args: "merge-base" in args, returncode=ancestor)


def _git_calls(cmd: FakeCommandRunner) -> list[list[str]]:
    return [
        call.args
        for call in cmd.calls
        if call.args[:1] == ["git"] and ("merge-base" in call.args or "rev-parse" in call.args)
    ]


async def _runner(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path, **git: Any
) -> tuple[Any, str, OrderedForge, _HintCycle, FakeCommandRunner]:
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    _git(cmd, **git)
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
    return runner, workspace_id, gh, cycle, cmd


async def _guide_poll(
    runner: Any, workspace_id: str, gh: OrderedForge, state: MonitorState, status: PRStatus
) -> bool:
    gh.remember_review_threads(
        status.unresolved_inline_threads + status.outdated_unresolved_inline_threads
    )
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
        base_branch="main",
        remote_branch=f"awf/{workspace_id}",
        compose_project=f"awf_{workspace_id}",
        compose_file=Path("/tmp/compose.yml"),
        monitor_log=None,
        remote_push_url=None,
        finish_if_pr_terminal=_not_terminal,
        finish_if_pr_terminal_after_cleanup_error=_not_terminal,
    )


async def _disposition_events(
    factory: async_sessionmaker[AsyncSession], workspace_id: str
) -> list[Any]:
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        return [event for event in ws.events if event.event_type == _DISPOSITION_EVENT]


def _assert_untouched(state: MonitorState, thread_id: str = _THREAD) -> None:
    assert state.threads_addressed_ids[thread_id] == "needs_human"
    assert state.threads_addressed_ids[_reason_key(thread_id)].startswith(
        AGENT_NON_FIXED_WITH_MUTATION
    )
    assert _park_key(thread_id) in state.threads_addressed_ids


# --- parser -----------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("guide", "expected"),
    [
        (
            f"Make NO code changes; record dispositions only: thread {_THREAD} FIXED by "
            f"commit {_FIX[:10]}, thread {_OTHER} FIXED by `abc1234`.",
            {_THREAD: ParkRuling("fixed", _FIX[:10]), _OTHER: ParkRuling("fixed", "abc1234")},
        ),
        (f"{_THREAD} is a false positive.", {_THREAD: ParkRuling("false_positive")}),
        (f"{_THREAD}: false-positive", {_THREAD: ParkRuling("false_positive")}),
        # A ruling plus a bare id: the bare thread fails the guide closed.
        (
            f"{_THREAD} fixed in abc1234. Look at {_OTHER}.",
            {_THREAD: ParkRuling("fixed", "abc1234"), _OTHER: None},
        ),
        # Disagreeing rulings on one thread.
        (f"{_THREAD} FIXED by abc1234; {_THREAD} is a false positive", {_THREAD: None}),
        # A negated or conditional ruling voids that thread.
        (
            f"{_THREAD} FIXED by abc1234; {_OTHER} is not a false positive",
            {_THREAD: ParkRuling("fixed", "abc1234"), _OTHER: None},
        ),
        (
            f"{_THREAD} FIXED by abc1234. {_OTHER} FIXED by def5678 once CI passes",
            {_THREAD: ParkRuling("fixed", "abc1234"), _OTHER: None},
        ),
        # A condition in an earlier clause does not void a later, unconditional one.
        (
            f"Only rule what is listed. {_THREAD} is a false positive",
            {_THREAD: ParkRuling("false_positive")},
        ),
        # A negation in an earlier comma or "and" sub-clause rules on something else.
        (
            f"Make no code changes, {_THREAD} is a false positive",
            {_THREAD: ParkRuling("false_positive")},
        ),
        (
            f"Do not change code and {_THREAD} is FIXED by abc1234",
            {_THREAD: ParkRuling("fixed", "abc1234")},
        ),
        # So does an "only" there: it scopes that sub-clause, not the ruling.
        (
            f"Make only doc edits, {_THREAD} is a false positive",
            {_THREAD: ParkRuling("false_positive")},
        ),
    ],
)
def test_park_dispositions_parses_per_thread_rulings(
    guide: str, expected: dict[str, ParkRuling | None]
) -> None:
    assert park_dispositions(guide) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "guide",
    [
        None,
        "",
        "FIXED by abc1234",  # no thread named
        f"{_THREAD}: please take another look.",
        f"{_THREAD} is not fixed by abc1234",
        f"{_THREAD} FIXED by abc1234 once CI passes",
        f"{_THREAD} FIXED by 1234567",  # an all-digit token is not a SHA
        # A condition or negation earlier in the clause, before the thread id.
        f"If CI passes, {_THREAD} is a false positive",
        f"Do not mark {_THREAD} as a false positive",
        # A list of thread ids does not split a negation off the later ids.
        f"Do not mark {_OTHER}, {_THREAD} as a false positive",
        f"Do not mark {_OTHER} and {_THREAD} as a false positive",
        # A negation left dangling before a break still governs what follows it.
        f"Do not, under any circumstances, mark {_THREAD} as a false positive",
        # "only" in the ruling's own sub-clause still scopes the ruling.
        f"Make edits, only {_THREAD} is a false positive",
        f"Once CI is green: ok. Never call {_THREAD} FIXED by abc1234",
        # A question or an investigation request is not a ruling.
        f"{_THREAD}: investigate whether this is a false positive",
        f"{_THREAD}: is this a false positive?",
        f"{_THREAD}: is this a false positive",
        f"Can you check if {_THREAD} was FIXED by abc1234",
        f"{_THREAD} is probably a false positive",
        # A question that names the thread before the ruling phrase.
        f"Is {_THREAD} a false positive",
        f"Was {_THREAD} FIXED by abc1234",
        f"Do you think {_THREAD} is a false positive",
    ],
)
def test_park_dispositions_without_a_usable_ruling_is_none(guide: str | None) -> None:
    assert park_dispositions(guide) is None


# --- applied ----------------------------------------------------------------------


@pytest.mark.unit
async def test_fixed_by_ancestor_sha_resolves_the_parked_thread_and_stops_notify_human(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    runner, workspace_id, gh, cycle, cmd = await _runner(factory, tmp_path)
    thread = _thread()
    state = _parked(thread)
    status = _status(thread)
    _arm(state, f"Make NO code changes; record dispositions only: {_THREAD} FIXED by {_FIX[:8]}.")

    terminal = await _guide_poll(runner, workspace_id, gh, state, status)

    assert terminal is False
    assert cycle.calls == []  # no agent, no worktree mutation
    assert state.threads_addressed_ids[_THREAD] == "fix_committed"
    assert _reason_key() not in state.threads_addressed_ids
    assert _park_key() not in state.threads_addressed_ids
    assert state.pending_operator_hint is None
    assert state.threads_addressed_ids[operator_hint_processed_key("op_guide_1213")] == (
        "processed"
    )
    disposition = read_item_disposition(state, _THREAD)
    assert disposition is not None
    assert disposition.disposition_class == "fixed"
    assert disposition.source == "operator_disposition"
    assert disposition.commits == (_FIX,)
    assert disposition.reply_comment_id is not None
    assert disposition.resolved is True
    replies = gh.replies()
    assert [tid for tid, _ in replies] == [_THREAD]
    assert "class=fixed" in replies[0][1]
    assert _FIX[:12] in replies[0][1]
    assert ("resolve", _THREAD) in gh.events
    assert gh.events.index(("resolve", _THREAD)) > next(
        i for i, event in enumerate(gh.events) if event[0] == "reply"
    )
    merge_base = next(call for call in _git_calls(cmd) if "merge-base" in call)
    assert merge_base[-3:] == ["--is-ancestor", _FIX, _HEAD]
    # The resolved thread leaves the feed; nothing parks the PR any more.
    action = decide(_status(), state, MonitorConfig(auto_merge=True))
    assert not isinstance(action, NotifyHuman)
    events = await _disposition_events(factory, workspace_id)
    assert [event.reason_code for event in events] == [OPERATOR_PARK_DISPOSITION_APPLIED]
    evidence = events[0].payload["evidence"]
    assert evidence["thread_id"] == _THREAD
    assert evidence["disposition"] == "fixed"
    assert evidence["sha"] == _FIX
    assert evidence["head_sha"] == _HEAD
    assert evidence["rollback_head"] == _ROLLBACK
    assert evidence["hint_operation_id"] == "op_guide_1213"


@pytest.mark.unit
async def test_false_positive_ruling_resolves_an_outdated_park_without_git(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    runner, workspace_id, gh, cycle, cmd = await _runner(factory, tmp_path)
    thread = _thread(outdated=True)
    state = _parked(thread)
    _arm(state, f"{_THREAD} is a false positive.")

    await _guide_poll(runner, workspace_id, gh, state, _status(thread))

    assert cycle.calls == []
    assert _git_calls(cmd) == []
    assert state.threads_addressed_ids[_THREAD] == "false_positive"
    disposition = read_item_disposition(state, _THREAD)
    assert disposition is not None
    assert disposition.disposition_class == "false_positive"
    assert disposition.reason == "False positive per operator ruling."
    assert ("resolve", _THREAD) in gh.events


@pytest.mark.unit
async def test_two_threads_in_one_guide_are_both_applied(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    runner, workspace_id, gh, _cycle, _cmd = await _runner(factory, tmp_path)
    first, second = _thread(), _thread(_OTHER, body="other finding")
    state = _parked(first, second)
    _arm(state, f"thread {_THREAD} FIXED by {_FIX[:8]}, thread {_OTHER} FIXED by {_FIX[:9]}")

    await _guide_poll(runner, workspace_id, gh, state, _status(first, second))

    assert state.threads_addressed_ids[_THREAD] == "fix_committed"
    assert state.threads_addressed_ids[_OTHER] == "fix_committed"
    assert {event[1] for event in gh.events if event[0] == "resolve"} == {_THREAD, _OTHER}
    events = await _disposition_events(factory, workspace_id)
    assert [event.reason_code for event in events] == [OPERATOR_PARK_DISPOSITION_APPLIED] * 2


@pytest.mark.unit
async def test_legacy_mutation_park_without_its_record_is_still_ruleable(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    runner, workspace_id, gh, _cycle, _cmd = await _runner(factory, tmp_path)
    thread = _thread()
    state = _parked(thread)
    state.threads_addressed_ids.pop(_park_key())
    _arm(state, f"{_THREAD} FIXED by {_FIX[:8]}")

    await _guide_poll(runner, workspace_id, gh, state, _status(thread))

    assert state.threads_addressed_ids[_THREAD] == "fix_committed"
    events = await _disposition_events(factory, workspace_id)
    assert events[0].payload["evidence"]["rollback_head"] == _ROLLBACK[:12]


# --- refused ----------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("git", "detail"),
    [
        ({"ancestor": 1}, "sha_not_ancestor"),
        ({"ancestor": 128}, "ancestry_unverified"),
        ({"rev_parse": (1, "")}, "sha_unknown"),
        ({"rev_parse": (128, "")}, "ancestry_unverified"),
        ({"rev_parse": (0, "  \n")}, "ancestry_unverified"),
    ],
    ids=["not_ancestor", "inconclusive", "unknown_sha", "rev_parse_fatal", "rev_parse_empty"],
)
async def test_fixed_claim_without_ancestry_evidence_is_refused(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    git: dict[str, Any],
    detail: str,
) -> None:
    runner, workspace_id, gh, cycle, _cmd = await _runner(factory, tmp_path, **git)
    thread = _thread()
    state = _parked(thread)
    _arm(state, f"{_THREAD} FIXED by {_FIX[:8]}")

    terminal = await _guide_poll(runner, workspace_id, gh, state, _status(thread))

    assert terminal is False
    assert cycle.calls == []
    _assert_untouched(state)
    assert gh.events == []
    hint = state.pending_operator_hint
    assert hint is not None
    assert hint.status == "needs_human"
    assert hint.status_reason is not None
    assert hint.status_reason.startswith(f"{OPERATOR_PARK_DISPOSITION_UNAPPLIED}:")
    assert f"({detail})" in hint.status_reason
    events = await _disposition_events(factory, workspace_id)
    assert [event.reason_code for event in events] == [OPERATOR_PARK_DISPOSITION_UNAPPLIED]
    assert events[0].payload["evidence"]["detail"] == detail


@pytest.mark.unit
async def test_ancestry_probe_failure_is_refused(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    runner, workspace_id, gh, _cycle, _cmd = await _runner(factory, tmp_path)

    class _Broken(FakeCommandRunner):
        async def run(self, args: list[str], **kwargs: Any) -> Any:
            if "merge-base" in args:
                raise OSError("git vanished")
            return await super().run(args, **kwargs)

    broken = _Broken()
    _git(broken)
    runner._deps.runner = broken
    thread = _thread()
    state = _parked(thread)
    _arm(state, f"{_THREAD} FIXED by {_FIX[:8]}")

    await _guide_poll(runner, workspace_id, gh, state, _status(thread))

    _assert_untouched(state)
    hint = state.pending_operator_hint
    assert hint is not None and hint.status_reason is not None
    assert "(ancestry_unverified)" in hint.status_reason


@pytest.mark.unit
async def test_unknown_pr_head_is_refused(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    runner, workspace_id, gh, _cycle, cmd = await _runner(factory, tmp_path)
    thread = _thread()
    state = _parked(thread)
    _arm(state, f"{_THREAD} FIXED by {_FIX[:8]}")

    await _guide_poll(runner, workspace_id, gh, state, _status(thread, head=""))

    _assert_untouched(state)
    assert _git_calls(cmd) == []


@pytest.mark.unit
@pytest.mark.parametrize(
    ("setup", "detail"),
    [
        ("bare_second_thread", "thread_without_disposition"),
        ("not_mutation_park", "not_mutation_park"),
        ("thread_closed", "thread_not_open"),
        ("conversation_changed", "conversation_changed"),
        ("no_snapshot", "no_body_snapshot"),
    ],
)
async def test_guide_that_cannot_be_applied_fails_closed_and_changes_nothing(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path, setup: str, detail: str
) -> None:
    runner, workspace_id, gh, cycle, _cmd = await _runner(factory, tmp_path)
    thread = _thread()
    state = _parked(thread)
    live = (thread,)
    guide = f"{_THREAD} FIXED by {_FIX[:8]}"
    if setup == "bare_second_thread":
        guide += f". Also look at {_OTHER}."
    elif setup == "not_mutation_park":
        # A guide that rules on a mutation park takes over every thread it rules on.
        guide += f". {_OTHER} is a false positive."
        _sync_needs_human_reason(
            state, _OTHER, VerdictResult(verdict="needs_human", reason="needs a product call")
        )
    elif setup == "thread_closed":
        live = ()
    elif setup == "conversation_changed":
        live = (_thread(body="new reviewer reply"),)
    else:
        state.threads_addressed_ids.pop(f"__review_thread_body_hash__:{_THREAD}")
    _arm(state, guide)

    await _guide_poll(runner, workspace_id, gh, state, _status(*live))

    assert cycle.calls == []
    assert state.threads_addressed_ids[_THREAD] == "needs_human"
    assert gh.events == []
    hint = state.pending_operator_hint
    assert hint is not None and hint.status_reason is not None
    assert f"({detail})" in hint.status_reason
    events = await _disposition_events(factory, workspace_id)
    assert events[0].payload["evidence"]["detail"] == detail


@pytest.mark.unit
@pytest.mark.parametrize(
    "guide",
    [
        f"Please take another look at {_THREAD}.",
        f"{_THREAD}: investigate whether this is a false positive",
        f"{_THREAD}: is this a false positive?",
    ],
)
async def test_guide_without_a_ruling_still_runs_the_agent(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path, guide: str
) -> None:
    runner, workspace_id, gh, cycle, _cmd = await _runner(factory, tmp_path)
    thread = _thread()
    state = _parked(thread)
    _arm(state, guide)

    await _guide_poll(runner, workspace_id, gh, state, _status(thread))

    assert len(cycle.calls) == 1
    assert await _disposition_events(factory, workspace_id) == []


@pytest.mark.unit
@pytest.mark.parametrize(
    "guide",
    [
        f"{_THREAD} is a false positive.",
        f"Please take another look at {_OTHER}. {_THREAD} FIXED by {_FIX[:8]}.",
    ],
)
async def test_ruling_on_no_mutation_park_still_runs_the_agent(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path, guide: str
) -> None:
    runner, workspace_id, _gh, cycle, cmd = await _runner(factory, tmp_path)
    thread = _thread()
    state = _parked(_thread(_OTHER))
    _sync_needs_human_reason(
        state, _THREAD, VerdictResult(verdict="needs_human", reason="needs a product call")
    )
    _arm(state, guide)

    await _guide_poll(runner, workspace_id, _gh, state, _status(thread, _thread(_OTHER)))

    assert len(cycle.calls) == 1
    assert await _disposition_events(factory, workspace_id) == []
    assert _git_calls(cmd) == []


@pytest.mark.unit
async def test_pending_protected_block_is_left_to_the_grant_protocol(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    runner, workspace_id, _gh, _cycle, _cmd = await _runner(factory, tmp_path)
    thread = _thread()
    state = _parked(thread)
    state.mark_addressed(_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY, _HEAD)
    _arm(state, f"{_THREAD} FIXED by {_FIX[:8]}")
    hint = state.pending_operator_hint
    assert hint is not None

    outcome = await apply_park_disposition(
        runner,
        workspace_id=workspace_id,
        repo=_REPO,
        pr_number=42,
        status=_status(thread),
        state=state,
        hint=hint,
        base_branch="main",
        remote_branch=None,
        operation_id=None,
        monitor_log=None,
    )

    assert outcome is None
    _assert_untouched(state)


# --- reply / resolve faults -------------------------------------------------------


@pytest.mark.unit
async def test_resolve_fault_re_parks_with_the_forge_reason_and_a_repeat_reuses_the_reply(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    runner, workspace_id, gh, cycle, _cmd = await _runner(factory, tmp_path)
    resolve_error = GitHubClientError(
        operation="resolve_thread", returncode=1, stderr="HTTP 403: resource not accessible"
    )
    gh.resolve_error = resolve_error
    thread = _thread()
    state = _parked(thread)
    status = _status(thread)
    _arm(state, f"{_THREAD} FIXED by {_FIX[:8]}")

    await _guide_poll(runner, workspace_id, gh, state, status)

    assert state.threads_addressed_ids[_THREAD] == "needs_human"
    reason = state.threads_addressed_ids[_reason_key()]
    assert reason.startswith(f"{OPERATOR_PARK_DISPOSITION_RESOLVE_FAILED}:")
    assert f"({resolve_error.reason_code})" in reason
    assert state.pending_operator_hint is None
    posted = read_item_disposition(state, _THREAD)
    assert posted is not None and posted.reply_comment_id is not None
    events = await _disposition_events(factory, workspace_id)
    assert [event.reason_code for event in events] == [
        OPERATOR_PARK_DISPOSITION_APPLIED,
        OPERATOR_PARK_DISPOSITION_RESOLVE_FAILED,
    ]
    assert events[1].payload["evidence"]["forge_reason_code"] == resolve_error.reason_code

    gh.resolve_error = None
    gh.events.clear()
    _arm(state, f"{_THREAD} FIXED by {_FIX[:8]}", operation_id="op_guide_1213_retry")
    await _guide_poll(runner, workspace_id, gh, state, status)

    assert cycle.calls == []
    assert gh.replies() == []
    assert ("resolve", _THREAD) in gh.events
    assert state.threads_addressed_ids[_THREAD] == "fix_committed"
    retried = read_item_disposition(state, _THREAD)
    assert retried is not None
    assert retried.reply_comment_id == posted.reply_comment_id


@pytest.mark.unit
async def test_reply_fault_re_parks_and_a_repeat_guide_retries_the_reply(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    runner, workspace_id, gh, cycle, _cmd = await _runner(factory, tmp_path)
    gh.reply_error = GitHubClientError(
        operation="reply_to_thread", returncode=1, stderr="HTTP 502: bad gateway"
    )
    thread = _thread()
    state = _parked(thread)
    status = _status(thread)
    _arm(state, f"{_THREAD} FIXED by {_FIX[:8]}")

    await _guide_poll(runner, workspace_id, gh, state, status)

    assert state.threads_addressed_ids[_THREAD] == "needs_human"
    assert state.threads_addressed_ids[_reason_key()].startswith("THREAD_DISPOSITION_REPLY_FAILED:")
    assert _park_key() not in state.threads_addressed_ids

    gh.reply_error = None
    gh.events.clear()
    _arm(state, f"{_THREAD} FIXED by {_FIX[:8]}", operation_id="op_guide_1213_retry")
    await _guide_poll(runner, workspace_id, gh, state, status)

    assert cycle.calls == []
    assert [reply_thread for reply_thread, _body in gh.replies()] == [_THREAD]
    assert ("resolve", _THREAD) in gh.events
    assert state.threads_addressed_ids[_THREAD] == "fix_committed"
    events = await _disposition_events(factory, workspace_id)
    assert OPERATOR_PARK_DISPOSITION_UNAPPLIED not in [event.reason_code for event in events]


@pytest.mark.unit
async def test_reply_fault_park_left_by_another_source_is_not_ruleable(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    runner, workspace_id, gh, cycle, _cmd = await _runner(factory, tmp_path)
    thread = _thread()
    state = MonitorState()
    _mark_review_thread_addressed(state, thread, "needs_human")
    state.mark_addressed(_reason_key(), "THREAD_DISPOSITION_REPLY_FAILED: x")
    record_item_disposition(
        state,
        ThreadDisposition(
            item_id=_THREAD,
            disposition_class="false_positive",
            verdict="false_positive",
            reason="agent triage",
        ),
    )
    _arm(state, f"{_THREAD} is a false positive")

    await _guide_poll(runner, workspace_id, gh, state, _status(thread))

    assert len(cycle.calls) == 1
    assert gh.events == []


@pytest.mark.unit
async def test_ruling_retires_a_reply_posted_for_an_earlier_verdict(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    runner, workspace_id, gh, _cycle, _cmd = await _runner(factory, tmp_path)
    thread = _thread()
    state = _parked(thread)
    record_item_disposition(
        state,
        ThreadDisposition(
            item_id=_THREAD,
            disposition_class="false_positive",
            verdict="false_positive",
            reason="earlier",
            reply_comment_id="reply-earlier",
            resolved=True,
        ),
    )
    _arm(state, f"{_THREAD} FIXED by {_FIX[:8]}")

    await _guide_poll(runner, workspace_id, gh, state, _status(thread))

    disposition = read_item_disposition(state, _THREAD)
    assert disposition is not None
    assert disposition.source == "operator_disposition"
    assert disposition.reply_comment_id != "reply-earlier"
    assert any(
        key.startswith(f"__awf_disposition_retired__:{_THREAD}:")
        for key in state.threads_addressed_ids
    )


@pytest.mark.unit
async def test_refused_reply_keeps_the_thread_open_under_its_own_park(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from awf.runtime.pr_monitor_runner import operator_park_disposition

    runner, workspace_id, gh, _cycle, _cmd = await _runner(factory, tmp_path)

    async def _refuse(_self: Any, *, state: MonitorState, thread_id: str, **_kwargs: Any) -> None:
        state.mark_addressed(thread_id, "needs_human")
        state.mark_addressed(_reason_key(thread_id), "THREAD_DISPOSITION_EVIDENCE_MISSING: x")

    monkeypatch.setattr(operator_park_disposition, "reply_before_resolve", _refuse)
    thread = _thread()
    state = _parked(thread)
    _arm(state, f"{_THREAD} FIXED by {_FIX[:8]}")

    await _guide_poll(runner, workspace_id, gh, state, _status(thread))

    assert state.threads_addressed_ids[_THREAD] == "needs_human"
    assert gh.events == []
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert dict(ws.monitor_threads_addressed or {})[_THREAD] == "needs_human"
