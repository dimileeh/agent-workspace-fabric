"""PostgreSQL-only edge behavior that used to be hidden by loose test DBs."""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from awf.api.schemas import PullRequestMonitorAdoptionRequest
from awf.common.github_client import (
    PullRequestMetadataError,
    RepoRef,
    _head_repo_slug_from_adoption_payload,
)
from awf.db import session as session_mod
from awf.db.session import make_engine
from awf.runtime.merge_coordinator import InProcessMergeCoordinator
from awf.service.doctor import _database_endpoint
from awf.service.pr_monitor_adoption import (
    PRMonitorAdoptionError,
    _inline_profile_name,
    _raise_if_repo_identity_conflicts,
)
from awf.service.secret_leases import _ensure_utc
from awf.service.status import _utc_datetime
from awf.service.worker import _merge_coordinator_for_database_url
from tests import postgres as postgres_helpers
from tests.postgres import _drop_schema


class _AsyncConnectionContext:
    def __init__(self, connection: _RecordingConnection) -> None:
        self._connection = connection

    async def __aenter__(self) -> _RecordingConnection:
        return self._connection

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None


class _RecordingConnection:
    def __init__(self, statements: list[tuple[str, object | None]]) -> None:
        self._statements = statements

    async def execute(self, statement: object, params: object | None = None) -> None:
        self._statements.append((str(statement), params))

    async def commit(self) -> None:
        return None


@pytest.mark.unit
def test_make_engine_strips_test_url_options_and_enables_null_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def _create_async_engine(url: str, **kwargs: Any) -> object:
        captured["url"] = url
        captured["kwargs"] = kwargs
        return SimpleNamespace(url=url)

    monkeypatch.setattr(session_mod, "create_async_engine", _create_async_engine)

    engine = make_engine(
        "postgresql+asyncpg://awf:pw@localhost:5433/awf"
        "?awf_search_path=first&awf_search_path=second"
        "&awf_null_pool=true&awf_null_pool=false"
    )

    assert engine.url == "postgresql+asyncpg://awf:pw@localhost:5433/awf"
    assert captured["url"] == "postgresql+asyncpg://awf:pw@localhost:5433/awf"
    assert captured["kwargs"]["connect_args"]["server_settings"]["search_path"] == "first"
    assert captured["kwargs"]["poolclass"] is NullPool


@pytest.mark.unit
def test_make_engine_strips_test_retry_options_and_installs_async_creator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def _create_async_engine(url: str, **kwargs: Any) -> object:
        captured["url"] = url
        captured["kwargs"] = kwargs
        return SimpleNamespace(url=url)

    monkeypatch.setattr(session_mod, "create_async_engine", _create_async_engine)

    make_engine(
        "postgresql+asyncpg://awf:pw@localhost:5433/awf"
        "?awf_search_path=first&awf_connect_timeout=2&awf_connect_attempts=5"
    )

    assert captured["url"] == "postgresql+asyncpg://awf:pw@localhost:5433/awf"
    assert "connect_args" not in captured["kwargs"]
    assert callable(captured["kwargs"]["async_creator"])


@pytest.mark.unit
@pytest.mark.parametrize(
    ("url", "message"),
    [
        (
            "postgresql+asyncpg://awf:pw@localhost:5433/awf?awf_connect_timeout=nope",
            "awf_connect_timeout must be a positive number.",
        ),
        (
            "postgresql+asyncpg://awf:pw@localhost:5433/awf?awf_connect_timeout=0",
            "awf_connect_timeout must be a positive number.",
        ),
        (
            "postgresql+asyncpg://awf:pw@localhost:5433/awf?awf_connect_attempts=nope",
            "awf_connect_attempts must be a positive integer.",
        ),
        (
            "postgresql+asyncpg://awf:pw@localhost:5433/awf?awf_connect_attempts=0",
            "awf_connect_attempts must be a positive integer.",
        ),
    ],
)
def test_make_engine_rejects_invalid_test_retry_options(url: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        make_engine(url)


@pytest.mark.unit
async def test_test_retry_async_creator_retries_transient_connect_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def _create_async_engine(url: str, **kwargs: Any) -> object:
        captured["url"] = url
        captured["kwargs"] = kwargs
        return SimpleNamespace(url=url)

    class TransientConnectError(Exception):
        pass

    attempts: list[tuple[str, dict[str, object]]] = []
    sleeps: list[float] = []

    async def _connect(*, dsn: str, **connect_args: object) -> object:
        attempts.append((dsn, connect_args))
        if len(attempts) == 1:
            raise TransientConnectError
        return SimpleNamespace(ok=True)

    async def _sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(session_mod, "create_async_engine", _create_async_engine)
    monkeypatch.setattr(session_mod.asyncio, "sleep", _sleep)
    monkeypatch.setitem(
        sys.modules,
        "asyncpg",
        SimpleNamespace(
            connect=_connect,
            PostgresConnectionError=TransientConnectError,
            TooManyConnectionsError=RuntimeError,
        ),
    )

    make_engine(
        "postgresql+asyncpg://awf:pw@localhost:5433/awf"
        "?awf_search_path=first&awf_connect_timeout=2&awf_connect_attempts=2"
    )
    creator = captured["kwargs"]["async_creator"]

    assert await creator() == SimpleNamespace(ok=True)
    assert attempts == [
        (
            "postgresql://awf:pw@localhost:5433/awf",
            {"server_settings": {"search_path": "first"}, "timeout": 2.0},
        ),
        (
            "postgresql://awf:pw@localhost:5433/awf",
            {"server_settings": {"search_path": "first"}, "timeout": 2.0},
        ),
    ]
    assert sleeps == [0.1]


@pytest.mark.unit
async def test_test_retry_async_creator_reraises_after_configured_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def _create_async_engine(url: str, **kwargs: Any) -> object:
        captured["kwargs"] = kwargs
        return SimpleNamespace(url=url)

    class TransientConnectError(Exception):
        pass

    attempts = 0

    async def _connect(*, dsn: str, **connect_args: object) -> object:
        del dsn, connect_args
        nonlocal attempts
        attempts += 1
        raise TransientConnectError

    async def _sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(session_mod, "create_async_engine", _create_async_engine)
    monkeypatch.setattr(session_mod.asyncio, "sleep", _sleep)
    monkeypatch.setitem(
        sys.modules,
        "asyncpg",
        SimpleNamespace(
            connect=_connect,
            PostgresConnectionError=TransientConnectError,
            TooManyConnectionsError=RuntimeError,
        ),
    )

    make_engine(
        "postgresql+asyncpg://awf:pw@localhost:5433/awf"
        "?awf_connect_timeout=2&awf_connect_attempts=2"
    )

    with pytest.raises(TransientConnectError):
        await captured["kwargs"]["async_creator"]()
    assert attempts == 2


@pytest.mark.unit
async def test_drop_schema_terminates_lock_holders_before_cascade_drop() -> None:
    statements: list[tuple[str, object | None]] = []
    conn = _RecordingConnection(statements)

    await _drop_schema(conn, "awf_test", '"awf_test"')

    assert "pg_terminate_backend" in statements[0][0]
    assert statements[0][1] == {"schema": "awf_test"}
    assert statements[1] == ("SET LOCAL lock_timeout = '5s'", None)
    assert statements[2] == ('DROP SCHEMA IF EXISTS "awf_test" CASCADE', None)


@pytest.mark.unit
async def test_postgres_schema_ddl_lock_uses_database_advisory_lock() -> None:
    statements: list[tuple[str, object | None]] = []
    conn = _RecordingConnection(statements)
    engine = SimpleNamespace(connect=lambda: _AsyncConnectionContext(conn))

    async with postgres_helpers._postgres_schema_ddl_lock(engine):
        statements.append(("inside", None))

    assert "pg_advisory_lock" in statements[0][0]
    assert statements[0][1] == {"namespace": 0x415746, "key": 0x54455354}
    assert statements[1] == ("inside", None)
    assert "pg_advisory_unlock" in statements[2][0]
    assert statements[2][1] == {"namespace": 0x415746, "key": 0x54455354}


@pytest.mark.unit
async def test_postgres_test_url_marks_yielded_url_null_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Engine:
        async def dispose(self) -> None:
            return None

    @asynccontextmanager
    async def _fake_lock(engine: _Engine) -> AsyncIterator[_Engine]:
        yield engine

    async def _noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def _metadata_engine(_url: str) -> _Engine:
        return _Engine()

    monkeypatch.setattr(
        postgres_helpers,
        "postgres_test_database_url",
        lambda: "postgresql+asyncpg://awf:pw@localhost:5433/awf",
    )
    monkeypatch.setattr(postgres_helpers, "_make_test_engine", lambda _url: _Engine())
    monkeypatch.setattr(postgres_helpers, "_postgres_schema_ddl_lock", _fake_lock)
    monkeypatch.setattr(postgres_helpers, "_create_schema", _noop)
    monkeypatch.setattr(postgres_helpers, "_create_metadata_engine", _metadata_engine)
    monkeypatch.setattr(postgres_helpers, "_drop_schema", _noop)

    async with postgres_helpers.postgres_test_url() as database_url:
        parsed = make_url(database_url)

    assert parsed.query["awf_search_path"].strip('"').startswith("awf_test_")
    assert parsed.query["awf_null_pool"] == "1"
    assert parsed.query["awf_connect_timeout"] == "10"
    assert parsed.query["awf_connect_retries"] == "3"


@pytest.mark.unit
def test_postgres_datetime_helpers_return_aware_utc_values() -> None:
    naive = datetime(2026, 5, 4, 12, 0)

    assert _ensure_utc(naive) == naive.replace(tzinfo=UTC)
    assert _utc_datetime(naive) == naive.replace(tzinfo=UTC)


@pytest.mark.unit
def test_database_endpoint_reports_missing_host() -> None:
    assert _database_endpoint("postgresql+asyncpg:///awf") == (
        "AWF_DATABASE_URL must include a host for local service mode."
    )


@pytest.mark.unit
def test_worker_merge_coordinator_falls_back_for_nonstandard_postgres_scheme() -> None:
    coordinator = _merge_coordinator_for_database_url(
        "postgresqlfoo://awf:pw@localhost/awf",
        engine=SimpleNamespace(),
    )

    assert isinstance(coordinator, InProcessMergeCoordinator)


@pytest.mark.unit
def test_adoption_rejects_unparseable_request_repo_identity() -> None:
    request = PullRequestMonitorAdoptionRequest(repo_url="not-a-github-repo", pr_number=1)

    with pytest.raises(PRMonitorAdoptionError) as exc_info:
        _raise_if_repo_identity_conflicts(
            canonical_repo=RepoRef(owner="owner", name="repo"),
            request=request,
        )

    assert exc_info.value.error_code == "INVALID_GITHUB_REPO"
    assert exc_info.value.detail == {
        "repo": "not-a-github-repo",
        "field": "repo_url",
    }


@pytest.mark.unit
def test_inline_profile_name_requires_mapping_with_string_name() -> None:
    assert _inline_profile_name(None) is None
    assert _inline_profile_name({"name": 42}) is None
    assert _inline_profile_name({"name": "strict-postgres"}) == "strict-postgres"


@pytest.mark.unit
def test_adoption_head_repo_slug_validation_edges() -> None:
    repo = RepoRef(owner="owner", name="repo")

    assert (
        _head_repo_slug_from_adoption_payload(
            {"isCrossRepository": False},
            repo=repo,
            pr_number=7,
        )
        == "owner/repo"
    )

    with pytest.raises(PullRequestMetadataError) as invalid_repo:
        _head_repo_slug_from_adoption_payload(
            {"headRepository": {"nameWithOwner": "not enough parts"}},
            repo=repo,
            pr_number=7,
        )
    assert invalid_repo.value.reason_code == "PR_METADATA_INVALID"

    with pytest.raises(PullRequestMetadataError) as missing_fork_repo:
        _head_repo_slug_from_adoption_payload(
            {"isCrossRepository": True},
            repo=repo,
            pr_number=7,
        )
    assert missing_fork_repo.value.reason_code == "PR_METADATA_INVALID"
