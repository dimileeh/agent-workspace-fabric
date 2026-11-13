"""The checkpoint contract matches the catalog of an actual Alembic-migrated schema."""

import asyncio

import pytest
from sqlalchemy import text

from awf.db.session import make_engine
from awf.hosted_node.checkpoint_contract import CONTRACT
from tests.postgres import postgres_empty_test_url
from tests.unit.hosted_node.test_lifecycle_migration import _migrate


@pytest.fixture
async def migrated():
    async with postgres_empty_test_url() as url:
        result = await asyncio.to_thread(_migrate, url, "upgrade", "head")
        assert result.returncode == 0, result.stderr.decode()[-2000:]
        engine = make_engine(url)
        try:
            async with engine.connect() as connection:
                yield connection
        finally:
            await engine.dispose()


async def _rows(connection, query):
    return [tuple(row) for row in (await connection.execute(text(query))).all()]


async def test_migrated_triggers_match_contract(migrated):
    rows = await _rows(
        migrated,
        "SELECT c.relname, t.tgname, t.tgenabled::text FROM pg_trigger t"
        " JOIN pg_class c ON c.oid = t.tgrelid"
        " WHERE c.relnamespace = current_schema()::regnamespace AND NOT t.tgisinternal"
        " ORDER BY 1, 2, 3",
    )
    assert tuple(rows) == CONTRACT.triggers


async def test_migrated_sequences_match_contract(migrated):
    rows = await _rows(
        migrated,
        "SELECT relname FROM pg_class"
        " WHERE relkind = 'S' AND relnamespace = current_schema()::regnamespace"
        " ORDER BY relname",
    )
    assert tuple(name for (name,) in rows) == CONTRACT.sequences


async def test_migrated_tables_and_revision_match_contract(migrated):
    rows = await _rows(
        migrated,
        "SELECT relname FROM pg_class"
        " WHERE relkind IN ('r', 'p') AND relnamespace = current_schema()::regnamespace"
        " ORDER BY relname",
    )
    assert tuple(name for (name,) in rows) == CONTRACT.tables
    version = await migrated.scalar(text("SELECT version_num FROM alembic_version"))
    assert version == CONTRACT.schema_revision
