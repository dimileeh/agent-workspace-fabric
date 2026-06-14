"""Cross-cycle and revoke-race marker-write guards (#399), Postgres-backed.

Split out of ``test_cleanup_auth_overlay_retry_part_002`` to keep each test file
under the maintainability line limit. These cover sections 8 and 8b: marker writes
re-check effective release under the row lock (revoke race) and verify the release
*cycle* still matches the listed floor (revoke + re-release). Shared fixtures and
helpers (``_sweeper`` / ``_make_workspace`` / ``_event_types`` / the release-history
builders) are reused from parts 001 and 002.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.control.worker import cleanup_auth_overlay as worker_overlay
from awf.db.models import WorkspaceEvent
from awf.db.repositories import WorkspaceRepository
from awf.db.repositories.base import (
    TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
    latest_terminal_runtime_release_event_order,
)
from awf.db.session import make_session_factory
from tests.postgres import postgres_test_engine
from tests.unit.control.test_cleanup_auth_overlay_retry_parts.test_cleanup_auth_overlay_retry_part_001 import (
    _candidate,
    _RecordingLog,
)
from tests.unit.control.test_cleanup_auth_overlay_retry_parts.test_cleanup_auth_overlay_retry_part_002 import (
    _build_revoked_then_rereleased_cycle,
    _event_types,
    _make_workspace,
    _mark_runtime_release_revoked,
    _mark_runtime_released,
    _sweeper,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


# --------------------------------------------------------------------------- #
# 8. Marker writes re-check effective release under the row lock (revoke race)
# --------------------------------------------------------------------------- #
#
# The candidate query gates on effective release, but a candidate can go stale: the
# provisioner can write ``terminal_runtime_release_revoked`` (under ``get_for_update``)
# after the candidate is listed and before the deferred retry's marker write runs. A
# terminal ``resolved``/``exhausted`` marker (or a fresh ``pending``) written in that
# window would suppress / burn the umount retry still owed once the runtime is genuinely
# released, leaking the overlay mount. Each marker write therefore re-checks effective
# release under the same row lock and skips (loudly) when the release was revoked.


@pytest.mark.asyncio
async def test_record_resolved_skips_when_release_revoked(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``resolved`` marker is NOT written when the release was revoked after listing —
    the retry stays owed and the skip is surfaced under its own reason code."""
    log = _RecordingLog()
    monkeypatch.setattr(worker_overlay, "_log", log)
    sweeper = _sweeper(factory)
    ws_id: str = ""
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await _make_workspace(session, repo)
        ws_id = ws.id
        await _mark_runtime_release_revoked(session, repo, ws)
        await session.commit()

    await sweeper._record_terminal_auth_overlay_unmount_resolved(  # noqa: SLF001
        _candidate(ws_id), auth_overlay_unmounted=True
    )

    async with factory() as session:
        types = await _event_types(session, ws_id)
    assert worker_overlay._TERMINAL_AUTH_OVERLAY_UNMOUNT_RESOLVED_EVENT_TYPE not in types
    revoked = [
        fields
        for event, fields in log.warnings
        if event == worker_overlay._TERMINAL_AUTH_OVERLAY_UNMOUNT_RELEASE_REVOKED_EVENT_TYPE
    ]
    assert len(revoked) == 1
    assert revoked[0]["marker"] == "resolved"
    assert (
        revoked[0]["reason_code"]
        == worker_overlay._TERMINAL_AUTH_OVERLAY_UNMOUNT_RELEASE_REVOKED_REASON_CODE
    )


@pytest.mark.asyncio
async def test_record_exhausted_skips_when_release_revoked(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``exhausted`` marker is NOT written when the release was revoked after listing,
    so a temporary revoke cannot permanently suppress the retry."""
    log = _RecordingLog()
    monkeypatch.setattr(worker_overlay, "_log", log)
    sweeper = _sweeper(factory)
    ws_id: str = ""
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await _make_workspace(session, repo)
        ws_id = ws.id
        await _mark_runtime_release_revoked(session, repo, ws)
        await session.commit()

    await sweeper._record_terminal_auth_overlay_unmount_exhausted(  # noqa: SLF001
        _candidate(ws_id), attempts=5
    )

    async with factory() as session:
        types = await _event_types(session, ws_id)
    assert worker_overlay._TERMINAL_AUTH_OVERLAY_UNMOUNT_EXHAUSTED_EVENT_TYPE not in types
    revoked = [
        fields
        for event, fields in log.warnings
        if event == worker_overlay._TERMINAL_AUTH_OVERLAY_UNMOUNT_RELEASE_REVOKED_EVENT_TYPE
    ]
    assert len(revoked) == 1
    assert revoked[0]["marker"] == "exhausted"


@pytest.mark.asyncio
async def test_append_pending_skips_when_release_revoked(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh ``pending`` marker is NOT appended when the release was revoked after
    listing, so the revoke window cannot burn one of the bounded deferred attempts."""
    log = _RecordingLog()
    monkeypatch.setattr(worker_overlay, "_log", log)
    sweeper = _sweeper(factory)
    ws_id: str = ""
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await _make_workspace(session, repo)
        ws_id = ws.id
        await _mark_runtime_release_revoked(session, repo, ws)
        # An original pending marker already exists (co-written at release time); the
        # revoke superseded the release before this deferred sweep ran.
        await repo.add_event(
            ws,
            event_type=worker_overlay._TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_EVENT_TYPE,
            reason_code=worker_overlay._TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_REASON_CODE,
            payload={"attempt": 1},
        )
        await session.commit()

    await sweeper._append_terminal_auth_overlay_unmount_pending(  # noqa: SLF001
        _candidate(ws_id), attempt=2
    )

    async with factory() as session:
        types = await _event_types(session, ws_id)
    # The original pending marker survives, but no second one is appended.
    assert types.count(worker_overlay._TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_EVENT_TYPE) == 1
    revoked = [
        fields
        for event, fields in log.warnings
        if event == worker_overlay._TERMINAL_AUTH_OVERLAY_UNMOUNT_RELEASE_REVOKED_EVENT_TYPE
    ]
    assert len(revoked) == 1
    assert revoked[0]["marker"] == "pending"


# --------------------------------------------------------------------------- #
# 8b. Marker writes verify the *cycle* under the row lock (revoke + re-release)
# --------------------------------------------------------------------------- #
#
# The bare effective-release recheck only asks whether *some* release is currently
# effective. A revoke *plus* a genuine re-release after listing leaves the workspace
# effectively released again but under a NEW cycle (a higher release ``event_order``).
# The cycle-scoped terminal-marker guard then sees no terminal marker for the new cycle,
# so a stale-cycle outcome would write a ``resolved``/``exhausted`` (or burn a ``pending``)
# at/after the new floor and suppress the retry the fresh cycle just owed. Each marker write
# therefore also verifies the latest release cycle still matches the listed floor.


async def _min_release_event_order(session: AsyncSession, workspace_id: str) -> int:
    """Return the *earliest* ``terminal_runtime_released`` order — the prior cycle's floor."""
    return int(
        (
            await session.execute(
                sa.select(sa.func.min(WorkspaceEvent.event_order))
                .where(WorkspaceEvent.workspace_id == workspace_id)
                .where(WorkspaceEvent.event_type == TERMINAL_RUNTIME_RELEASE_EVENT_TYPE)
            )
        ).scalar_one()
    )


@pytest.mark.asyncio
async def test_record_resolved_skips_when_release_cycle_changed(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A candidate listed under a now-superseded cycle must NOT write a ``resolved`` marker:
    the workspace is effectively released again, but under a fresh cycle that owes its own
    retry, so the stale-cycle outcome is skipped under its own reason code."""
    log = _RecordingLog()
    monkeypatch.setattr(worker_overlay, "_log", log)
    sweeper = _sweeper(factory)
    ws_id: str = ""
    stale_floor: int = 0
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await _make_workspace(session, repo)
        ws_id = ws.id
        await _build_revoked_then_rereleased_cycle(repo, ws)
        await session.commit()
    async with factory() as session:
        stale_floor = await _min_release_event_order(session, ws_id)

    await sweeper._record_terminal_auth_overlay_unmount_resolved(  # noqa: SLF001
        _candidate(ws_id, release_cycle_floor=stale_floor), auth_overlay_unmounted=True
    )

    async with factory() as session:
        types = await _event_types(session, ws_id)
    assert worker_overlay._TERMINAL_AUTH_OVERLAY_UNMOUNT_RESOLVED_EVENT_TYPE not in types
    revoked = [
        fields
        for event, fields in log.warnings
        if event == worker_overlay._TERMINAL_AUTH_OVERLAY_UNMOUNT_RELEASE_REVOKED_EVENT_TYPE
    ]
    assert len(revoked) == 1
    assert revoked[0]["marker"] == "resolved"


@pytest.mark.asyncio
async def test_record_exhausted_skips_when_release_cycle_changed(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``exhausted`` marker computed from a superseded cycle must not starve the fresh
    cycle's deferred-sweep budget."""
    log = _RecordingLog()
    monkeypatch.setattr(worker_overlay, "_log", log)
    sweeper = _sweeper(factory)
    ws_id: str = ""
    stale_floor: int = 0
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await _make_workspace(session, repo)
        ws_id = ws.id
        await _build_revoked_then_rereleased_cycle(repo, ws)
        await session.commit()
    async with factory() as session:
        stale_floor = await _min_release_event_order(session, ws_id)

    await sweeper._record_terminal_auth_overlay_unmount_exhausted(  # noqa: SLF001
        _candidate(ws_id, release_cycle_floor=stale_floor), attempts=5
    )

    async with factory() as session:
        types = await _event_types(session, ws_id)
    assert worker_overlay._TERMINAL_AUTH_OVERLAY_UNMOUNT_EXHAUSTED_EVENT_TYPE not in types
    revoked = [
        fields
        for event, fields in log.warnings
        if event == worker_overlay._TERMINAL_AUTH_OVERLAY_UNMOUNT_RELEASE_REVOKED_EVENT_TYPE
    ]
    assert len(revoked) == 1
    assert revoked[0]["marker"] == "exhausted"


@pytest.mark.asyncio
async def test_append_pending_skips_when_release_cycle_changed(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh ``pending`` appended for a superseded cycle would inflate the new cycle's
    bounded-sweep count toward a premature ``exhausted``; it is skipped instead."""
    log = _RecordingLog()
    monkeypatch.setattr(worker_overlay, "_log", log)
    sweeper = _sweeper(factory)
    ws_id: str = ""
    stale_floor: int = 0
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await _make_workspace(session, repo)
        ws_id = ws.id
        # Cycle 1 carries one ``pending``; cycle 2 carries one fresh ``pending`` (two total).
        await _build_revoked_then_rereleased_cycle(repo, ws)
        await session.commit()
    async with factory() as session:
        stale_floor = await _min_release_event_order(session, ws_id)

    await sweeper._append_terminal_auth_overlay_unmount_pending(  # noqa: SLF001
        _candidate(ws_id, release_cycle_floor=stale_floor), attempt=2
    )

    async with factory() as session:
        types = await _event_types(session, ws_id)
    # No third ``pending`` appended — the two pre-existing markers are untouched.
    assert types.count(worker_overlay._TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_EVENT_TYPE) == 2
    revoked = [
        fields
        for event, fields in log.warnings
        if event == worker_overlay._TERMINAL_AUTH_OVERLAY_UNMOUNT_RELEASE_REVOKED_EVENT_TYPE
    ]
    assert len(revoked) == 1
    assert revoked[0]["marker"] == "pending"


@pytest.mark.asyncio
async def test_record_resolved_writes_when_release_cycle_matches(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """When the listed floor still matches the latest release cycle, the cycle guard is a
    no-op and the ``resolved`` marker is written normally."""
    sweeper = _sweeper(factory)
    ws_id: str = ""
    current_floor: int = 0
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await _make_workspace(session, repo)
        await _mark_runtime_released(repo, ws)
        ws_id = ws.id
        await session.commit()
    async with factory() as session:
        current_floor = await _min_release_event_order(session, ws_id)

    await sweeper._record_terminal_auth_overlay_unmount_resolved(  # noqa: SLF001
        _candidate(ws_id, release_cycle_floor=current_floor), auth_overlay_unmounted=True
    )

    async with factory() as session:
        types = await _event_types(session, ws_id)
    assert types.count(worker_overlay._TERMINAL_AUTH_OVERLAY_UNMOUNT_RESOLVED_EVENT_TYPE) == 1


@pytest.mark.asyncio
async def test_pending_candidate_query_carries_release_cycle_floor(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A listed candidate carries the current cycle's release ``event_order`` so the
    marker-write guards can detect a later cross-cycle re-release."""
    sweeper = _sweeper(factory)
    ws_id: str = ""
    current_floor: int = 0
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await _make_workspace(session, repo)
        ws_id = ws.id
        await _mark_runtime_released(repo, ws)
        await repo.add_event(
            ws,
            event_type=worker_overlay._TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_EVENT_TYPE,
            reason_code=worker_overlay._TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_REASON_CODE,
            payload={"attempt": 1},
        )
        await session.commit()
    async with factory() as session:
        current_floor = await _min_release_event_order(session, ws_id)

    candidates = await sweeper._list_pending_terminal_auth_overlay_unmount_candidates(limit=None)  # noqa: SLF001
    listed = next(c for c in candidates if c.workspace_id == ws_id)
    assert listed.release_cycle_floor == current_floor


@pytest.mark.asyncio
async def test_latest_release_event_order_reader_returns_none_then_floor(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The async cycle-floor reader returns ``None`` before any release event and the latest
    release ``event_order`` once one exists (the comparison the marker-write guards rely on)."""
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await _make_workspace(session, repo)
        await session.commit()
        assert await latest_terminal_runtime_release_event_order(session, ws.id) is None
        await _mark_runtime_released(repo, ws)
        await session.commit()
        expected = await _min_release_event_order(session, ws.id)
        assert await latest_terminal_runtime_release_event_order(session, ws.id) == expected


@pytest.mark.asyncio
async def test_teardown_guard_reports_released_revoked_and_skipped(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The pre-teardown guard returns ``"released"`` while the release is effective,
    ``"revoked"`` once a later revoke supersedes it (so the umount is gated off), and
    ``"skipped"`` when the workspace row is absent — all read under the row lock."""
    sweeper = _sweeper(factory)
    released_id: str = ""
    revoked_id: str = ""
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws_released = await _make_workspace(session, repo, compose_project_name="awf_guard_ok")
        ws_revoked = await _make_workspace(session, repo, compose_project_name="awf_guard_revoked")
        released_id = ws_released.id
        revoked_id = ws_revoked.id
        await _mark_runtime_released(repo, ws_released)
        await _mark_runtime_release_revoked(session, repo, ws_revoked)
        await session.commit()

    assert (
        await worker_overlay._terminal_auth_overlay_unmount_effective_release_guard(  # noqa: SLF001
            sweeper, _candidate(released_id)
        )
        == "released"
    )
    assert (
        await worker_overlay._terminal_auth_overlay_unmount_effective_release_guard(  # noqa: SLF001
            sweeper, _candidate(revoked_id)
        )
        == "revoked"
    )
    assert (
        await worker_overlay._terminal_auth_overlay_unmount_effective_release_guard(  # noqa: SLF001
            sweeper, _candidate("ws_guard_missing")
        )
        == "skipped"
    )
