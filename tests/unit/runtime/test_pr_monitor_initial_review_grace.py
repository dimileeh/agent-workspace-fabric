"""Tests for the PR monitor's one-time initial review grace window."""

from __future__ import annotations

import pytest

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
    decide,
)
from awf.runtime.pr_monitor_runner.helpers import (
    _initial_review_grace_done_key,
    _initial_review_grace_started_key,
    _initial_review_grace_wait_seconds,
)


def _status(*, inline: tuple[ReviewThread, ...] = ()) -> PRStatus:
    return PRStatus(
        number=42,
        head_sha="abc123",
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=inline,
        unresolved_review_comments=(),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
    )


@pytest.mark.unit
def test_default_initial_review_grace_is_fifteen_minutes() -> None:
    assert MonitorConfig().initial_review_grace_period_seconds == 900


@pytest.mark.unit
def test_zero_initial_review_grace_disables_wait() -> None:
    state = MonitorState()
    wait = _initial_review_grace_wait_seconds(
        state,
        pr_number=42,
        now=1_000,
        grace_seconds=0,
        poll_interval_seconds=60,
    )
    assert wait == 0
    assert state.threads_addressed_ids == {}


@pytest.mark.unit
def test_auto_merge_waits_while_initial_grace_is_active() -> None:
    state = MonitorState(started_at=1_000)
    wait = _initial_review_grace_wait_seconds(
        state,
        pr_number=42,
        now=1_000,
        grace_seconds=900,
        poll_interval_seconds=60,
    )
    assert wait == 60
    assert state.threads_addressed_ids[_initial_review_grace_started_key(42)] == "1000.000000"


@pytest.mark.unit
def test_initial_grace_uses_remaining_time_not_a_fresh_window() -> None:
    state = MonitorState(started_at=1_000)
    wait = _initial_review_grace_wait_seconds(
        state,
        pr_number=42,
        now=1_500,
        grace_seconds=900,
        poll_interval_seconds=600,
    )
    assert wait == 400


@pytest.mark.unit
def test_existing_pr_scoped_started_key_survives_remonitor_reset() -> None:
    state = MonitorState(started_at=1_500)
    state.mark_addressed(_initial_review_grace_started_key(42), "1000.000000")

    wait = _initial_review_grace_wait_seconds(
        state,
        pr_number=42,
        now=1_500,
        grace_seconds=900,
        poll_interval_seconds=600,
    )

    assert wait == 400
    assert state.threads_addressed_ids[_initial_review_grace_started_key(42)] == "1000.000000"


@pytest.mark.unit
def test_existing_pr_scoped_done_key_survives_remonitor_reset() -> None:
    state = MonitorState(started_at=1_500)
    state.mark_addressed(_initial_review_grace_done_key(42), "elapsed")

    wait = _initial_review_grace_wait_seconds(
        state,
        pr_number=42,
        now=1_500,
        grace_seconds=900,
        poll_interval_seconds=60,
    )

    assert wait == 0
    assert _initial_review_grace_started_key(42) not in state.threads_addressed_ids


@pytest.mark.unit
def test_grace_does_not_restart_after_it_elapsed_once() -> None:
    state = MonitorState(started_at=1_000)
    first = _initial_review_grace_wait_seconds(
        state,
        pr_number=42,
        now=1_901,
        grace_seconds=900,
        poll_interval_seconds=60,
    )
    second = _initial_review_grace_wait_seconds(
        state,
        pr_number=42,
        now=2_000,
        grace_seconds=900,
        poll_interval_seconds=60,
    )
    assert first == 0
    assert second == 0
    assert state.threads_addressed_ids[_initial_review_grace_done_key(42)] == "elapsed"


@pytest.mark.unit
def test_comments_arriving_during_grace_route_to_address_comments() -> None:
    state = MonitorState()
    state.mark_addressed(_initial_review_grace_started_key(42), "1000")
    action = decide(
        _status(
            inline=(
                ReviewThread(
                    thread_id="T1",
                    path="src/x.py",
                    line=10,
                    body_excerpt="please fix",
                ),
            )
        ),
        state,
        MonitorConfig(auto_merge=True),
    )
    assert isinstance(action, AddressComments)


@pytest.mark.unit
def test_manual_merge_mode_still_notifies_human_instead_of_grace_merging() -> None:
    action = decide(_status(), MonitorState(), MonitorConfig(auto_merge=False))
    assert isinstance(action, NotifyHuman)
