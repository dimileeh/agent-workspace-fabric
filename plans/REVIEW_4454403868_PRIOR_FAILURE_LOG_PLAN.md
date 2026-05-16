# Review 4454403868 Prior Failure Log Plan

## Problem Statement And Scope

Address the remaining review-level feedback for PR comment `issue:4454403868`.
The current branch already has OpenAPI artifact assertions that verify
`GET /v1/callbacks` and `POST /v1/callbacks` expose the Authorization header
with `required: true`, so the scoped implementation gap is callback
delivery-budget observability when a timeout occurs after prior pinned-address
failures.

## Requirements Checklist

- Preserve existing OpenAPI authorization contract tests and avoid weakening
  runtime auth enforcement.
- Add a regression test proving `CALLBACK_DELIVERY_BUDGET_EXCEEDED` structured
  logs include prior pinned-address failure details when the timeout budget is
  exhausted before later validated addresses can be attempted.
- Implement the smallest service change that carries the already-computed
  address failure summary into the structured log.
- Keep persisted delivery error messages bounded and unchanged except where the
  new structured log field is added.
- Run the narrow tests that prove the callback logging and OpenAPI regression
  coverage.

## Implementation Steps

1. Add a focused callback service test for the timeout-with-prior-failures log
   path.
2. Extend `CallbackDeliveryBudgetExceededError` to optionally carry a redacted
   prior failure summary and populate it at the existing timeout raise site.
3. Include `prior_failure_summary` in the structured warning only when present.
4. Run targeted tests and the OpenAPI spec drift check if practical.
5. Record validation results in
   `plans/REVIEW_4454403868_PRIOR_FAILURE_LOG_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py tests/unit/api/test_openapi_artifact.py -q`
  passes.
- `python scripts/generate_openapi.py --check` passes, or the equivalent
  project-environment command passes if the bare container Python lacks project
  dependencies.
- Validation document marks all planned requirements `Complete`, or documents
  any explicit defer reason.
