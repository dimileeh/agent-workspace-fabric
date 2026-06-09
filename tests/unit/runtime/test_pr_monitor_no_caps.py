"""Regression tests: the PR monitor must NOT abort on iteration or
wall-clock budget exhaustion.

Earlier versions of ``MonitorConfig`` carried ``iter_cap=10`` and
``wall_clock_cap_seconds=6*3600``. Both fired terminal ``Abort`` actions
that stranded otherwise-green PRs whenever a review attracted heavy bot
traffic — every review cycle consumed one iteration, and 5 bot reviewers
on a single PR would exhaust the 10-iteration budget before the PR could
land.

Policy: the monitor takes FULL responsibility for driving a PR until it
is merged or closed. Volume is not a terminal condition; branch-protection,
human-defer, and explicit GitHub refusal only produce live ``NotifyHuman``
wait states. So both fields are deleted from ``MonitorConfig`` and both
branches are removed from ``decide()``. These tests lock that in.
"""

from __future__ import annotations

import time

from awf.runtime.pr_monitor import (
    Abort,
    Merge,
    MonitorConfig,
    MonitorState,
    NotifyHuman,
    decide,
)
from tests.unit.runtime.test_pr_monitor import _status, _thread


class TestNoIterCapAbort:
    def test_decide_does_not_abort_on_high_iter_count(self) -> None:
        """A PR at iter_count=1000 with otherwise-green gates must still
        return Merge (auto_merge) — not Abort on iter_cap."""
        state = MonitorState(iter_count=1000)
        action = decide(_status(), state, MonitorConfig(auto_merge=True))
        assert not isinstance(action, Abort)
        assert isinstance(action, Merge)

    def test_decide_does_not_abort_on_high_iter_count_release_variant(self) -> None:
        """Same behaviour on the release-PR (auto_merge=False) variant —
        NotifyHuman, not Abort."""
        state = MonitorState(iter_count=1000)
        action = decide(_status(), state, MonitorConfig(auto_merge=False))
        assert not isinstance(action, Abort)
        assert isinstance(action, NotifyHuman)

    def test_high_iter_count_with_unresolved_comments_still_addresses(self) -> None:
        """Even at iter_count=1000 a fresh comment routes to
        AddressComments, not Abort. The monitor keeps servicing the PR
        regardless of how long it's been iterating."""
        state = MonitorState(iter_count=1000)
        action = decide(_status(inline=(_thread("T_fresh"),)), state, MonitorConfig())
        assert not isinstance(action, Abort)


class TestNoWallClockAbort:
    def test_decide_does_not_abort_on_long_wall_clock(self) -> None:
        """A monitor that started 48h ago must still drive the PR —
        legitimate PRs can take days to sort out."""
        state = MonitorState(started_at=time.monotonic() - 48 * 3600)
        action = decide(_status(), state, MonitorConfig(auto_merge=True))
        assert not isinstance(action, Abort)
        assert isinstance(action, Merge)

    def test_wall_clock_does_not_abort_release_variant(self) -> None:
        state = MonitorState(started_at=time.monotonic() - 48 * 3600)
        action = decide(_status(), state, MonitorConfig(auto_merge=False))
        assert not isinstance(action, Abort)
        assert isinstance(action, NotifyHuman)


class TestMonitorConfigShape:
    def test_monitor_config_has_no_iter_cap_field(self) -> None:
        """Deleting the field — not raising the default — is the point:
        a huge default would just defer the same failure mode."""
        cfg = MonitorConfig()
        assert not hasattr(cfg, "iter_cap")

    def test_monitor_config_has_no_wall_clock_cap_field(self) -> None:
        cfg = MonitorConfig()
        assert not hasattr(cfg, "wall_clock_cap_seconds")

    def test_monitor_config_construction_without_cap_kwargs(self) -> None:
        """Construction must succeed without passing iter_cap /
        wall_clock_cap_seconds — they no longer exist as parameters."""
        cfg = MonitorConfig(auto_merge=True, poll_interval_seconds=30.0)
        assert cfg.auto_merge is True
        assert cfg.poll_interval_seconds == 30.0
