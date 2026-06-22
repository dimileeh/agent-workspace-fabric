"""Round-trip tests for the awaiting-human attention-flag migration.

Covers both legs the plan requires:

- SQLite: the metadata-only ``op.add_column`` / ``op.drop_column`` ops apply and
  reverse cleanly.
- Postgres (the authoritative control-plane dialect): upgrade adds the two
  nullable columns over a pre-existing row, downgrade drops them, and re-upgrade
  succeeds (correct inverse).
"""

from __future__ import annotations

import ast
import importlib.util
import os
from pathlib import Path
from types import ModuleType

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text

from awf.db.session import make_engine
from tests.postgres import postgres_alembic_subprocess_lock, postgres_empty_test_url

from .test_migration_graph import (
    _AWAITING_HUMAN_ATTENTION_REVISION,
    _BLOCKED_STATUS_REVISION,
    _run_alembic,
)

_MIGRATION_FILENAME = "d7e8f9a0b1c2_workspace_awaiting_human_attention.py"
_NEW_COLUMNS = {"awaiting_human_since", "awaiting_human_reason"}


def _load_migration() -> ModuleType:
    repo_root = Path(__file__).resolve().parents[3]
    migration_path = repo_root / "migrations" / "versions" / _MIGRATION_FILENAME
    spec = importlib.util.spec_from_file_location(
        "awf_awaiting_human_attention_migration", migration_path
    )
    if spec is None or spec.loader is None:
        raise AssertionError(f"could not load migration module from {migration_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.unit
def test_awaiting_human_migration_uses_metadata_only_column_ops() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    migration_path = repo_root / "migrations" / "versions" / _MIGRATION_FILENAME
    tree = ast.parse(migration_path.read_text(encoding="utf-8"))

    call_nodes = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    call_attrs = [node.func.attr for node in call_nodes if isinstance(node.func, ast.Attribute)]
    op_call_attrs = {
        node.func.attr
        for node in call_nodes
        if isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "op"
    }

    assert "batch_alter_table" not in call_attrs
    assert op_call_attrs == {"add_column", "drop_column"}


@pytest.mark.unit
def test_awaiting_human_migration_sqlite_round_trips() -> None:
    migration = _load_migration()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("CREATE TABLE workspaces (id VARCHAR(36) PRIMARY KEY)")
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                migration.upgrade()

            insp = inspect(conn)
            columns = {column["name"] for column in insp.get_columns("workspaces")}
            assert columns >= _NEW_COLUMNS
            nullable = {
                column["name"]: column["nullable"] for column in insp.get_columns("workspaces")
            }
            assert nullable["awaiting_human_since"] is True
            assert nullable["awaiting_human_reason"] is True

            with Operations.context(ctx):
                migration.downgrade()

            insp_after = inspect(conn)
            after_columns = {column["name"] for column in insp_after.get_columns("workspaces")}
            assert not (_NEW_COLUMNS & after_columns)
    finally:
        engine.dispose()


@pytest.mark.unit
async def test_awaiting_human_migration_postgres_round_trips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    async with postgres_empty_test_url() as database_url:
        env = {**os.environ, "AWF_DATABASE_URL": database_url}

        def _alembic(*args: str) -> None:
            _run_alembic(repo_root, env, *args)

        monkeypatch.chdir(repo_root)
        with postgres_alembic_subprocess_lock(database_url):
            # Pre-migration row: insert before the columns exist so the upgrade
            # adds them as nullable (no backfill needed).
            _alembic("upgrade", _BLOCKED_STATUS_REVISION)
            engine = make_engine(database_url)
            try:
                async with engine.begin() as conn:
                    await conn.execute(
                        text(
                            """
                            INSERT INTO workspaces (
                                id, status, version, repo_url, branch_base,
                                task_title, task_prompt, agent, test_commands,
                                requires_database, created_at, updated_at
                            )
                            VALUES (
                                'ws_awaiting_human', 'monitoring_pr', 0,
                                'git@example.com:repo.git', 'main',
                                'in-flight row', 'do work', 'codex', '[]'::json,
                                false, '2026-06-01 00:00:00+00', '2026-06-01 00:00:00+00'
                            )
                            """
                        )
                    )
            finally:
                await engine.dispose()

            _alembic("upgrade", _AWAITING_HUMAN_ATTENTION_REVISION)
            engine = make_engine(database_url)
            try:
                async with engine.connect() as conn:
                    after_upgrade = await conn.run_sync(
                        lambda sync_conn: {
                            column["name"]: column["nullable"]
                            for column in inspect(sync_conn).get_columns("workspaces")
                        }
                    )
                    existing_value = await conn.scalar(
                        text(
                            "SELECT awaiting_human_since FROM workspaces "
                            "WHERE id = 'ws_awaiting_human'"
                        )
                    )
            finally:
                await engine.dispose()

            _alembic("downgrade", _BLOCKED_STATUS_REVISION)
            engine = make_engine(database_url)
            try:
                async with engine.connect() as conn:
                    after_downgrade = await conn.run_sync(
                        lambda sync_conn: {
                            column["name"]
                            for column in inspect(sync_conn).get_columns("workspaces")
                        }
                    )
            finally:
                await engine.dispose()

            # Re-upgrade must succeed (correct inverse).
            _alembic("upgrade", _AWAITING_HUMAN_ATTENTION_REVISION)

    assert set(after_upgrade) >= _NEW_COLUMNS
    assert after_upgrade["awaiting_human_since"] is True  # nullable
    assert after_upgrade["awaiting_human_reason"] is True  # nullable
    assert existing_value is None
    assert not (_NEW_COLUMNS & after_downgrade)
