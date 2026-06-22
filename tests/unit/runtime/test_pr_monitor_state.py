"""Unit tests for ``MonitorState`` merge-block attention marker TTL behavior.

Covers the timestamped marker introduced to distinguish a STILL-blocked
branch-protection fallback (re-stamped every poll, fresh within the TTL) from
a RESOLVED block (no fallback has fired recently, marker age exceeds the TTL)
so ``_clear_stale_merge_attention`` can clear the resolved case without reset
risk to the still-blocked episode timer (#661, #663).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from awf.runtime.pr_monitor import (
    _MERGE_BLOCK_ATTENTION_STATE_KEY,
    MonitorState,
)


def _parse_marker_timestamp(state: MonitorState) -> datetime | None:
    raw = state.threads_addressed_ids.get(_MERGE_BLOCK_ATTENTION_STATE_KEY)
    if raw is None or raw == "1":
        return None
    return datetime.fromisoformat(raw)


@pytest.mark.unit
def test_mark_merge_block_attention_stamps_wall_clock_timestamp() -> None:
    """``mark_merge_block_attention`` stores an ISO-8601 wall-clock timestamp."""
    state = MonitorState()
    now = datetime(2026, 6, 22, 12, 0, tzinfo=UTC)

    state.mark_merge_block_attention(now=now)

    raw = state.threads_addressed_ids[_MERGE_BLOCK_ATTENTION_STATE_KEY]
    assert raw != "1"
    parsed = datetime.fromisoformat(raw)
    assert parsed == now


@pytest.mark.unit
def test_merge_block_attention_active_true_within_ttl() -> None:
    """A fresh marker (age <= TTL) reports active — still-blocked, preserved."""
    state = MonitorState()
    now = datetime(2026, 6, 22, 12, 0, tzinfo=UTC)
    state.mark_merge_block_attention(now=now)

    # Same instant: age 0, well within a 120s TTL.
    assert state.merge_block_attention_active(now=now, ttl_seconds=120.0) is True
    # The marker is preserved (not dropped) when fresh.
    assert _parse_marker_timestamp(state) == now


@pytest.mark.unit
def test_merge_block_attention_active_false_after_ttl_drops_marker() -> None:
    """A stale marker (age > TTL) reports inactive (RESOLVED) and is dropped."""
    state = MonitorState()
    stamped = datetime(2026, 6, 22, 12, 0, tzinfo=UTC)
    state.mark_merge_block_attention(now=stamped)

    # 300s later with a 120s TTL: age 300 > TTL 120 → resolved.
    later = stamped + timedelta(seconds=300)
    assert state.merge_block_attention_active(now=later, ttl_seconds=120.0) is False
    # The stale marker is dropped so the next fresh poll re-stamps cleanly.
    assert _MERGE_BLOCK_ATTENTION_STATE_KEY not in state.threads_addressed_ids


@pytest.mark.unit
def test_merge_block_attention_active_without_ttl_preserves_legacy_behavior() -> None:
    """When no TTL is configured the marker is active whenever present.

    Preserves the pre-TTL contract used by callers that have not opted into
    the resolved-vs-still-blocked distinction.
    """
    state = MonitorState()
    now = datetime(2026, 6, 22, 12, 0, tzinfo=UTC)
    state.mark_merge_block_attention(now=now)

    # ttl_seconds=None disables the TTL gate: present ⇒ active, regardless of age.
    far_future = now + timedelta(hours=24)
    assert state.merge_block_attention_active(now=far_future) is True
    assert _parse_marker_timestamp(state) == now


@pytest.mark.unit
def test_merge_block_attention_active_legacy_boolean_marker_treated_as_fresh() -> None:
    """A legacy boolean ``"1"`` marker (pre-TTL persisted state) is treated as
    fresh on first read so an in-flight monitor is not cleared on age alone.

    The next ``mark_merge_block_attention`` re-stamps it to a timestamp.
    """
    state = MonitorState(threads_addressed_ids={_MERGE_BLOCK_ATTENTION_STATE_KEY: "1"})

    # Legacy marker: unknown age ⇒ preserved (fresh), regardless of TTL.
    now = datetime(2026, 6, 22, 12, 0, tzinfo=UTC)
    assert state.merge_block_attention_active(now=now, ttl_seconds=1.0) is True
    # The legacy value is left in place until a re-stamp.
    assert state.threads_addressed_ids[_MERGE_BLOCK_ATTENTION_STATE_KEY] == "1"

    # Re-stamp upgrades the legacy value to a timestamp.
    state.mark_merge_block_attention(now=now)
    assert state.threads_addressed_ids[_MERGE_BLOCK_ATTENTION_STATE_KEY] != "1"
    assert _parse_marker_timestamp(state) == now


@pytest.mark.unit
def test_clear_merge_block_attention_drops_marker_idempotently() -> None:
    """``clear_merge_block_attention`` is an idempotent pop (unchanged behavior)."""
    state = MonitorState()
    now = datetime(2026, 6, 22, 12, 0, tzinfo=UTC)
    state.mark_merge_block_attention(now=now)
    assert _MERGE_BLOCK_ATTENTION_STATE_KEY in state.threads_addressed_ids

    state.clear_merge_block_attention()
    assert _MERGE_BLOCK_ATTENTION_STATE_KEY not in state.threads_addressed_ids

    # Second clear is a no-op.
    state.clear_merge_block_attention()
    assert _MERGE_BLOCK_ATTENTION_STATE_KEY not in state.threads_addressed_ids
