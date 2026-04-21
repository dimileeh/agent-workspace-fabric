"""Integration test: Alembic migrations apply cleanly against real Postgres.

Runs against ``AWF_DATABASE_URL`` (expected to point at a live Postgres instance
in CI). Skipped locally if no Postgres URL is configured, so developers don't
need to run ``docker compose up postgres`` just to run the test suite.

What this covers:
- asyncpg driver + ``async_engine_from_config`` path in migrations/env.py
- The autogen migration's Postgres dialect output (which differs subtly from
  SQLite; e.g., index creation for JSON columns)
- Round-trip upgrade → downgrade → upgrade on the real DB
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _database_url() -> str | None:
    url = os.environ.get("AWF_DATABASE_URL")
    if url and url.startswith("postgresql"):
        return url
    return None


@pytest.mark.integration
@pytest.mark.skipif(
    _database_url() is None,
    reason="AWF_DATABASE_URL not set to a postgres URL (CI-only by default)",
)
def test_alembic_upgrade_downgrade_upgrade_on_postgres() -> None:
    """Apply → revert → re-apply the full migration chain against live Postgres."""
    env = {**os.environ}
    cwd = str(_REPO_ROOT)

    def _alembic(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [".venv/bin/alembic", "-c", "alembic.ini", *args],
            cwd=cwd,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )

    # Start from a known-clean state — if CI re-runs on the same DB, make sure
    # prior state doesn't leak in.
    _alembic("downgrade", "base")

    # Full chain up.
    _alembic("upgrade", "head")
    # Full chain back down.
    _alembic("downgrade", "base")
    # And up again — proves the down migrations are correct inverses.
    _alembic("upgrade", "head")
