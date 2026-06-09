from __future__ import annotations

import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from redis_client import RedisClient  # noqa: E402

HEARTBEAT_KEY = "awf:redis-worker:heartbeat"


def _check_app() -> None:
    body = urllib.request.urlopen(
        "http://127.0.0.1:8080/healthz",
        timeout=5,
    ).read()
    if body != b"ok\n":
        raise RuntimeError(f"unexpected app health response: {body!r}")


def _check_worker() -> None:
    expected = os.environ.get("WORKER_ID", "redis-worker-fixture")
    value = RedisClient.from_url(os.environ["REDIS_URL"]).get(HEARTBEAT_KEY)
    if value != expected:
        raise RuntimeError(f"unexpected worker heartbeat: {value!r}")


def main() -> None:
    target = sys.argv[1] if len(sys.argv) > 1 else ""
    if target == "app":
        _check_app()
    elif target == "worker":
        _check_worker()
    else:
        raise RuntimeError(f"unknown healthcheck target: {target!r}")


if __name__ == "__main__":
    main()
