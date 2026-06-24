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
def test_mark_merge_block_attention_records_merge_rejection_origin() -> None:
    """A structured marker, not the human-facing reason text, records origin."""
    state = MonitorState()
    now = datetime(2026, 6, 22, 12, 0, tzinfo=UTC)

    state.mark_merge_block_attention(now=now, originated_from_merge_rejection=True)

    assert state.merge_block_attention_originated_from_merge_rejection() is True


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
    state.mark_merge_block_attention(now=stamped, originated_from_merge_rejection=True)

    # 300s later with a 120s TTL: age 300 > TTL 120 → resolved.
    later = stamped + timedelta(seconds=300)
    assert state.merge_block_attention_active(now=later, ttl_seconds=120.0) is False
    # The stale marker is dropped so the next fresh poll re-stamps cleanly.
    assert _MERGE_BLOCK_ATTENTION_STATE_KEY not in state.threads_addressed_ids
    assert state.merge_block_attention_originated_from_merge_rejection() is False


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

    The first read *re-stamps* the legacy value to a timestamp (now) so the
    marker becomes age-trackable. If the branch-protection block later
    resolves and no fallback fires to re-stamp via ``mark_merge_block_attention``,
    the TTL can still age the marker out and ``_clear_stale_merge_attention``
    can drop the stale ``awaiting_human_since`` (PRRT_kwDOSJAM6s6LapQB).
    """
    state = MonitorState(threads_addressed_ids={_MERGE_BLOCK_ATTENTION_STATE_KEY: "1"})

    # Legacy marker: unknown age ⇒ preserved (fresh), regardless of TTL.
    now = datetime(2026, 6, 22, 12, 0, tzinfo=UTC)
    assert state.merge_block_attention_active(now=now, ttl_seconds=1.0) is True
    # The legacy value is re-stamped to a timestamp on first read so it becomes
    # age-trackable for later TTL expiry.
    assert state.threads_addressed_ids[_MERGE_BLOCK_ATTENTION_STATE_KEY] != "1"
    assert _parse_marker_timestamp(state) == now

    # An explicit re-stamp upgrades the value to a timestamp (unchanged contract).
    state.mark_merge_block_attention(now=now)
    assert state.threads_addressed_ids[_MERGE_BLOCK_ATTENTION_STATE_KEY] != "1"
    assert _parse_marker_timestamp(state) == now


@pytest.mark.unit
def test_merge_block_attention_active_legacy_marker_expires_after_ttl_once_resolved() -> None:
    """A legacy ``"1"`` marker that is never re-stamped by a still-blocked
    fallback still ages out via the TTL once the block resolves, because the
    first read re-stamped it to a trackable timestamp (PRRT_kwDOSJAM6s6LapQB).

    Regression for the pre-fix bug where the legacy ``"1"`` branch returned
    ``True`` on every call and never re-stamped, so a resolved block kept
    ``awaiting_human_since`` surfaced forever while only non-human gates
    remained.
    """
    state = MonitorState(threads_addressed_ids={_MERGE_BLOCK_ATTENTION_STATE_KEY: "1"})

    # First poll: legacy marker re-stamped to "now" (12:00), preserved as fresh.
    first_poll = datetime(2026, 6, 22, 12, 0, tzinfo=UTC)
    assert state.merge_block_attention_active(now=first_poll, ttl_seconds=120.0) is True
    assert _parse_marker_timestamp(state) == first_poll

    # Block resolves; no fallback fires to re-stamp. 300s later (>120s TTL) the
    # now-timestamped marker is stale and dropped, so the clear proceeds.
    later = first_poll + timedelta(seconds=300)
    assert state.merge_block_attention_active(now=later, ttl_seconds=120.0) is False
    assert _MERGE_BLOCK_ATTENTION_STATE_KEY not in state.threads_addressed_ids


@pytest.mark.unit
def test_clear_merge_block_attention_drops_marker_idempotently() -> None:
    """``clear_merge_block_attention`` is an idempotent pop (unchanged behavior)."""
    state = MonitorState()
    now = datetime(2026, 6, 22, 12, 0, tzinfo=UTC)
    state.mark_merge_block_attention(now=now, originated_from_merge_rejection=True)
    assert _MERGE_BLOCK_ATTENTION_STATE_KEY in state.threads_addressed_ids
    assert state.merge_block_attention_originated_from_merge_rejection() is True

    state.clear_merge_block_attention()
    assert _MERGE_BLOCK_ATTENTION_STATE_KEY not in state.threads_addressed_ids
    assert state.merge_block_attention_originated_from_merge_rejection() is False

    # Second clear is a no-op.
    state.clear_merge_block_attention()
    assert _MERGE_BLOCK_ATTENTION_STATE_KEY not in state.threads_addressed_ids
    assert state.merge_block_attention_originated_from_merge_rejection() is False


@pytest.mark.unit
def test_merge_block_attention_active_unrecognized_marker_shape_preserved_as_fresh() -> None:
    """An unrecognized marker shape (not ``"1"``, not an ISO timestamp) is treated
    as fresh rather than silently clearing an in-flight block. The next
    branch-protection fallback re-stamps it to a known form via
    ``mark_merge_block_attention``. Covers the ``except ValueError`` arm.
    """
    state = MonitorState(
        threads_addressed_ids={_MERGE_BLOCK_ATTENTION_STATE_KEY: "not-a-timestamp"}
    )
    now = datetime(2026, 6, 22, 12, 0, tzinfo=UTC)
    # Unrecognized shape ⇒ preserved (fresh) regardless of TTL; the value is
    # left untouched (the next fallback re-stamps it).
    assert state.merge_block_attention_active(now=now, ttl_seconds=1.0) is True
    assert state.threads_addressed_ids[_MERGE_BLOCK_ATTENTION_STATE_KEY] == "not-a-timestamp"


@pytest.mark.unit
def test_merge_block_attention_active_naive_timestamp_assumed_utc() -> None:
    """A naive (tzinfo-less) persisted timestamp is assumed UTC before measuring
    its age against the caller's aware ``now``. Covers the ``tzinfo is None`` arm.

    A naive marker could only arise from a legacy persisted value lacking tz
    (``mark_merge_block_attention`` always stamps with an aware UTC datetime); the
    age comparison must normalize it to UTC rather than raising on the
    aware-minus-naive subtraction.
    """
    naive_stamp = datetime(2026, 6, 22, 12, 0)
    state = MonitorState(
        threads_addressed_ids={_MERGE_BLOCK_ATTENTION_STATE_KEY: naive_stamp.isoformat()}
    )
    # 60s later, aware ``now`` in UTC — the naive stamp is treated as 12:00 UTC,
    # age 60s <= 120s TTL ⇒ fresh (preserve).
    now = datetime(2026, 6, 22, 12, 1, tzinfo=UTC)
    assert state.merge_block_attention_active(now=now, ttl_seconds=120.0) is True
    # 300s later (>120s TTL) ⇒ stale, marker dropped.
    later = datetime(2026, 6, 22, 12, 5, tzinfo=UTC)
    assert state.merge_block_attention_active(now=later, ttl_seconds=120.0) is False
    assert _MERGE_BLOCK_ATTENTION_STATE_KEY not in state.threads_addressed_ids
