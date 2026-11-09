"""Apply the actual forward migration to legacy SQL rows in isolated Postgres."""

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from awf.db.session import make_engine
from tests.postgres import postgres_empty_test_url


async def test_migration_preserves_history_and_rejects_lossy_downgrade():
    path = Path("migrations/versions/85ce3f9b2a61_provider_connection_scope.py")
    spec = importlib.util.spec_from_file_location("connection_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    async with postgres_empty_test_url() as url:
        engine = make_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "CREATE TABLE provider_model_circuit_breakers (id text PRIMARY KEY, provider text NOT NULL, model text NOT NULL, failure_count integer, updated_at timestamptz, CONSTRAINT uq_provider_model_circuit_breakers_pair UNIQUE(provider, model))"
                    )
                )
                await conn.execute(
                    text("CREATE TABLE worker_heartbeats (worker_id text PRIMARY KEY)")
                )
                await conn.execute(
                    text(
                        "INSERT INTO provider_model_circuit_breakers VALUES ('old','ollama','coder',7,'2026-01-01Z')"
                    )
                )
                await conn.execute(text("INSERT INTO worker_heartbeats VALUES ('old-worker')"))

                def migrate(sync):
                    with Operations.context(MigrationContext.configure(sync)):
                        module.upgrade()

                await conn.run_sync(migrate)
                assert (
                    await conn.execute(
                        text(
                            "SELECT connection_scope, failure_count, extract(year from updated_at) FROM provider_model_circuit_breakers"
                        )
                    )
                ).one() == ("legacy", 7, 2026)
                assert (
                    await conn.execute(
                        text("SELECT execution_contract_version FROM worker_heartbeats")
                    )
                ).scalar_one() == 0
                await conn.execute(
                    text(
                        "INSERT INTO provider_model_circuit_breakers (id,provider,model,connection_scope) VALUES ('new','ollama','coder','independent')"
                    )
                )

                def downgrade(sync):
                    with Operations.context(MigrationContext.configure(sync)):
                        module.downgrade()

                with pytest.raises(RuntimeError, match="DOWNGRADE_UNSAFE"):
                    await conn.run_sync(downgrade)
                assert (
                    await conn.execute(text("SELECT count(*) FROM provider_model_circuit_breakers"))
                ).scalar_one() == 2
                await conn.execute(
                    text("DELETE FROM provider_model_circuit_breakers WHERE id='new'")
                )
                await conn.run_sync(downgrade)
                assert (
                    await conn.execute(
                        text("SELECT failure_count FROM provider_model_circuit_breakers")
                    )
                ).scalar_one() == 7
        finally:
            await engine.dispose()
