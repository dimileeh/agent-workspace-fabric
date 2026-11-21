"""The lifecycle producer contract on actual current Alembic PostgreSQL schema."""

import asyncio

import pytest
from sqlalchemy import text

from awf.db.session import make_engine
from awf.hosted_node.lifecycle_barrier import GUARDED_TABLES
from tests.integration import test_hosted_lifecycle_parity as parity
from tests.postgres import postgres_empty_test_url
from tests.unit import test_hosted_runtime_authority as fixture_module
from tests.unit.hosted_node.test_lifecycle_migration import _migrate
from tests.unit.test_hosted_runtime_authority import authority as authority
from tests.unit.test_hosted_runtime_review_create import create_application as create_application
from tests.unit.test_hosted_runtime_review_create import create_client as create_client


@pytest.fixture
async def engine(monkeypatch):
    async with postgres_empty_test_url() as url:
        result = await asyncio.to_thread(_migrate, url, "upgrade", "head")
        assert result.returncode == 0, result.stderr.decode()[-2000:]
        database = make_engine(url)

        def existing_barrier(connection):
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version")) == "fa2c80d79e31"
            )
            assert connection.scalar(
                text(
                    "SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid WHERE t.tgname='awf_node_admission' AND c.relnamespace=current_schema()::regnamespace"
                )
            ) == len(GUARDED_TABLES)

        # The reusable fixture normally installs guards on ORM-created tables.
        # Here migrations already installed the actual guards: verify, do not replace.
        monkeypatch.setattr(fixture_module, "install_barrier", existing_barrier)
        try:
            yield database
        finally:
            await database.dispose()


@pytest.mark.parametrize("action", ["guide", "remonitor", "retry", "adopt"])
async def test_migrated_lifecycle_contract(
    authority, create_client, create_application, tmp_path, action
):
    if action in {"guide", "remonitor"}:
        await parity.test_hosted_existing_control_uses_current_authority_and_replays(
            authority, create_client, tmp_path, action
        )
    elif action == "retry":
        await parity.test_hosted_retry_new_association_and_lost_response_replay(
            authority, create_client, tmp_path
        )
    else:
        await parity.test_hosted_adopt_new_association_and_attach_exact_only(
            authority, create_client, create_application
        )


async def test_dormancy_migration_keeps_existing_monitor_and_defaults_to_unset():
    async with postgres_empty_test_url() as url:
        result = await asyncio.to_thread(_migrate, url, "upgrade", "9e3b7c41d2a6")
        assert result.returncode == 0, result.stderr.decode()[-2000:]
        database = make_engine(url)
        try:
            async with database.begin() as connection:
                await connection.execute(
                    text("""
                    INSERT INTO workspaces (
                        id, status, version, repo_url, branch_base, task_title, task_prompt,
                        agent, test_commands, requires_database, created_at, updated_at
                    ) VALUES (
                        'ws_existing', 'monitoring_pr', 7, 'https://example.test/repo.git',
                        'main', 'Existing monitor', 'Wait', 'codex', '[]'::json, false, now(), now()
                    )
                """)
                )
            result = await asyncio.to_thread(_migrate, url, "upgrade", "head")
            assert result.returncode == 0, result.stderr.decode()[-2000:]
            async with database.connect() as connection:
                existing = await connection.execute(
                    text("""
                    SELECT status, version, human_wait_dormant_after_seconds, dormant_at
                    FROM workspaces WHERE id = 'ws_existing'
                """)
                )
                assert existing.one() == ("monitoring_pr", 7, None, None)
                columns = await connection.execute(
                    text("""
                    SELECT column_name, data_type, is_nullable, column_default
                    FROM information_schema.columns
                    WHERE table_schema = current_schema() AND table_name = 'workspaces'
                    AND column_name IN ('human_wait_dormant_after_seconds', 'dormant_at')
                    ORDER BY column_name
                """)
                )
                assert columns.all() == [
                    ("dormant_at", "timestamp with time zone", "YES", None),
                    ("human_wait_dormant_after_seconds", "double precision", "YES", None),
                ]
        finally:
            await database.dispose()
