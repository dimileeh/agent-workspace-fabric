"""Actual additive migration, old-schema trigger compatibility and closed downgrade."""

import asyncio

from sqlalchemy import text

from awf.db.session import make_engine
from tests.postgres import postgres_empty_test_url
from tests.unit.hosted_node.test_lifecycle_migration import _migrate


async def test_checkpoint_columns_are_additive_and_downgrade_cannot_erase_authority():
    async with postgres_empty_test_url() as url:
        for revision in ("ec19f5061728", "head"):
            result = await asyncio.to_thread(_migrate, url, "upgrade", revision)
            assert result.returncode == 0, result.stderr.decode()[-2000:]
        engine = make_engine(url)
        try:
            async with engine.begin() as connection:
                columns = (
                    await connection.execute(
                        text(
                            "SELECT column_name,is_nullable FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='hosted_node_lifecycle' AND column_name IN ('checkpoint_marker','restore_quarantine')"
                        )
                    )
                ).all()
                assert set(columns) == {("checkpoint_marker", "YES"), ("restore_quarantine", "YES")}
                await connection.execute(
                    text(
                        "INSERT INTO hosted_node_lifecycle (singleton,identity,disk_ref,database_ref,phase,aborted_through) VALUES (1,'{}','disk','database','recovering',0)"
                    )
                )
            result = await asyncio.to_thread(_migrate, url, "downgrade", "ec19f5061728")
            assert result.returncode != 0 and b"NODE_LIFECYCLE_STILL_ENABLED" in result.stderr
            async with engine.begin() as connection:
                await connection.execute(text("DELETE FROM hosted_node_lifecycle"))
            for direction, revision in (("downgrade", "ec19f5061728"), ("upgrade", "head")):
                result = await asyncio.to_thread(_migrate, url, direction, revision)
                assert result.returncode == 0, result.stderr.decode()[-2000:]
        finally:
            await engine.dispose()
