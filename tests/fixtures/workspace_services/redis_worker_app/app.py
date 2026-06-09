from __future__ import annotations

import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from redis_client import RedisClient

JOB_ID = "awf-redis-worker-fixture"
QUEUE_KEY = "awf:redis-worker:queue"
RESULT_KEY = "awf:redis-worker:result"


def _redis() -> RedisClient:
    return RedisClient.from_url(os.environ["REDIS_URL"])


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        try:
            if self.path == "/healthz":
                self._handle_healthz()
                return
            if self.path == "/enqueue":
                self._handle_enqueue()
                return
            if self.path == "/status":
                self._handle_status()
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
        if _redis().ping() != "PONG":
            raise RuntimeError("Redis PING failed")
        self._send_text("ok\n")

    def _handle_enqueue(self) -> None:
        client = _redis()
        client.delete(RESULT_KEY, QUEUE_KEY)
        client.lpush(QUEUE_KEY, JOB_ID)
        self._send_text(f"enqueued {JOB_ID}\n")

    def _handle_status(self) -> None:
        result = _redis().get(RESULT_KEY)
        self._send_text(f"{result or 'pending'}\n")

    def _handle_validate(self) -> None:
        result = _redis().get(RESULT_KEY)
        expected = f"worker processed {JOB_ID}"
        if result != expected:
            raise RuntimeError(f"unexpected worker result: {result!r}")
        self._send_text(f"validated {result}\n")

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
