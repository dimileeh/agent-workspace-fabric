# Callback DNS Shutdown Plan

## Problem Statement and Scope

PR thread `PRRT_kwDOSJAM6s6CRBgZ` reports that callback target DNS validation can still block process shutdown when `socket.getaddrinfo` is already running. The current callback validation pool is a standard `ThreadPoolExecutor`; `shutdown(wait=False)` does not stop running workers, and those workers can keep the process alive.

Scope is limited to the callback target validation executor and focused regression coverage for non-blocking shutdown behavior.

## Requirements Checklist

- Add a regression test showing `shutdown_callback_target_validation_executor(wait=False)` allows a process to exit even when a DNS validation worker is still running.
- Keep callback DNS work isolated from asyncio's default executor.
- Preserve lazy executor creation and shutdown reset behavior.
- Avoid weakening existing callback safety policy or delivery tests.
- Run the narrow relevant test surface after implementation.

## Implementation Steps

1. Add a failing regression in `tests/unit/service/test_callbacks.py` using a subprocess with a running callback DNS worker.
2. Replace the standard callback validation `ThreadPoolExecutor` with an executor whose running workers cannot keep the process alive after non-waiting shutdown.
3. Keep `submit()` and `shutdown(wait=..., cancel_futures=...)` semantics needed by `loop.run_in_executor`.
4. Re-run the targeted callback tests.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py -q`
- Pass criteria: the new shutdown regression passes, existing callback executor tests pass, and no callback service regressions are introduced.
