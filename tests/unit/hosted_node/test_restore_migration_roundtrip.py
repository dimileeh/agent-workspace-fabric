"""PostgreSQL's dump CHECK definitions must reparse to the identical fingerprint."""

import asyncio
import subprocess
import sys

import pytest
from sqlalchemy import text

from awf.db.session import make_engine
from awf.hosted_node.checkpoint_cut import schema_fingerprint
from tests.postgres import postgres_empty_test_url
from tests.unit.hosted_node.test_lifecycle_migration import _migrate


@pytest.mark.parametrize("entrypoint", ["awf.hosted_node.lifecycle_models", "awf.db.models"])
def test_cold_model_import_registers_restore_foreign_key_targets(entrypoint):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f"""
import importlib
importlib.import_module({entrypoint!r})
from awf.db.base import Base
assert {{'hosted_node_lifecycle', 'hosted_restore_reconciliations', 'hosted_restore_row_holds'}} <= {{t.name for t in Base.metadata.sorted_tables}}
foreign = next(iter(Base.metadata.tables['hosted_node_lifecycle'].c.restore_reconciliation_id.foreign_keys))
assert foreign.column.table.name == 'hosted_restore_reconciliations'
""",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


async def test_migrated_restore_check_definitions_have_stable_pg_dump_roundtrip():
    async with postgres_empty_test_url() as url:
        result = await asyncio.to_thread(_migrate, url, "upgrade", "head")
        assert result.returncode == 0, result.stderr.decode()[-2000:]
        engine = make_engine(url)
        try:
            async with engine.begin() as connection:
                raw = await connection.get_raw_connection()
                driver = raw.driver_connection
                before = await schema_fingerprint(driver)
                query = text("""
                    SELECT c.relname, x.conname, pg_get_constraintdef(x.oid)
                    FROM pg_constraint x JOIN pg_class c ON c.oid=x.conrelid
                    WHERE c.relnamespace=current_schema()::regnamespace AND x.contype='c'
                      AND c.relname IN ('hosted_restore_reconciliations','hosted_restore_row_holds')
                    ORDER BY c.relname,x.conname
                """)
                definitions = (await connection.execute(query)).all()
                assert len(definitions) == 6
                # This is pg_dump's exact definition parser round trip, without
                # needing a client-version-matched pg_dump executable in Core CI.
                for table, name, definition in definitions:
                    await connection.execute(
                        text(f'ALTER TABLE "{table}" DROP CONSTRAINT "{name}"')
                    )
                    await connection.execute(
                        text(f'ALTER TABLE "{table}" ADD CONSTRAINT "{name}" {definition}')
                    )
                assert (await connection.execute(query)).all() == definitions
                assert await schema_fingerprint(driver) == before
        finally:
            await engine.dispose()
