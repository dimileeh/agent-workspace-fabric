"""Postgres-backed coverage for the worker-side auth-overlay umount retry (#399).

This part continues ``test_cleanup_auth_overlay_retry_part_001``: the candidate
query, the ``pending``/``resolved``/``exhausted`` record helpers, the pending-event
counters, and the release-cycle scoping all run against a real Postgres so the
event-type SQL predicates are exercised end-to-end. Shared in-memory doubles
(``_candidate`` / ``_RecordingLog``) and the ``_REPO`` / ``_BASE`` constants are
reused from part 001.
"""

from __future__ import annotations

import importlib.util
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.control.worker import WorkerConfig
from awf.control.worker import cleanup as worker_cleanup
from awf.control.worker import cleanup_auth_overlay as worker_overlay
from awf.db.enums import WorkspaceStatus
from awf.db.models import Workspace, WorkspaceEvent
from awf.db.repositories import (
    ResourceReservationRepository,
    TaskAttemptRepository,
    TaskRepository,
    WorkspaceRepository,
)
from awf.db.repositories.base import (
    TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
    TERMINAL_RUNTIME_RELEASE_REASON_CODE,
    TERMINAL_RUNTIME_RELEASE_REVOKED_EVENT_TYPE,
    TERMINAL_RUNTIME_RELEASE_REVOKED_REASON_CODE,
    latest_terminal_runtime_release_event_order_expr,
)
from awf.db.session import make_session_factory
from awf.node.cleanup import WorkspaceCleanupResult
from tests.postgres import postgres_test_engine
from tests.unit.control.test_cleanup_auth_overlay_retry_parts.test_cleanup_auth_overlay_retry_part_001 import (
    _BASE,
    _REPO,
    _candidate,
)

# --------------------------------------------------------------------------- #
# 7. Candidate query + record helpers + counters against a real Postgres
# --------------------------------------------------------------------------- #

_AUTH_OVERLAY_BACKFILL_REVISION = "0f1e2d3c4b5a"
_AUTH_OVERLAY_BACKFILL_FILENAME = (
    f"{_AUTH_OVERLAY_BACKFILL_REVISION}_backfill_auth_overlay_unmount_pending.py"
)


def _load_auth_overlay_backfill_migration() -> ModuleType:
    repo_root = Path(__file__).resolve().parents[4]
    migration_path = repo_root / "migrations" / "versions" / _AUTH_OVERLAY_BACKFILL_FILENAME
    spec = importlib.util.spec_from_file_location(
        "awf_auth_overlay_unmount_backfill_migration",
        migration_path,
    )
    if spec is None or spec.loader is None:
        raise AssertionError(f"could not load migration module from {migration_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _sweeper(factory: async_sessionmaker[AsyncSession], node_id: str = "node-1") -> Any:
    cfg = WorkerConfig(node_id=node_id)
    return type(
        "OverlaySweeper",
        (),
        {
            "_config": cfg,
            "_session_factory": factory,
            "_log_transient_db_retry": lambda *_: None,
            "_list_pending_terminal_auth_overlay_unmount_candidates": (
                worker_overlay._list_pending_terminal_auth_overlay_unmount_candidates
            ),
            "_terminal_auth_overlay_unmount_effective_release_guard": (
                worker_overlay._terminal_auth_overlay_unmount_effective_release_guard
            ),
            "_count_terminal_auth_overlay_unmount_pending_events": (
                worker_overlay._count_terminal_auth_overlay_unmount_pending_events
            ),
            "_has_terminal_auth_overlay_unmount_terminal_event": (
                worker_overlay._has_terminal_auth_overlay_unmount_terminal_event
            ),
            "_record_terminal_auth_overlay_unmount_resolved": (
                worker_overlay._record_terminal_auth_overlay_unmount_resolved
            ),
            "_record_terminal_auth_overlay_unmount_exhausted": (
                worker_overlay._record_terminal_auth_overlay_unmount_exhausted
            ),
            "_append_terminal_auth_overlay_unmount_pending": (
                worker_overlay._append_terminal_auth_overlay_unmount_pending
            ),
            "_retry_pending_terminal_auth_overlay_unmounts": (
                worker_overlay._retry_pending_terminal_auth_overlay_unmounts
            ),
            "_retry_pending_terminal_auth_overlay_unmount_for_candidate": (
                worker_overlay._retry_pending_terminal_auth_overlay_unmount_for_candidate
            ),
        },
    )()


async def _make_workspace(
    session: AsyncSession,
    repo: WorkspaceRepository,
    *,
    status: WorkspaceStatus = WorkspaceStatus.failed,
    node_id: str | None = "node-1",
    compose_project_name: str = "awf_overlay_ws",
) -> Workspace:
    ws = await repo.create(
        repo_url=_REPO,
        branch_base=_BASE,
        task_title="overlay retry",
        task_prompt="test",
        agent="codex",
        test_commands=[],
        task_policy={},
    )
    ws.status = status.value
    ws.node_id = node_id
    ws.compose_project_name = compose_project_name
    await session.commit()
    return ws


async def _event_types(session: AsyncSession, workspace_id: str) -> list[str]:
    rows = (
        await session.execute(
            sa.select(WorkspaceEvent.event_type).where(WorkspaceEvent.workspace_id == workspace_id)
        )
    ).all()
    return [r[0] for r in rows]


@pytest.mark.asyncio
async def test_auth_overlay_backfill_seeds_pre_upgrade_failed_release_and_retry_sweep_resolves(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-upgrade failed umount release gets a backfilled ``pending`` marker,
    enters the existing deferred sweep, and is resolved using the same reason-coded
    marker flow as post-upgrade failures."""
    migration = _load_auth_overlay_backfill_migration()
    sweeper = _sweeper(factory)
    teardown_calls: list[str] = []

    async def _teardown(_self: object, candidate: Any) -> bool:
        teardown_calls.append(candidate.workspace_id)
        return True

    monkeypatch.setattr(worker_overlay, "_teardown_terminal_auth_overlay", _teardown)

    ws_id = ""
    release_order = 0
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await _make_workspace(
            session,
            repo,
            compose_project_name="awf_backfilled_retry",
        )
        ws_id = ws.id
        release = await repo.add_event(
            ws,
            event_type=TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
            reason_code=TERMINAL_RUNTIME_RELEASE_REASON_CODE,
            payload={
                "auth_overlay_unmounted": False,
                "compose_project_name": "awf_backfilled_retry",
            },
        )
        assert release.event_order is not None
        release_order = release.event_order
        await session.commit()

    async with factory() as session:
        conn = await session.connection()
        inserted = await conn.run_sync(migration.backfill_auth_overlay_unmount_pending)
        await session.commit()

    candidates = await sweeper._list_pending_terminal_auth_overlay_unmount_candidates(  # noqa: SLF001
        limit=None
    )
    backfilled_candidate = next(
        candidate for candidate in candidates if candidate.workspace_id == ws_id
    )
    assert backfilled_candidate.release_cycle_floor == release_order
    assert inserted == 1

    await sweeper._retry_pending_terminal_auth_overlay_unmounts(limit=None)  # noqa: SLF001

    async with factory() as session:
        events = (
            await session.execute(
                sa.select(
                    WorkspaceEvent.event_type,
                    WorkspaceEvent.reason_code,
                    WorkspaceEvent.payload,
                    WorkspaceEvent.event_order,
                )
                .where(WorkspaceEvent.workspace_id == ws_id)
                .where(
                    WorkspaceEvent.event_type.in_(
                        (
                            worker_overlay._TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_EVENT_TYPE,
                            worker_overlay._TERMINAL_AUTH_OVERLAY_UNMOUNT_RESOLVED_EVENT_TYPE,
                        )
                    )
                )
                .order_by(WorkspaceEvent.event_order.asc())
            )
        ).all()

    assert teardown_calls == [ws_id]
    assert [row.event_type for row in events] == [
        worker_overlay._TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_EVENT_TYPE,
        worker_overlay._TERMINAL_AUTH_OVERLAY_UNMOUNT_RESOLVED_EVENT_TYPE,
    ]
    pending, resolved = events
    assert pending.reason_code == worker_overlay._TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_REASON_CODE
    assert pending.payload == {
        "compose_project_name": "awf_backfilled_retry",
        "workspace_status": WorkspaceStatus.failed.value,
        "attempt": 1,
    }
    assert pending.event_order >= release_order
    assert (
        resolved.reason_code == worker_overlay._TERMINAL_AUTH_OVERLAY_UNMOUNT_RESOLVED_REASON_CODE
    )
    assert resolved.payload == {
        "compose_project_name": "awf_backfilled_retry",
        "workspace_status": WorkspaceStatus.failed.value,
        "auth_overlay_unmounted": True,
    }


@pytest.mark.asyncio
async def test_auth_overlay_backfill_null_order_release_retry_sweep_resolves(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A backfilled pending marker for a NULL-order release still resolves after retry."""
    migration = _load_auth_overlay_backfill_migration()
    sweeper = _sweeper(factory)
    teardown_calls: list[str] = []

    async def _teardown(_self: object, candidate: Any) -> bool:
        teardown_calls.append(candidate.workspace_id)
        return True

    monkeypatch.setattr(worker_overlay, "_teardown_terminal_auth_overlay", _teardown)

    ws_id = ""
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await _make_workspace(
            session,
            repo,
            compose_project_name="awf_backfilled_null_order_retry",
        )
        ws_id = ws.id
        release = await repo.add_event(
            ws,
            event_type=TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
            reason_code=TERMINAL_RUNTIME_RELEASE_REASON_CODE,
            payload={
                "auth_overlay_unmounted": False,
                "compose_project_name": "awf_backfilled_null_order_retry",
            },
        )
        release.event_order = None
        await session.commit()

    async with factory() as session:
        conn = await session.connection()
        inserted = await conn.run_sync(migration.backfill_auth_overlay_unmount_pending)
        await session.commit()

    candidates = await sweeper._list_pending_terminal_auth_overlay_unmount_candidates(  # noqa: SLF001
        limit=None
    )
    backfilled_candidate = next(
        candidate for candidate in candidates if candidate.workspace_id == ws_id
    )
    assert inserted == 1
    assert backfilled_candidate.release_cycle_floor == -1

    await sweeper._retry_pending_terminal_auth_overlay_unmounts(limit=None)  # noqa: SLF001

    async with factory() as session:
        events = (
            await session.execute(
                sa.select(
                    WorkspaceEvent.event_type,
                    WorkspaceEvent.reason_code,
                    WorkspaceEvent.payload,
                )
                .where(WorkspaceEvent.workspace_id == ws_id)
                .where(
                    WorkspaceEvent.event_type.in_(
                        (
                            worker_overlay._TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_EVENT_TYPE,
                            worker_overlay._TERMINAL_AUTH_OVERLAY_UNMOUNT_RESOLVED_EVENT_TYPE,
                        )
                    )
                )
                .order_by(WorkspaceEvent.event_order.asc())
            )
        ).all()

    assert teardown_calls == [ws_id]
    assert [row.event_type for row in events] == [
        worker_overlay._TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_EVENT_TYPE,
        worker_overlay._TERMINAL_AUTH_OVERLAY_UNMOUNT_RESOLVED_EVENT_TYPE,
    ]
    resolved = events[-1]
    assert (
        resolved.reason_code == worker_overlay._TERMINAL_AUTH_OVERLAY_UNMOUNT_RESOLVED_REASON_CODE
    )
    assert resolved.payload == {
        "compose_project_name": "awf_backfilled_null_order_retry",
        "workspace_status": WorkspaceStatus.failed.value,
        "auth_overlay_unmounted": True,
    }


@pytest.mark.asyncio
async def test_pending_candidate_query_detects_legacy_null_order_pending_marker(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A NULL-order marker belongs to a NULL-order release cycle.

    The backfill migration treats such a marker as current-cycle dedupe, so the
    runtime sweep must also see it when the coalesced release floor is ``-1``.
    """
    sweeper = _sweeper(factory)
    ws_id = ""
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await _make_workspace(
            session,
            repo,
            compose_project_name="awf_legacy_null_pending",
        )
        ws_id = ws.id
        release = await repo.add_event(
            ws,
            event_type=TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
            reason_code=TERMINAL_RUNTIME_RELEASE_REASON_CODE,
        )
        pending = await repo.add_event(
            ws,
            event_type=worker_overlay._TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_EVENT_TYPE,
            reason_code=worker_overlay._TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_REASON_CODE,
            payload={"attempt": 1},
        )
        release.event_order = None
        pending.event_order = None
        await session.commit()

    candidates = await sweeper._list_pending_terminal_auth_overlay_unmount_candidates(  # noqa: SLF001
        limit=None
    )
    listed = next(candidate for candidate in candidates if candidate.workspace_id == ws_id)
    assert listed.release_cycle_floor == -1
    assert await sweeper._count_terminal_auth_overlay_unmount_pending_events(ws_id) == 1  # noqa: SLF001


@pytest.mark.asyncio
async def test_terminal_marker_check_detects_legacy_null_order_marker(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A NULL-order terminal marker suppresses the NULL-order release cycle."""
    sweeper = _sweeper(factory)
    ws_id = ""
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await _make_workspace(
            session,
            repo,
            compose_project_name="awf_legacy_null_resolved",
        )
        ws_id = ws.id
        release = await repo.add_event(
            ws,
            event_type=TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
            reason_code=TERMINAL_RUNTIME_RELEASE_REASON_CODE,
        )
        pending = await repo.add_event(
            ws,
            event_type=worker_overlay._TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_EVENT_TYPE,
            reason_code=worker_overlay._TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_REASON_CODE,
            payload={"attempt": 1},
        )
        resolved = await repo.add_event(
            ws,
            event_type=worker_overlay._TERMINAL_AUTH_OVERLAY_UNMOUNT_RESOLVED_EVENT_TYPE,
            reason_code=worker_overlay._TERMINAL_AUTH_OVERLAY_UNMOUNT_RESOLVED_REASON_CODE,
        )
        release.event_order = None
        pending.event_order = None
        resolved.event_order = None
        await session.commit()

    candidates = await sweeper._list_pending_terminal_auth_overlay_unmount_candidates(  # noqa: SLF001
        limit=None
    )
    assert ws_id not in {candidate.workspace_id for candidate in candidates}
    async with factory() as session:
        assert (
            await sweeper._has_terminal_auth_overlay_unmount_terminal_event(session, ws_id)  # noqa: SLF001
        ) is True


async def _mark_runtime_released(repo: WorkspaceRepository, ws: Workspace) -> None:
    """Write the ``terminal_runtime_released`` event a deferred candidate always carries.

    A ``pending`` overlay marker is co-written with ``terminal_runtime_released`` in the
    same transaction, so every deferred candidate is effectively released. The marker-write
    helpers re-check that under the row lock, so the precondition must hold for a write to
    land — mirror it here.
    """
    await repo.add_event(
        ws,
        event_type=TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
        reason_code=TERMINAL_RUNTIME_RELEASE_REASON_CODE,
    )


async def _mark_runtime_release_revoked(
    session: AsyncSession, repo: WorkspaceRepository, ws: Workspace
) -> None:
    """Make the latest release/revoke event a revocation → not effectively released.

    Models the race the marker-write recheck guards: the candidate was effectively
    released when listed, then the provisioner superseded the release with a
    ``terminal_runtime_release_revoked`` (orphan containers still hold the overlay bind)
    before the deferred retry's marker write runs.
    """
    await repo.add_event(
        ws,
        event_type=TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
        reason_code=TERMINAL_RUNTIME_RELEASE_REASON_CODE,
    )
    released_ev = (
        await session.execute(
            sa.select(WorkspaceEvent)
            .where(WorkspaceEvent.workspace_id == ws.id)
            .where(WorkspaceEvent.event_type == TERMINAL_RUNTIME_RELEASE_EVENT_TYPE)
        )
    ).scalar_one()
    released_ev.occurred_at = datetime(2026, 5, 31, 12, 0, 0, tzinfo=UTC)
    await repo.add_event(
        ws,
        event_type=TERMINAL_RUNTIME_RELEASE_REVOKED_EVENT_TYPE,
        reason_code=TERMINAL_RUNTIME_RELEASE_REVOKED_REASON_CODE,
    )
    revoked_ev = (
        await session.execute(
            sa.select(WorkspaceEvent)
            .where(WorkspaceEvent.workspace_id == ws.id)
            .where(WorkspaceEvent.event_type == TERMINAL_RUNTIME_RELEASE_REVOKED_EVENT_TYPE)
        )
    ).scalar_one()
    revoked_ev.occurred_at = datetime(2026, 5, 31, 12, 0, 1, tzinfo=UTC)


@pytest.mark.asyncio
async def test_pending_candidate_query_excludes_resolved_and_exhausted(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Only terminal workspaces with an unresolved ``pending`` marker surface: a
    ``resolved`` or ``exhausted`` marker (or no pending marker at all) excludes
    them from the deferred sweep."""
    sweeper = _sweeper(factory)
    ids: dict[str, str] = {}
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws_pending = await _make_workspace(session, repo, compose_project_name="awf_pending")
        ws_resolved = await _make_workspace(session, repo, compose_project_name="awf_resolved")
        ws_exhausted = await _make_workspace(session, repo, compose_project_name="awf_exhausted")
        ws_none = await _make_workspace(session, repo, compose_project_name="awf_none")
        ws_null_node = await _make_workspace(
            session, repo, node_id=None, compose_project_name="awf_null"
        )
        ids = {
            "pending": ws_pending.id,
            "resolved": ws_resolved.id,
            "exhausted": ws_exhausted.id,
            "none": ws_none.id,
            "null": ws_null_node.id,
        }
        for ws in (ws_pending, ws_resolved, ws_exhausted, ws_null_node):
            # In production a ``pending`` marker is always co-written with
            # ``terminal_runtime_released`` (same transaction), so the candidate is
            # effectively released. Mirror that here so the effective-release gate is
            # satisfied for the rows that should surface.
            await repo.add_event(
                ws,
                event_type=TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
                reason_code=TERMINAL_RUNTIME_RELEASE_REASON_CODE,
            )
            await repo.add_event(
                ws,
                event_type=worker_overlay._TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_EVENT_TYPE,
                reason_code=worker_overlay._TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_REASON_CODE,
                payload={"attempt": 1},
            )
        await repo.add_event(
            ws_resolved,
            event_type=worker_overlay._TERMINAL_AUTH_OVERLAY_UNMOUNT_RESOLVED_EVENT_TYPE,
            reason_code=worker_overlay._TERMINAL_AUTH_OVERLAY_UNMOUNT_RESOLVED_REASON_CODE,
        )
        await repo.add_event(
            ws_exhausted,
            event_type=worker_overlay._TERMINAL_AUTH_OVERLAY_UNMOUNT_EXHAUSTED_EVENT_TYPE,
            reason_code=worker_overlay._TERMINAL_AUTH_OVERLAY_UNMOUNT_EXHAUSTED_REASON_CODE,
        )
        await session.commit()

    # ``limit=None`` exercises the unbounded scan path (no ``.limit()`` clause).
    candidates = await sweeper._list_pending_terminal_auth_overlay_unmount_candidates(limit=None)  # noqa: SLF001
    candidate_ids = {c.workspace_id for c in candidates}
    assert ids["pending"] in candidate_ids
    assert ids["null"] in candidate_ids  # node_id IS NULL is still swept (single-node)
    assert ids["resolved"] not in candidate_ids
    assert ids["exhausted"] not in candidate_ids
    assert ids["none"] not in candidate_ids


@pytest.mark.asyncio
async def test_pending_candidate_query_excludes_revoked_release(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A pending overlay marker whose ``terminal_runtime_released`` was superseded by a
    later ``terminal_runtime_release_revoked`` (orphan containers still running) must
    NOT surface for a deferred umount retry: the overlay bind is still held, so a retry
    would burn the bounded sweeps and write a terminal marker that suppresses the retry
    owed once the runtime is genuinely released. The sibling row whose release is still
    effective continues to surface."""
    sweeper = _sweeper(factory)
    revoked_id: str = ""
    effective_id: str = ""
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws_revoked = await _make_workspace(session, repo, compose_project_name="awf_revoked")
        ws_effective = await _make_workspace(session, repo, compose_project_name="awf_effective")
        revoked_id = ws_revoked.id
        effective_id = ws_effective.id

        for ws in (ws_revoked, ws_effective):
            await repo.add_event(
                ws,
                event_type=TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
                reason_code=TERMINAL_RUNTIME_RELEASE_REASON_CODE,
            )
            await repo.add_event(
                ws,
                event_type=worker_overlay._TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_EVENT_TYPE,
                reason_code=worker_overlay._TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_REASON_CODE,
                payload={"attempt": 1},
            )

        # Order the revoked workspace's release before its revocation so the latest
        # release/revoke event is the revocation -> not effectively released.
        released_ev = (
            await session.execute(
                sa.select(WorkspaceEvent)
                .where(WorkspaceEvent.workspace_id == ws_revoked.id)
                .where(WorkspaceEvent.event_type == TERMINAL_RUNTIME_RELEASE_EVENT_TYPE)
            )
        ).scalar_one()
        released_ev.occurred_at = datetime(2026, 5, 31, 12, 0, 0, tzinfo=UTC)
        await repo.add_event(
            ws_revoked,
            event_type=TERMINAL_RUNTIME_RELEASE_REVOKED_EVENT_TYPE,
            reason_code=TERMINAL_RUNTIME_RELEASE_REVOKED_REASON_CODE,
        )
        revoked_ev = (
            await session.execute(
                sa.select(WorkspaceEvent)
                .where(WorkspaceEvent.workspace_id == ws_revoked.id)
                .where(WorkspaceEvent.event_type == TERMINAL_RUNTIME_RELEASE_REVOKED_EVENT_TYPE)
            )
        ).scalar_one()
        revoked_ev.occurred_at = datetime(2026, 5, 31, 12, 0, 1, tzinfo=UTC)
        await session.commit()

    candidates = await sweeper._list_pending_terminal_auth_overlay_unmount_candidates(limit=None)  # noqa: SLF001
    candidate_ids = {c.workspace_id for c in candidates}
    assert revoked_id not in candidate_ids, (
        "revoked release must keep the workspace out of the deferred overlay sweep"
    )
    assert effective_id in candidate_ids, (
        "effectively-released workspace with a pending marker must still surface"
    )


async def _reserve_on_node(
    session: AsyncSession,
    ws: Workspace,
    *,
    node_id: str,
) -> None:
    """Stamp *ws* with a ``ResourceReservation`` on *node_id* (and clear ``node_id``).

    Models a legacy unstamped row (``Workspace.node_id IS NULL``) whose effective
    owner is recoverable only from its reservation, exactly as the deferred-sweep
    candidate query now resolves it.
    """
    ws.node_id = None
    task = await TaskRepository(session).create_or_get(
        repo_url=ws.repo_url,
        base_branch=ws.branch_base,
        title=ws.task_title,
        prompt=ws.task_prompt,
        external_id=None,
        idempotency_key=None,
        task_class=ws.task_class,
        owned_paths=list(ws.owned_paths),
    )
    attempt = await TaskAttemptRepository(session).create_for_workspace(task=task, workspace=ws)
    await ResourceReservationRepository(session).create(
        workspace_id=ws.id,
        attempt_id=attempt.id,
        node_id=node_id,
        steady_cpu=1.0,
        steady_memory_gb=1.0,
        peak_cpu=1.0,
        peak_memory_gb=1.0,
        disk_mb=None,
        phase="workspace_lifecycle",
    )


@pytest.mark.asyncio
async def test_pending_candidate_query_uses_reservation_effective_node(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A legacy ``node_id IS NULL`` row is swept only by its reservation owner.

    Without the effective-node fallback the deferred overlay sweep would admit such
    a row on *every* worker; a non-owning worker would then tear down nothing in its
    own namespace and record a terminal ``resolved`` marker that permanently
    suppresses the owning worker's retry while the overlay stays leaked on the real
    node. Coalescing onto the active/latest reservation node keeps the row on the
    reservation owner; a row whose reservation is on a *different* node must not
    surface for this worker.
    """
    sweeper = _sweeper(factory, node_id="node-1")
    owned_id: str = ""
    foreign_id: str = ""
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws_owned = await _make_workspace(session, repo, compose_project_name="awf_owned")
        ws_foreign = await _make_workspace(session, repo, compose_project_name="awf_foreign")
        owned_id = ws_owned.id
        foreign_id = ws_foreign.id
        await _reserve_on_node(session, ws_owned, node_id="node-1")
        await _reserve_on_node(session, ws_foreign, node_id="node-2")
        for ws in (ws_owned, ws_foreign):
            await repo.add_event(
                ws,
                event_type=TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
                reason_code=TERMINAL_RUNTIME_RELEASE_REASON_CODE,
            )
            await repo.add_event(
                ws,
                event_type=worker_overlay._TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_EVENT_TYPE,
                reason_code=worker_overlay._TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_REASON_CODE,
                payload={"attempt": 1},
            )
        await session.commit()

    candidates = await sweeper._list_pending_terminal_auth_overlay_unmount_candidates(limit=None)  # noqa: SLF001
    candidate_ids = {c.workspace_id for c in candidates}
    assert owned_id in candidate_ids, (
        "a null-node row reserved on this worker's node must be swept by this worker"
    )
    assert foreign_id not in candidate_ids, (
        "a null-node row reserved on another node must not be swept by this worker"
    )


@pytest.mark.asyncio
async def test_count_pending_events_reflects_marker_count(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    sweeper = _sweeper(factory)
    ws_id: str = ""
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await _make_workspace(session, repo)
        ws_id = ws.id
        for attempt in range(3):
            await repo.add_event(
                ws,
                event_type=worker_overlay._TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_EVENT_TYPE,
                reason_code=worker_overlay._TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_REASON_CODE,
                payload={"attempt": attempt + 1},
            )
        await session.commit()

    count = await sweeper._count_terminal_auth_overlay_unmount_pending_events(ws_id)  # noqa: SLF001
    assert count == 3


# --------------------------------------------------------------------------- #
# 7b. Overlay markers are scoped to the current effective-release cycle
# --------------------------------------------------------------------------- #
#
# Markers are append-only and workspace-lifetime. A release that is revoked
# (``terminal_runtime_release_revoked``) and later genuinely re-released opens a *new*
# cycle: the agent container is recreated and the overlay is mounted afresh, so the new
# release owes its own full deferred-sweep budget. Without scoping, an earlier cycle's
# ``resolved``/``exhausted`` marker would exclude the workspace forever, and the earlier
# cycle's co-written ``pending`` markers would inflate the bound into a premature
# ``exhausted`` — so issue #399 retries never run for the new release.


async def _build_revoked_then_rereleased_cycle(
    repo: WorkspaceRepository,
    ws: Workspace,
    *,
    cycle1_pending: int = 1,
    cycle1_terminal: str | None = None,
) -> None:
    """Append a release→revoke→re-release history spanning two effective-release cycles.

    Cycle 1 records a ``terminal_runtime_released`` with *cycle1_pending* co-written
    ``pending`` markers and an optional terminal marker (*cycle1_terminal* is
    ``"resolved"`` or ``"exhausted"``), then a later ``terminal_runtime_release_revoked``
    that supersedes it. Cycle 2 records a still-later genuine ``terminal_runtime_released``
    (the latest release/revoke event → effectively released) with one fresh ``pending``
    marker. Only the cycle-2 marker falls at or after the latest release ``event_order``,
    so the current-cycle-scoped predicates must see exactly one pending and no terminal
    marker.
    """
    released_1 = await repo.add_event(
        ws,
        event_type=TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
        reason_code=TERMINAL_RUNTIME_RELEASE_REASON_CODE,
    )
    released_1.occurred_at = datetime(2026, 5, 31, 12, 0, 0, tzinfo=UTC)
    for _ in range(cycle1_pending):
        await repo.add_event(
            ws,
            event_type=worker_overlay._TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_EVENT_TYPE,
            reason_code=worker_overlay._TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_REASON_CODE,
            payload={"attempt": 1},
        )
    if cycle1_terminal == "resolved":
        await repo.add_event(
            ws,
            event_type=worker_overlay._TERMINAL_AUTH_OVERLAY_UNMOUNT_RESOLVED_EVENT_TYPE,
            reason_code=worker_overlay._TERMINAL_AUTH_OVERLAY_UNMOUNT_RESOLVED_REASON_CODE,
        )
    elif cycle1_terminal == "exhausted":
        await repo.add_event(
            ws,
            event_type=worker_overlay._TERMINAL_AUTH_OVERLAY_UNMOUNT_EXHAUSTED_EVENT_TYPE,
            reason_code=worker_overlay._TERMINAL_AUTH_OVERLAY_UNMOUNT_EXHAUSTED_REASON_CODE,
        )
    revoked = await repo.add_event(
        ws,
        event_type=TERMINAL_RUNTIME_RELEASE_REVOKED_EVENT_TYPE,
        reason_code=TERMINAL_RUNTIME_RELEASE_REVOKED_REASON_CODE,
    )
    revoked.occurred_at = datetime(2026, 5, 31, 12, 0, 1, tzinfo=UTC)
    released_2 = await repo.add_event(
        ws,
        event_type=TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
        reason_code=TERMINAL_RUNTIME_RELEASE_REASON_CODE,
    )
    released_2.occurred_at = datetime(2026, 5, 31, 12, 0, 2, tzinfo=UTC)
    await repo.add_event(
        ws,
        event_type=worker_overlay._TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_EVENT_TYPE,
        reason_code=worker_overlay._TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_REASON_CODE,
        payload={"attempt": 1},
    )


@pytest.mark.asyncio
async def test_pending_candidate_query_scopes_terminal_marker_to_current_cycle(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A ``resolved``/``exhausted`` marker from an earlier revoked-then-re-released cycle
    must NOT exclude the workspace from the current cycle's deferred sweep — the new
    release re-mounted the overlay and owes its own retry."""
    sweeper = _sweeper(factory)
    resolved_id: str = ""
    exhausted_id: str = ""
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws_resolved = await _make_workspace(
            session, repo, compose_project_name="awf_rereleased_resolved"
        )
        ws_exhausted = await _make_workspace(
            session, repo, compose_project_name="awf_rereleased_exhausted"
        )
        resolved_id = ws_resolved.id
        exhausted_id = ws_exhausted.id
        await _build_revoked_then_rereleased_cycle(repo, ws_resolved, cycle1_terminal="resolved")
        await _build_revoked_then_rereleased_cycle(repo, ws_exhausted, cycle1_terminal="exhausted")
        await session.commit()

    candidates = await sweeper._list_pending_terminal_auth_overlay_unmount_candidates(limit=None)  # noqa: SLF001
    candidate_ids = {c.workspace_id for c in candidates}
    assert resolved_id in candidate_ids, (
        "a stale resolved marker from a prior revoked cycle must not exclude the "
        "re-released workspace from the current deferred sweep"
    )
    assert exhausted_id in candidate_ids, (
        "a stale exhausted marker from a prior revoked cycle must not exclude the "
        "re-released workspace from the current deferred sweep"
    )


@pytest.mark.asyncio
async def test_count_pending_events_scoped_to_current_release_cycle(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The bounded-sweep count ignores ``pending`` markers co-written with an earlier
    (revoked) release, so a fresh release is not pushed straight to ``exhausted``."""
    sweeper = _sweeper(factory)
    ws_id: str = ""
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await _make_workspace(session, repo)
        ws_id = ws.id
        # Four stale cycle-1 pending markers (≥ the bound of 5 once the lone cycle-2
        # marker is added) must not count toward the current release's budget.
        await _build_revoked_then_rereleased_cycle(repo, ws, cycle1_pending=4)
        await session.commit()

    count = await sweeper._count_terminal_auth_overlay_unmount_pending_events(ws_id)  # noqa: SLF001
    assert count == 1, (
        "pending markers co-written with an earlier revoked release must not count "
        "toward the current release's bounded deferred-sweep budget"
    )


@pytest.mark.asyncio
async def test_has_terminal_marker_scoped_to_current_release_cycle(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The write-path terminal-marker guard ignores an earlier cycle's ``resolved``
    marker, so the current cycle can still record its own terminal marker (and the
    candidate query — equally scoped — does not re-list it forever)."""
    sweeper = _sweeper(factory)
    ws_id: str = ""
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await _make_workspace(session, repo)
        ws_id = ws.id
        await _build_revoked_then_rereleased_cycle(repo, ws, cycle1_terminal="resolved")
        await session.commit()

    async with factory() as session:
        assert (
            await sweeper._has_terminal_auth_overlay_unmount_terminal_event(session, ws_id)  # noqa: SLF001
        ) is False, (
            "a resolved marker from an earlier revoked cycle must not block the current "
            "cycle from recording its own terminal overlay-umount marker"
        )


def test_latest_release_event_order_expr_requires_exactly_one_selector() -> None:
    """The cycle-floor helper rejects ambiguous/empty selectors (same contract as the
    effective-release expression)."""
    with pytest.raises(ValueError, match="exactly one"):
        latest_terminal_runtime_release_event_order_expr()
    with pytest.raises(ValueError, match="exactly one"):
        latest_terminal_runtime_release_event_order_expr(
            correlated_to=Workspace, workspace_id="ws_x"
        )


@pytest.mark.asyncio
async def test_record_resolved_writes_event_and_dedupes(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The ``resolved`` marker is written once; a second call is a no-op under the
    terminal-marker idempotency guard."""
    sweeper = _sweeper(factory)
    ws_id: str = ""
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await _make_workspace(session, repo)
        await _mark_runtime_released(repo, ws)
        ws_id = ws.id
        await session.commit()

    candidate = _candidate(ws_id)
    await sweeper._record_terminal_auth_overlay_unmount_resolved(  # noqa: SLF001
        candidate, auth_overlay_unmounted=True
    )
    await sweeper._record_terminal_auth_overlay_unmount_resolved(  # noqa: SLF001
        candidate, auth_overlay_unmounted=True
    )

    async with factory() as session:
        types = await _event_types(session, ws_id)
    assert types.count(worker_overlay._TERMINAL_AUTH_OVERLAY_UNMOUNT_RESOLVED_EVENT_TYPE) == 1


@pytest.mark.asyncio
async def test_record_exhausted_writes_event_and_dedupes(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    sweeper = _sweeper(factory)
    ws_id: str = ""
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await _make_workspace(session, repo)
        await _mark_runtime_released(repo, ws)
        ws_id = ws.id
        await session.commit()

    candidate = _candidate(ws_id)
    await sweeper._record_terminal_auth_overlay_unmount_exhausted(candidate, attempts=5)  # noqa: SLF001
    await sweeper._record_terminal_auth_overlay_unmount_exhausted(candidate, attempts=5)  # noqa: SLF001

    async with factory() as session:
        types = await _event_types(session, ws_id)
    assert types.count(worker_overlay._TERMINAL_AUTH_OVERLAY_UNMOUNT_EXHAUSTED_EVENT_TYPE) == 1


@pytest.mark.asyncio
async def test_append_pending_writes_event_but_not_after_terminal_marker(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A fresh ``pending`` marker is appended normally, but once a terminal marker
    exists a concurrent append is suppressed (cannot resurrect the candidate)."""
    sweeper = _sweeper(factory)
    ws_id: str = ""
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await _make_workspace(session, repo)
        await _mark_runtime_released(repo, ws)
        ws_id = ws.id
        await session.commit()

    candidate = _candidate(ws_id)
    await sweeper._append_terminal_auth_overlay_unmount_pending(candidate, attempt=2)  # noqa: SLF001
    async with factory() as session:
        assert (await _event_types(session, ws_id)).count(
            worker_overlay._TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_EVENT_TYPE
        ) == 1

    # A terminal marker now exists -> the append guard suppresses a new pending.
    await sweeper._record_terminal_auth_overlay_unmount_resolved(  # noqa: SLF001
        candidate, auth_overlay_unmounted=True
    )
    await sweeper._append_terminal_auth_overlay_unmount_pending(candidate, attempt=3)  # noqa: SLF001
    async with factory() as session:
        assert (await _event_types(session, ws_id)).count(
            worker_overlay._TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_EVENT_TYPE
        ) == 1


def _prompt_releaser(factory: async_sessionmaker[AsyncSession], node_id: str = "node-1") -> Any:
    """Build a worker double wiring the prompt-path release recorder + its guards.

    Mirrors ``_sweeper`` but for ``_record_terminal_runtime_released`` and the
    duplicate-release overlay-marker seed guard it relies on, so the real SQL helper
    runs end-to-end against Postgres.
    """
    return type(
        "PromptReleaser",
        (),
        {
            "_config": WorkerConfig(node_id=node_id),
            "_session_factory": factory,
            "_log_transient_db_retry": lambda *_: None,
            "_has_terminal_runtime_release_event": (
                worker_cleanup._has_terminal_runtime_release_event
            ),
            "_has_current_cycle_terminal_auth_overlay_unmount_marker": (
                worker_overlay._has_current_cycle_terminal_auth_overlay_unmount_marker
            ),
            "_record_terminal_runtime_released": (worker_cleanup._record_terminal_runtime_released),
        },
    )()


@pytest.mark.asyncio
async def test_record_released_seeds_pending_overlay_on_preemitted_stop_stack_release(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A ``cancel --stop-stack`` pre-emits ``terminal_runtime_released`` after only a
    ``docker stop`` (no overlay work). The prompt path then runs ``compose down`` + the
    overlay umount; when that umount fails it reaches ``_record_terminal_runtime_released``
    with the release event already present. It must still seed exactly one ``pending``
    overlay marker so the deferred re-sweep has a candidate (the ~1.7 GB overlay no longer
    leaks), and a second invocation is idempotent — the release event is never re-written."""
    releaser = _prompt_releaser(factory)
    ws_id: str = ""
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await _make_workspace(session, repo)
        # Service pre-emit after ``docker stop`` only: a bare release, no overlay marker.
        await repo.add_event(
            ws,
            event_type=TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
            reason_code=TERMINAL_RUNTIME_RELEASE_REASON_CODE,
        )
        ws_id = ws.id
        await session.commit()

    candidate = _candidate(ws_id)
    for _ in range(2):
        await releaser._record_terminal_runtime_released(  # noqa: SLF001
            candidate,
            WorkspaceCleanupResult.skipped(),
            auth_overlay_unmounted=False,
        )

    async with factory() as session:
        types = await _event_types(session, ws_id)
    assert types.count(worker_overlay._TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_EVENT_TYPE) == 1
    assert types.count(TERMINAL_RUNTIME_RELEASE_EVENT_TYPE) == 1


@pytest.mark.asyncio
async def test_record_released_does_not_seed_pending_when_terminal_marker_present(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """When a current-cycle terminal (``resolved``) overlay marker already exists, the
    duplicate-release path does not seed a fresh ``pending`` that would resurrect the
    workspace as a deferred candidate after the sweep already concluded."""
    releaser = _prompt_releaser(factory)
    sweeper = _sweeper(factory)
    ws_id: str = ""
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await _make_workspace(session, repo)
        await _mark_runtime_released(repo, ws)
        ws_id = ws.id
        await session.commit()

    candidate = _candidate(ws_id)
    # The deferred sweep already resolved this cycle's overlay.
    await sweeper._record_terminal_auth_overlay_unmount_resolved(  # noqa: SLF001
        candidate, auth_overlay_unmounted=True
    )

    await releaser._record_terminal_runtime_released(  # noqa: SLF001
        candidate,
        WorkspaceCleanupResult.skipped(),
        auth_overlay_unmounted=False,
    )

    async with factory() as session:
        types = await _event_types(session, ws_id)
    assert worker_overlay._TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_EVENT_TYPE not in types


@pytest.mark.asyncio
async def test_record_helpers_skip_when_workspace_missing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Each record helper is a clean no-op when the locked row has vanished
    (``get_for_update`` returns ``None``) — never a crash, and nothing written."""
    sweeper = _sweeper(factory)
    candidate = _candidate("ws_missing")
    await sweeper._record_terminal_auth_overlay_unmount_resolved(  # noqa: SLF001
        candidate, auth_overlay_unmounted=True
    )
    await sweeper._record_terminal_auth_overlay_unmount_exhausted(  # noqa: SLF001
        candidate, attempts=5
    )
    await sweeper._append_terminal_auth_overlay_unmount_pending(  # noqa: SLF001
        candidate, attempt=2
    )

    async with factory() as session:
        assert await _event_types(session, "ws_missing") == []


@pytest.mark.asyncio
async def test_has_terminal_marker_detects_resolved(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    sweeper = _sweeper(factory)
    ws_id: str = ""
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await _make_workspace(session, repo)
        await _mark_runtime_released(repo, ws)
        ws_id = ws.id
        await session.commit()

    async with factory() as session:
        assert (
            await sweeper._has_terminal_auth_overlay_unmount_terminal_event(session, ws_id)  # noqa: SLF001
        ) is False

    await sweeper._record_terminal_auth_overlay_unmount_exhausted(  # noqa: SLF001
        _candidate(ws_id), attempts=5
    )
    async with factory() as session:
        assert (
            await sweeper._has_terminal_auth_overlay_unmount_terminal_event(session, ws_id)  # noqa: SLF001
        ) is True
