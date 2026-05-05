"""Integration test: Alembic migrations apply cleanly against real Postgres.

Runs against the live Postgres server configured by ``AWF_TEST_DATABASE_URL`` or
``AWF_DATABASE_URL``. The test creates a temporary database on that server so
the migration round-trip never downgrades the operator's real AWF database.

What this covers:
- asyncpg driver + ``async_engine_from_config`` path in migrations/env.py
- The autogen migration's Postgres dialect output for JSON and index DDL
- Round-trip upgrade → downgrade → upgrade on the real DB
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from pathlib import Path

import asyncpg
import pytest
from dotenv import dotenv_values
from sqlalchemy.engine import URL, make_url

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _raw_postgres_database_url() -> str | None:
    raw_url = os.environ.get("AWF_TEST_DATABASE_URL") or os.environ.get("AWF_DATABASE_URL")
    if raw_url:
        return raw_url

    # Load only this test URL from the repo-local dotenv file. Pulling the whole
    # file into os.environ would leak host provider/auth settings into hermetic
    # readiness tests.
    dotenv_url = dotenv_values(_REPO_ROOT / ".env").get("AWF_TEST_DATABASE_URL")
    if isinstance(dotenv_url, str) and dotenv_url.strip():
        return dotenv_url
    return None


def _postgres_database_url() -> URL:
    raw_url = _raw_postgres_database_url()
    if not raw_url:
        pytest.fail(
            "AWF_TEST_DATABASE_URL or AWF_DATABASE_URL must point at a live PostgreSQL "
            "server for the full integration suite."
        )
    url = make_url(raw_url)
    if url.get_backend_name() != "postgresql":
        pytest.fail(
            "AWF_TEST_DATABASE_URL/AWF_DATABASE_URL must use a PostgreSQL backend for "
            "the full integration suite."
        )
    return url


def test_postgres_database_url_reads_repo_dotenv_when_environment_is_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AWF_TEST_DATABASE_URL", raising=False)
    monkeypatch.delenv("AWF_DATABASE_URL", raising=False)
    monkeypatch.setattr(sys.modules[__name__], "_REPO_ROOT", tmp_path)
    (tmp_path / ".env").write_text(
        "AWF_TEST_DATABASE_URL=postgresql+asyncpg://awf:awf_dev@localhost:5433/awf\n",
        encoding="utf-8",
    )

    assert _postgres_database_url().render_as_string(hide_password=False) == (
        "postgresql+asyncpg://awf:awf_dev@localhost:5433/awf"
    )


def _asyncpg_url(url: URL) -> str:
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


def _maintenance_database_url(url: URL) -> URL:
    return url.set(database="postgres")


async def _create_database(maintenance_url: URL, database_name: str) -> None:
    conn = await asyncpg.connect(dsn=_asyncpg_url(maintenance_url))
    try:
        await conn.execute(f'CREATE DATABASE "{database_name}"')
    finally:
        await conn.close()


async def _drop_database(maintenance_url: URL, database_name: str) -> None:
    conn = await asyncpg.connect(dsn=_asyncpg_url(maintenance_url))
    try:
        await conn.execute(
            """
            SELECT pg_terminate_backend(pid)
            FROM pg_stat_activity
            WHERE datname = $1 AND pid <> pg_backend_pid()
            """,
            database_name,
        )
        await conn.execute(f'DROP DATABASE IF EXISTS "{database_name}"')
    finally:
        await conn.close()


@pytest.mark.integration
@pytest.mark.timeout(120)
def test_alembic_upgrade_downgrade_upgrade_on_postgres() -> None:
    """Apply → revert → re-apply the full migration chain against live Postgres."""
    configured_url = _postgres_database_url()
    maintenance_url = _maintenance_database_url(configured_url)
    database_name = f"awf_test_alembic_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    database_url = configured_url.set(database=database_name)
    asyncio.run(_create_database(maintenance_url, database_name))

    env = {**os.environ, "AWF_DATABASE_URL": database_url.render_as_string(hide_password=False)}
    cwd = str(_REPO_ROOT)

    def _alembic(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "alembic", "-c", "alembic.ini", *args],
            cwd=cwd,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )

    try:
        # Start from a known-clean state.
        _alembic("downgrade", "base")

        # Full chain up.
        _alembic("upgrade", "head")
        # Full chain back down.
        _alembic("downgrade", "base")
        # And up again — proves the down migrations are correct inverses.
        _alembic("upgrade", "head")
    finally:
        asyncio.run(_drop_database(maintenance_url, database_name))
