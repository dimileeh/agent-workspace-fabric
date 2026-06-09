"""Integration test: Alembic migrations apply cleanly against real Postgres.

Runs against the live Postgres server configured by ``AWF_TEST_DATABASE_URL`` or
the dedicated default test database. The test creates a temporary schema on
that server so the migration round-trip never downgrades the operator's real
AWF schema.

What this covers:
- asyncpg driver + ``async_engine_from_config`` path in migrations/env.py
- The autogen migration's Postgres dialect output for JSON and index DDL
- Round-trip upgrade → downgrade → upgrade on the real DB dialect
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from dotenv import dotenv_values
from sqlalchemy.engine import URL, make_url

from tests.postgres import postgres_alembic_subprocess_lock, postgres_empty_test_url

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _raw_postgres_database_url() -> str | None:
    raw_url = os.environ.get("AWF_TEST_DATABASE_URL")
    if raw_url:
        return raw_url

    # Load only this test URL from the repo-local dotenv file. Pulling the whole
    # file into os.environ would leak host provider/auth settings into hermetic
    # readiness tests.
    dotenv_config = dotenv_values(_REPO_ROOT / ".env")
    dotenv_url = dotenv_config.get("AWF_TEST_DATABASE_URL")
    if isinstance(dotenv_url, str) and dotenv_url.strip():
        return dotenv_url
    return None


def _postgres_database_url() -> URL:
    raw_url = _raw_postgres_database_url()
    if not raw_url:
        pytest.fail(
            "AWF_TEST_DATABASE_URL must point at a live PostgreSQL server for "
            "the full integration suite."
        )
    url = make_url(raw_url)
    if url.get_backend_name() != "postgresql":
        pytest.fail(
            "AWF_TEST_DATABASE_URL must use a PostgreSQL backend for the full integration suite."
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


def test_postgres_database_url_ignores_repo_dotenv_database_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AWF_TEST_DATABASE_URL", raising=False)
    monkeypatch.delenv("AWF_DATABASE_URL", raising=False)
    monkeypatch.setattr(sys.modules[__name__], "_REPO_ROOT", tmp_path)
    (tmp_path / ".env").write_text(
        "AWF_DATABASE_URL=postgresql+asyncpg://awf:awf_dev@localhost:5433/awf\n",
        encoding="utf-8",
    )

    with pytest.raises(pytest.fail.Exception, match="AWF_TEST_DATABASE_URL"):
        _postgres_database_url()


@pytest.mark.integration
@pytest.mark.timeout(120)
async def test_alembic_upgrade_downgrade_upgrade_on_postgres() -> None:
    """Apply → revert → re-apply the full migration chain against live Postgres."""
    async with postgres_empty_test_url() as database_url:
        env = {**os.environ, "AWF_DATABASE_URL": database_url}
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

        with postgres_alembic_subprocess_lock(database_url):
            # Start from a known-clean state.
            _alembic("downgrade", "base")

            # Full chain up.
            _alembic("upgrade", "head")
            # Full chain back down.
            _alembic("downgrade", "base")
            # And up again — proves the down migrations are correct inverses.
            _alembic("upgrade", "head")
