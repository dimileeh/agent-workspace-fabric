"""Shared pytest fixtures for AWF.

Fixtures are organized by scope: module-level primitives here, per-test overrides in
subdirectory conftest.py files.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from awf.api.app import create_app


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """An AsyncClient bound to a fresh AWF app instance for a single test.

    Uses ASGITransport so we exercise the real routing/middleware stack without a
    running server. Each test gets an isolated app to prevent state bleed.
    """
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
