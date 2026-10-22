"""Filesystem completion marker migration round trips over existing rows."""

from __future__ import annotations

import ast
import importlib.util
import os
from datetime import UTC, datetime
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text

from awf.db.models import Workspace
from awf.db.session import make_engine
from tests.postgres import postgres_alembic_subprocess_lock, postgres_empty_test_url
from tests.unit.db.test_migration_graph import _run_alembic

REVISION = "a875b1c2d3e4"
PREVIOUS = "f1a2b3c4d5e6"
COLUMN = "filesystem_reclaimed_at"
ROOT = Path(__file__).resolve().parents[3]
MIGRATION = ROOT / "migrations" / "versions" / f"{REVISION}_workspace_filesystem_reclaimed_at.py"


def test_marker_model_is_nullable_and_timezone_aware() -> None:
    column = Workspace.__table__.c[COLUMN]
    assert column.nullable
    assert column.type.timezone


def test_marker_migration_has_only_column_operations() -> None:
    tree = ast.parse(MIGRATION.read_text())
    calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "op"
    }
    assert calls == {"add_column", "drop_column"}


def test_marker_migration_sqlite_round_trip() -> None:
    spec = importlib.util.spec_from_file_location("reclaimed_migration", MIGRATION)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE workspaces (id VARCHAR(36) PRIMARY KEY)")
        conn.exec_driver_sql("INSERT INTO workspaces (id) VALUES ('legacy')")
        ctx = MigrationContext.configure(conn)
        for _ in range(2):
            with Operations.context(ctx):
                migration.upgrade()
            columns = {col["name"]: col for col in inspect(conn).get_columns("workspaces")}
            assert columns[COLUMN]["nullable"]
            assert conn.execute(text(f"SELECT id, {COLUMN} FROM workspaces")).one() == (
                "legacy",
                None,
            )
            with Operations.context(ctx):
                migration.downgrade()
            assert COLUMN not in {col["name"] for col in inspect(conn).get_columns("workspaces")}
    engine.dispose()


async def test_marker_migration_postgres_round_trip() -> None:
    async with postgres_empty_test_url() as database_url:
        env = {**os.environ, "AWF_DATABASE_URL": database_url}
        with postgres_alembic_subprocess_lock(database_url):
            _run_alembic(ROOT, env, "upgrade", PREVIOUS)
            engine = make_engine(database_url)
            try:
                async with engine.begin() as conn:
                    await conn.execute(
                        text("""
                        INSERT INTO workspaces (
                            id, status, version, repo_url, branch_base, task_title,
                            task_prompt, agent, test_commands, requires_database,
                            created_at, updated_at
                        ) VALUES (
                            'ws_legacy', 'completed', 0, 'https://example.com/repo',
                            'main', 'legacy', 'task', 'codex', '[]'::json, false,
                            '2026-01-01 00:00:00+00', '2026-01-01 00:00:00+00'
                        )
                    """)
                    )
            finally:
                await engine.dispose()
            for _ in range(2):
                _run_alembic(ROOT, env, "upgrade", REVISION)
                engine = make_engine(database_url)
                try:
                    async with engine.connect() as conn:
                        columns = await conn.run_sync(
                            lambda c: {
                                col["name"]: col for col in inspect(c).get_columns("workspaces")
                            }
                        )
                        assert columns[COLUMN]["nullable"]
                        assert columns[COLUMN]["type"].timezone
                        row = (
                            await conn.execute(
                                text(
                                    f"SELECT status, {COLUMN}, updated_at FROM workspaces WHERE id='ws_legacy'"
                                )
                            )
                        ).one()
                        assert row.status == "completed"
                        assert row.filesystem_reclaimed_at is None
                        assert row.updated_at == datetime(2026, 1, 1, tzinfo=UTC)
                finally:
                    await engine.dispose()
                _run_alembic(ROOT, env, "downgrade", PREVIOUS)
                engine = make_engine(database_url)
                try:
                    async with engine.connect() as conn:
                        columns = await conn.run_sync(
                            lambda c: {col["name"] for col in inspect(c).get_columns("workspaces")}
                        )
                        assert COLUMN not in columns
                        assert (
                            await conn.scalar(text("SELECT task_title FROM workspaces")) == "legacy"
                        )
                finally:
                    await engine.dispose()
