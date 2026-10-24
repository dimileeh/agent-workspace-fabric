"""Separate least-privilege login on a disposable real PostgreSQL database."""

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from awf.db.models import Base, Task
from awf.db.session import make_session_factory
from awf.hosted_node.host_database import no_work_history, validate_database
from awf.hosted_node.lifecycle_barrier import install_barrier
from awf.hosted_node.lifecycle_store import database_quiescent
from tests.postgres import postgres_test_database_url
from tests.unit.hosted_node.test_host_config import host_config


async def test_bootstrap_history_checks_beyond_empty_workspaces(engine):
    factory = make_session_factory(engine)
    async with factory.begin() as session:
        assert await no_work_history(session)
        session.add(
            Task(
                id=str(uuid4()),
                repo_url="https://example.test/repo.git",
                base_branch="main",
                title="Unstarted prior task",
                prompt="Synthetic task history without a workspace or attempt",
            )
        )
    async with factory() as session:
        assert await session.scalar(text("SELECT count(*) FROM workspaces")) == 0
        assert await database_quiescent(session)
        assert not await no_work_history(session)


@pytest.fixture
async def runtime_database():
    from sqlalchemy.engine import make_url

    suffix = uuid4().hex
    name, role = "host_db_" + suffix, "host_app_" + suffix
    url = make_url(postgres_test_database_url())
    admin = create_async_engine(url, isolation_level="AUTOCOMMIT")
    migration = runtime = None
    try:
        async with admin.connect() as conn:
            await conn.execute(
                text(f"CREATE ROLE \"{role}\" LOGIN PASSWORD 'synthetic-runtime-only'")
            )
            await conn.execute(text(f'CREATE DATABASE "{name}"'))
            await conn.execute(text(f'REVOKE ALL ON DATABASE "{name}" FROM PUBLIC'))
            await conn.execute(text(f'GRANT CONNECT ON DATABASE "{name}" TO "{role}"'))
        migration = create_async_engine(url.set(database=name))
        async with migration.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(install_barrier)
            await conn.execute(text("REVOKE ALL ON SCHEMA public FROM PUBLIC"))
            await conn.execute(text(f'GRANT USAGE ON SCHEMA public TO "{role}"'))
            await conn.execute(
                text(
                    f'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO "{role}"'
                )
            )
            await conn.execute(
                text(f'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO "{role}"')
            )
        runtime = create_async_engine(
            url.set(database=name, username=role, password="synthetic-runtime-only"),
            connect_args={"server_settings": {"search_path": "public"}},
        )
        config = host_config(database_name=name, database_role=role)
        yield make_session_factory(runtime), config, migration, runtime
    finally:
        if runtime:
            await runtime.dispose()
        if migration:
            await migration.dispose()
        async with admin.connect() as conn:
            await conn.execute(
                text(f'REVOKE SET ON PARAMETER session_replication_role FROM "{role}"')
            )
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
            await conn.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
        await admin.dispose()


async def test_runtime_database_is_bound_without_ddl(runtime_database):
    factory, config, _, _ = runtime_database
    async with factory() as session:
        await validate_database(session, config)
    for change in (
        {"database_name": "wrong"},
        {"database_role": "wrong"},
        {"database_schema": "wrong"},
    ):
        async with factory() as session:
            with pytest.raises(ValueError, match="HOST_DATABASE_UNSAFE"):
                await validate_database(session, config.model_copy(update=change))


@pytest.mark.parametrize(
    "privilege",
    [
        "schema",
        "temporary",
        "truncate",
        "trigger",
        "owner",
        "bypassrls",
        "createdb",
        "createrole",
        "replication",
        "membership",
        "parameter",
        "security_definer",
        "database_create",
        "superuser",
    ],
)
async def test_elevated_runtime_fails_closed(runtime_database, privilege):
    factory, config, migration, _ = runtime_database
    role, name = config.database_role, config.database_name
    statements = {
        "schema": f'GRANT CREATE ON SCHEMA public TO "{role}"',
        "temporary": f'GRANT TEMP ON DATABASE "{name}" TO "{role}"',
        "truncate": f'GRANT TRUNCATE ON workspaces TO "{role}"',
        "trigger": f'GRANT TRIGGER ON workspaces TO "{role}"',
        "owner": f'ALTER TABLE workspaces OWNER TO "{role}"',
        "bypassrls": f'ALTER ROLE "{role}" BYPASSRLS',
        "createdb": f'ALTER ROLE "{role}" CREATEDB',
        "createrole": f'ALTER ROLE "{role}" CREATEROLE',
        "replication": f'ALTER ROLE "{role}" REPLICATION',
        "membership": f'GRANT pg_read_all_data TO "{role}"',
        "parameter": f'GRANT SET ON PARAMETER session_replication_role TO "{role}"',
        "security_definer": "CREATE FUNCTION host_privileged() RETURNS integer LANGUAGE SQL SECURITY DEFINER AS 'SELECT 1'",
        "database_create": f'GRANT CREATE ON DATABASE "{name}" TO "{role}"',
        "superuser": f'ALTER ROLE "{role}" SUPERUSER',
    }
    async with migration.begin() as conn:
        await conn.execute(text(statements[privilege]))
    async with factory() as session:
        with pytest.raises(ValueError, match="HOST_DATABASE_UNSAFE"):
            await validate_database(session, config)
    if privilege == "parameter":
        async with migration.begin() as conn:
            await conn.execute(
                text(f'REVOKE SET ON PARAMETER session_replication_role FROM "{role}"')
            )


async def test_migration_login_is_not_a_runtime_login(runtime_database):
    _, config, migration, _ = runtime_database
    async with make_session_factory(migration)() as session:
        with pytest.raises(ValueError, match="HOST_DATABASE_UNSAFE"):
            await validate_database(session, config)


async def test_qualified_security_definer_outside_search_path_is_rejected(runtime_database):
    factory, config, migration, _ = runtime_database
    async with migration.begin() as conn:
        await conn.execute(text("CREATE SCHEMA extras"))
        await conn.execute(text("GRANT USAGE ON SCHEMA extras TO PUBLIC"))
        await conn.execute(
            text("""CREATE FUNCTION extras.elevated_login() RETURNS name
            LANGUAGE SQL SECURITY DEFINER AS 'SELECT current_user'""")
        )
        privileged = await conn.scalar(text("SELECT current_user"))
    async with factory() as session:
        assert await session.scalar(text("SELECT current_schemas(false)")) == ["public"]
        assert await session.scalar(text("SELECT extras.elevated_login()")) == privileged
        with pytest.raises(ValueError, match="HOST_DATABASE_UNSAFE"):
            await validate_database(session, config)
    # Merely existing routines in inaccessible schemas are not authority.
    async with migration.begin() as conn:
        await conn.execute(text("REVOKE USAGE ON SCHEMA extras FROM PUBLIC"))
    async with factory() as session:
        await validate_database(session, config)
