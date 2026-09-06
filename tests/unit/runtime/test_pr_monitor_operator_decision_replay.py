"""Operator decision replay into a re-opened thread's repair prompt (issue #939).

Issue #938 made a guide directive retire a thread's parked ``needs_human`` by
*clearing* the verdict, so the thread re-enters ``AddressComments``. That alone
is not enough: the follow-up comment-repair prompt is rebuilt from the reviewer
thread only, so it carries no trace of the operator's ruling and the agent can
repeat the rejected fix and re-park. These tests pin the marker that carries the
decision across that gap — written on retirement, quoted in the thread prompt,
and dropped once the thread records a real verdict.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.common.github_client import RepoRef
from awf.runtime.feedback_policy import review_thread_body_hash, review_thread_body_state_key
from awf.runtime.monitor_prompts import address_thread_prompt
from awf.runtime.monitor_state_keys import _operator_decision_key
from awf.runtime.pr_monitor import MonitorState, OperatorHint, _mark_review_thread_addressed
from awf.runtime.pr_monitor_models import ReviewThread
from awf.runtime.pr_monitor_runner import comments
from awf.runtime.pr_monitor_runner.comment_verdict import VerdictResult
from awf.runtime.pr_monitor_runner.helpers import (
    _clear_addressed_state_by_id,
    _drop_stale_review_thread_addressed_state,
)
from awf.runtime.pr_monitor_runner.operator_hints import (
    _OPERATOR_DECISION_MAX_CHARS,
    _mark_referenced_needs_human_feedback_answered,
)

THREAD_ID = "PRRT_kwDOSJAM6s6fsqcA"
OTHER_THREAD_ID = "PRRT_kwDOSJAM6s6fsqcB"
DECISION_KEY = _operator_decision_key(THREAD_ID)
DIRECTIVE = (
    f"For {THREAD_ID}: the off-anchor edit was wrong. Fix the guard at the "
    "reviewer's line and record FIXED; do not re-escalate."
)
SECOND_DIRECTIVE = (
    f"For {THREAD_ID}: ignore my earlier call — the guard belongs on the "
    "caller side after all; record FIXED there."
)
_REPO = RepoRef(owner="dimileeh", name="agent-workspace-fabric")


def _guide(directive: str) -> OperatorHint:
    return OperatorHint(
        reason="operator guide",
        directive=directive,
        operation_id="op_guide_939",
        requested_at="2026-09-06T21:00:00+00:00",
        reason_code="OPERATOR_GUIDE",
    )


def _parked_state(*thread_ids: str) -> MonitorState:
    addressed: dict[str, str] = {}
    for thread_id in thread_ids or (THREAD_ID,):
        addressed[thread_id] = "needs_human"
        addressed[review_thread_body_state_key(thread_id)] = f"hash-{thread_id}"
    return MonitorState(threads_addressed_ids=addressed)


def _thread(thread_id: str = THREAD_ID) -> ReviewThread:
    return ReviewThread(
        thread_id=thread_id,
        path="src/awf/runtime/pr_monitor_runner/loop.py",
        line=42,
        body_excerpt="this guard is on the wrong branch",
        author="reviewer",
    )


@pytest.mark.unit
def test_retired_thread_stores_the_operator_directive() -> None:
    """Clearing a parked thread also records the ruling that cleared it."""
    state = _parked_state()

    _mark_referenced_needs_human_feedback_answered(state, hint=_guide(DIRECTIVE))

    assert THREAD_ID not in state.threads_addressed_ids
    assert state.threads_addressed_ids[DECISION_KEY] == DIRECTIVE


@pytest.mark.unit
def test_retirement_stores_the_text_actually_shown_to_the_agent() -> None:
    """A directiveless guide stores the ``acted_text`` reason it presented."""
    state = _parked_state()
    reason = f"Approved: keep the current behavior on {THREAD_ID} and resolve it."

    _mark_referenced_needs_human_feedback_answered(state, hint=_guide(""), acted_text=reason)

    assert state.threads_addressed_ids[DECISION_KEY] == reason


@pytest.mark.unit
def test_stored_operator_directive_is_bounded() -> None:
    """An unbounded directive cannot crowd out the reviewer feedback."""
    state = _parked_state()
    directive = f"{THREAD_ID} " + ("x" * (_OPERATOR_DECISION_MAX_CHARS * 3))

    _mark_referenced_needs_human_feedback_answered(state, hint=_guide(directive))

    stored = state.threads_addressed_ids[DECISION_KEY]
    assert len(stored) == _OPERATOR_DECISION_MAX_CHARS + 1
    assert stored.endswith("…")
    assert stored[:-1] == directive[:_OPERATOR_DECISION_MAX_CHARS]


@pytest.mark.unit
def test_retirement_leaves_unnamed_threads_untouched() -> None:
    """Only the thread the directive names gains a decision marker."""
    state = _parked_state(THREAD_ID, OTHER_THREAD_ID)

    _mark_referenced_needs_human_feedback_answered(state, hint=_guide(DIRECTIVE))

    assert DECISION_KEY in state.threads_addressed_ids
    assert _operator_decision_key(OTHER_THREAD_ID) not in state.threads_addressed_ids
    assert state.threads_addressed_ids[OTHER_THREAD_ID] == "needs_human"


@pytest.mark.unit
def test_non_durable_thread_records_no_decision() -> None:
    """A thread without a body-hash sidecar is not retired, so nothing is stored."""
    state = MonitorState(threads_addressed_ids={THREAD_ID: "needs_human"})

    _mark_referenced_needs_human_feedback_answered(state, hint=_guide(DIRECTIVE))

    assert state.threads_addressed_ids[THREAD_ID] == "needs_human"
    assert DECISION_KEY not in state.threads_addressed_ids


@pytest.mark.unit
def test_retired_review_comment_records_no_thread_decision() -> None:
    """The comment arm flips to ``false_positive``; it has no repair prompt to feed."""
    comment_id = "issue:3476977020"
    state = MonitorState(
        threads_addressed_ids={
            comment_id: "needs_human",
            f"__review_comment_body_hash__:{comment_id}": "comment-hash",
        }
    )

    _mark_referenced_needs_human_feedback_answered(
        state, hint=_guide(f"Resolved out of band: {comment_id} is not actionable.")
    )

    assert state.threads_addressed_ids[comment_id] == "false_positive"
    assert _operator_decision_key(comment_id) not in state.threads_addressed_ids


@pytest.mark.unit
def test_thread_prompt_quotes_the_operator_decision() -> None:
    """The re-addressed thread prompt carries the ruling as untrusted evidence."""
    prompt = address_thread_prompt(
        pr_number=939,
        repo_slug=_REPO.slug(),
        thread=_thread(),
        operator_decision=DIRECTIVE,
    )

    assert "Operator decision for this thread:" in prompt
    assert DIRECTIVE in prompt
    assert "source_kind: operator_decision" in prompt


@pytest.mark.unit
@pytest.mark.parametrize("decision", [None, "", "   "])
def test_thread_prompt_without_a_decision_is_unchanged(decision: str | None) -> None:
    """Threads with no operator ruling keep the pre-#939 prompt byte-for-byte."""
    baseline = address_thread_prompt(pr_number=939, repo_slug=_REPO.slug(), thread=_thread())

    assert (
        address_thread_prompt(
            pr_number=939,
            repo_slug=_REPO.slug(),
            thread=_thread(),
            operator_decision=decision,
        )
        == baseline
    )
    assert "Operator decision for this thread:" not in baseline


@pytest.mark.unit
async def test_address_thread_feeds_the_stored_decision_into_the_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The runner reads the marker for the thread it is addressing, not another."""
    seen: list[object] = []

    def _prompt(**kwargs: object) -> str:
        seen.append(kwargs.get("operator_decision"))
        return "PROMPT"

    async def _invoke(**_kwargs: object) -> VerdictResult:
        return VerdictResult(verdict="fix_committed")

    monkeypatch.setattr(comments, "address_thread_prompt", _prompt)
    runner = SimpleNamespace(
        _workspace_runtime_context=None,
        _invoke_cli_for_verdict_result=_invoke,
    )
    state = MonitorState(threads_addressed_ids={DECISION_KEY: DIRECTIVE})

    for thread_id in (THREAD_ID, OTHER_THREAD_ID):
        await comments._address_thread(
            runner,
            workspace_id="ws_939",
            repo=_REPO,
            pr_number=939,
            thread=_thread(thread_id),
            compose_project="proj",
            compose_file=Path("compose.yml"),
            state=state,
            owned_paths=["src/"],
            task_tag=None,
        )

    assert seen == [DIRECTIVE, None]


@pytest.mark.unit
async def test_address_thread_without_state_reads_no_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stateless call has no marker store and must not blow up on it."""
    seen: list[object] = []

    def _prompt(**kwargs: object) -> str:
        seen.append(kwargs.get("operator_decision"))
        return "PROMPT"

    async def _invoke(**_kwargs: object) -> VerdictResult:
        return VerdictResult(verdict="fix_committed")

    monkeypatch.setattr(comments, "address_thread_prompt", _prompt)
    runner = SimpleNamespace(
        _workspace_runtime_context=None,
        _invoke_cli_for_verdict_result=_invoke,
    )

    await comments._address_thread(
        runner,
        workspace_id="ws_939",
        repo=_REPO,
        pr_number=939,
        thread=_thread(),
        compose_project="proj",
        compose_file=Path("compose.yml"),
        state=None,
        owned_paths=["src/"],
        task_tag=None,
    )

    assert seen == [None]


@pytest.mark.unit
async def test_address_thread_drops_a_decision_the_new_feedback_supersedes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reviewer reply after the ruling supersedes it, so it is not replayed.

    ``_drop_stale_review_thread_addressed_state`` retires an *answered* ruling on
    fresh feedback, but it skips threads whose verdict still needs attention —
    exactly the state a live ruling sits in (guide-cleared verdict, or
    ``agent_failed``). Replaying it there would quote guidance the operator never
    gave for this conversation while telling the agent not to re-escalate.
    """
    seen: list[object] = []

    def _prompt(**kwargs: object) -> str:
        seen.append(kwargs.get("operator_decision"))
        return "PROMPT"

    async def _invoke(**_kwargs: object) -> VerdictResult:
        return VerdictResult(verdict="fix_committed")

    monkeypatch.setattr(comments, "address_thread_prompt", _prompt)
    runner = SimpleNamespace(
        _workspace_runtime_context=None,
        _invoke_cli_for_verdict_result=_invoke,
    )
    thread = _thread()
    state = MonitorState(
        threads_addressed_ids={
            DECISION_KEY: DIRECTIVE,
            review_thread_body_state_key(THREAD_ID): review_thread_body_hash(thread),
        }
    )

    await comments._address_thread(
        runner,
        workspace_id="ws_939",
        repo=_REPO,
        pr_number=939,
        thread=replace(thread, body_excerpt="new reviewer reply"),
        compose_project="proj",
        compose_file=Path("compose.yml"),
        state=state,
        owned_paths=["src/"],
        task_tag=None,
    )

    assert seen == [None]
    # Dropped, not merely skipped: a lingering marker would be parked into the
    # retired sidecar by the verdict recording and restored by a later rollback.
    assert DECISION_KEY not in state.threads_addressed_ids


@pytest.mark.unit
async def test_address_thread_replays_the_decision_for_the_ruled_on_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unchanged conversation is the one the operator ruled on — replay it."""
    seen: list[object] = []

    def _prompt(**kwargs: object) -> str:
        seen.append(kwargs.get("operator_decision"))
        return "PROMPT"

    async def _invoke(**_kwargs: object) -> VerdictResult:
        return VerdictResult(verdict="fix_committed")

    monkeypatch.setattr(comments, "address_thread_prompt", _prompt)
    runner = SimpleNamespace(
        _workspace_runtime_context=None,
        _invoke_cli_for_verdict_result=_invoke,
    )
    thread = _thread()
    state = MonitorState(
        threads_addressed_ids={
            DECISION_KEY: DIRECTIVE,
            review_thread_body_state_key(THREAD_ID): review_thread_body_hash(thread),
        }
    )

    await comments._address_thread(
        runner,
        workspace_id="ws_939",
        repo=_REPO,
        pr_number=939,
        thread=thread,
        compose_project="proj",
        compose_file=Path("compose.yml"),
        state=state,
        owned_paths=["src/"],
        task_tag=None,
    )

    assert seen == [DIRECTIVE]
    assert state.threads_addressed_ids[DECISION_KEY] == DIRECTIVE


@pytest.mark.unit
@pytest.mark.parametrize("verdict", ["fix_committed", "false_positive", "defer", "needs_human"])
def test_recorded_verdict_retires_the_operator_decision(verdict: str) -> None:
    """A real verdict answers the ruling, so the marker must not linger."""
    state = MonitorState(threads_addressed_ids={DECISION_KEY: DIRECTIVE})

    _mark_review_thread_addressed(state, _thread(), verdict)

    assert state.threads_addressed_ids[THREAD_ID] == verdict
    assert DECISION_KEY not in state.threads_addressed_ids


@pytest.mark.unit
def test_agent_failure_keeps_the_operator_decision() -> None:
    """``agent_failed`` owes the thread another attempt — keep the ruling in context."""
    state = MonitorState(threads_addressed_ids={DECISION_KEY: DIRECTIVE})

    _mark_review_thread_addressed(state, _thread(), "agent_failed")

    assert state.threads_addressed_ids[DECISION_KEY] == DIRECTIVE


@pytest.mark.unit
@pytest.mark.parametrize("verdict", ["fix_committed", "false_positive", "defer", "needs_human"])
def test_rolled_back_verdict_restores_the_operator_decision(verdict: str) -> None:
    """A cleared, unconfirmed verdict un-answers the ruling, so it comes back.

    Fix-cycle rollbacks (push failure, mid-batch abort, resolve retry) drop the
    verdict so the thread re-enters ``AddressComments``. Retirement was tied to
    that verdict, so the ruling that un-parked the thread must return with it —
    otherwise the agent re-reads only the reviewer text it already escalated on
    and can repeat the rejected approach and re-park (issue #939).
    """
    state = MonitorState(threads_addressed_ids={DECISION_KEY: DIRECTIVE})
    _mark_review_thread_addressed(state, _thread(), verdict)

    _clear_addressed_state_by_id(state, THREAD_ID)

    assert state.threads_addressed_ids[DECISION_KEY] == DIRECTIVE
    assert THREAD_ID not in state.threads_addressed_ids


@pytest.mark.unit
def test_rollback_without_a_retired_decision_adds_nothing() -> None:
    """A thread that never had a ruling gains no marker from a rollback."""
    state = MonitorState(threads_addressed_ids={})
    _mark_review_thread_addressed(state, _thread(), "fix_committed")

    _clear_addressed_state_by_id(state, THREAD_ID)

    assert DECISION_KEY not in state.threads_addressed_ids


@pytest.mark.unit
def test_rollback_keeps_a_newer_operator_ruling_over_the_parked_one() -> None:
    """A live ruling outranks the parked one a rollback would restore.

    The operator can rule again on a thread that re-parked after answering an
    earlier ruling. Restoring the parked (older) ruling over that live one would
    quote superseded guidance in the repair prompt.
    """
    state = MonitorState(threads_addressed_ids={DECISION_KEY: DIRECTIVE})
    _mark_review_thread_addressed(state, _thread(), "needs_human")
    _mark_referenced_needs_human_feedback_answered(state, hint=_guide(SECOND_DIRECTIVE))
    assert state.threads_addressed_ids[DECISION_KEY] == SECOND_DIRECTIVE

    _clear_addressed_state_by_id(state, THREAD_ID)

    assert state.threads_addressed_ids[DECISION_KEY] == SECOND_DIRECTIVE


@pytest.mark.unit
def test_fresh_reviewer_feedback_does_not_replay_the_answered_decision() -> None:
    """A superseding body change retires the ruling for good, not provisionally.

    Unlike a rollback, a changed body means new reviewer feedback the ruling
    never spoke to; replaying it there would be stale guidance.
    """
    state = MonitorState(threads_addressed_ids={DECISION_KEY: DIRECTIVE})
    thread = _thread()
    _mark_review_thread_addressed(state, thread, "fix_committed")
    status = SimpleNamespace(
        unresolved_inline_threads=(replace(thread, body_excerpt="new reviewer reply"),)
    )

    assert _drop_stale_review_thread_addressed_state(status, state) is True

    assert DECISION_KEY not in state.threads_addressed_ids
    assert THREAD_ID not in state.threads_addressed_ids


@pytest.mark.unit
def test_settle_re_address_on_new_feedback_drops_the_parked_decision() -> None:
    """A settle pass re-addressing new feedback retires the parked ruling too.

    ``_drop_stale_review_thread_addressed_state`` only runs on the poll boundary,
    so a fix-cycle settle pass can re-address the same thread on a changed body
    without it. Without dropping the parked ruling here, a later rollback of the
    *new* verdict would restore guidance that spoke to the previous body.
    """
    state = MonitorState(threads_addressed_ids={DECISION_KEY: DIRECTIVE})
    thread = _thread()
    _mark_review_thread_addressed(state, thread, "fix_committed")

    _mark_review_thread_addressed(
        state, replace(thread, body_excerpt="new reviewer reply"), "fix_committed"
    )
    _clear_addressed_state_by_id(state, THREAD_ID)

    assert DECISION_KEY not in state.threads_addressed_ids


@pytest.mark.unit
def test_settle_re_address_on_the_same_body_keeps_the_parked_decision() -> None:
    """Re-recording the same body is not fresh feedback — the ruling stays parked."""
    state = MonitorState(threads_addressed_ids={DECISION_KEY: DIRECTIVE})
    thread = _thread()
    _mark_review_thread_addressed(state, thread, "fix_committed")

    _mark_review_thread_addressed(state, thread, "defer")
    _clear_addressed_state_by_id(state, THREAD_ID)

    assert state.threads_addressed_ids[DECISION_KEY] == DIRECTIVE


@pytest.mark.unit
def test_agent_failure_on_new_feedback_drops_the_parked_decision() -> None:
    """``agent_failed`` keeps the live ruling, but a superseded parked one still goes."""
    state = MonitorState(threads_addressed_ids={DECISION_KEY: DIRECTIVE})
    thread = _thread()
    _mark_review_thread_addressed(state, thread, "fix_committed")

    _mark_review_thread_addressed(
        state, replace(thread, body_excerpt="new reviewer reply"), "agent_failed"
    )
    _clear_addressed_state_by_id(state, THREAD_ID)

    assert DECISION_KEY not in state.threads_addressed_ids


@pytest.mark.unit
def test_recorded_verdict_leaves_other_threads_decisions_alone() -> None:
    """Retirement is scoped to the thread whose verdict was just recorded."""
    other_key = _operator_decision_key(OTHER_THREAD_ID)
    state = MonitorState(threads_addressed_ids={other_key: DIRECTIVE})

    _mark_review_thread_addressed(state, _thread(), "fix_committed")

    assert state.threads_addressed_ids[other_key] == DIRECTIVE
