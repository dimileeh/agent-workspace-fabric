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


class TestMergeBlockAttentionTtlCoupling:
    """The merge-block attention TTL must track ``poll_interval_seconds`` so a
    marker re-stamped at the end of poll N stays fresh through poll N+1.

    A fixed TTL default (the legacy ``120.0``) silently broke when an operator
    raised ``poll_interval_seconds`` above it: the branch-protection fallback's
    fresh marker at end of poll N had already aged past the TTL by poll N+1, so
    ``_clear_stale_merge_attention`` treated the still-active block as resolved
    and cleared the awaiting-human signal — the exact #663 regression
    (PRRT_kwDOSJAM6s6LaEpY)."""

    def test_default_ttl_couples_to_default_poll_interval(self) -> None:
        cfg = MonitorConfig()
        assert cfg.poll_interval_seconds == 60.0
        # 2× the default poll interval = the historical 120 s default.
        assert cfg.merge_block_attention_ttl_seconds == 120.0

    def test_ttl_scales_with_poll_interval_when_not_set(self) -> None:
        cfg = MonitorConfig(poll_interval_seconds=300.0)
        # A 5-minute poll must carry a TTL above 5 minutes so the marker
        # survives to the next poll instead of being read as resolved.
        assert cfg.merge_block_attention_ttl_seconds == 600.0

    def test_explicit_ttl_is_honored_unchanged(self) -> None:
        cfg = MonitorConfig(
            poll_interval_seconds=300.0,
            merge_block_attention_ttl_seconds=60.0,
        )
        assert cfg.merge_block_attention_ttl_seconds == 60.0

    def test_ttl_above_poll_interval_when_default_poll_scaled(self) -> None:
        cfg = MonitorConfig(poll_interval_seconds=90.0)
        assert cfg.merge_block_attention_ttl_seconds > cfg.poll_interval_seconds
