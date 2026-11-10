"""``provider_managed_keys`` schema, repository and migration contracts.

Rows hold only non-secret managed-key identity (name, hash, seq, limit, usage);
key values are never persisted.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.ids import new_provider_managed_key_id
from awf.db.enums import ProviderManagedKeyStatus, WorkspaceStatus
from awf.db.models import ProviderManagedKey
from awf.db.repositories import ProviderManagedKeyRepository, WorkspaceRepository
from awf.db.session import make_engine, make_session_factory
from awf.hosted_node.checkpoint_cut import TABLES
from tests.postgres import (
    postgres_alembic_subprocess_lock,
    postgres_empty_test_url,
    postgres_test_engine,
)

from .test_migration_graph import _PROVIDER_CONNECTIONS_REVISION, _run_alembic

pytestmark = pytest.mark.unit

_REVISION = "9e3b7c41d2a6"
_COLUMNS = {
    "id",
    "workspace_id",
    "provider",
    "purpose",
    "seq",
    "key_name",
    "key_hash",
    "status",
    "limit_usd",
    "usage_usd",
    "byok_usage_usd",
    "usage_read_at",
    "final_usage_read",
    "lease_expires_at",
    "last_reason_code",
    "created_at",
    "updated_at",
    "deleted_at",
}


async def _workspace(factory: async_sessionmaker[AsyncSession]) -> str:
    async with factory() as session:
        ws = await WorkspaceRepository(session).create(
            repo_url="git@example.com:repo.git",
            branch_base="main",
            task_title="managed key",
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        await session.commit()
        return ws.id


def _row(workspace_id: str, seq: int, **overrides) -> ProviderManagedKey:
    now = datetime.now(UTC)
    values = {
        "id": new_provider_managed_key_id(),
        "workspace_id": workspace_id,
        "provider": "openrouter",
        "purpose": "agent",
        "seq": seq,
        "key_name": f"awf-{workspace_id}-{seq}",
        "key_hash": f"hash{workspace_id[-6:]}{seq}",
        "status": ProviderManagedKeyStatus.active.value,
        "limit_usd": Decimal("10"),
        "usage_usd": Decimal("0"),
        "byok_usage_usd": Decimal("0"),
        "created_at": now,
        "updated_at": now,
    }
    values.update(overrides)
    return ProviderManagedKey(**values)


def test_checkpoint_cut_lists_provider_managed_keys() -> None:
    assert "provider_managed_keys" in TABLES
    assert tuple(sorted(TABLES)) == TABLES


def test_key_ids_and_status_vocabulary() -> None:
    first, second = new_provider_managed_key_id(), new_provider_managed_key_id()
    assert first.startswith("pmk_") and first != second and len(first) <= 36
    assert {status.value for status in ProviderManagedKeyStatus} == {
        "minting",
        "active",
        "deleted",
        "delete_failed",
        "mint_failed",
        "orphan_deleted",
    }
    assert not ({status.value for status in ProviderManagedKeyStatus} & set(WorkspaceStatus))


async def test_repository_sums_spend_and_tracks_unfinalized_keys() -> None:
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        ws = await _workspace(factory)
        other = await _workspace(factory)
        read_at = datetime.now(UTC)
        async with factory() as session:
            repo = ProviderManagedKeyRepository(session)
            assert await repo.next_seq(ws) == 1
            await repo.add(
                _row(
                    ws,
                    1,
                    status="deleted",
                    usage_usd=Decimal("1.25"),
                    byok_usage_usd=Decimal("0.25"),
                    final_usage_read=True,
                    usage_read_at=read_at - timedelta(minutes=5),
                )
            )
            await repo.add(_row(ws, 2, status="delete_failed", usage_usd=Decimal("2")))
            await repo.add(
                _row(ws, 3, usage_usd=Decimal("0.5"), usage_read_at=read_at, key_hash=None)
            )
            await repo.add(_row(other, 1, status="mint_failed", key_hash=None))
            await session.commit()
        async with factory() as session:
            repo = ProviderManagedKeyRepository(session)
            assert await repo.next_seq(ws) == 4
            spend = await repo.spend_by_workspace_ids([ws, other, "ws_missing"])
            assert spend[ws].spend_usd == Decimal("4.0")
            assert spend[ws].key_count == 3
            assert spend[ws].live_key_count == 2
            assert spend[ws].last_read_at == read_at
            assert spend[other].spend_usd == Decimal("0")
            assert spend[other].live_key_count == 0
            assert "ws_missing" not in spend
            assert await repo.spend_by_workspace_ids([]) == {}
            assert [row.seq for row in await repo.for_workspace(ws)] == [1, 2, 3]
            assert [row.seq for row in await repo.unfinalized_for_workspace(ws)] == [2, 3]
            hashed = await repo.by_hashes([f"hash{ws[-6:]}1", "unknown"])
            assert list(hashed) == [f"hash{ws[-6:]}1"]
            assert await repo.by_hashes([]) == {}
            named = await repo.by_names([f"awf-{ws}-3"])
            assert named[f"awf-{ws}-3"].seq == 3
            assert await repo.by_names([]) == {}
            assert await repo.workspace_ids_with_unfinalized_keys(limit=10) == [ws]
            assert (await repo.get(named[f"awf-{ws}-3"].id)).seq == 3


async def test_seq_name_and_hash_are_unique() -> None:
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        ws = await _workspace(factory)
        async with factory() as session:
            await ProviderManagedKeyRepository(session).add(_row(ws, 1))
            await session.commit()
        for duplicate in (
            _row(ws, 1, key_name="other", key_hash="other"),
            _row(ws, 2, key_name=f"awf-{ws}-1", key_hash="other2"),
            _row(ws, 3, key_name="other3", key_hash=f"hash{ws[-6:]}1"),
        ):
            async with factory() as session:
                with pytest.raises(IntegrityError):
                    await ProviderManagedKeyRepository(session).add(duplicate)


async def test_migration_round_trips_and_refuses_lossy_downgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    async with postgres_empty_test_url() as database_url:
        env = {**os.environ, "AWF_DATABASE_URL": database_url}
        monkeypatch.chdir(repo_root)
        with postgres_alembic_subprocess_lock(database_url):
            _run_alembic(repo_root, env, "upgrade", _REVISION)
            engine = make_engine(database_url)
            try:
                async with engine.begin() as conn:
                    columns = await conn.run_sync(
                        lambda sync: {
                            c["name"]: c["nullable"]
                            for c in inspect(sync).get_columns("provider_managed_keys")
                        }
                    )
                    indexes = await conn.run_sync(
                        lambda sync: {
                            i["name"] for i in inspect(sync).get_indexes("provider_managed_keys")
                        }
                    )
                    uniques = await conn.run_sync(
                        lambda sync: {
                            tuple(u["column_names"])
                            for u in inspect(sync).get_unique_constraints("provider_managed_keys")
                        }
                    )
                    foreign_keys = await conn.run_sync(
                        lambda sync: inspect(sync).get_foreign_keys("provider_managed_keys")
                    )
                    await conn.execute(
                        text(
                            "INSERT INTO workspaces (id, repo_url, branch_base, status, "
                            "agent, task_title, task_prompt, test_commands, version, "
                            "requires_database, task_kind, monitor_iter_count, "
                            "monitor_threads_addressed, created_at, updated_at) VALUES "
                            "('ws_m', 'r', 'main', 'requested', 'codex', 't', 'p', '[]', 1, "
                            "false, 'feature_branch_pr', 0, '[]', now(), now())"
                        )
                    )
                    await conn.execute(
                        text(
                            "INSERT INTO provider_managed_keys (id, workspace_id, provider, "
                            "purpose, seq, key_name, status, limit_usd, created_at, updated_at) "
                            "VALUES ('pmk_1', 'ws_m', 'openrouter', 'agent', 1, 'awf-ws_m-1', "
                            "'minting', 10, now(), now())"
                        )
                    )
            finally:
                await engine.dispose()
            with pytest.raises(BaseException, match="DOWNGRADE_UNSAFE"):
                _run_alembic(repo_root, env, "downgrade", _PROVIDER_CONNECTIONS_REVISION)
            engine = make_engine(database_url)
            try:
                async with engine.begin() as conn:
                    await conn.execute(text("DELETE FROM provider_managed_keys"))
            finally:
                await engine.dispose()
            _run_alembic(repo_root, env, "downgrade", _PROVIDER_CONNECTIONS_REVISION)
            engine = make_engine(database_url)
            try:
                async with engine.connect() as conn:
                    tables = await conn.run_sync(lambda sync: set(inspect(sync).get_table_names()))
            finally:
                await engine.dispose()
            _run_alembic(repo_root, env, "upgrade", _REVISION)

    assert set(columns) == _COLUMNS
    assert {name for name, nullable in columns.items() if nullable} == {
        "key_hash",
        "usage_read_at",
        "lease_expires_at",
        "last_reason_code",
        "deleted_at",
    }
    assert {"ix_provider_managed_keys_workspace", "ix_provider_managed_keys_status"} <= indexes
    assert {("workspace_id", "seq"), ("key_name",), ("key_hash",)} <= uniques
    assert [
        (fk["referred_table"], fk["constrained_columns"], fk["options"].get("ondelete"))
        for fk in foreign_keys
    ] == [("workspaces", ["workspace_id"], "CASCADE")]
    assert "provider_managed_keys" not in tables
