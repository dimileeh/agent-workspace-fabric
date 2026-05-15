# PRRT_kwDOSJAM6s6CMbrW Console URL Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6CMbrW` reports that a malformed configured
console URL, such as `http://localhost:badport`, can raise `httpx.InvalidURL`
from the default console probe and abort `awf smoke run`. The console probe is
optional diagnostic behavior, so malformed URLs should be treated like
unreachable console URLs and reported through the existing
`SMOKE_CONSOLE_UNAVAILABLE` warning path.

Scope is limited to the smoke console checker and its unit tests.

## Requirements Checklist

- Add a regression test proving `_default_console_checker()` returns `False`
  for malformed URLs that raise `httpx.InvalidURL`.
- Update the default console checker so `httpx.InvalidURL` is handled like
  other unreachable console failures.
- Preserve existing behavior for reachable URLs and HTTP error/status handling.
- Run the narrow smoke unit tests that cover the changed behavior.

## Implementation Steps

1. Add a failing unit test in `tests/unit/service/test_smoke.py` for malformed
   console URLs.
2. Run the focused test to confirm the current failure.
3. Update `src/awf/service/smoke.py` to catch `httpx.InvalidURL`.
4. Re-run the focused smoke tests and any narrow adjacent checks needed.
5. Record validation evidence in
   `plans/PRRT_kwDOSJAM6s6CMbrW_CONSOLE_URL_VALIDATION.md`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_smoke.py -q`
  must pass.
