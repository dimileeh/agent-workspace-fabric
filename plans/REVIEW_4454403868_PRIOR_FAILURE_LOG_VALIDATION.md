# Review 4454403868 Prior Failure Log Validation

Plan reference: `plans/REVIEW_4454403868_PRIOR_FAILURE_LOG_PLAN.md`

## Requirement Status

- Preserve existing OpenAPI authorization contract tests and avoid weakening
  runtime auth enforcement: Complete. Existing callback OpenAPI assertions were
  left intact and the targeted OpenAPI test module passed.
- Add a regression test proving `CALLBACK_DELIVERY_BUDGET_EXCEEDED` structured
  logs include prior pinned-address failure details: Complete.
  `test_delivery_budget_log_includes_prior_validated_address_failure_summary`
  fails without the service change and passes with it.
- Carry the already-computed address failure summary into the structured log:
  Complete. `CallbackDeliveryBudgetExceededError` now optionally stores the
  summary, and `_record_callback_delivery_budget_exceeded` emits a redacted
  `prior_failure_summary` field only when present.
- Keep persisted delivery error messages bounded and unchanged except for the
  new structured log field: Complete. The database update still uses the
  existing bounded error message path.
- Run narrow tests that prove callback logging and OpenAPI regression coverage:
  Complete.

## Evidence

Files changed:

- `src/awf/service/callbacks.py`
- `tests/unit/service/test_callbacks.py`
- `plans/REVIEW_4454403868_PRIOR_FAILURE_LOG_PLAN.md`
- `plans/REVIEW_4454403868_PRIOR_FAILURE_LOG_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py::test_delivery_budget_log_includes_prior_validated_address_failure_summary -q`
  passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py tests/unit/api/test_openapi_artifact.py -q`
  passed: 62 tests.
- `python scripts/generate_openapi.py --check` failed before app import because
  the container's bare Python environment did not have `fastapi` installed.
- `uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check`
  passed.
- `uv run --python 3.12 --extra dev ruff check src/awf tests` passed.
- `uv run --python 3.12 --extra dev mypy src/awf` passed.

## Gaps

None.
