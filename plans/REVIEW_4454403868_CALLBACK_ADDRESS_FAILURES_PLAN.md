# Review 4454403868 Callback Address Failures Plan

## Problem Statement And Scope

The callback delivery fallback loop retries each validated callback target IP address, but when every address fails it raises only the final exception. Earlier address failures are absent from the structured request-failure traceback, which makes multi-address delivery failures harder to diagnose.

Scope is limited to the callback service multi-address delivery failure path and its unit coverage.

## Requirements Checklist

- Add a regression test proving that all per-address failures are preserved when every validated callback address fails.
- Preserve successful fallback behavior when a later validated address succeeds.
- Keep callback failure classification as `CALLBACK_REQUEST_FAILED`.
- Avoid logging or committing any secrets.

## Implementation Steps

1. Add a focused unit test in `tests/unit/service/test_callbacks.py` for a target resolving to multiple public addresses where every attempt raises a distinct exception.
2. Update `_post_to_validated_callback_addresses` in `src/awf/service/callbacks.py` to collect all per-address failures and raise an aggregate exception when all attempts fail.
3. Prefer a Python 3.12 `ExceptionGroup` so the existing redacted traceback logger records every underlying exception.
4. Run the narrow callback service tests, then lint the touched Python files.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/service/callbacks.py tests/unit/service/test_callbacks.py`

Both commands must pass.
