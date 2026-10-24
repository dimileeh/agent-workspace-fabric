import asyncio
import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import text

from awf.db.session import make_engine, make_session_factory
from awf.hosted_node.config import HostedSettings
from awf.hosted_node.identity import boot
from awf.hosted_node.lifecycle_barrier import GUARDED_TABLES
from tests.postgres import postgres_alembic_subprocess_lock, postgres_empty_test_url

_ROOT = Path(__file__).resolve().parents[3]


def _migrate(url, *args):
    with postgres_alembic_subprocess_lock(url):
        return subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=_ROOT,
            env={**os.environ, "AWF_DATABASE_URL": url},
            capture_output=True,
            timeout=90,
        )


async def test_lifecycle_migration_roundtrip():
    async with postgres_empty_test_url() as url:

        async def migrate(*args):
            result = await asyncio.to_thread(_migrate, url, *args)
            assert result.returncode == 0, result.stderr.decode()[-2000:]

        await migrate("upgrade", "head")
        engine = make_engine(url)
        try:

            async def guards():
                async with engine.connect() as connection:
                    return await connection.scalar(
                        text(
                            "SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
                            "WHERE t.tgname='awf_node_admission' "
                            "AND c.relnamespace=current_schema()::regnamespace"
                        )
                    )

            assert await guards() == len(GUARDED_TABLES)
            await migrate("downgrade", "b986c2d3e4f5")
            assert await guards() == 0
            await migrate("upgrade", "head")
            assert await guards() == len(GUARDED_TABLES)
            await boot(
                HostedSettings(
                    owner="owner",
                    incarnation="123",
                    recovery_generation=1,
                    controller_token="synthetic-controller-only",
                    material_root="/unused",
                    lifecycle={"disk_ref": "disk", "database_ref": "db"},
                ),
                make_session_factory(engine),
            )
            refused = await asyncio.to_thread(_migrate, url, "downgrade", "b986c2d3e4f5")
            assert refused.returncode != 0
            assert b"NODE_LIFECYCLE_STILL_ENABLED" in refused.stderr
            assert await guards() == len(GUARDED_TABLES)
        finally:
            await engine.dispose()
