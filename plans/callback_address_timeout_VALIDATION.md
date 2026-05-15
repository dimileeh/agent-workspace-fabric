# Callback Address Timeout Validation

Plan reference: `plans/callback_address_timeout_PLAN.md`

## Requirement Status

- Add a regression test proving fallback attempts receive only the remaining timeout budget: Complete.
  - Evidence: `tests/unit/service/test_callbacks.py::test_validated_address_fallback_reuses_one_delivery_timeout_budget`.
  - Failing-first evidence: the test failed against the previous helper behavior with second-attempt timeouts `[10.0, 10.0]` instead of `[10.0, 3.75]`.
- Keep successful fallback across validated addresses working when prior addresses fail quickly: Complete.
  - Evidence: existing `test_successful_delivery_prefers_ipv4_then_falls_back_across_validated_callback_addresses` still passes in the full callback service unit file.
- Preserve current failure logging and exception notes for attempted addresses: Complete.
  - Evidence: existing `test_callback_request_failures_log_all_validated_address_failures` still passes in the full callback service unit file.
- Avoid changing callback registration, target validation, or repository behavior: Complete.
  - Evidence: implementation changes are limited to `_post_to_validated_callback_addresses`; tests are limited to callback service behavior.

## Verification

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py::test_validated_address_fallback_reuses_one_delivery_timeout_budget tests/unit/service/test_callbacks.py::test_validated_address_fallback_stops_when_timeout_budget_is_exhausted -q`
  - Result: passed, 2 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py -q`
  - Result: passed, 22 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/callbacks.py tests/unit/service/test_callbacks.py`
  - Result: passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Result: passed.

## Gaps

None.
