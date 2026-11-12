"""``WorkSlotLeaseRepository`` and ``work_slot_leases`` schema tests (slot-release).

A work slot means provisioning, an agent run or a validation run is in flight right
now. The broker persists each slot as a ``work_slot_leases`` row; these tests pin
the schema contract (columns, the at-most-one-held-lease-per-workspace backstop,
cascade with the workspace row via the FK) and the repository's flush-only query helpers.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.ids import new_work_slot_invocation_id
from awf.db.enums import WorkSlotLeasePhase, WorkspaceStatus
from awf.db.models import WorkSlotLease
from awf.db.repositories import WorkSlotLeaseRepository, WorkspaceRepository
from awf.db.session import make_engine, make_session_factory
from awf.hosted_node.checkpoint_cut import TABLES
from tests.postgres import (
    postgres_alembic_subprocess_lock,
    postgres_empty_test_url,
    postgres_test_engine,
)

from .test_migration_graph import (
    _BARRIER_IDLE_CLEAR_ONLY_REVISION,
    _WORK_SLOT_LEASES_REVISION,
    _run_alembic,
)

pytestmark = pytest.mark.unit

_LEASE_COLUMNS = {
    "workspace_id",
    "invocation_id",
    "holder_worker_id",
    "holder_epoch",
    "ticket_at",
    "acquired_at",
    "expires_at",
    "phase",
    "cleanup_unproven",
}


async def _create_workspace(
    factory: async_sessionmaker[AsyncSession],
    *,
    node_id: str | None = None,
) -> str:
    async with factory() as session:
        ws = await WorkspaceRepository(session).create(
            repo_url="git@example.com:repo.git",
            branch_base="main",
            task_title="lease",
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        ws.node_id = node_id
        await session.commit()
        return ws.id


def _lease(
    workspace_id: str,
    *,
    phase: WorkSlotLeasePhase,
    now: datetime,
    holder: str = "control-worker-a",
    ticket_offset: float = 0.0,
    expires_in: float = 300.0,
) -> WorkSlotLease:
    held = phase in {WorkSlotLeasePhase.held, WorkSlotLeasePhase.adopted}
    return WorkSlotLease(
        invocation_id=new_work_slot_invocation_id(),
        workspace_id=workspace_id,
        holder_worker_id=holder,
        holder_epoch=1,
        ticket_at=now + timedelta(seconds=ticket_offset),
        acquired_at=now if held else None,
        expires_at=now + timedelta(seconds=expires_in),
        phase=phase.value,
        cleanup_unproven=False,
    )


def test_checkpoint_cut_lists_work_slot_leases() -> None:
    # The hosted checkpoint validator requires every table to be listed, otherwise
    # a cut taken with live leases fails BARRIER_REQUIRED.
    assert "work_slot_leases" in TABLES
    assert tuple(sorted(TABLES)) == TABLES


def test_work_slot_invocation_ids_are_shell_safe_and_unique() -> None:
    first = new_work_slot_invocation_id()
    second = new_work_slot_invocation_id()
    assert first.startswith("wsl_")
    assert first != second
    assert all(ch.isalnum() or ch in "_.-" for ch in first)
    assert len(first) <= 64


async def test_repository_round_trips_and_scopes_leases_to_node() -> None:
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        local_ws = await _create_workspace(factory, node_id="node-a")
        legacy_ws = await _create_workspace(factory, node_id=None)
        other_ws = await _create_workspace(factory, node_id="node-b")
        now = datetime.now(UTC)
        async with factory() as session:
            repo = WorkSlotLeaseRepository(session)
            held = await repo.add(_lease(local_ws, phase=WorkSlotLeasePhase.held, now=now))
            waiting = await repo.add(
                _lease(legacy_ws, phase=WorkSlotLeasePhase.waiting, now=now, ticket_offset=1)
            )
            await repo.add(_lease(other_ws, phase=WorkSlotLeasePhase.held, now=now))
            await session.commit()

        async with factory() as session:
            repo = WorkSlotLeaseRepository(session)
            fetched = await repo.get(held.invocation_id)
            assert fetched is not None
            assert fetched.holder_epoch == 1
            assert fetched.cleanup_unproven is False
            scoped = await repo.list_for_node(node_id="node-a")
            assert [lease.invocation_id for lease in scoped] == [
                held.invocation_id,
                waiting.invocation_id,
            ]
            assert await repo.count_in_use(node_id="node-a") == 1
            assert await repo.count_in_use(node_id="node-b") == 1
            assert await repo.held_for_workspace(local_ws) is not None
            assert await repo.held_for_workspace(legacy_ws) is None
            everything = await repo.list_for_node(node_id=None)
            assert len(everything) == 3


async def test_partial_unique_index_rejects_second_held_lease_for_workspace() -> None:
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        workspace_id = await _create_workspace(factory)
        now = datetime.now(UTC)
        async with factory() as session:
            repo = WorkSlotLeaseRepository(session)
            await repo.add(_lease(workspace_id, phase=WorkSlotLeasePhase.held, now=now))
            # Any number of not-yet-acquired rows may coexist with the held one.
            await repo.add(_lease(workspace_id, phase=WorkSlotLeasePhase.waiting, now=now))
            await session.commit()
        async with factory() as session:
            repo = WorkSlotLeaseRepository(session)
            with pytest.raises(IntegrityError):
                await repo.add(_lease(workspace_id, phase=WorkSlotLeasePhase.adopted, now=now))


async def test_fenced_update_and_delete_require_holder_and_epoch() -> None:
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        workspace_id = await _create_workspace(factory)
        now = datetime.now(UTC)
        async with factory() as session:
            lease = await WorkSlotLeaseRepository(session).add(
                _lease(workspace_id, phase=WorkSlotLeasePhase.held, now=now)
            )
            invocation_id = lease.invocation_id
            await session.commit()
        async with factory() as session:
            repo = WorkSlotLeaseRepository(session)
            later = now + timedelta(seconds=600)
            assert not await repo.update_fenced(
                invocation_id,
                holder_worker_id="control-worker-a",
                holder_epoch=2,
                values={"expires_at": later},
            )
            assert not await repo.delete_fenced(
                invocation_id, holder_worker_id="control-worker-b", holder_epoch=1
            )
            assert await repo.update_fenced(
                invocation_id,
                holder_worker_id="control-worker-a",
                holder_epoch=1,
                values={"expires_at": later},
            )
            await session.commit()
        async with factory() as session:
            repo = WorkSlotLeaseRepository(session)
            fetched = await repo.get(invocation_id)
            assert fetched is not None
            assert fetched.expires_at == later
            assert await repo.delete_fenced(
                invocation_id, holder_worker_id="control-worker-a", holder_epoch=1
            )
            await session.commit()
        async with factory() as session:
            assert await WorkSlotLeaseRepository(session).get(invocation_id) is None


async def test_fifo_waiters_skip_expired_and_unsatisfiable_rows() -> None:
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        ids = [await _create_workspace(factory) for _ in range(4)]
        now = datetime.now(UTC)
        async with factory() as session:
            repo = WorkSlotLeaseRepository(session)
            expired = await repo.add(
                _lease(ids[0], phase=WorkSlotLeasePhase.waiting, now=now, expires_in=-1)
            )
            unsatisfiable = await repo.add(
                _lease(ids[1], phase=WorkSlotLeasePhase.unsatisfiable, now=now, ticket_offset=1)
            )
            head = await repo.add(
                _lease(ids[2], phase=WorkSlotLeasePhase.waiting, now=now, ticket_offset=2)
            )
            tail = await repo.add(
                _lease(ids[3], phase=WorkSlotLeasePhase.waiting, now=now, ticket_offset=3)
            )
            await session.commit()
        async with factory() as session:
            repo = WorkSlotLeaseRepository(session)
            first = await repo.fifo_head(node_id="local", now=now)
            assert first is not None
            assert first.invocation_id == head.invocation_id
            assert await repo.count_live_waiters(node_id="local", now=now) == 2
            deleted = await repo.delete_expired_waiters(node_id="local", now=now)
            assert deleted == [expired.invocation_id]
            await session.commit()
        async with factory() as session:
            repo = WorkSlotLeaseRepository(session)
            assert await repo.get(expired.invocation_id) is None
            assert await repo.get(unsatisfiable.invocation_id) is not None
            assert await repo.get(tail.invocation_id) is not None


async def test_delete_expired_waiters_keeps_ticket_renewed_after_the_scan() -> None:
    # The reconciler scans without the capacity lock: a waiter renewed between the
    # scan and the delete keeps its row and its original FIFO ticket.
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        workspace_id = await _create_workspace(factory)
        now = datetime.now(UTC)
        async with factory() as session:
            renewed = await WorkSlotLeaseRepository(session).add(
                _lease(workspace_id, phase=WorkSlotLeasePhase.waiting, now=now, expires_in=-1)
            )
            await session.commit()
        ticket_at = renewed.ticket_at
        async with factory() as session:
            original_execute = session.execute
            calls = 0

            async def _renew_after_scan(*args: object, **kwargs: object) -> object:
                nonlocal calls
                result = await original_execute(*args, **kwargs)  # type: ignore[arg-type]
                calls += 1
                if calls == 1:
                    async with factory() as other:
                        assert await WorkSlotLeaseRepository(other).update_fenced(
                            renewed.invocation_id,
                            holder_worker_id="control-worker-a",
                            holder_epoch=1,
                            values={"expires_at": now + timedelta(seconds=300)},
                        )
                        await other.commit()
                return result

            session.execute = _renew_after_scan  # type: ignore[method-assign]
            deleted = await WorkSlotLeaseRepository(session).delete_expired_waiters(
                node_id="local", now=now
            )
            await session.commit()
        assert deleted == []
        async with factory() as session:
            kept = await WorkSlotLeaseRepository(session).get(renewed.invocation_id)
            assert kept is not None
            assert kept.ticket_at == ticket_at


def test_lease_phase_vocabulary_is_not_a_workspace_status() -> None:
    # The lease phase is a lease-lifecycle vocabulary, never a workspace status.
    assert {phase.value for phase in WorkSlotLeasePhase} == {
        "waiting",
        "unsatisfiable",
        "held",
        "adopted",
    }
    assert not ({phase.value for phase in WorkSlotLeasePhase} & set(WorkspaceStatus))


async def test_work_slot_leases_migration_postgres_round_trips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    async with postgres_empty_test_url() as database_url:
        env = {**os.environ, "AWF_DATABASE_URL": database_url}
        monkeypatch.chdir(repo_root)
        with postgres_alembic_subprocess_lock(database_url):
            _run_alembic(repo_root, env, "upgrade", _WORK_SLOT_LEASES_REVISION)
            engine = make_engine(database_url)
            try:
                async with engine.connect() as conn:
                    columns = await conn.run_sync(
                        lambda sync_conn: {
                            column["name"]: column["nullable"]
                            for column in inspect(sync_conn).get_columns("work_slot_leases")
                        }
                    )
                    indexes = await conn.run_sync(
                        lambda sync_conn: {
                            index["name"]
                            for index in inspect(sync_conn).get_indexes("work_slot_leases")
                        }
                    )
                    foreign_keys = await conn.run_sync(
                        lambda sync_conn: inspect(sync_conn).get_foreign_keys("work_slot_leases")
                    )
                    held_index_definition = await conn.scalar(
                        text(
                            "SELECT indexdef FROM pg_indexes "
                            "WHERE indexname = 'uq_work_slot_leases_workspace_held' "
                            "AND schemaname = current_schema()"
                        )
                    )
            finally:
                await engine.dispose()
            _run_alembic(repo_root, env, "downgrade", _BARRIER_IDLE_CLEAR_ONLY_REVISION)
            engine = make_engine(database_url)
            try:
                async with engine.connect() as conn:
                    tables_after_downgrade = await conn.run_sync(
                        lambda sync_conn: set(inspect(sync_conn).get_table_names())
                    )
            finally:
                await engine.dispose()
            # Re-upgrade must succeed (correct inverse).
            _run_alembic(repo_root, env, "upgrade", _WORK_SLOT_LEASES_REVISION)

    assert set(columns) == _LEASE_COLUMNS
    assert columns["acquired_at"] is True
    assert all(nullable is False for name, nullable in columns.items() if name != "acquired_at")
    assert {
        "ix_work_slot_leases_workspace",
        "ix_work_slot_leases_phase_ticket",
        "uq_work_slot_leases_workspace_held",
    } <= indexes
    assert held_index_definition is not None
    assert "UNIQUE" in held_index_definition
    assert "acquired_at IS NOT NULL" in held_index_definition
    # Lease rows never outlive their workspace row.
    assert [
        (fk["referred_table"], fk["constrained_columns"], fk["options"].get("ondelete"))
        for fk in foreign_keys
    ] == [("workspaces", ["workspace_id"], "CASCADE")]
    assert "work_slot_leases" not in tables_after_downgrade
