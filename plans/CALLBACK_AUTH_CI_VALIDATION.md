# Callback Auth CI Validation

Plan reference: `plans/CALLBACK_AUTH_CI_PLAN.md`

## Requirement Status

- Confirm the reported failing tests fail locally before editing: Complete.
  - Evidence: initial focused run returned `201` for callback registration and
    `200` for callback listing when those tests expected `401`.
- Preserve the callback routes' `require_api_token` protection: Complete.
  - Evidence: no production route code was changed; `src/awf/api/routes/callbacks.py`
    still declares `Depends(require_api_token)` for register and list.
- Make missing-token callback tests send requests without a default
  `Authorization` header: Complete.
  - Evidence: `tests/unit/api/test_callbacks.py` now uses
    `callback_app_and_client` for the missing-token tests.
- Keep invalid-token callback tests intact: Complete.
  - Evidence: invalid-token tests were not changed.
- Run focused verification for the callback auth tests and a reasonable callback
  API unit surface: Complete.
  - Evidence: commands below passed.

## Verification Evidence

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py::test_register_callback_requires_authorization_token tests/unit/api/test_callbacks.py::test_list_callbacks_requires_authorization_token -q`
  - Result: `2 passed in 9.01s`
- `uv run --python 3.12 --extra dev ruff check tests/unit/api/test_callbacks.py`
  - Result: `All checks passed!`
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py -q`
  - Result: `62 passed in 116.61s (0:01:56)`

## Remaining Gaps

None.
