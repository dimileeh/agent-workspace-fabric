from __future__ import annotations

import socket
from collections.abc import Iterable
from urllib.parse import urlparse


class RedisError(RuntimeError):
    pass


class RedisClient:
    def __init__(self, host: str, port: int, db: int = 0, timeout: float = 5.0) -> None:
        self._host = host
        self._port = port
        self._db = db
        self._timeout = timeout

    @classmethod
    def from_url(cls, url: str, *, timeout: float = 5.0) -> RedisClient:
        parsed = urlparse(url)
        if parsed.scheme != "redis" or not parsed.hostname:
            raise ValueError(f"unsupported Redis URL: {url!r}")
        db = int(parsed.path.lstrip("/") or "0")
        return cls(parsed.hostname, parsed.port or 6379, db=db, timeout=timeout)

    def ping(self) -> str:
        response = self.execute("PING")
        if not isinstance(response, str):
            raise RedisError(f"unexpected PING response: {response!r}")
        return response

    def get(self, key: str) -> str | None:
        response = self.execute("GET", key)
        if response is None or isinstance(response, str):
            return response
        raise RedisError(f"unexpected GET response: {response!r}")

    def set(self, key: str, value: str) -> str:
        response = self.execute("SET", key, value)
        if not isinstance(response, str):
            raise RedisError(f"unexpected SET response: {response!r}")
        return response

    def delete(self, *keys: str) -> int:
        response = self.execute("DEL", *keys)
        if not isinstance(response, int):
            raise RedisError(f"unexpected DEL response: {response!r}")
        return response

    def lpush(self, key: str, value: str) -> int:
        response = self.execute("LPUSH", key, value)
        if not isinstance(response, int):
            raise RedisError(f"unexpected LPUSH response: {response!r}")
        return response

    def brpop(self, key: str, timeout_seconds: int) -> tuple[str, str] | None:
        response = self.execute("BRPOP", key, str(timeout_seconds), timeout=timeout_seconds + 2)
        if response is None:
            return None
        if (
            isinstance(response, list)
            and len(response) == 2
            and isinstance(response[0], str)
            and isinstance(response[1], str)
        ):
            return response[0], response[1]
        raise RedisError(f"unexpected BRPOP response: {response!r}")

    def execute(self, *parts: str, timeout: float | None = None) -> object:
        with socket.create_connection((self._host, self._port), timeout or self._timeout) as sock:
            sock.settimeout(timeout or self._timeout)
            with sock.makefile("rb") as reader:
                if self._db:
                    sock.sendall(_encode_command(("SELECT", str(self._db))))
                    select_response = _read_response(reader)
                    if select_response != "OK":
                        raise RedisError(f"unexpected SELECT response: {select_response!r}")
                sock.sendall(_encode_command(parts))
                return _read_response(reader)


def _encode_command(parts: Iterable[str]) -> bytes:
    encoded_parts = [part.encode("utf-8") for part in parts]
    chunks = [f"*{len(encoded_parts)}\r\n".encode("ascii")]
    for part in encoded_parts:
        chunks.append(f"${len(part)}\r\n".encode("ascii"))
        chunks.append(part)
        chunks.append(b"\r\n")
    return b"".join(chunks)


def _read_response(reader: object) -> object:
    prefix = _read_exact(reader, 1)
    if prefix == b"+":
        return _read_line(reader).decode("utf-8")
    if prefix == b"-":
        raise RedisError(_read_line(reader).decode("utf-8"))
    if prefix == b":":
        return int(_read_line(reader))
    if prefix == b"$":
        size = int(_read_line(reader))
        if size == -1:
            return None
        payload = _read_exact(reader, size)
        _read_exact(reader, 2)
        return payload.decode("utf-8")
    if prefix == b"*":
        size = int(_read_line(reader))
        if size == -1:
            return None
        return [_read_response(reader) for _ in range(size)]
    raise RedisError(f"unexpected RESP prefix: {prefix!r}")


def _read_line(reader: object) -> bytes:
    line = reader.readline()  # type: ignore[attr-defined]
    if not line:
        raise RedisError("connection closed by Redis")
    return line.removesuffix(b"\r\n")


def _read_exact(reader: object, size: int) -> bytes:
    data = reader.read(size)  # type: ignore[attr-defined]
    if not isinstance(data, bytes) or len(data) != size:
        raise RedisError("connection closed by Redis")
    return data
