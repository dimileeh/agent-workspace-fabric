# Callback URL Port Validation Validation

Plan reference: `plans/CALLBACK_URL_PORT_VALIDATION_PLAN.md`

## Requirement Status

- Complete: Add regression coverage for callback registration rejecting
  malformed and out-of-range target URL ports.
  - Evidence: `tests/unit/api/test_callbacks.py` now includes
    `https://operator.example.com:abc/events` and
    `https://operator.example.com:99999/events` in the invalid registration
    payload matrix.
- Complete: Add regression coverage for service-side callback target policy
  rejecting the same invalid ports before DNS or delivery.
  - Evidence: `tests/unit/service/test_callbacks.py` now asserts both invalid
    ports fail `_validate_callback_target` with `target_url must include a valid
    port`.
- Complete: Preserve existing valid callback URL behavior, including explicit
  valid ports.
  - Evidence: existing callback helper coverage still passes, including
    explicit `:8443` URL handling.
- Complete: Keep the change scoped to callback URL validation.
  - Evidence: implementation changes are limited to shared callback target port
    validation and the two callers that already validate callback URLs.

## Verification Evidence

- Confirmed failing regression before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py::test_register_callback_validates_url_events_and_extra_fields tests/unit/service/test_callbacks.py::test_validate_callback_target_rejects_unsafe_stored_url_invariants -q`
  failed for the malformed and out-of-range port cases.
- Passed after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py::test_register_callback_validates_url_events_and_extra_fields tests/unit/service/test_callbacks.py::test_validate_callback_target_rejects_unsafe_stored_url_invariants -q`
  with 17 passed.
- Passed:
  `uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py tests/unit/service/test_callbacks.py -q`
  with 111 passed.
- Passed:
  `uv run --python 3.12 --extra dev ruff check src/awf/common/callback_targets.py src/awf/api/schemas.py src/awf/service/callbacks.py tests/unit/api/test_callbacks.py tests/unit/service/test_callbacks.py`
- Passed:
  `uv run --python 3.12 --extra dev mypy src/awf`

## Remaining Gaps

None.
