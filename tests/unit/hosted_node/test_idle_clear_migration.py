"""Upgraded databases converge on the live admission barrier body."""

import asyncio

from sqlalchemy import text

from awf.db.session import make_engine
from awf.hosted_node.lifecycle_barrier import GUARDED_TABLES
from tests.postgres import postgres_empty_test_url
from tests.unit.hosted_node.test_lifecycle_migration import _migrate

_CLEAR_ONLY = """IF gate.idle_since IS NOT NULL THEN
                    UPDATE hosted_node_lifecycle SET idle_since = NULL WHERE singleton = 1;
                END IF;"""
_UNCONDITIONAL = "UPDATE hosted_node_lifecycle SET idle_since = NULL WHERE singleton = 1;"
_INDEXED_TASKS = "SELECT 1 FROM task_attempts a JOIN workspaces w ON w.id = a.workspace_id"


async def test_barrier_idle_clear_migration_converges_stale_bodies():
    async with postgres_empty_test_url() as url:

        async def migrate(*args):
            result = await asyncio.to_thread(_migrate, url, *args)
            assert result.returncode == 0, result.stderr.decode()[-2000:]

        await migrate("upgrade", "head")
        engine = make_engine(url)
        try:

            async def barrier():
                async with engine.connect() as connection:
                    guards = await connection.scalar(
                        text(
                            "SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
                            "WHERE t.tgname='awf_node_admission' "
                            "AND c.relnamespace=current_schema()::regnamespace"
                        )
                    )
                    body = await connection.scalar(
                        text(
                            "SELECT prosrc FROM pg_proc WHERE proname='awf_node_admission_guard' "
                            "AND pronamespace=current_schema()::regnamespace"
                        )
                    )
                return guards, body

            guards, live = await barrier()
            assert guards == len(GUARDED_TABLES)
            assert _CLEAR_ONLY in live and _INDEXED_TASKS in live
            await migrate("downgrade", "b830e6d9a142")
            assert (await barrier())[0] == len(GUARDED_TABLES)
            # Stand in for a database upgraded before this revision: the same
            # triggers still call a previously installed function body.
            stale = live.replace(_CLEAR_ONLY, _UNCONDITIONAL)
            assert stale != live
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "CREATE OR REPLACE FUNCTION awf_node_admission_guard() RETURNS trigger "
                        f"LANGUAGE plpgsql AS $${stale}$$"
                    )
                )
            assert await barrier() == (len(GUARDED_TABLES), stale)
            await migrate("upgrade", "head")
            assert await barrier() == (len(GUARDED_TABLES), live)
            await migrate("downgrade", "b830e6d9a142")
            await migrate("upgrade", "head")
            assert await barrier() == (len(GUARDED_TABLES), live)
        finally:
            await engine.dispose()
