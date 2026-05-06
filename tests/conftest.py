"""Shared pytest fixtures for AWF.

The ``client`` fixture wires each test to its own PostgreSQL schema with a fresh
app instance, so tests are isolated and can run in parallel. Tests that need to
inspect the DB directly can use the ``engine`` fixture with the same pattern.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
from collections import namedtuple
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from awf.api.app import configure_database, create_app
from awf.common.config import Settings
from awf.db.session import make_session_factory
from awf.service.disk import DiskCheck
from tests.postgres import postgres_test_engine

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows does not run AWF Docker CI.
    fcntl = None  # type: ignore[assignment]

_POSTGRES_TEST_TIMEOUT_SECONDS = 120
_POSTGRES_FIXTURE_NAMES = frozenset(
    {
        "client",
        "disk_app_and_client",
        "engine",
        "session_factory",
    }
)
_POSTGRES_SOURCE_SENTINELS = (
    "tests.postgres",
    "postgres_test_",
    "create_postgres_test_engine",
)


def _test_source_uses_postgres(path: Path, cache: dict[Path, bool]) -> bool:
    cached = cache.get(path)
    if cached is not None:
        return cached
    try:
        source = path.read_text(encoding="utf-8")
    except OSError:
        cache[path] = False
        return False
    uses_postgres = any(sentinel in source for sentinel in _POSTGRES_SOURCE_SENTINELS)
    cache[path] = uses_postgres
    return uses_postgres


def _uses_postgres_test_database(item: pytest.Item, cache: dict[Path, bool]) -> bool:
    fixture_names = set(getattr(item, "fixturenames", ()))
    if not fixture_names.isdisjoint(_POSTGRES_FIXTURE_NAMES):
        return True
    return _test_source_uses_postgres(item.path, cache)


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    del config
    source_cache: dict[Path, bool] = {}
    for item in items:
        if item.get_closest_marker("timeout") is not None:
            continue
        if not _uses_postgres_test_database(item, source_cache):
            continue
        item.add_marker(pytest.mark.timeout(_POSTGRES_TEST_TIMEOUT_SECONDS))


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


def pytest_collection_finish(session: pytest.Session) -> None:
    """Clear stale Postgres schemas before DB-backed test selections start."""

    source_cache: dict[Path, bool] = {}
    if any(_uses_postgres_test_database(item, source_cache) for item in session.items):
        from tests.postgres import cleanup_stale_postgres_test_schemas

        cleanup_stale_postgres_test_schemas()


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


@contextmanager
def _docker_test_lock() -> Iterator[None]:
    if fcntl is None:
        yield
        return
    run_uid = os.environ.get("PYTEST_XDIST_TESTRUNUID", "local")
    lock_path = Path(tempfile.gettempdir()) / f"awf-pytest-docker-{run_uid}.lock"
    with lock_path.open("w", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@pytest.fixture(autouse=True)
def _serialize_docker_daemon_tests(request: pytest.FixtureRequest) -> Iterator[None]:
    """Serialize real Docker daemon tests under xdist.

    Compose projects share the same Docker daemon, image cache, plugin process,
    and network/volume namespace. The application remains parallel-safe; these
    integration tests are the shared external resource and need a narrow lock.
    """
    if request.node.get_closest_marker("docker") is None:
        yield
        return
    with _docker_test_lock():
        yield


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    """Per-test PostgreSQL engine with schema created from ORM metadata."""
    async with postgres_test_engine() as eng:
        yield eng


@pytest.fixture
async def client(engine: AsyncEngine) -> AsyncIterator[AsyncClient]:
    """AsyncClient bound to a fresh AWF app with an isolated PostgreSQL schema.

    Uses ``use_lifespan=False`` so the real lifespan (which reads env + builds a
    production engine) doesn't run; we attach our own session factory instead.
    """
    app = create_app(use_lifespan=False)
    configure_database(app, make_session_factory(engine))
    app.state.workspace_admission_disk_check = _ok_workspace_admission_disk_check

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
def mock_docker_cli_probe(monkeypatch):
    """Safely mock docker CLI probes."""
    original_run = subprocess.run

    def _mock_run(args, **kwargs):
        if len(args) > 0 and args[0] == "docker" and any("command -v" in str(a) for a in args):
            CompletedProcess = namedtuple("CompletedProcess", ["returncode", "stdout", "stderr"])
            return CompletedProcess(returncode=0, stdout="/usr/bin/cli\n", stderr="")
        return original_run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _mock_run)
