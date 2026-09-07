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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.common.github_client import RepoRef
from awf.runtime.feedback_policy import review_thread_body_hash, review_thread_body_state_key
from awf.runtime.monitor_prompts import address_thread_prompt
from awf.runtime.monitor_state_keys import (
    _operator_decision_issued_at_key,
    _operator_decision_key,
)
from awf.runtime.pr_monitor import MonitorState, OperatorHint, _mark_review_thread_addressed
from awf.runtime.pr_monitor_models import ReviewThread, ReviewThreadComment
from awf.runtime.pr_monitor_runner import comments
from awf.runtime.pr_monitor_runner.comment_verdict import VerdictResult
from awf.runtime.pr_monitor_runner.helpers import (
    _clear_addressed_state_by_id,
    _drop_stale_review_thread_addressed_state,
)
from awf.runtime.pr_monitor_runner.operator_hint_parsing import _OPERATOR_DECISION_MAX_CHARS
from awf.runtime.pr_monitor_runner.operator_hints import (
    _mark_referenced_needs_human_feedback_answered,
)
from awf.service.pr_monitor_adoption_seed import seedable_monitor_state

THREAD_ID = "PRRT_kwDOSJAM6s6fsqcA"
OTHER_THREAD_ID = "PRRT_kwDOSJAM6s6fsqcB"
DECISION_KEY = _operator_decision_key(THREAD_ID)
ISSUED_AT_KEY = _operator_decision_issued_at_key(THREAD_ID)
DIRECTIVE = (
    f"For {THREAD_ID}: the off-anchor edit was wrong. Fix the guard at the "
    "reviewer's line and record FIXED; do not re-escalate."
)
SECOND_DIRECTIVE = (
    f"For {THREAD_ID}: ignore my earlier call — the guard belongs on the "
    "caller side after all; record FIXED there."
)
_REPO = RepoRef(owner="dimileeh", name="agent-workspace-fabric")


GUIDE_REQUESTED_AT = "2026-09-06T21:00:00+00:00"


def _guide(directive: str) -> OperatorHint:
    return OperatorHint(
        reason="operator guide",
        directive=directive,
        operation_id="op_guide_939",
        requested_at=GUIDE_REQUESTED_AT,
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
def test_bounded_directive_keeps_each_named_thread_ruling() -> None:
    """A capped multi-thread guide stores the slice naming the thread it retires.

    A guide longer than the cap can name a second thread only past the boundary.
    Storing the leading prefix would tell that thread's agent to follow a ruling
    the operator gave for a different thread while forbidding re-escalation, so
    the stored copy is windowed on the thread id (PRRT_kwDOSJAM6s6fxBwP).
    """
    state = _parked_state(THREAD_ID, OTHER_THREAD_ID)
    first_ruling = f"For {THREAD_ID}: rework the guard. " + ("x" * _OPERATOR_DECISION_MAX_CHARS)
    second_ruling = f"For {OTHER_THREAD_ID}: the reviewer is wrong; record FALSE POSITIVE."
    directive = f"{first_ruling}\n{second_ruling}"

    _mark_referenced_needs_human_feedback_answered(state, hint=_guide(directive))

    first_stored = state.threads_addressed_ids[DECISION_KEY]
    second_stored = state.threads_addressed_ids[_operator_decision_key(OTHER_THREAD_ID)]
    assert first_stored.startswith(f"For {THREAD_ID}: rework the guard.")
    assert second_ruling in second_stored
    assert second_stored.startswith("…")
    for stored in (first_stored, second_stored):
        assert len(stored) <= _OPERATOR_DECISION_MAX_CHARS + 2


@pytest.mark.unit
def test_bounded_directive_ignores_prefix_sibling_thread_ids() -> None:
    """Windowing anchors on the whole thread key, not a longer sibling's prefix.

    ``bb:acme/widgets#12:345`` is a substring of ``bb:acme/widgets#12:3456``, so a
    plain substring search would window the shorter thread's stored copy on the
    sibling's mention and quote the sibling's ruling under "do not re-escalate"
    (PRRT_kwDOSJAM6s6fxier).
    """
    sibling_id = "bb:acme/widgets#12:3456"
    thread_id = "bb:acme/widgets#12:345"
    state = _parked_state(thread_id, sibling_id)
    sibling_ruling = f"For {sibling_id}: the reviewer is wrong; record FALSE POSITIVE. " + (
        "x" * (_OPERATOR_DECISION_MAX_CHARS * 2)
    )
    ruling = f"For {thread_id}: rework the guard and record FIXED."
    directive = f"{sibling_ruling}\n{ruling}"

    _mark_referenced_needs_human_feedback_answered(state, hint=_guide(directive))

    stored = state.threads_addressed_ids[_operator_decision_key(thread_id)]
    assert ruling in stored
    assert "FALSE POSITIVE" not in stored


@pytest.mark.unit
def test_retirement_leaves_unnamed_threads_untouched() -> None:
    """Only the thread the directive names gains a decision marker."""
    state = _parked_state(THREAD_ID, OTHER_THREAD_ID)

    _mark_referenced_needs_human_feedback_answered(state, hint=_guide(DIRECTIVE))

    assert DECISION_KEY in state.threads_addressed_ids
    assert _operator_decision_key(OTHER_THREAD_ID) not in state.threads_addressed_ids
    assert state.threads_addressed_ids[OTHER_THREAD_ID] == "needs_human"


@pytest.mark.unit
def test_hashless_thread_is_retired_and_records_its_decision() -> None:
    """A monitor-seeded row without a body-hash sidecar retires with its ruling.

    Outdated hygiene seeds ``needs_human`` with no snapshot, and an unchanged
    ``needs_human`` never re-enters ``AddressComments`` — skipping these rows
    would strand the guide at ``NotifyHuman`` (PRRT_kwDOSJAM6s6fw5zx).
    """
    state = MonitorState(threads_addressed_ids={THREAD_ID: "needs_human"})

    _mark_referenced_needs_human_feedback_answered(state, hint=_guide(DIRECTIVE))

    assert THREAD_ID not in state.threads_addressed_ids
    assert DECISION_KEY in state.threads_addressed_ids


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
def test_rollback_keeps_the_restored_ruling_for_the_unchanged_body() -> None:
    """The restored ruling stays bound to the conversation it covered.

    The rollback deletes the thread's body snapshot along with the verdict, so
    without re-recording it the restored ruling would carry no hash to compare —
    and ``_operator_decision_for_thread`` deliberately keeps rulings it cannot
    compare. On an unchanged conversation the ruling is still the operator's word.
    """
    state = MonitorState(threads_addressed_ids={DECISION_KEY: DIRECTIVE})
    thread = _thread()
    _mark_review_thread_addressed(state, thread, "fix_committed")

    _clear_addressed_state_by_id(state, THREAD_ID)

    assert comments._operator_decision_for_thread(state, thread) == DIRECTIVE


@pytest.mark.unit
def test_rollback_restored_ruling_is_dropped_by_a_reviewer_reply() -> None:
    """A reply arriving before the retry supersedes the restored ruling.

    Without the preserved snapshot the un-comparable ruling would be replayed
    into the repair prompt as guidance for feedback the operator never read,
    while telling the agent not to re-escalate.
    """
    state = MonitorState(threads_addressed_ids={DECISION_KEY: DIRECTIVE})
    thread = _thread()
    _mark_review_thread_addressed(state, thread, "fix_committed")

    _clear_addressed_state_by_id(state, THREAD_ID)

    replied = replace(thread, body_excerpt="new reviewer reply")
    assert comments._operator_decision_for_thread(state, replied) is None
    assert DECISION_KEY not in state.threads_addressed_ids


@pytest.mark.unit
def test_rollback_keeps_a_live_ruling_comparable_to_the_new_feedback() -> None:
    """A live ruling the rollback preserves keeps its body binding too.

    The clear drops the snapshot for every item; a live ruling that survives it
    would otherwise become un-comparable in exactly the same way as a restored one.
    """
    state = MonitorState(threads_addressed_ids={DECISION_KEY: DIRECTIVE})
    thread = _thread()
    _mark_review_thread_addressed(state, thread, "needs_human")
    _mark_referenced_needs_human_feedback_answered(state, hint=_guide(SECOND_DIRECTIVE))

    _clear_addressed_state_by_id(state, THREAD_ID)

    replied = replace(thread, body_excerpt="new reviewer reply")
    assert comments._operator_decision_for_thread(state, replied) is None
    assert DECISION_KEY not in state.threads_addressed_ids


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


def _hashless_ruling_state() -> MonitorState:
    """A guide-retired thread seeded by hygiene, i.e. with no body snapshot."""
    state = MonitorState(threads_addressed_ids={THREAD_ID: "needs_human"})
    _mark_referenced_needs_human_feedback_answered(state, hint=_guide(DIRECTIVE))
    return state


def _replied_thread(*, at: datetime | None, viewer_did_author: bool = False) -> ReviewThread:
    return replace(
        _thread(),
        comments=(
            ReviewThreadComment(
                comment_id="4688598838",
                body="the guard still misses the empty case",
                author="reviewer",
                created_at=at,
                viewer_did_author=viewer_did_author,
            ),
        ),
    )


@pytest.mark.unit
def test_hashless_retirement_stamps_the_ruling_issue_time() -> None:
    """The stamp is when the operator wrote the guide, not when AWF consumed it.

    A guide can sit queued for a monitor cycle; dating the ruling from consumption
    would adopt a reply that landed in between as feedback the operator ruled on.
    """
    state = _hashless_ruling_state()

    stamped = datetime.fromisoformat(state.threads_addressed_ids[ISSUED_AT_KEY])
    assert stamped == datetime.fromisoformat(GUIDE_REQUESTED_AT)


@pytest.mark.unit
def test_naive_requested_at_is_stamped_as_utc() -> None:
    """An offsetless hint timestamp still orders against forge timestamps."""
    state = MonitorState(threads_addressed_ids={THREAD_ID: "needs_human"})
    naive = replace(_guide(DIRECTIVE), requested_at="2026-09-06T21:00:00")

    _mark_referenced_needs_human_feedback_answered(state, hint=naive)

    assert state.threads_addressed_ids[ISSUED_AT_KEY] == GUIDE_REQUESTED_AT


@pytest.mark.unit
@pytest.mark.parametrize("requested_at", [None, "", "not-a-timestamp"])
def test_undatable_guide_stamps_the_consume_time(requested_at: str | None) -> None:
    """Without a usable request time, now is the latest the ruling can be from."""
    before = datetime.now(UTC)
    state = MonitorState(threads_addressed_ids={THREAD_ID: "needs_human"})
    undatable = replace(_guide(DIRECTIVE), requested_at=requested_at)

    _mark_referenced_needs_human_feedback_answered(state, hint=undatable)

    stamped = datetime.fromisoformat(state.threads_addressed_ids[ISSUED_AT_KEY])
    assert before <= stamped <= datetime.now(UTC)


@pytest.mark.unit
def test_reply_while_the_guide_was_queued_retires_the_ruling() -> None:
    """The window between writing the guide and consuming it is now covered.

    This reply predates the retirement pass, so a consume-time stamp would have
    kept the ruling and replayed it over feedback the operator never read
    (PRRT_kwDOSJAM6s6fxBwT).
    """
    state = _hashless_ruling_state()
    queued_reply = _replied_thread(
        at=datetime.fromisoformat(GUIDE_REQUESTED_AT) + timedelta(minutes=5)
    )

    assert comments._operator_decision_for_thread(state, queued_reply) is None
    assert DECISION_KEY not in state.threads_addressed_ids


@pytest.mark.unit
def test_hash_bound_retirement_records_no_stamp() -> None:
    """A snapshot binds the ruling on its own; a stale stamp is cleared with it."""
    state = _parked_state()
    state.threads_addressed_ids[ISSUED_AT_KEY] = "2026-09-06T20:00:00+00:00"

    _mark_referenced_needs_human_feedback_answered(state, hint=_guide(DIRECTIVE))

    assert ISSUED_AT_KEY not in state.threads_addressed_ids


@pytest.mark.unit
def test_reply_after_a_hashless_ruling_retires_it() -> None:
    """A reply landing before the re-addressed pass supersedes the ruling.

    The hygiene-seeded row carries no body hash, so the snapshot comparison cannot
    see this reply; without the issue-time stamp the repair prompt would quote a
    ruling made before the feedback under "do not escalate" (PRRT_kwDOSJAM6s6fxBwT).
    """
    state = _hashless_ruling_state()
    replied = _replied_thread(at=datetime.now(UTC) + timedelta(hours=1))

    assert comments._operator_decision_for_thread(state, replied) is None
    assert DECISION_KEY not in state.threads_addressed_ids
    assert ISSUED_AT_KEY not in state.threads_addressed_ids


@pytest.mark.unit
def test_unchanged_conversation_keeps_the_hashless_ruling() -> None:
    """Activity the operator already read does not retire their ruling."""
    state = _hashless_ruling_state()
    unchanged = _replied_thread(at=datetime.fromisoformat(GUIDE_REQUESTED_AT) - timedelta(hours=1))

    assert comments._operator_decision_for_thread(state, unchanged) == DIRECTIVE
    assert state.threads_addressed_ids[ISSUED_AT_KEY]


@pytest.mark.unit
def test_awf_reply_after_a_hashless_ruling_keeps_it() -> None:
    """AWF's own follow-up is not reviewer feedback the operator failed to read."""
    state = _hashless_ruling_state()
    own_reply = _replied_thread(at=datetime.now(UTC) + timedelta(hours=1), viewer_did_author=True)

    assert comments._operator_decision_for_thread(state, own_reply) == DIRECTIVE


@pytest.mark.unit
def test_naive_reply_timestamp_is_read_as_utc() -> None:
    """A forge timestamp without an offset still orders against the stamp."""
    state = _hashless_ruling_state()
    replied = _replied_thread(at=datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1))

    assert comments._operator_decision_for_thread(state, replied) is None


@pytest.mark.unit
def test_untimestamped_conversation_keeps_the_hashless_ruling() -> None:
    """Ordering is unprovable without comment timestamps, so the ruling stands."""
    state = _hashless_ruling_state()

    assert comments._operator_decision_for_thread(state, _replied_thread(at=None)) == DIRECTIVE


@pytest.mark.unit
def test_unparseable_stamp_keeps_the_hashless_ruling() -> None:
    """A stamp AWF cannot read proves nothing about ordering — fail open."""
    state = _hashless_ruling_state()
    state.threads_addressed_ids[ISSUED_AT_KEY] = "not-a-timestamp"
    replied = _replied_thread(at=datetime.now(UTC) + timedelta(hours=1))

    assert comments._operator_decision_for_thread(state, replied) == DIRECTIVE


@pytest.mark.unit
def test_unstamped_hashless_ruling_is_still_kept() -> None:
    """Rows written before the stamp existed keep the pre-existing behavior."""
    state = MonitorState(threads_addressed_ids={DECISION_KEY: DIRECTIVE})
    replied = _replied_thread(at=datetime.now(UTC) + timedelta(hours=1))

    assert comments._operator_decision_for_thread(state, replied) == DIRECTIVE


@pytest.mark.unit
def test_stale_hash_bound_ruling_drops_its_stamp_too() -> None:
    """A ruling retired by the snapshot check leaves no orphan stamp behind."""
    state = _hashless_ruling_state()
    thread = _thread()
    state.threads_addressed_ids[review_thread_body_state_key(THREAD_ID)] = review_thread_body_hash(
        thread
    )

    replied = replace(thread, body_excerpt="new reviewer reply")
    assert comments._operator_decision_for_thread(state, replied) is None
    assert ISSUED_AT_KEY not in state.threads_addressed_ids


@pytest.mark.unit
def test_adoption_seeds_the_ruling_with_its_issue_time_binding() -> None:
    """The stamp crosses re-adoption with the ruling it binds.

    Left behind, the successor would hold an unbindable ruling again and quote it
    over a reply the operator never read.
    """
    seeded = seedable_monitor_state(
        {DECISION_KEY: DIRECTIVE, ISSUED_AT_KEY: "2026-09-07T02:00:00+00:00"}
    )

    assert seeded == {DECISION_KEY: DIRECTIVE, ISSUED_AT_KEY: "2026-09-07T02:00:00+00:00"}
