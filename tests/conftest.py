"""Shared pytest fixtures for AWF.

The ``client`` fixture wires each test to its own PostgreSQL schema with a fresh
app instance, so tests are isolated and can run in parallel. Tests that need to
inspect the DB directly can use the ``engine`` fixture with the same pattern.
"""

from __future__ import annotations

import ast
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
from awf.common.config import Settings, get_settings
from awf.db.session import make_session_factory
from awf.service.disk import DiskCheck
from tests.postgres import postgres_test_engine
from tests.util import _positive_int_env

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows does not run AWF Docker CI.
    fcntl = None  # type: ignore[assignment]


_POSTGRES_TEST_TIMEOUT_SECONDS = _positive_int_env("AWF_POSTGRES_TEST_TIMEOUT_SECONDS", 120)
_DOCKER_TEST_TIMEOUT_SECONDS = _positive_int_env("AWF_DOCKER_TEST_TIMEOUT_SECONDS", 1200)
_SESSION_PATH = os.environ.get("PATH")
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
_POSTGRES_SCHEMA_HELPER_CALLS = frozenset(
    {
        "create_postgres_test_engine",
        "postgres_empty_test_url",
        "postgres_test_engine",
        "postgres_test_session",
        "postgres_test_url",
        "postgres_test_url_sync",
    }
)
_API_TEST_TOKEN = "secret"


def _uses_postgres_test_fixture(item: pytest.Item) -> bool:
    fixture_names = set(getattr(item, "fixturenames", ()))
    return not fixture_names.isdisjoint(_POSTGRES_FIXTURE_NAMES)


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


def _postgres_call_helper_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _item_test_name(item: pytest.Item) -> str | None:
    original_name = getattr(item, "originalname", None)
    if isinstance(original_name, str):
        return original_name
    name = getattr(item, "name", None)
    if not isinstance(name, str):
        return None
    return name.split("[", 1)[0].rsplit("::", 1)[-1]


def _item_source_line(item: pytest.Item) -> int | None:
    location = getattr(item, "location", None)
    if not isinstance(location, tuple) or len(location) < 2:
        return None
    lineno = location[1]
    if not isinstance(lineno, int):
        return None
    return lineno + 1


def _item_fixture_names(item: pytest.Item) -> tuple[str, ...]:
    return tuple(
        sorted(name for name in getattr(item, "fixturenames", ()) if isinstance(name, str))
    )


def _test_function_contains_line(node: ast.FunctionDef | ast.AsyncFunctionDef, line: int) -> bool:
    return node.lineno <= line <= getattr(node, "end_lineno", node.lineno)


def _selected_test_function_nodes(
    tree: ast.AST,
    item: pytest.Item,
) -> tuple[ast.FunctionDef | ast.AsyncFunctionDef, ...]:
    function_nodes = tuple(
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    )
    source_line = _item_source_line(item)
    if source_line is not None:
        line_matches = tuple(
            node for node in function_nodes if _test_function_contains_line(node, source_line)
        )
        if line_matches:
            return line_matches

    test_name = _item_test_name(item)
    if test_name is None:
        return ()
    return tuple(node for node in function_nodes if node.name == test_name)


def _pytest_fixture_name(
    node: ast.expr,
    function_name: str,
) -> str | None:
    decorator = node.func if isinstance(node, ast.Call) else node
    if isinstance(decorator, ast.Name):
        is_fixture = decorator.id == "fixture"
    elif isinstance(decorator, ast.Attribute):
        is_fixture = decorator.attr == "fixture"
    else:
        is_fixture = False
    if not is_fixture:
        return None
    if isinstance(node, ast.Call):
        for keyword in node.keywords:
            if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
                name = keyword.value.value
                if isinstance(name, str):
                    return name
    return function_name


def _selected_fixture_nodes(
    tree: ast.AST,
    item: pytest.Item,
) -> tuple[ast.FunctionDef | ast.AsyncFunctionDef, ...]:
    fixture_names = set(_item_fixture_names(item))
    if not fixture_names:
        return ()
    selected_nodes: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if any(
            _pytest_fixture_name(decorator, node.name) in fixture_names
            for decorator in node.decorator_list
        ):
            selected_nodes.append(node)
    return tuple(selected_nodes)


def _node_calls_postgres_schema_helper(node: ast.AST) -> bool:
    return any(
        _postgres_call_helper_name(child.func) in _POSTGRES_SCHEMA_HELPER_CALLS
        for child in ast.walk(node)
        if isinstance(child, ast.Call)
    )


def _test_source_calls_postgres_schema_helper(
    item: pytest.Item,
    cache: dict[tuple[Path, str | None, int | None, tuple[str, ...]], bool],
) -> bool:
    path = item.path
    cache_key = (path, _item_test_name(item), _item_source_line(item), _item_fixture_names(item))
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    try:
        source = path.read_text(encoding="utf-8")
    except OSError:
        cache[cache_key] = False
        return False
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        cache[cache_key] = False
        return False

    selected_nodes = _selected_test_function_nodes(tree, item) + _selected_fixture_nodes(tree, item)
    nodes_to_scan: tuple[ast.AST, ...] = selected_nodes or (tree,)
    calls_helper = any(_node_calls_postgres_schema_helper(node) for node in nodes_to_scan)
    cache[cache_key] = calls_helper
    return calls_helper


def _uses_postgres_test_database(item: pytest.Item, cache: dict[Path, bool]) -> bool:
    if _uses_postgres_test_fixture(item):
        return True
    return _test_source_uses_postgres(item.path, cache)


def _uses_postgres_schema_allocator(
    item: pytest.Item,
    cache: dict[tuple[Path, str | None, int | None, tuple[str, ...]], bool],
) -> bool:
    if _uses_postgres_test_fixture(item):
        return True
    return _test_source_calls_postgres_schema_helper(item, cache)


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    del config
    source_cache: dict[Path, bool] = {}
    for item in items:
        if item.get_closest_marker("docker") is not None:
            timeout_marker = item.get_closest_marker("timeout")
            timeout_seconds = (
                timeout_marker.args[0] if timeout_marker and timeout_marker.args else 0
            )
            if not isinstance(timeout_seconds, int | float) or (
                timeout_seconds < _DOCKER_TEST_TIMEOUT_SECONDS
            ):
                item.add_marker(pytest.mark.timeout(_DOCKER_TEST_TIMEOUT_SECONDS), append=False)
            continue
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

    source_cache: dict[tuple[Path, str | None, int | None, tuple[str, ...]], bool] = {}
    if any(_uses_postgres_schema_allocator(item, source_cache) for item in session.items):
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
    previous_path = os.environ.get("PATH")
    if _SESSION_PATH is None:
        os.environ.pop("PATH", None)
    else:
        os.environ["PATH"] = _SESSION_PATH
    try:
        with _docker_test_lock():
            yield
    finally:
        if previous_path is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = previous_path


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    """Per-test PostgreSQL engine with schema created from ORM metadata."""
    async with postgres_test_engine() as eng:
        yield eng


@pytest.fixture
async def client(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[AsyncClient]:
    """AsyncClient bound to a fresh AWF app with an isolated PostgreSQL schema.

    Uses ``use_lifespan=False`` so the real lifespan (which reads env + builds a
    production engine) doesn't run; we attach our own session factory instead.
    """
    api_token = os.environ.get("AWF_API_TOKEN") or _API_TEST_TOKEN
    monkeypatch.setenv("AWF_API_TOKEN", api_token)

    try:
        get_settings.cache_clear()
        app = create_app(use_lifespan=False)
        configure_database(app, make_session_factory(engine))
        app.state.workspace_admission_disk_check = _ok_workspace_admission_disk_check

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            c.headers["Authorization"] = f"Bearer {api_token}"
            yield c
    finally:
        get_settings.cache_clear()


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
