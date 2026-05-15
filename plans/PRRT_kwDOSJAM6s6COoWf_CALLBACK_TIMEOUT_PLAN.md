# PRRT_kwDOSJAM6s6COoWf Callback Timeout Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6COoWf` reports that callback target
validation timeouts are stored with `CALLBACK_TARGET_INVALID`, the same code as
permanently invalid callback URLs. The fix should preserve retry behavior while
making timeout failures distinguishable for operators and alerting.

Scope is limited to callback target validation timeout classification and its
focused tests/docs evidence.

## Requirements Checklist

- Add or update a regression test that proves target validation timeouts use a
  dedicated error code.
- Keep permanent target validation failures classified as
  `CALLBACK_TARGET_INVALID`.
- Preserve existing retry/backoff behavior for timed-out target validation.
- Keep logged and stored error messages bounded and redacted through existing
  helpers.
- Avoid unrelated callback delivery behavior changes.

## Implementation Steps

1. Update the existing callback target validation timeout test so it expects
   `CALLBACK_TARGET_VALIDATION_TIMEOUT` in logs and stored delivery state.
2. Confirm the updated test fails against the current code when practical.
3. Add a dedicated timeout exception or equivalent typed signal from
   `_validate_callback_target_with_timeout`.
4. Catch that signal in callback delivery and persist/log
   `CALLBACK_TARGET_VALIDATION_TIMEOUT` while using the same retry path and
   backoff as invalid-target failures.
5. Run focused callback tests plus narrow lint/type checks as time allows.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/callbacks.py tests/unit/service/test_callbacks.py`
  passes.
- Validation doc records requirement status and command evidence.
