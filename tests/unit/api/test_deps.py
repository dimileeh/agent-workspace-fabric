"""FastAPI dependency edge-case tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import structlog
from fastapi import HTTPException

import awf.api.deps as deps
from awf.common.config import Settings


@pytest.mark.unit
def test_require_api_token_reports_missing_and_invalid_tokens() -> None:
    missing_settings = Settings(_env_file=None, api_token=None)
    with pytest.raises(HTTPException) as missing:
        deps.require_api_token(None, settings=missing_settings)
    assert missing.value.status_code == 503
    assert missing.value.detail["error_code"] == "API_TOKEN_NOT_CONFIGURED"

    configured_settings = Settings(_env_file=None, api_token="secret")
    with pytest.raises(HTTPException) as unauthorized:
        deps.require_api_token("Bearer wrong", settings=configured_settings)
    assert unauthorized.value.status_code == 401
    assert unauthorized.value.detail["error_code"] == "UNAUTHORIZED"
    assert unauthorized.value.headers == {"WWW-Authenticate": "Bearer"}

    deps.require_api_token("Bearer secret", settings=configured_settings)


@pytest.mark.unit
def test_require_api_token_rejects_non_ascii_bearer_as_unauthorized() -> None:
    settings = Settings(_env_file=None, api_token="secret")

    with pytest.raises(HTTPException) as unauthorized:
        deps.require_api_token("Bearer caf\u00e9", settings=settings)

    assert unauthorized.value.status_code == 401
    assert unauthorized.value.detail["error_code"] == "UNAUTHORIZED"
    assert unauthorized.value.headers == {"WWW-Authenticate": "Bearer"}


@pytest.mark.unit
def test_require_api_token_compares_tokens_with_constant_time_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(_env_file=None, api_token="secret-token")
    observed: list[tuple[object, object]] = []

    def _compare(left: object, right: object) -> bool:
        observed.append((left, right))
        return left == right

    monkeypatch.setattr(deps.hmac, "compare_digest", _compare)

    with pytest.raises(HTTPException):
        deps.require_api_token("Bearer wrong", settings=settings)
    with pytest.raises(HTTPException):
        deps.require_api_token(None, settings=settings)
    deps.require_api_token("Bearer secret-token", settings=settings)

    assert (b"Bearer wrong", b"Bearer secret-token") in observed
    assert (b"", b"Bearer secret-token") in observed
    assert (b"Bearer secret-token", b"Bearer secret-token") in observed


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
async def test_get_db_session_rolls_back_and_closes_on_base_exception() -> None:
    session = _RecordingSession()
    request = _request_with_factory(lambda: session)

    generator = deps.get_db_session(request)  # type: ignore[arg-type]
    yielded = await generator.__anext__()
    assert yielded is session

    class _RouteAbort(BaseException):
        pass

    with pytest.raises(_RouteAbort):
        await generator.athrow(_RouteAbort("abort"))

    assert session.calls == ["rollback", "close"]


@pytest.mark.unit
async def test_get_db_session_close_error_does_not_mask_route_error() -> None:
    session = _RecordingSession(close_error=RuntimeError("close failed"))
    request = _request_with_factory(lambda: session)

    generator = deps.get_db_session(request)  # type: ignore[arg-type]
    yielded = await generator.__anext__()
    assert yielded is session

    with pytest.raises(ValueError, match="route failed"):
        await generator.athrow(ValueError("route failed"))

    assert session.calls == ["rollback", "close"]


@pytest.mark.unit
async def test_get_db_session_logs_close_error_after_route_error() -> None:
    session = _RecordingSession(close_error=RuntimeError("close failed"))
    request = _request_with_factory(lambda: session)

    generator = deps.get_db_session(request)  # type: ignore[arg-type]
    yielded = await generator.__anext__()
    assert yielded is session

    with (
        structlog.testing.capture_logs() as captured,
        pytest.raises(ValueError, match="route failed"),
    ):
        await generator.athrow(ValueError("route failed"))

    assert session.calls == ["rollback", "close"]
    assert any(
        entry.get("event") == "get_db_session.close_failed_during_exception"
        and entry.get("log_level") == "warning"
        and entry.get("error_type") == "RuntimeError"
        and entry.get("error") == "close failed"
        for entry in captured
    )


@pytest.mark.unit
async def test_get_db_session_close_error_propagates_after_success() -> None:
    session = _RecordingSession(close_error=RuntimeError("close failed"))
    request = _request_with_factory(lambda: session)

    generator = deps.get_db_session(request)  # type: ignore[arg-type]
    yielded = await generator.__anext__()
    assert yielded is session

    with pytest.raises(RuntimeError, match="close failed"):
        await generator.__anext__()

    assert session.calls == ["commit", "close"]


@pytest.mark.unit
async def test_get_db_session_factory_fast_paths_return_existing_factory() -> None:
    factory = object()
    request = _request_with_factory(lambda: factory)

    assert await deps.get_db_session_factory(request) is request.app.state.db_session_factory  # type: ignore[arg-type]


def _request_with_factory(factory: object) -> SimpleNamespace:
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                db_session_factory=factory,
            )
        )
    )


class _RecordingSession:
    def __init__(self, *, close_error: Exception | None = None) -> None:
        self.calls: list[str] = []
        self.close_error = close_error

    async def commit(self) -> None:
        self.calls.append("commit")

    async def rollback(self) -> None:
        self.calls.append("rollback")

    async def close(self) -> None:
        self.calls.append("close")
        if self.close_error is not None:
            raise self.close_error
