from __future__ import annotations

import os
import signal
import time

from redis_client import RedisClient

JOB_ID = "awf-redis-worker-fixture"
QUEUE_KEY = "awf:redis-worker:queue"
RESULT_KEY = "awf:redis-worker:result"
HEARTBEAT_KEY = "awf:redis-worker:heartbeat"

_running = True


def _stop(_signum: int, _frame: object) -> None:
    global _running
    _running = False


def main() -> None:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    worker_id = os.environ.get("WORKER_ID", "redis-worker-fixture")
    client = RedisClient.from_url(os.environ["REDIS_URL"])
    while _running:
        client.set(HEARTBEAT_KEY, worker_id)
        item = client.brpop(QUEUE_KEY, 1)
        if item is None:
            continue
        _queue, payload = item
        if payload == JOB_ID:
            client.set(RESULT_KEY, f"worker processed {payload}")
        time.sleep(0.1)


if __name__ == "__main__":
    main()
