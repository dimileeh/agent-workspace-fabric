import asyncio
import os
import subprocess
import sys

from sqlalchemy import inspect

from awf.db.session import make_engine
from awf.hosted_node.models import Receipt
from tests.postgres import postgres_empty_test_url


def test_receipt_metadata_indexes_grant():
    index = next(
        (index for index in Receipt.__table__.indexes if index.name == "ix_hosted_receipts_grant"),
        None,
    )
    assert index is not None
    assert [column.name for column in index.columns] == ["grant"]
    assert not index.unique


async def test_upgrade_downgrade_upgrade():
    async with postgres_empty_test_url() as url:

        async def migrate(*args):
            result = await asyncio.to_thread(
                subprocess.run,
                [sys.executable, "-m", "alembic", *args],
                env={**os.environ, "AWF_DATABASE_URL": url},
                capture_output=True,
                timeout=90,
            )
            assert result.returncode == 0, result.stderr.decode()[-2000:]

        await migrate("upgrade", "a875b1c2d3e4")
        await migrate("upgrade", "head")
        engine = make_engine(url)
        try:

            async def names():
                async with engine.connect() as connection:
                    return await connection.run_sync(lambda c: inspect(c).get_table_names())

            async def assert_grant_index():
                async with engine.connect() as connection:
                    indexes = await connection.run_sync(
                        lambda c: inspect(c).get_indexes("hosted_receipts")
                    )
                index = next(
                    (index for index in indexes if index["name"] == "ix_hosted_receipts_grant"),
                    None,
                )
                assert index is not None
                assert index["column_names"] == ["grant"]
                assert not index["unique"]

            assert "hosted_receipts" in await names()
            await assert_grant_index()
            await migrate("downgrade", "a875b1c2d3e4")
            assert "hosted_receipts" not in await names()
            assert "workspace_secret_leases" in await names()
            await migrate("upgrade", "head")
            assert "hosted_owners" in await names()
            await assert_grant_index()
        finally:
            await engine.dispose()
