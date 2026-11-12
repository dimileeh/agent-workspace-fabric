"""``AGENT_NON_FIXED_WITH_MUTATION`` parks are re-evaluated, not sticky (issue #1213).

A correction attempt that mutated the worktree and then reported a non-FIXED
verdict is rolled back and its review thread parked ``needs_human``. That verdict
was rendered on the rollback commit. Once the PR head moves past that commit, or
the thread becomes outdated, the verdict is stale. ``decide()`` used to keep the
thread parked forever and loop ``NotifyHuman`` on the stale reason. These tests
pin the re-evaluation and the cases that must still park.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from awf.runtime.monitor_state_keys import _mutation_park_key, _needs_human_reason_key
from awf.runtime.pr_monitor import (
    AddressComments,
    CheckState,
    MergeableState,
    MergeStateStatus,
    MonitorConfig,
    MonitorState,
    NotifyHuman,
    PRStatus,
    ReviewThread,
    ReviewThreadComment,
    _mark_review_thread_addressed,
    decide,
)
from awf.runtime.pr_monitor_runner import comment_verdict
from awf.runtime.pr_monitor_runner.comment_verdict_correction_mutation import (
    resolve_correction_non_fixed_mutation,
)
from awf.runtime.pr_monitor_runner.comment_verdict_protocol_types import (
    AGENT_NON_FIXED_WITH_MUTATION,
    AGENT_VERDICT_PROTOCOL_VIOLATION,
    VerdictResult,
)
from awf.runtime.pr_monitor_runner.helpers import (
    _clear_addressed_state_by_id,
    _notify_human_reason,
    _sync_needs_human_reason,
)
from awf.runtime.pr_monitor_runner.notify_human_details import _needs_human_reason_state_key

_THREAD = "PRRT_kwDOmutationpark0A"
_ROLLBACK = "a1b2c3d4e5f6" + "0" * 28
_NEW_HEAD = "f6e5d4c3b2a1" + "9" * 28
_REASON_KEY = f"__needs_human_reason__:{_THREAD}"
_PARK_KEY = f"__awf_mutation_park__:{_THREAD}"


def _thread(*, outdated: bool = False, body: str = "guard the None payload") -> ReviewThread:
    return ReviewThread(
        thread_id=_THREAD,
        path="src/app.py",
        line=12,
        body_excerpt=body,
        author="review-bot",
        is_resolved=False,
        is_outdated=outdated,
        comments=(ReviewThreadComment(comment_id="c-1", body=body, author="review-bot"),),
    )


def _status(thread: ReviewThread, *, head: str) -> PRStatus:
    active = () if thread.is_outdated else (thread,)
    outdated = (thread,) if thread.is_outdated else ()
    return PRStatus(
        number=7,
        head_sha=head,
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=active,
        unresolved_review_comments=(),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
        outdated_unresolved_inline_threads=outdated,
    )


def _mutation_park(rollback_head: str | None = _ROLLBACK) -> VerdictResult:
    short = (rollback_head or "")[:12] or "<unknown>"
    return VerdictResult(
        verdict="needs_human",
        reason=(
            f"{AGENT_NON_FIXED_WITH_MUTATION}: Correction attempt mutated the worktree then "
            f"reported a non-FIXED verdict. The correction attempt's edits were rolled back "
            f"to {short}; the item is parked for human review. Agent verdict: defer. "
            "Agent reason: out of scope"
        ),
        reason_code=AGENT_NON_FIXED_WITH_MUTATION,
        rollback_head=rollback_head,
    )


def _parked(
    thread: ReviewThread, result: VerdictResult | None = None, *, is_outdated: bool = False
) -> MonitorState:
    """Record the park exactly the way ``_address_thread`` records it."""
    state = MonitorState()
    result = result or _mutation_park()
    _mark_review_thread_addressed(state, thread, result.verdict)
    _sync_needs_human_reason(state, thread.thread_id, result, is_outdated=is_outdated)
    return state


def _decide(status: PRStatus, state: MonitorState) -> Any:
    return decide(status, state, MonitorConfig(auto_merge=True))


# --- re-evaluation ----------------------------------------------------------------


@pytest.mark.unit
def test_head_moved_past_rollback_commit_re_evaluates_the_parked_thread() -> None:
    thread = _thread()
    state = _parked(thread)

    action = _decide(_status(thread, head=_NEW_HEAD), state)

    assert isinstance(action, AddressComments)
    assert [t.thread_id for t in action.threads] == [_THREAD]


@pytest.mark.unit
def test_same_head_keeps_a_genuine_mutation_park_and_its_reason() -> None:
    """Regression: a non-FIXED verdict on the current head still parks for a human."""
    thread = _thread()
    state = _parked(thread)
    status = _status(thread, head=_ROLLBACK)

    action = _decide(status, state)

    assert isinstance(action, NotifyHuman)
    reason = _notify_human_reason(status, state)
    assert reason is not None
    assert reason.startswith(AGENT_NON_FIXED_WITH_MUTATION)


@pytest.mark.unit
def test_thread_that_became_outdated_is_re_evaluated_on_the_same_head() -> None:
    state = _parked(_thread())

    action = _decide(_status(_thread(outdated=True), head=_ROLLBACK), state)

    assert isinstance(action, AddressComments)
    assert [t.thread_id for t in action.threads] == [_THREAD]


@pytest.mark.unit
def test_thread_parked_while_already_outdated_is_not_re_queued_on_the_same_head() -> None:
    thread = _thread(outdated=True)
    state = _parked(thread, is_outdated=True)

    action = _decide(_status(thread, head=_ROLLBACK), state)

    assert isinstance(action, NotifyHuman)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("head", "expected"),
    [(_NEW_HEAD, AddressComments), (_ROLLBACK, NotifyHuman)],
    ids=["head_moved", "same_head"],
)
def test_legacy_park_without_sidecar_falls_back_to_the_reason(head: str, expected: type) -> None:
    """Parks persisted before #1213 (or re-adopted) carry only the prose reason."""
    thread = _thread()
    state = _parked(thread)
    state.threads_addressed_ids.pop(_PARK_KEY)

    assert isinstance(_decide(_status(thread, head=head), state), expected)


@pytest.mark.unit
def test_legacy_park_with_unknown_rollback_stays_parked() -> None:
    thread = _thread()
    state = _parked(thread, _mutation_park(rollback_head=None))
    state.threads_addressed_ids.pop(_PARK_KEY, None)

    assert isinstance(_decide(_status(thread, head=_NEW_HEAD), state), NotifyHuman)


@pytest.mark.unit
def test_genuine_needs_human_that_is_not_a_mutation_park_stays_sticky() -> None:
    thread = _thread()
    state = _parked(thread, VerdictResult(verdict="needs_human", reason="needs a product decision"))

    assert _PARK_KEY not in state.threads_addressed_ids
    assert isinstance(_decide(_status(thread, head=_NEW_HEAD), state), NotifyHuman)


@pytest.mark.unit
def test_re_parked_on_the_new_head_is_not_re_queued_again() -> None:
    """Loop safety: a fresh park on the new head records the new rollback head."""
    thread = _thread()
    state = _parked(thread)
    _sync_needs_human_reason(state, _THREAD, _mutation_park(_NEW_HEAD))

    assert isinstance(_decide(_status(thread, head=_NEW_HEAD), state), NotifyHuman)


# --- sidecar bookkeeping ----------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("is_outdated", [False, True])
def test_sync_writes_the_mutation_park_record(is_outdated: bool) -> None:
    state = _parked(_thread(), is_outdated=is_outdated)

    assert json.loads(state.threads_addressed_ids[_PARK_KEY]) == {
        "rollback_head": _ROLLBACK,
        "outdated": is_outdated,
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    "later",
    [
        VerdictResult(verdict="needs_human", reason="needs a product decision"),
        VerdictResult(verdict="fix_committed", reason="fixed it"),
    ],
    ids=["other_park", "fixed"],
)
def test_any_later_verdict_drops_the_mutation_park_record(later: VerdictResult) -> None:
    state = _parked(_thread())

    _sync_needs_human_reason(state, _THREAD, later)

    assert _PARK_KEY not in state.threads_addressed_ids


@pytest.mark.unit
def test_clear_addressed_state_drops_the_mutation_park_record() -> None:
    state = _parked(_thread())

    _clear_addressed_state_by_id(state, _THREAD)

    assert _PARK_KEY not in state.threads_addressed_ids


@pytest.mark.unit
def test_state_key_builders_share_the_persisted_literals() -> None:
    assert _mutation_park_key(_THREAD) == _PARK_KEY
    assert _needs_human_reason_key(_THREAD) == _REASON_KEY
    assert _needs_human_reason_state_key(_THREAD) == _REASON_KEY


# --- producer ---------------------------------------------------------------------


async def _resolve(monkeypatch: pytest.MonkeyPatch, *, pre_sink_head_unreadable: bool) -> Any:
    async def _rolled_back(*_args: Any, **_kwargs: Any) -> bool:
        return True

    monkeypatch.setattr(comment_verdict, "_rollback_or_classify_failure", _rolled_back)
    return await resolve_correction_non_fixed_mutation(
        object(),  # type: ignore[arg-type]
        workspace_id="ws_x",
        worktree_path=Path("/nonexistent"),
        rollback_floor_head=_ROLLBACK,
        item_start_last_push_sha=None,
        state=None,
        protocol_attempt=1,
        attempt_start_head=_ROLLBACK,
        post_attempt_head=_NEW_HEAD,
        verdict="defer",
        agent_reason="out of scope",
        dirty_changes_committed=False,
        stranded_dirty_residue=False,
        pre_sink_head_unreadable=pre_sink_head_unreadable,
        pre_sink_probe_exc=None,
    )


@pytest.mark.unit
async def test_mutation_park_carries_its_reason_code_and_rollback_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = await _resolve(monkeypatch, pre_sink_head_unreadable=False)

    assert result.verdict == "needs_human"
    assert result.reason_code == AGENT_NON_FIXED_WITH_MUTATION
    assert result.rollback_head == _ROLLBACK


@pytest.mark.unit
async def test_unmeasurable_attempt_park_is_not_a_mutation_park(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = await _resolve(monkeypatch, pre_sink_head_unreadable=True)

    assert result.reason is not None
    assert result.reason.startswith(AGENT_VERDICT_PROTOCOL_VIOLATION)
    assert result.reason_code is None
    assert result.rollback_head is None
