from __future__ import annotations

import os
import socket
import struct
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import TracebackType
from urllib.parse import urlparse

_TABLE = "awf_profile_fixture"
_RECORD_ID = "awf-db-profile-fixture"
_RECORD_VALUE = "db-backed validation ok"


def _database_url() -> str:
    value = os.environ.get("DATABASE_URL")
    if not value:
        raise RuntimeError("DATABASE_URL is required")
    return value


def _read_exact(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise RuntimeError("postgres connection closed unexpectedly")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _error_message(payload: bytes) -> str:
    fields = payload.rstrip(b"\x00").split(b"\x00")
    messages = [
        field[1:].decode("utf-8", errors="replace")
        for field in fields
        if field[:1] in {b"M", b"D", b"H"}
    ]
    return "; ".join(messages) or payload.decode("utf-8", errors="replace")


class _PostgresConnection:
    def __init__(self, url: str) -> None:
        parsed = urlparse(url)
        self.database = parsed.path.lstrip("/") or "awf"
        self.user = parsed.username or "awf"
        self.host = parsed.hostname or "postgres"
        self.port = parsed.port or 5432
        self.sock = socket.create_connection((self.host, self.port), timeout=10)
        self.sock.settimeout(10)
        self._startup()

    def __enter__(self) -> _PostgresConnection:
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        try:
            self.sock.sendall(b"X" + struct.pack("!I", 4))
        finally:
            self.sock.close()

    def _send_startup(self) -> None:
        params = {
            "user": self.user,
            "database": self.database,
            "client_encoding": "UTF8",
        }
        payload = struct.pack("!I", 196608)
        for key, value in params.items():
            payload += key.encode("utf-8") + b"\x00" + value.encode("utf-8") + b"\x00"
        payload += b"\x00"
        self.sock.sendall(struct.pack("!I", len(payload) + 4) + payload)

    def _read_message(self) -> tuple[bytes, bytes]:
        message_type = _read_exact(self.sock, 1)
        length = struct.unpack("!I", _read_exact(self.sock, 4))[0]
        return message_type, _read_exact(self.sock, length - 4)

    def _startup(self) -> None:
        self._send_startup()
        while True:
            message_type, payload = self._read_message()
            if message_type == b"R":
                auth_code = struct.unpack("!I", payload[:4])[0]
                if auth_code != 0:
                    raise RuntimeError(f"unexpected postgres auth request: {auth_code}")
            elif message_type == b"E":
                raise RuntimeError(_error_message(payload))
            elif message_type == b"Z":
                return

    def execute(self, query: str) -> list[tuple[str | None, ...]]:
        payload = query.encode("utf-8") + b"\x00"
        self.sock.sendall(b"Q" + struct.pack("!I", len(payload) + 4) + payload)
        rows: list[tuple[str | None, ...]] = []
        while True:
            message_type, payload = self._read_message()
            if message_type == b"D":
                rows.append(self._parse_data_row(payload))
            elif message_type == b"E":
                raise RuntimeError(_error_message(payload))
            elif message_type == b"Z":
                return rows

    @staticmethod
    def _parse_data_row(payload: bytes) -> tuple[str | None, ...]:
        column_count = struct.unpack("!H", payload[:2])[0]
        offset = 2
        values: list[str | None] = []
        for _ in range(column_count):
            value_length = struct.unpack("!i", payload[offset : offset + 4])[0]
            offset += 4
            if value_length == -1:
                values.append(None)
                continue
            raw_value = payload[offset : offset + value_length]
            offset += value_length
            values.append(raw_value.decode("utf-8"))
        return tuple(values)


def _connect() -> _PostgresConnection:
    return _PostgresConnection(_database_url())


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        try:
            if self.path == "/healthz":
                self._handle_healthz()
                return
            if self.path == "/setup":
                self._handle_setup()
                return
            if self.path == "/validate":
                self._handle_validate()
                return
            self._send_text("not found\n", status=HTTPStatus.NOT_FOUND)
        except Exception as exc:  # pragma: no cover - exercised by Docker health failures.
            self._send_text(f"error: {exc}\n", status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def log_message(self, _format: str, *args: object) -> None:
        return

    def _handle_healthz(self) -> None:
        with _connect() as conn:
            conn.execute("SELECT 1")
        self._send_text("ok\n")

    def _handle_setup(self) -> None:
        with _connect() as conn:
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {_TABLE} (
                    id text PRIMARY KEY,
                    value text NOT NULL
                )
                ;
                TRUNCATE TABLE {_TABLE}
                """
            )
        self._send_text("setup ok\n")

    def _handle_validate(self) -> None:
        with _connect() as conn:
            rows = conn.execute(
                f"""
                INSERT INTO {_TABLE} (id, value)
                VALUES ('{_RECORD_ID}', '{_RECORD_VALUE}')
                ON CONFLICT (id) DO UPDATE SET value = EXCLUDED.value
                ;
                SELECT value FROM {_TABLE} WHERE id = '{_RECORD_ID}'
                """
            )
        if rows != [(_RECORD_VALUE,)]:
            raise RuntimeError(f"unexpected validation rows: {rows!r}")
        self._send_text(f"validated {_RECORD_ID}\n")

    def _send_text(self, body: str, *, status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main() -> None:
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
