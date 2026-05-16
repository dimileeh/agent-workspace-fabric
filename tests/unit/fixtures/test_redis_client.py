"""Regression tests for the Redis worker fixture client."""

from __future__ import annotations

import importlib.util
import io
from pathlib import Path
from types import ModuleType, TracebackType

import pytest

_FIXTURE_ROOT = (
    Path(__file__).resolve().parents[2] / "fixtures" / "workspace_services" / "redis_worker_app"
)

pytestmark = pytest.mark.unit


class _FakeSocket:
    def __init__(self, reader: io.BytesIO) -> None:
        self.reader = reader
        self.sent: list[bytes] = []
        self.timeout: float | None = None
        self.closed = False

    def __enter__(self) -> _FakeSocket:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.closed = True

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def makefile(self, mode: str) -> io.BytesIO:
        assert mode == "rb"
        return self.reader

    def sendall(self, payload: bytes) -> None:
        self.sent.append(payload)


def _load_redis_client_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "redis_worker_fixture_redis_client",
        _FIXTURE_ROOT / "redis_client.py",
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_execute_closes_reader_created_from_socket_makefile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_client = _load_redis_client_module()
    reader = io.BytesIO(b"+PONG\r\n")
    fake_socket = _FakeSocket(reader)

    def create_connection(address: tuple[str, int], timeout: float) -> _FakeSocket:
        assert address == ("redis", 6379)
        assert timeout == 1.5
        return fake_socket

    monkeypatch.setattr(redis_client.socket, "create_connection", create_connection)

    client = redis_client.RedisClient("redis", 6379, timeout=1.5)
    assert client.execute("PING") == "PONG"

    assert fake_socket.timeout == 1.5
    assert fake_socket.closed
    assert reader.closed


def test_execute_requires_select_ok_response(monkeypatch: pytest.MonkeyPatch) -> None:
    redis_client = _load_redis_client_module()
    reader = io.BytesIO(b"+QUEUED\r\n+PONG\r\n")
    fake_socket = _FakeSocket(reader)

    def create_connection(address: tuple[str, int], timeout: float) -> _FakeSocket:
        assert address == ("redis", 6379)
        assert timeout == 5.0
        return fake_socket

    monkeypatch.setattr(redis_client.socket, "create_connection", create_connection)

    client = redis_client.RedisClient("redis", 6379, db=2)
    with pytest.raises(redis_client.RedisError, match="unexpected SELECT response"):
        client.execute("PING")

    assert fake_socket.sent == [
        b"*2\r\n$6\r\nSELECT\r\n$1\r\n2\r\n",
    ]
    assert fake_socket.closed
    assert reader.closed
