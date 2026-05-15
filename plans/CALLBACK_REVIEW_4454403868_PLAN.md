# Callback Review 4454403868 Plan

## Problem Statement And Scope

Address the review-level PR comment for callback hardening. The actionable gap is
the delivery path that exhausts its timeout immediately after callback target DNS
validation: it currently falls through to the generic request failure handler
instead of recording `CALLBACK_TARGET_VALIDATION_TIMEOUT`. The registration-time
policy finding must be verified against existing code and tests before making
any change.

## Requirements Checklist

- Preserve AWF workspace constraints: stay on the current branch, do not push,
  and keep changes scoped.
- Add or update a regression test first for the post-validation timeout path so
  it expects `CALLBACK_TARGET_VALIDATION_TIMEOUT`.
- Change delivery behavior so a timeout budget exhausted by validation is
  handled by the dedicated validation timeout logging and persistence path.
- Verify whether `callbacks_require_https` and `callbacks_allowed_hosts` are
  already enforced at registration; only change code if the finding is still
  valid.
- Run the narrow callback tests that prove the changed behavior and relevant
  registration policy behavior.
- Record validation evidence in
  `plans/CALLBACK_REVIEW_4454403868_VALIDATION.md`.

## Implementation Steps

1. Inspect callback service, callback route, and callback tests.
2. Update the existing timeout-budget regression test to expect the dedicated
   validation timeout event and error code.
3. Run that targeted test and confirm it fails against the current
   implementation.
4. Replace the generic `TimeoutError` raised after validation consumes the
   budget with `CallbackTargetValidationTimeoutError`.
5. Run the targeted service test and the registration policy API tests.
6. Create the validation document with requirement status and command evidence.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py::test_drain_due_records_request_failure_when_validation_consumes_timeout_budget -q`
  - First run should fail after the test update and before implementation.
  - Final run should pass after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py::test_register_callback_rejects_http_target_when_https_required_without_insert tests/unit/api/test_callbacks.py::test_register_callback_rejects_non_allowlisted_target_without_insert -q`
  - Must pass, proving the registration policy finding is already covered.
