from __future__ import annotations

import os
import urllib.request

EXPECTED = "validated worker processed awf-redis-worker-fixture\n"


def main() -> None:
    body = (
        urllib.request.urlopen(
            os.environ["APP_BASE_URL"] + "/validate",
            timeout=10,
        )
        .read()
        .decode()
    )
    assert body == EXPECTED, body
    print(body, end="")


if __name__ == "__main__":
    main()
