"""Shared pytest fixtures for AWF.

The ``client`` fixture wires each test to its own in-memory SQLite database with
a fresh app instance, so tests are isolated and can run in parallel. Tests that
need to inspect the DB directly can use the ``session`` fixture (added in
tests/unit/db/test_workspace_repository.py) which uses the same pattern.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from awf.api.app import configure_database, create_app
from awf.db.base import Base
from awf.db.session import make_engine, make_session_factory


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    """Per-test in-memory SQLite engine with schema created from ORM metadata."""
    eng = make_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest.fixture
async def client(engine: AsyncEngine) -> AsyncIterator[AsyncClient]:
    """AsyncClient bound to a fresh AWF app with an in-memory SQLite DB.

    Uses ``use_lifespan=False`` so the real lifespan (which reads env + builds a
    production engine) doesn't run; we attach our own session factory instead.
    """
    app = create_app(use_lifespan=False)
    configure_database(app, make_session_factory(engine))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
