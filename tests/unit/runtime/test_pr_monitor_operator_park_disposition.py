"""Operator ruling that closes an ``AGENT_NON_FIXED_WITH_MUTATION`` park (issue #1213).

The production repro: three threads were parked on refused correction attempts,
later commits fixed them, and the operator guided "make NO code changes; record
dispositions only: thread A FIXED by commit Y". The agent ran, did nothing, the
hint was consumed, and the monitor returned to ``NotifyHuman`` on every poll.
These tests pin the explicit ruling, one strict ``AWF-RULING`` line per thread
(#1215; prose is never parsed for one): it is applied without the agent, the
FIXED claim is checked against the PR head, and the thread is replied to and
resolved with an operator-sourced disposition. Anything short of that fails
closed with a distinct reason code.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import COMMAND_TIMEOUT_REASON, CommandResult, FakeCommandRunner
from awf.common.github_client import GitHubClientError, RepoRef
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.monitor_state_keys import _disposition_unverified_key
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
from awf.runtime.pr_monitor_runner.thread_disposition_ack import UNJUDGED, record_ack_judgment
from awf.runtime.pr_monitor_runner.thread_disposition_gate import (
    DispositionGateResult,
    review_thread_disposition_gate,
)
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)
from tests.unit.runtime._thread_disposition_fixtures import (
    OrderedForge,
    settled_baseline_state,
)
from tests.unit.runtime.test_pr_monitor_thread_disposition_gate import _marker

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
    runner: Any,
    workspace_id: str,
    gh: OrderedForge,
    state: MonitorState,
    status: PRStatus,
    *,
    remote_push_url: str | None = None,
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
        remote_push_url=remote_push_url,
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


def _fixed(thread_id: str = _THREAD, sha: str = _FIX[:8]) -> str:
    return f"AWF-RULING {thread_id} FIXED {sha}"


def _false_positive(thread_id: str = _THREAD) -> str:
    return f"AWF-RULING {thread_id} FALSE_POSITIVE"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("guide", "expected"),
    [
        (_fixed(sha=_FIX), {_THREAD: ParkRuling("fixed", _FIX)}),
        (_fixed(sha="ABC1234"), {_THREAD: ParkRuling("fixed", "abc1234")}),
        # An all-digit SHA is a SHA here: the grammar, not the token shape, says so.
        (_fixed(sha="1234567"), {_THREAD: ParkRuling("fixed", "1234567")}),
        (_false_positive(), {_THREAD: ParkRuling("false_positive")}),
        (
            f"{_false_positive()}: the guard already exists upstream",
            {_THREAD: ParkRuling("false_positive")},
        ),
        # One ruling per line; prose lines around them are not rulings.
        (
            f"Make NO code changes; record dispositions only.\n"
            f"{_fixed()}\r\n  {_false_positive(_OTHER)}  \nThanks!",
            {_THREAD: ParkRuling("fixed", _FIX[:8]), _OTHER: ParkRuling("false_positive")},
        ),
        # A repeated identical ruling is one ruling; disagreeing ones fail closed.
        (f"{_fixed()}\n{_fixed()}", {_THREAD: ParkRuling("fixed", _FIX[:8])}),
        (f"{_fixed()}\n{_false_positive()}", {_THREAD: None}),
        (f"{_fixed()}\n{_fixed(sha='abc1234')}", {_THREAD: None}),
    ],
)
def test_park_dispositions_accepts_the_exact_ruling_grammar(
    guide: str, expected: dict[str, ParkRuling | None]
) -> None:
    assert park_dispositions(guide) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "guide",
    [
        None,
        "",
        # The prose rulings earlier rounds parsed are not rulings any more.
        f"{_THREAD} FIXED by {_FIX[:8]}",
        f"{_THREAD} is a false positive.",
        f"Make no code changes, {_THREAD} is a false positive",
        # Near-misses: case, extra words, missing or malformed parts.
        f"awf-ruling {_THREAD} FIXED {_FIX[:8]}",
        f"AWF-RULING {_THREAD} fixed {_FIX[:8]}",
        f"AWF-RULING {_THREAD} false_positive",
        f"AWF-RULING {_THREAD} FALSE POSITIVE",
        f"AWF-RULING {_THREAD} FIXED by {_FIX[:8]}",
        f"AWF-RULING {_THREAD} FIXED {_FIX[:8]} once CI passes",
        f"AWF-RULING {_THREAD} FALSE_POSITIVE?",
        f"AWF-RULING {_THREAD} FALSE_POSITIVE:",
        f"AWF-RULING {_THREAD} FIXED abc123",
        f"AWF-RULING {_THREAD} FIXED {'a' * 41}",
        f"AWF-RULING {_THREAD} FIXED {_FIX[:7]}z",
        f"AWF-RULING {_THREAD}",
        f"AWF-RULING FIXED {_FIX[:8]}",
        f"AWF-RULING thread-{_THREAD} FALSE_POSITIVE",
        # A ruling line embedded in prose, quoted, or bulleted is not on its own line.
        f"Please apply: {_fixed()}",
        f"Do not send {_false_positive()}",
        f"- {_fixed()}",
        f"`{_fixed()}`",
    ],
)
def test_park_dispositions_ignores_anything_but_the_exact_grammar(guide: str | None) -> None:
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
    _arm(state, f"Make NO code changes; record dispositions only.\n{_fixed()}")

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


def _fetches(cmd: FakeCommandRunner) -> list[list[str]]:
    return [call.args for call in cmd.calls if call.args[:1] == ["git"] and "fetch" in call.args]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("remote_push_url", "remote"),
    [(None, "origin"), ("https://forge.example/acme/widgets.git", None)],
    ids=["origin", "push_url"],
)
async def test_fixed_ruling_fetches_the_pr_branch_before_the_ancestry_probe(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    remote_push_url: str | None,
    remote: str | None,
) -> None:
    # After a mutation park the worktree sits on the rollback commit while the PR
    # head (and the operator's fix) may exist only on the remote: without a fetch
    # the probe would refuse a legitimate ruling on every retry.
    runner, workspace_id, gh, _cycle, cmd = await _runner(factory, tmp_path)
    thread = _thread()
    state = _parked(thread)
    _arm(state, _fixed())

    await _guide_poll(
        runner, workspace_id, gh, state, _status(thread), remote_push_url=remote_push_url
    )

    assert state.threads_addressed_ids[_THREAD] == "fix_committed"
    fetches = _fetches(cmd)
    assert len(fetches) == 1
    assert fetches[0][-2:] == [remote or remote_push_url, f"refs/heads/awf/{workspace_id}"]
    order = [
        "fetch" if "fetch" in call.args else "probe"
        for call in cmd.calls
        if call.args[:1] == ["git"] and ({"fetch", "rev-parse", "merge-base"} & set(call.args))
    ]
    assert order[0] == "fetch"


@pytest.mark.unit
async def test_failed_pr_branch_fetch_still_probes_local_objects(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    runner, workspace_id, gh, _cycle, cmd = await _runner(factory, tmp_path)
    cmd.respond_when(lambda args: "fetch" in args, returncode=128, stderr="network down")
    thread = _thread()
    state = _parked(thread)
    _arm(state, _fixed())

    await _guide_poll(runner, workspace_id, gh, state, _status(thread))

    assert len(_fetches(cmd)) == 1
    assert state.threads_addressed_ids[_THREAD] == "fix_committed"


@pytest.mark.unit
async def test_stalled_pr_branch_fetch_is_bounded_and_still_probes_local_objects(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    # A remote that accepts the connection and then goes silent must not wedge
    # the ruling: the fetch is bounded, and its timeout falls through to the
    # local ancestry probe exactly like a failed fetch.
    runner, workspace_id, gh, _cycle, _cmd = await _runner(factory, tmp_path)

    class _StalledFetch(FakeCommandRunner):
        async def run(self, args: list[str], **kwargs: Any) -> Any:
            if "fetch" not in args:
                return await super().run(args, **kwargs)
            await super().run(args, **kwargs)
            if kwargs.get("timeout_seconds") is None:
                await asyncio.sleep(3600.0)
            return CommandResult(124, "", "timed out", COMMAND_TIMEOUT_REASON)

    stalled = _StalledFetch()
    _git(stalled)
    runner._deps.runner = stalled
    thread = _thread()
    state = _parked(thread)
    _arm(state, _fixed())

    await asyncio.wait_for(_guide_poll(runner, workspace_id, gh, state, _status(thread)), 5.0)

    fetch_calls = [call for call in stalled.calls if "fetch" in call.args]
    assert len(fetch_calls) == 1
    assert fetch_calls[0].timeout_seconds is not None
    assert state.threads_addressed_ids[_THREAD] == "fix_committed"


@pytest.mark.unit
async def test_ancestry_probe_without_a_remote_branch_skips_the_fetch(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    runner, workspace_id, _gh, _cycle, cmd = await _runner(factory, tmp_path)
    thread = _thread()
    state = _parked(thread)
    _arm(state, _fixed())
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

    assert outcome is not None
    assert _fetches(cmd) == []
    assert any("merge-base" in call for call in _git_calls(cmd))


@pytest.mark.unit
async def test_false_positive_ruling_resolves_an_outdated_park_without_git(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    runner, workspace_id, gh, cycle, cmd = await _runner(factory, tmp_path)
    thread = _thread(outdated=True)
    state = _parked(thread)
    _arm(state, _false_positive())

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
async def test_ruling_note_that_reads_like_prose_does_not_change_the_action(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    # The note is free text; the #1189 prose parser must not claim the ruling line.
    runner, workspace_id, gh, cycle, _cmd = await _runner(factory, tmp_path)
    thread = _thread(outdated=True)
    state = _parked(thread)
    _arm(state, f"{_false_positive()}: reject the preserved attempt and resolve this thread")

    await _guide_poll(runner, workspace_id, gh, state, _status(thread))

    assert cycle.calls == []
    assert state.threads_addressed_ids[_THREAD] == "false_positive"
    assert ("resolve", _THREAD) in gh.events


@pytest.mark.unit
async def test_two_threads_in_one_guide_are_both_applied(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    runner, workspace_id, gh, _cycle, _cmd = await _runner(factory, tmp_path)
    first, second = _thread(), _thread(_OTHER, body="other finding")
    state = _parked(first, second)
    _arm(state, f"{_fixed()}\n{_fixed(_OTHER, _FIX[:9])}")

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
    _arm(state, _fixed())

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
    _arm(state, _fixed())

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
    _arm(state, _fixed())

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
    _arm(state, _fixed())

    await _guide_poll(runner, workspace_id, gh, state, _status(thread, head=""))

    _assert_untouched(state)
    assert _git_calls(cmd) == []


@pytest.mark.unit
@pytest.mark.parametrize(
    ("setup", "detail"),
    [
        ("conflicting_rulings", "thread_without_disposition"),
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
    guide = _fixed()
    if setup == "conflicting_rulings":
        guide += f"\n{_false_positive()}"
    elif setup == "not_mutation_park":
        # A guide that rules on a mutation park takes over every thread it rules on.
        guide += f"\n{_false_positive(_OTHER)}"
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
        f"{_THREAD}: is this a false positive?",
        # Prose and near-miss rulings go through the agent cycle, never applied.
        f"Make NO code changes; {_THREAD} FIXED by {_FIX[:8]}.",
        f"awf-ruling {_THREAD} FIXED {_FIX[:8]}",
        f"AWF-RULING {_THREAD} FALSE_POSITIVE because the guard exists",
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
        _false_positive(),
        f"Please take another look at {_OTHER}.\n{_fixed()}",
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
    _arm(state, _fixed())
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
    _arm(state, _fixed())

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
    _arm(state, _fixed(), operation_id="op_guide_1213_retry")
    await _guide_poll(runner, workspace_id, gh, state, status)

    assert cycle.calls == []
    assert gh.replies() == []
    assert ("resolve", _THREAD) in gh.events
    assert state.threads_addressed_ids[_THREAD] == "fix_committed"
    retried = read_item_disposition(state, _THREAD)
    assert retried is not None
    assert retried.reply_comment_id == posted.reply_comment_id


@pytest.mark.unit
async def test_repeat_guide_retries_only_the_thread_whose_resolve_failed(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    runner, workspace_id, gh, cycle, _cmd = await _runner(factory, tmp_path)
    forge_resolve = gh.resolve_thread

    async def _resolve_fails_for_other(*, thread_id: str) -> None:
        if thread_id == _OTHER:
            gh.events.append(("resolve_failed", thread_id))
            raise GitHubClientError(
                operation="resolve_thread", returncode=1, stderr="HTTP 502: bad gateway"
            )
        await forge_resolve(thread_id=thread_id)

    gh.resolve_thread = _resolve_fails_for_other  # type: ignore[method-assign]
    first, second = _thread(), _thread(_OTHER, body="other finding")
    state = _parked(first, second)
    guide = f"{_fixed()}\n{_false_positive(_OTHER)}"
    _arm(state, guide)

    await _guide_poll(runner, workspace_id, gh, state, _status(first, second))

    assert state.threads_addressed_ids[_THREAD] == "fix_committed"
    assert state.threads_addressed_ids[_OTHER] == "needs_human"
    assert state.threads_addressed_ids[_reason_key(_OTHER)].startswith(
        f"{OPERATOR_PARK_DISPOSITION_RESOLVE_FAILED}:"
    )

    # The resolved thread is off the PR; the same guide finishes the other one.
    gh.resolve_thread = forge_resolve  # type: ignore[method-assign]
    gh.events.clear()
    _arm(state, guide, operation_id="op_guide_1213_retry")
    await _guide_poll(runner, workspace_id, gh, state, _status(second))

    assert cycle.calls == []
    assert gh.replies() == []
    assert gh.events == [("resolve", _OTHER)]
    assert state.threads_addressed_ids[_OTHER] == "false_positive"
    assert state.threads_addressed_ids[_THREAD] == "fix_committed"
    events = await _disposition_events(factory, workspace_id)
    assert OPERATOR_PARK_DISPOSITION_UNAPPLIED not in [event.reason_code for event in events]


@pytest.mark.unit
@pytest.mark.parametrize(
    "closed_ruling",
    [_false_positive(), _fixed(sha="abc1234")],
    ids=["other-kind", "other-sha"],
)
async def test_repeat_guide_with_another_ruling_on_a_closed_thread_is_refused(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path, closed_ruling: str
) -> None:
    runner, workspace_id, gh, cycle, _cmd = await _runner(factory, tmp_path)
    first, second = _thread(), _thread(_OTHER, body="other finding")
    state = _parked(first, second)
    _arm(state, _fixed())
    await _guide_poll(runner, workspace_id, gh, state, _status(first, second))
    assert state.threads_addressed_ids[_THREAD] == "fix_committed"

    # A ruling the closed thread never got is no retry of it: fail closed.
    gh.events.clear()
    _arm(state, f"{closed_ruling}\n{_false_positive(_OTHER)}", operation_id="op_2")
    await _guide_poll(runner, workspace_id, gh, state, _status(second))

    assert cycle.calls == []
    assert gh.events == []
    _assert_untouched(state, _OTHER)
    events = await _disposition_events(factory, workspace_id)
    assert events[-1].reason_code == OPERATOR_PARK_DISPOSITION_UNAPPLIED
    assert events[-1].payload["evidence"]["detail"] == "not_mutation_park"


@pytest.mark.unit
async def test_repeat_guide_on_a_reopened_closed_thread_is_refused(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    runner, workspace_id, gh, cycle, _cmd = await _runner(factory, tmp_path)
    first, second = _thread(), _thread(_OTHER, body="other finding")
    state = _parked(first, second)
    _arm(state, _fixed())
    await _guide_poll(runner, workspace_id, gh, state, _status(first, second))

    # The ruled thread is open again: the earlier ruling no longer covers it.
    gh.events.clear()
    _arm(state, f"{_fixed()}\n{_false_positive(_OTHER)}", operation_id="op_2")
    await _guide_poll(runner, workspace_id, gh, state, _status(first, second))

    assert cycle.calls == []
    assert gh.events == []
    _assert_untouched(state, _OTHER)
    events = await _disposition_events(factory, workspace_id)
    assert events[-1].payload["evidence"]["detail"] == "not_mutation_park"


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
    _arm(state, _fixed())

    await _guide_poll(runner, workspace_id, gh, state, status)

    assert state.threads_addressed_ids[_THREAD] == "needs_human"
    assert state.threads_addressed_ids[_reason_key()].startswith("THREAD_DISPOSITION_REPLY_FAILED:")
    assert _park_key() not in state.threads_addressed_ids

    gh.reply_error = None
    gh.events.clear()
    _arm(state, _fixed(), operation_id="op_guide_1213_retry")
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
    _arm(state, _false_positive())

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
    _arm(state, _fixed())

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
    _arm(state, _fixed())

    await _guide_poll(runner, workspace_id, gh, state, _status(thread))

    assert state.threads_addressed_ids[_THREAD] == "needs_human"
    assert gh.events == []
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert dict(ws.monitor_threads_addressed or {})[_THREAD] == "needs_human"


# --- #1238: a ruling settles a thread the disposition gate flagged ------------------

_UNVERIFIED = "REVIEW_THREAD_DISPOSITION_UNVERIFIED"
_BOT = {"login": "review-bot", "__typename": "Bot"}


def _resolved_with_bot_reply(
    gh: OrderedForge, thread_id: str = _THREAD, *, resolved_by: str = "awf-test-bot"
) -> None:
    """AWF replied FALSE POSITIVE and resolved; the opening bot then replied after it."""
    nodes = [
        ("c-1", "guard the None payload", False, "2026-11-01T00:00:00+00:00"),
        ("reply-0", _marker("false_positive", thread_id), True, "2026-11-02T00:00:00+00:00"),
        ("c-3", "This is a fix rather than a false positive.", False, "2026-11-03T00:00:00+00:00"),
    ]
    gh.review_threads[thread_id] = {
        "id": thread_id,
        "isResolved": True,
        "isOutdated": False,
        "resolvedBy": {"login": resolved_by},
        "comments": {
            "nodes": [
                {
                    "id": node_id,
                    "body": body,
                    "bodyText": body,
                    "viewerDidAuthor": viewer,
                    "createdAt": created_at,
                    **({} if viewer else {"author": _BOT}),
                }
                for node_id, body, viewer, created_at in nodes
            ],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        },
    }


async def _audit_gate(runner: Any, workspace_id: str, state: MonitorState) -> DispositionGateResult:
    return await review_thread_disposition_gate(
        runner,
        workspace_id=workspace_id,
        repo=_REPO,
        pr_number=42,
        status=_status(),
        state=state,
        base_branch="main",
        remote_branch=f"awf/{workspace_id}",
        monitor_log=None,
    )


async def _gate_flagged(
    runner: Any, workspace_id: str, gh: OrderedForge, thread_id: str = _THREAD
) -> MonitorState:
    """The issue's park: AWF's FALSE POSITIVE stands, the bot's reply was never kept."""
    state = settled_baseline_state()
    state.mark_addressed(thread_id, "false_positive")
    state.mark_addressed(f"__review_thread_body_hash__:{thread_id}", "f" * 64)
    record_item_disposition(
        state,
        ThreadDisposition(
            item_id=thread_id,
            disposition_class="false_positive",
            verdict="false_positive",
            reason="the caller already rejects None payloads",
            reply_comment_id="reply-0",
            resolved=True,
            conversation_digest="c" * 64,
        ),
    )
    audit = await gh.fetch_review_thread_audit(repo=_REPO, pr_number=42)
    [bot_reply] = audit.threads[0].trailing_comments
    record_ack_judgment(state, thread_id, bot_reply, UNJUDGED)
    blocked = await _audit_gate(runner, workspace_id, state)
    assert blocked.unverified == (thread_id,)
    assert state.threads_addressed_ids[_disposition_unverified_key(thread_id)] == _UNVERIFIED
    return state


@pytest.mark.unit
@pytest.mark.parametrize(
    ("ruling", "verdict", "disposition_class"),
    [
        (_false_positive(), "false_positive", "false_positive"),
        (_fixed(), "fix_committed", "fixed"),
    ],
    ids=["false-positive", "fixed"],
)
async def test_ruling_on_a_gate_flagged_resolved_thread_settles_it_and_the_gate_certifies(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    ruling: str,
    verdict: str,
    disposition_class: str,
) -> None:
    runner, workspace_id, gh, cycle, _cmd = await _runner(factory, tmp_path)
    _resolved_with_bot_reply(gh)
    state = await _gate_flagged(runner, workspace_id, gh)
    _arm(state, ruling)

    terminal = await _guide_poll(runner, workspace_id, gh, state, _status())

    assert terminal is False
    assert cycle.calls == []  # no agent
    assert state.pending_operator_hint is None
    assert state.threads_addressed_ids[_THREAD] == verdict
    disposition = read_item_disposition(state, _THREAD)
    assert disposition is not None
    assert disposition.disposition_class == disposition_class
    assert disposition.source == "operator_disposition"
    assert disposition.reply_comment_id is not None and disposition.resolved is True
    assert disposition.thread_body_hash == "f" * 64
    replies = gh.replies()
    assert [tid for tid, _ in replies] == [_THREAD]
    assert f"class={disposition_class}" in replies[0][1]
    assert gh.events[-1] == ("resolve", _THREAD)
    events = await _disposition_events(factory, workspace_id)
    assert [event.reason_code for event in events] == [OPERATOR_PARK_DISPOSITION_APPLIED]
    assert events[0].payload["evidence"]["thread_id"] == _THREAD

    certified = await _audit_gate(runner, workspace_id, state)

    assert certified.blocker is None and certified.count_rows is not None
    assert certified.unverified == ()
    assert _disposition_unverified_key(_THREAD) not in state.threads_addressed_ids

    # The same guide again is satisfied: nothing is posted or resolved twice.
    gh.events.clear()
    _arm(state, ruling, operation_id="op_guide_1238_repeat")
    await _guide_poll(runner, workspace_id, gh, state, _status())
    assert cycle.calls == [] and gh.events == []


@pytest.mark.unit
@pytest.mark.parametrize(
    ("setup", "detail"),
    [
        ("listed_open", "flagged_thread_open"),
        ("resolved_by_other", "flagged_thread_not_resolved_by_awf"),
        ("audit_fault", "flagged_thread_unreadable"),
        ("audit_unsupported", "flagged_thread_unreadable"),
        ("thread_missing", "flagged_thread_unreadable"),
        ("not_ancestor", "sha_not_ancestor"),
    ],
)
async def test_ruling_on_a_gate_flagged_thread_fails_closed_when_it_cannot_be_proven(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path, setup: str, detail: str
) -> None:
    runner, workspace_id, gh, cycle, _cmd = await _runner(
        factory, tmp_path, ancestor=1 if setup == "not_ancestor" else 0
    )
    _resolved_with_bot_reply(gh)
    state = await _gate_flagged(runner, workspace_id, gh)
    status = _status()
    if setup == "listed_open":
        status = _status(_thread())
    elif setup == "resolved_by_other":
        gh.review_threads[_THREAD]["resolvedBy"] = {"login": "octocat"}
    elif setup in {"audit_fault", "audit_unsupported", "thread_missing"}:
        audit = await gh.fetch_review_thread_audit(repo=_REPO, pr_number=42)

        async def _read(**_kwargs: Any) -> Any:
            if setup == "audit_fault":
                raise GitHubClientError(
                    operation="fetch_review_thread_audit", returncode=1, stderr="HTTP 502"
                )
            return replace(audit, supported=setup == "thread_missing", threads=())

        gh.fetch_review_thread_audit = _read  # type: ignore[method-assign]
    _arm(state, _fixed() if setup == "not_ancestor" else _false_positive())

    await _guide_poll(runner, workspace_id, gh, state, status)

    assert cycle.calls == []
    assert gh.events == []
    assert state.threads_addressed_ids[_THREAD] == "false_positive"
    assert state.threads_addressed_ids[_disposition_unverified_key(_THREAD)] == _UNVERIFIED
    hint = state.pending_operator_hint
    assert hint is not None and hint.status_reason is not None
    assert f"({detail})" in hint.status_reason
    assert OPERATOR_PARK_DISPOSITION_UNAPPLIED in hint.status_reason
    events = await _disposition_events(factory, workspace_id)
    assert [event.payload["evidence"]["detail"] for event in events] == [detail]
