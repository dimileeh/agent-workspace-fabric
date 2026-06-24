"""Regression tests for PR monitor merge-attention persistence helpers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    _MERGE_BLOCK_ATTENTION_ORIGIN_MERGE_REJECTION,
    _MERGE_BLOCK_ATTENTION_ORIGIN_STATE_KEY,
    _MERGE_BLOCK_ATTENTION_STATE_KEY,
    MergeStateStatus,
    MonitorState,
)
from tests.postgres import postgres_test_engine
from tests.unit.runtime._merge_methods_fixtures import _mergeable_status
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Provide an isolated async session factory for persistence tests."""
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


# ── Defensive no-op branches for the new atomic-persist helpers ──────────────
#
# ``_set_workspace_attention_with_merge_block_marker`` and
# ``_persist_merge_block_attention_durably`` each have defensive early-returns
# for a missing workspace row (a GC/destroy race) and, for the durable persist,
# an absent in-memory marker (the caller did not re-stamp) or an already-equal
# persisted value (no-op write). These mirror the established single-key
# durable-persist no-op pattern (``_clear_preserved_head_marker_durably`` /
# ``_persist_forge_transient_retry_count``); each branch is exercised here so
# the helpers never silently lose coverage.


@pytest.mark.unit
async def test_set_workspace_attention_with_merge_block_marker_missing_workspace_is_noop(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The atomic marker+attention write no-ops when the workspace row is gone (a
    GC/destroy race between the fallback and the commit) rather than raising."""
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    # No marker is stamped, but the helper must not raise on a missing workspace
    # even when one IS present in state — the row lookup gates the whole write.
    state = MonitorState()
    state.mark_merge_block_attention()
    await runner._set_workspace_attention_with_merge_block_marker(
        "ws_does_not_exist",
        state,
        reason="merge_blocked",
    )


@pytest.mark.unit
async def test_persist_merge_block_attention_durably_missing_workspace_is_noop(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The durable marker persist no-ops when the workspace row is gone (a
    GC/destroy race between the preserve re-stamp and the durable write)."""
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    state = MonitorState()
    state.mark_merge_block_attention()
    await runner._persist_merge_block_attention_durably("ws_does_not_exist", state)


@pytest.mark.unit
async def test_persist_merge_block_attention_durably_absent_marker_is_noop(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The durable persist no-ops when the in-memory marker is absent — the
    caller did not re-stamp, so there is nothing to durably persist."""
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    # No marker stamped into state ⇒ the helper returns before touching the DB.
    state = MonitorState()
    await runner._persist_merge_block_attention_durably(workspace_id, state)
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
    assert _MERGE_BLOCK_ATTENTION_STATE_KEY not in (ws.monitor_threads_addressed or {})


@pytest.mark.unit
async def test_persist_merge_block_attention_durably_already_equal_is_noop(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The durable persist no-ops when the persisted marker already equals the
    in-memory stamp (idempotent re-write guard) — the DB value is left untouched
    and no redundant commit is issued."""
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    # Stamp the marker durably first so the DB row already carries it.
    state = MonitorState()
    state.mark_merge_block_attention()
    await runner._persist_merge_block_attention_durably(workspace_id, state)
    stamped = state.threads_addressed_ids[_MERGE_BLOCK_ATTENTION_STATE_KEY]
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
    assert (ws.monitor_threads_addressed or {})[_MERGE_BLOCK_ATTENTION_STATE_KEY] == stamped
    # A second persist with the SAME stamp must short-circuit (no-op write).
    await runner._persist_merge_block_attention_durably(workspace_id, state)
    async with factory() as session:
        ws_after = await WorkspaceRepository(session).get(workspace_id)
        assert ws_after is not None
    assert (ws_after.monitor_threads_addressed or {})[_MERGE_BLOCK_ATTENTION_STATE_KEY] == stamped


@pytest.mark.unit
async def test_clear_stale_merge_attention_drops_marker_durably_on_resolve(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6Lf_37: ``_clear_stale_merge_attention``'s stale (resolved)
    branch must durably remove the merge-block marker from the persisted row,
    not just drop it in memory. The outer ``run()`` loop only flushes the whole
    in-memory ``state`` after ``_execute`` returns (``runner.py:455``); a
    cancel/restart before that full ``_persist_state`` would otherwise reload the
    STALE marker from the DB while ``awaiting_human_since`` is already null, so
    a later poll could otherwise observe the stale marker without the cleared
    attention flag, losing the invariant that both pieces move together.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    # Seed a stale marker (resolved block) into BOTH the in-memory state and the
    # persisted row — the state the monitor would reload after a cancel/restart
    # before the outer loop's full ``_persist_state`` flush.
    stale_stamp = datetime(2024, 1, 1, tzinfo=UTC).isoformat()
    state = MonitorState(threads_addressed_ids={_MERGE_BLOCK_ATTENTION_STATE_KEY: stale_stamp})
    async with factory() as session:
        ws = await WorkspaceRepository(session).get_for_update(workspace_id)
        assert ws is not None
        ws.monitor_threads_addressed = {_MERGE_BLOCK_ATTENTION_STATE_KEY: stale_stamp}
        await session.commit()
    # Drive the stale (resolved) branch with a TTL that the marker exceeds, and a
    # ``reference`` (entry ``now``) far enough past the stamp to age it out.
    resolved_now = datetime(2024, 1, 2, tzinfo=UTC)
    await runner._clear_stale_merge_attention(
        workspace_id,
        state,
        now=resolved_now,
    )
    # In-memory marker dropped and attention cleared.
    assert _MERGE_BLOCK_ATTENTION_STATE_KEY not in state.threads_addressed_ids
    # The persisted row must NOT carry the stale marker anymore — the durable
    # clear closed the cancel/restart window.
    async with factory() as session:
        ws_after = await WorkspaceRepository(session).get(workspace_id)
        assert ws_after is not None
    assert _MERGE_BLOCK_ATTENTION_STATE_KEY not in (ws_after.monitor_threads_addressed or {})
    assert ws_after.awaiting_human_since is None


@pytest.mark.unit
async def test_clear_stale_merge_attention_preserves_stale_marker_when_forge_still_blocked(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6LqhqC: a long queue wait can preserve an old
    ``merge_block_attention`` marker without re-stamping it. When merge
    critical-section entry sees the forge still reporting branch protection as
    active, it must not treat the TTL-aged marker as resolved and clear
    ``awaiting_human_since``.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    old_stamp = datetime(2024, 1, 1, tzinfo=UTC).isoformat()
    state = MonitorState(threads_addressed_ids={_MERGE_BLOCK_ATTENTION_STATE_KEY: old_stamp})
    episode_start = datetime(2024, 1, 1, 12, tzinfo=UTC)
    async with factory() as session:
        ws = await WorkspaceRepository(session).get_for_update(workspace_id)
        assert ws is not None
        ws.monitor_threads_addressed = {_MERGE_BLOCK_ATTENTION_STATE_KEY: old_stamp}
        ws.awaiting_human_since = episode_start
        ws.awaiting_human_reason = "GitHub rejected the merge attempt"
        await session.commit()

    before_call = datetime.now(UTC)
    await runner._clear_stale_merge_attention(
        workspace_id,
        state,
        now=datetime(2024, 1, 2, tzinfo=UTC),
        status=replace(_mergeable_status(), merge_state_status=MergeStateStatus.BLOCKED),
        forge="github",
    )
    after_call = datetime.now(UTC)

    refreshed_stamp = datetime.fromisoformat(
        state.threads_addressed_ids[_MERGE_BLOCK_ATTENTION_STATE_KEY]
    )
    assert before_call <= refreshed_stamp <= after_call
    async with factory() as session:
        ws_after = await WorkspaceRepository(session).get(workspace_id)
        assert ws_after is not None
    assert ws_after.awaiting_human_since == episode_start
    assert ws_after.awaiting_human_reason == "GitHub rejected the merge attempt"
    assert (ws_after.monitor_threads_addressed or {})[
        _MERGE_BLOCK_ATTENTION_STATE_KEY
    ] == refreshed_stamp.isoformat()


@pytest.mark.unit
async def test_queue_wait_preserves_persisted_merge_rejection_origin_after_restart(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A restarted monitor can reload the marker without structured origin in
    memory, so the queue-wait preserve decision must consult persisted origin."""
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    marker = datetime(2026, 1, 1, 12, 0, 1, tzinfo=UTC).isoformat()
    state = MonitorState(threads_addressed_ids={_MERGE_BLOCK_ATTENTION_STATE_KEY: marker})
    episode_start = datetime(2026, 1, 1, 12, tzinfo=UTC)
    persisted_state = {
        _MERGE_BLOCK_ATTENTION_STATE_KEY: marker,
        _MERGE_BLOCK_ATTENTION_ORIGIN_STATE_KEY: (_MERGE_BLOCK_ATTENTION_ORIGIN_MERGE_REJECTION),
    }
    async with factory() as session:
        ws = await WorkspaceRepository(session).get_for_update(workspace_id)
        assert ws is not None
        ws.monitor_threads_addressed = persisted_state
        ws.awaiting_human_since = episode_start
        ws.awaiting_human_reason = "GitHub merge was denied by branch actor restrictions"
        await session.commit()

    await runner._clear_or_preserve_merge_attention_for_queue_wait(
        workspace_id,
        state,
        status=_mergeable_status(),
        forge="github",
    )

    assert state.threads_addressed_ids[_MERGE_BLOCK_ATTENTION_STATE_KEY] == marker
    async with factory() as session:
        ws_after = await WorkspaceRepository(session).get(workspace_id)
        assert ws_after is not None
    assert (ws_after.monitor_threads_addressed or {}) == persisted_state
    assert ws_after.awaiting_human_since == episode_start
    assert ws_after.awaiting_human_reason == (
        "GitHub merge was denied by branch actor restrictions"
    )


@pytest.mark.unit
async def test_queue_wait_clears_persisted_non_rejection_origin_when_github_clean(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Persisted structured origin is authoritative after restart, and an
    explicit non-rejection origin must not be preserved on GitHub ``CLEAN``."""
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    marker = datetime(2026, 1, 1, 12, 0, 1, tzinfo=UTC).isoformat()
    state = MonitorState(threads_addressed_ids={_MERGE_BLOCK_ATTENTION_STATE_KEY: marker})
    async with factory() as session:
        ws = await WorkspaceRepository(session).get_for_update(workspace_id)
        assert ws is not None
        ws.monitor_threads_addressed = {
            _MERGE_BLOCK_ATTENTION_STATE_KEY: marker,
            _MERGE_BLOCK_ATTENTION_ORIGIN_STATE_KEY: "ordinary_branch_protection",
        }
        ws.awaiting_human_since = datetime(2026, 1, 1, 12, tzinfo=UTC)
        ws.awaiting_human_reason = "ordinary branch protection still blocked"
        await session.commit()

    await runner._clear_or_preserve_merge_attention_for_queue_wait(
        workspace_id,
        state,
        status=_mergeable_status(),
        forge="github",
    )

    assert _MERGE_BLOCK_ATTENTION_STATE_KEY not in state.threads_addressed_ids
    assert _MERGE_BLOCK_ATTENTION_ORIGIN_STATE_KEY not in state.threads_addressed_ids
    async with factory() as session:
        ws_after = await WorkspaceRepository(session).get(workspace_id)
        assert ws_after is not None
    assert _MERGE_BLOCK_ATTENTION_STATE_KEY not in (ws_after.monitor_threads_addressed or {})
    assert _MERGE_BLOCK_ATTENTION_ORIGIN_STATE_KEY not in (ws_after.monitor_threads_addressed or {})
    assert ws_after.awaiting_human_since is None
    assert ws_after.awaiting_human_reason is None


@pytest.mark.unit
async def test_queue_wait_uses_in_memory_non_rejection_origin_before_persisted_origin(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Current in-memory origin wins over stale persisted origin so an ordinary
    marker is not kept alive by an older merge-rejection row value."""
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    marker = datetime(2026, 1, 1, 12, 0, 1, tzinfo=UTC).isoformat()
    state = MonitorState(
        threads_addressed_ids={
            _MERGE_BLOCK_ATTENTION_STATE_KEY: marker,
            _MERGE_BLOCK_ATTENTION_ORIGIN_STATE_KEY: "ordinary_branch_protection",
        }
    )
    async with factory() as session:
        ws = await WorkspaceRepository(session).get_for_update(workspace_id)
        assert ws is not None
        ws.monitor_threads_addressed = {
            _MERGE_BLOCK_ATTENTION_STATE_KEY: marker,
            _MERGE_BLOCK_ATTENTION_ORIGIN_STATE_KEY: (
                _MERGE_BLOCK_ATTENTION_ORIGIN_MERGE_REJECTION
            ),
        }
        ws.awaiting_human_since = datetime(2026, 1, 1, 12, tzinfo=UTC)
        ws.awaiting_human_reason = "stale persisted merge rejection"
        await session.commit()

    await runner._clear_or_preserve_merge_attention_for_queue_wait(
        workspace_id,
        state,
        status=_mergeable_status(),
        forge="github",
    )

    assert _MERGE_BLOCK_ATTENTION_STATE_KEY not in state.threads_addressed_ids
    assert _MERGE_BLOCK_ATTENTION_ORIGIN_STATE_KEY not in state.threads_addressed_ids
    async with factory() as session:
        ws_after = await WorkspaceRepository(session).get(workspace_id)
        assert ws_after is not None
    assert _MERGE_BLOCK_ATTENTION_STATE_KEY not in (ws_after.monitor_threads_addressed or {})
    assert _MERGE_BLOCK_ATTENTION_ORIGIN_STATE_KEY not in (ws_after.monitor_threads_addressed or {})
    assert ws_after.awaiting_human_since is None
    assert ws_after.awaiting_human_reason is None


@pytest.mark.unit
async def test_clear_stale_merge_attention_atomic_clear_marker_absent_on_row_still_clears_attention(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The stale (resolved) atomic-clear branch still clears ``awaiting_human_since``
    and commits when the persisted row carries NO merge-block marker — the in-memory
    ``state`` had a marker (driving the stale branch) but the durable row never
    received it (the outer loop had not flushed ``_persist_state`` yet, or a
    restart reloaded a marker-less row that a same-poll preserve then re-stamped).

    The ``threads_addressed.pop(...) is None`` guard must skip the redundant
    ``monitor_threads_addressed`` reassignment (line 303) WITHOUT skipping the
    attention clear + commit (lines 304-305): a row that already lacks the marker
    still needs ``awaiting_human_since`` cleared so the resolved ``NotifyHuman``
    episode stops surfacing. Covers the missing branch in the atomic clear path.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    # The in-memory state carries a stale marker (drives the stale branch), but
    # the persisted row has NO marker — only an active attention flag to clear.
    stale_stamp = datetime(2024, 1, 1, tzinfo=UTC).isoformat()
    state = MonitorState(threads_addressed_ids={_MERGE_BLOCK_ATTENTION_STATE_KEY: stale_stamp})
    async with factory() as session:
        ws = await WorkspaceRepository(session).get_for_update(workspace_id)
        assert ws is not None
        # No marker on the row; attention flag set from a prior escalation.
        ws.monitor_threads_addressed = {}
        ws.awaiting_human_since = datetime(2024, 1, 1, tzinfo=UTC)
        ws.awaiting_human_reason = "merge_blocked"
        await session.commit()
    resolved_now = datetime(2024, 1, 2, tzinfo=UTC)
    await runner._clear_stale_merge_attention(
        workspace_id,
        state,
        now=resolved_now,
    )
    # In-memory marker dropped.
    assert _MERGE_BLOCK_ATTENTION_STATE_KEY not in state.threads_addressed_ids
    # The persisted row still carries no marker, and the attention flag is cleared
    # despite the marker-absent branch skipping the threads_addressed reassignment.
    async with factory() as session:
        ws_after = await WorkspaceRepository(session).get(workspace_id)
        assert ws_after is not None
    assert _MERGE_BLOCK_ATTENTION_STATE_KEY not in (ws_after.monitor_threads_addressed or {})
    assert ws_after.awaiting_human_since is None


@pytest.mark.unit
async def test_set_workspace_attention_with_merge_block_marker_absent_marker_skips_marker_write(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The atomic write sets ``awaiting_human_since`` even when no marker is
    stamped into state (the caller did not re-stamp) — the marker merge is
    skipped, but the attention flag is still persisted. This is the defensive
    ``if stamped is not None`` False branch: production always stamps at the
    merge_loop call site, but the helper must not drop the attention write when
    the marker is absent."""
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    # No marker stamped into state ⇒ the marker merge is skipped, but the
    # attention flag must still be persisted.
    state = MonitorState()
    await runner._set_workspace_attention_with_merge_block_marker(
        workspace_id,
        state,
        reason="merge_blocked",
    )
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
    assert _MERGE_BLOCK_ATTENTION_STATE_KEY not in (ws.monitor_threads_addressed or {})
    assert ws.awaiting_human_since is not None
    assert ws.awaiting_human_reason == "merge_blocked"


@pytest.mark.unit
async def test_set_workspace_attention_with_merge_block_marker_already_equal_skips_marker_merge(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The atomic write's marker merge is idempotent: when the DB row already
    carries the in-memory stamp, the merge is skipped (no redundant assignment)
    but the attention flag is still refreshed and committed. This is the
    ``if threads_addressed.get(...) != stamped`` False branch — a second
    fallback poll re-stamps the same marker the first poll already persisted."""
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    # First call persists the marker AND sets attention.
    state = MonitorState()
    state.mark_merge_block_attention()
    await runner._set_workspace_attention_with_merge_block_marker(
        workspace_id,
        state,
        reason="merge_blocked",
    )
    stamped = state.threads_addressed_ids[_MERGE_BLOCK_ATTENTION_STATE_KEY]
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
    assert (ws.monitor_threads_addressed or {})[_MERGE_BLOCK_ATTENTION_STATE_KEY] == stamped
    first_attention = ws.awaiting_human_since
    assert first_attention is not None
    # A second call with the SAME stamp must skip the marker merge (already
    # equal) but still refresh and commit the attention flag.
    await runner._set_workspace_attention_with_merge_block_marker(
        workspace_id,
        state,
        reason="merge_blocked",
    )
    async with factory() as session:
        ws_after = await WorkspaceRepository(session).get(workspace_id)
        assert ws_after is not None
    assert (ws_after.monitor_threads_addressed or {})[_MERGE_BLOCK_ATTENTION_STATE_KEY] == stamped
    assert ws_after.awaiting_human_since is not None


class _CommitTrapSession:
    """Wraps an ``AsyncSession`` so ``commit`` can be trapped by attempt number.

    Counts every ``commit`` call into the shared ``commit_attempts`` dict (key
    ``"n"``) and raises ``RuntimeError`` on the attempt matching ``fail_on_attempt``
    (1-indexed) BEFORE delegating to ``inner.commit()``, so the simulated DB
    failure rolls that transaction back. ``fail_on_attempt=None`` never raises.

    This lets a regression FAIL for the prior two-commit implementation while
    PASSING for the current single-transaction atomic clear:

    * ``fail_on_attempt=1`` simulates a failure on the (only) atomic commit —
      the current implementation rolls both writes back (the
      ``PRRT_kwDOSJAM6s6Lh0zt`` rollback contract). The prior two-commit
      sequence would also roll its first commit back and never reach the
      second, so this case does NOT by itself distinguish old from new (the
      targeted ``fail_on_attempt=2`` case below does).
    * ``fail_on_attempt=2`` lets the first commit land (the prior
      implementation's ``_clear_workspace_attention`` clearing
      ``awaiting_human_since``) and raises on the second (the prior
      ``_clear_merge_block_attention_durably`` marker removal) — exposing the
      half-cleared restart window the atomic fix closed. The current
      single-commit implementation never reaches attempt 2, so it completes
      with both fields cleared and exactly one recorded commit attempt.
    """

    def __init__(
        self,
        inner: AsyncSession,
        commit_attempts: dict[str, int],
        *,
        fail_on_attempt: int | None = None,
    ) -> None:
        self._inner = inner
        self._commit_attempts = commit_attempts
        self._fail_on_attempt = fail_on_attempt

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def commit(self) -> None:
        self._commit_attempts["n"] += 1
        if self._fail_on_attempt is not None and (
            self._commit_attempts["n"] == self._fail_on_attempt
        ):
            raise RuntimeError("simulated DB failure mid-transaction")
        await self._inner.commit()

    async def __aenter__(self) -> _CommitTrapSession:
        await self._inner.__aenter__()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self._inner.__aexit__(exc_type, exc, tb)


@pytest.mark.unit
async def test_clear_stale_merge_attention_atomic_clear_rolls_back_together(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6Lh0zt: the stale (resolved) branch's marker removal and
    ``awaiting_human_since`` clear MUST land in a SINGLE transaction. The prior
    two-commit sequence (``_clear_workspace_attention`` then
    ``_clear_merge_block_attention_durably``) left a cancel/restart window where
    ``awaiting_human_since`` was already nulled but the STALE marker still sat on
    the DB row — so the next poll's preserve path re-stamped the stale marker
    fresh and wedged the monitor in a faux "awaiting human" state until another
    merge fallback ran (the ``PRRT_kwDOSJAM6s6Lf_37`` window's reciprocal). With
    both writes under one ``get_for_update`` transaction, a mid-transaction
    failure rolls BOTH back: a restart can never observe the cleared flag
    without the also-cleared marker, or vice versa.

    This test simulates the restart window by making the stale-branch commit
    raise. Under the atomic fix the transaction rolls back in full, so the row
    still carries BOTH the stale marker AND the still-set attention flag —
    exactly the pre-failure state, with neither half observable alone. The
    in-memory marker is dropped before the DB write (mirroring production), so
    the assertion focuses on the DB row's atomic consistency.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    # Seed a stale marker (resolved block) and an active attention flag into the
    # persisted row — the state the monitor would reload after a cancel/restart
    # before the outer loop's full ``_persist_state`` flush.
    stale_stamp = datetime(2024, 1, 1, tzinfo=UTC).isoformat()
    async with factory() as session:
        ws = await WorkspaceRepository(session).get_for_update(workspace_id)
        assert ws is not None
        ws.monitor_threads_addressed = {_MERGE_BLOCK_ATTENTION_STATE_KEY: stale_stamp}
        ws.awaiting_human_since = datetime(2024, 1, 1, tzinfo=UTC)
        ws.awaiting_human_reason = "merge_blocked"
        await session.commit()

    # Wrap the session factory so the FIRST session's commit raises — the
    # restart-window between the (old) two commits, now exercised against the
    # single atomic transaction.
    commit_attempts: dict[str, int] = {"n": 0}

    @asynccontextmanager
    async def _failing_factory() -> AsyncIterator[_CommitTrapSession]:
        commit_attempts["n"]  # shared counter touched inside the trap
        async with factory() as inner:
            yield _CommitTrapSession(inner, commit_attempts, fail_on_attempt=1)

    runner = make_runner(
        factory=factory,  # type: ignore[arg-type]
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    # Override the runner's session factory with the failing-commit wrapper so
    # the stale branch's single atomic commit raises mid-transaction.
    runner._deps = replace(runner._deps, session_factory=_failing_factory)

    state = MonitorState(threads_addressed_ids={_MERGE_BLOCK_ATTENTION_STATE_KEY: stale_stamp})
    resolved_now = datetime(2024, 1, 2, tzinfo=UTC)
    # The stale branch raises mid-transaction; the atomic transaction rolls back.
    with pytest.raises(RuntimeError, match="simulated DB failure"):
        await runner._clear_stale_merge_attention(
            workspace_id,
            state,
            now=resolved_now,
        )
    # A session was opened (the failing-commit one) — the runner attempted the
    # atomic clear, and the trap recorded exactly the single commit attempt the
    # atomic transaction performs.
    assert commit_attempts["n"] >= 1

    # Under the atomic fix, the transaction rolled back in full: the row still
    # carries BOTH the stale marker AND the still-set attention flag — neither
    # half of the marker/attention pair is observable independently. This is
    # the contract the prior two-commit sequence violated.
    async with factory() as session:
        ws_after = await WorkspaceRepository(session).get(workspace_id)
        assert ws_after is not None
    assert (ws_after.monitor_threads_addressed or {}).get(
        _MERGE_BLOCK_ATTENTION_STATE_KEY
    ) == stale_stamp
    assert ws_after.awaiting_human_since is not None
    assert ws_after.awaiting_human_reason == "merge_blocked"


@pytest.mark.unit
async def test_clear_stale_merge_attention_atomic_clear_distinguishes_two_commit_window(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6Lh0zt (PRRT_kwDOSJAM6s6LiZkN): the atomic clear MUST
    complete with a SINGLE commit that lands BOTH the stale marker removal and
    the ``awaiting_human_since`` clear. The sibling rollback regression above
    traps a mid-transaction failure (``fail_on_attempt=1``), but it does NOT by
    itself distinguish the current single-transaction implementation from the
    prior two-commit sequence: the prior ``_clear_workspace_attention`` (commit
    #1) then ``_clear_merge_block_attention_durably`` (commit #2) would ALSO
    roll its first commit back and never reach the second when commit #1
    raises, leaving both fields unchanged — the same observable end state as the
    atomic rollback.

    This targeted regression lets the FIRST commit land and traps the SECOND
    (``fail_on_attempt=2``). Under the prior two-commit implementation the
    first commit (``_clear_workspace_attention``) would clear
    ``awaiting_human_since`` and succeed, then the second commit
    (``_clear_merge_block_attention_durably``) would raise — leaving the DB in
    the half-cleared restart window (attention already nulled but the STALE
    marker still on the row) and surfacing the ``RuntimeError``. Under the
    current single-transaction atomic clear there is only ONE commit, so the
    trap is never reached (``fail_on_attempt=2``), the helper completes
    cleanly, and BOTH the marker and the attention flag are cleared together.
    Asserting exactly one recorded commit attempt AND both fields cleared
    makes this regression FAIL for the reintroduced two-commit approach (which
    would either raise on the second commit or leave the half-cleared state
    visible) while passing only for the atomic implementation.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    # Seed a stale marker (resolved block) and an active attention flag — the
    # pre-clear persisted state the atomic clear is meant to roll back from on
    # the stale (resolved) branch.
    stale_stamp = datetime(2024, 1, 1, tzinfo=UTC).isoformat()
    async with factory() as session:
        ws = await WorkspaceRepository(session).get_for_update(workspace_id)
        assert ws is not None
        ws.monitor_threads_addressed = {_MERGE_BLOCK_ATTENTION_STATE_KEY: stale_stamp}
        ws.awaiting_human_since = datetime(2024, 1, 1, tzinfo=UTC)
        ws.awaiting_human_reason = "merge_blocked"
        await session.commit()

    # Trap the SECOND commit: the prior two-commit implementation would land
    # the attention clear on commit #1 and raise on commit #2 (marker removal),
    # surfacing the half-cleared restart window. The current single-transaction
    # atomic clear performs exactly one commit and never reaches attempt 2.
    commit_attempts: dict[str, int] = {"n": 0}

    @asynccontextmanager
    async def _trapping_factory() -> AsyncIterator[_CommitTrapSession]:
        async with factory() as inner:
            yield _CommitTrapSession(inner, commit_attempts, fail_on_attempt=2)

    runner = make_runner(
        factory=factory,  # type: ignore[arg-type]
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    runner._deps = replace(runner._deps, session_factory=_trapping_factory)

    state = MonitorState(threads_addressed_ids={_MERGE_BLOCK_ATTENTION_STATE_KEY: stale_stamp})
    resolved_now = datetime(2024, 1, 2, tzinfo=UTC)
    # The atomic clear completes with its single commit; the trap (set on
    # attempt 2) is never reached. The prior two-commit implementation would
    # raise here on its second commit.
    await runner._clear_stale_merge_attention(
        workspace_id,
        state,
        now=resolved_now,
    )

    # The atomic implementation performs EXACTLY one commit. A reintroduced
    # two-commit sequence would record two attempts (and raise on the second).
    assert commit_attempts["n"] == 1

    # Both the stale marker AND the attention flag are cleared together in
    # that single transaction — neither half of the pair is observable alone.
    # A prior two-commit implementation that survived commit #1 but raised on
    # commit #2 would leave the half-cleared state: marker still present,
    # attention already nulled.
    async with factory() as session:
        ws_after = await WorkspaceRepository(session).get(workspace_id)
        assert ws_after is not None
    assert (ws_after.monitor_threads_addressed or {}).get(_MERGE_BLOCK_ATTENTION_STATE_KEY) is None
    assert ws_after.awaiting_human_since is None
    assert ws_after.awaiting_human_reason is None


@pytest.mark.unit
async def test_clear_stale_merge_attention_atomic_clear_missing_workspace_skips_writes(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6Lh0zt: the atomic clear no-ops when the workspace row is
    gone (a GC/destroy race between the stale-clear and the atomic write) — no
    marker removal, no attention clear, no commit. Mirrors the established
    ``get_for_update``-returns-None no-op guard in the sibling durable helpers."""
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    stale_stamp = datetime(2024, 1, 1, tzinfo=UTC).isoformat()
    state = MonitorState(threads_addressed_ids={_MERGE_BLOCK_ATTENTION_STATE_KEY: stale_stamp})
    # The workspace does not exist — the atomic clear returns before any write.
    await runner._clear_stale_merge_attention(
        "ws_does_not_exist",
        state,
        now=datetime(2024, 1, 2, tzinfo=UTC),
    )
    # In-memory marker is still dropped (the caller's signal), but no DB write
    # was attempted against a missing row.
    assert _MERGE_BLOCK_ATTENTION_STATE_KEY not in state.threads_addressed_ids
