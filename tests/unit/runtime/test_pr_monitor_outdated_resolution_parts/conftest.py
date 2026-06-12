"""Shared fixtures for the outdated-thread resolve-hygiene suites.

The split mirrors ``tests/unit/runtime/_monitor_runner_fixtures.py``: the real
PostgreSQL session factory every part needs lives here as a conftest fixture so
pytest injects it without each part re-declaring it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.db.session import make_session_factory
from tests.postgres import postgres_test_engine


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)
