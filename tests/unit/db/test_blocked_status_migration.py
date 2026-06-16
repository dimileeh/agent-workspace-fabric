"""Round-trip tests for the blocked-status / operator-grant migration.

Covers both legs the plan requires:

- SQLite: the migration DDL (``op.add_column`` / ``op.create_table`` /
  ``op.create_index`` and their inverses) applies and reverses cleanly.
- Postgres (the authoritative control-plane dialect): upgrade adds the columns +
  table, an existing pre-migration row backfills ``block_epoch`` to 0 with the
  column NOT NULL, and downgrade drops everything (correct inverse).
"""

from __future__ import annotations

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
    _BLOCKED_STATUS_REVISION,
    _SERVICE_GC_REQUESTS_REVISION,
    _run_alembic,
)

_BLOCKED_MIGRATION_FILENAME = "c1d2e3f4a5b6_blocked_status_and_operator_grants.py"
_NEW_WORKSPACE_COLUMNS = {
    "block_reason_code",
    "block_type",
    "block_violations",
    "block_resume_phase",
    "block_epoch",
    "blocked_at",
    "pending_operator_hint",
    "block_baseline_coverage",
    "block_planning_conformance_handoff",
}
_GRANT_TABLE = "operator_grant_audit_records"
_GRANT_COLUMNS = {
    "id",
    "workspace_id",
    "operator",
    "reason",
    "normalized_path",
    "block_epoch",
    "approve_policy_downgrade",
    "created_at",
    "consumed_at",
    "revoked_at",
}


def _load_blocked_migration() -> ModuleType:
    repo_root = Path(__file__).resolve().parents[3]
    migration_path = repo_root / "migrations" / "versions" / _BLOCKED_MIGRATION_FILENAME
    spec = importlib.util.spec_from_file_location("awf_blocked_status_migration", migration_path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"could not load migration module from {migration_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.unit
def test_blocked_migration_sqlite_round_trips() -> None:
    migration = _load_blocked_migration()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("CREATE TABLE workspaces (id VARCHAR(36) PRIMARY KEY)")
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                migration.upgrade()

            insp = inspect(conn)
            workspace_columns = {column["name"] for column in insp.get_columns("workspaces")}
            assert workspace_columns >= _NEW_WORKSPACE_COLUMNS
            assert _GRANT_TABLE in insp.get_table_names()
            grant_columns = {column["name"] for column in insp.get_columns(_GRANT_TABLE)}
            assert grant_columns == _GRANT_COLUMNS
            grant_indexes = {index["name"] for index in insp.get_indexes(_GRANT_TABLE)}
            assert {
                "ix_operator_grant_audit_records_workspace",
                "ix_operator_grant_audit_records_created_at",
            } <= grant_indexes

            with Operations.context(ctx):
                migration.downgrade()

            insp_after = inspect(conn)
            after_columns = {column["name"] for column in insp_after.get_columns("workspaces")}
            assert not (_NEW_WORKSPACE_COLUMNS & after_columns)
            assert _GRANT_TABLE not in insp_after.get_table_names()
    finally:
        engine.dispose()


@pytest.mark.unit
async def test_blocked_migration_postgres_round_trips_and_backfills_epoch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    async with postgres_empty_test_url() as database_url:
        env = {**os.environ, "AWF_DATABASE_URL": database_url}

        def _alembic(*args: str) -> None:
            _run_alembic(repo_root, env, *args)

        monkeypatch.chdir(repo_root)
        with postgres_alembic_subprocess_lock(database_url):
            # Pre-migration row: insert before the block columns exist so the
            # upgrade has to backfill block_epoch to its server_default.
            _alembic("upgrade", _SERVICE_GC_REQUESTS_REVISION)
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
                                'ws_block_backfill', 'running', 0,
                                'git@example.com:repo.git', 'main',
                                'in-flight row', 'do work', 'codex', '[]'::json,
                                false, '2026-06-01 00:00:00+00', '2026-06-01 00:00:00+00'
                            )
                            """
                        )
                    )
            finally:
                await engine.dispose()

            _alembic("upgrade", _BLOCKED_STATUS_REVISION)
            engine = make_engine(database_url)
            try:
                async with engine.connect() as conn:
                    epoch = await conn.scalar(
                        text("SELECT block_epoch FROM workspaces WHERE id = 'ws_block_backfill'")
                    )
                    after_upgrade_ws = await conn.run_sync(
                        lambda sync_conn: {
                            column["name"]: column["nullable"]
                            for column in inspect(sync_conn).get_columns("workspaces")
                        }
                    )
                    after_upgrade_tables = await conn.run_sync(
                        lambda sync_conn: set(inspect(sync_conn).get_table_names())
                    )
                    grant_columns = await conn.run_sync(
                        lambda sync_conn: {
                            column["name"]
                            for column in inspect(sync_conn).get_columns(_GRANT_TABLE)
                        }
                    )
            finally:
                await engine.dispose()

            _alembic("downgrade", _SERVICE_GC_REQUESTS_REVISION)
            engine = make_engine(database_url)
            try:
                async with engine.connect() as conn:
                    after_downgrade_ws = await conn.run_sync(
                        lambda sync_conn: {
                            column["name"]
                            for column in inspect(sync_conn).get_columns("workspaces")
                        }
                    )
                    after_downgrade_tables = await conn.run_sync(
                        lambda sync_conn: set(inspect(sync_conn).get_table_names())
                    )
            finally:
                await engine.dispose()

            # Re-upgrade must succeed (correct inverse).
            _alembic("upgrade", _BLOCKED_STATUS_REVISION)

    assert epoch == 0
    assert set(after_upgrade_ws) >= _NEW_WORKSPACE_COLUMNS
    assert after_upgrade_ws["block_epoch"] is False  # NOT NULL
    assert _GRANT_TABLE in after_upgrade_tables
    assert grant_columns == _GRANT_COLUMNS
    assert not (_NEW_WORKSPACE_COLUMNS & after_downgrade_ws)
    assert _GRANT_TABLE not in after_downgrade_tables
