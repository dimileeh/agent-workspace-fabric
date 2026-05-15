# PRRT_kwDOSJAM6s6CQHoH Callback POST Wall Timeout Validation

Plan reference: `PRRT_kwDOSJAM6s6CQHoH_CALLBACK_POST_WALL_TIMEOUT_PLAN.md`

## Requirement Status

- Complete: Add a regression test showing each validated-address POST attempt is wrapped in the remaining wall-clock timeout.
  - Evidence: Added `test_validated_address_post_attempt_uses_remaining_wall_clock_timeout` in `tests/unit/service/test_callbacks.py`.
  - TDD evidence: The new focused test failed before implementation with `Failed: DID NOT RAISE <class 'TimeoutError'>`.
- Complete: Preserve the existing validated-address fallback behavior and error aggregation.
  - Evidence: The full callback test file passed after the change.
- Complete: Keep the default HTTPX poster API unchanged while continuing to pass the remaining timeout into HTTPX.
  - Evidence: `CallbackHttpPoster` and `_httpx_post_json` signatures are unchanged; `_post_to_validated_callback_addresses` still passes `timeout=remaining_timeout` into `poster`.
- Complete: Run the narrow callback unit tests that cover the changed behavior.
  - Evidence: `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py -q` passed.

## Commands Run

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py::test_validated_address_post_attempt_uses_remaining_wall_clock_timeout -q`
  - Failed before implementation as expected.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py::test_validated_address_post_attempt_uses_remaining_wall_clock_timeout -q`
  - Passed after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py -q`
  - Passed: 44 passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/callbacks.py tests/unit/service/test_callbacks.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Passed.

## Remaining Gaps

None.
