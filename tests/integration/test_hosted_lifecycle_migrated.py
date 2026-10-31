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
                connection.scalar(text("SELECT version_num FROM alembic_version")) == "b555242e9032"
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
