"""Unit tests for helpers in ``pr_monitor_runner``.

These cover the pure, side-effect-free helpers: ``_parse_verdict`` (CLI
reply → structured verdict) and ``_collect_defer_items`` (PRStatus +
MonitorState → bot/human defer buckets for the terminal artifact). The
full runner loop is exercised in
``tests/integration/runtime/test_pr_monitor_runner.py`` — this file
keeps the tight, no-I/O cases alongside the rest of the unit suite so
they run in the fast default lane.
"""

from __future__ import annotations

import pytest

from awf.runtime.pr_monitor import (
    MonitorState,
    ReviewComment,
    ReviewThread,
)
from awf.runtime.pr_monitor_runner import (
    MonitorRunnerConfig,
    _collect_defer_items,
    _parse_verdict,
)
from tests.unit.runtime.test_pr_monitor import _status


class TestParseVerdict:
    @pytest.mark.unit
    def test_empty_stdout_defers(self) -> None:
        assert _parse_verdict("") == "defer"

    @pytest.mark.unit
    def test_false_positive_marker(self) -> None:
        assert _parse_verdict("FALSE POSITIVE: reviewer misread the diff") == "false_positive"

    @pytest.mark.unit
    def test_false_positive_case_insensitive(self) -> None:
        assert _parse_verdict("false positive: minor") == "false_positive"

    @pytest.mark.unit
    def test_defer_marker(self) -> None:
        assert _parse_verdict("DEFER: needs human judgement") == "defer"

    @pytest.mark.unit
    def test_plain_reply_counts_as_fix_committed(self) -> None:
        assert _parse_verdict("Committed fix in abc1234: renamed variable.") == "fix_committed"

    @pytest.mark.unit
    def test_false_positive_takes_precedence_over_defer(self) -> None:
        # Scanner checks FALSE POSITIVE first.
        reply = "FALSE POSITIVE: not a real issue. (not DEFER:)"
        assert _parse_verdict(reply) == "false_positive"


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
            author="coderabbitai[bot]",
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
            author="coderabbitai[bot]",
        )
        state = MonitorState(threads_addressed_ids={"T3": "fix_committed"})
        bots, humans = _collect_defer_items(_status(inline=(t,)), state)
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
        """The runner keeps ``max_outer_iterations`` as a pure safety net
        against decision-loop bugs — a legitimate session exits via a
        terminal action well before this. The cap that WAS removed is
        ``MonitorConfig.iter_cap`` (decision-core gate). Keep these
        distinct so future refactors don't conflate them."""
        cfg = MonitorRunnerConfig()
        assert cfg.max_outer_iterations >= 1000
        assert cfg.max_fix_cycle_passes >= 1
