# PRRT_kwDOSJAM6s6COoWf Callback Timeout Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6COoWf_CALLBACK_TIMEOUT_PLAN.md`

## Requirement Status

- Add or update a regression test that proves target validation timeouts use a
  dedicated error code: Complete.
  `tests/unit/service/test_callbacks.py` now expects
  `CALLBACK_TARGET_VALIDATION_TIMEOUT` in the timeout log and stored delivery.
- Keep permanent target validation failures classified as
  `CALLBACK_TARGET_INVALID`: Complete.
  The existing `ValueError` path remains unchanged for invalid URL, allowlist,
  DNS, and private-address validation failures.
- Preserve existing retry/backoff behavior for timed-out target validation:
  Complete.
  Timeout classification still calls `mark_failed_or_retry` with the
  subscription's initial backoff and no response status code.
- Keep logged and stored error messages bounded and redacted through existing
  helpers: Complete.
  The timeout path uses `redact_audit_text` for logs and
  `_bounded_error_message` for stored delivery state.
- Avoid unrelated callback delivery behavior changes: Complete.
  Scope is limited to the target-validation timeout branch, one regression
  expectation, and REST API documentation of the new code.

## Evidence

Changed files:

- `src/awf/service/callbacks.py`
- `tests/unit/service/test_callbacks.py`
- `docs/REST_API_REFERENCE.md`
- `plans/PRRT_kwDOSJAM6s6COoWf_CALLBACK_TIMEOUT_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6COoWf_CALLBACK_TIMEOUT_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py -q -k validation_timeout`
  - First run failed before implementation with stored/logged
    `CALLBACK_TARGET_INVALID`.
  - Final run passed: `1 passed, 28 deselected`.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py -q`
  - Passed: `29 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/callbacks.py tests/unit/service/test_callbacks.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Passed.

## Remaining Gaps

None.
