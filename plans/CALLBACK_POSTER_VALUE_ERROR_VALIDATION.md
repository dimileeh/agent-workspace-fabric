# Callback Poster ValueError Validation

Plan reference: `CALLBACK_POSTER_VALUE_ERROR_PLAN.md`

## Requirement Status

- Complete: Added a regression test proving a poster-raised `ValueError` is recorded as
  `CALLBACK_REQUEST_FAILED`, not `CALLBACK_TARGET_INVALID`.
- Complete: Kept validation-raised `ValueError` behavior unchanged by leaving the
  existing target-policy handling intact.
- Complete: Split `CallbackDeliveryService.drain_due` exception handling so
  `ValueError` is caught only around `_validate_callback_target`.
- Complete: Ran focused callback service verification.

## Evidence

Files changed:

- `src/awf/service/callbacks.py`
- `tests/unit/service/test_callbacks.py`
- `plans/CALLBACK_POSTER_VALUE_ERROR_PLAN.md`
- `plans/CALLBACK_POSTER_VALUE_ERROR_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py::test_callback_poster_value_error_is_request_failure -q`
  - Failed before implementation with `CALLBACK_TARGET_INVALID`.
  - Passed after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py -q`
  - Passed: `19 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/callbacks.py tests/unit/service/test_callbacks.py`
  - Passed.

## Gaps

None.
