# Review Thread PRRT_kwDOSJAM6s6CLdGt Plan

## Problem Statement and Scope

`CallbackDeliveryService.drain_due` validates callback targets inline before
posting. The validation path resolves DNS through `socket.getaddrinfo`, so the
async drain loop can block the event loop while processing due callback
deliveries. Scope is limited to offloading target validation from the event loop
without changing callback target policy, delivery retry semantics, or sanitized
envelope behavior.

## Requirements Checklist

- Add a regression test proving `drain_due` dispatches callback target
  validation through `asyncio.to_thread`.
- Preserve existing invalid-target handling as `CALLBACK_TARGET_INVALID`.
- Preserve successful delivery behavior, including passing the validated connect
  IP address to the HTTP poster.
- Keep changes scoped to callback delivery service code, tests, and this plan
  workflow documentation.

## Implementation Steps

1. Add a focused unit test in `tests/unit/service/test_callbacks.py` that
   patches `asyncio.to_thread`, records the offloaded function and arguments,
   and confirms a due delivery still posts with the resolved connect IP.
2. Run the new test before implementation when practical to confirm it fails.
3. Import `asyncio` in `src/awf/service/callbacks.py` and offload
   `_validate_callback_target` with `await asyncio.to_thread(...)` inside
   `drain_due`.
4. Re-run the targeted callback service test and relevant static checks.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/callbacks.py tests/unit/service/test_callbacks.py`
  passes.
- `uv run --python 3.12 --extra dev mypy src/awf`
  passes, or any unrelated pre-existing failure is documented in validation.
