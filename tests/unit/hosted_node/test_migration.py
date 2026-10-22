import asyncio
import os
import subprocess
import sys

from sqlalchemy import inspect

from awf.db.session import make_engine
from tests.postgres import postgres_empty_test_url


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

            assert "hosted_receipts" in await names()
            await migrate("downgrade", "a875b1c2d3e4")
            assert "hosted_receipts" not in await names()
            assert "workspace_secret_leases" in await names()
            await migrate("upgrade", "head")
            assert "hosted_owners" in await names()
        finally:
            await engine.dispose()
