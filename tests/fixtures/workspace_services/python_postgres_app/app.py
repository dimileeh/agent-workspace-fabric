from __future__ import annotations

import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg

_TABLE = "awf_profile_fixture"
_RECORD_ID = "awf-db-profile-fixture"
_RECORD_VALUE = "db-backed validation ok"


def _database_url() -> str:
    value = os.environ.get("DATABASE_URL")
    if not value:
        raise RuntimeError("DATABASE_URL is required")
    return value


def _connect() -> psycopg.Connection[tuple[object, ...]]:
    return psycopg.connect(_database_url(), autocommit=True, connect_timeout=3)


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
        with _connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        self._send_text("ok\n")

    def _handle_setup(self) -> None:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {_TABLE} (
                    id text PRIMARY KEY,
                    value text NOT NULL
                )
                """
            )
            cur.execute(f"TRUNCATE TABLE {_TABLE}")
        self._send_text("setup ok\n")

    def _handle_validate(self) -> None:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {_TABLE} (id, value)
                VALUES (%s, %s)
                ON CONFLICT (id) DO UPDATE SET value = EXCLUDED.value
                """,
                (_RECORD_ID, _RECORD_VALUE),
            )
            cur.execute(f"SELECT value FROM {_TABLE} WHERE id = %s", (_RECORD_ID,))
            row = cur.fetchone()
        if row != (_RECORD_VALUE,):
            raise RuntimeError(f"unexpected validation row: {row!r}")
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
