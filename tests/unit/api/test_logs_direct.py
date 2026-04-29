"""Direct coverage for ``awf.api.routes.logs``.

The ASGI tests prove the public contract, but route handlers wrapped by
FastAPI dependency injection can leave branch coverage opaque. These tests
exercise the handlers directly with fake repositories so the error handling
and response construction remain instrumented.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import awf.api.routes.logs as logs_route


class _WorkspaceRepo:
    workspace_exists = True

    def __init__(self, _session: object) -> None:
        pass

    async def get(self, workspace_id: str) -> object | None:
        if self.workspace_exists:
            return SimpleNamespace(id=workspace_id)
        return None


class _LogRepo:
    rows: list[SimpleNamespace] = []
    stream: SimpleNamespace | None = None

    def __init__(self, _session: object) -> None:
        pass

    async def list_for_workspace(self, _workspace_id: str) -> list[SimpleNamespace]:
        return self.rows

    async def get(self, *, workspace_id: str, stream_id: str) -> SimpleNamespace | None:
        _ = workspace_id, stream_id
        return self.stream


@pytest.fixture(autouse=True)
def _fake_repositories(monkeypatch: pytest.MonkeyPatch) -> None:
    _WorkspaceRepo.workspace_exists = True
    _LogRepo.rows = []
    _LogRepo.stream = None
    monkeypatch.setattr(logs_route, "WorkspaceRepository", _WorkspaceRepo)
    monkeypatch.setattr(logs_route, "WorkspaceLogStreamRepository", _LogRepo)


def _stream(**overrides: object) -> SimpleNamespace:
    values = {
        "stream_id": "agent.stdout",
        "source": "agent",
        "name": "Agent stdout",
        "kind": "stdout",
        "path": "/tmp/agent.log",
        "byte_count": 10,
        "line_count": 2,
        "opened_at": datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
        "closed_at": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.unit
async def test_list_workspace_logs_direct() -> None:
    _LogRepo.rows = [_stream(stream_id="agent.stdout"), _stream(stream_id="agent.stderr")]

    response = await logs_route.list_workspace_logs("ws_logs", session=object())

    assert [item.stream_id for item in response.items] == ["agent.stdout", "agent.stderr"]
    assert response.next_cursor is None
    assert response.has_more is False
    assert response.limit == 2
    assert response.cursor is None


@pytest.mark.unit
async def test_read_workspace_log_direct(tmp_path: Path) -> None:
    log_path = tmp_path / "agent.log"
    log_path.write_text("hello\nworld\n", encoding="utf-8")
    _LogRepo.stream = _stream(path=str(log_path))

    response = await logs_route.read_workspace_log(
        "ws_logs",
        "agent.stdout",
        offset=6,
        limit_bytes=64,
        session=object(),
    )

    assert response.stream_id == "agent.stdout"
    assert response.offset == 6
    assert response.next_offset == len("hello\nworld\n")
    assert response.eof is True
    assert response.data == "world\n"


@pytest.mark.unit
async def test_read_workspace_log_direct_raises_for_missing_stream() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await logs_route.read_workspace_log(
            "ws_logs",
            "missing",
            offset=0,
            limit_bytes=64,
            session=object(),
        )

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail["error_code"] == "NOT_FOUND"


@pytest.mark.unit
async def test_read_workspace_log_direct_raises_for_missing_file(tmp_path: Path) -> None:
    _LogRepo.stream = _stream(path=str(tmp_path / "deleted.log"))

    with pytest.raises(HTTPException) as exc_info:
        await logs_route.read_workspace_log(
            "ws_logs",
            "agent.stdout",
            offset=0,
            limit_bytes=64,
            session=object(),
        )

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail["error_code"] == "LOG_FILE_MISSING"


@pytest.mark.unit
async def test_require_workspace_direct_raises_for_missing_workspace() -> None:
    _WorkspaceRepo.workspace_exists = False

    with pytest.raises(HTTPException) as exc_info:
        await logs_route.list_workspace_logs("ws_missing", session=object())

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail["error_code"] == "NOT_FOUND"
