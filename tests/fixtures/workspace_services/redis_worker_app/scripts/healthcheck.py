from __future__ import annotations

import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from redis_client import RedisClient  # noqa: E402

EXPECTED_WORKER_RESULT = "worker processed awf-redis-worker-fixture\n"


def _check_redis() -> None:
    pong = RedisClient.from_url(os.environ["REDIS_URL"]).ping()
    assert pong == "PONG", pong
    print("+PONG")


def _check_app() -> None:
    body = (
        urllib.request.urlopen(
            os.environ["APP_BASE_URL"] + "/healthz",
            timeout=10,
        )
        .read()
        .decode()
    )
    assert body == "ok\n", body
    print(body, end="")


def _check_worker() -> None:
    body = (
        urllib.request.urlopen(
            os.environ["WORKER_STATUS_URL"],
            timeout=10,
        )
        .read()
        .decode()
    )
    assert body == EXPECTED_WORKER_RESULT, body
    print(body, end="")


def main() -> None:
    target = sys.argv[1] if len(sys.argv) > 1 else ""
    if target == "redis":
        _check_redis()
    elif target == "app":
        _check_app()
    elif target == "worker":
        _check_worker()
    else:
        raise RuntimeError(f"unknown healthcheck target: {target!r}")


if __name__ == "__main__":
    main()
