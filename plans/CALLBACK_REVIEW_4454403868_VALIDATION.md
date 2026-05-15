# Callback Review 4454403868 Validation

Plan reference: `plans/CALLBACK_REVIEW_4454403868_PLAN.md`

## Requirement Status

- Preserve AWF workspace constraints: Complete.
  - Stayed on the existing branch and did not push, rebase, or switch branches.
- Add or update a regression test first for the post-validation timeout path:
  Complete.
  - Updated
    `tests/unit/service/test_callbacks.py::test_drain_due_records_validation_timeout_when_validation_consumes_timeout_budget`
    to expect `CALLBACK_TARGET_VALIDATION_TIMEOUT`.
  - Confirmed the updated test failed before the service change.
- Change delivery behavior so a timeout budget exhausted by validation is
  handled by the dedicated validation timeout path: Complete.
  - `src/awf/service/callbacks.py` now raises
    `CallbackTargetValidationTimeoutError` when the delivery budget is exhausted
    immediately after target validation.
  - A shared helper records the dedicated timeout log event and delivery error
    code for both DNS-validation timeout and post-validation exhaustion paths.
- Verify whether `callbacks_require_https` and `callbacks_allowed_hosts` are
  already enforced at registration: Complete.
  - Existing route/service code enforces both settings during registration.
  - Existing API tests prove HTTP targets and non-allowlisted hosts are rejected
    before insert.
- Run narrow callback verification commands: Complete.

## Evidence

Files changed:

- `src/awf/service/callbacks.py`
- `tests/unit/service/test_callbacks.py`
- `plans/CALLBACK_REVIEW_4454403868_PLAN.md`
- `plans/CALLBACK_REVIEW_4454403868_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py::test_drain_due_records_validation_timeout_when_validation_consumes_timeout_budget -q`
  - Failed before implementation with no
    `callback.delivery_target_validation_timeout` log event.
  - Passed after implementation: `1 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py::test_register_callback_rejects_http_target_when_https_required_without_insert tests/unit/api/test_callbacks.py::test_register_callback_rejects_non_allowlisted_target_without_insert -q`
  - Passed: `2 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py::test_drain_due_marks_callback_target_validation_timeout_with_dedicated_code -q`
  - Passed: `1 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/callbacks.py tests/unit/service/test_callbacks.py tests/unit/api/test_callbacks.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Passed.

## Gaps

None.
