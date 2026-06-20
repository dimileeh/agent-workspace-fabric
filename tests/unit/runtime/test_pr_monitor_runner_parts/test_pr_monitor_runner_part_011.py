"""Notification and initial-review-grace tests for ``pr_monitor_runner`` helpers."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from awf.runtime.pr_monitor import (
    MergeStateStatus,
    MonitorState,
    ReviewComment,
)
from awf.runtime.pr_monitor_runner.helpers import (
    _initial_review_grace_done_key,
    _initial_review_grace_started_key,
    _initial_review_grace_state_for_persistence,
    _initial_review_grace_state_for_runtime,
    _initial_review_grace_wait_seconds,
    _initial_review_grace_wall_seconds,
    _initial_review_grace_wall_started_value_from_datetime,
    _notify_human_reason,
)
from tests.unit.runtime.test_pr_monitor import _status


class TestNotificationAndGraceHelpers:
    @pytest.mark.unit
    def test_notify_human_reason_prioritizes_blocking_conditions(self) -> None:
        blocking_review = ReviewComment(
            comment_id="C-block",
            body_excerpt="external policy gate",
            author="review-bot",
            blocks_merge=True,
        )
        deferred_review = ReviewComment(
            comment_id="C-human",
            body_excerpt="please inspect",
            author="human",
        )
        deferred_state = MonitorState(threads_addressed_ids={"C-human": "defer"})

        assert "merge-blocking changes-requested review" in (
            _notify_human_reason(_status(reviews=(blocking_review,)), MonitorState()) or ""
        )
        assert "required protection" in (
            _notify_human_reason(
                _status(merge_state_status=MergeStateStatus.BLOCKED),
                MonitorState(),
            )
            or ""
        )
        assert (
            _notify_human_reason(
                _status(
                    reviews=(deferred_review,),
                    merge_state_status=MergeStateStatus.BLOCKED,
                ),
                deferred_state,
            )
            == "human review feedback was deferred by the agent and remains unresolved"
        )
        assert (
            _notify_human_reason(
                _status(reviews=(deferred_review,)),
                deferred_state,
            )
            == "human review feedback was deferred by the agent and remains unresolved"
        )
        assert _notify_human_reason(_status(), MonitorState()) is None

    @pytest.mark.unit
    def test_initial_review_grace_state_converts_between_wall_and_monotonic_time(self) -> None:
        pr_number = 42
        started_key = _initial_review_grace_started_key(pr_number)
        done_key = _initial_review_grace_done_key(pr_number)
        wall_started = datetime(2026, 4, 27, 12, 0, tzinfo=UTC).timestamp()
        runtime_state = {started_key: f"{wall_started:.6f}"}
        persisted_state = {started_key: "900.000000"}

        assert _initial_review_grace_wall_seconds(object()) is None
        assert _initial_review_grace_wall_seconds("not-a-number") is None
        assert _initial_review_grace_wall_seconds("123.0") is None
        assert _initial_review_grace_wall_seconds(wall_started) == wall_started
        assert (
            _initial_review_grace_wall_started_value_from_datetime(
                datetime(2026, 4, 27, 12, 0),
            )
            == f"{wall_started:.6f}"
        )

        converted_runtime = _initial_review_grace_state_for_runtime(
            runtime_state,
            pr_number=pr_number,
            now_monotonic=1000.0,
            now_wall_seconds=wall_started + 30.0,
        )
        legacy_runtime = _initial_review_grace_state_for_runtime(
            {started_key: "900.0"},
            pr_number=pr_number,
            now_monotonic=1000.0,
            now_wall_seconds=wall_started,
            legacy_monotonic_fallback=875.0,
        )
        converted_persistence = _initial_review_grace_state_for_persistence(
            persisted_state,
            pr_number=pr_number,
            now_monotonic=1000.0,
            now_wall_seconds=wall_started + 100.0,
        )
        invalid_persistence = _initial_review_grace_state_for_persistence(
            {started_key: "invalid"},
            pr_number=pr_number,
            now_monotonic=1000.0,
            now_wall_seconds=wall_started,
        )
        unchanged_persistence = _initial_review_grace_state_for_persistence(
            {},
            pr_number=pr_number,
            now_monotonic=1000.0,
            now_wall_seconds=wall_started,
        )

        assert converted_runtime[started_key] == "970.000000"
        assert legacy_runtime[started_key] == "875.000000"
        assert converted_persistence[started_key] == f"{wall_started:.6f}"
        assert invalid_persistence[started_key] == "invalid"
        assert unchanged_persistence == {}

        waiting = MonitorState(started_at=10.0)
        assert (
            _initial_review_grace_wait_seconds(
                waiting,
                pr_number=pr_number,
                now=12.0,
                grace_seconds=10.0,
                poll_interval_seconds=3.0,
            )
            == 3.0
        )
        assert waiting.threads_addressed_ids[started_key] == "10.000000"

        invalid_started = MonitorState(
            started_at=20.0,
            threads_addressed_ids={started_key: "not-float"},
        )
        assert (
            _initial_review_grace_wait_seconds(
                invalid_started,
                pr_number=pr_number,
                now=35.0,
                grace_seconds=10.0,
                poll_interval_seconds=5.0,
            )
            == 0.0
        )
        assert invalid_started.threads_addressed_ids[started_key] == "20.000000"
        assert invalid_started.threads_addressed_ids[done_key] == "elapsed"
