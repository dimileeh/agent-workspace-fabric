"""PR monitor helper and config shape tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from awf.runtime.pr_monitor import (
    CheckTiming,
    MonitorState,
    ReviewComment,
    ReviewThread,
)
from awf.runtime.pr_monitor_runner import MonitorRunnerConfig
from awf.runtime.pr_monitor_runner.helpers import (
    _as_utc,
    _collect_defer_items,
    _is_pending_check,
    _stale_pending_check_warning_key,
    _stale_pending_check_warnings,
)
from tests.unit.runtime.test_pr_monitor import _status


class TestCollectDeferItems:
    @pytest.mark.unit
    def test_empty_status_yields_empty_buckets(self) -> None:
        bots, humans = _collect_defer_items(_status(), MonitorState())
        assert bots == []
        assert humans == []

    @pytest.mark.unit
    def test_thread_deferred_by_bot_goes_to_bot_bucket(self) -> None:
        t = ReviewThread(
            thread_id="T1",
            path="src/x.py",
            line=1,
            body_excerpt="nit",
            author="reviewer-bot[bot]",
        )
        state = MonitorState(threads_addressed_ids={"T1": "defer"})
        bots, humans = _collect_defer_items(_status(inline=(t,)), state)
        assert len(bots) == 1
        assert bots[0]["id"] == "T1"
        assert bots[0]["kind"] == "thread"
        assert humans == []

    @pytest.mark.unit
    def test_thread_deferred_by_human_goes_to_human_bucket(self) -> None:
        t = ReviewThread(
            thread_id="T2",
            path="src/y.py",
            line=5,
            body_excerpt="real concern",
            author="dimileeh",
        )
        state = MonitorState(threads_addressed_ids={"T2": "defer"})
        bots, humans = _collect_defer_items(_status(inline=(t,)), state)
        assert bots == []
        assert len(humans) == 1
        assert humans[0]["id"] == "T2"

    @pytest.mark.unit
    def test_non_deferred_items_are_excluded(self) -> None:
        t = ReviewThread(
            thread_id="T3",
            path=None,
            line=None,
            body_excerpt="fixed",
            author="reviewer-bot[bot]",
        )
        state = MonitorState(threads_addressed_ids={"T3": "fix_committed"})
        bots, humans = _collect_defer_items(_status(inline=(t,)), state)
        assert bots == []
        assert humans == []

    @pytest.mark.unit
    def test_non_deferred_review_comments_are_excluded(self) -> None:
        c = ReviewComment(
            comment_id="C2",
            body_excerpt="already handled",
            author="dimileeh",
        )

        bots, humans = _collect_defer_items(_status(reviews=(c,)), MonitorState())

        assert bots == []
        assert humans == []

    @pytest.mark.unit
    def test_review_comment_deferred_includes_kind_review(self) -> None:
        c = ReviewComment(
            comment_id="C1",
            body_excerpt="overall concern",
            author="greptile-apps[bot]",
        )
        state = MonitorState(threads_addressed_ids={"C1": "defer"})
        bots, humans = _collect_defer_items(_status(reviews=(c,)), state)
        assert len(bots) == 1
        assert bots[0]["kind"] == "review"
        assert bots[0]["id"] == "C1"
        assert humans == []


class TestRunnerConfigShape:
    @pytest.mark.unit
    def test_runner_config_defaults_include_safety_net(self) -> None:
        cfg = MonitorRunnerConfig()
        assert cfg.max_outer_iterations >= 1000
        assert cfg.max_fix_cycle_passes >= 1


class TestPendingCheckHelpers:
    @pytest.mark.unit
    def test_pending_check_warnings_include_only_old_non_terminal_checks(self) -> None:
        now = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
        old = now - timedelta(minutes=10)
        status = replace(
            _status(),
            checks=(
                CheckTiming(
                    name="ci/build",
                    status="IN_PROGRESS",
                    started_at=old,
                    details_url="https://checks.example/build",
                ),
                CheckTiming(name="ci/no-start", status="PENDING", started_at=None),
                CheckTiming(name="ci/fresh", status="QUEUED", started_at=now),
                CheckTiming(name="ci/done", status="COMPLETED", conclusion=None, started_at=old),
                CheckTiming(name="ci/skipped", status=None, conclusion="SKIPPED", started_at=old),
            ),
        )

        disabled = _stale_pending_check_warnings(
            status,
            now=now,
            threshold_seconds=0,
        )
        warnings = _stale_pending_check_warnings(
            status,
            now=now,
            threshold_seconds=120,
        )

        assert disabled == ()
        assert len(warnings) == 1
        assert warnings[0].payload() == {
            "check_name": "ci/build",
            "age_seconds": 600,
            "head_sha": "abc123",
            "pr_number": 42,
            "threshold_seconds": 120,
            "threshold_window": 5,
            "check_status": "IN_PROGRESS",
            "check_conclusion": None,
            "details_url": "https://checks.example/build",
        }
        assert (
            _stale_pending_check_warning_key(
                workspace_id="ws_1",
                head_sha="abc123",
                check_name="ci/build",
                threshold_seconds=120,
                threshold_window=5,
            )
            == '__awf_pending_check_stale__:["ws_1","abc123","ci/build","120",5]'
        )

    @pytest.mark.unit
    def test_pending_check_classifier_handles_provider_status_edges(self) -> None:
        assert _is_pending_check(CheckTiming(name="unknown", status="waiting")) is True
        assert _is_pending_check(CheckTiming(name="terminal", status="success")) is False
        assert (
            _is_pending_check(CheckTiming(name="terminal-conclusion", conclusion="timed_out"))
            is False
        )
        assert _is_pending_check(CheckTiming(name="future-provider", status="mystery")) is True
        assert _is_pending_check(CheckTiming(name="empty")) is False
        naive = datetime(2026, 4, 27, 12, 0)
        assert _as_utc(naive).tzinfo is UTC
