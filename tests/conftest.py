"""Shared pytest fixtures for AWF.

The ``client`` fixture wires each test to its own in-memory SQLite database with
a fresh app instance, so tests are isolated and can run in parallel. Tests that
need to inspect the DB directly can use the ``session`` fixture (added in
tests/unit/db/test_workspace_repository.py) which uses the same pattern.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import suppress

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from awf.api.app import configure_database, create_app
from awf.common.config import Settings
from awf.db.base import Base
from awf.db.session import make_engine, make_session_factory
from awf.service.disk import DiskCheck


def _ok_workspace_admission_disk_check(settings: Settings) -> DiskCheck:
    threshold = settings.min_free_disk_bytes
    free = threshold + 1
    return DiskCheck(
        path=settings.work_dir,
        checked_path=settings.work_dir,
        total_bytes=free,
        used_bytes=0,
        free_bytes=free,
        percent_free=100.0,
        threshold_bytes=threshold,
        ok=True,
        status="ok",
        reason="SUFFICIENT_DISK",
    )


@pytest.fixture(autouse=True)
def _close_idle_asyncio_policy_loop() -> Iterator[None]:
    """Close dormant loops pytest-asyncio can leave on the event-loop policy.

    pytest-asyncio preserves a previous policy loop while setting up async
    fixtures. On Python 3.12, looking up that previous loop can create an idle
    selector loop; when later sync tests run, pytest's unraisable-exception
    collector can surface the idle loop's self-pipe as ResourceWarning. The
    fixture only closes a non-running current policy loop after pytest has
    finished the test body and fixture cleanup.
    """
    yield

    policy = asyncio.get_event_loop_policy()
    local = getattr(policy, "_local", None)
    loop = getattr(local, "_loop", None)
    if loop is None or loop.is_closed() or loop.is_running():
        return

    loop.close()
    with suppress(RuntimeError):
        asyncio.set_event_loop(None)


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
    app.state.workspace_admission_disk_check = _ok_workspace_admission_disk_check

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
import pytest
from collections import namedtuple
import subprocess

@pytest.fixture
def mock_docker_cli_probe(monkeypatch):
    """Safely mock docker CLI probes."""
    original_run = subprocess.run
    def _mock_run(args, **kwargs):
        if len(args) > 0 and args[0] == "docker" and any("command -v" in str(a) for a in args):
            CompletedProcess = namedtuple('CompletedProcess', ['returncode', 'stdout', 'stderr'])
            return CompletedProcess(returncode=0, stdout="/usr/bin/cli\n", stderr="")
        return original_run(args, **kwargs)
    
    monkeypatch.setattr(subprocess, "run", _mock_run)
