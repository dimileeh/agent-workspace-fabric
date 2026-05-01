"""FastAPI dependency edge-case tests."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import text

import awf.api.deps as deps
from awf.common.config import get_settings


@pytest.mark.unit
def test_sqlite_file_path_rejects_invalid_non_file_and_memory_urls(tmp_path: Path) -> None:
    db_path = tmp_path / "awf.db"

    assert deps._sqlite_file_path("not a url://[") is None
    assert deps._sqlite_file_path("postgresql+asyncpg://u:p@example/awf") is None
    assert deps._sqlite_file_path("sqlite+aiosqlite:///:memory:") is None
    assert deps._sqlite_file_path(f"sqlite+aiosqlite:///{db_path}") == db_path.resolve()


@pytest.mark.unit
def test_sqlite_identity_reports_missing_and_existing_files(tmp_path: Path) -> None:
    missing = tmp_path / "missing.db"
    existing = tmp_path / "awf.db"
    existing.write_text("", encoding="utf-8")

    assert deps._sqlite_identity(missing) is None
    assert deps._sqlite_identity(existing) == (
        existing.stat().st_dev,
        existing.stat().st_ino,
    )


@pytest.mark.unit
def test_require_api_token_reports_missing_and_invalid_tokens() -> None:
    previous = os.environ.get("AWF_API_TOKEN")
    try:
        os.environ["AWF_API_TOKEN"] = ""
        get_settings.cache_clear()
        with pytest.raises(HTTPException) as missing:
            deps.require_api_token(None)
        assert missing.value.status_code == 503
        assert missing.value.detail["error_code"] == "API_TOKEN_NOT_CONFIGURED"

        os.environ["AWF_API_TOKEN"] = "secret"
        get_settings.cache_clear()
        with pytest.raises(HTTPException) as unauthorized:
            deps.require_api_token("Bearer wrong")
        assert unauthorized.value.status_code == 401
        assert unauthorized.value.detail["error_code"] == "UNAUTHORIZED"
        assert unauthorized.value.headers == {"WWW-Authenticate": "Bearer"}

        deps.require_api_token("Bearer secret")
    finally:
        if previous is None:
            os.environ.pop("AWF_API_TOKEN", None)
        else:
            os.environ["AWF_API_TOKEN"] = previous
        get_settings.cache_clear()


@pytest.mark.unit
async def test_get_db_session_commits_and_closes_on_success() -> None:
    session = _RecordingSession()
    request = _request_with_factory(lambda: session)

    generator = deps.get_db_session(request)  # type: ignore[arg-type]
    yielded = await generator.__anext__()
    assert yielded is session

    with pytest.raises(StopAsyncIteration):
        await generator.__anext__()

    assert session.calls == ["commit", "close"]


@pytest.mark.unit
async def test_get_db_session_rolls_back_and_closes_on_error() -> None:
    session = _RecordingSession()
    request = _request_with_factory(lambda: session)

    generator = deps.get_db_session(request)  # type: ignore[arg-type]
    yielded = await generator.__anext__()
    assert yielded is session

    with pytest.raises(ValueError):
        await generator.athrow(ValueError("boom"))

    assert session.calls == ["rollback", "close"]


@pytest.mark.unit
async def test_get_db_session_factory_fast_paths_return_existing_factory() -> None:
    factory = object()
    no_url_request = _request_with_factory(lambda: factory)
    assert await deps.get_db_session_factory(no_url_request) is no_url_request.app.state.db_session_factory  # type: ignore[arg-type]

    non_sqlite_request = _request_with_factory(lambda: factory)
    non_sqlite_request.app.state.database_url = "postgresql+asyncpg://u:p@example/awf"
    assert await deps.get_db_session_factory(non_sqlite_request) is non_sqlite_request.app.state.db_session_factory  # type: ignore[arg-type]


@pytest.mark.unit
async def test_get_db_session_factory_rechecks_sqlite_identity_inside_existing_lock(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "awf.db"
    db_path.write_text("", encoding="utf-8")
    state = SimpleNamespace(
        db_session_factory=object(),
        database_url=f"sqlite+aiosqlite:///{db_path}",
        db_sqlite_identity=(0, 0),
    )

    class RefreshingLock:
        async def __aenter__(self) -> None:
            state.db_sqlite_identity = deps._sqlite_identity(db_path)

        async def __aexit__(self, *_exc_info: object) -> None:
            return None

    state.db_reconnect_lock = RefreshingLock()
    request = SimpleNamespace(app=SimpleNamespace(state=state))

    factory = await deps.get_db_session_factory(request)  # type: ignore[arg-type]

    assert factory is state.db_session_factory


@pytest.mark.unit
async def test_get_db_session_factory_rebuilds_for_replaced_sqlite_file(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "awf.db"

    class DisposableEngine:
        def __init__(self) -> None:
            self.disposed = False

        async def dispose(self) -> None:
            self.disposed = True

    old_factory = object()
    old_engine = DisposableEngine()
    state = SimpleNamespace(
        db_session_factory=old_factory,
        database_url=f"sqlite+aiosqlite:///{db_path}",
        db_sqlite_identity=(0, 0),
        db_engine=old_engine,
    )
    request = SimpleNamespace(app=SimpleNamespace(state=state))

    new_factory = await deps.get_db_session_factory(request)  # type: ignore[arg-type]
    try:
        assert new_factory is state.db_session_factory
        assert new_factory is not old_factory
        assert state.db_sqlite_identity == deps._sqlite_identity(db_path)
        assert old_engine.disposed is True
        async with new_factory() as session:
            result = await session.execute(text("SELECT 1"))
            assert result.scalar_one() == 1
    finally:
        await state.db_engine.dispose()


@pytest.mark.unit
async def test_get_db_session_factory_rebuilds_without_previous_engine(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "awf-no-old-engine.db"
    old_factory = object()
    state = SimpleNamespace(
        db_session_factory=old_factory,
        database_url=f"sqlite+aiosqlite:///{db_path}",
        db_sqlite_identity=(0, 0),
    )
    request = SimpleNamespace(app=SimpleNamespace(state=state))

    new_factory = await deps.get_db_session_factory(request)  # type: ignore[arg-type]
    try:
        assert new_factory is state.db_session_factory
        assert new_factory is not old_factory
        assert state.db_sqlite_identity == deps._sqlite_identity(db_path)
    finally:
        await state.db_engine.dispose()


def _request_with_factory(factory: object) -> SimpleNamespace:
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                db_session_factory=factory,
            )
        )
    )


class _RecordingSession:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def commit(self) -> None:
        self.calls.append("commit")

    async def rollback(self) -> None:
        self.calls.append("rollback")

    async def close(self) -> None:
        self.calls.append("close")
